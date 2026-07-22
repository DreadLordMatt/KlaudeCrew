"""Split of interactions.py (see the interactions.py shim for details)."""

from __future__ import annotations

import asyncio
import logging

from kiro_crew.config.loader import ACTIVATION_REVIEW
from kiro_crew.slack import interactions_core as _core
from kiro_crew.slack.handler import APPROVAL_INTERACTIVE
from kiro_crew.slack.interactions_core import register_view_handler

logger = logging.getLogger(__name__)


_REVIEW_AUTH_DENIED_MSG = (
    "⚠️ Only the bot owner or the user who requested this draft can act on it."
)


async def _delete_review_placeholder(channel: str, thread_ts: str) -> None:
    """Clear the 'Awaiting review…' thread status indicator."""
    if not _core._orch or not _core._orch.slack:
        return
    try:
        await _core._orch.slack.set_thread_status(channel, thread_ts, "")
    except Exception:
        logger.debug("Failed to clear review thread status", exc_info=True)


async def _post_review_auth_error(response_url: str) -> None:
    """Reply with an ephemeral error via response_url (replaces the draft)."""
    if not response_url:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            await sess.post(
                response_url,
                json={
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "text": _REVIEW_AUTH_DENIED_MSG,
                },
            )
    except Exception:
        logger.debug("Failed to post review auth-denied ephemeral", exc_info=True)


def _parse_draft_key(meta: str) -> tuple[str, str, str] | None:
    """Parse draft key 'channel|thread_ts|uuid' → (channel, thread_ts, draft_key) or None."""
    parts = meta.split("|")
    if len(parts) < 2:
        return None
    channel, thread_ts = parts[0], parts[1]
    return channel, thread_ts, meta


def _can_act_on_review_draft(caller: str, requester: str) -> bool:
    """Authorize a review-mode action: bot owner OR the requester who triggered the draft."""
    return bool(caller) and (caller == requester or _core.is_owner(caller))


async def _handle_review_approve(payload: dict, action: dict) -> None:
    """Post the approved draft to the channel."""
    if not _core._orch or not _core._orch.slack:
        return
    caller = payload.get("user", {}).get("id", "")
    parsed = _parse_draft_key(action.get("value", ""))
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed

    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_pop

    _, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
        _core.sel().log_api_access(
            caller=caller,
            operation="slack.review_approve",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        await _post_review_auth_error(payload.get("response_url", ""))
        return

    draft, _requester = _review_drafts_pop(draft_key)
    if not draft:
        logger.warning("Review approve: no draft found for %s", draft_key)
        return
    draft, _ = _core.redact_exfiltration_urls(draft)
    draft, _ = _core.redact_credentials(draft)
    await _core._orch.slack.post_message(channel, draft, thread_ts)
    await _delete_review_placeholder(channel, thread_ts)
    # Delete the ephemeral draft message
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json={"delete_original": True})
        except Exception:
            logger.debug("Failed to delete review ephemeral", exc_info=True)
    _core.sel().log_api_access(
        caller=caller,
        operation="slack.review_approve",
        outcome="allowed",
        source="slack",
        resources=channel,
    )
    logger.info("Review approved by %s in %s", caller, channel)


async def _handle_review_edit(payload: dict, action: dict) -> None:
    """Open a modal pre-filled with the draft for editing."""
    if not _core._orch or not _core._orch.slack:
        return
    caller = payload.get("user", {}).get("id", "")
    trigger_id = payload.get("trigger_id", "")
    if not trigger_id:
        logger.warning("Review edit: no trigger_id in payload")
        return
    parsed = _parse_draft_key(action.get("value", ""))
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed

    from kiro_crew.slack.blocks import review_edit_modal
    from kiro_crew.slack.handler import _review_drafts_get

    draft, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
        _core.sel().log_api_access(
            caller=caller,
            operation="slack.review_edit",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        await _post_review_auth_error(payload.get("response_url", ""))
        return
    if not draft:
        logger.warning("Review edit: no draft found for %s", draft_key)
        return
    modal = review_edit_modal(draft, draft_key)
    await _core._orch.slack.views_open(trigger_id, modal)
    # Delete the ephemeral draft message
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json={"delete_original": True})
        except Exception:
            logger.debug("Failed to delete review ephemeral", exc_info=True)
    _core.sel().log_api_access(
        caller=caller,
        operation="slack.review_edit",
        outcome="allowed",
        source="slack",
        resources=channel,
    )


async def _handle_review_cancel(payload: dict, action: dict) -> None:
    """Discard the draft and delete the ephemeral message."""
    if not _core._orch or not _core._orch.slack:
        return
    caller = payload.get("user", {}).get("id", "")
    parsed = _parse_draft_key(action.get("value", ""))
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed

    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_pop

    _, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
        _core.sel().log_api_access(
            caller=caller,
            operation="slack.review_cancel",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        await _post_review_auth_error(payload.get("response_url", ""))
        return

    _review_drafts_pop(draft_key)
    await _delete_review_placeholder(channel, thread_ts)
    # Delete the ephemeral draft message
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json={"delete_original": True})
        except Exception:
            logger.debug("Failed to delete review ephemeral", exc_info=True)
    _core.sel().log_api_access(
        caller=caller,
        operation="slack.review_cancel",
        outcome="allowed",
        source="slack",
        resources=channel,
    )
    logger.info("Review cancelled by %s in %s", caller, channel)


