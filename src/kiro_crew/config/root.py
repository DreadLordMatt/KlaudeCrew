"""The aggregate :class:`KiroCrewConfig` root configuration object.

Extracted from ``config/loader.py``. Composes every per-feature section
(imported from :mod:`kiro_crew.config.sections`) and owns ``load()`` /
``to_dict()`` / ``save()`` / ``create_provider_factory()`` / ``load_credentials()``.

Import graph: this module imports from ``config.sections`` and ``config.paths``
at load time, but NOT from ``config.loader``. The loader-resident path helpers,
the validated-data cache accessors, and the security-clamp are imported
*lazily inside the methods that use them*. That keeps the module import graph a
DAG (``loader`` imports ``root``, never the reverse at import time) AND preserves
the ``patch("kiro_crew.config.loader.config_path", ...)`` / ``_validate_config_data``
test seams — a function-local ``from kiro_crew.config.loader import config_path``
re-reads the (possibly patched) attribute on every call.

All public names are re-exported from ``config.loader`` for backward compat.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import __version__
from kiro_crew.config.paths import config_package_dir, kiro_agents_dir
from kiro_crew.config.sections import (
    ACTIVATION_ALWAYS,
    ACTIVATION_MENTION,
    DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
    DEFAULT_MAX_PARALLEL_STEPS,
    DEFAULT_MODEL,
    DEFAULT_SESSION_TIMEOUT,
    AgentConfig,
    ChannelConfig,
    CronHistoryConfig,
    DashboardConfig,
    ExternalRegistryConfig,
    HeartbeatConfig,
    InstancesConfig,
    KiroCrewAgentConfig,
    KnowledgeConfig,
    McpGatewayConfig,
    MemoryConfig,
    MemoryStoreConfig,
    MessagingConfig,
    OrchestratorConfig,
    PublishConfig,
    SessionConfig,
    SkillsConfig,
    SlackConfig,
    SttConfig,
    TaskRunnerConfig,
    TelegramConfig,
    TelemetryConfig,
    TunnelConfig,
    WatchdogConfig,
    WeComConfig,
    WorkspaceConfig,
    _archive_retention_days,
    _coerce_embedding_provider,
    _coerce_int,
    _coerce_int_ids,
    _meta,
    _migrate_workspaces,
    _normalize_jail,
    _sanitize_bot_name,
    _validate_activation,
    _validate_tracking_channels,
    _validated_completion_keep,
    _validated_stt_provider,
)
from kiro_crew.effort import is_valid_effort, model_supports_effort
from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _DEFAULT_MAX_RECOVERY
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _DEFAULT_PROBE_FAILS
from kiro_crew.instances.constants import DEFAULT_RECOVER_BACKOFF_MAX_SECS as _DEFAULT_BACKOFF_MAX
from kiro_crew.instances.constants import DEFAULT_SSH_COMPRESSION as _DEFAULT_SSH_COMPRESSION
from kiro_crew.instances.constants import DEFAULT_TUNNEL_BASE_PORT as _DEFAULT_TUNNEL_BASE_PORT
from kiro_crew.instances.constants import DEFAULT_WARM_SET_CAP as _DEFAULT_WARM_SET_CAP
from kiro_crew.mcp_gateway.rewriter import default_overlay_dir, default_socket_path

logger = logging.getLogger("kiro_crew.config.loader")


@dataclass
class KiroCrewConfig:
    agent: AgentConfig = field(
        default_factory=AgentConfig,
        metadata=_meta("Agent", "Agent runtime configuration."),
    )
    session: SessionConfig = field(
        default_factory=SessionConfig,
        metadata=_meta("Session", "Session management settings."),
    )
    taskrunner: TaskRunnerConfig = field(
        default_factory=TaskRunnerConfig,
        metadata=_meta("Task Runner", "Task runner configuration."),
    )
    orchestrator: OrchestratorConfig = field(
        default_factory=OrchestratorConfig,
        metadata=_meta("Orchestrator", "Autopilot/orchestrator settings."),
    )
    messaging: MessagingConfig = field(
        default_factory=MessagingConfig,
        metadata=_meta("Messaging", "Channel-neutral messaging transport settings."),
    )
    cron_history: CronHistoryConfig = field(
        default_factory=CronHistoryConfig,
        metadata=_meta("Cron History", "Cron execution history storage limits."),
    )
    memory: MemoryConfig = field(
        default_factory=MemoryConfig,
        metadata=_meta("Memory", "Memory and embedding configuration."),
    )
    knowledge: KnowledgeConfig = field(
        default_factory=KnowledgeConfig,
        metadata=_meta("Knowledge", "Knowledge Library ingestion settings."),
    )
    skills: SkillsConfig = field(
        default_factory=SkillsConfig,
        metadata=_meta("Skills", "Skill loading and matching configuration."),
    )
    telemetry: TelemetryConfig = field(
        default_factory=TelemetryConfig,
        metadata=_meta(
            "Telemetry",
            "Metrics telemetry (local-first JSONL sink). Off by default.",
        ),
    )
    stt: SttConfig = field(
        default_factory=SttConfig,
        metadata=_meta("STT", "Speech-to-text transcription settings."),
    )
    mcp_gateway: McpGatewayConfig = field(
        default_factory=McpGatewayConfig,
        metadata=_meta("MCP Gateway", "Sidecar MCP broker that shares backends across sessions."),
    )
    instances: InstancesConfig = field(
        default_factory=InstancesConfig,
        metadata=_meta(
            "Instances", "Multi-instance management — manage/switch remote KiroCrews over SSH."
        ),
    )
    heartbeat: HeartbeatConfig = field(
        default_factory=HeartbeatConfig,
        metadata=_meta("Heartbeat", "Heartbeat background task queue delivery defaults."),
    )
    watchdog: WatchdogConfig = field(
        default_factory=WatchdogConfig,
        metadata=_meta("Watchdog", "ACP per-session watchdog / liveness-oracle windows."),
    )

    slack: SlackConfig = field(
        default_factory=SlackConfig,
        metadata=_meta("Slack", "Slack integration settings.", tags=["slack"]),
    )
    publish: PublishConfig = field(
        default_factory=PublishConfig,
        metadata=_meta(
            "Publish", "Artifact publishing controls (destinations allowlist).", tags=["publish"]
        ),
    )
    wechat: WeComConfig = field(
        default_factory=WeComConfig,
        metadata=_meta("WeChat", "WeChat (WeCom AI-bot) integration settings.", tags=["wechat"]),
    )
    telegram: TelegramConfig = field(
        default_factory=TelegramConfig,
        metadata=_meta("Telegram", "Telegram Bot API integration settings.", tags=["telegram"]),
    )
    dashboard: DashboardConfig = field(
        default_factory=DashboardConfig,
        metadata=_meta("Dashboard", "Dashboard UI settings."),
    )
    tunnel: TunnelConfig = field(
        default_factory=TunnelConfig,
        metadata=_meta("Tunnel", "AEA tunnel settings for remote dashboard access."),
    )
    hooks: dict = field(
        default_factory=dict,
        metadata=_meta("Hooks", "Script hook definitions keyed by hook ID."),
    )
    slack_channels: dict[str, ChannelConfig] = field(
        default_factory=dict,
        metadata=_meta("Slack Channels", "Per-channel activation config."),
    )
    slack_dm_activation: str = field(
        default=ACTIVATION_ALWAYS,
        metadata=_meta("Slack DM Activation", "Default activation mode for DMs."),
    )
    observe_max_messages: int = field(
        default=200,
        metadata=_meta("Observe Max Messages", "Max messages per observe-mode channel."),
    )
    observe_ttl_hours: float = field(
        default=168.0,
        metadata=_meta("Observe TTL Hours", "Hours to keep observe history."),
    )
    agents: dict[str, KiroCrewAgentConfig] = field(
        default_factory=dict,
        metadata=_meta("Agents", "Named KiroCrew agent definitions."),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Active KiroCrew agent name from the agents section."),
    )
    workspaces: dict[str, WorkspaceConfig] = field(
        default_factory=dict,
        metadata=_meta("Workspaces", "Named workspace definitions."),
    )
    default_workspace: str = field(
        default="default",
        metadata=_meta("Default Workspace", "Active workspace name."),
    )
    memory_stores: dict[str, MemoryStoreConfig] = field(
        default_factory=dict,
        metadata=_meta("Memory Stores", "Named memory store definitions."),
    )
    default_memory_store: str = field(
        default="default",
        metadata=_meta("Default Memory Store", "Fallback memory store name."),
    )
    auto_update: bool = field(
        default=True,
        metadata=_meta("Auto Update", "Enable automatic update checks."),
    )
    timezone: str = field(
        default="",
        metadata=_meta(
            "Timezone",
            "IANA timezone name (e.g. 'America/Los_Angeles'). "
            "Used to display cron schedules in local time.",
        ),
    )
    snapshot_dir: str = field(
        default="",
        metadata=_meta(
            "Snapshot Directory",
            "Directory for kirocrew snapshot output. "
            "Defaults to ~/.kirocrew/snapshots if empty.",
        ),
    )
    registries: list[ExternalRegistryConfig] = field(
        default_factory=list,
        metadata=_meta(
            "Registries",
            "External app registries (org-owned repos). " "Each entry: {name, repo, branch}.",
        ),
    )

    def channel_config(self, channel_id: str) -> ChannelConfig:
        """Return the config for *channel_id*, falling back to defaults.

        DMs (channel IDs starting with ``D``) use ``slack_dm_activation``.
        Group channels use ``mention`` unless overridden in ``slack_channels``.
        """
        if channel_id in self.slack_channels:
            return self.slack_channels[channel_id]
        if channel_id.startswith("D"):
            return ChannelConfig(activation=self.slack_dm_activation)
        return ChannelConfig(activation=ACTIVATION_MENTION)

    @property
    def slack_enterprise_ids(self) -> set[str]:
        """Extra allowed enterprise IDs from ``slack.allowed_enterprise_ids``."""
        return set(self.slack.allowed_enterprise_ids)

    @classmethod
    def load(cls) -> KiroCrewConfig:
        """Load config from ~/.kirocrew/config.json, falling back to defaults.

        If ``config.local.json`` exists alongside ``config.json``, it is
        deep-merged on top. User overrides in the local file survive
        upgrades that regenerate ``config.json``.

        The overlay is applied at load time but NOT persisted back by
        ``save()`` — only the base config is written to ``config.json``.
        """
        # Path helpers, the validated-data cache accessors, the deep-merge, and
        # the security-clamp stay defined in ``config.loader`` (their ``config_dir``
        # lookups resolve in the loader namespace, preserving the
        # ``patch("kiro_crew.config.loader.config_path", ...)`` /
        # ``_validate_config_data`` test seams). Import them lazily at call time so
        # this module never imports ``loader`` at module load (DAG) and so a test
        # patch on the loader attribute is re-read on every ``load()``.
        from kiro_crew.config.loader import (
            _cached_validated_data,
            _clamp_security_bounds,
            _config_fingerprint,
            _deep_merge,
            _safe_float,
            _safe_int,
            _store_validated_data,
            _validate_config_data,
            config_local_path,
            config_path,
        )

        path = config_path()

        # Hot-path cache: reuse the validated, merged dict when neither config
        # file has changed since the last load. Skips read + json.loads +
        # _deep_merge + the full jsonschema.validate. A deep copy is returned so
        # in-place mutation by callers (and the write-back migration below) can
        # never corrupt the cached original.
        cached_data = _cached_validated_data()
        if cached_data is not None:
            data = cached_data
        else:
            # Capture the fingerprint BEFORE reading so a write landing during
            # the read is detected: we cache under this pre-read fp, which won't
            # match the post-write on-disk stat, so the next load() re-reads
            # instead of serving the content we read mid-write (read->store
            # TOCTOU). _store_validated_data documents this contract.
            pre_read_fp = _config_fingerprint()
            data = {}
            loaded_base = False
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = raw
                        loaded_base = True
                    else:
                        logger.warning("Config is not a JSON object, using defaults")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to load config from %s: %s", path, e)

            # Deep-merge config.local.json overlay (user-owned, never touched by setup)
            local_data: dict = {}
            local_path = config_local_path()
            if local_path.is_file():
                try:
                    st_mode = local_path.stat().st_mode
                    if st_mode & 0o002:
                        logger.warning(
                            "config.local.json is world-writable (%o); "
                            "consider running: chmod 600 %s",
                            st_mode & 0o777,
                            local_path,
                        )
                    raw_local = json.loads(local_path.read_text(encoding="utf-8"))
                    if isinstance(raw_local, dict):
                        local_data = raw_local
                    else:
                        logger.warning("config.local.json is not a JSON object, ignoring")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to load config.local.json: %s", e)

            if local_data:
                data = _deep_merge(data, local_data)

            # Return defaults only if neither file was successfully loaded. Seed
            # the default "kirocrew" agent in-memory (matching the on-disk
            # migration below) so a never-setup home still lists the default
            # agent — but do NOT persist: a plain read (e.g. `agent list`) must
            # not create config files as a side effect. Not cached — there's no
            # file to invalidate against, and the path is already cheap
            # (existence checks only, no read/parse/validate).
            if not loaded_base and not local_data:
                cfg = cls()
                kiro = cfg.agent.default_agent or "kirocrew"
                cfg.agents["default"] = KiroCrewAgentConfig(
                    kiro_agent=kiro,
                    workspace="default",
                    memory_store="default",
                )
                cfg.default_agent = "default"
                return cfg

            # Validate against JSON Schema (advisory — never fatal)
            _validate_config_data(data)
            # Clamp security-relevant resource-limit knobs to their API ceilings
            # BEFORE caching, so a hand-edited/prompt-injected config.json that
            # exceeds a ceiling cannot drive resource exhaustion (DoS). Runs only
            # on the disk-read path; cache hits below already serve clamped values.
            _clamp_security_bounds(data)
            # Cache the validated, merged dict under the PRE-read fingerprint so
            # a mid-read write self-heals (next load misses and re-reads).
            _store_validated_data(data, pre_read_fp)

        agent_data = data.get("agent", {})
        if not isinstance(agent_data, dict):
            agent_data = {}
        session_data = data.get("session", {})
        if not isinstance(session_data, dict):
            session_data = {}
        taskrunner_data = data.get("taskrunner", {})
        cron_history_data = data.get("cron_history", {})
        if not isinstance(cron_history_data, dict):
            cron_history_data = {}
        memory_data = data.get("memory", {})
        if not isinstance(memory_data, dict):
            memory_data = {}
        knowledge_data = data.get("knowledge", {})
        if not isinstance(knowledge_data, dict):
            knowledge_data = {}
        telegram_data = data.get("telegram", {})
        if not isinstance(telegram_data, dict):
            telegram_data = {}
        slack_data = data.get("slack", {})
        if not isinstance(slack_data, dict):
            slack_data = {}
        publish_data = data.get("publish", {})
        if not isinstance(publish_data, dict):
            publish_data = {}
        wechat_data = data.get("wechat", {})
        if not isinstance(wechat_data, dict):
            wechat_data = {}
        dashboard_data = data.get("dashboard", {})
        if not isinstance(dashboard_data, dict):
            dashboard_data = {}
        stt_data = data.get("stt", {})
        if not isinstance(stt_data, dict):
            stt_data = {}
        instances_data = data.get("instances", {})
        if not isinstance(instances_data, dict):
            instances_data = {}
        mcp_gateway_data = data.get("mcp_gateway", {})
        if not isinstance(mcp_gateway_data, dict):
            mcp_gateway_data = {}
        heartbeat_data = data.get("heartbeat", {})
        if not isinstance(heartbeat_data, dict):
            heartbeat_data = {}
        heartbeat_default_deliver = (
            str(heartbeat_data.get("default_deliver", "slack")).strip().lower()
        )
        if heartbeat_default_deliver not in ("slack", "dashboard"):
            heartbeat_default_deliver = "slack"
        tunnel_data = data.get("tunnel", {})
        if not isinstance(tunnel_data, dict):
            tunnel_data = {}
        skills_data = data.get("skills", {})
        if not isinstance(skills_data, dict):
            skills_data = {}
        messaging_data = data.get("messaging", {})
        if not isinstance(messaging_data, dict):
            messaging_data = {}
        telemetry_data = data.get("telemetry", {})
        if not isinstance(telemetry_data, dict):
            telemetry_data = {}
        orchestrator_data = data.get("orchestrator", {})
        if not isinstance(orchestrator_data, dict):
            orchestrator_data = {}
        watchdog_data = data.get("watchdog", {})
        if not isinstance(watchdog_data, dict):
            watchdog_data = {}

        # Parse agents section into dict[str, KiroCrewAgentConfig]
        raw_agents = data.get("agents", {})
        agents: dict[str, KiroCrewAgentConfig] = {}
        if isinstance(raw_agents, dict):
            for name, entry in raw_agents.items():
                if isinstance(entry, dict):
                    agents[name] = KiroCrewAgentConfig(
                        kiro_agent=entry.get("kiro_agent", ""),
                        workspace=entry.get("workspace", "default"),
                        memory_store=entry.get("memory_store", "default"),
                        description=entry.get("description", ""),
                        source=entry.get("source", "kirocrew"),
                    )

        # Migrate workspaces from flat or structured format
        raw_workspaces = data.get("workspaces", {})
        if not isinstance(raw_workspaces, dict):
            raw_workspaces = {}
        workspaces = _migrate_workspaces(raw_workspaces)

        # Parse memory_stores; synthesize default if missing
        raw_stores = data.get("memory_stores", {})
        memory_stores: dict[str, MemoryStoreConfig] = {}
        if isinstance(raw_stores, dict) and raw_stores:
            for name, entry in raw_stores.items():
                if isinstance(entry, dict):
                    memory_stores[name] = MemoryStoreConfig(
                        description=entry.get("description", ""),
                        embedding_provider=entry.get("embedding_provider", ""),
                    )
        if not memory_stores:
            memory_stores["default"] = MemoryStoreConfig()

        # Parse top-level default_agent and default_memory_store
        default_agent_val = data.get("default_agent", "")
        if not isinstance(default_agent_val, str):
            default_agent_val = ""
        default_memory_store_val = data.get("default_memory_store", "default")
        if not isinstance(default_memory_store_val, str):
            default_memory_store_val = "default"

        cfg = cls(
            agent=AgentConfig(
                approval_mode=agent_data.get("approval_mode", "auto"),
                streaming=agent_data.get("streaming", True),
                model=agent_data.get("model", DEFAULT_MODEL),
                provider=agent_data.get("provider", "acp"),
                default_agent=agent_data.get("default_agent", ""),
                sandbox=agent_data.get("sandbox", "auto"),
                sandbox_allow_no_isolation=bool(
                    agent_data.get("sandbox_allow_no_isolation", False)
                ),
                sandbox_allow_unsandboxed_exec=bool(
                    agent_data.get("sandbox_allow_unsandboxed_exec", False)
                ),
                apps_allow_third_party=bool(agent_data.get("apps_allow_third_party", True)),
                jail=_normalize_jail(agent_data.get("jail", "auto")),
                yolo=agent_data.get("yolo", False),
                notify_override_expiry=agent_data.get("notify_override_expiry", True),
                conductor_skill=agent_data.get("conductor_skill", False),
                tool_search=bool(agent_data.get("tool_search", True)),
                session_sharing=bool(agent_data.get("session_sharing", True)),
                max_subagents=agent_data.get("max_subagents", 0),
                subagent_mem_buffer_pct=int(agent_data.get("subagent_mem_buffer_pct", 20)),
                subagent_cost_gb=float(agent_data.get("subagent_cost_gb", 0.5)),
                subagent_cpu_cost_cores=float(agent_data.get("subagent_cpu_cost_cores", 1.0)),
                subagent_auto_max=int(agent_data.get("subagent_auto_max", 32)),
                subagent_spawn_stagger_secs=float(
                    agent_data.get("subagent_spawn_stagger_secs", 2.0)
                ),
                subagent_max_turns=agent_data.get("subagent_max_turns", 100),
                subagent_timeout_secs=agent_data.get("subagent_timeout_secs", 1800),
                completion_keep=_validated_completion_keep(
                    agent_data.get("completion_keep", "head")
                ),
                completion_keep_chars=int(agent_data.get("completion_keep_chars", 3000)),
                subagent_result_ttl_secs=int(agent_data.get("subagent_result_ttl_secs", 3600)),
                subagent_cwd_allowed_roots=list(
                    agent_data.get(
                        "subagent_cwd_allowed_roots",
                        ["~/workspace", "~/workspaces", "~/workplace", "~/workplaces"],
                    )
                ),
                log_level=agent_data.get("log_level", "WARNING").upper(),
                enforce_denied_commands=agent_data.get("enforce_denied_commands", "all")
                .lower()
                .strip(),
                bot_name=_sanitize_bot_name(agent_data.get("bot_name", "")),
                max_channels=agent_data.get("max_channels", 1),
                max_channel_agents=agent_data.get("max_channel_agents", 3),
                soft_stop_budget_secs=max(
                    0.5, min(60.0, float(agent_data.get("soft_stop_budget_secs", 10.0)))
                ),
            ),
            session=SessionConfig(
                timeout_secs=session_data.get("timeout_secs", DEFAULT_SESSION_TIMEOUT),
                autocompact_pct=float(session_data.get("autocompact_pct", 90.0)),
                pool_size=int(session_data.get("pool_size", 2)),
                pool_agent=str(session_data.get("pool_agent", "")),
                pool_ttl_secs=int(session_data.get("pool_ttl_secs", 1800)),
                archive_retention_days=_archive_retention_days(session_data),
                watchdog_rss_max_mb=int(session_data.get("watchdog_rss_max_mb", 0)),
            ),
            taskrunner=TaskRunnerConfig(
                max_parallel_steps=taskrunner_data.get(
                    "max_parallel_steps", DEFAULT_MAX_PARALLEL_STEPS
                ),
                workspace_dir=str(taskrunner_data.get("workspace_dir", "")),
            ),
            cron_history=CronHistoryConfig(
                cron_summary_cap=int(cron_history_data.get("cron_summary_cap", 200)),
                cron_trace_cap_kb=int(cron_history_data.get("cron_trace_cap_kb", 50)),
                cron_max_records_per_job=int(
                    cron_history_data.get("cron_max_records_per_job", 100)
                ),
                cron_max_index_records=int(cron_history_data.get("cron_max_index_records", 2000)),
            ),
            messaging=MessagingConfig(
                use_transport=bool(messaging_data.get("use_transport", True)),
                dm_scope=str(messaging_data.get("dm_scope", "per-channel-peer")),
                idle_reset_minutes=_coerce_int(messaging_data.get("idle_reset_minutes"), 0),
                daily_reset_hour=_coerce_int(messaging_data.get("daily_reset_hour"), -1),
                queue_mode=str(messaging_data.get("queue_mode", "steer")),
            ),
            # orchestrator/watchdog were advertised in config-baseline.json and
            # served by /api/config/schema, and real consumers read them
            # (acp/session_handle.py, dashboard/chat_orchestrator.py), but load()
            # never passed these kwargs — so config.json values were silently
            # ignored and the dataclass defaults always won.
            orchestrator=OrchestratorConfig(
                stage_timeout_seconds=_safe_int(
                    orchestrator_data.get("stage_timeout_seconds", 1800), 1800
                ),
            ),
            watchdog=WatchdogConfig(
                check_after_secs=_safe_float(
                    watchdog_data.get("check_after_secs", 60.0), 60.0
                ),
                stale_window_secs=_safe_float(
                    watchdog_data.get("stale_window_secs", 300.0), 300.0
                ),
                tool_stall_suspect_secs=_safe_float(
                    watchdog_data.get("tool_stall_suspect_secs", 600.0), 600.0
                ),
                tool_stall_hard_cap_secs=_safe_float(
                    watchdog_data.get("tool_stall_hard_cap_secs", 2700.0), 2700.0
                ),
                model_silent_probe_secs=_safe_float(
                    watchdog_data.get("model_silent_probe_secs", 900.0), 900.0
                ),
                wellness_sample_secs=_safe_float(
                    watchdog_data.get("wellness_sample_secs", 3.0), 3.0
                ),
            ),
            telemetry=TelemetryConfig(
                enabled=bool(telemetry_data.get("enabled", False)),
                local_dir=str(telemetry_data.get("local_dir", "")),
                export_interval_seconds=int(telemetry_data.get("export_interval_seconds", 60)),
            ),
            memory=MemoryConfig(
                embedding_provider=_coerce_embedding_provider(
                    memory_data.get("embedding_provider", "llama_cpp")
                ),
                embedding_dim=memory_data.get("embedding_dim", 1024),
                embed_model_url=memory_data.get("embed_model_url", ""),
                semantic_confidence_threshold=memory_data.get("semantic_confidence_threshold", 0.8),
                episodic_dedup_threshold=memory_data.get("episodic_dedup_threshold", 0.88),
                episodic_max_results=memory_data.get("episodic_max_results", 8),
                episodic_max_count=memory_data.get("episodic_max_count", 10_000),
                semantic_keys=memory_data.get("semantic_keys", []),
                history_idle_hours=memory_data.get("history_idle_hours", 3.0),
                history_max_days=memory_data.get("history_max_days", 365),
                migrated=memory_data.get("migrated", False),
            ),
            knowledge=KnowledgeConfig(
                auto_ingest_artifacts=bool(knowledge_data.get("auto_ingest_artifacts", True)),
                auto_ingest_artifact_kinds=[
                    k
                    for k in knowledge_data.get(
                        "auto_ingest_artifact_kinds",
                        DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
                    )
                    if isinstance(k, str)
                ],
                max_ingest_file_mb=(
                    float(mb)
                    if isinstance(
                        (mb := knowledge_data.get("max_ingest_file_mb", 100.0)),
                        (int, float),
                    )
                    and not isinstance(mb, bool)
                    and mb >= 0
                    else 100.0
                ),
                embed_timeout_secs=float(knowledge_data.get("embed_timeout_secs", 10.0)),
                embed_content_budget=int(knowledge_data.get("embed_content_budget", 0)),
                pool_idle_ttl_secs=(
                    ttl
                    if isinstance(
                        (ttl := knowledge_data.get("pool_idle_ttl_secs", 300)), int
                    )
                    and not isinstance(ttl, bool)
                    and ttl >= 0
                    else 300
                ),
            ),
            telegram=TelegramConfig(
                enabled=bool(telegram_data.get("enabled", False)),
                bot_token=str(telegram_data.get("bot_token", "")),
                allowed_user_ids=_coerce_int_ids(telegram_data.get("allowed_user_ids")),
                soft_threshold_pct=max(
                    1, min(100, _coerce_int(telegram_data.get("soft_threshold_pct"), 80))
                ),
            ),
            slack=SlackConfig(
                allowed_users=[
                    u
                    for u in slack_data.get("allowed_users", [])
                    if isinstance(u, dict) and u.get("slack_id")
                ],
                tracking_channels=_validate_tracking_channels(
                    slack_data.get("tracking_channels", [])
                ),
                open_channels=[
                    c for c in slack_data.get("open_channels", []) if isinstance(c, str)
                ],
                command=slack_data.get("command", "kirocrew"),
                forward_to_agent_callback=str(
                    slack_data.get("forward_to_agent_callback") or ""
                ).strip(),
                trusted_bot_ids=set(slack_data.get("trusted_bot_ids", [])),
                allowed_enterprise_ids=[
                    e
                    for e in slack_data.get("allowed_enterprise_ids", [])
                    if isinstance(e, str) and (e.startswith("E") or e.startswith("T"))
                ],
                reactions={
                    k: v
                    for k, v in slack_data.get("reactions", {}).items()
                    if isinstance(k, str) and (v is None or (isinstance(v, str) and v))
                },
                reactions_enabled=bool(slack_data.get("reactions_enabled", True)),
                use_tunnel_url=bool(slack_data.get("use_tunnel_url", False)),
                show_thinking=bool(slack_data.get("show_thinking", True)),
            ),
            publish=PublishConfig(
                allowed_destinations=[
                    d
                    for d in publish_data.get("allowed_destinations", [])
                    if isinstance(d, str) and d
                ],
                relocate_roots=[
                    r
                    for r in publish_data.get("relocate_roots", [])
                    if isinstance(r, str) and r.strip()
                ],
            ),
            wechat=WeComConfig(
                enabled=bool(wechat_data.get("enabled", False)),
                allowed_users=[
                    u
                    for u in wechat_data.get("allowed_users", [])
                    if isinstance(u, dict) and u.get("userid")
                ],
                ws_url=str(wechat_data.get("ws_url", "wss://openws.work.weixin.qq.com")),
                soft_threshold_pct=int(wechat_data.get("soft_threshold_pct", 80)),
                hard_threshold_pct=int(wechat_data.get("hard_threshold_pct", 95)),
            ),
            dashboard=DashboardConfig(
                url=dashboard_data.get("url", ""),
                restore_sessions=dashboard_data.get("restore_sessions", False),
                restore_window_minutes=dashboard_data.get("restore_window_minutes", 30),
                bot_name=dashboard_data.get("bot_name", ""),
                avatar=dashboard_data.get("avatar", ""),
                merge_queued_messages=dashboard_data.get("merge_queued_messages", False),
                mcp_probe_timeout_secs=_safe_int(
                    dashboard_data.get("mcp_probe_timeout_secs", 15), 15
                ),
                auto_open_browser=dashboard_data.get("auto_open_browser", True),
                quick_send=dashboard_data.get("quick_send", False),
                session_grid=dashboard_data.get("session_grid", False),
                widget_density=dashboard_data.get("widget_density", "more"),
                tail_fork_enabled=dashboard_data.get("tail_fork_enabled", False),
                terminal=dashboard_data.get("terminal", {"enabled": True}),
                default_project=dashboard_data.get("default_project", ""),
                theme_mode=dashboard_data.get("theme_mode", ""),
                theme_color=dashboard_data.get("theme_color", ""),
                recent_tint_count=_safe_int(dashboard_data.get("recent_tint_count", 0), 0),
                onboarded=bool(dashboard_data.get("onboarded", False)),
                tips_enabled=bool(dashboard_data.get("tips_enabled", True)),
                tips_cadence_hours=_safe_float(dashboard_data.get("tips_cadence_hours", 6.0), 6.0, lo=0.0),
                tips_snooze_hours=_safe_float(dashboard_data.get("tips_snooze_hours", 48.0), 48.0, lo=0.0),
                tips_recency_decay=_safe_float(dashboard_data.get("tips_recency_decay", 0.6), 0.6, lo=0.0, hi=1.0),
                tips_model=str(dashboard_data.get("tips_model", "claude-haiku-4.5")),
                tips_explore_ratio=_safe_float(dashboard_data.get("tips_explore_ratio", 0.2), 0.2, lo=0.0, hi=1.0),
            ),
            tunnel=TunnelConfig(
                enabled=bool(tunnel_data.get("enabled", False)),
                name_mode=str(tunnel_data.get("name_mode", "username")),
                name_override=str(tunnel_data.get("name_override", "")),
            ),
            hooks=data.get("hooks", {}),
            agents=agents,
            default_agent=default_agent_val,
            workspaces=workspaces,
            default_workspace=data.get("default_workspace", "default"),
            memory_stores=memory_stores,
            default_memory_store=default_memory_store_val,
            stt=SttConfig(
                enabled=stt_data.get("enabled", False),
                provider=_validated_stt_provider(stt_data.get("provider", "whisper")),
                whisper_path=stt_data.get("whisper_path", ""),
                # Default changed from "base" to "turbo" — turbo is faster and
                # recommended for most users (809M vs 74M, but much better latency).
                model=stt_data.get("model", "turbo"),
                mlx_model=stt_data.get("mlx_model", "mlx-community/whisper-large-v3-turbo"),
                device=stt_data.get("device", "cpu"),
                timeout_secs=stt_data.get("timeout_secs", 300),
                transcribe_region=stt_data.get("transcribe_region", "us-east-1"),
                transcribe_profile=stt_data.get("transcribe_profile", ""),
                language_code=stt_data.get("language_code", "en-US"),
                streaming=stt_data.get("streaming", False),
            ),
            auto_update=data.get("auto_update", True),
            timezone=data.get("timezone", ""),
            snapshot_dir=data.get("snapshot_dir", ""),
            registries=[
                ExternalRegistryConfig(
                    name=str(r.get("name", "")),
                    repo=str(r.get("repo", "")),
                    branch=str(r.get("branch", "mainline")),
                )
                for r in (data.get("registries") or [])
                if isinstance(r, dict) and r.get("repo")
            ],
            mcp_gateway=McpGatewayConfig(
                enabled=bool(mcp_gateway_data.get("enabled", False)),
                socket_path=str(mcp_gateway_data.get("socket_path", "")),
                overlay_dir=str(mcp_gateway_data.get("overlay_dir", "")),
                idle_timeout_secs=max(10, int(mcp_gateway_data.get("idle_timeout_secs", 300))),
                max_backends=max(1, int(mcp_gateway_data.get("max_backends", 64))),
                poolable_servers=[
                    s for s in mcp_gateway_data.get("poolable_servers", []) if isinstance(s, str)
                ],
                prewarm_count=max(0, int(mcp_gateway_data.get("prewarm_count", 0))),
                read_buffer_limit_bytes=max(1024, int(mcp_gateway_data.get("read_buffer_limit_bytes", 64 * 1024 * 1024))),
                response_spill_threshold_bytes=max(0, int(mcp_gateway_data.get("response_spill_threshold_bytes", 256 * 1024))),
            ),
            instances=InstancesConfig(
                enabled=bool(instances_data.get("enabled", False)),
                warm_set_cap=int(instances_data.get("warm_set_cap", _DEFAULT_WARM_SET_CAP)),
                tunnel_base_port=int(
                    instances_data.get("tunnel_base_port", _DEFAULT_TUNNEL_BASE_PORT)
                ),
                ssh_compression=bool(
                    instances_data.get("ssh_compression", _DEFAULT_SSH_COMPRESSION)
                ),
                max_recovery_attempts=int(
                    instances_data.get("max_recovery_attempts", _DEFAULT_MAX_RECOVERY)
                ),
                recover_backoff_max_secs=float(
                    instances_data.get("recover_backoff_max_secs", _DEFAULT_BACKOFF_MAX)
                ),
                probe_failure_threshold=int(
                    instances_data.get("probe_failure_threshold", _DEFAULT_PROBE_FAILS)
                ),
            ),
            heartbeat=HeartbeatConfig(default_deliver=heartbeat_default_deliver),
            skills=SkillsConfig(
                max_triggered=int(skills_data.get("max_triggered", 3)),
                lazy_load=bool(skills_data.get("lazy_load", False)),
                auto_create_from_sessions=bool(skills_data.get("auto_create_from_sessions", False)),
                auto_refine_on_deviation=bool(skills_data.get("auto_refine_on_deviation", False)),
                auto_min_tool_calls=int(skills_data.get("auto_min_tool_calls", 5)),
                auto_similarity_threshold=float(skills_data.get("auto_similarity_threshold", 0.85)),
                extra_paths=list(skills_data.get("extra_paths", [])),
            ),
            slack_channels={
                ch_id: ChannelConfig.from_dict(ch_data)
                for ch_id, ch_data in data.get("slack", {}).get("channels", {}).items()
                if isinstance(ch_data, dict)
            },
            slack_dm_activation=_validate_activation(
                data.get("slack", {}).get("dm_activation", ACTIVATION_ALWAYS)
            ),
            observe_max_messages=max(
                1, int(data.get("slack", {}).get("observe_max_messages", 200))
            ),
            observe_ttl_hours=max(
                0.0, float(data.get("slack", {}).get("observe_ttl_hours", 168.0))
            ),
        )

        # Write-back migration: if the on-disk config has legacy format
        # (flat workspace strings, missing sections), back up the original
        # and save the migrated version.  One-shot — subsequent loads see
        # the canonical format and skip.
        try:
            needs_migration = False
            # Flat workspace strings → need migration to {"dir": ...}
            for v in raw_workspaces.values():
                if isinstance(v, str):
                    needs_migration = True
                    break

            # One-time migration: create default agent when none exists
            if not cfg.agents:
                kiro = cfg.agent.default_agent or "kirocrew"
                cfg.agents["default"] = KiroCrewAgentConfig(
                    kiro_agent=kiro,
                    workspace="default",
                    memory_store="default",
                )
                needs_migration = True
            if not cfg.default_agent or cfg.default_agent not in cfg.agents:
                # Prefer "default" if it exists, otherwise use first available agent
                if "default" in cfg.agents:
                    cfg.default_agent = "default"
                elif cfg.agents:
                    cfg.default_agent = next(iter(cfg.agents))
                else:
                    cfg.default_agent = "default"
                needs_migration = True

            if needs_migration:
                backup = path.with_suffix(".json.bak")
                import shutil

                shutil.copy2(path, backup)
                logger.info(
                    "Config migrated — backup saved to %s",
                    backup,
                )
                cfg.save()
        except Exception as e:
            # Migration write-back is best-effort; never block startup.
            logger.warning("Config write-back failed: %s", e)

        return cfg

    def to_dict(self) -> dict:
        """Serialize config to the JSON structure used by config.json."""
        from dataclasses import asdict

        d: dict = {
            "agent": asdict(self.agent),
            "session": asdict(self.session),
            "memory": asdict(self.memory),
            "slack": asdict(self.slack),
            "publish": asdict(self.publish),
            "telegram": asdict(self.telegram),
            "dashboard": asdict(self.dashboard),
            "tunnel": asdict(self.tunnel),
            "hooks": self.hooks,
            "agents": {name: asdict(agent_cfg) for name, agent_cfg in self.agents.items()},
            "default_agent": self.default_agent,
            "workspaces": {name: asdict(ws_cfg) for name, ws_cfg in self.workspaces.items()},
            "default_workspace": self.default_workspace,
            "memory_stores": {name: asdict(ms_cfg) for name, ms_cfg in self.memory_stores.items()},
            "default_memory_store": self.default_memory_store,
            "stt": asdict(self.stt),
            "instances": asdict(self.instances),
            "mcp_gateway": asdict(self.mcp_gateway),
            "taskrunner": asdict(self.taskrunner),
            "orchestrator": asdict(self.orchestrator),
            "watchdog": asdict(self.watchdog),
            "messaging": asdict(self.messaging),
            "cron_history": asdict(self.cron_history),
            "knowledge": asdict(self.knowledge),
            "heartbeat": asdict(self.heartbeat),
            "skills": asdict(self.skills),
            "telemetry": asdict(self.telemetry),
            "snapshot_dir": self.snapshot_dir,
            "timezone": self.timezone,
            "auto_update": self.auto_update,
        }
        # External registries (always serialized so save() round-trips the field)
        d["registries"] = [asdict(r) for r in self.registries]
        # Preserve per-channel activation settings on round-trip
        slack_section = d.setdefault("slack", {})
        if self.slack_channels:
            slack_section["channels"] = {
                ch_id: asdict(cfg) for ch_id, cfg in self.slack_channels.items()
            }
        if self.slack_dm_activation != ACTIVATION_ALWAYS:
            slack_section["dm_activation"] = self.slack_dm_activation
        slack_section["observe_max_messages"] = self.observe_max_messages
        if self.slack.trusted_bot_ids:
            slack_section["trusted_bot_ids"] = sorted(self.slack.trusted_bot_ids)
        else:
            slack_section.pop("trusted_bot_ids", None)
        slack_section["observe_ttl_hours"] = self.observe_ttl_hours
        return d

    def save(self) -> None:
        """Write current config to ~/.kirocrew/config.json.

        Stamps a ``meta`` block with the current version and timestamp
        so we can tell which build last touched the file.

        Values that exist in ``config.local.json`` are stripped from the
        output to prevent overlay settings from leaking into the base file.
        """
        from kiro_crew.config.loader import (
            _invalidate_config_cache,
            _subtract_overlay,
            config_local_path,
            config_path,
        )

        meta = {
            "lastTouchedVersion": __version__,
            "lastTouchedAt": datetime.now(timezone.utc).isoformat(),
        }
        d = self.to_dict()

        # Strip overlay-owned values so they don't leak into config.json
        local_path = config_local_path()
        if local_path.is_file():
            try:
                raw_local = json.loads(local_path.read_text(encoding="utf-8"))
                if isinstance(raw_local, dict):
                    d = _subtract_overlay(d, raw_local)
            except (json.JSONDecodeError, OSError):
                pass

        d = {"meta": meta, **d}
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        # Drop the validated-data cache so the next load() re-reads this write.
        # mtime-keying already detects the change; this makes it immediate even
        # if the filesystem mtime resolution is coarse.
        _invalidate_config_cache()

    @staticmethod
    def _resolve_agent_model() -> str:
        """Read model from installed agent config, falling back to bundled defaults."""
        # Installed agent config (generated by kirocrew setup)
        agent_json = Path.home() / ".kiro" / "agents" / "kirocrew.json"
        if agent_json.is_file():
            try:
                data = json.loads(agent_json.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if model:
                    return model
            except (json.JSONDecodeError, OSError):
                pass
        # Bundled defaults.json
        bundled = config_package_dir() / "defaults.json"
        if bundled.is_file():
            try:
                data = json.loads(bundled.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if model:
                    return model
            except (json.JSONDecodeError, OSError):
                pass
        return DEFAULT_MODEL

    @staticmethod
    def _resolve_named_agent_model(agent: str, agents_dir: Path | None = None) -> str:
        """Return a named agent's own kiro ``model`` field, or ``""`` if none.

        Used by :meth:`SessionManager.get_or_create` so an explicit global
        ``agent.model`` does not override an agent that pins its own model — the
        global default must rank *below* a per-agent pin. Returns the kiro
        ``model`` slot only; ``""`` when the agent declares none, so the caller
        falls back to the global. ``agents_dir`` overrides the lookup directory
        (a dependency-injection seam for tests); defaults to ``kiro_agents_dir()``.
        """
        if not agent:
            return ""
        base = agents_dir if agents_dir is not None else kiro_agents_dir()
        for af in base.glob("*.json"):
            try:
                ad = json.loads(af.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            # Skip stray non-object JSON a user may have dropped in the dir.
            if isinstance(ad, dict) and (ad.get("name") == agent or af.stem == agent):
                return ad.get("model") or ""
        return ""

    def load_credentials(self) -> dict[str, str]:
        """Load credentials from ~/.kirocrew/.env and environment variables.

        .env format: KEY=VALUE (one per line, # comments, no quotes required).
        Environment variables override .env values.
        """
        from kiro_crew.config.loader import _CREDENTIAL_KEYS, env_path

        creds: dict[str, str] = {}
        ep = env_path()
        if ep.exists():
            # Enforce restrictive permissions on credential file
            try:
                if ep.stat().st_mode & 0o077:
                    ep.chmod(0o600)
            except OSError:
                logger.warning("Cannot enforce permissions on %s", ep)
            for line in ep.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()

        for key in _CREDENTIAL_KEYS:
            val = os.environ.get(key)
            if val:
                creds[key] = val

        # Propagate credentials into the process environment so spawned children
        # (sandboxed agents, MCP servers, cron-fired subprocesses) inherit them
        # via Popen's default env=os.environ.copy() — even when their view of
        # ~/.kirocrew/.env is a bind-mounted empty file. setdefault() preserves
        # any value the caller already set explicitly.
        for k, v in creds.items():
            if v:
                os.environ.setdefault(k, v)

        return creds

    def create_provider_factory(self) -> Callable:
        """Return a factory that creates LLMProvider instances from config.

        KiroCrew is KiroACP-only: the sole provider is the ACP adapter driving
        the kiro-cli backend. The factory accepts an optional ``session_key`` to
        create a per-session subdirectory under ``workspace_root()``.
        """
        from kiro_crew.config.loader import _session_work_dir
        from kiro_crew.providers.acp import (
            AcpProvider,  # circular: acp -> client -> session -> config.loader
        )

        model = self.agent.model
        if model == DEFAULT_MODEL:
            model = self._resolve_agent_model()

        sandbox = self.agent.sandbox
        tool_search = self.agent.tool_search

        # MCP gateway: resolve overlay + socket once when enabled. None when
        # the feature flag is off -> AcpClient falls through to per-session MCP.
        _gw = self.mcp_gateway
        if _gw.enabled:
            _gw_overlay = _gw.overlay_dir or str(default_overlay_dir())
            _gw_socket = _gw.socket_path or str(default_socket_path())
            _gw_settings = str(Path(_gw_overlay).parent / "settings" / "mcp.json")
        else:
            _gw_overlay = None
            _gw_socket = None
            _gw_settings = None

        def _acp(
            session_key: str | None = None,
            agent: str | None = None,
            channel_id: str | None = None,
            model_override: str | None = None,
            cwd: str | None = None,
            extra_env: dict[str, str] | None = None,
            reasoning_effort_override: str | None = None,
            **_kwargs: object,
        ) -> AcpProvider:
            wdir = Path(cwd) if cwd else _session_work_dir(session_key)
            # Custom agents use their own model from their agent config;
            # only override model for the default kirocrew agent.
            # If model_override is provided (from slot.model), use it.
            if model_override:
                m = model_override
            elif not agent or agent == "kirocrew":
                m = model
            else:
                m = None
            # Thread the slot's effort into a per-model override so the kiro
            # cli.json overlay is written from it at spawn — without this, a
            # kiro cold start (or the handler's reset-then-respawn) would only
            # pick up effort already recovered from a pre-existing overlay,
            # never the freshly-set slot value. Mirrors the _claude_code path.
            _eff_per_model: dict[str, str] = {}
            if (
                m
                and reasoning_effort_override
                and is_valid_effort(reasoning_effort_override)
                and model_supports_effort(m)
            ):
                _eff_per_model[m] = reasoning_effort_override
            return AcpProvider(
                work_dir=wdir,
                model=m,
                agent=agent,
                sandbox_mode=sandbox,
                session_key=session_key,
                channel_id=channel_id,
                extra_env=extra_env,
                effort_per_model=_eff_per_model,
                tool_search=tool_search,
                mcp_gateway_overlay=_gw_overlay,
                mcp_gateway_settings_mcp_json=_gw_settings,
                mcp_gateway_socket=_gw_socket,
            )

        return _acp
