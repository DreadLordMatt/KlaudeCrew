"""Per-feature configuration dataclasses for KiroCrew.

Extracted from ``config/loader.py``: every ``*Config`` DTO plus the small
coercion / validation helpers and constants they use. Depends only on the
standard library and ``kiro_crew.instances.constants`` — it is a leaf in the
config import graph, so ``loader`` / ``root`` / ``bindings`` all import from it
without a cycle.

All names here are re-exported from ``kiro_crew.config.loader`` for backward
compatibility. Warnings are emitted on the ``kiro_crew.config.loader`` logger
channel (the loader's observable contract), not this module's own ``__name__``.
"""

from __future__ import annotations

import logging
import re as _re
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _DEFAULT_MAX_RECOVERY
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _DEFAULT_PROBE_FAILS
from kiro_crew.instances.constants import DEFAULT_RECOVER_BACKOFF_MAX_SECS as _DEFAULT_BACKOFF_MAX
from kiro_crew.instances.constants import DEFAULT_SSH_COMPRESSION as _DEFAULT_SSH_COMPRESSION
from kiro_crew.instances.constants import DEFAULT_TUNNEL_BASE_PORT as _DEFAULT_TUNNEL_BASE_PORT
from kiro_crew.instances.constants import DEFAULT_WARM_SET_CAP as _DEFAULT_WARM_SET_CAP
from kiro_crew.instances.constants import MAX_RECOVERY_ATTEMPTS_CEILING as _MAX_RECOVERY_CEILING
from kiro_crew.instances.constants import (
    RECOVER_BACKOFF_MAX_CEILING_SECS as _RECOVER_BACKOFF_CEILING,
)

logger = logging.getLogger("kiro_crew.config.loader")


DEFAULT_MODEL = "auto"
DEFAULT_SESSION_TIMEOUT = 3600  # 60 min
DEFAULT_MAX_PARALLEL_STEPS = 0  # 0 = auto: derive from agent.subagent_auto_max via compute_max_subagents


def _meta(label: str, help: str, **kwargs: object) -> dict:
    """Helper to build field metadata dicts with safe defaults."""
    return {"label": label, "help": help, **kwargs}


_BOT_NAME_MAX = 50
_BOT_NAME_RE = _re.compile(r"[^a-zA-Z0-9 _\-.]")


def _sanitize_bot_name(raw: str) -> str:
    """Sanitize bot_name: strip markdown, braces, limit length."""
    if not isinstance(raw, str):
        return ""
    name = raw.strip()[:_BOT_NAME_MAX]
    name = name.replace("{", "").replace("}", "")
    return _BOT_NAME_RE.sub("", name)


def _archive_retention_days(session_data: dict) -> int:
    """Resolve session.archive_retention_days, normalizing the disable sentinel.

    ``null`` (absent/None in JSON) and any negative value both mean "disable
    automatic cleanup"; both normalize to ``-1``.  A non-negative integer is the
    retention window in days.  Defaults to 30 when unset.
    """
    raw = session_data.get("archive_retention_days", 30)
    if raw is None:
        return -1
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 30
    return val if val >= 0 else -1


# Process-isolation jail modes (``agent.jail``).  Single source of truth shared by
# ``_normalize_jail``, the ``AgentConfig.jail`` field metadata enum, and tests —
# a new mode added in one place can't silently normalize back to the default.
JAIL_MODE_AUTO = "auto"
JAIL_MODE_ON = "on"
JAIL_MODE_OFF = "off"
_VALID_JAIL_MODES = (JAIL_MODE_AUTO, JAIL_MODE_ON, JAIL_MODE_OFF)


