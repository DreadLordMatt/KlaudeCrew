"""Claim-push and abort-push frame handlers for the MCP gateway.

Split out of :mod:`kiro_crew.mcp_gateway.gatewayd` (LOC refactor). Both handlers
operate on the single-owner connection index (:mod:`conn_registry`) via its
accessor, audit through :mod:`audit`, and build caller identities via
:mod:`peer_identity` — a strict downstream dependency (nothing here is imported
back by those leaves).
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.mcp_gateway.audit import _audit_abort_applied, _audit_caller_claimed
from kiro_crew.mcp_gateway.conn_registry import _conn_index_get
from kiro_crew.mcp_gateway.peer_identity import _caller_from_register
from kiro_crew.mcp_gateway.pool import BackendPool

logger = logging.getLogger(__name__)


def _apply_claim(frame: dict[str, Any]) -> dict[str, Any]:
    """Apply a ``claim`` frame to every indexed connection of the target PID.

    Returns the ack frame. Validation is deny-by-default: a non-integer or
    out-of-range pid, or an empty/malformed caller, updates nothing and is
    audited as denied. A valid claim REPLACES existing identities (gateway-
    trusted; this is what keeps callers correct across warm-pool re-claims).
    """
    raw_pid = frame.get("pid")
    pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else 0
    updated_caller = _caller_from_register(frame)
    if pid <= 1 or updated_caller is None or not updated_caller.session_key:
        reason = f"malformed claim: pid={raw_pid!r} session_key={'' if updated_caller is None else updated_caller.session_key!r}"
        logger.warning("claim rejected: %s", reason)
        _audit_caller_claimed("", "", "pid-index", "denied", reason)
        return {"type": "claim-rejected", "reason": reason}
    conns = _conn_index_get(pid)
    if not conns:
        # A claim naming a pid with NO indexed connection is the exact silent
        # failure that produced orphan subagents (host-pid claim vs
        # namespace-pid index, Mesh ticket 8abcd9fe). It can also mean the
        # runtime's stubs disconnected — either way it deserves a loud trail,
        # not a silent {"updated": 0}.
        logger.warning(
            "claim matched ZERO connections: pid=%d session_key=%s — "
            "stub identity will stay stale (possible pid-index mismatch)",
            pid, updated_caller.session_key,
        )
        _audit_caller_claimed(
            "", updated_caller.session_key, "pid-index", "noop",
            f"claim pid={pid} matched no indexed connection",
        )
        return {"type": "claim-noop", "updated": 0, "connections": 0}
    updated = 0
    for conn in conns:
        old_key = conn.caller.session_key if conn.caller is not None else ""
        if old_key == updated_caller.session_key:
            continue  # already correct — idempotent re-claim
        conn.caller = updated_caller
        updated += 1
        _audit_caller_claimed(old_key, updated_caller.session_key, conn.pool_label, "allowed")
        logger.info(
            "stub %s claim → session_key=%s type=%s (was %s)",
            conn.stub_uuid,
            updated_caller.session_key,
            updated_caller.session_type,
            old_key or "<none>",
        )
    return {"type": "claimed", "updated": updated, "connections": len(conns)}


async def _apply_abort(frame: dict[str, Any], pool: "BackendPool") -> dict[str, Any]:
    """Apply an ``abort`` frame: cancel in-flight requests for all stubs under
    the named PIDs.

    This is the gateway-authoritative abort path (Mesh-2808 Scope A):
    on session hard-stop, the gateway sends abort for the killed runtime's
    PIDs so gatewayd can propagate MCP cancel notifications to backends.
    Backend recycle happens on the subsequent stub disconnect path, not here.
    """
    raw_pids = frame.get("pids")
    if not isinstance(raw_pids, list):
        _audit_abort_applied([], "missing or invalid pids", "denied")
        return {"type": "abort-rejected", "reason": "missing or invalid pids"}
    pids = [p for p in raw_pids if isinstance(p, int) and not isinstance(p, bool) and p > 1]
    if not pids:
        _audit_abort_applied([], "no valid pids", "denied")
        return {"type": "abort-rejected", "reason": "no valid pids"}
    reason = str(frame.get("reason", "session hard-stop"))

    total_cancelled = 0
    affected_stubs = set()
    for pid in pids:
        conns = _conn_index_get(pid)
        for conn in list(conns):
            affected_stubs.add(conn.stub_uuid)
    # Find backends attached to the affected stubs and cancel their in-flight work
    for backend in pool.all_backends():
        for stub_uuid in affected_stubs:
            cancelled = await backend.cancel_in_flight_for_stub(stub_uuid)
            total_cancelled += len(cancelled)

    logger.info(
        "abort applied: pids=%r reason=%s cancelled=%d stubs=%d",
        pids,
        reason,
        total_cancelled,
        len(affected_stubs),
    )
    _audit_abort_applied(pids, reason, "allowed", total_cancelled, len(affected_stubs))
    return {"type": "aborted", "cancelled": total_cancelled, "stubs": len(affected_stubs)}
