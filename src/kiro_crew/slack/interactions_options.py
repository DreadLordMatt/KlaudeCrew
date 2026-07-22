"""Split of interactions.py (see the interactions.py shim for details)."""

from __future__ import annotations

import asyncio
import json
import logging

from kiro_crew.slack import interactions_core as _core
from kiro_crew.slack.format import (
    OPTIONS_ACTION_PREFIX,
    OPTIONS_CHECKBOXES_ACTION,
    OPTIONS_SUBMIT_ACTION,
    build_options_selected_blocks,
)
from kiro_crew.slack.handler import (
    APPROVAL_INTERACTIVE,
    handle_interaction,
)
from kiro_crew.slack.interactions_core import (
    _extract_selected_value,
    _route_action_to_session,
    ack_button,
)

logger = logging.getLogger(__name__)


_ACTION_PREFIX = "action::"


def _replace_options_blocks(
    blocks: list[dict], selected_blocks: list[dict]
) -> list[dict]:
    """Replace OPTIONS actions block(s) with *selected_blocks* in place.

    Walks *blocks* looking for any ``actions`` block whose elements include
    ``OPTIONS_CHECKBOXES_ACTION``, ``OPTIONS_SUBMIT_ACTION``, or an action_id
    starting with ``OPTIONS_ACTION_PREFIX``. The first such block is replaced
    by *selected_blocks* (inserted in order); subsequent OPTIONS actions blocks
    are dropped. All other blocks are preserved unchanged.
    """
    result: list[dict] = []
    inserted = False
    for block in blocks:
        if block.get("type") != "actions":
            result.append(block)
            continue
        elements = block.get("elements", [])
        is_options_block = any(
            el.get("action_id") in (OPTIONS_CHECKBOXES_ACTION, OPTIONS_SUBMIT_ACTION)
            or el.get("action_id", "").startswith(OPTIONS_ACTION_PREFIX)
            for el in elements
        )
        if not is_options_block:
            result.append(block)
            continue
        if not inserted:
            result.extend(selected_blocks)
            inserted = True
        # Drop the OPTIONS actions block itself
    if not inserted:
        # No OPTIONS actions block found — append selected_blocks at end so
        # the user still sees their selection (defensive fallback).
        logger.warning(
            "OPTIONS actions block not found in parent message blocks; "
            "appending selection at end"
        )
        result.extend(selected_blocks)
    return result