@dataclass
class AgentConfig:
    approval_mode: str = field(
        default="auto",
        metadata=_meta("Approval Mode", "Tool approval mode.", enum=["auto", "interactive"]),
    )
    streaming: bool = field(
        default=True,
        metadata=_meta("Streaming", "Enable streaming responses."),
    )
    model: str = field(
        default=DEFAULT_MODEL,
        metadata=_meta("Model", "LLM model identifier. 'auto' resolves from agent config."),
    )
    provider: str = field(
        default="acp",
        metadata=_meta("Provider", "LLM provider backend (KiroACP / kiro-cli).", enum=["acp"]),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Default agent name for new sessions."),
    )
    sandbox: str = field(
        default="auto",
        metadata=_meta("Sandbox", "Sandbox mode for ACP provider.", enum=["auto", "off"]),
    )
    sandbox_allow_no_isolation: bool = field(
        default=False,
        metadata=_meta(
            "Allow No-Isolation Fallback",
            "Acknowledge running the agent subprocess WITHOUT OS-level credential "
            "isolation when no sandbox backend is available (e.g. macOS >= 26, or "
            "Linux without user namespaces). When false (default), that fallback is "
            "logged as a loud SECURITY warning. When true, the operator has accepted "
            "the risk and it is logged at info level.",
        ),
    )
    sandbox_allow_unsandboxed_exec: bool = field(
        default=False,
        metadata=_meta(
            "Allow Unsandboxed Execution",
            "When true, allow agent subprocesses to execute without any sandbox "
            "backend (fail-open). When false (default), wrap_argv raises a "
            "RuntimeError if no sandbox backend is available and mode is not 'off', "
            "preventing unsandboxed execution entirely (fail-closed). This is "
            "distinct from sandbox_allow_no_isolation which only controls warning "
            "severity — this field controls whether execution proceeds at all.",
        ),
    )
    apps_allow_third_party: bool = field(
        default=True,
        metadata=_meta(
            "Allow Third-Party Apps",
            "Allow running third-party (non-builtin) app Python. App code runs with "
            "FULL gateway privileges (filesystem, network, in-memory credentials) and "
            "is NOT sandboxed — the permission system gates only the SDK tool surface. "
            "Defaults to true (apps are operator-installed). Set false to refuse both "
            "in-process module loads AND out-of-process backend spawns for any app "
            "outside apps/builtins/ until out-of-process isolation ships (CSE SEC-012).",
        ),
    )
    jail: str = field(
        default=JAIL_MODE_AUTO,
        metadata=_meta(
            "Jail",
            "Process-isolation jail mode for agent-bearing commands. 'auto' uses a "
            "jail when the active edition supplies a working backend (the public "
            "edition has none, so 'auto' and 'on' are no-ops there); 'off' disables "
            "it. Disable per-invocation with --no-jail or KIROCREW_NO_JAIL=1.",
            enum=list(_VALID_JAIL_MODES),
        ),
    )
    yolo: bool = field(
        default=False,
        metadata=_meta("YOLO Mode", "Skip tool approval confirmations."),
    )
    notify_override_expiry: bool = field(
        default=True,
        metadata=_meta(
            "Notify on Override Expiry",
            "DM the Slack owner when the time-limited safety override (YOLO) expires. "
            "Disable to silence the recurring expiry DM; the dashboard banner still shows.",
        ),
    )
    bot_name: str = field(
        default="",
        metadata=_meta(
            "Bot Name",
            "Custom name the bot identifies as in conversations. Leave empty for default.",
        ),
    )
    conductor_skill: bool = field(
        default=False,
        metadata=_meta(
            "Conductor Skill",
            "Enable agent delegation — loads conductor skill with agent roster.",
        ),
    )
    tool_search: bool = field(
        default=True,
        metadata=_meta(
            "MCP Tool Search",
            "Load MCP tool specs on demand (search-and-call) instead of sending "
            "every tool definition each turn, keeping the context window clear "
            "when many MCP servers are configured. kiro-cli backend only. When "
            "enabled, KiroCrew forces deferral always-on (minPct=0/minTokens=0) "
            "via the per-session kiro settings overlay; disabling reverts to "
            "sending full tool specs. No effect on the Claude Code backend.",
        ),
    )
    session_sharing: bool = field(
        default=True,
        metadata=_meta(
            "Session Sharing",
            "Subagents reuse a shared ACP runtime instead of spawning a fresh "
            "kiro-cli process per subagent. Reduces startup from ~3-5s to ~200ms "
            "and memory from ~400MB to near-zero per subagent. Default ON for the "
            "kiro-cli backend; always off / ignored for Claude Code (which uses "
            "AcpClient). Set false to opt kiro back onto per-subagent processes.",
        ),
    )
    max_subagents: int = field(
        default=0,
        metadata=_meta(
            "Max SubAgents",
            "Maximum amount of subagents at one time. 0 = auto-size the cap at "
            "startup from host memory/CPU and a learned per-agent cost "
            "(see dynamic-subagent-sizing docs). Default; set a positive integer "
            "to pin a fixed cap.",
        ),
    )
    spawn_min_memory_gb: float = field(
        default=4.0,
        metadata=_meta(
            "Spawn Min Memory GB",
            "Minimum available memory (GB) required to spawn a subagent. 0 disables the check.",
        ),
    )
    subagent_mem_buffer_pct: int = field(
        default=20,
        metadata=_meta(
            "SubAgent Memory Buffer %",
            "Percent of available memory and CPU reserved for the OS and other "
            "processes when auto-sizing the subagent cap (max_subagents=0).",
        ),
    )
    subagent_cost_gb: float = field(
        default=0.5,
        metadata=_meta(
            "SubAgent Memory Cost (GB)",
            "First-boot per-agent memory-cost fallback (GB) used to auto-size the "
            "cap until a learned value accumulates.",
        ),
    )
    subagent_cpu_cost_cores: float = field(
        default=1.0,
        metadata=_meta(
            "SubAgent CPU Cost (cores)",
            "First-boot per-agent CPU-cost fallback (cores) used to auto-size the "
            "cap until a learned value accumulates.",
        ),
    )
    subagent_auto_max: int = field(
        default=32,
        metadata=_meta(
            "SubAgent Auto-Size Max",
            "Ceiling on the auto-sized subagent cap (only applies when "
            "max_subagents=0). Stands in for the LLM-provider concurrency limit "
            "the local memory/CPU formula does not model. Ignored when "
            "max_subagents is set explicitly.",
        ),
    )
    subagent_spawn_stagger_secs: float = field(
        default=2.0,
        metadata=_meta(
            "SubAgent Spawn Stagger (seconds)",
            "Delay between successive subagent spawns (initial fill and queued "
            "drain) to bound cold-start CPU/memory spikes.",
        ),
    )
    subagent_max_turns: int = field(
        default=100,
        metadata=_meta("SubAgent Max Turns", "Default tool-call budget per subagent."),
    )
    subagent_timeout_secs: int = field(
        default=1800,
        metadata=_meta(
            "SubAgent Timeout (seconds)",
            "Wall-clock timeout per subagent execution. 0 uses hardcoded default (1800s).",
        ),
    )
    completion_keep: str = field(
        default="head",
        metadata=_meta(
            "Completion Keep",
            "Which end of the subagent transcript to keep in the completion event "
            "injected into the parent session. Three values: 'head' (first N chars), "
            "'tail' (last N chars), 'both' (head + middle marker + tail). The full "
            "transcript stays in result.txt until cleanup; use spawn_status MCP tool "
            "to read it.",
            enum=["head", "tail", "both"],
        ),
    )
    completion_keep_chars: int = field(
        default=3000,
        metadata=_meta(
            "Completion Keep Chars",
            "Maximum characters retained in the completion event after applying "
            "completion_keep. 0 disables truncation entirely. Default 3000.",
        ),
    )
    subagent_result_ttl_secs: int = field(
        default=3600,
        metadata=_meta(
            "SubAgent Result TTL (seconds)",
            "How long a delivered subagent's result.txt is retained before the "
            "reaper prunes it. The completion event returns a summary plus this "
            "file path; the parent reads the full transcript on demand (read / "
            "grep / spawn_status) within this window instead of re-running the "
            "subagent. 0 prunes on the next reaper sweep. Default 3600 (1h).",
        ),
    )
    subagent_cwd_allowed_roots: list[str] = field(
        default_factory=lambda: ["~/workspace", "~/workplace"],
        metadata=_meta(
            "SubAgent CWD Allowed Roots",
            "Directory roots under which spawn_run's cwd parameter is permitted. "
            "Values support ~ expansion. Empty list disables cwd overrides.",
        ),
    )
    max_channels: int = field(
        default=1,
        metadata=_meta("Max Channels", "Maximum concurrent agent channels (1-5)."),
    )
    max_channel_agents: int = field(
        default=3,
        metadata=_meta("Max Channel Agents", "Maximum agents per channel (1-10)."),
    )
    log_level: str = field(
        default="WARNING",
        metadata=_meta(
            "Log Level",
            "Persistent log level for the kiro_crew logger. "
            "Applied at startup; overridden by --verbose CLI flag.",
            enum=["DEBUG", "INFO", "WARNING", "ERROR"],
        ),
    )
    enforce_denied_commands: str = field(
        default="all",
        metadata=_meta(
            "Enforce Denied Commands",
            "Scope for deniedCommands enforcement on kiro agent configs. "
            "'all' enforces on every agent; 'kirocrew' only on the kirocrew agent.",
            enum=["all", "kirocrew"],
        ),
    )
    soft_stop_budget_secs: float = field(
        default=10.0,
        metadata=_meta(
            "Soft-Stop Budget",
            "Seconds to wait for cooperative cancel before hard-killing the session.",
        ),
    )

    def __post_init__(self) -> None:
        self.max_channels = max(1, min(5, self.max_channels))
        self.max_channel_agents = max(1, min(10, self.max_channel_agents))
        # Clamp to [0.5, 60.0] to match ``KiroCrewConfig.load()`` behavior
        # (dashboard PATCH and YAML loader both clamp rather than raise).
        clamped = max(0.5, min(60.0, float(self.soft_stop_budget_secs)))
        if clamped != self.soft_stop_budget_secs:
            logger.warning(
                "soft_stop_budget_secs=%s out of range [0.5, 60.0]; clamped to %s",
                self.soft_stop_budget_secs,
                clamped,
            )
            self.soft_stop_budget_secs = clamped


@dataclass
class SessionConfig:
    timeout_secs: int = field(
        default=DEFAULT_SESSION_TIMEOUT,
        metadata=_meta("Session Timeout", "Idle session timeout in seconds."),
    )
    autocompact_pct: float = field(
        default=90.0,
        metadata=_meta(
            "Auto-Compact Threshold",
            "Context usage percentage at which auto-compaction triggers (5-90).",
        ),
    )
    pool_size: int = field(
        default=0,
        metadata=_meta(
            "Warm Pool Size",
            "Number of pre-spawned kiro-cli processes kept ready for instant session start. 0 disables.",
        ),
    )
    pool_agent: str = field(
        default="",
        metadata=_meta(
            "Warm Pool Agent",
            "Agent name for warm pool processes. Empty string uses agent.default_agent.",
        ),
    )
    pool_ttl_secs: int = field(
        default=1800,
        metadata=_meta(
            "Warm Pool TTL",
            "Max age in seconds for pooled processes. Stale processes are discarded at claim time. 0 disables.",
        ),
    )
    archive_retention_days: int = field(
        default=30,
        metadata=_meta(
            "Archive Retention (days)",
            "Days to keep compacted/rotated session archives before auto-cleanup. "
            "-1 disables cleanup (manage deletion manually).",
            nullable=True,
        ),
    )
    watchdog_rss_max_mb: int = field(
        default=0,
        metadata=_meta(
            "Watchdog RSS Limit (MiB)",
            "Recycle a session when its process tree resident memory exceeds "
            "this many MiB. 0 disables (default). Busy sessions (turn in "
            "flight) are never recycled.",
        ),
    )


@dataclass
class TaskRunnerConfig:
    max_parallel_steps: int = field(
        default=DEFAULT_MAX_PARALLEL_STEPS,
        metadata=_meta("Max Parallel Steps", "Maximum task steps to run in parallel. 0 = auto (the host-safe cap from agent.subagent_auto_max, clamped to memory/CPU). A positive value only *lowers* concurrency — it is capped at the auto maximum and can never exceed the host-safe limit."),
    )
    workspace_dir: str = field(
        default="",
        metadata=_meta(
            "Workspace Folder",
            "Absolute path where task runner executions run. When set, "
            "every execution operates in this folder instead of a per-run scratch "
            "directory, so the task runner works on the intended target location. "
            "Empty = use the default per-run workspace directory.",
        ),
    )


