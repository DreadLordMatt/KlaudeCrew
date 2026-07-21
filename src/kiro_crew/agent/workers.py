"""Background/companion agent installers (lite, knowledge, research, heartbeat).

Depends on ``paths``, ``prompts``, and ``builder``.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

from kiro_crew import agent_state
from kiro_crew.agent import builder as _builder
from kiro_crew.agent import paths as _paths
from kiro_crew.agent import prompts as _prompts

logger = logging.getLogger("kiro_crew.agent")

# Cheap Claude Code model for KiroCrew's background agents (lite / heartbeat).
# Stored in the agent_state sidecar, never in the kiro spec (deny_unknown_fields).
_BACKGROUND_CC_MODEL = "claude-sonnet-4.6"


_LITE_AGENT_FILENAME = "kirocrew-lite.json"

_KIROCREW_AIM_PACKAGE = "KiroCrewAICapabilities"

_LITE_AGENT_NAMES = frozenset({_LITE_AGENT_FILENAME, f"{_KIROCREW_AIM_PACKAGE}-kirocrew-lite.json"})


def is_aim_package_installed(package: str) -> bool:
    """Check if an AIM agents package is already installed.

    AIM is an Amazon-internal package manager and is absent on a public
    install, so this returns ``False`` unless an ``aim`` binary happens to
    be on PATH and reports the package.
    """
    aim = shutil.which("aim")
    if not aim:
        return False
    try:
        result = subprocess.run(
            [aim, "agents", "list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return any(line.startswith(package) for line in (result.stdout or "").splitlines())
    except Exception:
        return False


def _install_aim_capabilities() -> None:
    """Write a bare ``kirocrew-lite`` agent config.

    Symbol preserved for callers (``rebuild_agent_config``).  The previous
    AIM-package install path is omitted on public installs (AIM is an
    Amazon-internal package manager); the generic ``kirocrew-lite`` fallback
    config — used by the claude_code provider for cheap background work — is
    still written.
    """
    _install_lite_agent_fallback()


def _remove_bare_lite_if_aim_installed() -> None:
    """No-op on public installs (AIM package manager absent).

    Symbol preserved for backward compatibility.  Previously removed the
    bare ``kirocrew-lite.json`` when an AIM-installed duplicate existed; with
    AIM install neutralized there is no AIM-managed copy to deduplicate.
    """
    return None


def _install_lite_agent_fallback() -> None:
    """Write a bare kirocrew-lite config (cheap background agent)."""
    lite_path = _paths.KIRO_AGENTS_DIR / _LITE_AGENT_FILENAME
    lite_config = {
        "name": "kirocrew-lite",
        "model": "claude-opus-4.6",
        "tools": [],
        "mcpServers": {},
        "prompt": "",
    }
    _paths._atomic_json_write(lite_path, lite_config)
    # Cheap model for the claude_code (CC) provider. kiro-cli resolves the lite
    # model from `model` via --agent; the CC backend can't, so the provider
    # factory reads this cc_model for the lite agent. Sonnet is plenty for
    # background title/compaction/heartbeat work and far cheaper than the global
    # Opus 4.8 default. Stored in the sidecar (kiro spec stays schema-clean).
    agent_state.set_cc_model("kirocrew-lite", _BACKGROUND_CC_MODEL)


_KNOWLEDGE_AGENT_FILENAME = "kirocrew-knowledge.json"


def _install_knowledge_agent() -> None:
    """Generate and install the kirocrew-knowledge agent config.

    This agent is used by the Knowledge Library's LLMPool for document
    extraction.  It uses claude-haiku-4.5 (cheapest model).  The previous
    Amazon-internal ``builder-mcp`` / ReadInternalWebsites wiring is omitted
    on public installs; the agent ships without MCP servers and relies on the
    model's own capabilities for extraction.  Symbol preserved for callers.
    """
    path = _paths.KIRO_AGENTS_DIR / _KNOWLEDGE_AGENT_FILENAME

    config: dict[str, object] = {
        "name": "kirocrew-knowledge",
        "description": (
            "Dedicated agent for knowledge extraction, categorization, " "and summarization."
        ),
        "model": "claude-haiku-4.5",
        "includeMcpJson": False,
        "prompt": _prompts._KNOWLEDGE_SYSTEM_PROMPT,
        "mcpServers": {},
        "tools": [],
    }

    _paths._atomic_json_write(path, config)
    logger.info("Installed knowledge agent config: %s", path)


_RESEARCH_AGENT_FILENAME = "kirocrew-research.json"


def _install_research_agent() -> None:
    """Generate and install the kirocrew-research agent config.

    Derives from the kirocrew agent (MCP servers, security, tools) but swaps in a
    lean research-worker prompt + identity. Used by the Research Lab app's
    autonudge loop to run one research cycle per turn.
    """
    config = _builder.build_agent_config()
    config["name"] = "kirocrew-research"
    config["description"] = (
        "Autonomous research worker — runs one research cycle per turn "
        "in a Research Lab campaign loop."
    )
    config["prompt"] = _prompts._RESEARCH_SYSTEM_PROMPT
    _paths.KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _paths.KIRO_AGENTS_DIR / _RESEARCH_AGENT_FILENAME
    _paths._atomic_json_write(path, config)
    logger.info("Installed research agent config: %s", path)


_HEARTBEAT_AGENT_FILENAME = "kirocrew-heartbeat.json"


def _install_heartbeat_agent() -> None:
    """Generate and install the kirocrew-heartbeat agent config.

    A dedicated agent for HeartbeatService.  Minimal MCP surface — only
    ``kirocrew-core`` (learn/cron/spawn list, recall, artifacts read) on
    public installs.  Tool approval is enforced gateway-side against
    ``HEARTBEAT_SAFE_TOOLS`` regardless; the per-agent MCP narrowing here
    keeps cold-start cost low and reduces the surface the gateway has to
    police.

    (The Amazon-internal ``builder-mcp`` CR/ticket/pipeline read wiring is
    omitted on public installs, matching ``_install_research_agent`` /
    ``_install_knowledge_agent``.)

    SEL audit logging stays at the gateway side — see
    ``GatewayOrchestrator._heartbeat_approval``.
    """
    _paths.KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _paths.KIRO_AGENTS_DIR / _HEARTBEAT_AGENT_FILENAME

    # Pull the ``kirocrew-core`` entry from the main agent config so the
    # resolved command + skill-paths match the main agent (write-denied
    # commands and security still come from bundled hooks). Strip the main
    # agent's ``--include-tools``/``--include-tool-tags``/``--exclude-tools``
    # filters so all read tools surface to the heartbeat agent — security is
    # enforced gateway-side against ``HEARTBEAT_SAFE_TOOLS`` via
    # ``_heartbeat_approval``, not by per-agent MCP filtering.
    main_config = _builder._load_json(_paths.KIRO_AGENTS_DIR / _paths.AGENT_FILENAME)
    main_mcp = main_config.get("mcpServers", {}) or {}

    _strip_flags = ("--include-tools", "--include-tool-tags", "--exclude-tools")
    mcp: dict[str, dict] = {}
    for name in ("kirocrew-core",):
        entry = main_mcp.get(name)
        if not isinstance(entry, dict):
            continue
        cleaned = dict(entry)
        args = entry.get("args") or []
        if isinstance(args, list):
            filtered: list[str] = []
            skip_next = False
            for arg in args:
                if skip_next:
                    skip_next = False
                    continue
                if not isinstance(arg, str):
                    filtered.append(arg)
                    continue
                if any(arg == f or arg.startswith(f + "=") for f in _strip_flags):
                    # Form ``--flag=value`` is dropped; bare ``--flag`` consumes
                    # the next arg too.
                    skip_next = "=" not in arg
                    continue
                filtered.append(arg)
            cleaned["args"] = filtered
        mcp[name] = cleaned

    config: dict[str, object] = {
        "name": "kirocrew-heartbeat",
        "description": (
            "Unattended polling worker — runs one HeartbeatService task per "
            "cycle with a read-only MCP toolset. Tool approval is gated "
            "gateway-side against HEARTBEAT_SAFE_TOOLS."
        ),
        "model": "claude-sonnet-4.6",
        "includeMcpJson": False,
        "prompt": _prompts._HEARTBEAT_SYSTEM_PROMPT,
        "mcpServers": mcp,
        # Build from the servers actually resolved so we never reference a
        # tool namespace without a matching mcpServers entry — the
        # rebuild_agent_config flow may run before either main entry exists.
        "tools": [f"@{name}" for name in mcp],
    }

    _paths._atomic_json_write(path, config)
    # CC model for the heartbeat agent lives in the sidecar, not the kiro spec.
    agent_state.set_cc_model("kirocrew-heartbeat", _BACKGROUND_CC_MODEL)
    logger.info("Installed heartbeat agent config: %s", path)


def sync_aim_packages() -> None:
    """No-op on public installs (AIM package manager absent).

    Symbol preserved for callers (``rebuild_agent_config``).  AIM is an
    Amazon-internal agents/skills/plugins package manager; there is nothing
    to sync across providers on a public install, so this returns immediately.
    """
    return None
