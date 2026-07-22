"""KiroCrew Slack message routing, dedup, transcription, and dispatch.

Validates, deduplicates, applies channel-activation rules, transcribes voice
memos, recovers forwarded-message text, and dispatches incoming Slack messages
to the native or transport handler. ``_route_message`` delegates the
``!restart`` bang alias to :mod:`kiro_crew.slack.events_slash`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import (
    ACTIVATION_MENTION,
    ACTIVATION_OBSERVE,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    KiroCrewConfig,
)
from kiro_crew.platform import current_context
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
    should_record_observe_history,
)
from kiro_crew.sel import sel
from kiro_crew.slack import events_slash
from kiro_crew.slack.blocks import build_stopping_blocks
from kiro_crew.slack.events_core import _safe_log
from kiro_crew.slack.files import process_slack_files
from kiro_crew.slack.handler import (
    APPROVAL_AUTO,
    APPROVAL_INTERACTIVE,
    handle_message,
    is_allowed_user,
    is_owner,
    is_yolo_mode,
)
from kiro_crew.slack.transport_dispatch import handle_message_transport
from kiro_crew.transcribe import is_available as stt_available
from kiro_crew.transcribe import transcribe_audio

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps
    from kiro_crew.slack.events_core import SeenCache
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audio transcription helpers
# ---------------------------------------------------------------------------


_AUDIO_MIMETYPES = {"audio/", "video/webm"}


async def _transcribe_with_reaction(
    slack_client: "SlackClientOps",
    channel: str,
    msg_ts: str,
    orch: "GatewayOrchestrator",
    files: list[dict],
) -> list[str]:
    """Transcribe audio files with a reaction indicator for user feedback."""
    _stt_reaction_added = False
    try:
        await slack_client.add_reaction(channel, msg_ts, "studio_microphone")
        _stt_reaction_added = True
    except Exception:
        logger.debug("Failed to add STT reaction", exc_info=True)

    try:
        transcripts = await _transcribe_files(orch, files)
    finally:
        if _stt_reaction_added:
            try:
                await slack_client.remove_reaction(
                    channel,
                    msg_ts,
                    "studio_microphone",
                )
            except Exception:
                logger.debug("Failed to remove STT reaction", exc_info=True)
    return transcripts


async def _transcribe_files(orch: "GatewayOrchestrator", files: list[dict]) -> list[str]:
    """Download and transcribe audio files, return list of transcription strings."""
    results: list[str] = []
    for f in files:
        mimetype = f.get("mimetype", "")
        if not any(mimetype.startswith(prefix) for prefix in _AUDIO_MIMETYPES):
            continue
        url = f.get("url_private_download") or f.get("url_private", "")
        if not url:
            continue
        dest: str | None = None
        try:
            raw_ft = re.sub(r"[^a-zA-Z0-9]", "", f.get("filetype", "webm"))
            suffix = "." + (raw_ft or "webm")
            fd, dest = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            assert orch.slack is not None
            assert dest is not None
            await orch.slack.download_file(url, dest)
            sel().log_api_access(
                caller="stt",
                operation="slack.download_file",
                outcome="success",
                source="transcribe",
                resources=f.get("name", "?"),
            )
            transcript = await transcribe_audio(dest)
            sel().log_api_access(
                caller="stt",
                operation="whisper.transcribe",
                outcome="success" if transcript else "empty",
                source="transcribe",
                resources=f.get("name", "?"),
            )
            if transcript:
                results.append(transcript)
                logger.info("Transcribed voice memo: %d chars", len(transcript))
            else:
                logger.warning("Transcription returned empty for %s", f.get("name", "?"))
        except Exception:
            logger.exception("Failed to transcribe file %s", f.get("name", "?"))
            sel().log_api_access(
                caller="stt",
                operation="whisper.transcribe",
                outcome="error",
                source="transcribe",
                resources=f.get("name", "?"),
                error="transcription_failed",
            )
        finally:
            if dest:
                try:
                    os.unlink(dest)
                except OSError:
                    pass
    return results


# -------------------------------------------------------------------------
# Message routing
# -------------------------------------------------------------------------


async def _handle_message_deleted(orch: GatewayOrchestrator, event: dict) -> None:
    """Handle message_deleted subtype — cancel queued or in-flight messages."""
    deleted_ts = event.get("deleted_ts")
    _del_thread_ts = event.get("previous_message", {}).get("thread_ts")
    _del_channel = event.get("channel", "")
    _del_user = event.get("previous_message", {}).get("user", "")
    if deleted_ts and _del_channel and is_allowed_user(_del_user):
        _del_session_key = _del_thread_ts or deleted_ts
        was_queued = False
        if orch.sessions:
            was_queued = orch.sessions.cancel_queued(_del_session_key, deleted_ts)
        if not was_queued:
            _pq = orch._pending_queue.get(_del_session_key, [])
            _filtered = [item for item in _pq if item[0] != deleted_ts]
            if len(_filtered) < len(_pq):
                was_queued = True
                if _filtered:
                    orch._pending_queue[_del_session_key] = _filtered
                else:
                    orch._pending_queue.pop(_del_session_key, None)
        if was_queued:
            logger.info(
                "message_deleted: ts=%s session=%s queued=%s",
                deleted_ts,
                _del_session_key,
                was_queued,
            )
        sel().log_api_access(
            caller=event.get("previous_message", {}).get("user", "unknown"),
            operation="slack.message_deleted",
            outcome="allowed",
            source="slack",
            resources=f"ts={deleted_ts} session={_del_session_key} queued={was_queued}",
        )


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Slack dispatch approval mode: CLI --approval flag wins, else config.

    Normalized to handle_message's auto/interactive contract; reads/yolo are
    gated separately (gateway approval-event path, global YOLO/trust).
    """
    # Runtime YOLO (owner-toggled via /meshclaw yolo, TTL-capped safety_override)
    # auto-approves all tools. The native loop checks is_yolo_mode() inline; the
    # transport TurnDriver only sees this resolved mode, so fold YOLO in here at
    # the single per-message chokepoint (evaluated fresh each message) — both
    # paths then honor the runtime toggle consistently.
    if is_yolo_mode():
        return APPROVAL_AUTO
    mode = orch._approval_mode or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def _dispatch_queued(
    orch: GatewayOrchestrator,
    session_key: str,
    msg_ts: str,
    text: str,
    kwargs: dict,
) -> None:
    """Dispatch a queued message — remove ⏳ reaction and call handle_message."""
    channel = kwargs.get("channel", "")
    thread_ts = kwargs.get("thread_ts")
    if orch.slack:
        try:
            await orch.slack.remove_reaction(channel, msg_ts, "hourglass_flowing_sand")
        except Exception:
            pass
    # Route the queued follow-up through the SAME gate as the initial message so
    # behavior is consistent mid-conversation: a thread that took the transport
    # path must keep taking it for its queued follow-ups (not silently fall back
    # to native). Review-mode channels stay on native (privacy gate), matching
    # the _route_message gate.
    _activation = orch._cfg.channel_config(channel).activation
    _use_transport = (
        getattr(getattr(orch._cfg, "messaging", None), "use_transport", False) is True
        and _activation != ACTIVATION_REVIEW
    )
    try:
        if _use_transport:
            await handle_message_transport(
                orch.slack,  # type: ignore[arg-type]
                orch.sessions,  # type: ignore[arg-type]
                channel,
                text,
                thread_ts,
                msg_ts,
                kwargs.get("sender_id", ""),
                context_builder=orch.ctx_builder,
                conversation_log=orch.conv_log,
                approval_mode=_resolve_approval_mode(orch),
                agent_override=kwargs.get("agent_override"),
                subagent_manager=orch.subagent_mgr,
                task_runner=orch.task_runner,
                cron_service=orch.cron_svc,
                # Live-read per message (parity with native handle_message, which
                # loads config at handler.py:2661/2683): orch._cfg is captured at
                # startup, so reading it here would make settings-UI toggle saves
                # silently inert until restart.
                reactions_enabled=KiroCrewConfig.load().slack.reactions_enabled,
                show_thinking=KiroCrewConfig.load().slack.show_thinking,
                consolidator=orch.consolidator,
                user_display_name=kwargs.get("user_display_name"),
            )
            return
        await handle_message(
            orch.slack,  # type: ignore[arg-type]
            orch.sessions,  # type: ignore[arg-type]
            channel,
            text,
            thread_ts,
            msg_ts,
            kwargs.get("sender_id", ""),
            team_id=kwargs.get("team_id", ""),
            approval_mode=_resolve_approval_mode(orch),
            context_builder=orch.ctx_builder,
            cron_service=orch.cron_svc,
            conversation_log=orch.conv_log,
            consolidator=orch.consolidator,
            subagent_manager=orch.subagent_mgr,
            task_runner=orch.task_runner,
            channel_agent=kwargs.get("agent_override"),
            user_display_name=kwargs.get("user_display_name"),
        )
    finally:
        # The enqueue path deferred temp-image cleanup to here so the queued
        # turn's text could still resolve its image paths (see _route_message).
        # Unlink them now that the turn has consumed them — in finally so a
        # raising turn can't leak the temp files.
        for _p in kwargs.get("image_temp_paths") or []:
            try:
                os.unlink(_p)
            except OSError:
                pass