@dataclass
class OrchestratorConfig:
    stage_timeout_seconds: int = field(
        default=1800,
        metadata=_meta(
            "Stage Timeout", "Max seconds per stage before auto-run stops. Default 30 min."
        ),
    )


@dataclass
class MessagingConfig:
    use_transport: bool = field(
        default=True,
        metadata=_meta(
            "Use Transport",
            "Route inbound Slack messages through the SlackTransport → TurnDriver → "
            "SlackRenderer channel-neutral path instead of the native handle_message "
            "monolith. Default ON in KiroCrew (the transport abstraction is the canonical "
            "path, shared with future channels). Set to false to fall back to the legacy "
            "native handler.",
        ),
    )
    dm_scope: str = field(
        default="per-channel-peer",
        metadata=_meta(
            "DM Session Scope",
            "How direct-message conversations map to sessions. 'per-channel-peer' "
            "(default) keeps one session per (channel, user), so the same person on "
            "Telegram vs WeCom stays isolated. 'unified' collapses all DMs into one "
            "shared session per agent for cross-surface continuity.",
        ),
    )
    idle_reset_minutes: int = field(
        default=0,
        metadata=_meta(
            "DM Idle Reset (minutes)",
            "Start a fresh session generation when a DM arrives after this many "
            "minutes of inactivity. 0 (default) disables idle reset.",
        ),
    )
    daily_reset_hour: int = field(
        default=-1,
        metadata=_meta(
            "DM Daily Reset Hour",
            "Local-time hour (0-23) at which the next DM starts a fresh session "
            "generation once per day. -1 (default) disables daily reset.",
        ),
    )
    queue_mode: str = field(
        default="steer",
        metadata=_meta(
            "DM Queue Mode",
            "How a DM that arrives while a turn is running is handled. 'steer' "
            "(default) folds it into the running reply; 'queue' holds it and runs "
            "it after the current turn finishes.",
        ),
    )

    def __post_init__(self) -> None:
        # Fail safe on hand-edited values (mirrors WeComConfig): an unknown scope
        # or mode falls back to the safe default, and the reset windows clamp to
        # valid ranges so a bad config can't wedge dispatch.
        if self.dm_scope not in ("per-channel-peer", "unified"):
            self.dm_scope = "per-channel-peer"
        if self.queue_mode not in ("steer", "queue"):
            self.queue_mode = "steer"
        self.idle_reset_minutes = max(0, self.idle_reset_minutes)
        if not 0 <= self.daily_reset_hour <= 23:
            self.daily_reset_hour = -1


@dataclass
class CronHistoryConfig:
    cron_summary_cap: int = field(
        default=200,
        metadata=_meta("Summary Cap", "Max characters for run summary field."),
    )
    cron_trace_cap_kb: int = field(
        default=50,
        metadata=_meta("Trace Cap KB", "Max kilobytes for run trace field."),
    )
    cron_max_records_per_job: int = field(
        default=100,
        metadata=_meta("Max Records Per Job", "Max history records kept per job file."),
    )
    cron_max_index_records: int = field(
        default=2000,
        metadata=_meta("Max Index Records", "Max records in the global index."),
    )


@dataclass
class MemoryConfig:
    embedding_provider: str = field(
        default="llama_cpp",
        metadata=_meta(
            "Embedding Provider",
            "Vector embedding backend (always-on). In-process via vendored llama-cpp-python. "
            "Legacy configs with 'ollama' or 'none' are auto-migrated to 'llama_cpp'.",
            enum=["llama_cpp"],
        ),
    )
    embedding_dim: int = field(
        default=1024,
        metadata=_meta("Embedding Dimension", "Dimensionality of embedding vectors."),
    )
    embed_model_url: str = field(
        default="",
        metadata=_meta(
            "Embedding Model URL",
            "Override HTTPS URL for the embedding model GGUF download (mirrored/airgapped "
            "deployments). Empty uses the public KiroCrew CDN default; the "
            "KIROCREW_EMBED_MODEL_URL env var wins over both. The download is "
            "sha256-verified regardless of source.",
        ),
    )
    semantic_confidence_threshold: float = field(
        default=0.8,
        metadata=_meta(
            "Semantic Confidence Threshold",
            "Minimum similarity score for semantic search results.",
        ),
    )
    episodic_dedup_threshold: float = field(
        default=0.88,
        metadata=_meta(
            "Episodic Dedup Threshold",
            "Similarity threshold for deduplicating episodic memories.",
        ),
    )
    episodic_max_results: int = field(
        default=8,
        metadata=_meta("Episodic Max Results", "Maximum episodic memory results per query."),
    )
    episodic_max_count: int = field(
        default=10_000,
        metadata=_meta("Episodic Max Count", "Maximum total episodic memories stored."),
    )
    semantic_keys: list[str] = field(
        default_factory=list,
        metadata=_meta("Semantic Keys", "Keys to index for semantic search."),
    )
    history_idle_hours: float = field(
        default=3.0,
        metadata=_meta(
            "History Idle Hours",
            "Hours of inactivity before history consolidation.",
        ),
    )
    history_max_days: int = field(
        default=365,
        metadata=_meta("History Max Days", "Maximum days of history to retain."),
    )
    migrated: bool = field(
        default=False,
        metadata=_meta("Migrated", "Whether memory has been migrated to vector store."),
    )


#: Default artifact kinds eligible for Knowledge Library auto-ingest. These are
#: the substantial-document kinds whose content the KB file reader can extract
#: (routed through the same reader as folders/uploads): markdown/text/json read
#: as text, and html goes through HTML prose extraction. ``widget`` is excluded
#: -- widgets/dashboards are UI, not documents (and a remote widget round-trips
#: back to kind="widget" via the publish/clone unwrap, so this also skips cloned
#: widgets). ``svg`` is excluded because ``.svg`` is not in
#: ``FileReader.SUPPORTED``.
DEFAULT_AUTO_INGEST_ARTIFACT_KINDS = ["markdown", "text", "html", "json"]


def _coerce_embedding_provider(raw: str) -> str:
    """Normalize legacy or unknown embedding_provider values.

    Embeddings are always-on: every value coerces to ``"llama_cpp"``. Old configs
    may carry ``"ollama"`` (previous runtime) or ``"none"`` (previously-disabled);
    both are transparently upgraded. Unknown values also coerce so a config file
    from a newer/older version never crashes.
    """
    return "llama_cpp"


@dataclass
class KnowledgeConfig:
    """Knowledge Library ingestion settings.

    Embedding/retrieval settings live under :class:`MemoryConfig` (shared with
    the memory subsystem via ``create_embedder_from_config``); this section
    holds Knowledge-Library-specific ingestion toggles.
    """

    auto_ingest_artifacts: bool = field(
        default=True,
        metadata=_meta(
            "Auto-Ingest Artifacts",
            "Automatically ingest content-bearing local artifacts (markdown/text "
            "documents you save and iterate) into the Knowledge Library so they "
            "become searchable, keep them in sync as the artifact changes, and "
            "remove them from the Library when the artifact is deleted. They "
            "appear as a single aggregate 'Artifacts' source. On by default.",
        ),
    )
    auto_ingest_artifact_kinds: list[str] = field(
        default_factory=lambda: list(DEFAULT_AUTO_INGEST_ARTIFACT_KINDS),
        metadata=_meta(
            "Auto-Ingest Artifact Kinds",
            "Artifact kinds eligible for auto-ingest. Defaults to substantial "
            "document kinds (markdown, text, html, json); widget is excluded "
            "(UI/dashboards, not documents) and svg has no reader support.",
        ),
    )
    max_ingest_file_mb: float = field(
        default=100.0,
        metadata=_meta(
            "Max Ingest File Size (MB)",
            "Per-file size cap for Knowledge Library ingestion. Oversized files "
            "are skipped with a WARNING naming the file instead of being chunked "
            "-- chunking a very large file (e.g. a tens-of-MB CSV->MD conversion) "
            "is CPU-bound and previously hung gateway startup. Set 0 to disable "
            "the cap.",
        ),
    )
    embed_timeout_secs: float = field(
        default=10.0,
        metadata=_meta(
            "Embed Timeout (seconds)",
            "Per-request timeout for the Knowledge-Library embedder. Raise it "
            "when a large chunk times out on a cold Ollama model load (the embed "
            "then never completes and the item is retried every maintenance "
            "pass). 0 or unset keeps the built-in 10s default.",
        ),
    )
    embed_content_budget: int = field(
        default=0,
        metadata=_meta(
            "Embed Content Budget (chars)",
            "Safety bound (chars) on chunk content folded into an item embedding. "
            "0 or unset keeps the built-in default (a generous backstop for "
            "pathological un-chunked input); raise/lower only to tune truncation.",
        ),
    )
    pool_idle_ttl_secs: int = field(
        default=300,
        metadata=_meta(
            "Pool Idle TTL (secs)",
            "Seconds the document-extraction worker pool may sit fully idle "
            "before it is scaled to zero (all workers shut down, freeing ~1GB "
            "of held process trees); the next ingest respawns them lazily. "
            "0 keeps the workers warm indefinitely.",
        ),
    )


