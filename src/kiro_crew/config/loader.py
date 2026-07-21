"""Configuration loader for KiroCrew.

Config location: ~/.kirocrew/config.json (overridden by KIROCREW_HOME)
Credentials:    ~/.kirocrew/.env (overridden by KIROCREW_HOME)

KiroCrew is KiroACP-only: the sole provider is the ACP adapter driving the
kiro-cli backend. This module handles session timeouts, hook rules, and the
dashboard URL via the config file. (The dashboard *port* is set with the
``KIROCREW_PORT`` env var, not a config key.)
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew.config.bindings import (  # noqa: F401
    _workspace_name_for_dir,
    build_provider_factory,
    resolve_agent_bindings,
    validate_kiro_agent_references,
)
from kiro_crew.config.paths import (  # noqa: F401
    _WORKSPACE_DIR_NAME,
    CONFIG_DIR_NAME,
    OUTBOX_DIR_NAME,
    _default_workspace_base,
    _safe_dir_name,
    config_dir,
    config_package_dir,
    kiro_agents_dir,
)
from kiro_crew.config.root import KiroCrewConfig  # noqa: F401

# The per-feature config dataclasses, the aggregate ``KiroCrewConfig`` root, and
# the provider-factory / agent-binding helpers were extracted into sibling
# modules (``sections``, ``root``, ``bindings``). They are re-exported here so
# every historical ``from kiro_crew.config.loader import X`` keeps working.
from kiro_crew.config.sections import (  # noqa: F401
    _BOT_NAME_MAX,
    _BOT_NAME_RE,
    _VALID_ACTIVATIONS,
    _VALID_CHANNEL_PREFIXES,
    _VALID_COMPLETION_KEEP,
    _VALID_JAIL_MODES,
    _VALID_STT_PROVIDERS,
    ACTIVATION_ALWAYS,
    ACTIVATION_MENTION,
    ACTIVATION_OBSERVE,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
    DEFAULT_MAX_PARALLEL_STEPS,
    DEFAULT_MODEL,
    DEFAULT_SESSION_TIMEOUT,
    JAIL_MODE_AUTO,
    JAIL_MODE_OFF,
    JAIL_MODE_ON,
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
    ResolvedBindings,
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
    resolve_memory_store_config,
)
from kiro_crew.config.validation import (  # noqa: F401
    _CONFIG_CACHE,
    _CONFIG_CACHE_LOCK,
    _HAS_JSONSCHEMA,
    _actual_type_name,
    _apply_field_default,
    _dot_path_from_json_path,
    _get_help_text,
    _is_deprecated_path,
    _is_sensitive_path,
    _lookup_schema_node,
    _mask_value,
)
from kiro_crew.config.validation import validate_config_data as _validate_config_data  # noqa: F401

logger = logging.getLogger(__name__)

# Credential keys loaded from .env / environment
CRED_SLACK_APP_TOKEN = "SLACK_APP_TOKEN"
CRED_SLACK_BOT_TOKEN = "SLACK_BOT_TOKEN"
CRED_OWNER_ID = "KIROCREW_OWNER_ID"
CRED_WECOM_BOT_ID = "WECOM_BOT_ID"
CRED_WECOM_SECRET = "WECOM_SECRET"
CRED_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
_CREDENTIAL_KEYS = (
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    CRED_OWNER_ID,
    CRED_WECOM_BOT_ID,
    CRED_WECOM_SECRET,
    CRED_TELEGRAM_BOT_TOKEN,
)

_DEFAULT_PORT = 5476

# KIROCREW_PORT is validated at CLI entry (cli.py main()).
# By the time loader.py is imported the env var is a valid int or absent.
DASHBOARD_PORT: int = int(os.environ.get("KIROCREW_PORT", _DEFAULT_PORT))


# Dir-derived path helpers (workspace_root, config_path, workspace_dir_for, …)
# build on the pure primitives imported from ``config.paths`` above. They live
# here — not in the leaf — so their ``config_dir()`` / ``_default_workspace_base()``
# lookups resolve in this module's namespace, keeping the
# ``patch("kiro_crew.config.loader.config_dir", ...)`` test seam working.


def _workspace_dir_file() -> Path:
    """Return the path to the saved workspace_dir file, respecting KIROCREW_HOME."""
    return config_dir() / "workspace_dir"


def _resolve_workspace_root(root: Path) -> Path:
    """Realpath-normalize a workspace root after ensuring it exists.

    On hosts with a symlinked ``$HOME``/workspace path (e.g. ``/home/<u> ->
    /local/home/<u>``, ``/home/<u>/workplace -> /workplace/<u>``) the symlink-form
    root and its resolved form name the same directory via different strings. The
    per-session work_dir built from this root is passed as the spawn cwd and
    persisted as ``cwd`` in session_map.json. If the stored cwd is the symlink form
    while the transcript is written under the resolved form, cold resume misses and
    silently falls back to a fresh session.

    Normalizing here, at the single source, makes the SAME resolved path flow into
    spawn cwd and the persisted session_map cwd so write and resume always agree.
    This mirrors the existing ``os.path.realpath`` in ``default_project_dir``.
    """
    root.mkdir(parents=True, exist_ok=True)
    return Path(os.path.realpath(str(root)))


def workspace_root() -> Path:
    """Return the top-level workspace root for LLM sessions and tasks.

    Resolution order:
    1. ``KIROCREW_WORKSPACE`` env var (used as-is, no subdirectory appended)
    2. Saved path in ``config_dir()/workspace_dir`` (written by ``kirocrew setup``)
    3. Platform default with ``kirocrew-workspace`` subdirectory

    The chosen root is realpath-normalized (see ``_resolve_workspace_root``) so
    sessions resume correctly on hosts with a symlinked home/workspace path.
    """
    override = os.environ.get("KIROCREW_WORKSPACE")
    if override:
        return _resolve_workspace_root(Path(override))
    if _workspace_dir_file().is_file():
        try:
            saved = _workspace_dir_file().read_text(encoding="utf-8").strip()
            if saved:
                return _resolve_workspace_root(Path(saved))
        except OSError:
            pass
    base = _default_workspace_base()
    return _resolve_workspace_root(base / _WORKSPACE_DIR_NAME)


def _safe_int(value: object, default: int) -> int:
    """Convert *value* to int, returning *default* on failure."""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def _safe_float(
    value: object,
    default: float,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    """Convert *value* to float, returning *default* on failure, clamped to [lo, hi].

    Non-finite results (NaN/Infinity) are replaced with *default* — NaN compares
    false against any bound so it would silently bypass clamping (e.g. a
    configured ``tips_cadence_hours: NaN`` would permanently suppress tips).
    """
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        # OverflowError: json parses arbitrarily large ints fine, but float()
        # on a several-hundred-digit int raises — must not crash config load.
        result = default
    if not math.isfinite(result):
        result = default
    if lo is not None and result < lo:
        result = lo
    if hi is not None and result > hi:
        result = hi
    return result


def _session_work_dir(session_key: str | None) -> Path:
    """Return a per-session subdirectory under workspace_root()."""
    root = workspace_root()
    if session_key:
        return root / _safe_dir_name(session_key)
    return root / "_default"


def outbox_dir() -> Path:
    """Return the outbox directory for agent-to-user file delivery."""
    d = workspace_root() / OUTBOX_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def config_local_path() -> Path:
    """Return path to config.local.json — user overrides that survive upgrades."""
    return config_dir() / "config.local.json"


def read_local_secret() -> str:
    """Read ``<config_dir>/.local_secret`` (the gateway IPC secret), or ``""``.

    Single home for the secret-file read that callers (cron scripts, MCP tool
    bridges, CLI) need to authenticate to the gateway's internal API. Returns
    empty string if the file is absent/unreadable.
    """
    try:
        return (config_dir() / ".local_secret").read_text().strip()
    except OSError:
        return ""


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* into *base*, returning a new dict.

    - Dict values are merged recursively
    - All other types in overlay replace base values
    - Keys in overlay not in base are added
    """
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _subtract_overlay(merged: dict, overlay: dict) -> dict:
    """Remove leaf values from *merged* that are owned by the overlay.

    For nested dicts, recurse. For leaf keys present in both overlay and
    merged with the same value, remove from the result so they only live
    in config.local.json.
    """
    result = dict(merged)
    for key, ov_value in overlay.items():
        if key not in result:
            continue
        if isinstance(ov_value, dict) and isinstance(result[key], dict):
            cleaned = _subtract_overlay(result[key], ov_value)
            if cleaned:
                result[key] = cleaned
            else:
                del result[key]
        elif result[key] == ov_value:
            del result[key]
    return result