# Maximum characters to recover from block extraction (DoS guard).
# Slack message bodies can be ~40k; 16k is a safe recovery cap for downstream processing.
_MAX_RECOVERED_TEXT_CHARS = 16000


def _render_rich_text_element(el: dict) -> str:
    """Render a single rich_text inline element to plain text.

    Handles all documented Slack rich_text element types:
    text, link, emoji, user, usergroup, channel, broadcast, date.
    """
    if not isinstance(el, dict):
        return ""
    el_type = el.get("type")
    if el_type == "text":
        return el.get("text", "")
    if el_type == "link":
        # link: show "text (url)" if both present; else whichever exists
        text = el.get("text", "")
        url = el.get("url", "")
        if text and url:
            return f"{text} ({url})"
        return text or url
    if el_type == "emoji":
        # emoji: use :name: format; fall back to unicode if name missing
        name = el.get("name")
        if name:
            return f":{name}:"
        return el.get("unicode", "")
    if el_type == "user":
        user_id = el.get("user_id", "")
        return f"<@{user_id}>" if user_id else ""
    if el_type == "usergroup":
        usergroup_id = el.get("usergroup_id", "")
        return f"<!subteam^{usergroup_id}>" if usergroup_id else ""
    if el_type == "channel":
        channel_id = el.get("channel_id", "")
        return f"<#{channel_id}>" if channel_id else ""
    if el_type == "broadcast":
        # broadcast range: here, channel, or everyone
        range_val = el.get("range", "")
        return f"<!{range_val}>" if range_val else ""
    if el_type == "date":
        # date: use fallback text if present (human-readable rendering)
        return el.get("fallback", "")
    # Unknown element type — attempt text field, log for observability
    logger.debug("Unknown rich_text element type=%r, attempting text field", el_type)
    return el.get("text", "")