@dataclass
class SlackConfig:
    allowed_users: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Users",
            "List of Slack users allowed to interact. Each entry: {slack_id, name}.",
        ),
    )
    tracking_channels: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Tracking Channels",
            "Slack channels to monitor. Each entry: {channel_id, name}.",
        ),
    )
    open_channels: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Open Channels",
            "Channel IDs where all users are authorized without allowlist.",
        ),
    )
    command: str = field(
        default="kirocrew",
        metadata=_meta("Command", "Slack slash command trigger word."),
    )
    forward_to_agent_callback: str = field(
        default="",
        metadata=_meta(
            "Forward to Agent Callback",
            "Callback ID for the 'Forward to Agent' message shortcut. "
            "Must match the callback_id configured in your Slack app manifest. "
            "Leave empty to disable the feature.",
            tags=["slack"],
        ),
    )
    trusted_bot_ids: set[str] = field(
        default_factory=set,
        metadata=_meta(
            "Trusted Bot IDs",
            "Bot IDs allowed to bypass the bot filter for multi-node mesh communication.",
            tags=["slack"],
        ),
    )
    allowed_enterprise_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Enterprise IDs",
            "Slack Enterprise Grid org IDs to allow. Empty list allows all orgs (default-open).",
            tags=["slack"],
        ),
    )
    reactions: dict[str, str | None] = field(
        default_factory=dict,
        metadata=_meta(
            "Reactions",
            "Override phase reaction emojis. Valid keys: queued, thinking, coding, browsing, tool, done, error. "
            "Set a value to null to suppress that phase entirely.",
            tags=["slack"],
        ),
    )
    reactions_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Reactions Enabled",
            "Show phase-aware emoji reactions on Slack messages during processing.",
            tags=["slack"],
        ),
    )
    show_thinking: bool = field(
        default=True,
        metadata=_meta(
            "Show Thinking",
            "Post the model's thinking/reasoning as a thread reply in Slack. "
            "Disable to keep responses concise.",
            tags=["slack"],
        ),
    )
    home_tab_sessions_per_kind: int = field(
        default=5,
        metadata=_meta(
            "Home Tab Sessions Per Kind",
            "Max sessions shown per category (main chat / autopilot) in the Slack Home Tab.",
            tags=["slack"],
        ),
    )
    use_tunnel_url: bool = field(
        default=False,
        metadata=_meta(
            "Use Tunnel URL in Slack",
            "When true, dashboard links posted to Slack (e.g. via /kirocrew dashboard) "
            "use the tunnel URL if one is active. When false (default), "
            "Slack links always use the configured dashboard origin or host:port. "
            "Disabled by default until the tunnel mechanism is scaled for general use.",
            tags=["slack"],
        ),
    )


@dataclass
class PublishConfig:
    """Operator-facing controls for artifact publishing.

    Publishing an artifact to an external destination is provided by a
    ``publish_provider`` registered through the ``platform`` CPP seam
    (``PublishRegistry``). The public edition registers NO provider, so
    publishing is unavailable regardless of these settings; a companion edition
    registers a concrete destination.

    This ``allowed_destinations`` list is the STANDALONE operator's narrowing
    knob (default-open, mirroring ``SlackConfig.allowed_enterprise_ids``): empty
    means "allow every registered destination". It is enforced at the publish
    handler chokepoint IN ADDITION TO the governance ceiling
    (``capabilities.publish``) — like the Slack allowlist, config can only
    NARROW, never widen: a destination denied by the enterprise policy cannot be
    re-permitted here (the security policy is never merged from ``config.json``).
    """

    allowed_destinations: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Publish Destinations",
            "Publish-provider ids the operator permits (e.g. 'artifactory'). "
            "Empty list allows all registered destinations (default-open). "
            "Cannot widen past the enterprise governance ceiling.",
            tags=["publish"],
        ),
    )
    #: Extra filesystem roots (beyond the user's home dir) that an artifact may
    #: be relocated to point at (``artifact_relocate`` / the ``artifact_move`` MCP
    #: tool). Relocate is confined to the user home by default so an agent cannot
    #: aim an artifact at ``/etc/passwd`` or another user's files and exfiltrate
    #: them via a later artifact GET; each entry here widens the allowed set to an
    #: additional absolute root (e.g. a shared project dir). Paths are expanded +
    #: realpath-resolved; a relocate target must resolve under the home dir OR one
    #: of these roots (AND still pass the sensitive-path denylist).
    relocate_roots: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Artifact Relocate Roots",
            "Extra absolute filesystem roots an artifact may be relocated into, "
            "beyond your home directory. Empty = home-only (the secure default). "
            "The sensitive-path denylist (~/.aws, ~/.ssh, ~/.kirocrew, …) still "
            "applies inside every allowed root.",
            tags=["artifacts"],
        ),
    )