def _raw_config() -> dict:
    """Load raw config.json as dict (cached per process)."""
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def workspace_dir_for(workspace: str | None = None) -> Path:
    """Resolve a named workspace to its directory path.

    Reads the ``dir`` field from ``WorkspaceConfig`` objects (new structured
    format) or falls back to raw string values (legacy flat format).

    Values starting with ``/`` or ``~`` are treated as absolute paths.
    Otherwise the value is relative to ``config_dir()`` (``~/.kirocrew/``).
    Unmapped workspace names fall back to ``"workspace"``.
    """
    data = _raw_config()
    ws = workspace or data.get("default_workspace", "default")
    mapping = data.get("workspaces", {})
    raw_value = mapping.get(ws, "workspace")

    # Extract the directory string from either format
    if isinstance(raw_value, dict):
        dirname = raw_value.get("dir", "workspace")
    elif isinstance(raw_value, str):
        dirname = raw_value
    else:
        dirname = "workspace"

    p = Path(dirname).expanduser()
    if p.is_absolute():
        return p
    return config_dir() / dirname


def default_project_dir(workspace: str | None = None) -> str:
    """Resolve the default project directory for a workspace.

    Returns the realpath of ``workspace_dir_for(workspace)`` if it exists and
    is not a sensitive path, otherwise returns ``""``.

    Used by chat_handlers (slot.project fallback) and session.py (pool cwd)
    to avoid duplicating the same resolution + validation logic.
    """
    from kiro_crew.security import is_sensitive_path  # circular import

    try:
        ws_dir = os.path.realpath(str(workspace_dir_for(workspace)))
        if os.path.isdir(ws_dir) and not is_sensitive_path(ws_dir):
            return ws_dir
    except Exception:
        pass
    return ""


