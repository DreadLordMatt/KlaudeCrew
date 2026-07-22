"""Split from slack/handler.py: privacy_modes cluster."""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import TYPE_CHECKING

from kiro_crew.sel import sel
from kiro_crew.session import SessionManager
from kiro_crew.slack.client import SlackClientOps

if TYPE_CHECKING:
    from kiro_crew.session_map import SessionMap

logger = logging.getLogger(__name__)


# Set via !temporary modifier — thread-scoped temporary (blank-slate) mode.
# Bounded LRU to prevent unbounded growth in long-running bots.
_THREAD_TEMPORARY_MAX = 10_000
_thread_temporary: OrderedDict[str, None] = OrderedDict()


def _mark_temporary(key: str) -> None:
    """Add key to the bounded LRU temporary-thread tracker."""
    _thread_temporary[key] = None
    _thread_temporary.move_to_end(key)
    if len(_thread_temporary) > _THREAD_TEMPORARY_MAX:
        _thread_temporary.popitem(last=False)


def is_thread_temporary(session_key: str) -> bool:
    """Public check — used by API handlers to gate memory writes."""
    return session_key in _thread_temporary


_RESTRICTED_WRITE_MSG = "Memory writes are not allowed in this session mode."


_THREAD_INCOGNITO_MAX = 10_000
_thread_incognito: OrderedDict[str, None] = OrderedDict()


def _mark_incognito(key: str) -> None:
    """Add key to the bounded LRU incognito-thread tracker."""
    _thread_incognito[key] = None
    _thread_incognito.move_to_end(key)
    if len(_thread_incognito) > _THREAD_INCOGNITO_MAX:
        _thread_incognito.popitem(last=False)


def is_thread_incognito(session_key: str) -> bool:
    """Public check — used by API handlers."""
    return session_key in _thread_incognito


def _is_slack_restricted(session_key: str) -> bool:
    """Return True if this Slack session should skip memory writes."""
    return session_key in _thread_temporary or session_key in _thread_incognito


def _conv_state_map(sessions: object) -> "SessionMap | None":
    """Return the SessionManager's canonical SessionMap, or None.

    v1c-B: the per-conversation ``temporary``/``incognito`` flags are persisted
    on the session entry via the SAME ``SessionMap`` instance the
    ``SessionManager`` owns (so writes stay consistent — no second instance can
    clobber them on save). Test doubles without ``_session_map`` return None, in
    which case callers fall back to the in-memory LRU dicts only.
    """
    return getattr(sessions, "_session_map", None)


def _hydrate_conv_flags(sessions: object, session_key: str) -> None:
    """Restore persisted temporary/incognito flags into the in-memory caches.

    Called once per session in ``handle_message`` so a thread marked temporary
    or incognito stays so across a gateway restart (the in-memory LRU is rebuilt
    from the durable ``SessionMap`` entry).
    """
    sm = _conv_state_map(sessions)
    if sm is None:
        return
    if sm.get_flag(session_key, "temporary"):
        _mark_temporary(session_key)
    if sm.get_flag(session_key, "incognito"):
        _mark_incognito(session_key)


_INCOGNITO_TOKEN_RE = re.compile(r"(?<!\S)!incognito(?!\S)", re.IGNORECASE)


def _strip_incognito_token(text: str) -> tuple[str, bool]:
    """Remove standalone ``!incognito`` token from *text*."""
    new, n = _INCOGNITO_TOKEN_RE.subn("", text)
    if not n:
        return text, False
    return " ".join(new.split()), True


_TEMPORARY_TOKEN_RE = re.compile(r"(?<!\S)!temporary(?!\S)", re.IGNORECASE)


def _strip_temporary_token(text: str) -> tuple[str, bool]:
    """Remove standalone ``!temporary`` token from *text*.

    Returns ``(cleaned_text, found)`` where *found* is True if the token
    was present.  The cleaned text has the token removed and excess
    whitespace collapsed.
    """
    new, n = _TEMPORARY_TOKEN_RE.subn("", text)
    if not n:
        return text, False
    return " ".join(new.split()), True


