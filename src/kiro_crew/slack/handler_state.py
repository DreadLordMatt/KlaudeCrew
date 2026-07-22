"""Split from slack/handler.py: handler_state cluster."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.providers.base import LLMProvider
from kiro_crew.safety_override import safety_override
from kiro_crew.voice_reply import VALID_PROVIDERS
from kiro_crew.voice_reply import validate_length_scale as _validate_length_scale

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)


# Approval modes (UX-level, not provider-specific)
APPROVAL_AUTO = "auto"
APPROVAL_INTERACTIVE = "interactive"


# Trust/YOLO state
# trust: auto-approve tools for a specific session (via Trust button)
# yolo: auto-approve all tools globally for all sessions (via !yolo on command, owner-only)
_trusted_sessions: set[str] = set()
_YOLO_TTL_SECS = 1800  # 30 minutes for !yolo on command

# Allowed user IDs for Slack access (set by gateway at startup).
# Falls back to single KIROCREW_OWNER_ID for backward compatibility.
_allowed_users: set[str] = set()


# ── Voice reply state ──
@dataclass
class _VoiceConfig:
    """Per-session and global voice reply settings."""

    sessions: set[str] = None  # type: ignore[assignment]  # threads with voice on
    global_enabled: bool = False
    auto_speak: bool = False
    voices: dict[str, str] = None  # type: ignore[assignment]
    engines: dict[str, str] = None  # type: ignore[assignment]
    rates: dict[str, str] = None  # type: ignore[assignment]
    pitches: dict[str, str] = None  # type: ignore[assignment]
    default_voice: str = "Ruth"
    default_engine: str = "generative"
    default_rate: str = "100%"
    default_pitch: str = "+0%"
    aws_profile: str = ""
    region: str = ""
    # TTS provider: "polly" (default, AWS) or "piper" (local neural TTS).
    provider: str = "polly"
    # Piper-specific (ignored when provider="polly"):
    piper_binary: str = ""
    piper_model: str = ""
    piper_model_config: str = ""
    piper_length_scale: float = 1.0
    # If True, a message carrying voice input (a transcribed voice memo)
    # automatically receives a voice reply, even without `!voice on`. The
    # config-load default follows ``enabled`` (see ``set_orch_cfg``); the
    # in-memory default below is False so an unconfigured ``_VoiceConfig``
    # behaves the same as a default-config user (``enabled=false``).
    auto_reply_to_voice: bool = False

    def __post_init__(self) -> None:
        self.sessions = self.sessions or set()
        self.voices = self.voices or {}
        self.engines = self.engines or {}
        self.rates = self.rates or {}
        self.pitches = self.pitches or {}


_vc = _VoiceConfig()

# Primary owner ID — for owner-only commands like !agent.
_owner_id: str = ""

# Tracked channel IDs for member_joined_channel monitoring.
_tracking_channels: set[str] = set()
_open_channels: set[str] = set()

# Live reference to the orchestrator's config — set by events.py, reloaded
# after !channel writes so activation changes take effect immediately.
_orch_cfg: KiroCrewConfig | None = None

# Dashboard state reference for pushing refresh events (set by gateway).
_dashboard_state: object | None = None

# Per-thread agent overrides: session_key → agent name.
# Set via !ta command (thread-agent).
_thread_agents: dict[str, str] = {}

# Per-thread project directory overrides: session_key → absolute path.
# Set via !project command.
_thread_projects: dict[str, str] = {}

# Guard set for _hydrate_thread_overrides to avoid repeated I/O per session.
_hydrated_sessions: set[str] = set()


# Tracks Slack threads that already have a title (auto or manual).
# Bounded LRU to prevent unbounded growth in long-running bots.

_TITLED_THREADS_MAX = 10_000
_titled_threads: OrderedDict[str, str | None] = OrderedDict()


def _mark_titled(key: str, kind: str | None = None) -> None:
    """Add key to the bounded LRU title tracker."""
    _titled_threads[key] = kind
    _titled_threads.move_to_end(key)
    if len(_titled_threads) > _TITLED_THREADS_MAX:
        _titled_threads.popitem(last=False)


class _PendingApproval:
    __slots__ = ("provider", "request_id", "session_key", "future")

    def __init__(self, provider: LLMProvider, request_id: str | int, session_key: str = "") -> None:
        self.provider = provider
        self.request_id = request_id
        self.session_key = session_key
        self.future: asyncio.Future[str] = asyncio.get_running_loop().create_future()


class _LinkedApproval:
    """A tool-approval prompt posted to Slack on behalf of a *linked dashboard
    slot*.

    Unlike :class:`_PendingApproval`, this entry does NOT own the ACP backend
    answer. For a Slack-linked dashboard session the consumer that actually
    calls ``approve_tool`` / ``reject_tool`` is the dashboard's ``_run_chat``
    loop, which is parked on the slot's approval *future*. A Slack button click
    here must therefore ONLY resolve that future (via
    ``state.resolve_approval``); the dashboard loop then answers the backend
    exactly once. Calling ``approve_tool`` from here too would answer the
    JSON-RPC request twice.
    """

    __slots__ = ("request_id", "session_key")

    def __init__(self, request_id: str | int, session_key: str) -> None:
        self.request_id = request_id
        self.session_key = session_key


def set_allowed_users(user_ids: set[str]) -> None:
    """Set the allowed user IDs for Slack access (called by gateway)."""
    global _allowed_users
    _allowed_users = user_ids


def set_owner_id(owner_id: str) -> None:
    """Set the primary owner ID for owner-only commands (called by gateway)."""
    global _owner_id
    _owner_id = owner_id


def set_yolo_mode(enabled: bool) -> None:
    """Set YOLO mode at startup from config (called by gateway)."""
    if enabled:
        safety_override().activate("config")


def set_orch_cfg(cfg: KiroCrewConfig) -> None:
    """Store a live reference to the orchestrator's config (called by events.py)."""
    global _orch_cfg
    _orch_cfg = cfg
    # Load voice_reply defaults from config
    _vr: dict = cfg.raw.get("voice_reply", {}) if hasattr(cfg, "raw") else {}
    if not _vr:
        try:
            with open(config_path()) as f:
                _vr = json.load(f).get("voice_reply", {})
        except Exception:
            _vr = {}
    _enabled = bool(_vr.get("enabled", False))
    if _enabled:
        _vc.global_enabled = True
    _vc.auto_speak = bool(_vr.get("auto_speak", False))
    _vc.default_voice = _vr.get("voice_id", "Ruth")
    _vc.default_engine = _vr.get("engine", "generative")
    _vc.default_rate = _vr.get("rate", "100%")
    _vc.default_pitch = _vr.get("pitch", "+0%")
    _vc.aws_profile = _vr.get("aws_profile", "")
    _vc.region = _vr.get("region", "")
    # ``auto_reply_to_voice`` defaults to ``enabled``'s value: users with
    # explicit ``enabled=false`` keep the existing zero-voice behavior, and
    # users who turn voice on globally also get symmetric voice-in/voice-out
    # without needing to set a second flag.
    _vc.auto_reply_to_voice = bool(_vr.get("auto_reply_to_voice", _enabled))
    # Validate provider on load — a typo (e.g. "ploly") would otherwise pass
    # through and only fail at synthesis time, after the user has already sent
    # a voice memo expecting a voice reply.
    _provider = _vr.get("provider", "polly")
    if _provider not in VALID_PROVIDERS:
        logger.warning(
            "voice_reply.provider %r not in %s, defaulting to %r",
            _provider,
            sorted(VALID_PROVIDERS),
            "polly",
        )
        _provider = "polly"
    _vc.provider = _provider
    _vc.piper_binary = _vr.get("piper_binary", "")
    _vc.piper_model = _vr.get("piper_model", "")
    _vc.piper_model_config = _vr.get("piper_model_config", "")
    # Coerce to finite/positive — a config.json with inf/NaN (JSON accepts both)
    # would otherwise reach synthesis and be re-serialized as non-RFC JSON,
    # breaking the dashboard's config GET.
    _vc.piper_length_scale = _validate_length_scale(_vr.get("piper_length_scale", 1.0))


