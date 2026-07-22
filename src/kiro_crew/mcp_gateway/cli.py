"""CLI entry point for ``python -m kiro_crew.mcp_gateway.gatewayd``.

Split out of :mod:`kiro_crew.mcp_gateway.gatewayd` (LOC refactor). Owns the
argument parser, the async ``_amain`` bootstrap (signal handlers, loop
exception hook, heartbeat), and the sync ``main`` wrapper.

``run_gatewayd`` is imported LAZILY inside :func:`_amain` rather than at module
scope: ``gatewayd`` re-exports ``main`` from here (module-level), so a
module-level import of ``gatewayd`` here would form an import cycle. The lazy
import keeps the static dependency graph a DAG (gatewayd → cli only).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Optional

from kiro_crew.sandbox import prewarm_backend

logger = logging.getLogger(__name__)

# Subdirectory under ``$XDG_RUNTIME_DIR`` (or ``/tmp`` fallback) where the
# gateway puts its socket by default. Callers normally supply an explicit
# path via :func:`run_gatewayd`; this default is for tests and ad-hoc runs.
_DEFAULT_SOCKET_SUBDIR = "kirocrew"
_DEFAULT_SOCKET_NAME = "mcp-gateway.sock"


def _default_cli_socket_path() -> Path:
    """Fallback socket path for the CLI's ``--socket`` argparse default.

    This is used ONLY when ``python -m kiro_crew.mcp_gateway.gatewayd`` is
    invoked without an explicit ``--socket`` flag — a rare operator path,
    typically ad-hoc debugging. The KiroCrew production path always
    derives the socket from ``McpGatewayConfig.socket_path`` / the
    ``default_socket_path()`` in :mod:`kiro_crew.mcp_gateway.rewriter`,
    which returns ``$KIROCREW_HOME/mcp-gateway/gateway.sock``.

    Preference order for this CLI fallback:
    1. ``$XDG_RUNTIME_DIR/kirocrew/mcp-gateway.sock`` when XDG is set.
    2. ``/tmp/kirocrew-mcp-gateway.sock`` fallback.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / _DEFAULT_SOCKET_SUBDIR / _DEFAULT_SOCKET_NAME
    return Path("/tmp") / f"kirocrew-{_DEFAULT_SOCKET_NAME}"


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mc-mcp-gatewayd",
        description="KiroCrew MCP gateway daemon — pools MCP backends across sessions",
    )
    p.add_argument(
        "--socket",
        dest="socket",
        default=str(_default_cli_socket_path()),
        help="Unix socket path to bind. Default: $XDG_RUNTIME_DIR/kirocrew/mcp-gateway.sock",
    )
    p.add_argument(
        "--max-backends",
        dest="max_backends",
        type=int,
        default=20,
        help="Maximum concurrent backend subprocesses. LRU-evicted beyond this.",
    )
    p.add_argument(
        "--idle-timeout-secs",
        dest="idle_timeout_secs",
        type=int,
        default=300,
        help="Seconds an unattached backend is kept before the idle sweeper drains it.",
    )
    p.add_argument(
        "--prewarm-count",
        dest="prewarm_count",
        type=int,
        default=0,
        help="Number of hottest observed (agent x server x channel) backends to "
        "spawn at startup, before the first stub connects, to remove the "
        "cold-after-restart new-chat latency. 0 (default) disables prewarming.",
    )
    p.add_argument(
        "--credential-watch-path",
        dest="credential_watch_paths",
        action="append",
        default=[],
        metavar="PATH",
        help="Credential file to watch for content changes (repeatable). On a "
        "real rotation, all pooled backends are drained via a blue-green "
        "cutover and respawned with the fresh credential. No flag "
        "(default) disables the watcher entirely.",
    )
    p.add_argument(
        "--log-level",
        dest="log_level",
        default=os.environ.get("MC_GATEWAYD_LOG", "INFO"),
        help="Python logging level (DEBUG, INFO, WARNING, ...).",
    )
    return p


async def _amain(argv: Optional[list[str]] = None) -> int:
    # Lazy import to keep the static dependency graph a DAG: gatewayd
    # re-exports ``main`` from this module, so importing gatewayd at module
    # scope here would be a cycle. run_gatewayd is only needed at call time.
    from kiro_crew.mcp_gateway.gatewayd import run_gatewayd

    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    # Catch exceptions that slip past per-task handlers — e.g. a
    # fire-and-forget coroutine that blows up without ``await``. Without
    # this hook they get logged through asyncio's default handler only
    # if the task is awaited; zombie modes have been traced to exactly
    # this path.
    def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        msg = context.get("message", "unhandled event loop error")
        if exc is not None:
            logger.error("gatewayd event-loop exception: %s", msg, exc_info=exc)
        else:
            logger.error("gatewayd event-loop error: %s | context=%r", msg, context)

    loop.set_exception_handler(_loop_exception_handler)

    # Heartbeat: emit a line every 60s so a silent stdout stream becomes
    # visible proof that the daemon has zombified. Also logs pool stats
    # to give shape to load growth between heartbeats.
    async def _heartbeat() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                logger.info("gatewayd heartbeat: alive, stop_event=unset")
            except asyncio.CancelledError:
                return

    hb_task = asyncio.create_task(_heartbeat(), name="mcp-gateway-heartbeat")

    # Prewarm sandbox probe cache so backends spawned on-loop never hit cold path
    prewarm_backend()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await run_gatewayd(
            args.socket,
            max_backends=args.max_backends,
            idle_timeout_secs=args.idle_timeout_secs,
            stop_event=stop_event,
            prewarm_count=args.prewarm_count,
            credential_watch_paths=[Path(p) for p in args.credential_watch_paths],
        )
    except Exception:
        logger.exception("gatewayd exited with unhandled exception")
        return 1
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hb_task
    return 0


def main() -> None:
    """Sync entry point for ``python -m kiro_crew.mcp_gateway.gatewayd``."""
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)
