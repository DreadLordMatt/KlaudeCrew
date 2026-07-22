"""Peer-identity and target-resolution helpers for the MCP gateway.

Split out of :mod:`kiro_crew.mcp_gateway.gatewayd` (LOC refactor). Groups the
three identity/resolution helpers that turn wire data into gateway concepts:

* :func:`env_target_resolver` — map a :class:`PoolKey` to its spawn 4-tuple.
* :func:`_resolve_peer_identity` — walk the peer's real /proc ancestry.
* :func:`_caller_from_register` — build a :class:`CallerContext` from a frame.

Leaf module — imports nothing from the rest of the ``mcp_gateway`` split.
"""

from __future__ import annotations

import logging
import os
import shlex
from typing import Any, Optional

from kiro_crew.config.loader import config_dir as _config_dir
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_caller import _parent_pid as _ppid_fn
from kiro_crew.mcp_gateway.manager import _scrub_sensitive_env
from kiro_crew.mcp_gateway.pool import PoolKey

logger = logging.getLogger(__name__)


def env_target_resolver(pool_key: PoolKey) -> Optional[tuple[str, list[str], dict[str, str], str]]:
    """Look up ``MC_MCP_TARGET_<SERVER>`` in the process env and return the
    spawn tuple, or ``None`` if no mapping is set.

    Wire format: ``MC_MCP_TARGET_SLACK_MCP="slack-mcp --stdio"``.
    The server name is upper-cased with ``-`` replaced by ``_``. Env is
    inherited from the gateway process with ``KIROCREW_CHANNEL_ID``
    overlaid when the pool key carries one — this keeps cron / send_message
    fallbacks pointed at the correct channel on a per-pool-key basis.

    Defense-in-depth: env is scrubbed through
    :func:`kiro_crew.mcp_gateway.manager._scrub_sensitive_env` so even if
    the gateway process somehow inherited credential vars, backends won't.
    """
    base = "MC_MCP_TARGET_" + pool_key.server_name.upper().replace("-", "_")
    # Prefer the args-disambiguated entry (written by
    # rewriter._collect_target_env) so two agents that share a server name but
    # declare different --target-args each spawn their OWN backend command,
    # instead of resolving to whichever agent sorted first alphabetically. Fall
    # back to the bare server-name entry for older overlays predating the
    # disambiguated keys.
    spec = os.environ.get(base + "__" + pool_key.command_args_hash) or os.environ.get(base)
    if not spec:
        return None
    parts = shlex.split(spec)
    if not parts:
        return None
    command, *args = parts
    env = _scrub_sensitive_env(dict(os.environ))
    # Strip PYTHONPATH/PYTHONHOME so the KiroCrew process's own Python
    # environment doesn't leak into Python-based MCP backends (import conflicts).
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    if pool_key.channel_id:
        env["KIROCREW_CHANNEL_ID"] = pool_key.channel_id
    return command, args, env, pool_key.work_dir


def _resolve_peer_identity(peer_pid: int) -> tuple[str, list[int]]:
    """Walk the peer's real-PID ancestry (server-side): session key + host chain.

    Runs in gatewayd's own PID namespace (real pids), so it works regardless
    of how the stub sees the world. A single /proc walk returns both:

    * the session_key from the first ancestor with a ``session_pid_<pid>.txt``
      file (``""`` when none matches — normal at register time for a runtime
      that has not been claimed yet), and
    * the full HOST ancestor PID chain (peer first). The register handler
      indexes the stub connection under this chain so a later ``claim`` frame
      — which always carries the runtime's HOST pid — matches even when the
      stub's self-reported ``ancestor_pids`` are namespace-local (sandbox
      PID-namespace topology). Without the host chain in ``_CONN_INDEX`` the
      claim-push silently updates zero connections and the stub stays
      identity-less for life: orphan subagents with empty ``parent_session``
      and undeliverable completion events (Mesh ticket 8abcd9fe).

    The walk continues past a session-key match so the chain is complete for
    claim matching at any ancestry level.
    """
    session_key = ""
    chain: list[int] = []
    try:
        cfg_dir = _config_dir()
    except Exception:
        return "", []

    pid = peer_pid
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        chain.append(pid)
        if not session_key:
            pid_file = cfg_dir / f"session_pid_{pid}.txt"
            try:
                if pid_file.exists():
                    session_key = pid_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        try:
            pid = _ppid_fn(pid)
        except (OSError, ValueError):
            # Target exited mid-walk (/proc/<pid>/stat gone or malformed).
            break
    return session_key, chain


def _caller_from_register(register: dict[str, Any]) -> Optional[CallerContext]:
    """Build a :class:`CallerContext` from the stub's Register payload.

    The wire format is flexible to support both short and long-lived stubs:

    * Inline ``session_key`` / ``session_type`` / ``principal_id`` /
      ``channel_id`` fields on the Register envelope (tests and the Rust
      stub both use this shape).
    * A nested ``caller`` dict with the same field names — matches the
      Rust ``StubToGateway::Register { caller }`` variant.

    Missing fields default to the empty string. ``from_gateway=True`` is
    forced since this context came through the gateway register path.
    """
    nested = register.get("caller")
    src: dict[str, Any] = nested if isinstance(nested, dict) else register
    session_key = str(src.get("session_key") or src.get("sessionKey") or "")
    if not session_key:
        return None
    return CallerContext(
        session_key=session_key,
        session_type=str(src.get("session_type") or src.get("sessionType") or "unknown"),
        principal_id=str(src.get("principal_id") or src.get("principalId") or ""),
        channel_id=str(src.get("channel_id") or src.get("channelId") or ""),
        from_gateway=True,
    )