@dataclass
class DashboardConfig:
    url: str = field(
        default="",
        metadata=_meta(
            "Dashboard URL",
            "Public URL for the dashboard (used in Slack links).",
        ),
    )
    restore_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Restore Sessions",
            "Re-open recently active sessions on startup.",
        ),
    )
    restore_window_minutes: int = field(
        default=30,
        metadata=_meta(
            "Restore Window Minutes",
            "Time window (minutes) for session restoration (0-1440). 0 = restore all.",
        ),
    )
    bot_name: str = field(
        default="",
        metadata=_meta(
            "Bot Name",
            "Custom bot display name for the dashboard UI.",
        ),
    )
    avatar: str = field(
        default="",
        metadata=_meta(
            "Avatar",
            "Path to custom avatar image for the dashboard UI.",
        ),
    )
    merge_queued_messages: bool = field(
        default=False,
        metadata=_meta(
            "Merge Queued Messages",
            "Concatenate follow-up messages while the agent is busy instead of queueing them separately.",
        ),
    )
    mcp_probe_timeout_secs: int = field(
        default=15,
        metadata=_meta(
            "MCP Probe Timeout",
            "Seconds to wait for MCP server handshake during probe (5-120).",
        ),
    )
    widget_density: str = field(
        default="more",
        metadata=_meta(
            "Widget Density",
            "How aggressively the agent uses inline widgets. "
            "'more' encourages widgets for any visual content; "
            "'less' limits to only when markdown is clearly insufficient.",
            enum=["more", "less"],
        ),
    )
    tail_fork_enabled: bool = field(
        default=False,
        metadata=_meta(
            "Tail-only Fork",
            "When forking, keep only the messages after the chosen point. The "
            "earlier messages are dropped.",
        ),
    )
    auto_open_browser: bool = field(
        default=True,
        metadata=_meta(
            "Auto Open Browser",
            "Open the dashboard URL in the default browser on gateway startup.",
        ),
    )
    quick_send: bool = field(
        default=False,
        metadata=_meta(
            "Quick Send",
            "Click a suggested reply to send it instantly. Shift+Click to select multiple.",
        ),
    )
    session_grid: bool = field(
        default=False,
        metadata=_meta(
            "Session Grid (Split View)",
            "Opt-in: enable terminal-style split view to run multiple chat sessions side by side.",
        ),
    )
    terminal: dict = field(
        default_factory=lambda: {"enabled": True},
        metadata=_meta(
            "Terminal",
            "Terminal panel configuration. Set enabled=false to hide the CLI panel in the dashboard.",
        ),
    )
    default_project: str = field(
        default="",
        metadata=_meta(
            "Default Project",
            "Directory path used as the project for new chat tabs. Empty = workspace dir.",
        ),
    )
    theme_mode: str = field(
        default="",
        metadata=_meta(
            "Theme Mode",
            "Dashboard color mode preference: 'dark', 'light', or 'system'. "
            "Empty = unset (frontend falls back to localStorage or 'system').",
            enum=["", "dark", "light", "system"],
        ),
    )
    theme_color: str = field(
        default="",
        metadata=_meta(
            "Theme Color",
            "Dashboard color theme slug (e.g. 'kiro', 'emerald', 'monokai'). "
            "Empty = unset (frontend falls back to localStorage or 'kiro').",
        ),
    )
    recent_tint_count: int = field(
        default=0,
        metadata=_meta(
            "Recent Session Tint Count",
            "Number of most-recently-active sessions to highlight in the sidebar with a "
            "graded accent stripe (0-10; 0 = off).",
        ),
    )
    onboarded: bool = field(
        default=False,
        metadata=_meta(
            "Onboarded",
            "Whether the user has completed the dashboard onboarding flow. "
            "When true, the 'Choose your look' modal is skipped on first load.",
        ),
    )
    tips_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Tips Enabled",
            "Show feature tip cards while the agent is thinking.",
        ),
    )
    tips_cadence_hours: float = field(
        default=6.0,
        metadata=_meta(
            "Tips Cadence Hours",
            "Minimum hours between showing a new tip.",
        ),
    )
    tips_snooze_hours: float = field(
        default=48.0,
        metadata=_meta(
            "Tips Snooze Hours",
            "Hours before a snoozed tip becomes eligible again.",
        ),
    )
    tips_recency_decay: float = field(
        default=0.6,
        metadata=_meta(
            "Tips Recency Decay",
            "Decay factor for weighted-random selection (0-1). Lower = stronger bias to newer tips.",
        ),
    )
    tips_model: str = field(
        default="claude-haiku-4.5",
        metadata=_meta(
            "Tips Model",
            "Model ID for tips generation (pinned to Haiku-class for cost efficiency).",
        ),
    )
    tips_explore_ratio: float = field(
        default=0.2,
        metadata=_meta(
            "Tips Explore Ratio",
            "Probability of picking a random catalog tip instead of personalized (0-1). Higher = more general discovery.",
        ),
    )


@dataclass
class KiroCrewAgentConfig:
    kiro_agent: str = field(
        default="",
        metadata=_meta("Kiro Agent", "Kiro agent name (modeId for session/set_mode)."),
    )
    workspace: str = field(
        default="default",
        metadata=_meta("Workspace", "Named workspace from the workspaces section."),
    )
    memory_store: str = field(
        default="default",
        metadata=_meta("Memory Store", "Named memory store from the memory_stores section."),
    )
    description: str = field(
        default="",
        metadata=_meta("Description", "Human-readable agent description."),
    )
    source: str = field(
        default="kirocrew",
        metadata=_meta("Source", "Agent origin: kirocrew or builtin."),
    )


@dataclass
class WorkspaceConfig:
    dir: str = field(
        default="workspace",
        metadata=_meta("Directory", "Workspace directory path."),
    )


@dataclass
class MemoryStoreConfig:
    description: str = field(
        default="",
        metadata=_meta("Description", "Human-readable purpose of this memory store."),
    )
    embedding_provider: str = field(
        default="",
        metadata=_meta(
            "Embedding Provider",
            "Override embedding backend for this store. Empty inherits from top-level memory "
            "(embeddings are always-on; per-store disable is not supported).",
            enum=["", "llama_cpp"],
        ),
    )


@dataclass
class ExternalRegistryConfig:
    """An external app registry source (org-owned repo with app.json files)."""

    name: str = field(
        default="",
        metadata=_meta("Name", "Human-readable registry name (e.g. 'identityservices')."),
    )
    repo: str = field(
        default="",
        metadata=_meta("Repo", "Git URL of the repo containing apps (https or ssh)."),
    )
    branch: str = field(
        default="mainline",
        metadata=_meta("Branch", "Git branch to read from."),
    )


@dataclass
class SkillsConfig:
    max_triggered: int = field(
        default=3,
        metadata=_meta("Max Triggered", "Maximum number of skills to load per message (≥1)."),
    )
    # ── Lazy skill injection (opt-in, like MCP prewarm) ──
    lazy_load: bool = field(
        default=False,
        metadata=_meta(
            "Lazy Skill Injection",
            "When true, the session-start skills block injects only a usage-ranked "
            "top-K of on-demand skills (bounded by its own section budget) and leaves "
            "the long tail discoverable via the skill_search tool / $skillname / "
            "triggers; each context section also gets its own independent char cap so "
            "the global ceiling becomes their sum (~190k) and a large skills set can "
            "never crowd out memory/lessons. Disabled by default (0-impact upgrade, "
            "like prewarm_count=0): off means the legacy full skills dump under a "
            "single shared 165k budget — unchanged behavior.",
        ),
    )
    # ── Hermes-style auto skill creation (Mesh-677) ──
    # All fields default to OFF so upgrades are zero-impact. Enable via
    # ``kirocrew config set skills.auto_create_from_sessions true`` or the
    # dashboard Settings → Skills panel (future).
    auto_create_from_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Create Skills",
            "When true, analyze each session after completion and synthesize a reusable "
            "SKILL.md when a non-trivial multi-step procedure is detected. Generated "
            "skills live under skills/auto/ so they never collide with hand-authored "
            "skills. Disabled by default.",
        ),
    )
    auto_refine_on_deviation: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Refine Skills",
            "When true, update an existing auto-created skill if the agent succeeds "
            "via a different tool sequence than documented. Requires "
            "auto_create_from_sessions. Disabled by default.",
        ),
    )
    auto_min_tool_calls: int = field(
        default=5,
        metadata=_meta(
            "Auto Min Tool Calls",
            "Minimum tool calls in a session for it to qualify for skill extraction "
            "(≥2). Lower values produce more skills but reduce quality.",
        ),
    )
    auto_similarity_threshold: float = field(
        default=0.85,
        metadata=_meta(
            "Auto Similarity Threshold",
            "Skip creation when an existing skill's description has keyword overlap "
            "≥ this fraction with the synthesized description (0.0-1.0). Prevents "
            "near-duplicate skills.",
        ),
    )
    extra_paths: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Extra Skill Paths",
            "Additional directories to scan for skills. Supports ~ expansion. "
            "Skills from extra_paths are read-only (trigger matching + loading). "
            "Local ~/.kirocrew/skills/ takes precedence for duplicate names.",
        ),
    )

    def __post_init__(self) -> None:
        if self.max_triggered < 1:
            logger.warning("max_triggered %d < 1, using 1", self.max_triggered)
            object.__setattr__(self, "max_triggered", 1)
        if self.auto_min_tool_calls < 2:
            logger.warning("auto_min_tool_calls %d < 2, using 2", self.auto_min_tool_calls)
            object.__setattr__(self, "auto_min_tool_calls", 2)
        if not 0.0 <= self.auto_similarity_threshold <= 1.0:
            logger.warning(
                "auto_similarity_threshold %.2f out of range [0.0, 1.0], using 0.85",
                self.auto_similarity_threshold,
            )
            object.__setattr__(self, "auto_similarity_threshold", 0.85)
        if self.auto_refine_on_deviation and not self.auto_create_from_sessions:
            logger.warning(
                "auto_refine_on_deviation requires auto_create_from_sessions; "
                "disabling auto_refine_on_deviation"
            )
            object.__setattr__(self, "auto_refine_on_deviation", False)