async def _handle_options_submit(payload: dict, channel: str, msg_ts: str) -> None:
    """User clicked Send on multi-select OPTIONS checkboxes."""
    if not (_core._orch and _core._orch.slack):
        return

    thread_ts = payload.get("message", {}).get("thread_ts") or msg_ts
    user_id = payload.get("user", {}).get("id", "")
    team_id = (payload.get("team") or {}).get("id", "")

    if not _core.is_allowed_user(user_id):
        _core.sel().log_tool_invocation(
            session_key=thread_ts, agent="kirocrew", source="slack",
            tool_name="options_submit", tool_kind="interaction",
            outcome="denied",
            metadata={"user_id": user_id, "reason": "not_allowed_user"},
        )
        return

    # Read checkbox state from the payload's state.values
    state_values = payload.get("state", {}).get("values", {})
    selected: list[str] = []
    for block_vals in state_values.values():
        cb_state = block_vals.get(OPTIONS_CHECKBOXES_ACTION)
        if cb_state:
            selected = [o["value"] for o in cb_state.get("selected_options", [])]
            break

    if not selected:
        _core.sel().log_tool_invocation(
            session_key=thread_ts, agent="kirocrew", source="slack",
            tool_name="options_submit", tool_kind="interaction",
            outcome="skipped", metadata={"reason": "empty_selection"},
        )
        return  # nothing checked, ignore

    # Extract all choices for the styled summary
    blocks = payload.get("message", {}).get("blocks", [])
    all_choices: list[str] = []
    for b in blocks:
        if b.get("type") != "actions":
            continue
        for el in b.get("elements", []):
            if el.get("action_id") == OPTIONS_CHECKBOXES_ACTION:
                all_choices = [o["value"] for o in el.get("options", [])]
                break

    # Compute indices BEFORE redaction — deduplicate to handle identical choices
    selected_set = set(selected)
    selected_indices: list[int] = []
    seen: set[str] = set()
    for i, c in enumerate(all_choices):
        if c in selected_set and c not in seen:
            selected_indices.append(i)
            seen.add(c)

    # Redact
    selected = [_core.redact_credentials(_core.redact_exfiltration_urls(s)[0])[0] for s in selected]
    all_choices = [_core.redact_credentials(_core.redact_exfiltration_urls(c)[0])[0] for c in all_choices]

    combined = ", ".join(selected)

    # Edit-in-place: replace only the OPTIONS actions block(s) with the
    # styled selection, preserving every other surrounding block. Falls back
    # to post-and-delete if update_message raises (resilience).
    selected_blocks = build_options_selected_blocks(all_choices, selected_indices)
    parent_blocks = payload.get("message", {}).get("blocks", [])
    new_blocks = _replace_options_blocks(parent_blocks, selected_blocks)
    new_ts = msg_ts
    edited = False
    try:
        await _core._orch.slack.update_message(
            channel, msg_ts, text=combined, blocks=new_blocks
        )
        edited = True
    except Exception:
        logger.debug(
            "update_message failed for options_submit, falling back to post+delete",
            exc_info=True,
        )

    if not edited:
        posted_ts = await _core._orch.slack.post_blocks(
            channel, selected_blocks, combined, thread_ts
        )
        if not posted_ts:
            logger.warning("Failed to post options choice — aborting")
            _core.sel().log_tool_invocation(
                session_key=thread_ts, agent="kirocrew", source="slack",
                tool_name="options_submit", tool_kind="interaction",
                outcome="failure", metadata={"reason": "post_blocks_failed"},
            )
            return
        new_ts = posted_ts
        try:
            await _core._orch.slack.delete_message(channel, msg_ts)
        except Exception:
            logger.warning(
                "Failed to delete original OPTIONS message after fallback "
                "post_blocks succeeded; user may see both the original "
                "and the new selection message",
                exc_info=True,
            )

    action_context = (
        "--- CONTEXT ENTRY BEGIN ---\n"
        f"[OPTIONS multi-select: {combined}]\n"
        "--- CONTEXT ENTRY END ---"
    )

    t = asyncio.create_task(
        _core.handle_message(
            _core._orch.slack,
            _core._orch.sessions,  # type: ignore[arg-type]
            channel,
            combined,
            thread_ts,
            new_ts,
            user_id,
            team_id=team_id,
            approval_mode=APPROVAL_INTERACTIVE,
            context_builder=_core._orch.ctx_builder,
            cron_service=_core._orch.cron_svc,
            conversation_log=_core._orch.conv_log,
            consolidator=_core._orch.consolidator,
            subagent_manager=_core._orch.subagent_mgr,
            task_runner=_core._orch.task_runner,
            action_context=action_context,
        )
    )
    _core._orch._handler_tasks.add(t)
    t.add_done_callback(_core._orch._handler_tasks.discard)
    _core.sel().log_tool_invocation(
        session_key=thread_ts, agent="kirocrew", source="slack",
        tool_name="options_submit", tool_kind="interaction",
        outcome="success", metadata={"selected": combined, "channel": channel},
    )


