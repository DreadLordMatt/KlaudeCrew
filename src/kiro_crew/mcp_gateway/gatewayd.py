"""Asyncio unix-socket server for the KiroCrew MCP gateway.

This module is the entry point for ``python -m
kiro_crew.mcp_gateway.gatewayd`` and for in-process use by
:class:`kiro_crew.mcp_gateway.manager.GatewayManager`.
The daemon wires the full bidirectional JSON-RPC pump on top of the
register skeleton:

* Register handshake (unchanged from M1) produces the :class:`PoolKey`.
* First non-register message triggers a lazy backend spawn through
  :meth:`BackendPool.get_or_create` — concurrent stubs with the same key
  share one backend, with spawn-dedup handled inside the pool.
* Stub→gateway pump reads line-delimited JSON-RPC and forwards through
  :meth:`Backend.forward_from_stub`, which handles id rewriting, caller-
  identity injection, and initialize caching.
* Gateway→stub pump drains the per-stub inbox queue populated by the
  backend's stdout task.
* Handshake phase has a timeout; the bridge phase is NOT timeout-wrapped
  (learned correction — a single timeout around the bridge silently kills
  healthy long-lived sessions).

Graceful shutdown: setting the ``stop_event`` stops accepts, drains
in-flight connection handlers up to ``_SHUTDOWN_DRAIN_SECS``, shuts the
pool down, and unlinks the socket before return. SIGTERM/SIGINT handlers
installed by the caller should just forward into ``stop_event.set()``.

LOC refactor: the connection-handling helpers, audit emitters, sweepers,
socket lifecycle, diagnostics, framing, peer-identity, claim/abort, metrics,
and CLI were extracted into sibling modules under ``kiro_crew.mcp_gateway``.
This module remains the daemon core (``run_gatewayd``, ``_handle_connection``,
and the backend acquire/respawn helpers) and re-exports the moved names so
``gatewayd.<symbol>`` access is preserved for importers and tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import credwatch, socketsec
from kiro_crew.mcp_gateway.audit import (
    _audit_abort_applied,
    _audit_caller_claimed,
    _audit_caller_rekey,
    _audit_peer_allowed,
    _audit_peer_denied,
    _audit_peer_identity_denied,
    _audit_peer_identity_resolved,
    _audit_pool_fallback,
    _audit_pool_rejected,
    _audit_prewarm_spawn,
    _audit_recaller_rejected,
)
from kiro_crew.mcp_gateway.backend import Backend, BackendGone, spawn_backend
from kiro_crew.mcp_gateway.breaker import CircuitBreaker
from kiro_crew.mcp_gateway.claim_abort import _apply_abort, _apply_claim
from kiro_crew.mcp_gateway.cli import (
    _DEFAULT_SOCKET_NAME,
    _DEFAULT_SOCKET_SUBDIR,
    _amain,
    _build_argparser,
    _default_cli_socket_path,
    main,
)
from kiro_crew.mcp_gateway.conn_registry import (
    _CONN_INDEX,
    _conn_index_add,
    _conn_index_discard,
    _register_pids,
    _StubConn,
)
from kiro_crew.mcp_gateway.diagnostics import (
    _ZOMBIE_PROBE_INTERVAL_SECS,
    _collect_task_stacks,
    _count_open_fds,
    _read_rss_kb,
    _snapshot_state,
    _write_diagnostic,
    _zombie_diagnostic,
    _zombie_diagnostic_path,
)
from kiro_crew.mcp_gateway.framing import (
    _MAX_FRAME_BYTES,
    _REGISTER_TIMEOUT_SECS,
    _WRITE_REPLY_TIMEOUT_SECS,
    _jsonrpc_error,
    _read_first_frame,
    _TargetUnknown,
    _write_json_line,
)
from kiro_crew.mcp_gateway.metrics import _emit_backend_acquire_metric, _emit_lazy_load_metrics
from kiro_crew.mcp_gateway.peer_identity import (
    _caller_from_register,
    _resolve_peer_identity,
    env_target_resolver,
)
from kiro_crew.mcp_gateway.pool import (
    READ_BUFFER_LIMIT_BYTES,
    BackendPool,
    BackendUnavailable,
    PoolAtCapacity,
    PoolKey,
)
from kiro_crew.mcp_gateway.prewarm import (
    HotKeyStore,
    default_hot_keys_path,
    prewarm_from_payloads,
)
from kiro_crew.mcp_gateway.socket_lifecycle import (
    _SINGLETON_LOCK_SUFFIX,
    _acquire_singleton_lock,
    _prepare_socket_dir,
    _probe_socket_live,
    _remove_stale_socket,
)
from kiro_crew.mcp_gateway.spill import cleanup_old_spill_files
from kiro_crew.mcp_gateway.sweepers import (
    _drain_and_rewarm_on_credential_change,
    _heartbeat_sweeper,
    _hot_keys_flush_sweeper,
    _idle_sweeper,
    _prewarm_topup_sweeper,
)
from kiro_crew.sel import SecurityEventLog

logger = logging.getLogger(__name__)

# Graceful-shutdown grace: in-flight connection handlers get this long to
# finish their current JSON-RPC round-trip before gatewayd cancels them
# and tears down the pool.
_SHUTDOWN_DRAIN_SECS = 10.0

# Interval between per-backend heartbeat sweeps. A backend
# that is gone, or wedged with an in-flight request outstanding past
# ``backend.HEARTBEAT_TIMEOUT_SECS``, is recycled on the next sweep. 60s
# balances recovery latency against ping overhead; the first sweep fires one
# interval after startup so short-lived runs (tests) never trigger it.
_HEARTBEAT_SWEEP_INTERVAL_SECS = 60.0

# Interval between credential-file change probes when one or more
# ``--credential-watch-path`` flags were supplied. On a content change,
# backends spawned with the stale credential are drained (blue-green
# cutover) so they respawn with the refreshed credential. The probe is a
# cheap stat (plus a hash only when mtime moved), so 30s keeps rotation
# latency low without measurable overhead. No flag ⇒ no watcher task.
_CREDENTIAL_WATCH_INTERVAL_SECS = 30.0

# Interval between hot-key persistence flushes when prewarming is enabled.
# Recording a register hit is O(1) in-memory; the actual disk write is
# batched onto this cadence and run via ``asyncio.to_thread`` so the event
# loop never blocks on IO. 30s bounds data loss on a hard kill to one
# interval of observation while keeping write volume negligible.
_HOT_KEYS_FLUSH_INTERVAL_SECS = 30.0

# Interval between warm-pool top-up passes when prewarming is enabled. A
# prewarmed backend can be lost between passes (it died, or was reclaimed under
# capacity pressure despite pinning if the cap was genuinely exhausted), so a
# periodic re-warm restores the hot set without waiting for the next restart.
# The pass is idempotent — a still-present backend is reused, not respawned —
# so this cadence only pays for backends that actually need re-warming. Set
# above the idle timeout so a healthy warm set is not needlessly re-checked too
# often, while still recovering a lost backend well within a few minutes.
_PREWARM_TOPUP_INTERVAL_SECS = 120.0

# --- Type aliases -----------------------------------------------------------

#: A ``target_resolver`` takes a :class:`PoolKey` and returns the
#: ``(command, args, env, work_dir)`` tuple used to spawn the backend, or
#: ``None`` if the server is unknown. The default resolver looks up
#: ``MC_MCP_TARGET_<SERVER>`` env vars (matches the Rust PoC and existing
#: rewriter wiring); tests inject their own resolver to avoid env-coupling.
TargetResolver = Callable[
    [PoolKey],
    Optional[tuple[str, list[str], dict[str, str], str]],
]


# --- Public API -------------------------------------------------------------


async def run_gatewayd(
    socket_path: Path | str,
    *,
    max_backends: int,
    idle_timeout_secs: int,
    stop_event: asyncio.Event,
    target_resolver: Optional[TargetResolver] = None,
    prewarm_count: int = 0,
    credential_watch_paths: Optional[list[Path]] = None,
) -> None:
    """Run the gateway until ``stop_event`` is set.

    Args:
        socket_path: Absolute path for the unix socket. Parent directories
            are created if missing; a stale socket left by a prior crash
            is removed before bind.
        max_backends: Pool capacity. When the pool is full and a new key
            arrives, :meth:`BackendPool.get_or_create` evicts the least-
            recently-used idle entry before spawning the new one.
        idle_timeout_secs: A backend whose stubs have all detached and
            whose ``last_used_at`` is older than this is evicted by the
            idle sweeper (runs every ``idle_timeout_secs / 4``, minimum
            500 ms).
        stop_event: Caller-owned event. Setting it triggers graceful
            shutdown: accept loop exits, in-flight handlers get
            ``_SHUTDOWN_DRAIN_SECS`` to finish, then everything cancels,
            the pool shuts down, and the socket is unlinked.
        target_resolver: Callable mapping :class:`PoolKey` to the spawn
            4-tuple ``(command, args, env, work_dir)``. Pass ``None`` to
            use the default :func:`env_target_resolver`. Tests supply a
            custom resolver to avoid coupling to environment variables.
        prewarm_count: Number of hottest observed PoolKeys to spawn at
            startup before the first stub connects, closing the
            cold-after-restart / cold-after-idle new-chat latency gap. The
            list of hot keys is learned from prior registers and persisted
            beside the socket in ``hot-keys.json``. ``0`` (default) disables
            prewarming entirely — no file is read or written, no extra task
            runs. Clamped to ``max_backends - 1`` if set at or above pool
            capacity, since prewarmed backends are pinned and would otherwise
            leave no reclaimable slot for a live, non-warm session.
        credential_watch_paths: Credential files to watch for content
            changes. On a real rotation (content digest change — a no-op
            rewrite with identical bytes never fires), ALL pooled backends
            are drained via a blue-green cutover so they respawn with the
            fresh credential, then the warm pool is re-warmed. ``None`` or
            empty (the public default) creates no watcher task — the run
            flow is byte-identical to the pre-watcher daemon. The paths are
            caller-supplied (typically threaded through the seam-resolved
            ``--credential-watch-path`` argv flags); the daemon never
            hardcodes or interprets any credential path.

    The function never raises on normal shutdown. Startup failures (e.g.
    socket directory not creatable, another daemon already bound to the
    path) propagate so the caller can surface a clear error.
    """
    socket_path = Path(socket_path)
    _prepare_socket_dir(socket_path)
    # Singleton guard (race-free): acquire an exclusive advisory flock on a
    # lockfile beside the socket BEFORE probing/unlinking/binding. Without it,
    # two daemons that start in the same instant both pass the connect-probe
    # in _remove_stale_socket, both unlink+bind, and the later bind silently
    # steals the socket from the earlier — leaving the earlier daemon
    # orphaned-but-listening. Repeated, this leaks N daemons on one socket
    # path and splits stub<->backend routing across them, surfacing to
    # kiro-cli as intermittent "transport closed". The flock lets exactly one
    # daemon win; losers exit cleanly below. The kernel releases the lock on
    # process death, so there is no stale-lock mode.
    lock_fd = _acquire_singleton_lock(socket_path)
    if lock_fd is None:
        logger.warning(
            "gatewayd: another instance already owns %s — exiting without "
            "binding (singleton guard)",
            socket_path,
        )
        return
    await _remove_stale_socket(socket_path)

    resolver = target_resolver if target_resolver is not None else env_target_resolver
    # Shared circuit breaker keyed by server name: a server
    # that crash-loops on spawn trips OPEN and get_or_create rejects further
    # spawns so the stub falls back to per-session exec instead of churning.
    breaker = CircuitBreaker()
    pool = BackendPool(max_backends=max_backends, breaker=breaker)
    connections: set[asyncio.Task[None]] = set()

    # Clamp prewarm_count below pool capacity. Prewarmed backends are pinned —
    # exempt from the idle sweeper and LRU eviction — so prewarming every slot
    # would leave no reclaimable capacity for a live stub whose key isn't in the
    # warm set, and get_or_create would raise PoolAtCapacity for real sessions.
    # Reserve at least one unpinned slot. (A misconfigured prewarm_count must
    # never be able to starve live traffic.)
    if prewarm_count > 0 and prewarm_count >= max_backends:
        clamped = max(0, max_backends - 1)
        logger.warning(
            "prewarm_count=%d >= max_backends=%d would pin the whole pool; "
            "clamping to %d to reserve capacity for live sessions",
            prewarm_count,
            max_backends,
            clamped,
        )
        prewarm_count = clamped

    # Hot-key store powers warm-pool prewarming. Only instantiated when
    # prewarming is enabled; otherwise ``None`` and the record path is a
    # no-op so the default (disabled) build pays nothing.
    hot_keys: Optional[HotKeyStore] = (
        HotKeyStore(default_hot_keys_path(socket_path)) if prewarm_count > 0 else None
    )

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        try:
            await _handle_connection(reader, writer, pool, resolver, socket_path, hot_keys)
        except asyncio.CancelledError:
            # Normal on shutdown — propagate for the gather() below.
            raise
        except Exception:
            logger.exception("connection handler crashed")
        finally:
            if task is not None:
                connections.discard(task)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _on_client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # asyncio.start_unix_server's callback isn't async; spawn the real
        # handler as a tracked task so shutdown can cancel it. Any
        # exception raised here (rare — create_task and set.add only fail
        # under resource exhaustion) would otherwise propagate into
        # asyncio's server internals and wedge the accept loop silently.
        # Explicit try/except + exception-level log keeps those failures
        # attributable.
        try:
            task = asyncio.create_task(_handle(reader, writer))
            connections.add(task)
        except Exception:
            logger.exception(
                "accept callback crashed while spawning handler; " "closing connection"
            )
            try:
                writer.close()
            except Exception:
                pass

    # --- Resource-guarded startup block ---
    # The flock (lock_fd) and the bound unix socket are acquired/created
    # below. If ANY step between bind and the main await-stop_event raises
    # (EADDRINUSE from start_unix_server, chmod failure, a create_task OOM),
    # the finally block ensures both the flock and the socket file are
    # released/unlinked — preventing a leaked flock that blocks restart and
    # a dangling socket that confuses the next startup probe.
    server: Optional[asyncio.base_events.Server] = None
    sweeper: Optional[asyncio.Task[None]] = None
    diagnostic: Optional[asyncio.Task[None]] = None
    heartbeat: Optional[asyncio.Task[None]] = None
    flush_sweeper: Optional[asyncio.Task[None]] = None
    topup_sweeper: Optional[asyncio.Task[None]] = None
    credential_watchers: list[asyncio.Task[None]] = []
    prewarm_tasks: set[asyncio.Task[None]] = set()
    _prewarm_lock = asyncio.Lock()  # serialize passes so unpin sees latest state

    try:
        # Windows: not yet supported — AF_UNIX / start_unix_server (and the
        # SO_PEERCRED peer check below) are POSIX-only; a TCP-loopback or named-pipe
        # abstraction is needed. The MCP gateway is opt-in and OFF by default, so this
        # is no parity loss at launch. Tracked in Mesh-2364
        # (https://taskei.amazon.dev/tasks/Mesh-2364).
        server = await asyncio.start_unix_server(
            _on_client_connected,
            path=str(socket_path),
            limit=READ_BUFFER_LIMIT_BYTES,
        )
        # Socket hardening: tighten the freshly-bound socket to
        # 0600 so only the owning uid can connect. Defense-in-depth on top of the
        # 0700 $KIROCREW_HOME directory; the per-connection SO_PEERCRED check in
        # _handle_connection is the second layer.
        socketsec.chmod_socket_0600(socket_path)
        # Mesh-2861: clean up stale spill files from prior runs (older than 24h).
        try:
            await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(), cleanup_old_spill_files
            )
        except Exception:  # pragma: no cover — defensive
            logger.debug("spill cleanup failed at startup", exc_info=True)
        logger.info(
            "gatewayd listening socket=%s max_backends=%d idle_timeout=%ds",
            socket_path,
            max_backends,
            idle_timeout_secs,
        )

        # Idle sweeper — wakes every ``idle_timeout_secs / 4`` (bounded to
        # 500 ms minimum) and evicts any backend whose stubs have all detached
        # and whose ``last_used_at`` is past the deadline.
        sweep_interval = max(0.5, float(idle_timeout_secs) / 4.0)
        sweeper = asyncio.create_task(
            _idle_sweeper(pool, idle_timeout_secs, sweep_interval, stop_event),
            name="mcp-gateway-idle-sweeper",
        )

        # Zombie diagnostic: probes
        # ``server.is_serving()`` every 30 s and dumps a post-mortem JSONL on
        # divergence. Costs ~0 in the healthy case; captures the cause of
        # accept-loop death on the first zombie event.
        diagnostic = asyncio.create_task(
            _zombie_diagnostic(server, pool, connections, stop_event),
            name="mcp-gateway-zombie-diagnostic",
        )

        # Per-backend heartbeat sweep: recycle gone/wedged
        # backends and feed the circuit breaker. First sweep fires one interval
        # after startup.
        heartbeat = asyncio.create_task(
            _heartbeat_sweeper(
                pool,
                _HEARTBEAT_SWEEP_INTERVAL_SECS,
                stop_event,
                backends_pidfile=Path(f"{socket_path}.backends"),
            ),
            name="mcp-gateway-heartbeat-sweeper",
        )

        # Warm-pool prewarming (optional): persist observed hot keys and keep the
        # hottest backends warm. All prewarm tasks are background tasks created
        # AFTER the socket is listening, so none delays the daemon becoming
        # reachable. Disabled (hot_keys is None) => no prewarm task is created and
        # the record/IO paths are no-ops.
        #
        # The warm set is kept ready by three triggers, all routed through the same
        # idempotent pass (a backend already in the pool is reused by the acquire
        # path, so re-running is cheap and self-healing):
        #   (a) once at startup,
        #   (b) a periodic top-up sweeper that re-warms any hot key whose backend
        #       has since died or been reclaimed under capacity pressure, and
        #   (c) after a credential-cookie refresh, so a freshly-rotated credential is
        #       baked into the warm backends before the next chat attaches.

        async def _run_prewarm_pass(*, initial: bool = False) -> None:
            # Warm the top-N hottest keys through the same acquire path live stubs
            # use. Fully best-effort: any failure leaves the daemon serving lazily.
            #
            # Disk is loaded ONLY on the initial startup pass. Re-loading on every
            # top-up / cookie-rewarm would overwrite the live in-memory tally with
            # the last-flushed snapshot -- regressing hit/miss counters and any keys
            # observed since the last flush (up to one flush interval of loss). The
            # running store already holds the freshest observations, so subsequent
            # passes read straight from memory.
            #
            # Serialized via _prewarm_lock so overlapping passes (startup vs top-up
            # vs cookie-refresh) never race on pin/unpin -- the unpin loop always
            # reflects the most recently warmed set.
            assert hot_keys is not None  # guarded by the caller
            async with _prewarm_lock:
                try:
                    if initial:
                        await asyncio.to_thread(hot_keys.load)
                    payloads = hot_keys.top_register_payloads(prewarm_count)
                    if not payloads:
                        logger.info("prewarm: no hot keys yet — nothing to warm")
                        return

                    async def _acquire(pool_key: PoolKey) -> Backend:
                        # Audit only a REAL spawn (not a pool reuse) so the SEL log
                        # reports actual out-of-handshake subprocess creations 1:1.
                        #
                        # Gate on ``was_spawned`` — set inside the pool's per-key
                        # create lock — NOT a racy ``pool.get()`` pre-check. A
                        # pooled backend can die or be evicted (idle/LRU/heartbeat
                        # sweep, capacity pressure) between a pre-check and the
                        # acquire, turning a "reuse" into a real spawn whose audit
                        # a pre-check would silently skip.
                        backend, was_spawned = await _acquire_backend(pool, pool_key, resolver)
                        if was_spawned:
                            _audit_prewarm_spawn(pool_key.human_readable())
                        return backend

                    await prewarm_from_payloads(
                        payloads,
                        _acquire,
                        limit=prewarm_count,
                        unreserve=pool.unreserve,
                    )

                    # Unpin backends whose key fell out of the current top-N so
                    # the idle sweeper can reclaim them. Prevents unbounded pin
                    # accumulation across hot-set drift and config_snapshot_hash
                    # changes (only the CURRENT top-N stays pinned).
                    current_top_digests = {
                        PoolKey.from_register(p).stable_hash() for p in payloads[:prewarm_count]
                    }
                    for pool_key, backend in await pool.snapshot():
                        if (
                            getattr(backend, "pinned", False)
                            and pool_key.stable_hash() not in current_top_digests
                        ):
                            backend.pinned = False
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover -- defensive
                    logger.exception("prewarm pass failed; serving lazily")

        def _schedule_prewarm(*, initial: bool = False) -> None:
            """Fire-and-forget one warm pass, tracked so shutdown can cancel it.
            ``initial=True`` loads persisted hot keys from disk (startup only).
            No-op when prewarming is disabled."""
            if hot_keys is None:
                return
            task = asyncio.create_task(
                _run_prewarm_pass(initial=initial), name="mcp-gateway-prewarm"
            )
            prewarm_tasks.add(task)
            task.add_done_callback(prewarm_tasks.discard)

        if hot_keys is not None:
            flush_sweeper = asyncio.create_task(
                _hot_keys_flush_sweeper(hot_keys, _HOT_KEYS_FLUSH_INTERVAL_SECS, stop_event),
                name="mcp-gateway-hot-keys-flush",
            )
            topup_sweeper = asyncio.create_task(
                _prewarm_topup_sweeper(_schedule_prewarm, _PREWARM_TOPUP_INTERVAL_SECS, stop_event),
                name="mcp-gateway-prewarm-topup",
            )
            # (a) Warm once at startup -- initial=True loads persisted hot keys.
            _schedule_prewarm(initial=True)

        # Credential-rotation drain: on a content change of any watched
        # credential file, drain ALL pooled backends (blue-green cutover) so
        # they respawn with the fresh credential, then re-warm. Watcher tasks
        # exist ONLY when the caller supplied watch paths — the public default
        # (no paths) creates no task and the run flow is byte-identical.
        async def _on_credential_change() -> None:
            await _drain_and_rewarm_on_credential_change(pool, _schedule_prewarm)

        for cred_path in credential_watch_paths or []:
            credential_watchers.append(
                asyncio.create_task(
                    credwatch.watch_credential(
                        cred_path,
                        _CREDENTIAL_WATCH_INTERVAL_SECS,
                        stop_event,
                        _on_credential_change,
                        logger,
                    ),
                    name="mcp-gateway-credential-watcher",
                )
            )

        await stop_event.wait()
    finally:
        logger.info("gatewayd shutting down (connections=%d)", len(connections))
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

        # Phase 1: let in-flight handlers drain cleanly. ``return_exceptions``
        # because a handler that was already errored will raise from the
        # gather; that's not a shutdown failure.
        if connections:
            drain_deadline = time.monotonic() + _SHUTDOWN_DRAIN_SECS
            while connections and time.monotonic() < drain_deadline:
                await asyncio.sleep(0.05)

        # Phase 2: cancel whatever is still in-flight.
        for task in list(connections):
            task.cancel()
        if connections:
            await asyncio.gather(*connections, return_exceptions=True)
        connections.clear()

        if sweeper is not None:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sweeper

        if diagnostic is not None:
            diagnostic.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await diagnostic

        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat

        if topup_sweeper is not None:
            topup_sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await topup_sweeper

        for watcher in credential_watchers:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        credential_watchers.clear()

        # Cancel any in-flight warm passes (startup / top-up / credential-triggered)
        # so a slow handshake cannot stall shutdown.
        for task in list(prewarm_tasks):
            task.cancel()
        if prewarm_tasks:
            await asyncio.gather(*prewarm_tasks, return_exceptions=True)
        prewarm_tasks.clear()

        if flush_sweeper is not None:
            flush_sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await flush_sweeper

        # Final flush so the last observation window isn't lost on a clean
        # shutdown. Off the loop; best-effort (we're tearing down anyway).
        if hot_keys is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(hot_keys.flush)

        await pool.shutdown_all()
        # Clean shutdown drained every backend; drop the out-of-band reap list
        # so a supervising manager never killpg's now-dead pids.
        with contextlib.suppress(OSError):
            Path(f"{socket_path}.backends").unlink()

        # Only unlink the socket WE bound. On the EADDRINUSE path a foreign
        # live daemon already owns it (server stays None, _remove_stale_socket
        # deliberately refused to remove the live socket) — unlinking here
        # would delete the running daemon's socket and send every stub to
        # per-session fallback. Mirror the ``server.close()`` guard above.
        if server is not None:
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("could not unlink gateway socket %s: %s", socket_path, exc)
        # Release the singleton flock (the kernel also releases it on process
        # death; this is the clean-path release).
        with contextlib.suppress(OSError):
            os.close(lock_fd)
        logger.info("gatewayd stopped")


# --- Connection handling ----------------------------------------------------


async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    pool: BackendPool,
    resolver: TargetResolver,
    socket_path: Path,
    hot_keys: Optional[HotKeyStore] = None,
) -> None:
    """Process one stub connection end-to-end.

    Phases:

    1. **Health probe** (optional): a client may send ``{"type": "ping"}``
       as its first frame. The gateway replies ``{"type": "pong"}`` and
       closes — used by :class:`GatewayManager` to confirm the daemon is
       serving before returning from ``start()``.
    2. **Handshake** (bounded by ``_REGISTER_TIMEOUT_SECS``): read the
       Register message, build the :class:`PoolKey`, reply with a
       Registered envelope containing a provisional ``backend_id``
       (the real backend is spawned lazily on the first MCP message —
       keeps idle stubs from pinning a backend).
    3. **Bridge** (no timeout wrapper — learned correction): stub frames
       go into :meth:`Backend.forward_from_stub`; a concurrent writer
       task drains the stub's inbox queue populated by the backend's
       stdout pump. Exits on any of: stub EOF, backend death, shutdown
       cancellation.
    """
    # Socket hardening: deny-by-default peer-uid check on every
    # platform. Where the platform can read SO_PEERCRED (Linux), reject any
    # connection whose peer uid is not a positively-confirmed MATCH (both a
    # MISMATCH and an UNVERIFIABLE socket-level failure fail closed). Where
    # SO_PEERCRED is structurally unavailable (e.g. macOS), peer-uid cannot be
    # read, so rather than silently proceeding we positively verify the
    # filesystem access gate -- the 0600 socket mode that already prevents any
    # other uid from connecting -- and fail closed if it has been loosened.
    if socketsec.PEERCRED_SUPPORTED:
        peer_result = socketsec.check_peer_uid(writer, os.getuid())
        if peer_result is not socketsec.PeerCredResult.MATCH:
            logger.warning(
                "rejecting gateway connection: peer uid not confirmed (%s)",
                peer_result.value,
            )
            _audit_peer_denied(f"peer uid not confirmed ({peer_result.value})")
            return
    else:
        if not socketsec.socket_owner_only(socket_path):
            logger.warning(
                "rejecting gateway connection: peer uid unverifiable on this "
                "platform and socket %s is not owner-only (0600)",
                socket_path,
            )
            _audit_peer_denied(f"peer uid unverifiable and socket not owner-only: {socket_path}")
            return
        logger.debug(
            "peer uid unverifiable on this platform; socket %s verified "
            "owner-only, proceeding on the filesystem gate",
            socket_path,
        )
    register = await _read_first_frame(reader)
    if register is None:
        logger.debug("stub disconnected before first frame")
        return

    # Health-probe short-circuit: any caller can check gatewayd is alive
    # with one round-trip without advertising a PoolKey. GatewayManager
    # uses this to confirm the daemon is serving before returning from
    # ``start()``.
    if register.get("type") == "ping":
        await _write_json_line(writer, {"type": "pong"})
        return

    # Metrics short-circuit: return a point-in-time pool snapshot (backends,
    # sessions, RSS) for the dashboard metrics panel. Read-only, no PoolKey.
    # When prewarming is enabled, fold in the cumulative warm-pool hit tally
    # so the dashboard can show a hit rate; absent (hot_keys is None) the keys
    # simply don't appear and the card omits the metric.
    if register.get("type") == "stats":
        snapshot = await pool.metrics_snapshot_async()
        if hot_keys is not None:
            snapshot.update(hot_keys.hit_stats())
        await _write_json_line(writer, {"type": "stats", **snapshot})
        return

    # Claim-push short-circuit (one-shot control connection from the main
    # gateway process): "session S now owns runtime PID P" — re-target the
    # caller identity of every live stub connection under that PID. This is
    # the event-driven replacement for the stub-side recaller poll, whose
    # bounded budget stranded pool runtimes claimed later than the budget.
    # Trust basis: the unix socket is uid-gated 0700 — the same gate that
    # authenticates Register — so a claim may REPLACE a stale identity
    # (fixes warm-pool re-claim staleness). Validation + auditing live in
    # ``_apply_claim``.
    if register.get("type") == "claim":
        await _write_json_line(writer, _apply_claim(register))
        return

    # Abort-push short-circuit (one-shot control connection from the main
    # gateway process): "cancel all in-flight tool calls for runtime PIDs X"
    # — sends MCP notifications/cancelled to each backend. Backend recycle
    # happens on the subsequent stub disconnect path, not here. Trust basis:
    # same uid-gated 0700 socket as Register/Claim.
    if register.get("type") == "abort":
        await _write_json_line(writer, await _apply_abort(register, pool))
        return

    if register.get("type") not in (None, "register"):
        logger.warning(
            "stub first frame has type=%r, want 'register' or 'ping'",
            register.get("type"),
        )
        return

    try:
        pool_key = PoolKey.from_register(register)
    except ValueError as exc:
        await _write_json_line(
            writer,
            {"type": "rejected", "reason": f"malformed Register: {exc}"},
        )
        logger.warning("rejected Register: %s", exc)
        return

    stub_uuid = str(register.get("stub_uuid", ""))
    if not stub_uuid:
        await _write_json_line(
            writer,
            {"type": "rejected", "reason": "missing stub_uuid"},
        )
        logger.warning("rejected Register: missing stub_uuid")
        return

    caller = _caller_from_register(register)

    # Server-side peer identity: when the stub self-reports an empty
    # session_key, resolve it from the peer's REAL pid (SO_PEERCRED) via a
    # host-side /proc ancestry walk — and capture the host ancestor chain for
    # claim indexing below. Deny-by-default: never grant an identity (nor
    # index host pids) without the kernel positively attesting the peer uid.
    resolved_session_key = ""
    peer_host_pids: list[int] = []
    if caller is None or not caller.session_key:
        peer_pid = socketsec.get_peer_pid(writer)
        peer_uid_ok = socketsec.check_peer_uid(writer, os.getuid())
        if peer_pid is None or peer_uid_ok is not socketsec.PeerCredResult.MATCH:
            _audit_peer_identity_denied(
                reason=(
                    "no peer pid (SO_PEERCRED unavailable)"
                    if peer_pid is None
                    else f"peer uid not positively verified ({peer_uid_ok.name})"
                ),
                peer_pid=peer_pid,
                stub_uuid=stub_uuid,
            )
        else:
            try:
                # subprocess_executor: a /proc read can block indefinitely on
                # a D-state target; isolate it from the default pools.
                resolved_session_key, peer_host_pids = (
                    await asyncio.get_running_loop().run_in_executor(
                        subprocess_executor(), _resolve_peer_identity, peer_pid
                    )
                )
            except Exception:  # graceful degradation: identity stays empty
                logger.exception(
                    "peer identity resolution failed for peer_pid=%d", peer_pid
                )
                resolved_session_key, peer_host_pids = "", []
            if resolved_session_key:
                caller = CallerContext(
                    session_key=resolved_session_key,
                    session_type="peer-resolved",
                    principal_id=str(
                        register.get("principal_id") or register.get("user_identity") or ""
                    ),
                    channel_id=str(register.get("channel_id") or ""),
                    from_gateway=True,
                )
                _audit_peer_identity_resolved(resolved_session_key, peer_pid, stub_uuid)
                logger.info(
                    "peer-resolved session_key for stub %s via peer_pid=%d",
                    stub_uuid, peer_pid,
                )

    # Claim-push index: record the runtime process tree that owns this stub
    # so a ``claim`` frame naming ANY level of that tree re-targets every
    # connection of the claimed runtime. Best-effort — stubs that send no
    # usable PIDs simply keep the recaller-poll fallback.
    #
    # The stub's self-reported ``ancestor_pids`` can be namespace-local
    # (sandbox PID-namespace topology) and then never match a claim frame's
    # HOST pid, so merge in the host-side ancestor chain resolved from the
    # SO_PEERCRED peer pid (empty when peer creds were not positively
    # verified — deny-by-default preserved).
    stub_pids = _register_pids(register)
    indexed_pids = stub_pids + [p for p in peer_host_pids if p not in stub_pids]
    conn = _StubConn(
        stub_uuid, indexed_pids, pool_key.human_readable(), caller
    )
    _conn_index_add(conn)

    # Provisional backend_id: the real pid isn't known until the backend
    # spawns. Using the pool digest gives operators a stable grep key that
    # ties together every stub sharing the same backend even before spawn.
    provisional_id = f"pending-{pool_key.stable_hash()[:12]}"
    await _write_json_line(
        writer,
        {
            "type": "registered",
            "backend_id": provisional_id,
            "pool_label": pool_key.human_readable(),
            # Capability advertisement: lets a new stub detect a
            # new gateway and run the ensure_backend pre-flight. Absent on an
            # old gateway, so the new stub skips the pre-flight (no 25s skew
            # penalty) and falls back to the legacy lazy-spawn path.
            "capabilities": ["ensure_backend"],
        },
    )
    logger.info(
        "registered stub_uuid=%s pool=%s",
        stub_uuid,
        pool_key.human_readable(),
    )
    # Accepting an identified stub is a permission decision; record it in the
    # SEL alongside the denial path so the audit trail covers both outcomes.
    _audit_peer_allowed(caller.session_key if caller else "", pool_key.human_readable())

    # Warm-pool observation: tally this accepted register so the hottest
    # PoolKeys can be prewarmed on the next startup. In-memory only here —
    # O(1), no IO — so it never slows the handshake; persistence is batched
    # by the flush sweeper. ``None`` when prewarming is disabled.
    if hot_keys is not None:
        hot_keys.record(register)
        # Hit-rate metric: a warm backend already pooled for this key (from a
        # prewarm or a prior chat) is a HIT; otherwise this register will fall
        # through to a lazy spawn below — a MISS. ``get`` is a non-mutating
        # lookup, so reading it here does not pin or alter the backend.
        hot_keys.record_outcome(hit=await pool.get(pool_key) is not None)

    # Bridge phase — ensure any attach is undone even if we bail early.
    backend: Optional[Backend] = None
    inbox: Optional["asyncio.Queue[bytes]"] = None
    writer_task: Optional[asyncio.Task[None]] = None
    # Per-connection write serialization. The outbound pump
    # (_drain_inbox_to_stub) and the forward loop's direct error replies both
    # write to this one StreamWriter; two concurrent writer.drain() calls trip a
    # CPython assert in _drain_helper and tear the transport down. Every
    # write+drain path acquires this lock (looked up off the writer).
    setattr(writer, "_mc_write_lock", asyncio.Lock())
    # Captured ``initialize`` frame for this connection. Stashed the first
    # time kiro-cli sends it so the transparent-respawn path can re-prime a
    # freshly spawned backend (kiro-cli never re-sends initialize after a
    # backend dies). Persists across warm-pool rekey since the stub process
    # — and this coroutine — outlive a single chat.
    captured_init: Optional[dict[str, Any]] = None
    try:
        while True:
            try:
                line = await reader.readuntil(b"\n")
            except asyncio.IncompleteReadError:
                return
            except asyncio.LimitOverrunError:
                logger.warning(
                    "stub %s frame exceeded %d bytes; dropping conn", stub_uuid, _MAX_FRAME_BYTES
                )
                return
            if not line:
                return
            if len(line) > _MAX_FRAME_BYTES:
                logger.warning("stub %s frame too large (%d bytes); dropping", stub_uuid, len(line))
                return
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                logger.warning("stub %s sent non-JSON frame: %s", stub_uuid, exc)
                continue
            if not isinstance(msg, dict):
                logger.warning("stub %s sent non-object frame; dropping", stub_uuid)
                continue
            # Claim-push pickup: a concurrent ``claim`` connection may have
            # re-targeted this connection's identity via ``conn.caller``.
            # Sync per-frame so the very next forward carries the new caller.
            caller = conn.caller
            if msg.get("type") == "unregister":
                logger.info("stub %s sent Unregister; closing", stub_uuid)
                return

            # Warm-pool caller repair: a stub that registered key-less (its
            # kiro-cli was pool-spawned before the session was claimed) sends
            # this once its session key materializes. Update the caller used
            # for subsequent forwards so ``_meta.kirocrew.caller`` carries the
            # real identity — without it, pooled state-mutating tools see an
            # empty session key. Never forwarded to the backend. An empty /
            # malformed key yields ``None`` from ``_caller_from_register`` and
            # is ignored, so a bad recaller can never clobber a good caller.
            if msg.get("type") == "recaller":
                # Deny-by-default: the ONLY permitted transition is a key-less
                # connection adopting a valid session key. Compute the current
                # identity up front, reject every non-permitted case with an
                # explicit ``continue``, and accept only on positive
                # confirmation of that one transition (the final branch) — any
                # unexpected state falls through to rejection, not acceptance.
                # Never forwarded to the backend. Legit warm-pool stubs only
                # ever send a recaller when their Register was key-less, so this
                # never blocks the intended path.
                existing_key = caller.session_key if caller is not None else ""
                if existing_key:
                    # Connection already carries an identity — reject the pivot
                    # (a compromised stub must not re-bind to another session).
                    attempted = _caller_from_register(msg)
                    attempted_key = attempted.session_key if attempted is not None else "<none>"
                    logger.warning(
                        "stub %s sent recaller but caller already set "
                        "(session_key=%s); ignoring",
                        stub_uuid,
                        existing_key,
                    )
                    _audit_recaller_rejected(
                        existing_key,
                        pool_key.human_readable(),
                        f"recaller pivot attempt to session_key={attempted_key}",
                    )
                    continue
                updated = _caller_from_register(msg)
                if updated is None or not updated.session_key:
                    # Empty/malformed identity claim — reject and audit so ALL
                    # recaller outcomes land on the SEL trail, not just pivots.
                    logger.warning(
                        "stub %s sent recaller with no usable session_key; ignoring",
                        stub_uuid,
                    )
                    _audit_recaller_rejected(
                        "",
                        pool_key.human_readable(),
                        "recaller frame with empty/malformed session_key",
                    )
                    continue
                # Positive confirmation: key-less connection + valid recaller
                # key — the one allowed transition. Audit the identity change.
                caller = updated
                conn.caller = updated
                _audit_caller_rekey(caller.session_key, pool_key.human_readable())
                logger.info(
                    "stub %s recaller → session_key=%s type=%s",
                    stub_uuid,
                    caller.session_key,
                    caller.session_type,
                )
                continue

            # B1 pre-flight: the stub sends ``ensure_backend``
            # before forwarding any real MCP frame. Spawning (or reusing)
            # the backend here — instead of lazily on the first real frame —
            # means a capacity / circuit-breaker rejection reaches the stub
            # BEFORE kiro-cli's ``initialize`` is consumed, so the stub can
            # fall back to a clean per-session exec (the unread ``initialize``
            # is still in its stdin). This control frame is never forwarded
            # downstream to the backend.
            if msg.get("type") == "ensure_backend":
                if backend is None:
                    _acquire_t0 = time.monotonic()
                    try:
                        backend, _was_spawned = await _acquire_backend(pool, pool_key, resolver)
                        # acquire-only duration, captured before the attach_stub
                        # + create_task overhead so the metric stays true to name.
                        _acquire_ms = (time.monotonic() - _acquire_t0) * 1000.0
                    except _TargetUnknown as exc:
                        _audit_pool_rejected(
                            caller.session_key if caller else "",
                            pool_key.human_readable(),
                            str(exc),
                        )
                        await _write_json_line(writer, {"type": "rejected", "reason": str(exc)})
                        return
                    except (BackendUnavailable, PoolAtCapacity) as exc:
                        logger.info(
                            "ensure_backend rejected (fallback-eligible) for %s: %s",
                            pool_key.human_readable(),
                            exc,
                        )
                        _audit_pool_fallback(
                            caller.session_key if caller else "",
                            pool_key.human_readable(),
                            str(exc),
                        )
                        await _write_json_line(
                            writer,
                            {"type": "rejected", "reason": str(exc), "fallback": True},
                        )
                        return
                    except OSError as exc:
                        # Spawn / fork failure (ENOMEM, EAGAIN, ENOENT, or a
                        # jail/pool-specific env mismatch). It may be transient
                        # or specific to the pooled spawn path, so a direct
                        # per-session exec can still succeed -- tag it
                        # fallback-eligible rather than dropping the server's
                        # tools for the whole session.
                        logger.warning(
                            "ensure_backend spawn failed (fallback-eligible) for %s: %s",
                            pool_key.human_readable(),
                            exc,
                        )
                        _audit_pool_fallback(
                            caller.session_key if caller else "",
                            pool_key.human_readable(),
                            f"spawn failed: {exc}",
                        )
                        await _write_json_line(
                            writer,
                            {
                                "type": "rejected",
                                "reason": f"backend spawn failed: {exc}",
                                "fallback": True,
                            },
                        )
                        return
                    except Exception as exc:
                        # Unexpected gateway-internal error (NOT an OS spawn
                        # failure) -- terminal, not fallback-eligible: surface it
                        # rather than masking a gateway bug behind an unpooled
                        # exec on every session.
                        logger.exception(
                            "ensure_backend internal error for %s",
                            pool_key.human_readable(),
                        )
                        _audit_pool_rejected(
                            caller.session_key if caller else "",
                            pool_key.human_readable(),
                            f"internal error: {exc}",
                        )
                        await _write_json_line(
                            writer,
                            {"type": "rejected", "reason": f"internal error: {exc}"},
                        )
                        return
                    # Attach BEFORE replying ``ready`` so the stub can never
                    # forward a frame before its inbox exists.
                    try:
                        inbox = await backend.attach_stub(stub_uuid)
                    finally:
                        # Release the hand-out reservation; once attached
                        # refcount>0 keeps the backend from eviction.
                        pool.unreserve(pool_key)
                    writer_task = asyncio.create_task(
                        _drain_inbox_to_stub(inbox, writer, stub_uuid),
                        name=f"mcp-gateway-stub-writer-{stub_uuid[:8]}",
                    )
                    # OTEL metric: acquire-only duration (captured above, before
                    # attach_stub + create_task overhead).
                    _emit_backend_acquire_metric(_acquire_ms, warm=not _was_spawned)
                await _write_json_line(writer, {"type": "ready"})
                continue

            # Lazy backend spawn on first forwarded message. The pool
            # dedups concurrent first-attaches so even if two stubs race
            # into this block at the same tick they share one backend.
            if backend is None:
                _lazy_t0 = time.monotonic()
                try:
                    backend, _lazy_was_spawned = await _acquire_backend(pool, pool_key, resolver)
                    # acquire/spawn-only duration, captured before the attach +
                    # create_task overhead.
                    _lazy_elapsed_ms = (time.monotonic() - _lazy_t0) * 1000.0
                except _TargetUnknown as exc:
                    _audit_pool_rejected(
                        caller.session_key if caller else "",
                        pool_key.human_readable(),
                        str(exc),
                    )
                    await _write_json_line(
                        writer,
                        {
                            "type": "rejected",
                            "reason": str(exc),
                        },
                    )
                    return
                except (BackendUnavailable, PoolAtCapacity) as exc:
                    # Legacy lazy-spawn path: only pre-ensure_backend stubs
                    # reach here, and they have already forwarded a real frame,
                    # so a fallback exec would lose it — NOT tagged
                    # fallback-eligible. New stubs pre-flight via ensure_backend.
                    logger.info(
                        "lazy-spawn rejected for %s: %s",
                        pool_key.human_readable(),
                        exc,
                    )
                    _audit_pool_rejected(
                        caller.session_key if caller else "",
                        pool_key.human_readable(),
                        str(exc),
                    )
                    await _write_json_line(
                        writer,
                        {
                            "type": "rejected",
                            "reason": str(exc),
                        },
                    )
                    return
                except Exception as exc:
                    logger.exception("backend spawn failed for %s", pool_key.human_readable())
                    _audit_pool_rejected(
                        caller.session_key if caller else "",
                        pool_key.human_readable(),
                        f"spawn failed: {exc}",
                    )
                    await _write_json_line(
                        writer,
                        {
                            "type": "rejected",
                            "reason": f"backend spawn failed: {exc}",
                        },
                    )
                    return
                try:
                    inbox = await backend.attach_stub(stub_uuid)
                finally:
                    pool.unreserve(pool_key)
                writer_task = asyncio.create_task(
                    _drain_inbox_to_stub(inbox, writer, stub_uuid),
                    name=f"mcp-gateway-stub-writer-{stub_uuid[:8]}",
                )
                # OTEL metrics: lazy-load count + duration + acquire duration
                # (elapsed captured above, before attach + task overhead).
                _emit_lazy_load_metrics(_lazy_elapsed_ms, warm=not _lazy_was_spawned)

            # Stash the initialize frame so a transparent respawn can re-prime
            # a fresh backend without kiro-cli re-sending initialize.
            if msg.get("method") == "initialize":
                captured_init = dict(msg)

            try:
                await backend.forward_from_stub(stub_uuid, msg, caller=caller)
            except BackendGone as exc:
                # Transparent respawn: a shared backend dying must NOT brick
                # this stub's transport (which would make kiro-cli mark the
                # MCP server dead for the whole session AND poison the warm
                # pool for new tabs). Rebuild a fresh backend, re-prime its
                # handshake from the captured initialize, re-attach this stub,
                # and fail ONLY this one in-flight request with a retryable
                # error. The transport stays open, so the next call self-heals.
                recovered = await _respawn_backend_for_stub(
                    pool,
                    pool_key,
                    resolver,
                    stub_uuid,
                    writer,
                    captured_init,
                    backend,
                    inbox,
                    writer_task,
                )
                if recovered is None:
                    # Genuinely unrecoverable (no captured init, circuit
                    # breaker open / capacity, or prime failed): fall back to
                    # the terminal error so the stub can do a clean
                    # per-session exec rather than churn against a dead server.
                    await _write_json_line(writer, _jsonrpc_error(msg, f"backend gone: {exc}"))
                    return
                backend, inbox, writer_task = recovered
                # Fail only this in-flight request; kiro-cli retries it on the
                # now-healthy transport. A duplicate error for this id from the
                # dying backend's broadcast is harmless — clients dedupe by id.
                if isinstance(msg, dict) and "method" in msg and msg.get("id") is not None:
                    await _write_json_line(
                        writer,
                        _jsonrpc_error(msg, f"backend restarted mid-call, retry: {exc}"),
                    )
                continue
            except Exception as exc:  # pragma: no cover — defensive
                logger.exception("forward_from_stub failed for %s", stub_uuid)
                await _write_json_line(writer, _jsonrpc_error(msg, f"forward failed: {exc}"))
                return
    finally:
        _conn_index_discard(conn)
        if backend is not None:
            # Scope A: before detaching, cancel any in-flight tool calls this
            # stub owned — the backend would otherwise run them to completion
            # with no consumer (the root cause of the stop/kill bug).
            # Best-effort: a failure here must never skip detach_stub below,
            # or the backend's refcount leaks and it can never be recycled.
            had_in_flight = any(
                p.stub_uuid == stub_uuid for p in backend._pending_requests.values()
            )
            cancelled: list = []
            try:
                cancelled = await backend.cancel_in_flight_for_stub(stub_uuid)
            except Exception:
                logger.warning(
                    "cancel_in_flight_for_stub failed for %s",
                    stub_uuid,
                    exc_info=True,
                )
            remaining = await backend.detach_stub(stub_uuid)
            if cancelled:
                logger.info(
                    "stub %s detached with %d in-flight request(s) %s -> cancelled; refcount=%d",
                    stub_uuid,
                    len(cancelled),
                    cancelled[:5],
                    remaining,
                )
                # SEL audit: cancelling in-flight tool work on a plain stub
                # disconnect is the same security-relevant action as the abort
                # frame path (which audits via _audit_abort_applied) — record
                # it so a disconnect-triggered cancellation has an audit trail.
                try:
                    SecurityEventLog().log_api_access(
                        caller="gatewayd",
                        operation="mcp-gateway.disconnect-cancel",
                        outcome="cancelled",
                        source="gateway",
                        resources=f"stub={stub_uuid} refcount={remaining}",
                        error=f"cancelled={len(cancelled)} in-flight on stub disconnect",
                    )
                except Exception:  # pragma: no cover — audit must never break detach
                    logger.debug("SEL audit for disconnect-cancel failed", exc_info=True)
            else:
                logger.debug("stub %s detached; refcount=%d", stub_uuid, remaining)
            # Scope B: if no consumers remain and the backend had in-flight
            # work, kill+respawn (the cancel notification is best-effort —
            # the backend may not honour it).
            if remaining == 0 and had_in_flight:
                await backend.recycle_if_idle()
            # Scope B: if quarantined and now drained, recycle
            elif remaining == 0 and backend.quarantined:
                await backend.recycle_if_idle()
        if writer_task is not None:
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer_task


async def _acquire_backend(
    pool: BackendPool,
    pool_key: PoolKey,
    resolver: TargetResolver,
) -> tuple[Backend, bool]:
    """Return ``(backend, was_spawned)`` for ``pool_key`` — spawning one via
    the resolver if absent.

    ``was_spawned`` is ``True`` iff THIS call actually created a new
    subprocess (the ``_spawn`` closure ran), ``False`` on a pool reuse. It is
    set inside ``pool.get_or_create`` under the per-key create lock, so it is
    the authoritative, race-free signal of a real spawn — callers can gate a
    spawn-only SEL audit on it without a racy ``pool.get()`` pre-check.

    Raises :class:`_TargetUnknown` when the resolver has no mapping for the
    server (a clean rejection, not a crash).
    """
    target = resolver(pool_key)
    if target is None:
        raise _TargetUnknown(
            f"no target mapping for server {pool_key.server_name!r}; "
            "set MC_MCP_TARGET_<SERVER> env var or pass a target_resolver"
        )
    command, args, env, work_dir = target

    was_spawned = False

    async def _spawn() -> Backend:
        # Runs only when the pool creates a new backend (guarded by the
        # per-key create lock), so this flag reports a real spawn 1:1.
        nonlocal was_spawned
        was_spawned = True
        backend = await spawn_backend(
            pool_key=pool_key,
            command=command,
            args=list(args),
            env=dict(env),
            work_dir=work_dir,
        )
        # Start the stdout pump immediately so replies to the first
        # forwarded message can route back. The task is owned by the
        # Backend and cancelled at shutdown().
        backend._stdout_task = asyncio.create_task(
            backend.run_stdout_pump(),
            name=f"mcp-gateway-backend-stdout-{backend.pid}",
        )
        return backend

    backend = await pool.get_or_create(pool_key, _spawn)
    return backend, was_spawned


async def _respawn_backend_for_stub(
    pool: BackendPool,
    pool_key: PoolKey,
    resolver: TargetResolver,
    stub_uuid: str,
    writer: asyncio.StreamWriter,
    captured_init: Optional[dict[str, Any]],
    old_backend: Backend,
    old_inbox: Optional["asyncio.Queue[bytes]"],
    old_writer_task: Optional[asyncio.Task[None]],
) -> Optional[tuple[Backend, "asyncio.Queue[bytes]", asyncio.Task[None]]]:
    """Rebuild a fresh backend for ``stub_uuid`` after its shared backend
    died and re-bind this stub to it transparently.

    Returns ``(new_backend, new_inbox, new_writer_task)`` on success, or
    ``None`` when recovery is impossible / undesirable (no captured
    initialize to replay, circuit breaker open, capacity, or the prime
    handshake failed) — the caller then falls back to the terminal error so
    the stub can do a clean per-session exec instead of the gateway churning
    spawns against a broken backend.

    Never re-forwards the in-flight request itself: a ``tools/call`` may have
    executed on the old backend before it died, so replaying it could
    double-execute a non-idempotent tool. The caller fails just that one
    request with a retryable error instead.
    """
    # Stop the old inbox drain first so it cannot race the new writer task
    # onto the same socket, then flush whatever the dying backend already
    # broadcast (errors for other in-flight requests of this stub) so
    # kiro-cli does not hang waiting on those ids.
    if old_writer_task is not None:
        old_writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await old_writer_task
    if old_inbox is not None:
        _lock = getattr(writer, "_mc_write_lock", None)
        _guard: Any = _lock if _lock is not None else contextlib.nullcontext()
        with contextlib.suppress(Exception):
            async with _guard:
                while True:
                    try:
                        payload = old_inbox.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    writer.write(payload)
                # Bounded: a stub that stopped reading during the respawn flush
                # must not pin this handler forever (the outer suppress cannot
                # catch a hang). Mirrors _write_json_line's bounded drain.
                await asyncio.wait_for(writer.drain(), timeout=_WRITE_REPLY_TIMEOUT_SECS)

    with contextlib.suppress(Exception):
        await old_backend.detach_stub(stub_uuid)

    if captured_init is None:
        # Never saw an initialize on this connection — a fresh backend cannot
        # be made usable without replaying it. Give up (terminal).
        logger.info(
            "respawn give-up (no captured initialize) stub=%s pool=%s",
            stub_uuid,
            pool_key.human_readable(),
        )
        return None

    try:
        new_backend, _ = await _acquire_backend(pool, pool_key, resolver)
    except (_TargetUnknown, BackendUnavailable, PoolAtCapacity, OSError) as exc:
        logger.info(
            "respawn give-up (acquire rejected) stub=%s pool=%s: %s",
            stub_uuid,
            pool_key.human_readable(),
            exc,
        )
        return None
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "respawn acquire crashed stub=%s pool=%s",
            stub_uuid,
            pool_key.human_readable(),
        )
        return None

    # _acquire_backend reserved the pool key; release it on every path below
    # (attached -> refcount>0 guards it; bailed -> let the sweeper reclaim it).
    # Without this the reserved digest is skipped by evict_idle/LRU forever,
    # leaking a pool slot for every key that ever mid-call respawned.
    try:
        try:
            await new_backend.prime_initialize(captured_init)
        except BackendGone as exc:
            logger.info(
                "respawn give-up (prime failed) stub=%s pool=%s: %s",
                stub_uuid,
                pool_key.human_readable(),
                exc,
            )
            return None
        new_inbox = await new_backend.attach_stub(stub_uuid)
    finally:
        pool.unreserve(pool_key)
    new_writer_task = asyncio.create_task(
        _drain_inbox_to_stub(new_inbox, writer, stub_uuid),
        name=f"mcp-gateway-stub-writer-{stub_uuid[:8]}",
    )
    logger.info(
        "transparent respawn: stub=%s rebound to fresh backend pid=%s pool=%s",
        stub_uuid,
        new_backend.pid,
        pool_key.human_readable(),
    )
    return new_backend, new_inbox, new_writer_task


async def _drain_inbox_to_stub(
    inbox: "asyncio.Queue[bytes]",
    writer: asyncio.StreamWriter,
    stub_uuid: str = "",
) -> None:
    """Forward every payload queued by the backend into the stub writer.

    Each payload is already a complete newline-terminated JSON frame built
    by :meth:`Backend._deliver_to_stub`. Exits on writer error (stub
    disconnected) or task cancellation at shutdown.
    """
    lock = getattr(writer, "_mc_write_lock", None)
    try:
        while True:
            payload = await inbox.get()
            guard: Any = lock if lock is not None else contextlib.nullcontext()
            try:
                async with guard:
                    writer.write(payload)
                    await asyncio.wait_for(writer.drain(), timeout=_WRITE_REPLY_TIMEOUT_SECS)
            except (ConnectionError, BrokenPipeError):
                # Scope E: log late responses dropped after stub detach
                # instead of letting BrokenPipeError propagate unlogged.
                logger.info(
                    "stub %s: response arrived after disconnect — dropped "
                    "(%d bytes); this is expected during session stop",
                    stub_uuid or "unknown",
                    len(payload),
                )
                return
            except asyncio.TimeoutError:
                # Stub passed the handshake but stopped reading; don't pin this
                # writer task (and its connection handler + fd) indefinitely.
                return
    except asyncio.CancelledError:
        raise


# --- Backward-compatible public surface -------------------------------------
# gatewayd.py was split into helper submodules (LOC refactor). ``__all__``
# declares the module's public API AND marks the re-exported helpers (imported
# above purely so ``gatewayd.<symbol>`` and the test suite keep working) as
# used, so linters do not flag them. Names are grouped by their new home.
__all__ = [
    # daemon core (defined here)
    "run_gatewayd",
    "_handle_connection",
    "_acquire_backend",
    "_respawn_backend_for_stub",
    "_drain_inbox_to_stub",
    "TargetResolver",
    "_SHUTDOWN_DRAIN_SECS",
    "_HEARTBEAT_SWEEP_INTERVAL_SECS",
    "_CREDENTIAL_WATCH_INTERVAL_SECS",
    "_HOT_KEYS_FLUSH_INTERVAL_SECS",
    "_PREWARM_TOPUP_INTERVAL_SECS",
    # metrics
    "_emit_backend_acquire_metric",
    "_emit_lazy_load_metrics",
    # framing
    "_MAX_FRAME_BYTES",
    "_REGISTER_TIMEOUT_SECS",
    "_WRITE_REPLY_TIMEOUT_SECS",
    "_jsonrpc_error",
    "_TargetUnknown",
    "_read_first_frame",
    "_write_json_line",
    # audit
    "_audit_peer_denied",
    "_audit_peer_allowed",
    "_audit_caller_rekey",
    "_audit_recaller_rejected",
    "_audit_caller_claimed",
    "_audit_peer_identity_resolved",
    "_audit_peer_identity_denied",
    "_audit_abort_applied",
    "_audit_pool_fallback",
    "_audit_pool_rejected",
    "_audit_prewarm_spawn",
    # conn_registry
    "_StubConn",
    "_CONN_INDEX",
    "_register_pids",
    "_conn_index_add",
    "_conn_index_discard",
    # peer_identity
    "env_target_resolver",
    "_resolve_peer_identity",
    "_caller_from_register",
    # sweepers
    "_idle_sweeper",
    "_hot_keys_flush_sweeper",
    "_prewarm_topup_sweeper",
    "_drain_and_rewarm_on_credential_change",
    "_heartbeat_sweeper",
    # socket_lifecycle
    "_prepare_socket_dir",
    "_acquire_singleton_lock",
    "_remove_stale_socket",
    "_probe_socket_live",
    "_SINGLETON_LOCK_SUFFIX",
    # diagnostics
    "_zombie_diagnostic",
    "_zombie_diagnostic_path",
    "_count_open_fds",
    "_read_rss_kb",
    "_collect_task_stacks",
    "_snapshot_state",
    "_write_diagnostic",
    "_ZOMBIE_PROBE_INTERVAL_SECS",
    # claim_abort
    "_apply_claim",
    "_apply_abort",
    # cli
    "main",
    "_amain",
    "_build_argparser",
    "_default_cli_socket_path",
    "_DEFAULT_SOCKET_SUBDIR",
    "_DEFAULT_SOCKET_NAME",
]


if __name__ == "__main__":
    main()