@dataclass
class TelemetryConfig:
    """Metrics telemetry settings (Wave 0 trunk).

    Default OFF: when disabled, metric call sites are cheap no-ops and nothing is
    written or exported (byte-identical to no telemetry), mirroring the
    ``mcp_gateway.enabled`` / ``skills.lazy_load`` opt-in convention. When
    enabled, a local-first JSONL sink under ``~/.kirocrew/metrics`` is activated;
    remote / OTLP egress is a separate opt-in (not wired yet).
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Main switch for KiroCrew metrics telemetry. Off by default: metric "
            "call sites are no-ops and nothing is written. When on, a local-first "
            "JSONL sink under ~/.kirocrew/metrics is enabled (no network egress).",
        ),
    )
    local_dir: str = field(
        default="",
        metadata=_meta(
            "Local Metrics Dir",
            "Directory for local JSONL metric shards. Empty = ~/.kirocrew/metrics. "
            "Supports ~ expansion.",
        ),
    )
    export_interval_seconds: int = field(
        default=60,
        metadata=_meta(
            "Export Interval (s)",
            "How often the local exporter flushes aggregated metrics to disk (>=1).",
        ),
    )

    def __post_init__(self) -> None:
        if self.export_interval_seconds < 1:
            logger.warning("export_interval_seconds %d < 1, using 1", self.export_interval_seconds)
            object.__setattr__(self, "export_interval_seconds", 1)


# Channel activation modes
ACTIVATION_ALWAYS = "always"  # Process every message
ACTIVATION_MENTION = "mention"  # Only respond when @mentioned
ACTIVATION_OBSERVE = "observe"  # Record messages, respond only when @mentioned (deep context)
ACTIVATION_REVIEW = "review"  # Generate response, show ephemeral draft for owner approval
ACTIVATION_OFF = "off"  # Ignore all messages completely — no history recorded
_VALID_ACTIVATIONS = frozenset(
    {ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OBSERVE, ACTIVATION_REVIEW, ACTIVATION_OFF}
)


@dataclass
class ChannelConfig:
    """Per-channel Slack configuration."""

    activation: str = field(
        default=ACTIVATION_MENTION,
        metadata=_meta(
            "Activation",
            "Channel activation mode.",
            enum=["always", "mention", "observe", "review", "off"],
        ),
    )
    agent: str = field(
        default="",
        metadata=_meta("Agent", "Agent override for this channel (empty = default)."),
    )
    project_path: str = field(
        default="",
        metadata=_meta(
            "Project Path",
            "Project directory for project-scoped agent (empty = global agent).",
        ),
    )
    thread_follow: bool = field(
        default=True,
        metadata=_meta(
            "Thread Follow",
            "Respond to all messages in threads where bot was previously @mentioned.",
        ),
    )

    @classmethod
    def from_dict(cls, data: dict) -> ChannelConfig:
        activation = data.get("activation", ACTIVATION_MENTION)
        if activation not in _VALID_ACTIVATIONS:
            activation = ACTIVATION_MENTION
        return cls(
            activation=activation,
            agent=data.get("agent", ""),
            project_path=data.get("project_path", ""),
            thread_follow=data.get("thread_follow", True),
        )


_VALID_STT_PROVIDERS = ("whisper", "mlx", "transcribe")
_VALID_CHANNEL_PREFIXES = ("C", "D", "G")


def _validated_stt_provider(value: str) -> str:
    """Return *value* if recognised, else warn and default to whisper."""
    if value in _VALID_STT_PROVIDERS:
        return value
    logger.warning("Unknown STT provider '%s', falling back to whisper", value)
    return "whisper"


_VALID_COMPLETION_KEEP = ("head", "tail", "both")


def _validated_completion_keep(value: object) -> str:
    """Return *value* if it is one of head/tail/both, else raise ValueError."""
    if isinstance(value, str) and value in _VALID_COMPLETION_KEEP:
        return value
    raise ValueError(
        f"agent.completion_keep must be one of {list(_VALID_COMPLETION_KEEP)}, " f"got {value!r}"
    )


def _normalize_jail(value: object) -> str:
    """Coerce a persisted ``agent.jail`` value to a valid mode, deny-by-default.

    Valid persisted modes are ``auto`` / ``on`` / ``off``.  An unknown or
    non-string value normalizes to ``auto`` (the safe default — let the active
    edition decide; the public edition's jail provider is a no-op regardless).
    ``off`` per-invocation is expressed via ``--no-jail`` / ``KIROCREW_NO_JAIL``,
    not persisted config.
    """
    if isinstance(value, str) and value in _VALID_JAIL_MODES:
        return value
    return JAIL_MODE_AUTO


def _validate_activation(value: str) -> str:
    """Return *value* if it is a valid activation mode, else ``mention`` (deny-by-default)."""
    return value if value in _VALID_ACTIVATIONS else ACTIVATION_MENTION


def _validate_tracking_channels(raw: list) -> list[dict]:
    """Validate and coerce tracking_channels entries.

    Accepted formats:
    - ``{"channel_id": "C...", "name": "..."}`` — passed through
    - ``"C..."`` (bare string) — auto-coerced to ``{"channel_id": "C..."}`` with a warning

    Rejects entries that are neither strings starting with C/D/G nor dicts with channel_id.
    """
    if not raw:
        return []
    result: list[dict] = []
    coerced = 0
    rejected = 0
    for entry in raw:
        if isinstance(entry, dict) and entry.get("channel_id"):
            result.append(entry)
        elif isinstance(entry, str) and len(entry) > 1 and entry[0] in _VALID_CHANNEL_PREFIXES:
            result.append({"channel_id": entry})
            coerced += 1
        else:
            rejected += 1
    if coerced:
        logger.warning(
            "Config: slack.tracking_channels has %d bare string(s) — auto-coerced to "
            '{"channel_id": "..."} format. Prefer: [{"channel_id": "C...", "name": "..."}]',
            coerced,
        )
    if rejected:
        logger.warning(
            "Config: slack.tracking_channels has %d invalid entries (expected objects with "
            '"channel_id" field or bare channel ID strings starting with C/D/G). '
            "These entries were ignored.",
            rejected,
        )
    return result


def _migrate_workspaces(raw_workspaces: dict) -> dict[str, WorkspaceConfig]:
    """Auto-migrate workspaces from flat or structured format.

    - String values → WorkspaceConfig(dir=value)
    - Dict values with ``dir`` key → WorkspaceConfig(dir=value["dir"])
    - Non-string/non-dict values → default WorkspaceConfig()
    - Empty input → {"default": WorkspaceConfig(dir="workspace")}
    """
    result: dict[str, WorkspaceConfig] = {}
    for name, value in raw_workspaces.items():
        if isinstance(value, str):
            result[name] = WorkspaceConfig(dir=value)
        elif isinstance(value, dict):
            result[name] = WorkspaceConfig(dir=value.get("dir", "workspace"))
        else:
            result[name] = WorkspaceConfig()
    if not result:
        result["default"] = WorkspaceConfig(dir="workspace")
    return result


def resolve_memory_store_config(
    top_level_memory: dict,
    store_overrides: dict,
) -> dict:
    """Deep-merge store overrides onto top-level memory defaults.

    Merge happens at the raw dict level BEFORE dataclass construction.
    A store that only sets embedding_provider inherits all other memory
    settings from the top-level config, not from MemoryConfig defaults.
    """
    merged = dict(top_level_memory)
    for key, value in store_overrides.items():
        if key == "description":
            continue  # description is store-only metadata, not a memory setting
        if value != "" and value is not None:
            merged[key] = value
    return merged


@dataclass
class ResolvedBindings:
    """Resolved workspace, memory store, and kiro agent for a session."""

    workspace_dir: Path
    memory_store_name: str
    effective_memory_config: dict
    kiro_agent: str


@dataclass
class SttConfig:
    """Speech-to-text configuration (opt-in, disabled by default)."""

    enabled: bool = field(
        default=True,
        metadata=_meta("Enabled", "Enable voice memo transcription."),
    )
    provider: str = field(
        default="whisper",
        metadata=_meta("Provider", "STT provider.", enum=list(_VALID_STT_PROVIDERS)),
    )
    whisper_path: str = field(
        default="",
        metadata=_meta("Whisper Path", "Path to whisper binary (auto-detected if empty)."),
    )
    model: str = field(
        default="turbo",
        metadata=_meta("Model", "Whisper model size.", enum=["turbo"]),
    )
    mlx_model: str = field(
        default="mlx-community/whisper-large-v3-turbo",
        metadata=_meta(
            "MLX Model",
            "Hugging Face repo for the mlx_whisper model (mlx provider only).",
        ),
    )
    device: str = field(
        default="cpu",
        metadata=_meta("Device", "Computation device.", enum=["cpu", "cuda"]),
    )
    timeout_secs: int = field(
        default=300,
        metadata=_meta("Timeout", "Transcription timeout in seconds."),
    )
    transcribe_region: str = field(
        default="us-east-1",
        metadata=_meta("Transcribe Region", "AWS region for Transcribe API."),
    )
    transcribe_profile: str = field(
        default="",
        metadata=_meta("Transcribe Profile", "AWS profile for Transcribe API."),
    )
    language_code: str = field(
        default="en-US",
        metadata=_meta(
            "Language Code", "Language for speech recognition (e.g. en-US, fr-FR, es-ES)."
        ),
    )
    streaming: bool = field(
        default=False,
        metadata=_meta(
            "Streaming",
            "Stream partial transcripts live to the dashboard input (transcribe provider only).",
        ),
    )


@dataclass
class McpGatewayConfig:
    """Sidecar MCP broker daemon — shares MCP backends across sessions."""

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Route MCP traffic through the shared sidecar broker. Default False — opt-in.",
        ),
    )
    socket_path: str = field(
        default="",
        metadata=_meta(
            "Socket Path",
            "Unix socket for the broker. Empty -> $KIROCREW_HOME/mcp-gateway/gateway.sock.",
        ),
    )
    overlay_dir: str = field(
        default="",
        metadata=_meta(
            "Overlay Dir",
            "Directory of rewritten agent JSON, bind-mounted over ~/.kiro/agents per session. "
            "Empty -> $KIROCREW_HOME/mcp-gateway/agents.",
        ),
    )
    idle_timeout_secs: int = field(
        default=300,
        metadata=_meta("Idle Timeout", "Seconds a refcount=0 MCP backend is kept before drain."),
    )
    max_backends: int = field(
        default=64,
        metadata=_meta(
            "Max Backends",
            "Max concurrent pooled MCP backends before the pool refuses a new one. "
            "Must be >= the number of distinct (agent x server) backends that can be "
            "live at once: each agent keeps its own backend per server, so N concurrent "
            "agents with ~S servers each need N*S slots. Bounded by design: idle "
            "backends drain after idle_timeout_secs, so steady-state RAM tracks real "
            "concurrency, not this ceiling.",
        ),
    )
    poolable_servers: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Poolable Servers",
            "MCP server names allowed to share a pooled backend across sessions. "
            "A stdio server is pooled when its name appears here OR its agent-JSON "
            "entry sets poolable:true. Safe by default — non-listed servers run "
            "per-session. Managed from Settings -> Shared MCP gateway.",
        ),
    )
    prewarm_count: int = field(
        default=0,
        metadata=_meta(
            "Prewarm Count",
            "Number of hottest observed (agent x server x channel) MCP backends "
            "to spawn at gateway startup, before the first session connects. "
            "Removes the cold-start latency on the first new-chat after a "
            "gateway restart or after all backends have idled out — the steady "
            "state already reuses warm backends within the idle timeout. The "
            "hot set is learned from prior registers and persisted beside the "
            "socket; channel_id is a stable id, so a prewarmed backend is "
            "reused by every later new-chat in that channel. 0 (default) "
            "disables prewarming — no hot-key file is read or written.",
        ),
    )
    read_buffer_limit_bytes: int = field(
        default=64 * 1024 * 1024,
        metadata=_meta(
            "Read Buffer Limit",
            "Maximum bytes for a single MCP response line before asyncio drops it. "
            "Default 64 MiB. Responses exceeding this are fast-failed with -32000. "
            "Env override: KIROCREW_MCP_READ_LIMIT.",
        ),
    )
    response_spill_threshold_bytes: int = field(
        default=256 * 1024,
        metadata=_meta(
            "Response Spill Threshold",
            "Tool-call responses larger than this (bytes) have their text content "
            "written to ~/.kirocrew/mcp_spill/ and truncated inline to 16 KiB + "
            "a file path marker. Default 256 KiB. Set 0 to disable spilling. "
            "Env override: KIROCREW_MCP_SPILL_THRESHOLD.",
        ),
    )


@dataclass
class InstancesConfig:
    """Multi-instance management (the *Instances* feature).

    Gates and tunes the gateway's ability to manage/switch between several
    remote KiroCrew instances over SSH tunnels. Off by default — opt-in only,
    since enabling it allows the gateway to open SSH ``-L`` forwards and relaxes
    the dashboard CSP ``frame-src`` for the active loopback tunnel ports.

    The numeric tunables default to constants defined in
    ``kiro_crew.instances.constants`` so the canonical default lives in one
    place and cannot drift from this dataclass.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable multi-instance management — lets this gateway open SSH tunnels "
            "to remote KiroCrews and embed their dashboards. Default off (opt-in). "
            "Enabling also scopes a CSP frame-src relaxation to active tunnel ports.",
        ),
    )
    warm_set_cap: int = field(
        default=_DEFAULT_WARM_SET_CAP,
        metadata=_meta(
            "Warm Set Cap",
            "Max number of remote instances kept warm (iframe mounted + tunnel live) "
            "at once. Least-recently-used instances beyond this are evicted and "
            "reconnected on demand. Bounds memory/socket use (each warm instance is a "
            "full dashboard SPA).",
        ),
    )
    tunnel_base_port: int = field(
        default=_DEFAULT_TUNNEL_BASE_PORT,
        metadata=_meta(
            "Tunnel Base Port",
            "First local loopback port used for an SSH -L forward. The allocator "
            "increments from here, skipping ports already in use.",
        ),
    )
    ssh_compression: bool = field(
        default=_DEFAULT_SSH_COMPRESSION,
        metadata=_meta(
            "SSH Compression",
            "Enable SSH transport compression (ssh -C) on instance tunnels. The "
            "remote dashboard SPA bundle plus all API/WebSocket traffic travel over "
            "this forwarded stream and are highly compressible; the gateway does not "
            "gzip HTTP responses, so this is the only compression in the path. "
            "Default on (best for a dedicated remote host over a slow link); turn off "
            "on a fast/local link where compression CPU outweighs the bandwidth win.",
        ),
    )
    max_recovery_attempts: int = field(
        default=_DEFAULT_MAX_RECOVERY,
        metadata=_meta(
            "Max Recovery Attempts",
            "Consecutive self-heal attempts before a dropped tunnel is left "
            "disconnected. With the capped-exponential backoff, the default 8 spans a "
            "~2 min recovery window, enough to outlast a transient drop (screen lock, "
            "proxy warmup) before giving up.",
        ),
    )
    recover_backoff_max_secs: float = field(
        default=_DEFAULT_BACKOFF_MAX,
        metadata=_meta(
            "Recover Backoff Cap (secs)",
            "Cap on the per-attempt backoff between self-heal attempts. The wait grows "
            "1, 2, 4, 8, 16 then holds at this cap; raising it spaces retries further "
            "across a slow reconnect.",
        ),
    )
    probe_failure_threshold: int = field(
        default=_DEFAULT_PROBE_FAILS,
        metadata=_meta(
            "Probe Failure Threshold",
            "Consecutive health-probe failures before a connected-but-not-forwarding "
            "(zombie) tunnel is torn down to trigger self-heal.",
        ),
    )

    def __post_init__(self) -> None:
        if self.warm_set_cap < 1:
            logger.warning("instances.warm_set_cap %d < 1, using 1", self.warm_set_cap)
            object.__setattr__(self, "warm_set_cap", 1)
        if not (1 <= self.tunnel_base_port <= 65535):
            logger.warning(
                "instances.tunnel_base_port %d out of range [1, 65535], using %d",
                self.tunnel_base_port,
                _DEFAULT_TUNNEL_BASE_PORT,
            )
            object.__setattr__(self, "tunnel_base_port", _DEFAULT_TUNNEL_BASE_PORT)
        if self.max_recovery_attempts < 1:
            logger.warning(
                "instances.max_recovery_attempts %d < 1, using %d",
                self.max_recovery_attempts,
                _DEFAULT_MAX_RECOVERY,
            )
            object.__setattr__(self, "max_recovery_attempts", _DEFAULT_MAX_RECOVERY)
        elif self.max_recovery_attempts > _MAX_RECOVERY_CEILING:
            logger.warning(
                "instances.max_recovery_attempts %d > %d, clamping to %d "
                "(guards against a near-infinite self-heal loop on a dead connection)",
                self.max_recovery_attempts,
                _MAX_RECOVERY_CEILING,
                _MAX_RECOVERY_CEILING,
            )
            object.__setattr__(self, "max_recovery_attempts", _MAX_RECOVERY_CEILING)
        if self.recover_backoff_max_secs <= 0:
            logger.warning(
                "instances.recover_backoff_max_secs %s <= 0, using %s",
                self.recover_backoff_max_secs,
                _DEFAULT_BACKOFF_MAX,
            )
            object.__setattr__(self, "recover_backoff_max_secs", _DEFAULT_BACKOFF_MAX)
        elif self.recover_backoff_max_secs > _RECOVER_BACKOFF_CEILING:
            logger.warning(
                "instances.recover_backoff_max_secs %s > %s, clamping to %s "
                "(guards against a multi-day self-heal window on a dead connection)",
                self.recover_backoff_max_secs,
                _RECOVER_BACKOFF_CEILING,
                _RECOVER_BACKOFF_CEILING,
            )
            object.__setattr__(self, "recover_backoff_max_secs", _RECOVER_BACKOFF_CEILING)
        if self.probe_failure_threshold < 1:
            logger.warning(
                "instances.probe_failure_threshold %d < 1, using %d",
                self.probe_failure_threshold,
                _DEFAULT_PROBE_FAILS,
            )
            object.__setattr__(self, "probe_failure_threshold", _DEFAULT_PROBE_FAILS)