async def _handle_review_edit_submit(payload: dict) -> None:
    """Post the edited text from the review edit modal."""
    if not _core._orch or not _core._orch.slack:
        return
    caller = payload.get("user", {}).get("id", "")
    view = payload.get("view", {})
    meta = view.get("private_metadata", "")
    parsed = _parse_draft_key(meta)
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed

    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_pop

    _, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
        _core.sel().log_api_access(
            caller=caller,
            operation="slack.review_edit_submit",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        return
    values = view.get("state", {}).get("values", {})
    edited = (
        values.get("mc_review_edit_block", {})
        .get("mc_review_edit_input", {})
        .get("value", "")
    )
    if not edited:
        return

    _review_drafts_pop(draft_key)
    edited, _ = _core.redact_exfiltration_urls(edited)
    edited, _ = _core.redact_credentials(edited)
    await _core._orch.slack.post_message(channel, edited, thread_ts)
    await _delete_review_placeholder(channel, thread_ts)
    _core.sel().log_api_access(
        caller=caller,
        operation="slack.review_edit_submit",
        outcome="allowed",
        source="slack",
        resources=channel,
    )
    logger.info("Review edited and posted by %s in %s", caller, channel)


register_view_handler("mc_review_edit_submit", _handle_review_edit_submit)


async def _handle_review_revise(payload: dict, action: dict) -> None:
    """Open a modal for the user to provide revision feedback."""
    if not _core._orch or not _core._orch.slack:
        return
    caller = payload.get("user", {}).get("id", "")
    trigger_id = payload.get("trigger_id", "")
    if not trigger_id:
        logger.warning("Review revise: no trigger_id in payload")
        return
    parsed = _parse_draft_key(action.get("value", ""))
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed

    from kiro_crew.slack.blocks import review_revise_modal
    from kiro_crew.slack.handler import _review_drafts_get

    _, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
        _core.sel().log_api_access(
            caller=caller,
            operation="slack.review_revise",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        await _post_review_auth_error(payload.get("response_url", ""))
        return

    modal = review_revise_modal(draft_key)
    await _core._orch.slack.views_open(trigger_id, modal)
    # Delete the ephemeral draft message (new one will appear after revision)
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json={"delete_original": True})
        except Exception:
            logger.debug("Failed to delete review ephemeral", exc_info=True)
    _core.sel().log_api_access(
        caller=caller,
        operation="slack.review_revise",
        outcome="allowed",
        source="slack",
        resources=channel,
    )


async def _handle_review_revise_submit(payload: dict) -> None:
    """Take revision feedback, send to LLM with draft context, post new ephemeral draft."""
    if not _core._orch or not _core._orch.slack:
        return
    caller = payload.get("user", {}).get("id", "")
    view = payload.get("view", {})
    meta = view.get("private_metadata", "")
    parsed = _parse_draft_key(meta)
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed

    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_pop

    _, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
        _core.sel().log_api_access(
            caller=caller,
            operation="slack.review_revise_submit",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        return
    values = view.get("state", {}).get("values", {})
    feedback = (
        values.get("mc_review_revise_block", {})
        .get("mc_review_revise_input", {})
        .get("value", "")
    )
    if not feedback:
        return

    draft, _requester = _review_drafts_pop(draft_key)
    if not draft:
        logger.warning("Review revise: no draft found for %s", draft_key)
        return

    # Send revision request through _core.handle_message with context
    revision_prompt = (
        f"I asked you a question and you drafted this response:\n\n"
        f"---\n{draft}\n---\n\n"
        f"Please revise it based on this feedback: {feedback}\n\n"
        f"Respond ONLY with the revised response text, nothing else."
    )
    # Use _core.handle_message so the revision goes through the full pipeline
    # (including review mode interception → new ephemeral draft)
    # Fire-and-forget: Slack requires view_submission response within ~3s
    # Audit the permission decision before spawning the task so it's always recorded.
    _core.sel().log_api_access(
        caller=caller,
        operation="slack.review_revise_submit",
        outcome="allowed",
        source="slack",
        resources=channel,
    )

    async def _do_revise() -> None:
        try:
            await _core.handle_message(
                _core._orch.slack,  # type: ignore[arg-type]
                _core._orch.sessions,  # type: ignore[arg-type]
                channel,
                revision_prompt,
                thread_ts,
                thread_ts,  # msg_ts = thread_ts for revision
                caller,
                approval_mode=APPROVAL_INTERACTIVE,
                context_builder=_core._orch.ctx_builder,
                cron_service=_core._orch.cron_svc,
                conversation_log=_core._orch.conv_log,
                consolidator=_core._orch.consolidator,
                subagent_manager=_core._orch.subagent_mgr,
                task_runner=_core._orch.task_runner,
                channel_activation=ACTIVATION_REVIEW,
            )
            logger.info("Review revision requested by %s in %s", caller, channel)
        except Exception:
            _core.sel().log_api_access(
                caller=caller,
                operation="slack.review_revise_submit",
                outcome="error",
                source="slack",
                resources=channel,
                error="_core.handle_message failed",
            )
            logger.exception("Review revision failed for %s in %s", caller, channel)

    asyncio.create_task(_do_revise())


register_view_handler("mc_review_revise_submit", _handle_review_revise_submit)
