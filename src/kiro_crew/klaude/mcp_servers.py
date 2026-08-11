"""Fork (KlaudeCrew): MCP server injection for the claude ACP backend.

claude-agent-acp does not read any on-disk agent spec or honor ``--agent`` —
kiro-cli's own config-file resolution never runs for it. Everything it should
see must be handed to it directly in ``session/new``/``session/load``'s
``mcpServers`` array (the seam ``AcpClient._claude_session_mcp_servers()``
exists for exactly this — see ``acp/client.py``).

:func:`claude_session_servers` is that source: it reads the same merged agent
spec kiro-cli itself consults (``~/.kiro/agents/<agent>.json``, kept current by
``agent.rebuild_agent_config()`` on every gateway boot and dashboard MCP
mutation) and reshapes it into ACP's wire form. Read fresh on every spawn —
no sidecar file, no extra writer, no staleness: an MCP install/toggle made
while KiroCrew is running on the kiro backend applies to the very next claude
session, and vice versa.

KiroCrew's managed servers (``agent._MANAGED_MCP_SERVERS`` — currently
kirocrew-core, kirocrew-cron, kirocrew-computer) are load-bearing (cron,
subagents, memory, and computer-use all depend on them) and are always
injected with their canonical stdio command, overriding whatever the spec
file says for those names — mirrors the guarantee ``rebuild_agent_config()``
already gives the kiro-cli path. Built from the exact same source
(``_MANAGED_MCP_SERVERS`` / ``_managed_mcp_env``), never a second, drifting
copy of the invocation logic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.agent_files import (
    AGENT_FILENAME,
    HEARTBEAT_AGENT_FILENAME,
    KNOWLEDGE_AGENT_FILENAME,
    LITE_AGENT_FILENAME,
    RESEARCH_AGENT_FILENAME,
)
from kiro_crew.config.paths import kiro_agents_dir

logger = logging.getLogger(__name__)

# Built-in role agents ship under a fixed filename (agent_files.py). A custom
# named agent's spec lives at "<name>.json" by kiro-cli's own --agent
# convention, which the claude backend can't resolve for itself.
_ROLE_AGENT_FILENAMES: dict[str, str] = {
    "kirocrew": AGENT_FILENAME,
    "kirocrew-lite": LITE_AGENT_FILENAME,
    "kirocrew-knowledge": KNOWLEDGE_AGENT_FILENAME,
    "kirocrew-research": RESEARCH_AGENT_FILENAME,
    "kirocrew-heartbeat": HEARTBEAT_AGENT_FILENAME,
}


def _agent_spec_path(agent: str | None) -> Path:
    name = agent or "kirocrew"
    filename = _ROLE_AGENT_FILENAMES.get(name, f"{name}.json")
    return kiro_agents_dir() / filename


def _load_agent_spec(agent: str | None) -> dict[str, Any]:
    path = _agent_spec_path(agent)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("claude MCP injection: cannot read agent spec %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _env_pairs(raw: Any) -> list[dict[str, str]]:
    """kiro-agent-JSON's ``env`` mapping -> ACP's array-of-pairs form."""
    if not isinstance(raw, dict):
        return []
    return [{"name": str(k), "value": str(v)} for k, v in raw.items()]


def _shape_entry(name: str, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Reshape one kiro-agent-JSON ``mcpServers`` entry into an ACP element.

    Returns ``None`` for a disabled entry, or a stdio entry missing its
    required ``command`` (nothing to launch) -- either way the caller simply
    omits the server rather than injecting something broken.
    """
    if entry.get("disabled"):
        return None
    if entry.get("url"):
        shaped: dict[str, Any] = {
            "name": name,
            # kiro's own remote-server schema carries no "type" field (it
            # infers transport from the URL); default to the modern
            # streamable-HTTP transport when the entry doesn't say otherwise.
            "type": entry.get("type") or "http",
            "url": entry["url"],
        }
        if isinstance(entry.get("headers"), dict) and entry["headers"]:
            shaped["headers"] = entry["headers"]
        return shaped
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        return None
    return {
        "name": name,
        "type": "stdio",
        "command": command,
        "args": [a if isinstance(a, str) else json.dumps(a, sort_keys=True, default=str)
                 for a in (entry.get("args") or [])],
        "env": _env_pairs(entry.get("env")),
    }


def _canonical_managed_servers() -> dict[str, dict[str, Any]]:
    """kirocrew-core / kirocrew-cron, built from the same source agent.py's
    own ``rebuild_agent_config`` uses -- never a second, drifting copy of the
    invocation logic."""
    try:
        from kiro_crew.agent import _MANAGED_MCP_SERVERS, _managed_mcp_env
    except ImportError:  # pragma: no cover - agent.py always present in-tree
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in _MANAGED_MCP_SERVERS.items():
        try:
            if "invocation_fn" in spec:
                cmd, args = spec["invocation_fn"]()
            else:
                cmd = spec.get("command") or spec["command_fn"]()
                args = list(spec["args"])
        except Exception:
            logger.warning(
                "claude MCP injection: managed server %r invocation failed",
                name, exc_info=True,
            )
            continue
        entry: dict[str, Any] = {
            "name": name,
            "type": "stdio",
            "command": cmd,
            "args": [str(a) for a in args],
        }
        env = _managed_mcp_env()
        if env:
            entry["env"] = _env_pairs(env)
        out[name] = entry
    return out


def claude_session_servers(agent: str | None) -> list[dict[str, Any]]:
    """ACP ``mcpServers`` array for a claude-backed session bound to *agent*.

    Fail-soft throughout: any unreadable/malformed spec yields just the
    canonical managed servers (never an exception), matching the module's
    "load-bearing servers are always present" guarantee.
    """
    spec = _load_agent_spec(agent)
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}

    managed = _canonical_managed_servers()

    out: list[dict[str, Any]] = []
    for name, entry in sorted(servers.items()):
        name = str(name)
        if name in managed:
            continue  # canonical command always wins for managed servers
        if not isinstance(entry, dict):
            continue
        shaped = _shape_entry(name, entry)
        if shaped is not None:
            out.append(shaped)
    out.extend(managed.values())
    return out
