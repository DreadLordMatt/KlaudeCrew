"""Live stub-connection registry for the MCP gateway.

Split out of :mod:`kiro_crew.mcp_gateway.gatewayd` (LOC refactor). This module
is the SINGLE owner of the ``_CONN_INDEX`` module-global: every other module
mutates or reads it through the accessor functions here
(:func:`_conn_index_add`, :func:`_conn_index_discard`, :func:`_conn_index_get`)
so the index has exactly one home. Leaf module — imports nothing from the rest
of the ``mcp_gateway`` split.
"""

from __future__ import annotations

from typing import Any, Optional

from kiro_crew.mcp_caller import CallerContext


class _StubConn:
    """Mutable per-connection identity holder, indexed by the owning runtime's
    ancestor PID chain so a ``claim`` frame (claim-push) can update the caller
    of every stub connection belonging to a just-claimed warm-pool runtime.

    ``ancestor_pids`` is the stub's parent chain (nearest first) from the
    Register frame. The connection is indexed under EVERY ancestor because
    the PID the gateway names in a claim (``AcpClient._process.pid``) can sit
    several layers above the stub's immediate parent (sandbox wrapper →
    kiro-cli → kiro-cli-chat → stub); indexing a single level was found live
    to make every claim miss.

    ``caller`` starts as the register-time identity (often ``None`` for
    warm-pool stubs) and is replaced by ``recaller`` frames (stub-initiated,
    deny-by-default) or ``claim`` frames (gateway-initiated, replace-allowed).
    Single event loop — no locking needed.
    """

    __slots__ = ("stub_uuid", "ancestor_pids", "pool_label", "caller")

    def __init__(
        self,
        stub_uuid: str,
        ancestor_pids: list[int],
        pool_label: str,
        caller: Optional[CallerContext],
    ) -> None:
        self.stub_uuid = stub_uuid
        self.ancestor_pids = ancestor_pids
        self.pool_label = pool_label
        self.caller = caller


#: Live stub connections indexed by every ancestor PID of the kiro-cli
#: process tree that spawned the stub (``ancestor_pids`` on the Register
#: frame; legacy single ``parent_pid`` accepted). Claim-push looks up this
#: index to retarget every connection of a claimed runtime at once. Entries
#: without usable PIDs (old stubs) are simply not indexed — they keep the
#: recaller-poll fallback.
_CONN_INDEX: dict[int, set[_StubConn]] = {}


def _register_pids(register: dict[str, Any]) -> list[int]:
    """Extract the ancestor PID list from a Register frame.

    Accepts the current ``ancestor_pids`` list and the legacy single
    ``parent_pid`` int. Non-int and out-of-range entries are dropped
    (deny-by-default: garbage never lands in the index).
    """
    raw = register.get("ancestor_pids")
    if not isinstance(raw, list):
        legacy = register.get("parent_pid")
        raw = [legacy] if legacy is not None else []
    return [p for p in raw if isinstance(p, int) and not isinstance(p, bool) and p > 1]


def _conn_index_add(conn: _StubConn) -> None:
    for pid in conn.ancestor_pids:
        _CONN_INDEX.setdefault(pid, set()).add(conn)


def _conn_index_discard(conn: _StubConn) -> None:
    for pid in conn.ancestor_pids:
        conns = _CONN_INDEX.get(pid)
        if conns is not None:
            conns.discard(conn)
            if not conns:
                _CONN_INDEX.pop(pid, None)


def _conn_index_get(pid: int) -> set[_StubConn]:
    """Return the set of connections indexed under ``pid`` (empty if none).

    Read accessor for the single-owner ``_CONN_INDEX``: claim/abort handling
    looks connections up by PID through this instead of importing the raw
    dict, so the index stays owned solely by this module.
    """
    return _CONN_INDEX.get(pid, set())