async def _apply_temporary_modifier(
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    reply_ts: str,
) -> None:
    """Mark a session as temporary and notify the user (idempotent)."""
    if session_key in _thread_temporary:
        return
    _mark_temporary(session_key)
    # v1c-B: persist on the session entry (durable across restart).
    _sm = _conv_state_map(sessions)
    if _sm is not None:
        _sm.set_flag(session_key, "temporary", True)
    sel().log_api_access(
        caller=user_id,
        operation="slack.temporary_mode",
        outcome="allowed",
        source="slack",
        resources=f"{channel}:{session_key}",
    )
    # Register thread so follow-up messages pass the in_active_thread
    # gate in mention/observe channels without needing another @mention.
    # reply_ts is the bare Slack thread_ts; session_key may be namespaced.
    sessions.set_slack_link(session_key, reply_ts, channel)
    await slack.post_message(
        channel,
        "🔒 Temporary mode ON — this thread won't read or save memory.",
        reply_ts,
    )


async def _apply_incognito_modifier(
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    reply_ts: str,
) -> None:
    """Mark a session as incognito and notify the user (idempotent)."""
    if session_key in _thread_incognito:
        return
    _mark_incognito(session_key)
    # v1c-B: persist on the session entry (durable across restart).
    _sm = _conv_state_map(sessions)
    if _sm is not None:
        _sm.set_flag(session_key, "incognito", True)
    sel().log_api_access(
        caller=user_id,
        operation="slack.incognito_mode",
        outcome="allowed",
        source="slack",
        resources=f"{channel}:{session_key}",
    )
    # reply_ts is the bare Slack thread_ts; session_key may be namespaced.
    sessions.set_slack_link(session_key, reply_ts, channel)
    await slack.post_message(
        channel,
        "🕶️ Incognito mode ON — this thread can read memory but won't save anything.",
        reply_ts,
    )


async def maybe_apply_privacy_modifiers(
    text: str,
    cmd_text: str,
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    reply_ts: str,
) -> tuple[str, str, bool]:
    """Strip and apply the ``!temporary`` / ``!incognito`` privacy modifiers.

    Shared by the native ``handle_message`` path and the messaging-transport
    ``handle_message_transport`` path so the privacy controls behave identically
    on both (and the modifier token never leaks into the LLM prompt).

    Returns ``(text, cmd_text, only_modifier)``:
    - *text* — the LLM-facing message with the modifier token(s) removed.
    - *cmd_text* — the mention-stripped command text with the token removed
      (the native path reuses it for its subsequent ``!compact``/``!bang``
      checks; the transport path ignores it).
    - *only_modifier* — True when the message was nothing but the modifier(s);
      the caller MUST then return without starting an LLM turn.

    Mirrors native's original inline ordering exactly, including the early
    return between ``!temporary`` and ``!incognito`` when nothing remains.
    """
    cmd_stripped, had_temporary = _strip_temporary_token(cmd_text)
    if had_temporary:
        await _apply_temporary_modifier(session_key, user_id, channel, slack, sessions, reply_ts)
        cmd_text = cmd_stripped
        text = _TEMPORARY_TOKEN_RE.sub("", text)
        text = " ".join(text.split()) or text  # collapse whitespace
        if not cmd_text:
            # Message was *only* "!temporary" with no remaining content.
            return text, cmd_text, True

    cmd_stripped, had_incognito = _strip_incognito_token(cmd_text)
    if had_incognito:
        await _apply_incognito_modifier(session_key, user_id, channel, slack, sessions, reply_ts)
        cmd_text = cmd_stripped
        text = _INCOGNITO_TOKEN_RE.sub("", text)
        text = " ".join(text.split()) or text
        if not cmd_text:
            return text, cmd_text, True

    return text, cmd_text, False
