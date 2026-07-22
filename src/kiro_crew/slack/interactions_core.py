"""Split of interactions.py (see the interactions.py shim for details)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.slack.allowlist import (
    ACTION_ALLOWLIST_APPROVE,
    ACTION_ALLOWLIST_DENY,
    ACTION_TRACK_APPROVE,
    ACTION_TRACK_DENY,
)
from kiro_crew.slack.format import (
    LINK_DASHBOARD_ACTION,
    OPTIONS_ACTION_PREFIX,
    OPTIONS_CHECKBOXES_ACTION,
    OPTIONS_SUBMIT_ACTION,
)
from kiro_crew.slack.handler import (
    APPROVAL_INTERACTIVE,
    add_trusted_session,
    handle_message,
    is_allowed_user,
    is_owner,
)
from kiro_crew.slack.renderer import (
    TOOL_APPROVE_ACTION_PREFIX,
    TOOL_DENY_ACTION_PREFIX,
    TOOL_TRUST_ACTION_PREFIX,
    SlackApprovalDecider,
)

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


_FENCE_MARKER_RE = re.compile(
    r"-{0,}\s*(?:UNTRUSTED FORWARDED CONTENT|CONTEXT ENTRY)\s+(?:BEGIN|END)\s*-{0,}",
    re.IGNORECASE,
)


def _neutralize_fence_markers(text: str) -> str:
    """Strip any embedded quarantine/context fence markers from untrusted text.

    The forwarded body is authored by an arbitrary third party (possibly
    external via Slack-Connect). If it contains a literal ``--- UNTRUSTED
    FORWARDED CONTENT END ---`` (or a CONTEXT ENTRY marker), interpolating it
    between the real fence markers would let the attacker's trailing text break
    out of the quarantine and land in the trusted first-party region of the
    prompt. Replace any such marker phrase with a defanged placeholder so the
    boundary the model relies on cannot be forged from within the content.
    """
    return _FENCE_MARKER_RE.sub("[removed embedded fence marker]", text)


_orch: GatewayOrchestrator | None = None


def init(orchestrator: GatewayOrchestrator) -> None:
    """Bind the orchestrator so interactive handlers can reach services."""
    from kiro_crew.slack.interactions_config import _handle_shortcut_submission
    global _orch
    _orch = orchestrator
    fwd_cb = _get_forward_callback()
    if fwd_cb:
        register_view_handler(fwd_cb, _handle_shortcut_submission)


ViewHandler = Callable[[dict], Awaitable[None]]


VIEW_REGISTRY: dict[str, ViewHandler] = {}


def register_view_handler(callback_id: str, handler: ViewHandler) -> None:  # type: ignore[type-arg]
    """Register a handler for a ``view_submission`` or ``view_closed`` callback_id."""
    VIEW_REGISTRY[callback_id] = handler


async def handle_view_submission(payload: dict) -> None:
    """Dispatch a view_submission event to the registered handler."""
    from kiro_crew.slack.interactions_config import _handle_shortcut_submission
    view = payload.get("view", {})
    callback_id = view.get("callback_id", "")
    handler = VIEW_REGISTRY.get(callback_id)
    if handler is None and callback_id and callback_id == _get_forward_callback():
        # Live-reconfig fallback: the forward-to-agent callback is operator-
        # configurable, so unlike the fixed-string sibling handlers it may be
        # enabled/changed after init() ran (which registered nothing, or a now-
        # stale key). The modal-open path (_handle_message_shortcut) already
        # resolves the callback dynamically on every event; resolve it here too
        # so the open and submit paths agree and a forward can't silently vanish.
        handler = _handle_shortcut_submission
    if handler is None:
        logger.warning("No view handler registered for callback_id=%s", callback_id)
        return
    try:
        await handler(payload)  # type: ignore[misc]
    except Exception:
        logger.exception("View handler failed for callback_id=%s", callback_id)


async def handle_view_closed(payload: dict) -> None:
    """Dispatch a view_closed event. Uses same registry with ``_closed`` suffix fallback."""
    view = payload.get("view", {})
    callback_id = view.get("callback_id", "")
    # Try <callback_id>_closed first, then fall back to <callback_id>
    handler = VIEW_REGISTRY.get(callback_id + "_closed")
    if handler is None:
        logger.debug("No view_closed handler for callback_id=%s (ignored)", callback_id)
        return
    try:
        await handler(payload)  # type: ignore[misc]
    except Exception:
        logger.exception("View closed handler failed for callback_id=%s", callback_id)


async def ack_button(payload: dict, channel: str, msg_ts: str) -> None:
    """Replace an ack/approve button message with '✅ Acknowledged'.

    Tries ``response_url`` first (instant, no API call), then falls
    back to ``chat.update``.
    """
    response_url = payload.get("response_url", "")
    blocks = payload.get("message", {}).get("blocks", [])

    # Strip action blocks, keep content — append ack context
    acked_blocks = []
    for b in blocks:
        if b.get("type") == "actions":
            continue
        if b.get("type") == "section" and b.get("text", {}).get("text", ""):
            b = {**b, "text": {**b["text"], "text": b["text"]["text"][:2990]}}
        acked_blocks.append(b)
    acked_blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "✅ Acknowledged"}]}
    )

    updated = False
    if response_url:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as sess:
                resp = await sess.post(
                    response_url,
                    json={
                        "replace_original": True,
                        "text": "✅ Acknowledged",
                        "blocks": acked_blocks,
                    },
                )
                updated = resp.status == 200
        except Exception:
            logger.debug("response_url update failed", exc_info=True)

    if not updated and _orch and _orch.slack and channel and msg_ts:
        try:
            await _orch.slack.update_message(
                channel, msg_ts, text="✅ Acknowledged", blocks=acked_blocks
            )
        except Exception:
            logger.debug("chat.update fallback failed", exc_info=True)


def _get_forward_callback() -> str:
    """Return the configured forward-to-agent callback ID, or empty if disabled."""
    if not _orch or not _orch._cfg:
        return ""
    return _orch._cfg.slack.forward_to_agent_callback


async def dispatch(payload: dict) -> None:
    """Route a Block Kit interactive payload to the correct handler."""
    # Call-time (local) imports break the core<->handler import cycle: handler
    # submodules import helpers + the live _orch from this module at load time,
    # so their names can only be bound here once everything is initialized. One-
    # way DAG: core -> handler submodules; no submodule imports dispatch.
    from kiro_crew.slack.interactions_config import (
        _handle_agent_select,
        _handle_allowlist,
        _handle_allowlist_remove,
        _handle_ch_activation,
        _handle_ch_add,
        _handle_ch_agent,
        _handle_ch_remove,
        _handle_channel_remove,
        _handle_channels_select,
        _handle_message_shortcut,
        _handle_track_channel,
        _handle_users_select,
    )
    from kiro_crew.slack.interactions_options import (
        _handle_cron_ack,
        _handle_options,
        _handle_options_submit,
        _handle_subagent_ack,
        _handle_tool_approval,
    )
    from kiro_crew.slack.interactions_review import (
        _handle_review_approve,
        _handle_review_cancel,
        _handle_review_edit,
        _handle_review_revise,
    )
    from kiro_crew.slack.interactions_sessions import (
        _handle_inline_stop,
        _handle_resume_choice,
        _handle_session_end,
        _handle_session_new,
        _handle_session_resume,
        _handle_stop_cancel,
        _handle_stop_confirm,
        _handle_stop_kill_now,
    )

    # ── View submissions and closures (modals) ──
    payload_type = payload.get("type", "")
    if payload_type == "view_submission":
        await handle_view_submission(payload)
        return
    if payload_type == "view_closed":
        await handle_view_closed(payload)
        return

    # ── Message shortcuts (right-click → "Forward to Agent") ──
    if payload_type == "message_action":
        await _handle_message_shortcut(payload)
        return

    actions = payload.get("actions", [])
    if not actions:
        return

    action = actions[0]
    action_id = action.get("action_id", "")
    channel = payload.get("channel", {}).get("id", "")
    msg_ts = payload.get("message", {}).get("ts", "")
    user_id = payload.get("user", {}).get("id", "")

    # ── Access check — deny-by-default ──
    if not is_allowed_user(user_id):
        logger.warning(
            "Rejecting interactive payload from unauthorized user %s (action=%s)",
            user_id or "unknown",
            action_id,
        )
        sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.interactive",
            outcome="denied",
            source="slack",
            resources=action_id,
            error="unauthorized user",
        )
        if _orch and _orch.slack and channel and user_id:
            try:
                await _orch.slack.post_ephemeral(
                    channel, user_id, "⛔ You are not authorized to use these buttons."
                )
            except Exception:
                logger.debug("Failed to send ephemeral rejection", exc_info=True)
        return

    # ── OPTIONS checkboxes toggle — no-op, wait for Send ──
    if action_id == OPTIONS_CHECKBOXES_ACTION:
        return

    # ── OPTIONS Send button ──
    if action_id == OPTIONS_SUBMIT_ACTION:
        await _handle_options_submit(payload, channel, msg_ts)
        return

    # ── Legacy OPTIONS choice buttons ──
    if action_id.startswith(OPTIONS_ACTION_PREFIX):
        if "_done_" in action_id:
            return
        await _handle_options(payload, action, channel, msg_ts)
        return

    # ── Cron acknowledge ──
    from kiro_crew.slack.format import CRON_ACK_ACTION_PREFIX

    if action_id.startswith(CRON_ACK_ACTION_PREFIX):
        await _handle_cron_ack(payload, action, channel, msg_ts)
        return

    # ── Subagent acknowledge ──
    from kiro_crew.slack.format import SUBAGENT_ACK_ACTION_PREFIX

    if action_id.startswith(SUBAGENT_ACK_ACTION_PREFIX):
        await _handle_subagent_ack(payload, action, channel, msg_ts)
        return

    # ── Allowlist approve / deny (owner-only) ──
    if action_id in (ACTION_ALLOWLIST_APPROVE, ACTION_ALLOWLIST_DENY):
        if not is_owner(user_id):
            logger.warning("Rejecting allowlist action from non-owner %s", user_id)
            sel().log_api_access(
                caller=user_id,
                operation="slack.allowlist.button",
                outcome="denied",
                source="slack",
                resources=action_id,
                error="non-owner",
            )
            return
        await _handle_allowlist(payload, action, action_id, channel, msg_ts, user_id)
        return

    # ── Track channel approve / deny (owner-only) ──
    if action_id in (ACTION_TRACK_APPROVE, ACTION_TRACK_DENY):
        if not is_owner(user_id):
            logger.warning("Rejecting track-channel action from non-owner %s", user_id)
            sel().log_api_access(
                caller=user_id,
                operation="slack.track_channel.button",
                outcome="denied",
                source="slack",
                resources=action_id,
                error="non-owner",
            )
            return
        await _handle_track_channel(payload, action, action_id, channel, msg_ts, user_id)
        return

    # ── Stop confirm / cancel ──
    if action_id == "mc_stop_confirm":
        await _handle_stop_confirm(payload, channel, msg_ts, user_id)
        return
    if action_id == "mc_stop_cancel":
        await _handle_stop_cancel(payload, channel, msg_ts)
        return

    # ── Kill Now (ephemeral stop escalation) ──
    if action_id == "stop_kill_now":
        await _handle_stop_kill_now(payload, action, channel, msg_ts, user_id)
        return

    # ── Dashboard copy link ──
    if action_id == "mc_dashboard_copy":
        url = action.get("value", "")
        response_url = payload.get("response_url", "")
        if response_url and url:
            import aiohttp

            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url,
                    json={
                        "replace_original": False,
                        "response_type": "ephemeral",
                        "text": f"📋 Copy this link:\n```{url}```",
                    },
                )
        return

    # ── Link to Dashboard button ──
    if action_id == LINK_DASHBOARD_ACTION:
        user_id = payload.get("user", {}).get("id", "")
        if not is_allowed_user(user_id):
            sel().log_tool_invocation(
                session_key="", agent="kirocrew", source="slack",
                tool_name="mc_link_dashboard", tool_kind="interaction",
                outcome="denied",
                metadata={"user_id": user_id, "reason": "not_allowed_user"},
            )
            return
        thread_ts = payload.get("message", {}).get("thread_ts") or payload.get("container", {}).get("thread_ts", "")
        if not thread_ts:
            sel().log_tool_invocation(
                session_key="", agent="kirocrew", source="slack",
                tool_name="mc_link_dashboard", tool_kind="interaction",
                outcome="failure",
                metadata={"user_id": user_id, "reason": "no_thread_ts"},
            )
            return
        ds = _orch.dashboard_state if _orch else None
        if not ds or not hasattr(ds, "get_or_create_slot"):
            sel().log_tool_invocation(
                session_key="", agent="kirocrew", source="slack",
                tool_name="mc_link_dashboard", tool_kind="interaction",
                outcome="failure",
                metadata={"user_id": user_id, "reason": "no_dashboard"},
            )
            return
        if not _orch or not _orch.slack:
            sel().log_tool_invocation(
                session_key="", agent="kirocrew", source="slack",
                tool_name="mc_link_dashboard", tool_kind="interaction",
                outcome="failure",
                metadata={"user_id": user_id, "reason": "no_slack_client"},
            )
            return
        slot = await _import_thread_to_slot(_orch.slack, ds, channel, thread_ts)
        if not slot:
            sel().log_tool_invocation(
                session_key="", agent="kirocrew", source="slack",
                tool_name="mc_link_dashboard", tool_kind="interaction",
                outcome="failure",
                metadata={"channel": channel, "thread_ts": thread_ts, "reason": "empty_thread"},
            )
            response_url = payload.get("response_url", "")
            if response_url and response_url.startswith("https://hooks.slack.com/"):
                import aiohttp

                async with aiohttp.ClientSession() as sess:
                    await sess.post(
                        response_url,
                        json={
                            "replace_original": False,
                            "response_type": "ephemeral",
                            "text": "⚠️ Could not import thread history.",
                        },
                    )
            return
        sel().log_tool_invocation(
            session_key=slot.key, agent="kirocrew", source="slack",
            tool_name="mc_link_dashboard", tool_kind="interaction",
            outcome="success",
            metadata={"slot": slot.key, "channel": channel, "thread_ts": thread_ts},
        )
        # Replace the button with confirmation
        response_url = payload.get("response_url", "")
        if response_url and response_url.startswith("https://hooks.slack.com/"):
            import aiohttp

            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url,
                    json={
                        "replace_original": False,
                        "response_type": "ephemeral",
                        "text": f"Linked to dashboard session *{slot.key}* -- messages sync both ways.",
                    },
                )
        return

    # ── Agent select dropdown ──
    if action_id == "mc_agent_select":
        await _handle_agent_select(payload, action, channel, msg_ts, user_id)
        return

    # ── Users multi-select ──
    if action_id == "mc_users_select":
        await _handle_users_select(payload, action, channel, msg_ts, user_id)
        return

    # ── Channels multi-select ──
    if action_id == "mc_channels_select":
        await _handle_channels_select(payload, action, channel, msg_ts, user_id)
        return

    # ── Session resume choice buttons ──
    if action_id.startswith("mc_resume_thread_"):
        await _handle_resume_choice(payload, action, channel, msg_ts, user_id, mode="thread")
        return
    if action_id.startswith("mc_resume_dm_"):
        await _handle_resume_choice(payload, action, channel, msg_ts, user_id, mode="dm")
        return

    # ── Session resume/end/new buttons ──
    if action_id.startswith("mc_session_resume_"):
        await _handle_session_resume(payload, action, channel, msg_ts, user_id)
        return
    if action_id.startswith("mc_session_end_"):
        await _handle_session_end(payload, action, channel, msg_ts, user_id)
        return
    if action_id.startswith("mc_inline_stop_"):
        await _handle_inline_stop(payload, action, channel, msg_ts, user_id)
        return
    if action_id == "mc_session_new":
        await _handle_session_new(payload, action, channel, msg_ts, user_id)
        return

    # ── Channel modal: activation change ──
    if action_id.startswith("mc_ch_activation_"):
        await _handle_ch_activation(payload, action)
        return

    # ── Channel modal: agent change ──
    if action_id.startswith("mc_ch_agent_"):
        await _handle_ch_agent(payload, action)
        return

    # ── Channel modal: remove channel ──
    if action_id.startswith("mc_ch_remove_"):
        await _handle_ch_remove(payload, action)
        return

    # ── Channel modal: add channel ──
    if action_id == "mc_ch_add":
        await _handle_ch_add(payload, action)
        return

    # ── Review mode: approve / edit / cancel ──
    if action_id == "mc_review_approve":
        await _handle_review_approve(payload, action)
        return
    if action_id == "mc_review_edit":
        await _handle_review_edit(payload, action)
        return
    if action_id == "mc_review_revise":
        await _handle_review_revise(payload, action)
        return
    if action_id == "mc_review_cancel":
        await _handle_review_cancel(payload, action)
        return

    # ── Allowlist / channel list remove buttons ──
    if action_id.startswith("mc_allowlist_remove_"):
        await _handle_allowlist_remove(payload, action, channel, msg_ts, user_id)
        return
    if action_id.startswith("mc_channel_remove_"):
        await _handle_channel_remove(payload, action, channel, msg_ts, user_id)
        return

    # ── Messaging-transport interactive tool approval (decider-backed) ──
    # These buttons come from the new transport path's SlackRenderer
    # (build_approval_blocks → mc_tool_approve_/trust_/deny_<rid>).
    # Resolve the per-turn SlackApprovalDecider via its process-global registry.
    if (
        action_id.startswith(TOOL_APPROVE_ACTION_PREFIX)
        or action_id.startswith(TOOL_TRUST_ACTION_PREFIX)
        or action_id.startswith(TOOL_DENY_ACTION_PREFIX)
    ):
        # Defense-in-depth auth gate: dispatch() already denies non-allowed
        # users at the top, but re-check here (deny-by-default) so a tool
        # approval / Trust escalation can never be resolved by an unauthorized
        # actor even if this branch is ever reached via another path. Mirrors
        # native _handle_tool_approval's explicit trust-escalation check.
        if not is_allowed_user(user_id):
            sel().log_api_access(
                caller=user_id or "unknown",
                operation="slack.transport_tool_approval",
                outcome="denied",
                source="slack",
                resources=f"action={action_id} unauthorized",
                error="unauthorized user",
            )
            return
        is_trust = action_id.startswith(TOOL_TRUST_ACTION_PREFIX)
        approved = is_trust or action_id.startswith(TOOL_APPROVE_ACTION_PREFIX)
        # value / action_id suffix carry the session-namespaced approval token
        # (session_key:request_id) so a click resolves ONLY its own session's
        # pending tool — kiro-cli request ids restart at 1 per session.
        approval_key = action.get("value", "") or action_id.rsplit("_", 1)[-1]
        # Trust grants per-session auto-approve BEFORE resolving, so subsequent
        # tools in this session are auto-approved (mirrors native trust_tool).
        if is_trust:
            sess_key = SlackApprovalDecider.session_for(approval_key)
            add_trusted_session(sess_key, _orch.sessions if _orch else None)
        resolved = SlackApprovalDecider.resolve_global(approval_key, approved)
        if resolved:
            label = (
                "🔓 Trusted this session — tools auto-approved"
                if is_trust
                else ("✅ Approved" if approved else "🚫 Denied")
            )
        else:
            label = "⏱ This approval already expired."
        sel().log_api_access(
            caller=user_id,
            operation="slack.transport_tool_approval",
            outcome=(
                ("trusted" if is_trust else ("approved" if approved else "denied"))
                if resolved
                else "expired"
            ),
            source="slack",
            resources=f"approval_key={approval_key}",
        )
        if _orch and _orch.slack and channel and msg_ts:
            try:
                await _orch.slack.update_message(channel, msg_ts, text=label)
            except Exception:
                logger.debug("Failed to update transport approval message", exc_info=True)
        return

    # ── Tool approval buttons (approve / trust / reject) ──
    if channel and msg_ts:
        await _handle_tool_approval(payload, action_id, channel, msg_ts, user_id)


def _mark_button_clicked(blocks: list[dict], clicked_action_id: str, label: str) -> list[dict]:
    """Replace a clicked button with a ✓ context block in the Block Kit message.

    Walks *blocks* looking for an ``actions`` block containing *clicked_action_id*.
    Removes that button element and inserts a ``context`` block with
    ``✓ {label}`` immediately before the actions block.  If no elements
    remain, the empty actions block is dropped entirely.
    """
    result: list[dict] = []
    for block in blocks:
        if block.get("type") != "actions":
            result.append(block)
            continue
        elements = block.get("elements", [])
        remaining = [e for e in elements if e.get("action_id") != clicked_action_id]
        if len(remaining) == len(elements):
            # Clicked button not in this actions block — keep as-is
            result.append(block)
            continue
        # Insert ✓ context block before the (possibly empty) actions block
        result.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"✓ {label}"}]}
        )
        if remaining:
            result.append({**block, "elements": remaining})
    return result


def _extract_selected_value(action: dict) -> tuple[str, str]:
    """Return ``(raw_value, display_text)`` from an extended element payload."""
    opt = action.get("selected_option")
    if opt:
        return opt.get("value", ""), opt.get("text", {}).get("text", "")
    for field in ("selected_date", "selected_time"):
        val = action.get(field)
        if val:
            return val, val
    dt = action.get("selected_date_time")
    if dt is not None:
        return str(dt), str(dt)
    return "", ""


_ACTION_PAYLOAD_CAP = 4000


async def _route_action_to_session(
    channel: str,
    msg_ts: str,
    thread_ts: str,
    user_id: str,
    team_id: str,
    label: str,
    payload_str: str,
    context_tag: str,
    action_id_value: str,
    blocks: list[dict],
) -> None:
    """Shared logic for routing an action:: interaction to the agent session."""
    assert _orch and _orch.slack  # caller already checked  # noqa: S101

    # Redact label before any Slack surface
    label, _ = redact_exfiltration_urls(label)
    label = redact_credentials(label)[0]

    # Update the message: replace clicked element with ✓ label
    updated_blocks = _mark_button_clicked(blocks, action_id_value, label)
    try:
        await _orch.slack.update_message(
            channel, msg_ts, text=label, blocks=updated_blocks
        )
    except Exception:
        logger.debug("Failed to update action message", exc_info=True)

    # Post display text as visible user message
    new_ts = await _orch.slack.post_message(channel, label, thread_ts)
    if not new_ts:
        logger.warning("Failed to post action label — aborting action routing")
        return

    # Redact and cap payload before embedding in context
    payload_str, _ = redact_exfiltration_urls(payload_str)
    payload_str = redact_credentials(payload_str)[0]
    if len(payload_str) > _ACTION_PAYLOAD_CAP:
        payload_str = payload_str[:_ACTION_PAYLOAD_CAP] + "… [truncated]"

    # SEL audit trail
    sel().log_api_access(
        caller=user_id,
        operation=f"slack.{context_tag.split()[0].lower()}",
        outcome="allowed",
        source="slack",
        resources=action_id_value,
    )

    # Build context entry for the agent
    action_context = (
        "--- CONTEXT ENTRY BEGIN ---\n"
        f"[{context_tag}: {payload_str}]\n"
        "--- CONTEXT ENTRY END ---"
    )

    t = asyncio.create_task(
        handle_message(
            _orch.slack,
            _orch.sessions,  # type: ignore[arg-type]
            channel,
            label,
            thread_ts,
            new_ts,
            user_id,
            team_id=team_id,
            approval_mode=APPROVAL_INTERACTIVE,
            context_builder=_orch.ctx_builder,
            cron_service=_orch.cron_svc,
            conversation_log=_orch.conv_log,
            consolidator=_orch.consolidator,
            subagent_manager=_orch.subagent_mgr,
            task_runner=_orch.task_runner,
            action_context=action_context,
        )
    )
    _orch._handler_tasks.add(t)
    t.add_done_callback(_orch._handler_tasks.discard)


async def _import_thread_to_slot(slack: Any, ds: Any, channel: str, thread_ts: str) -> Any:
    """Fetch a Slack thread, redact messages, and import into a new dashboard slot."""
    from kiro_crew.dashboard.chat import _save_slot_to_history

    # Idempotency: return existing slot if already linked
    existing = ds.get_linked_slot(thread_ts)
    if existing:
        return existing

    msgs = await slack.fetch_thread_replies(channel, thread_ts)
    if not msgs:
        return None
    # Pre-filter: drop empty text and !link-to-dashboard messages
    msgs = [m for m in msgs if m.get("text", "").strip() and not m.get("text", "").startswith("!link-to-dashboard")]
    if not msgs:
        return None
    # Cap to last 50 messages to avoid bloating the slot
    truncated = len(msgs) > 50
    if truncated:
        msgs = msgs[-50:]
    slot = ds.get_or_create_slot()
    slot.title = f"Slack thread {thread_ts[:10]}" + (" (truncated)" if truncated else "")
    bot_id = getattr(ds, "_self_bot_id", None) or ""
    for m in msgs:
        is_bot = bool(m.get("bot_id")) or m.get("user") == bot_id
        role = "assistant" if is_bot else "user"
        text_content = m.get("text", "")
        text_content, _ = redact_exfiltration_urls(text_content)
        text_content, _ = redact_credentials(text_content)
        slot.append(role, text_content, f"msg msg-{'a' if is_bot else 'u'}")
    ds.link_slack(slot.key, thread_ts, channel)
    _save_slot_to_history(ds, slot)
    ds.push_slots_update()
    return slot