async def _handle_options(payload: dict, action: dict, channel: str, msg_ts: str) -> None:
    """User picked an OPTIONS choice — delete footer, post styled selection."""
    choice = action.get("value", "")
    # Overflow menus nest the value under selected_option
    if not choice:
        choice = (action.get("selected_option") or {}).get("value", "")
    action_id = action.get("action_id", "")
    if not ((choice or action_id.startswith(_ACTION_PREFIX)) and channel and _core._orch and _core._orch.slack):
        return

    thread_ts = payload.get("message", {}).get("thread_ts") or msg_ts
    user_id = payload.get("user", {}).get("id", "")
    team_id = (payload.get("team") or {}).get("id", "")
    blocks = payload.get("message", {}).get("blocks", [])

    # ── Action button: route payload to existing session as context ──
    if choice.startswith(_ACTION_PREFIX):
        action_payload = choice[len(_ACTION_PREFIX):]
        label = action.get("text", {}).get("text", "")
        # Overflow menus: label is on the selected_option
        if not label:
            label = (action.get("selected_option") or {}).get("text", {}).get("text", "")
        action_id_value = action.get("action_id", "")
        await _route_action_to_session(
            channel, msg_ts, thread_ts, user_id, team_id,
            label, action_payload, "Action button clicked",
            action_id_value, blocks,
        )
        return

    # ── Extended element: action_id carries the action:: prefix ──
    action_id_value = action.get("action_id", "")
    if action_id_value.startswith(_ACTION_PREFIX):
        base_json = action_id_value[len(_ACTION_PREFIX):]
        raw_value, display_text = _extract_selected_value(action)

        # Merge selected_value into base payload
        try:
            merged = json.loads(base_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid JSON in action_id: %s", base_json[:200])
            return
        if not isinstance(merged, dict):
            logger.warning("Expected dict from action_id JSON, got %s", type(merged).__name__)
            return
        merged["selected_value"] = raw_value
        merged_json = json.dumps(merged)

        # Derive display label: placeholder + selected text
        placeholder = action.get("placeholder", {}).get("text", "")
        label = f"{placeholder}: {display_text}" if placeholder else display_text

        await _route_action_to_session(
            channel, msg_ts, thread_ts, user_id, team_id,
            label, merged_json, "Action element selected",
            action_id_value, blocks,
        )
        return

    # ── Standard OPTIONS choice: delete message, post value, new session ──

    # Determine which button was clicked
    try:
        selected_index = int(action_id.replace(OPTIONS_ACTION_PREFIX, ""))
    except (ValueError, TypeError):
        selected_index = 0

    # Extract all choices from the original message
    blocks = payload.get("message", {}).get("blocks", [])
    all_choices = [
        el.get("value", "")
        for b in blocks
        if b.get("type") == "actions"
        for el in b.get("elements", [])
        if el.get("action_id", "").startswith(OPTIONS_ACTION_PREFIX)
    ]

    # Redact LLM-generated content before any external use
    choice, _ = _core.redact_exfiltration_urls(choice)
    choice, _ = _core.redact_credentials(choice)
    all_choices = [_core.redact_credentials(_core.redact_exfiltration_urls(c)[0])[0] for c in all_choices]

    # Edit-in-place: replace only the OPTIONS actions block with the styled
    # selection, preserving every other surrounding block. Falls back to
    # post-and-delete if update_message raises.
    selected_blocks = build_options_selected_blocks(all_choices, selected_index)
    new_blocks = _replace_options_blocks(blocks, selected_blocks)
    new_ts = msg_ts
    edited = False
    try:
        await _core._orch.slack.update_message(
            channel, msg_ts, text=choice, blocks=new_blocks
        )
        edited = True
    except Exception:
        logger.debug(
            "update_message failed for options choice, falling back to post+delete",
            exc_info=True,
        )

    if not edited:
        posted_ts = await _core._orch.slack.post_blocks(
            channel, selected_blocks, choice, thread_ts
        )
        if not posted_ts:
            logger.warning("Failed to post options choice — aborting")
            _core.sel().log_tool_invocation(
                session_key=thread_ts, agent="kirocrew", source="slack",
                tool_name="options", tool_kind="interaction",
                outcome="failure", metadata={"reason": "post_blocks_failed"},
            )
            return
        new_ts = posted_ts
        try:
            await _core._orch.slack.delete_message(channel, msg_ts)
        except Exception:
            logger.warning(
                "Failed to delete original OPTIONS message after fallback "
                "post_blocks succeeded; user may see both the original "
                "and the new selection message",
                exc_info=True,
            )

    t = asyncio.create_task(
        _core.handle_message(
            _core._orch.slack,
            _core._orch.sessions,  # type: ignore[arg-type]
            channel,
            choice,
            thread_ts,
            new_ts,
            user_id,
            team_id=team_id,
            approval_mode=APPROVAL_INTERACTIVE,
            context_builder=_core._orch.ctx_builder,
            cron_service=_core._orch.cron_svc,
            conversation_log=_core._orch.conv_log,
            consolidator=_core._orch.consolidator,
            subagent_manager=_core._orch.subagent_mgr,
            task_runner=_core._orch.task_runner,
        )
    )
    _core._orch._handler_tasks.add(t)
    t.add_done_callback(_core._orch._handler_tasks.discard)


async def _handle_cron_ack(payload: dict, action: dict, channel: str, msg_ts: str) -> None:
    job_id = action.get("value", "")
    if not (job_id and _core._orch and _core._orch.cron_svc):
        return
    await ack_button(payload, channel, msg_ts)
    msg_text = payload.get("message", {}).get("text", "")[:200]
    _core._orch.cron_svc.ack_job(job_id, msg_text)
    if _core._orch.dashboard_state:
        for n in _core._orch.dashboard_state._notification_log:
            if n.get("job_id") == job_id and not n.get("acked"):
                _core._orch.dashboard_state.ack_notification(n["ts"])
                _core._orch.dashboard_state.broadcast_ws("notification_ack", {"ts": n["ts"]})


async def _handle_subagent_ack(payload: dict, action: dict, channel: str, msg_ts: str) -> None:
    subagent_id = action.get("value", "")
    await ack_button(payload, channel, msg_ts)
    if not (subagent_id and _core._orch and _core._orch.dashboard_state):
        return
    for n in _core._orch.dashboard_state._notification_log:
        if n.get("kind") == "subagent" and subagent_id in n.get("title", "") and not n.get("acked"):
            _core._orch.dashboard_state.ack_notification(n["ts"])
            _core._orch.dashboard_state.broadcast_ws("notification_ack", {"ts": n["ts"]})


async def _handle_tool_approval(
    payload: dict, action_id: str, channel: str, msg_ts: str, user_id: str
) -> None:
    """Route approve / trust / reject to the handler."""
    # Trust is restricted to DMs — fail-closed if orchestrator not ready
    if action_id == "trust_tool":
        if not _core._orch or not _core._orch.slack:
            logger.warning("trust_tool: orchestrator not ready — rejecting")
            return
        is_dm = await _core._orch.slack.is_dm(channel)
        if not is_dm:
            logger.warning("Rejecting trust_tool in non-DM channel %s (user=%s)", channel, user_id)
            return

    thread_ts = payload.get("message", {}).get("thread_ts", "")
    slack_ops = _core._orch.slack if _core._orch else None
    effective_action = await handle_interaction(channel, msg_ts, action_id, user_id=user_id, thread_ts=thread_ts, slack=slack_ops, sessions=_core._orch.sessions if _core._orch else None)

    # Replace buttons with outcome label — only when an action was processed.
    # When effective_action is None (unauthorized user or already resolved),
    # preserve buttons so the authorized owner can still click.
    if _core._orch and _core._orch.slack and effective_action:
        label = {
            "approve_tool": "✅ Approved",
            "trust_tool": "🤝 Trusted",
            "reject_tool": "🚫 Rejected",
        }.get(effective_action, "")
        if label:
            try:
                await _core._orch.slack.update_message(channel, msg_ts, text=label)
            except Exception:
                pass
