"""Background sweeper coroutines for the MCP gateway.

Split out of :mod:`kiro_crew.mcp_gateway.gatewayd` (LOC refactor). These are
the long-running housekeeping loops started by ``run_gatewayd``: idle
eviction, hot-key persistence, warm-pool top-up, credential-rotation
blue-green cutover, and per-backend heartbeat. Each takes its interval as a
parameter, so the daemon-level cadence constants stay in ``gatewayd``. Leaf
module — imports nothing from the rest of the ``mcp_gateway`` split.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from kiro_crew.mcp_gateway.pool import DRAIN_DEADLINE_SECS, BackendPool
from kiro_crew.mcp_gateway.prewarm import HotKeyStore

logger = logging.getLogger(__name__)


async def _idle_sweeper(
    pool: BackendPool,
    idle_timeout_secs: int,
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Periodically drop idle backends from ``pool`` until ``stop_event``
    is set. One sweep per ``interval`` seconds; sweeps themselves are
    non-blocking.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly
            except asyncio.TimeoutError:
                pass
            try:
                evicted = await pool.evict_idle(idle_timeout_secs)
                if evicted:
                    logger.debug("idle sweep evicted %d backends", evicted)
            except Exception:  # pragma: no cover — defensive
                logger.exception("idle sweep failed; continuing")
    except asyncio.CancelledError:
        pass


async def _hot_keys_flush_sweeper(
    hot_keys: HotKeyStore,
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Persist the hot-key tally once per ``interval`` until ``stop_event``
    is set. The write runs via :func:`asyncio.to_thread` so the blocking
    file IO never stalls the event loop — the on-loop path only ever
    mutates an in-memory dict. A flush that writes nothing (no new hits) is
    a cheap no-op inside :meth:`HotKeyStore.flush`.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly (final flush at shutdown)
            except asyncio.TimeoutError:
                pass
            try:
                wrote = await asyncio.to_thread(hot_keys.flush)
                if wrote:
                    logger.debug("hot-keys: flushed to %s", hot_keys.path)
            except Exception:  # pragma: no cover — defensive
                logger.exception("hot-keys flush failed; continuing")
    except asyncio.CancelledError:
        pass


async def _prewarm_topup_sweeper(
    schedule_prewarm: Callable[[], None],
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Re-warm the hot set once per ``interval`` until ``stop_event`` is set.

    Calls ``schedule_prewarm`` (a fire-and-forget scheduler), which runs an
    idempotent pass: a hot key whose backend is still pooled is reused at no
    cost, and one whose backend has died or been reclaimed is respawned. This
    keeps the warm set populated for the daemon's whole lifetime instead of
    only at startup. The scheduler itself is non-blocking, so the sweeper just
    sleeps between triggers.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly
            except asyncio.TimeoutError:
                pass
            try:
                schedule_prewarm()
            except Exception:  # pragma: no cover — defensive
                logger.exception("prewarm top-up scheduling failed; continuing")
    except asyncio.CancelledError:
        pass


async def _drain_and_rewarm_on_credential_change(
    pool: BackendPool,
    schedule_prewarm: Callable[[], None],
) -> None:
    """Handle a credential rotation via blue-green cutover: move ALL
    active backends (including in-use, refcount>0) to the draining list,
    then re-warm fresh backends with the new credential.

    Draining backends continue serving in-flight requests but are invisible
    to new acquires. The heartbeat sweeper reaps them when refcount drops to
    0 or the deadline expires, whichever first. New requests immediately cut
    over to fresh backends spawned with the rotated credential.

    If the drain itself raises, we deliberately skip the re-warm: stale
    backends may still be pooled, and re-warming would reuse + PIN them,
    making them harder to evict next cycle. Skipping leaves recovery to the
    next credential change or the top-up sweeper once they idle out.
    """
    try:
        # First evict truly idle backends (refcount==0) immediately — they
        # have no in-flight work and can be killed outright.
        idle_drained = await pool.evict_idle(0.0, include_pinned=True)
        # Move in-use backends (refcount>0) to the draining list for
        # blue-green cutover — they finish in-flight work then get reaped.
        moved = await pool.drain_all_to_bluegreen()
        logger.info(
            "credential file changed: blue-green cutover — evicted %d idle, "
            "moved %d in-use to draining (deadline=%ds)",
            idle_drained,
            moved,
            int(DRAIN_DEADLINE_SECS),
        )
    except Exception:
        logger.exception("credential-change blue-green cutover failed; skipping re-warm")
        return
    schedule_prewarm()


async def _heartbeat_sweeper(
    pool: BackendPool,
    interval: float,
    stop_event: asyncio.Event,
    backends_pidfile: Optional[Path] = None,
) -> None:
    """Probe every pooled backend's liveness once per ``interval`` and recycle
    any that are gone or wedged, until ``stop_event`` is set.

    For each backend, :meth:`Backend._heartbeat_once` classifies it:

    * ``"gone"`` / ``"wedged"`` -- the classify call has already errored every
      attached stub (via ``_broadcast_backend_gone``); the sweeper evicts the
      backend from the pool, shuts it down, and records the death against the
      circuit breaker so a crash loop trips it.
    * ``"alive"`` -- record a healthy signal that closes any OPEN breaker for
      the server.
    * ``"idle"`` -- left untouched; the idle sweeper owns eviction.

    The first sweep fires one full ``interval`` after startup, so short-lived
    runs (tests) never trigger the periodic logic.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly
            except asyncio.TimeoutError:
                pass
            try:
                now = time.monotonic()
                for key, backend in await pool.snapshot():
                    try:
                        state = await backend._heartbeat_once(now)
                    except Exception:  # pragma: no cover — defensive
                        logger.exception("heartbeat probe crashed for %s", key.human_readable())
                        continue
                    if state in ("gone", "wedged"):
                        pool.note_backend_death(key.stable_hash(), now - backend.created_at)
                        evicted = await pool.evict(key, expected=backend)
                        if evicted is not None:
                            with contextlib.suppress(Exception):
                                await evicted.shutdown(timeout=2.0)
                        logger.warning(
                            "heartbeat recycled %s backend pool=%s",
                            state,
                            key.human_readable(),
                        )
                    elif state == "alive":
                        pool.note_backend_healthy(key.stable_hash())
                # Reap draining backends (blue-green cutover) whose refcount
                # hit 0 or whose deadline expired.
                reaped = await pool.reap_draining()
                for backend in reaped:
                    logger.info(
                        "heartbeat reaped draining backend server=%s pid=%s "
                        "refcount=%d (credential-rotation cutover)",
                        backend.pool_key.server_name,
                        backend.pid,
                        backend.refcount,
                    )
                # Persist live backend pids out-of-band so the supervising
                # manager can killpg them if it must SIGKILL a wedged gatewayd
                # (which then never runs pool.shutdown_all()).
                if backends_pidfile is not None:
                    # Offload the file write: it is otherwise a synchronous
                    # open+write+close on the event loop (every other write in
                    # this module — _write_diagnostic, hot_keys.flush, socket
                    # probes — is offloaded via to_thread for the same reason).
                    pids = "\n".join(str(p) for p in pool.live_backend_pids())
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(backends_pidfile.write_text, pids)
            except Exception:  # pragma: no cover — defensive
                logger.exception("heartbeat sweep failed; continuing")
    except asyncio.CancelledError:
        pass