def _extract_blocks_text(blocks: list[dict]) -> str:
    """Extract readable text from Block Kit blocks (rich_text, section, context).

    Handles the common block types Slack uses for user messages and shared
    content.  Returns empty string if no text can be recovered.
    Defensive: never raises on malformed input.
    """
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "rich_text":
            elements = block.get("elements", [])
            if not isinstance(elements, list):
                elements = []
            for element in elements:
                if not isinstance(element, dict):
                    continue
                el_type = element.get("type")
                child_els = element.get("elements", [])
                if not isinstance(child_els, list):
                    child_els = []
                if el_type == "rich_text_list":
                    # Prefix each list item with "- " to preserve list structure.
                    # (Numbered vs bulleted distinction not preserved — simple bullet.)
                    for child in child_els:
                        if not isinstance(child, dict):
                            continue
                        sub_els = child.get("elements", [])
                        if not isinstance(sub_els, list):
                            sub_els = []
                        inline = "".join(
                            _render_rich_text_element(el) for el in sub_els
                        )
                        if inline:
                            parts.append(f"- {inline}")
                elif el_type == "rich_text_quote":
                    # Quote blocks: prefix with "> "
                    inline = "".join(
                        _render_rich_text_element(el) for el in child_els
                    )
                    if inline:
                        parts.append(f"> {inline}")
                else:
                    # rich_text_section, rich_text_preformatted
                    inline = "".join(
                        _render_rich_text_element(el) for el in child_els
                    )
                    if inline:
                        parts.append(inline)
        elif block_type == "section":
            text_obj = block.get("text")
            if isinstance(text_obj, dict):
                section_text = text_obj.get("text", "")
                if section_text:
                    parts.append(section_text)
        elif block_type == "context":
            ctx_elements = block.get("elements", [])
            if not isinstance(ctx_elements, list):
                ctx_elements = []
            for el in ctx_elements:
                if not isinstance(el, dict):
                    continue
                ctx_text = el.get("text", "")
                if ctx_text:
                    parts.append(ctx_text)
    result = "\n".join(parts).strip()
    if not result:
        return ""
    return result[:_MAX_RECOVERED_TEXT_CHARS]


# Slack's generic fallback strings for messages whose content lives in blocks.
# NOTE: These are best-effort, undocumented, English-only Slack placeholder strings.
# They may change or be localized — recovery is best-effort for non-English workspaces.
# No fuzzy/structural detection is attempted (out of scope; would change behavior broadly).
_SLACK_BLOCK_FALLBACKS = frozenset({
    "This message contains interactive elements.",
    "This content can't be displayed.",
})


def _normalize_message_blocks(raw: list) -> list[dict]:
    """Drill into the Slack message_blocks wrapper structure.

    ``message_blocks`` is a wrapper list:
    ``[{"team":..., "channel":..., "ts":..., "message": {"blocks": [...]}}]``
    This extracts and flattens the inner blocks from each wrapper.
    """
    result: list[dict] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict):
            continue
        msg = item.get("message")
        if isinstance(msg, dict):
            inner_blocks = msg.get("blocks")
            if isinstance(inner_blocks, list):
                result.extend(b for b in inner_blocks if isinstance(b, dict))
    return result


