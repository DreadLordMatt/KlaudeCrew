"""Zombie-detection diagnostics for the MCP gateway daemon.

Split out of :mod:`kiro_crew.mcp_gateway.gatewayd` (LOC refactor).

Chronic post-M5 issue: gatewayd's accept coroutine has been observed to
exit silently every ~2-3 h on the dev soak. The existing heartbeat only
proves the heartbeat task itself is alive; it does not prove the server
is still accepting connections. The diagnostic task below closes that
gap: it polls ``server.is_serving()`` and, on divergence from the
expected "serving while stop_event unset" invariant, dumps a full
post-mortem to a JSONL side-channel so the next event has a root-cause
paper trail.

Leaf module — imports nothing from the rest of the ``mcp_gateway`` split.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from kiro_crew.mcp_gateway.pool import BackendPool

logger = logging.getLogger(__name__)

# Interval between diagnostic snapshots. A 30 s sample rate catches the
# ~90 s window between zombie death and watchdog kill without generating
# excessive log volume in the healthy case.
_ZOMBIE_PROBE_INTERVAL_SECS = 30.0


def _zombie_diagnostic_path() -> Path:
    """Return the JSONL file path that receives zombie post-mortems.

    Lives next to the soak/gatewayd logs under
    ``$KIROCREW_HOME/logs/gatewayd_zombie_diagnostic.jsonl`` so a single
    ``tail -f`` follows both heartbeat (gatewayd.log) and any detected
    zombie state.
    """
    mc_home = os.environ.get("KIROCREW_HOME") or os.path.expanduser("~/.kirocrew")
    return Path(mc_home) / "logs" / "gatewayd_zombie_diagnostic.jsonl"


def _count_open_fds() -> int:
    """Return the number of open file descriptors for this process.

    FD exhaustion is one of the four hypothesised zombie causes; tracking
    the count per snapshot lets us confirm or eliminate that path without
    deploying a separate tracer.
    """
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def _read_rss_kb() -> int:
    """Return RSS in kilobytes from ``/proc/self/status`` or ``-1``."""
    try:
        with open("/proc/self/status", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return -1


def _collect_task_stacks() -> list[dict[str, Any]]:
    """Snapshot every live asyncio task with name + current stack.

    Used on zombie detection — gives the post-mortem enough context to
    tell whether a specific coroutine (backend pump, stub handler, idle
    sweeper) wedged the event loop versus an external cause (FD leak,
    blocking syscall, etc.).
    """
    out: list[dict[str, Any]] = []
    for task in asyncio.all_tasks():
        frames: list[str] = []
        try:
            for frame in task.get_stack(limit=10):
                frames.append(
                    "{}:{} in {}".format(
                        frame.f_code.co_filename,
                        frame.f_lineno,
                        frame.f_code.co_name,
                    )
                )
        except Exception:  # pragma: no cover — defensive
            frames = ["<stack unavailable>"]
        out.append(
            {
                "name": task.get_name(),
                "done": task.done(),
                "cancelled": task.cancelled(),
                "stack": frames,
            }
        )
    return out


def _snapshot_state(
    *,
    server: Optional[asyncio.base_events.Server],
    pool: BackendPool,
    connections: set[asyncio.Task[None]],
    task_count: int,
) -> dict[str, Any]:
    """Gather a single health sample used by the diagnostic loop."""
    is_serving: Optional[bool]
    try:
        is_serving = bool(server.is_serving()) if server is not None else None
    except Exception:
        is_serving = None
    return {
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts_epoch": time.time(),
        "is_serving": is_serving,
        "task_count": task_count,
        "fd_count": _count_open_fds(),
        "rss_kb": _read_rss_kb(),
        "pool_size": len(pool._backends),  # type: ignore[attr-defined]
        "connections_in_flight": len(connections),
    }


def _write_diagnostic(path: Path, record: dict[str, Any]) -> None:
    """Append one JSONL line to the diagnostic side-channel.

    Never raises — the diagnostic task is defensive enough that a missing
    directory or EROFS on the log volume must not crash gatewayd itself.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:  # pragma: no cover — defensive
        logger.warning("zombie diagnostic write failed: %s", exc)


async def _zombie_diagnostic(
    server: asyncio.base_events.Server,
    pool: BackendPool,
    connections: set[asyncio.Task[None]],
    stop_event: asyncio.Event,
) -> None:
    """Polling watchdog that captures accept-loop death.

    Every :data:`_ZOMBIE_PROBE_INTERVAL_SECS` seconds:

    1. Collect a health snapshot via :func:`_snapshot_state`.
    2. Append the snapshot to the diagnostic JSONL under the ``probe`` tag
       so there is a continuous baseline to correlate against.
    3. If ``server.is_serving()`` is ``False`` while ``stop_event`` is
       still unset, the accept loop has died silently — dump every live
       task stack, log at error level, and set ``stop_event`` so the
       process exits cleanly and the watchdog respawns us.
    """
    diag_path = _zombie_diagnostic_path()
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_ZOMBIE_PROBE_INTERVAL_SECS)
                return  # stop_event fired — clean exit
            except asyncio.TimeoutError:
                pass

            # asyncio.all_tasks() must be read ON the loop (it needs the
            # running loop); capture it here before offloading the blocking
            # /proc walk — calling it inside the worker thread raises
            # RuntimeError and would kill this watchdog on its first probe.
            task_count = len(asyncio.all_tasks())
            snap = await asyncio.to_thread(
                _snapshot_state,
                server=server,
                pool=pool,
                connections=connections,
                task_count=task_count,
            )
            snap["tag"] = "probe"
            await asyncio.to_thread(_write_diagnostic, diag_path, snap)

            if snap["is_serving"] is False and not stop_event.is_set():
                snap["tag"] = "zombie_detected"
                snap["tasks"] = _collect_task_stacks()
                snap["traceback"] = traceback.format_stack()
                await asyncio.to_thread(_write_diagnostic, diag_path, snap)
                logger.error(
                    "zombie gatewayd detected: is_serving=False while stop_event unset; "
                    "tasks=%d fd=%d rss_kb=%d — diagnostic dumped to %s; setting stop_event",
                    snap["task_count"],
                    snap["fd_count"],
                    snap["rss_kb"],
                    diag_path,
                )
                stop_event.set()
                return
    except asyncio.CancelledError:
        pass
