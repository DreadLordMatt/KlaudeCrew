"""Slack as an output channel — the pin board.

This is the half of the modeled workflow that previously existed only on paper.
Their ops channel WAS the dashboard: one message per incident, its emoji tracking
state, so anyone could read the room's health without opening a tool. That is what
this reproduces.

**No new credential.** This deliberately does NOT add a bot token to the app's
secret store. KiroCrew already holds one for its Slack gateway, and the live
``SlackClientOps`` is reachable in-process off gateway state — so this reuses it and
introduces zero new secret material, no second rotation obligation, and no second
copy to leak. Governance guidance on credential storage puts "prefer no secret to
rotate" first and permits a stored third-party token only where no such path
exists; here one does. The consequence is a real constraint, not a shortcut: if the
operator has not configured Slack for KiroCrew itself, this channel is simply
unavailable, and ``configured()`` says so rather than prompting for a token.

**One message per incident, edited in place.** The pin board is only readable if an
incident occupies ONE line that changes, not a stream of updates. So the first post
records ``slack_thread_ts`` on the incident and every later state change is a
``chat_update`` of that same message; detail (diagnosis, resolution) goes into the
thread beneath it so the top line stays scannable. If the ts is lost, we post fresh
rather than going silent — a duplicate line is a cosmetic problem, a missing alarm
is not.

**Failure is never fatal to a cycle.** Slack being down must not stop the agent from
investigating, so every send is wrapped: failures are logged and reported, and the
dispatch cycle proceeds. Notifying is not the work.

**Everything outbound is redacted.** Incident titles and diagnoses are model- and
provider-derived text heading to a channel with a different (usually wider)
audience than the dashboard, so it passes through ``security.redact`` first. A
credential that reached a provider's alarm description must not be republished into
Slack by us.

See ``docs/system-specs/modules/ops-mission-control.md`` § Slack output.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    STATUS_DISPATCHED,
    STATUS_ESCALATED,
    STATUS_INVESTIGATING,
    STATUS_NEEDS_HUMAN,
    STATUS_RESOLVED,
    STATUS_STALE,
    Incident,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    read_config,
    write_config,
)
from kiro_crew.security import redact

logger = logging.getLogger(__name__)

#: Config keys. Non-secret (a channel id is not a credential), so these live in
#: the plain app config rather than the keystone secret store.
_ENABLED_KEY = "slack_enabled"
_CHANNEL_KEY = "slack_channel"

#: State glyph per status. Emoji is correct HERE and only here: Slack messages are
#: not the dashboard, and `slack/blocks.py` already uses them. The repo's
#: no-emoji rule governs rendered dashboard UI, where Lucide icons are required.
_STATUS_EMOJI: dict[str, str] = {
    STATUS_DISPATCHED: "⏳",
    STATUS_INVESTIGATING: "🔍",
    STATUS_NEEDS_HUMAN: "🧑",
    STATUS_RESOLVED: "✅",
    STATUS_ESCALATED: "🚨",
    STATUS_STALE: "💤",
}
_DEFAULT_EMOJI = "•"

#: Slack hard-limits a text block to 3000 chars; stay well under so a long
#: diagnosis is truncated by us with an ellipsis rather than rejected by the API.
_MAX_DETAIL_CHARS = 2000

#: Cap on the incident title in the one-line summary, so the status and resource
#: stay visible on a narrow client.
_MAX_TITLE_CHARS = 160


def configured() -> bool:
    """True when the operator enabled this channel AND named a destination.

    Does not check that KiroCrew's own Slack client exists — that is a runtime
    condition (it depends on gateway boot), reported by ``status()``.
    """
    cfg = read_config()
    return bool(cfg.get(_ENABLED_KEY)) and bool(str(cfg.get(_CHANNEL_KEY, "")).strip())


def channel() -> str:
    return str(read_config().get(_CHANNEL_KEY, "")).strip()


def set_settings(*, enabled: bool | None = None, channel_id: str | None = None) -> None:
    """Persist the operator's choice. Non-secret, so plain app config."""
    cfg = read_config()
    if enabled is not None:
        cfg[_ENABLED_KEY] = bool(enabled)
    if channel_id is not None:
        cfg[_CHANNEL_KEY] = channel_id.strip()
    write_config(cfg)


def client_from_state(state: Any | None) -> Any | None:
    """Pull the live Slack client off gateway state, tolerating its absence.

    The client is passed in from the route layer (``request.app["state"]``) rather
    than fetched from a module global, because KiroCrew has no global state
    accessor — state is per-application. That makes the dependency explicit and
    lets every send be tested without a gateway.
    """
    return getattr(state, "slack_client", None) if state is not None else None