@dataclass
class HeartbeatConfig:
    """Heartbeat background task queue (~/.kirocrew/workspace/HEARTBEAT.md)."""

    default_deliver: str = field(
        default="slack",
        metadata=_meta(
            "Default delivery",
            "Where a heartbeat completion with no inline <!-- deliver:... --> tag is "
            "routed: 'slack' (Slack DM + dashboard bell, the default) or 'dashboard' "
            "(dashboard slot + bell only, no Slack). Per-task deliver tags always "
            "override this.",
        ),
    )


@dataclass
class WatchdogConfig:
    """ACP per-session watchdog / liveness-oracle tuning (acp/session_handle.py).

    Wellness (the liveness oracle) is the primary detector; these windows govern
    only the UNKNOWN-verdict backstop class. A WORKING verdict is never acted on
    at any elapsed time, and every watchdog action is non-lethal (auto-recovery,
    never a silent kill).
    """

    check_after_secs: float = field(
        default=60.0,
        metadata=_meta(
            "Check after (s)",
            "Idle seconds on a turn before the liveness oracle is consulted at all. "
            "Below this, the dispatch loop does no watchdog work.",
        ),
    )
    stale_window_secs: float = field(
        default=300.0,
        metadata=_meta(
            "Stale probe window (s)",
            "Idle seconds before an UNKNOWN-verdict model-wait turn is safe-probed "
            "via session/cancel. Probes are non-lethal: a live turn auto-recovers.",
        ),
    )
    tool_stall_suspect_secs: float = field(
        default=600.0,
        metadata=_meta(
            "Tool stall suspect (s)",
            "Idle seconds before an UNKNOWN-verdict in-flight tool is cancelled and "
            "the turn routed to tool-stall recovery (continue-nudge, no re-run of "
            "the original message). WORKING tools (e.g. a matched live build child) "
            "are never cancelled regardless of duration.",
        ),
    )
    tool_stall_hard_cap_secs: float = field(
        default=2700.0,
        metadata=_meta(
            "Hard cap (s)",
            "Absolute ceiling for UNKNOWN-verdict forbearance (e.g. the extended "
            "probably-thinking window). Applies ONLY to UNKNOWN verdicts — never "
            "to a WORKING session.",
        ),
    )
    model_silent_probe_secs: float = field(
        default=900.0,
        metadata=_meta(
            "Silent-think probe window (s)",
            "Extended probe window for a model-wait with an established backend "
            "connection but flat counters (non-streamed server-side reasoning, "
            "e.g. long xhigh thinks). Probing a live think cancels and regenerates "
            "it, so this window is deliberately generous.",
        ),
    )
    wellness_sample_secs: float = field(
        default=3.0,
        metadata=_meta(
            "Wellness sample interval (s)",
            "Minimum spacing between CPU/IO counter samples used for movement "
            "deltas in the liveness oracle.",
        ),
    )