def set_dashboard_state(state: object) -> None:
    """Store dashboard state reference for push_refresh (called by gateway)."""
    global _dashboard_state
    _dashboard_state = state


def _reload_orch_cfg() -> None:
    """Reload in-memory config after !channel writes so changes take effect immediately."""
    if _orch_cfg is not None:
        fresh = KiroCrewConfig.load()
        _orch_cfg.slack_channels = fresh.slack_channels
        _orch_cfg.slack_dm_activation = fresh.slack_dm_activation


def is_owner(user_id: str) -> bool:
    """Check if *user_id* is the primary owner (with W/U prefix cross-match)."""
    if not _owner_id or not user_id:
        return False
    if user_id == _owner_id:
        return True
    return user_id.replace("W", "U", 1) == _owner_id or user_id.replace("U", "W", 1) == _owner_id


def disable_yolo() -> None:
    """Disable YOLO mode (global auto-approve)."""
    if not safety_override().is_active():
        return
    safety_override().deactivate("slack")
    _trusted_sessions.clear()
    logger.info("YOLO mode OFF")


def enable_yolo_with_ttl(ttl_secs: int) -> None:
    """Enable YOLO mode with a specific TTL."""
    safety_override().activate("slack", ttl=ttl_secs)
    logger.info("YOLO mode ON (expires in %ds)", ttl_secs)


