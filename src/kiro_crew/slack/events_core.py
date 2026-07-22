"""Slack Socket Mode setup and shared event-routing primitives.

Owns the module-level state shared across the ``events_*`` submodules: the
fire-and-forget task-tracking sets (``_bg_tasks`` / ``_background_tasks``), the
slash-command registry (``SLASH_REGISTRY`` + ``register_slash_command``), the
bounded dedup cache (``SeenCache``), the skills-loader singleton, the log
sanitizer (``_safe_log``), and the Socket Mode client wiring
(``init_socket_mode``).

Split out of the former monolithic ``events.py`` (now a thin re-export shim).
``init_socket_mode`` lazily imports the sibling submodules (``events_slash`` /
``events_hometab`` / ``events_message``) so their slash registrations fire and
their handlers are reachable without a module-level import cycle.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient as WSSocketModeClient
from slack_sdk.web.async_client import AsyncWebClient

from kiro_crew.platform import current_context
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack.handler import (
    is_allowed_user,
    set_allowed_users,
    set_dashboard_state,
    set_open_channels,
    set_orch_cfg,
    set_owner_id,
    set_tracking_channels,
    set_yolo_mode,
)
from kiro_crew.slack.interactions import dispatch as dispatch_interactive

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


_skills_loader: SkillsLoader | None = None

# Strong references to fire-and-forget asyncio.Tasks scheduled from
# synchronous paths. Python's event loop keeps only *weak* references to
# tasks, so without a strong reference a task can be garbage-collected
# mid-execution. Tasks remove themselves on completion via add_done_callback.
_bg_tasks: set[asyncio.Task[object]] = set()


def _spawn_tracked(coro: Coroutine[object, object, object]) -> asyncio.Task[object]:
    """Schedule *coro* as a task and retain a strong reference until it finishes.

    ``asyncio.create_task``/``ensure_future`` alone is not enough: the event loop
    keeps only a weak reference, so a fire-and-forget task can be garbage-collected
    mid-execution (silently dropping the work). Tracking it in ``_bg_tasks`` and
    discarding on completion keeps it alive for its whole lifetime.
    """
    task = asyncio.ensure_future(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_on_tracked_done)
    return task


def _on_tracked_done(task: asyncio.Task[object]) -> None:
    """Discard a finished tracked task and surface any failure.

    A bare ``discard`` swallows exceptions from the spawned coroutine — a failed
    ``_respond`` POST (expired ``response_url``, network timeout) or a raising slash
    handler would store the exception in the task, which nobody reads and which is
    GC'd with the task, leaving operators blind to dropped slash replies. Log at
    DEBUG (these failures are routine and peer-driven) while still discarding.
    """
    _bg_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("Tracked slash task failed: %s", exc)


def _get_skills_loader() -> SkillsLoader:
    global _skills_loader  # noqa: PLW0603
    if _skills_loader is None:
        _skills_loader = SkillsLoader()
    return _skills_loader


# Suppress noisy Slack SDK WebSocket reconnect errors — these are normal
# idle connection drops that the SDK handles automatically.
# WARNING lets ERROR through (recv failures, reconnect failures) while
# suppressing INFO (session established) and DEBUG (every message/ping).
logging.getLogger("slack_sdk.socket_mode.websockets").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Dedup cache — bounded LRU to avoid processing duplicate Slack events
# ---------------------------------------------------------------------------

_MAX_SEEN = 5000


# prevent GC of fire-and-forget tasks (Python event loop holds weak refs)
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


class SeenCache:
    """Bounded set that remembers the last *maxlen* event IDs."""

    def __init__(self, maxlen: int = _MAX_SEEN):
        self._d: OrderedDict[str, None] = OrderedDict()
        self._maxlen = maxlen

    def check_and_add(self, key: str) -> bool:
        """Return ``True`` if *key* was already seen, else mark it."""
        if key in self._d:
            return True
        self._d[key] = None
        if len(self._d) > self._maxlen:
            self._d.popitem(last=False)
        return False


# ---------------------------------------------------------------------------
# Slash command registry
# ---------------------------------------------------------------------------

# Handler signature: async def handler(orch, caller_id, args, respond) -> None
SlashHandler = Callable[["GatewayOrchestrator", str, str, Callable], Coroutine[Any, Any, None]]

SLASH_REGISTRY: dict[str, tuple[SlashHandler, str]] = {}


def register_slash_command(name: str, handler: SlashHandler, description: str = "") -> None:
    """Register a sub-command for ``/kirocrew <name>``."""
    SLASH_REGISTRY[name] = (handler, description)


def _build_help_text(cmd_name: str = "kirocrew") -> str:
    """Build help message listing all registered sub-commands."""
    lines = ["*Available commands:*"]
    for name, (_, desc) in sorted(SLASH_REGISTRY.items()):
        lines.append(f"• `/{cmd_name} {name}` — {desc}" if desc else f"• `/{cmd_name} {name}`")
    lines.append(f"• `/{cmd_name} #channel` — track/untrack channel")
    return "\n".join(lines)


def _safe_log(text: str) -> str:
    """Sanitize free-form, user-controlled Slack text before logging.

    Strips CR/LF/tab to prevent log-forging (CWE-117) then redacts exfil URLs
    and credentials so customer prompt content isn't written verbatim to the
    app log (CWE-532). Mirrors the redaction the rest of this module already
    applies to other logged content.
    """
    if not text:
        return text
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


# -------------------------------------------------------------------------
# Socket Mode setup
# -------------------------------------------------------------------------


def init_socket_mode(orch: GatewayOrchestrator, seen: SeenCache) -> None:
    """Wire up the Socket Mode client and attach the event listener.

    Does nothing when Slack is disabled (missing tokens or no allowed
    users).  Mutates ``orch._socket_client`` in place.
    """
    # Lazy sibling imports (deferred to avoid a module-level cycle: the
    # ``events_*`` submodules import shared primitives from this module).
    # Importing ``events_slash`` here also fires its slash-command
    # registrations against ``SLASH_REGISTRY``.
    from kiro_crew.slack import events_hometab, events_message, events_slash

    if not orch._slack_enabled:
        return

    if not orch._owner_id:
        logger.error("KIROCREW_OWNER_ID is not set — Slack disabled for security")
        orch._slack_enabled = False
        orch.slack = None
        return

    # Invariant: _allowed_users contains only the owner (multi-user disabled)
    assert orch._owner_id, "owner_id must be set"

    # Share owner-only allowlist and tracking channels with handler modules
    set_allowed_users(orch._allowed_users)
    set_tracking_channels(orch._tracking_channels)
    set_open_channels(orch._open_channels)
    set_owner_id(orch._owner_id)
    if orch._cfg.agent.yolo:
        set_yolo_mode(True)
    set_orch_cfg(orch._cfg)
    if orch.dashboard_state:
        set_dashboard_state(orch.dashboard_state)

    # ── Enterprise Grid workspace validation ──
    # Blocks data exfiltration via personal/external Slack workspaces.
    extra_ids = orch._cfg.slack_enterprise_ids
    # Route through the active PlatformContext's Slack enterprise gate.  The
    # Default gate is open (opt-in allowlist), identical to today; the Amazon
    # companion supplies a fail-closed workspace allowlist.
    if not current_context().slack_gate.validate_enterprise(orch._bot_token, extra_ids=extra_ids):
        logger.error("Slack workspace failed enterprise validation — Slack disabled")
        orch._slack_enabled = False
        orch.slack = None
        return

    web_client = AsyncWebClient(token=orch._bot_token)
    orch._socket_client = WSSocketModeClient(
        app_token=orch._app_token,
        web_client=web_client,
    )

    async def _on_event(client: WSSocketModeClient, req: SocketModeRequest) -> None:
        # Always ack immediately so Slack doesn't retry
        try:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            logger.debug("Failed to ack event (WebSocket not ready), skipping")
            return

        if req.type == "interactive":
            t = asyncio.create_task(dispatch_interactive(req.payload or {}))
            orch._handler_tasks.add(t)
            t.add_done_callback(orch._handler_tasks.discard)
            return

        if req.type == "slash_commands":
            payload = req.payload or {}
            t = asyncio.create_task(events_slash._handle_slash(orch, payload))
            orch._handler_tasks.add(t)
            t.add_done_callback(orch._handler_tasks.discard)
            return

        if req.type != "events_api":
            return

        event = (req.payload or {}).get("event", {})
        event_type = event.get("type")

        # ── Tracking-channel join → allowlist prompt ──
        if event_type == "member_joined_channel":
            events_slash._maybe_prompt_owner(orch, event)
            return

        # ── Home Tab ──
        if event_type == "app_home_opened":
            user = event.get("user")
            if event.get("tab") == "home" and user:
                if is_allowed_user(user):
                    sel().log_api_access(
                        caller=user,
                        operation="slack.home_tab",
                        outcome="allowed",
                        source="slack",
                    )
                    task = asyncio.ensure_future(events_hometab._publish_home_tab(orch, user))
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                else:
                    sel().log_api_access(
                        caller=user,
                        operation="slack.home_tab",
                        outcome="denied",
                        source="slack",
                        error="unauthorized sender",
                    )
            return

        # ── Messages and mentions ──
        if event_type not in ("message", "app_mention"):
            return
        _bot_id = event.get("bot_id")
        _subtype = event.get("subtype")
        # ── message_deleted: cancel queued or in-flight messages ──
        if _subtype == "message_deleted":
            await events_message._handle_message_deleted(orch, event)
            return
        if _subtype and _subtype != "file_share":
            return
        if _bot_id:  # pragma: no cover — socket mode callback, tested via integration
            sel().log_api_access(
                caller=_bot_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                error="untrusted_bot",
            )
            return

        # Enterprise Grid: envelope team_id is the *bot's* workspace;
        # event["team"] may be the *sender's* workspace in shared channels.
        # Always prefer envelope to prevent cross-workspace bypass.
        outer_team = (req.payload or {}).get("team_id", "")
        if outer_team:
            event["team"] = outer_team
        elif not event.get("team"):
            logger.warning(
                "Enterprise Grid: no team_id from event or envelope " "(sender=%s) — rejecting",
                event.get("user", "unknown"),
            )
            sel().log_api_access(
                caller=event.get("user", "unknown"),
                operation="slack.message",
                outcome="denied",
                source="slack",
                error="missing_team_id",
            )
            return

        await events_message._route_message(
            orch,
            event,
            seen,
            is_mention=(event_type == "app_mention"),
            from_trusted_bot=False,
        )

    orch._socket_client.socket_mode_request_listeners.append(_on_event)  # type: ignore[arg-type]