@dataclass
class TunnelConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta("Enabled", "Enable a tunnel to expose the dashboard for remote access."),
    )
    name_mode: str = field(
        default="username",
        metadata=_meta(
            "Name Mode",
            "Tunnel naming: 'username' uses 'kirocrew', "
            "'hash' uses 'kirocrew-<hostHash>' for multi-host disambiguation.",
            enum=["username", "hash"],
        ),
    )
    name_override: str = field(
        default="",
        metadata=_meta(
            "Name Override",
            "Explicit tunnel name (overrides name_mode). "
            "Note: some tunnel providers prefix your username (e.g. 'foo' becomes '<user>-foo').",
        ),
    )


@dataclass
class WeComConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the WeChat channel via WeCom AI-bot. Requires the WECOM_BOT_ID "
            "and WECOM_SECRET credentials to be set.",
            tags=["wechat"],
        ),
    )
    allowed_users: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Users",
            "WeCom users allowed to DM the bot. Each entry: {userid, name}. "
            "The owner is always allowed.",
            tags=["wechat"],
        ),
    )
    ws_url: str = field(
        default="wss://openws.work.weixin.qq.com",
        metadata=_meta(
            "WebSocket URL",
            "WeCom AI-bot long-connection endpoint.",
            tags=["wechat"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["wechat"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["wechat"],
        ),
    )

    def __post_init__(self) -> None:
        # Clamp thresholds to [0, 100] and guarantee soft <= hard so a misconfig
        # (e.g. hard=50, soft=95, or an out-of-range value) can't make the soft
        # nudge unreachable -- _maybe_notice checks ``pct >= hard`` first.
        self.soft_threshold_pct = max(0, min(100, self.soft_threshold_pct))
        self.hard_threshold_pct = max(0, min(100, self.hard_threshold_pct))
        if self.soft_threshold_pct > self.hard_threshold_pct:
            self.soft_threshold_pct = self.hard_threshold_pct


def _coerce_int_ids(raw: object) -> list[int]:
    """Coerce a config value to a clean ``list[int]``, dropping anything invalid.

    Fail closed against a hand-edited config: a non-list (e.g. the string
    ``"12345"``) yields ``[]`` instead of iterating char-by-char, and any entry
    that isn't a clean base-10 integer (``"--100"``, ``"1.5"``, unicode digits,
    booleans) is skipped rather than raising in ``int()`` and crashing config
    load / gateway startup.
    """
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for u in raw:
        try:
            ids.append(int(str(u)))
        except (TypeError, ValueError):
            continue
    return ids


def _coerce_int(raw: object, default: int) -> int:
    """Return ``int(raw)`` or *default* if *raw* isn't a clean base-10 integer.

    Fail closed against a hand-edited non-numeric config value (e.g. ``"abc"``)
    that would otherwise raise in ``int()`` and crash config load.
    """
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


@dataclass
class TelegramConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Telegram Bot API channel (long-polling). Requires "
            "TELEGRAM_BOT_TOKEN (env/.env) or telegram.bot_token.",
            tags=["telegram"],
        ),
    )
    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Telegram Bot API token from @BotFather. Prefer the TELEGRAM_BOT_TOKEN "
            "credential (env/.env) over storing it here.",
            tags=["telegram"],
            sensitive=True,
        ),
    )
    allowed_user_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Numeric Telegram user IDs permitted to DM the bot. Empty = deny all "
            "(fail closed): a Telegram bot is globally reachable by @username.",
            tags=["telegram"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to /compact or /new when context passes this percentage.",
            tags=["telegram"],
        ),
    )