def status(client: Any | None = None) -> dict[str, Any]:
    """Why this channel is or is not usable — surfaced in Settings.

    Distinguishes the three failure modes, because they need three different
    fixes: not enabled (flip the toggle), no channel (name one), and no Slack on
    KiroCrew itself (configure the gateway's Slack integration — this app cannot
    fix that for you, by design, since it holds no token of its own).
    """
    cfg = read_config()
    enabled = bool(cfg.get(_ENABLED_KEY))
    chan = str(cfg.get(_CHANNEL_KEY, "")).strip()
    has_client = client is not None
    if not enabled:
        detail = "Off. Turn on to mirror incidents to a Slack channel."
    elif not chan:
        detail = "No channel set — enter a channel ID (e.g. C0123456789)."
    elif not has_client:
        detail = (
            "KiroCrew's own Slack integration is not connected, so there is "
            "nothing to post with. This app deliberately stores no Slack token "
            "of its own — configure Slack in Settings and it will work here."
        )
    else:
        detail = f"Mirroring incidents to {chan}."
    return {
        "enabled": enabled,
        "channel": chan,
        "slack_available": has_client,
        "ready": enabled and bool(chan) and has_client,
        "detail": detail,
    }


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def summary_line(incident: Incident) -> str:
    """The one line the pin board shows for this incident.

    Redacted: the title comes from a provider payload and may carry anything.
    """
    emoji = _STATUS_EMOJI.get(incident.status, _DEFAULT_EMOJI)
    title = _clip(redact(incident.signal.title), _MAX_TITLE_CHARS)
    parts = [f"{emoji} *{incident.incident_id}* {title}"]

    # The blocked reason, when present, is the actionable half — "Needs human"
    # alone does not tell the reader whether they must click approve or think.
    state = incident.blocked_reason or incident.status
    parts.append(f"_{state.replace('_', ' ')}_")

    if incident.signal.resource:
        parts.append(f"`{_clip(redact(incident.signal.resource), 120)}`")
    return "  ·  ".join(parts)


def _blocks(incident: Incident) -> list[dict]:
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary_line(incident)}}
    ]
    context: list[dict] = [
        {"type": "mrkdwn", "text": f"{incident.signal.source} · {incident.signal.severity}"}
    ]
    if incident.signal.url:
        context.append({"type": "mrkdwn", "text": f"<{incident.signal.url}|open in provider>"})
    if incident.ledger_matches:
        context.append(
            {"type": "mrkdwn", "text": f"{len(incident.ledger_matches)} known pattern(s)"}
        )
    blocks.append({"type": "context", "elements": context})
    return blocks


async def publish(incident: Incident, client: Any | None) -> bool:
    """Create or update this incident's line on the pin board.

    Returns True when Slack was actually written. Never raises: a Slack outage
    must not fail the dispatch cycle that called it.
    """
    if client is None or not configured():
        return False

    chan = channel()
    blocks = _blocks(incident)
    fallback = summary_line(incident)

    # Edit in place when we already own a message — that is what makes this a
    # board rather than a feed.
    if incident.slack_thread_ts:
        try:
            await client.update_message(
                chan, incident.slack_thread_ts, text=fallback, blocks=blocks
            )
            return True
        except Exception as exc:
            # Fall through to a fresh post: the old ts may be gone (message
            # deleted, channel changed). Silence would be the worse outcome.
            logger.warning(
                "ops-mission-control: Slack update failed for %s (%s) — reposting",
                incident.incident_id,
                exc,
            )

    try:
        ts = await client.post_blocks(chan, blocks, fallback)
    except Exception as exc:
        logger.warning(
            "ops-mission-control: Slack post failed for %s: %s", incident.incident_id, exc
        )
        return False

    if ts:
        try:
            store.update_fields(incident.incident_id, slack_thread_ts=str(ts))
        except Exception as exc:  # pragma: no cover - index write already logged
            # We posted but could not record the ts, so the NEXT update will post
            # a duplicate instead of editing. Cosmetic, and worth logging.
            logger.warning(
                "ops-mission-control: posted to Slack but could not record ts for %s: %s",
                incident.incident_id,
                exc,
            )
    return True


async def post_detail(incident: Incident, text: str, client: Any | None) -> bool:
    """Add detail in the incident's thread, keeping the top line scannable.

    Used for a diagnosis or resolution: it belongs with the incident but must not
    push the board's one-line summary out of view.
    """
    if client is None or not configured() or not incident.slack_thread_ts:
        return False
    body = _clip(redact(text), _MAX_DETAIL_CHARS)
    if not body:
        return False
    try:
        await client.post_message(channel(), body, thread_ts=incident.slack_thread_ts)
        return True
    except Exception as exc:
        logger.warning(
            "ops-mission-control: Slack thread post failed for %s: %s",
            incident.incident_id,
            exc,
        )
        return False


async def publish_all(incidents: list[Incident], client: Any | None) -> int:
    """Refresh the board for several incidents. Returns how many were written."""
    if client is None:
        return 0
    written = 0
    for incident in incidents:
        if await publish(incident, client):
            written += 1
    return written
