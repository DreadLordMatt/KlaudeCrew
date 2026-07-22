"""Line-delimited JSON-RPC framing helpers for the MCP gateway.

Split out of :mod:`kiro_crew.mcp_gateway.gatewayd` (LOC refactor). Owns the
frame-size cap and handshake/reply timeout constants shared by the daemon
core and its connection handler. Leaf module — imports nothing from the rest
of the ``mcp_gateway`` split.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Optional

from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES

logger = logging.getLogger(__name__)

# Max bytes accepted for any single stub->gateway frame. Registration
# payloads from the stub are well under 4 KiB; 1 MiB is a very loose cap
# that still guards against a malformed or hostile peer blowing memory
# with ``readuntil(b"\n")``.
_MAX_FRAME_BYTES = READ_BUFFER_LIMIT_BYTES  # 1 MiB; see pool.READ_BUFFER_LIMIT_BYTES

# How long a connection handler waits for the first Register message
# before giving up on an idle client. Keeps the event loop from
# accumulating half-open connections that never send anything.
_REGISTER_TIMEOUT_SECS = 5.0

# Upper bound on a single control/handshake reply's ``drain()`` (pong, stats,
# registered, rejected, ready, forward-error — everything sent via
# ``_write_json_line``). ``_REGISTER_TIMEOUT_SECS`` only bounds the inbound
# first-frame read; without a write bound a same-uid peer that passes the
# handshake then stops reading would pin its handler task for the daemon's
# lifetime. Generous — a peer that cannot accept a small reply in 30s is dead.
_WRITE_REPLY_TIMEOUT_SECS = 30.0


def _jsonrpc_error(msg: dict[str, Any], reason: str) -> dict[str, Any]:
    """Return a JSON-RPC 2.0 error envelope mirroring the id of ``msg``.

    Used to close the loop when a backend dies mid-forward: the stub sees
    a plain error response under its own id instead of a dangling request.
    """
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "error": {"code": -32000, "message": reason},
    }


class _TargetUnknown(RuntimeError):
    """Resolver returned no mapping — treated as a clean Register rejection
    rather than an internal error."""


async def _read_first_frame(reader: asyncio.StreamReader) -> Optional[dict[str, Any]]:
    """Read the first line-delimited JSON object from ``reader``.

    Returns ``None`` on clean EOF before a full line arrives, on malformed
    JSON, or on idle timeout. The caller dispatches on the ``type`` field:
    ``"ping"`` gets a pong reply, ``"register"`` (or no type) starts the
    handshake, anything else is logged and dropped.
    """
    try:
        line = await asyncio.wait_for(
            reader.readuntil(b"\n"),
            timeout=_REGISTER_TIMEOUT_SECS,
        )
    except asyncio.IncompleteReadError as exc:
        # Peer closed without a newline — treat as clean disconnect only
        # if we received zero bytes; partial frames are truncation errors.
        if exc.partial:
            logger.warning("stub sent partial first frame (%d bytes)", len(exc.partial))
        return None
    except asyncio.TimeoutError:
        logger.warning("stub idle for %.1fs without first frame; closing", _REGISTER_TIMEOUT_SECS)
        return None
    except asyncio.LimitOverrunError:
        logger.warning("stub first frame exceeded %d bytes; closing", _MAX_FRAME_BYTES)
        return None

    if len(line) > _MAX_FRAME_BYTES:
        logger.warning("stub first frame too large: %d bytes", len(line))
        return None

    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("stub first frame not valid JSON: %s", exc)
        return None

    if not isinstance(msg, dict):
        logger.warning("stub first frame not a JSON object: got %s", type(msg).__name__)
        return None
    return msg


async def _write_json_line(writer: asyncio.StreamWriter, obj: Any) -> None:
    """Serialize ``obj`` as one JSON line with a bounded ``drain()``.

    Backpressure (Phase-0 #2): a misbehaving peer that stops reading can
    otherwise let the kernel socket buffer fill silently, deadlocking the
    handler. ``drain()`` yields to the scheduler until the write is
    accepted or the peer's half of the connection drops.

    The drain is bounded by ``_WRITE_REPLY_TIMEOUT_SECS``: ``_REGISTER_TIMEOUT_SECS``
    only wraps the inbound first-frame read, so a same-uid peer that passes the
    handshake then stops reading could otherwise pin this handler task
    indefinitely on the registered/rejected/pong/stats reply.
    """
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
    lock = getattr(writer, "_mc_write_lock", None)
    guard: Any = lock if lock is not None else contextlib.nullcontext()
    async with guard:
        writer.write(payload)
        try:
            await asyncio.wait_for(writer.drain(), timeout=_WRITE_REPLY_TIMEOUT_SECS)
        except (ConnectionError, asyncio.TimeoutError):
            # Peer hung up or stopped reading mid-reply; nothing productive to do.
            return