def _extract_shared_text(event: dict) -> str:
    """Recover message text from forwarded-message attachments.

    Slack forwards carry their content in the ``attachments`` array (entries
    flagged ``is_share`` / ``is_msg_unfurl``), not in the top-level ``text``
    field. Link-unfurl attachments are excluded so pasted URLs don't leak
    preview text into the routed message body.

    When the attachment's ``text`` is empty and ``fallback`` is a generic
    Slack placeholder (e.g. "This message contains interactive elements."),
    attempts to reconstruct content from the attachment's ``blocks`` or the
    event-level ``blocks`` array.
    """
    attachments = event.get("attachments") or []
    parts: list[str] = []
    for att in attachments:
        if not (att.get("is_share") or att.get("is_msg_unfurl")):
            continue
        att_text = att.get("text") or ""
        if att_text:
            parts.append(att_text)
            continue
        # text is empty — try blocks before falling back to the generic fallback.
        # att["blocks"] is already a flat block list; att["message_blocks"] is a
        # wrapper list that must be normalized first.
        att_blocks = att.get("blocks")
        if isinstance(att_blocks, list) and att_blocks:
            extracted = _extract_blocks_text(att_blocks)
            if extracted:
                parts.append(extracted)
                continue
        msg_blocks = att.get("message_blocks")
        if msg_blocks:
            normalized = _normalize_message_blocks(msg_blocks)
            if normalized:
                extracted = _extract_blocks_text(normalized)
                if extracted:
                    parts.append(extracted)
                    continue
        # Last resort: use fallback unless it's a generic Slack placeholder
        fallback = att.get("fallback") or ""
        if fallback and fallback not in _SLACK_BLOCK_FALLBACKS:
            parts.append(fallback)
    # If attachments yielded nothing, try event-level blocks (Slack sometimes
    # puts the real content there for shared messages).
    if not parts:
        event_blocks = event.get("blocks") or []
        if event_blocks:
            extracted = _extract_blocks_text(event_blocks)
            if extracted:
                return extracted
    return "\n\n".join(part for part in parts if part).strip()