def is_yolo_mode() -> bool:
    """Return whether YOLO mode is currently active."""
    return safety_override().is_active()


def is_slack_session_trusted(session_key: str) -> bool:
    """Return whether *session_key* has been granted per-session Trust.

    Per-session trust auto-approves all subsequent tools for THIS session only
    (distinct from global YOLO). Populated by the Trust button on both the
    native and messaging-transport approval prompts.
    """
    return bool(session_key) and session_key in _trusted_sessions


def add_trusted_session(session_key: str, sessions: "SessionManager | None" = None) -> None:
    """Grant per-session Trust for *session_key* (mirrors native trust_tool).

    Adds the session to the in-memory trust set and, when a SessionManager is
    supplied, sets its approval policy to ``auto`` so spawned subagents inherit
    the trust (they read the parent's approval policy, not the in-memory set).
    """
    if not session_key:
        return
    _trusted_sessions.add(session_key)
    if sessions is not None:
        try:
            sessions.set_approval_policy(session_key, "auto")
        except Exception:
            logger.warning(
                "Failed to propagate trust approval policy for %s",
                session_key,
                exc_info=True,
            )


def is_allowed_user(user_id: str) -> bool:
    """Check if user_id is the owner.

    Multi-user access is disabled for security — only the owner
    (KIROCREW_OWNER_ID) is authorized to interact via Slack.
    """
    if not user_id:
        return False
    return is_owner(user_id)


def set_tracking_channels(channel_ids: set[str]) -> None:
    """Set the tracked channel IDs (called by gateway/interactions)."""
    global _tracking_channels
    _tracking_channels = channel_ids


def set_open_channels(channel_ids: set[str]) -> None:
    """Set channel IDs where all users are authorized (no allowlist needed)."""
    global _open_channels
    _open_channels = channel_ids


def is_open_channel(channel_id: str) -> bool:
    """Open channels are disabled — multi-user access is blocked for security."""
    return False


def is_tracked_channel(channel_id: str) -> bool:
    """Check if *channel_id* is in the tracking set."""
    return bool(channel_id and channel_id in _tracking_channels)