def env_path() -> Path:
    return config_dir() / ".env"


def resolve_agent_config_path() -> Path:
    """Return defaults.json, preferring project-dir override for development.

    All modules that need the agent config path should call this instead
    of reimplementing the resolution chain.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj:
        p = Path(proj) / "agents" / "defaults.json"
        if p.exists():
            return p
    return config_package_dir() / "defaults.json"


# ---------------------------------------------------------------------------
# Validation helpers — used by KiroCrewConfig.load()
# ---------------------------------------------------------------------------

# JSON Schema type → Python type names for log messages
_JSON_TYPE_LABELS: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


# ---------------------------------------------------------------------------
# Security-relevant resource-limit ceilings
# ---------------------------------------------------------------------------
# SINGLE SOURCE OF TRUTH for the upper bounds on the config knobs that govern
# host resource consumption. These same ceilings are enforced by the dashboard
# config API (``dashboard/handlers/core.py`` for the agent knobs,
# ``session.py`` for ``pool_size``); they live HERE so the API-write gate and
# the load-time clamp below cannot drift apart.
#
# Why the loader must also clamp (pentest — config-loader bound bypass): the
# REST API rejects out-of-range writes, but a direct edit of ``config.json``
# (any process running as the same OS user — including a prompt-injected agent
# with file-write access) bypassed that gate entirely. Each of these knobs
# controls a resource-consumption dimension — concurrent subagent processes
# (each a separate kiro-cli process), per-agent turn budget (unbounded LLM
# calls + context growth), and pre-warmed pool processes spawned at startup —
# so an inflated on-disk value can exhaust host memory / CPU / the process
# table (denial of service). Clamping at load time makes the on-disk value
# untrusted above range no matter which consumer reads it, and also means the
# GET /api/config/kirocrew response (which serializes a freshly loaded config)
# reports the clamped value rather than the tampered one.
SUBAGENT_AUTO_MAX_CEILING = 64  # agent.subagent_auto_max — concurrent subagent ceiling
SUBAGENT_MAX_TURNS_CEILING = 200  # agent.subagent_max_turns — per-subagent turn budget
POOL_SIZE_MAX = 10  # session.pool_size — pre-warmed process pool

# (section, key, min, max) for each bounded field clamped at load time. The
# mins match the existing runtime floors (0 / 1) so a legitimate in-range value
# is never altered — only out-of-range (tampered) values are clamped.
_SECURITY_BOUNDED_FIELDS: tuple[tuple[str, str, int, int], ...] = (
    ("agent", "subagent_auto_max", 1, SUBAGENT_AUTO_MAX_CEILING),
    ("agent", "max_subagents", 0, SUBAGENT_AUTO_MAX_CEILING),
    ("agent", "subagent_max_turns", 1, SUBAGENT_MAX_TURNS_CEILING),
    ("session", "pool_size", 0, POOL_SIZE_MAX),
)


def _log_config_clamp_event(field: str, file_value: int, clamped: int, lo: int, hi: int) -> None:
    """Emit a best-effort SEL security event for a clamped (tampered) config value.

    Recorded so tampering is detectable after the fact even though the loader
    self-heals by clamping. Lazily imports the SEL to avoid an import cycle and
    to keep the hot load() path free of SEL cost on the normal (in-range) path —
    this only fires when a value was actually out of range. Wrapped so a SEL
    failure can never make config loading raise.
    """
    try:
        from kiro_crew.sel import SecurityEvent, sel

        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type="config_bounds_clamped",
                caller_identity="config_loader",
                agent="",
                source="background",
                operation="config.load",
                outcome="clamped",
                resources=field,
                metadata={
                    "file_value": file_value,
                    "clamped_to": clamped,
                    "min": lo,
                    "max": hi,
                },
            )
        )
    except Exception:
        logger.debug("SEL config-clamp event failed", exc_info=True)


def _clamp_security_bounds(data: dict) -> None:
    """Clamp security-relevant bounded integers in *data* in place.

    Applies the same ceilings the dashboard API enforces at write time to the
    values read from disk (see ``_SECURITY_BOUNDED_FIELDS`` and the module-level
    ceiling constants for the rationale). Called once on the actual disk-read
    path (cache miss) BEFORE the validated dict is cached, so:

    * subsequent cache hits already serve clamped values (consistent), and
    * the tamper warning / SEL event fires once per file change — enough to
      detect tampering without spamming the hot load() path.

    Only real integers are clamped; ``bool`` (a JSON ``true``/``false``) and any
    non-int are left untouched for the dataclass construction path to
    coerce/default. A clamp is logged at WARNING and recorded as a SEL security
    event; both are best-effort and never fatal (config loading must not raise).
    """
    for section, key, lo, hi in _SECURITY_BOUNDED_FIELDS:
        sect = data.get(section)
        if not isinstance(sect, dict) or key not in sect:
            continue
        val = sect[key]
        # bool is an int subclass; a JSON true/false is not a real bound value.
        if isinstance(val, bool) or not isinstance(val, int):
            continue
        if val < lo or val > hi:
            clamped = max(lo, min(hi, val))
            sect[key] = clamped
            logger.warning(
                "config %s.%s=%d out of range [%d, %d]; clamped to %d "
                "(possible config tampering — a direct file edit cannot exceed "
                "the API-enforced ceiling)",
                section,
                key,
                val,
                lo,
                hi,
                clamped,
            )
            _log_config_clamp_event(f"{section}.{key}", val, clamped, lo, hi)


def _config_fingerprint() -> tuple:
    """Cheap signature of the config files — changes whenever either is edited.

    Uses st_mtime_ns + st_size + st_mode for both config.json and
    config.local.json so any edit, truncation, or replacement busts the cache.
    A missing file contributes a sentinel so create/delete also busts it.
    """
    sig: list = []
    for p in (config_path(), config_local_path()):
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size, st.st_mode))
        except OSError:
            sig.append((str(p), None))
    return tuple(sig)


def _cached_validated_data() -> dict | None:
    """Return a deep copy of the cached validated config dict, or None on miss.

    Thin wrapper over the :class:`~kiro_crew.config.validation.ConfigCache`:
    the fingerprint is computed here (``_config_fingerprint`` stays in this
    module because it reads ``config_path()``/``config_local_path()``, which the
    test suite patches as ``kiro_crew.config.loader.config_path``).
    """
    return _CONFIG_CACHE.get(_config_fingerprint())


def _store_validated_data(data: dict, fp: tuple) -> None:
    """Cache a deep copy of *data* under fingerprint *fp* (see ConfigCache.store)."""
    _CONFIG_CACHE.store(data, fp)


def _invalidate_config_cache() -> None:
    """Drop the cached validated config (called after save()/write-back)."""
    _CONFIG_CACHE.clear()
