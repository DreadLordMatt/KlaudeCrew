"""Provider-factory and agent-binding resolution helpers.

Extracted from ``config/loader.py``. These operate on a
:class:`~kiro_crew.config.root.KiroCrewConfig` to build the LLM provider factory
(via the platform seam) and to resolve a session's workspace, memory store, and
kiro agent. Re-exported by ``config.loader`` for backward compatibility.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from kiro_crew.config.root import KiroCrewConfig
from kiro_crew.config.sections import ResolvedBindings, resolve_memory_store_config

logger = logging.getLogger("kiro_crew.config.loader")


def build_provider_factory(cfg: "KiroCrewConfig") -> Callable:
    """Return the LLM-provider factory for *cfg*, via the platform seam.

    Routes through ``current_context().providers.create_factory(cfg)`` (the CPP
    ``ProviderRegistry`` extension point) instead of calling
    ``cfg.create_provider_factory()`` directly, so an edition can supply an
    alternate provider factory (e.g. re-registering an extra ACP backend through
    the dormant ``ACP_BACKEND_*`` seam).  The ``Default`` ProviderRegistry returns
    exactly ``cfg.create_provider_factory()``, so the public edition is
    behaviorally identical to calling it directly.

    Fail-closed: a :class:`PlatformCompositionError` (a non-standalone host that
    could not compose its companion) propagates.  Any other transient lookup
    failure degrades to ``cfg.create_provider_factory()`` so an unbooted /
    standalone call site never breaks — it just gets the public factory.

    The fallback is passed as ``fallback_factory`` (a lazy thunk), NOT eagerly:
    ``cfg.create_provider_factory()`` is built ONLY on the degrade path, so the
    standalone happy path builds the factory exactly once (the Default
    ``ProviderRegistry`` already returns ``cfg.create_provider_factory()``, so an
    eager fallback would build it a second time on every session/reload).  A
    failure INSIDE ``cfg.create_provider_factory()`` itself is handled by
    ``safe_context_call`` (which guards the factory call) rather than escaping
    uncaught; with no eager ``fallback`` here there is no usable factory, so a
    composition error propagates (fail-closed) and any other error re-raises —
    a corrupt-config failure surfaces at the factory site, it is not swallowed.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    return safe_context_call(
        lambda: current_context().providers.create_factory(cfg),
        fallback_factory=lambda: cfg.create_provider_factory(),
        log_message="providers.create_factory failed; using cfg.create_provider_factory()",
    )


# ---------------------------------------------------------------------------
# Agent resolver and kiro agent validation
# ---------------------------------------------------------------------------


def _workspace_name_for_dir(config: KiroCrewConfig, ws_dir: Path) -> str:
    """Find the workspace name whose dir matches *ws_dir*."""
    for name, ws_cfg in config.workspaces.items():
        if Path(ws_cfg.dir) == ws_dir:
            return name
    return "default"


def resolve_agent_bindings(
    config: KiroCrewConfig,
    agent_name: str | None = None,
) -> ResolvedBindings:
    """Resolve workspace, memory store, and kiro agent for a session.

    Resolution:
    1. If agent_name is given and exists in config.agents → use its bindings
    2. Otherwise use config.default_agent (guaranteed to exist by load())
    """
    import dataclasses as _dc

    # Step 1: explicit agent_name
    if agent_name and agent_name in config.agents:
        agent_cfg = config.agents[agent_name]
    elif config.default_agent and config.default_agent in config.agents:
        # Step 2: default_agent (guaranteed valid by load())
        agent_cfg = config.agents[config.default_agent]
    elif config.agents:
        # Defensive: default_agent not in agents, use first available
        first_name = next(iter(config.agents))
        logger.warning(
            "default_agent '%s' not found in agents, using '%s'",
            config.default_agent,
            first_name,
        )
        agent_cfg = config.agents[first_name]
    else:
        # No agents at all — return safe defaults
        logger.warning("No agents configured, using bare defaults")
        return ResolvedBindings(
            workspace_dir=Path("workspace"),
            memory_store_name=config.default_memory_store,
            effective_memory_config=_dc.asdict(config.memory),
            kiro_agent=config.agent.default_agent,
        )

    # Resolve workspace
    ws_name = agent_cfg.workspace
    if ws_name in config.workspaces:
        ws_dir = Path(config.workspaces[ws_name].dir)
    else:
        logger.warning(
            "Agent workspace '%s' not found, falling back to default_workspace '%s'",
            ws_name,
            config.default_workspace,
        )
        fallback_ws = config.workspaces.get(config.default_workspace)
        ws_dir = Path(fallback_ws.dir) if fallback_ws else Path("workspace")

    # Resolve memory store
    store_name = agent_cfg.memory_store
    if store_name not in config.memory_stores:
        logger.warning(
            "Agent memory_store '%s' not found, falling back to '%s'",
            store_name,
            config.default_memory_store,
        )
        store_name = config.default_memory_store

    kiro_agent = agent_cfg.kiro_agent

    # Build effective memory config via dict-level merge
    store_cfg = config.memory_stores.get(store_name)
    store_dict = _dc.asdict(store_cfg) if store_cfg else {}
    top_level_memory = _dc.asdict(config.memory)
    effective_memory = resolve_memory_store_config(top_level_memory, store_dict)

    return ResolvedBindings(
        workspace_dir=ws_dir,
        memory_store_name=store_name,
        effective_memory_config=effective_memory,
        kiro_agent=kiro_agent,
    )


def validate_kiro_agent_references(
    config: KiroCrewConfig,
    installed_agents: list[str],
) -> None:
    """Cross-reference kiro_agent values against installed agents.

    Logs warnings for unresolved references. Never raises.
    """
    installed_names = set(installed_agents)
    for mc_name, mc_agent in config.agents.items():
        if mc_agent.kiro_agent and mc_agent.kiro_agent not in installed_names:
            logger.warning(
                "KiroCrew agent '%s' references kiro agent '%s' " "which is not installed",
                mc_name,
                mc_agent.kiro_agent,
            )