async def _route_message(
    orch: GatewayOrchestrator,
    event: dict,
    seen: SeenCache,
    is_mention: bool = False,
    from_trusted_bot: bool = False,
) -> None:
    """Validate, dedup, check activation mode, and dispatch an incoming Slack message."""
    sender_id = event.get("user", "") or (event.get("bot_id", "") if from_trusted_bot else "")
    channel = event.get("channel", "")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts")
    msg_ts = event.get("ts", "")
    team_id = event.get("team", "")
    files = event.get("files", [])

    # Slack forwards carry content in attachments, not text — recover it so the
    # forward isn't silently dropped by the (not text and not files) guard below.
    # Also recover when Slack sets text to a generic Block Kit fallback placeholder.
    if not text or text in _SLACK_BLOCK_FALLBACKS:
        fallback = "" if text in _SLACK_BLOCK_FALLBACKS else text
        text = _extract_shared_text(event) or fallback

    logger.debug("Stream debug: team_id=%s user_id=%s channel=%s", team_id, sender_id, channel)

    if not sender_id or not channel or (not text and not files):
        return

    # ── Enterprise origin check: reject messages from swapped tokens ──
    # Per-message gate via the active PlatformContext (default-open; Amazon
    # companion fail-closed).
    if not current_context().slack_gate.check_message_origin(team_id):
        logger.error("Message rejected: team_id=%s does not match validated workspace", team_id)
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="denied",
            source="slack",
            resources=f"team_id={team_id} channel={channel}",
            error="enterprise_origin_mismatch",
        )
        return

    # ── Workspace routing cache for org-wide installs ──
    # Slack Web API calls (chat.postMessage, chat.startStream, etc.) need
    # team_id when the bot is org-wide installed; record the channel→team
    # mapping so outbound posts on this channel route to the correct
    # workspace and avoid ``team_access_not_granted``.
    record_team = getattr(orch.slack, "record_channel_team", None)
    if record_team and team_id:
        record_team(channel, team_id)

    # ── Access control: record authorization decision early for SEL audit ──
    # The ephemeral rejection is deferred until after activation checks so
    # users in observe/mention channels aren't spammed, but the SEL event
    # is always emitted to preserve the audit trail.
    _user_authorized = is_allowed_user(sender_id)
    if _user_authorized:
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="allowed",
            source="slack",
        )
    else:
        logger.warning("Ignoring message from unauthorized user %s", sender_id)
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="denied",
            source="slack",
            error="unauthorized sender",
        )

    # ── Channel activation mode (checked BEFORE ephemeral & dedup) ──
    # When activation=mention, Slack sends both a `message` and an
    # `app_mention` event for the same msg_ts.  We must skip the plain
    # `message` event *without* marking it as seen so the subsequent
    # `app_mention` event is still processed.
    ch_cfg = orch._cfg.channel_config(channel)
    activation = ch_cfg.activation

    if activation == ACTIVATION_OFF:
        # Allow !channel commands through so the owner can re-enable the channel.
        # Text may start with "<@BOTID> " when @mentioned, so strip that first.
        _stripped = text.lstrip()
        if _stripped.startswith("<@"):
            end = _stripped.find(">")
            if end != -1:
                _stripped = _stripped[end + 1 :].lstrip()
        if not _stripped.startswith("!channel"):
            logger.debug("Channel %s activation=off — ignoring message", channel)
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                resources=channel,
                error="activation=off",
            )
            return

    # Resolve sender's Slack display name so the LLM uses the actual
    # profile name instead of guessing from memory. Cached on channel
    # history (for history context) and passed to handle_message.
    _sender_display: str | None = None
    if orch.channel_history:
        _sender_display = orch.channel_history._user_names.get(sender_id)
    if not _sender_display and orch.slack and hasattr(orch.slack, "get_user_info"):
        try:
            info = await orch.slack.get_user_info(sender_id)
            _sender_display = info.get("real_name") or sender_id
            if orch.channel_history:
                orch.channel_history.set_user_name(sender_id, _sender_display)
        except Exception:
            logger.debug("Failed to resolve display name for %s", sender_id, exc_info=True)

    # Fallback: if display name is still the raw Slack ID, resolve from
    # allowed_users config (works even without Slack users:read scope).
    if (not _sender_display or _sender_display == sender_id) and hasattr(orch, "_cfg"):
        for u in getattr(orch._cfg.slack, "allowed_users", []):
            if u.get("slack_id") == sender_id and u.get("name"):
                _sender_display = u["name"]
                if orch.channel_history:
                    orch.channel_history.set_user_name(sender_id, u["name"])
                break

    # Observe mode: record history from authorized users only.
    # Previously recorded all channel traffic, but non-owner messages
    # could influence LLM context (Shepherd bdd39e84).
    if activation == ACTIVATION_OBSERVE:
        if should_record_observe_history(orch.channel_history, _user_authorized):
            assert orch.channel_history is not None  # narrowed by helper
            orch.channel_history.push(channel, sender_id, text, thread_ts=thread_ts, msg_ts=msg_ts)
        if not is_mention:
            in_active_thread = (
                ch_cfg.thread_follow
                and thread_ts
                and orch.sessions
                and (
                    orch.sessions.has_session(thread_ts)
                    or orch.sessions.get_session_for_thread(thread_ts)
                    or (orch.conv_log and orch.conv_log.has_log(thread_ts))
                )
            )
            if not in_active_thread:
                sel().log_api_access(
                    caller=sender_id,
                    operation="slack.message",
                    outcome="denied",
                    source="slack",
                    resources=channel,
                    error="activation=observe, no mention or active thread",
                )
                return

    if activation in (ACTIVATION_MENTION, ACTIVATION_REVIEW) and not is_mention:
        # In mention/review mode: ignore messages without @mention UNLESS the
        # message is a reply in a thread where the bot already has an active
        # session (i.e., the bot was previously @mentioned in that thread).
        # When thread_follow=false, always require @mention even in active threads.
        in_active_thread = (
            ch_cfg.thread_follow
            and thread_ts
            and orch.sessions
            and (
                orch.sessions.has_session(thread_ts)
                or orch.sessions.get_session_for_thread(thread_ts)
                or (orch.conv_log and orch.conv_log.has_log(thread_ts))
            )
        )
        if not in_active_thread:
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                resources=channel,
                error=f"activation={activation}, no mention or active thread",
            )
            return

    # ── Access control: send ephemeral rejection ──
    # Only reached for messages the bot would actually respond to,
    # preventing notification spam in observe/mention channels.
    if not _user_authorized:
        if orch.slack:
            try:
                await orch.slack.post_ephemeral(
                    channel,
                    sender_id,
                    "⛔ You are not authorized to use this bot. "
                    "Ask the owner to add you to the allowlist.",
                )
            except Exception:
                logger.debug("Failed to send ephemeral rejection", exc_info=True)
        return

    # Dedup AFTER activation check — prevents the plain `message` event
    # from poisoning the cache before the `app_mention` event arrives.
    if seen.check_and_add(msg_ts):
        return

    # ── Transcribe audio files (voice memos) ──
    # Placed after dedup + auth to avoid expensive work on duplicate events
    # or unauthorized users.
    _image_temp_paths: list[str] = []
    _had_voice_input = False
    if files and orch.slack and _user_authorized:
        if stt_available():
            transcripts = await _transcribe_with_reaction(
                orch.slack,
                channel,
                msg_ts,
                orch,
                files,
            )
            if transcripts:
                raw = "\n".join(transcripts)
                raw, _ = redact_exfiltration_urls(raw)
                raw, _ = redact_credentials(raw)
                prefix = f"[Voice memo transcription]\n{raw}\n[End of transcription]"
                text = f"{prefix}\n\n{text}" if text else prefix
                _had_voice_input = True

        # ── Process non-audio files (images, text, etc.) ──
        image_paths, text_blocks = await process_slack_files(orch, files)
        _image_temp_paths = image_paths

        # Inject image paths so AcpClient._send_prompt() inlines them as base64
        if image_paths:
            paths_text = "\n".join(image_paths)
            text = f"{text}\n{paths_text}" if text else paths_text

        # Inject text file contents
        if text_blocks:
            blocks_text = "\n\n".join(text_blocks)
            text = f"{text}\n\n{blocks_text}" if text else blocks_text

    # Bail out if we still have no text after attempting transcription
    if not text:
        # Clean up any downloaded image temp files
        for p in _image_temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        return

    def _cleanup_image_temps() -> None:
        for p in _image_temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    # Record messages in channel history buffer (observe channels already
    # pushed above, so skip them here to avoid duplicates).
    if activation != ACTIVATION_OBSERVE:
        if orch.channel_history is None:
            logger.error("channel_history not initialised — skipping history push")
        else:
            orch.channel_history.push(channel, sender_id, text, thread_ts=thread_ts, msg_ts=msg_ts)

    # Strip the leading bot @mention so the LLM sees clean text.
    # app_mention events always start with "<@BOTID> ..." — just slice past the first ">".
    clean_text = text
    if is_mention and text.startswith("<@"):
        end = text.find(">")
        if end != -1:
            clean_text = text[end + 1 :].lstrip()
    if not clean_text:
        _cleanup_image_temps()
        return

    # ── !stop: intercept BEFORE handle_message to bypass session semaphore ──
    if clean_text.strip().lower() == "!stop":
        if not (is_owner(sender_id) or is_allowed_user(sender_id)):
            sel().log_api_access(
                caller=sender_id,
                operation="slack.stop_command",
                outcome="denied",
                source="slack",
                resources="!stop",
                error="unauthorized sender",
            )
            if orch.slack:
                await orch.slack.post_message(channel, "⛔ Not authorized.", thread_ts or msg_ts)
            return
        if not orch.sessions:
            sel().log_tool_invocation(
                session_key=thread_ts or msg_ts,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="no_session",
                metadata={"user": sender_id, "channel": channel},
            )
            if orch.slack:
                await orch.slack.post_message(channel, "Nothing running.", thread_ts or msg_ts)
            return
        session_key = thread_ts or msg_ts
        has_session = orch.sessions.has_session(session_key)
        active_task = orch._session_tasks.pop(session_key, None)
        if has_session or active_task:
            orch.sessions.clear_queue(session_key)
            orch._pending_queue.pop(session_key, None)

            # Post ephemeral "Stopping…" block with Kill Now button
            if orch.slack:
                await orch.slack.post_ephemeral(
                    channel,
                    sender_id,
                    "Stopping…",
                    blocks=build_stopping_blocks(session_key),
                    thread_ts=session_key,
                )

            async def _on_soft() -> None:
                if orch.slack:
                    await orch.slack.post_message(channel, "⏹ Execution stopped.", session_key)

            async def _on_hard() -> None:
                if orch.slack:
                    await orch.slack.post_message(
                        channel, "⛔ Execution stopped — session reset.", session_key
                    )

            outcome = await orch.sessions.stop_turn(session_key, on_soft=_on_soft, on_hard=_on_hard)
            if active_task and not active_task.done():
                active_task.cancel()
            # If stop_turn returned "idle" (no active turn), neither callback
            # fired — dismiss the stale "Stopping…" ephemeral explicitly.
            if outcome == "idle" and orch.slack:
                await orch.slack.post_message(channel, "Nothing running.", session_key)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome=outcome,
                metadata={"user": sender_id, "channel": channel},
            )
        else:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="no_session",
                metadata={"user": sender_id, "channel": channel},
            )
            if orch.slack:
                await orch.slack.post_message(channel, "Nothing running.", thread_ts or msg_ts)
        return

    # ── !restart: bang alias for /kirocrew restart — intercept here so it
    #    never reaches the LLM session. Delegates to the slash handler
    #    (_handle_restart) which owns owner-check + supervisor guard, keeping
    #    a single source of truth for the restart logic. ──
    if clean_text.strip().lower() == "!restart":
        async def _restart_respond(text: str, **_kw: Any) -> None:
            if orch.slack:
                await orch.slack.post_message(channel, text, thread_ts or msg_ts)

        await events_slash._handle_restart(orch, sender_id, "", _restart_respond)
        return

    # Per-channel agent override
    agent_override = ch_cfg.agent or None

    # ── NOTE: NO challenge-and-redirect here (intentional) ──────────────────
    # Slack messages are processed INLINE — they fall straight through to the
    # queue/handle_message path below and reach the agent directly. They are
    # gated only by the user allowlist (is_allowed_user, checked earlier).
    #
    # The "challenge-and-redirect" flow (every message intercepted and turned
    # into a presigned dashboard-session link via send_channel_challenge) was
    # an Amazon-internal-only security posture and has been DELIBERATELY REMOVED
    # for external/open-source usage.
    #
    # DO NOT re-introduce it during an upstream (MeshClaw) sync. If a sync
    # surfaces a `_CHALLENGE_REDIRECT_ENABLED` gate or a `send_channel_challenge`
    # call here, DROP that hunk — see skills/meshclaw-sync/SKILL.md.
    logger.info(
        "Message from %s in %s (activation=%s): %s",
        sender_id,
        channel,
        activation,
        _safe_log(text[:80]),
    )

    # ── Queue check: if session is busy, enqueue instead of blocking ──
    session_key = thread_ts or msg_ts
    _task_busy = session_key in orch._session_tasks
    if _task_busy:
        # A task is already running for this session key.  Try the session-level
        # queue first (semaphore-based); fall back to an orchestrator-level
        # pre-session queue when the session object doesn't exist yet.
        _queued = orch.sessions and orch.sessions.enqueue(
            session_key,
            msg_ts,
            clean_text,
            force=True,
            channel=channel,
            thread_ts=thread_ts,
            sender_id=sender_id,
            team_id=team_id,
            agent_override=agent_override,
            user_display_name=_sender_display,
            image_temp_paths=list(_image_temp_paths),
        )
        if not _queued:
            # Session object not created yet — stash on orch._pending_queue
            orch._pending_queue.setdefault(session_key, []).append(
                (
                    msg_ts,
                    clean_text,
                    dict(
                        channel=channel,
                        thread_ts=thread_ts,
                        sender_id=sender_id,
                        team_id=team_id,
                        agent_override=agent_override,
                        user_display_name=_sender_display,
                        image_temp_paths=list(_image_temp_paths),
                    ),
                )
            )
        logger.info(
            "Message %s queued for busy session %s (session_obj=%s)", msg_ts, session_key, _queued
        )
        if orch.slack:
            try:
                await orch.slack.add_reaction(channel, msg_ts, "hourglass_flowing_sand")
            except Exception:
                logger.debug("Failed to add queue reaction", exc_info=True)
        # NOTE: do NOT _cleanup_image_temps() here — clean_text references these
        # temp-file paths and the queued turn hasn't run yet. They are carried in
        # the queue kwargs and unlinked by _dispatch_queued after the turn runs
        # (deleting them now dropped the images silently: p.is_file() was False
        # by dispatch time, so _send_prompt skipped them with no error).
        return
    elif orch.sessions and orch.sessions.enqueue(
        session_key,
        msg_ts,
        clean_text,
        channel=channel,
        thread_ts=thread_ts,
        sender_id=sender_id,
        team_id=team_id,
        agent_override=agent_override,
        user_display_name=_sender_display,
        image_temp_paths=list(_image_temp_paths),
    ):
        logger.info("Message %s queued for busy session %s", msg_ts, session_key)
        if orch.slack:
            try:
                await orch.slack.add_reaction(channel, msg_ts, "hourglass_flowing_sand")
            except Exception:
                logger.debug("Failed to add queue reaction", exc_info=True)
        # See the force=True branch above: cleanup is deferred to
        # _dispatch_queued so the queued turn's clean_text can still resolve
        # its image temp-file paths.
        return

    # ── New transport path: route to the messaging abstraction ──
    # When messaging.use_transport is True, drive the turn through
    # SlackTransport → TurnDriver → SlackRenderer instead of the native
    # inline handle_message loop. Default ON in this fork: MessagingConfig
    # and the loader both default use_transport to True and orch._cfg.messaging
    # is always populated (default_factory), so every install takes this path
    # unless it explicitly sets messaging.use_transport=false to opt back into
    # the native path. (KiroCrew has no challenge-redirect path, so this simply
    # replaces the native dispatch when the flag is on.)
    #
    # Review-mode channels are EXCLUDED from the transport path: review mode is
    # a privacy gate (suppress public streaming/output, post an ephemeral draft
    # with approve/edit/cancel for owner sign-off). That machinery lives only in
    # native handle_message; routing review-mode channels through native keeps
    # that guarantee intact rather than risking a partial re-implementation.
    _use_transport = (
        getattr(getattr(orch._cfg, "messaging", None), "use_transport", False) is True
        and activation != ACTIVATION_REVIEW
    )
    if _use_transport:
        t = asyncio.create_task(
            handle_message_transport(
                orch.slack,  # type: ignore[arg-type]
                orch.sessions,  # type: ignore[arg-type]
                channel,
                clean_text,
                thread_ts,
                msg_ts,
                sender_id,
                context_builder=orch.ctx_builder,
                conversation_log=orch.conv_log,
                # Same approval gating as the native path: respects the
                # configured mode + operator YOLO/SafetyOverride TTL, rather
                # than an unconditional auto-approve. Deny-by-default unless
                # auto-approve is explicitly active.
                approval_mode=_resolve_approval_mode(orch),
                # Per-channel agent override (slack.channels.<id>.agent), same
                # as native handle_message's channel_agent, so a channel-pinned
                # agent is honored on the transport path too.
                agent_override=agent_override,
                # Keyword-command services, same as native handle_message, so
                # `sessions`/`spawn`/`run`/`cron` work on the transport path via
                # the shared maybe_handle_keyword_command interceptor.
                subagent_manager=orch.subagent_mgr,
                task_runner=orch.task_runner,
                cron_service=orch.cron_svc,
                # Respect the user's phase-reaction setting, same as native
                # handle_message — live-read per message, NOT orch._cfg (which
                # is captured at startup and would make toggle saves inert
                # until restart).
                reactions_enabled=KiroCrewConfig.load().slack.reactions_enabled,
                # Respect slack.show_thinking (surface reasoning as a 💭 reply).
                show_thinking=KiroCrewConfig.load().slack.show_thinking,
                # History consolidation + display-name context, same as native
                # handle_message (parity: don't drop these on the transport path).
                consolidator=orch.consolidator,
                user_display_name=_sender_display,
            )
        )
        orch._session_tasks[session_key] = t

        def _on_transport_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
            orch._handler_tasks.discard(task)
            if orch._session_tasks.get(session_key) is task:
                del orch._session_tasks[session_key]
            _cleanup_image_temps()
            # Drain queue: only if no other task took over this session.
            # Mirrors native _on_done so messages queued while this session was
            # busy aren't stranded when the transport path is the active route.
            try:
                if session_key not in orch._session_tasks and orch.sessions:
                    _next = orch.sessions.dequeue(session_key)
                    # Fall back to orchestrator-level pending queue (pre-session).
                    if not _next:
                        _pq = orch._pending_queue.get(session_key)
                        if _pq:
                            _next = _pq.pop(0)
                            if not _pq:
                                del orch._pending_queue[session_key]
                    if _next:
                        _q_ts, _q_text, _q_kw = _next
                        _q_t = asyncio.ensure_future(
                            _dispatch_queued(orch, session_key, _q_ts, _q_text, _q_kw)
                        )
                        orch._session_tasks[session_key] = _q_t
                        orch._handler_tasks.add(_q_t)
                        _q_t.add_done_callback(_on_transport_done)
            except Exception:
                logger.exception("_on_transport_done drain failed for %s", session_key)

        t.add_done_callback(_on_transport_done)
        orch._handler_tasks.add(t)
        return

    try:
        t = asyncio.create_task(
            handle_message(
                orch.slack,  # type: ignore[arg-type]
                orch.sessions,  # type: ignore[arg-type]
                channel,
                clean_text,
                thread_ts,
                msg_ts,
                sender_id,
                team_id=team_id,
                approval_mode=_resolve_approval_mode(orch),
                context_builder=orch.ctx_builder,
                cron_service=orch.cron_svc,
                conversation_log=orch.conv_log,
                consolidator=orch.consolidator,
                subagent_manager=orch.subagent_mgr,
                task_runner=orch.task_runner,
                channel_agent=agent_override,
                user_display_name=_sender_display,
                from_trusted_bot=from_trusted_bot,
                channel_activation=activation,
                had_voice_input=_had_voice_input,
            )
        )
    except Exception:
        logger.exception("Failed to create handle_message task")
        _cleanup_image_temps()
        return

    orch._session_tasks[session_key] = t

    def _on_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
        orch._handler_tasks.discard(task)
        if orch._session_tasks.get(session_key) is task:
            del orch._session_tasks[session_key]
        _cleanup_image_temps()
        # Drain queue: only if no other task took over this session
        try:
            if session_key not in orch._session_tasks and orch.sessions:
                _next = orch.sessions.dequeue(session_key)
                # Fall back to orchestrator-level pending queue (pre-session messages)
                if not _next:
                    _pq = orch._pending_queue.get(session_key)
                    if _pq:
                        _next = _pq.pop(0)
                        if not _pq:
                            del orch._pending_queue[session_key]
                if _next:
                    _q_ts, _q_text, _q_kw = _next
                    _q_t = asyncio.ensure_future(
                        _dispatch_queued(orch, session_key, _q_ts, _q_text, _q_kw)
                    )
                    orch._session_tasks[session_key] = _q_t
                    orch._handler_tasks.add(_q_t)
                    _q_t.add_done_callback(_on_done)
        except Exception:
            logger.exception("_on_done drain failed for %s", session_key)

    orch._handler_tasks.add(t)
    t.add_done_callback(_on_done)
