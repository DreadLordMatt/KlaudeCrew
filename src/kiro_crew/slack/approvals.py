"""Split from slack/handler.py: approvals cluster."""

from __future__ import annotations

import asyncio
import logging

from kiro_crew.providers.base import LLMEvent, LLMProvider
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import SessionManager
from kiro_crew.slack import handler_state as _state
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.handler_state import (
    _LinkedApproval,
    _PendingApproval,
    _trusted_sessions,
    is_allowed_user,
)
from kiro_crew.slack.keyword_commands import _safe_update
from kiro_crew.stats import Stats

logger = logging.getLogger(__name__)


def _should_auto_approve_spawn(context_builder, event_title: str) -> bool:
    """Check if a spawn_run tool call should be auto-approved."""
    return bool(
        context_builder
        and context_builder.hooks
        and context_builder.hooks.auto_approve_subagent_spawn
        and event_title == "spawn_run"
    )


# Timeout for user to click approve/reject before auto-rejecting
_APPROVAL_TIMEOUT = 120.0

# Slack Block Kit section text limit (3000 chars max); leave room for
# markdown fences (``` ... ```) that wrap the tool input.
_SLACK_SECTION_TEXT_LIMIT = 2900

# Truncation marker appended when tool_input exceeds the limit
_TRUNCATION_MARKER = "\n… [truncated]"


# Pending approvals: keyed by f"{channel}:{approval_msg_ts}"
# Module-level dict — safe because gateway runs in a single asyncio event loop.
_pending_approvals: dict[str, _PendingApproval] = {}


# Linked-slot approvals: keyed by f"{channel}:{approval_msg_ts}", parallel to
# _pending_approvals. Kept separate so the click handler can tell a Slack-native
# approval (answer the backend) from a dashboard-linked one (resolve the slot
# future only).
_linked_approvals: dict[str, _LinkedApproval] = {}


_OUTCOME_APPROVED = "approved"
_OUTCOME_REJECTED = "rejected"

# Block Kit action IDs
_ACTION_APPROVE = "approve_tool"
_ACTION_TRUST = "trust_tool"
_ACTION_REJECT = "reject_tool"


async def _reject_orphaned_tool(provider: LLMProvider, request_id: "str | int") -> None:
    """Reject a pending ACP permission request that we can no longer surface.

    Both the pre-approval stream-prep and the approval-prompt post happen BEFORE
    the permission is answered; if either raises, the ACP request would be left
    unanswered and the agent subprocess wedges forever (every later turn blocks
    behind it). Callers invoke this on failure, then re-raise. Swallows any
    reject failure (best-effort) so the original error still propagates.
    """
    try:
        await provider.reject_tool(request_id)
    except Exception:
        logger.warning("Failed to reject orphaned tool %s", request_id, exc_info=True)


class _LinkedApprovalEvent:
    """Minimal event shim for :func:`_build_approval_blocks`.

    The dashboard's permission event (``AcpEvent``) and the Slack-native
    ``LLMEvent`` have different shapes, so adapt the few fields the block
    builder reads: ``request_id``, ``title``, ``tool_input``, ``tool_purpose``.
    """

    __slots__ = ("request_id", "title", "tool_input", "tool_purpose")

    def __init__(self, request_id: str | int, title: str, tool_input: str = "") -> None:
        self.request_id = request_id
        self.title = title
        self.tool_input = tool_input
        self.tool_purpose = ""


async def post_linked_approval(
    slack: SlackClientOps,
    channel: str,
    thread_ts: str,
    request_id: str | int,
    session_key: str,
    title: str,
    tool_input: str = "",
) -> str | None:
    """Mirror a dashboard tool-approval prompt into a linked Slack thread.

    Posts Approve / Reject buttons threaded under ``thread_ts`` and registers a
    :class:`_LinkedApproval` keyed by ``channel:ts`` so a button click resolves
    the dashboard slot's approval future (see :func:`handle_interaction`).

    Returns the Slack message ts on success, or ``None`` if the post failed.
    The caller (dashboard ``_run_chat``) treats ``None`` as "delivery failed"
    and surfaces it rather than silently parking on an unanswerable prompt.

    Trust is intentionally omitted (``is_dm=False``): trust for a linked slot is
    a dashboard-side mode, not wired through this path. Approve / Reject are
    sufficient to guarantee the prompt is answerable from Slack.
    """
    # title / tool_input are LLM-generated (the tool-use request). Slack is an
    # external surface, so scrub them the same way every other outbound LLM
    # string is scrubbed before posting — the dashboard path already redacts
    # these via perm_meta, but this Slack mirror must do its own redaction.
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    tool_input, _ = redact_exfiltration_urls(tool_input)
    tool_input, _ = redact_credentials(tool_input)
    event = _LinkedApprovalEvent(request_id, title, tool_input)
    # _build_approval_blocks is typed for AcpEvent but only reads the four
    # attributes the shim provides (request_id/title/tool_input/tool_purpose).
    blocks = _build_approval_blocks(event, is_dm=False)  # type: ignore[arg-type]
    try:
        approval_ts = await slack.post_blocks(
            channel, blocks, "Manual approval required", thread_ts
        )
    except Exception:
        logger.warning(
            "Failed to post linked approval prompt to Slack (session=%s req=%s)",
            session_key,
            request_id,
            exc_info=True,
        )
        return None
    _linked_approvals[f"{channel}:{approval_ts}"] = _LinkedApproval(request_id, session_key)
    return approval_ts


def resolve_linked_approval(channel: str, approval_ts: str) -> None:
    """Drop a linked-approval registry entry (after the dashboard resolved it)."""
    _linked_approvals.pop(f"{channel}:{approval_ts}", None)


async def _request_approval(
    slack: SlackClientOps,
    provider: LLMProvider,
    channel: str,
    thread_ts: str,
    event: LLMEvent,
    session_key: str = "",
    is_dm: bool = True,
) -> str:
    """Post approval buttons, wait for click, return 'approved' or 'rejected'."""
    blocks = _build_approval_blocks(event, is_dm=is_dm)
    # If posting the approval prompt fails, the ACP permission request would
    # otherwise be left unanswered — the subprocess blocks forever and every
    # later turn wedges behind it. Reject the tool before re-raising so the
    # turn unblocks and the caller's error path can run.
    try:
        approval_ts = await slack.post_blocks(
            channel, blocks, "Manual approval required", thread_ts
        )
    except Exception:
        await _reject_orphaned_tool(provider, event.request_id)
        raise

    key = f"{channel}:{approval_ts}"
    pending = _PendingApproval(provider, event.request_id, session_key)
    _pending_approvals[key] = pending

    try:
        outcome = await asyncio.wait_for(pending.future, timeout=_APPROVAL_TIMEOUT)
    except asyncio.TimeoutError:
        outcome = _OUTCOME_REJECTED
        await provider.reject_tool(event.request_id)
        Stats().inc_tool_denial()
    finally:
        _pending_approvals.pop(key, None)

    try:
        await slack.delete_message(channel, approval_ts)
    except Exception:
        status = "✅ Approved" if outcome == _OUTCOME_APPROVED else "🚫 Rejected"
        title_safe, _ = redact_exfiltration_urls(event.title)
        title_safe, _ = redact_credentials(title_safe)
        await _safe_update(slack, channel, approval_ts, f"🔐 *{title_safe}* — {status}")

    return outcome


async def handle_interaction(
    channel: str,
    msg_ts: str,
    action_id: str,
    user_id: str = "",
    thread_ts: str = "",
    slack: SlackClientOps | None = None,
    sessions: SessionManager | None = None,
) -> str | None:
    """Handle a Block Kit button click for tool approval.

    Supports four actions:
    - approve_tool: approve this one tool call
    - trust_tool: auto-approve all tools for this session (thread)
    - reject_tool: reject this tool call

    Security: rejects non-owner clicks. Trust requires DM channel
    (verified via conversations.info by the gateway caller).
    """

    # Deny-by-default: reject unless positively confirmed as allowed
    if not user_id or not is_allowed_user(user_id):
        logger.warning(
            "Rejecting interactive action from unauthorized user %s (action=%s)", user_id, action_id
        )
        sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.interactive.approval",
            outcome="denied",
            source="slack",
            resources=action_id,
            error="unauthorized user",
        )
        return None

    key = f"{channel}:{msg_ts}"

    # Linked-dashboard-slot approval: the dashboard's _run_chat owns the ACP
    # answer (it is parked on the slot's approval future). Resolve ONLY that
    # future here via state.resolve_approval — do NOT call approve_tool/reject
    # (that would answer the JSON-RPC request twice). Trust is not offered on
    # this path, so treat anything that isn't an explicit reject as approve.
    linked_entry = _linked_approvals.get(key)
    if linked_entry is not None:
        approved = action_id != _ACTION_REJECT
        resolved = False
        if _state._dashboard_state is not None and hasattr(
            _state._dashboard_state, "resolve_approval"
        ):
            try:
                resolved = bool(
                    _state._dashboard_state.resolve_approval(str(linked_entry.request_id), approved)  # type: ignore[attr-defined]
                )
            except Exception:
                logger.warning(
                    "Failed to resolve linked approval (req=%s)",
                    linked_entry.request_id,
                    exc_info=True,
                )
        _linked_approvals.pop(key, None)
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.approval_linked",
            outcome="allowed" if approved else "denied",
            source="slack",
            resources=linked_entry.session_key,
            error="" if resolved else "future_not_found",
        )
        if approved:
            Stats().inc_tool_approval()
        else:
            Stats().inc_tool_denial()
        return _ACTION_APPROVE if approved else _ACTION_REJECT

    pending = _pending_approvals.get(key)
    if not pending:
        # Approval already resolved (approved/rejected/timed out).
        # For trust clicks, still set trust using the thread as session key.
        # Replicate session_key derivation from handle_message: thread_ts,
        # then check for linked dashboard session override.
        if action_id == _ACTION_TRUST and thread_ts:
            if not is_allowed_user(user_id):
                logger.warning("Rejecting late trust click from non-allowed user %s", user_id)
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_late",
                    outcome="denied",
                    source="slack",
                    error="unauthorized user",
                )
                return None
            # Verify clicking user owns this thread (prevents privilege escalation)
            if not slack:
                logger.warning(
                    "Rejecting late trust click: cannot verify thread ownership (no slack client)"
                )
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_late",
                    outcome="denied",
                    source="slack",
                    error="no_slack_client",
                )
                return None
            try:
                msgs = await slack.fetch_thread_replies(channel, thread_ts, limit=1)
                thread_owner = msgs[0].get("user", "") if msgs else ""
            except Exception:
                logger.warning("Failed to verify thread ownership for %s", thread_ts, exc_info=True)
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_late",
                    outcome="denied",
                    source="slack",
                    error="thread_ownership_check_failed",
                )
                return None
            if not thread_owner or thread_owner != user_id:
                logger.warning("Rejecting late trust click: user %s is not thread owner", user_id)
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_late",
                    outcome="denied",
                    source="slack",
                    error="not_thread_owner",
                )
                return None
            from kiro_crew.session import SessionMap

            session_key = thread_ts
            try:
                linked = SessionMap().get_session_for_thread(thread_ts)
                if linked:
                    session_key = linked
            except Exception:
                logger.warning(
                    "SessionMap lookup failed for thread %s; refusing to grant trust",
                    thread_ts,
                    exc_info=True,
                )
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_late",
                    outcome="denied",
                    source="slack",
                    error="session_map_lookup_failed",
                )
                return None
            _trusted_sessions.add(session_key)
            # Propagate to subagents: the subagent loop reads
            # get_approval_policy(parent)=="auto" (see subagent.py), so the
            # in-memory _trusted_sessions set alone never reaches them.
            if sessions is not None:
                sessions.set_approval_policy(session_key, "auto")
            logger.info("Trust mode ON (late click) for session %s", session_key)
            sel().log_api_access(
                caller=user_id,
                operation="slack.interactive.trust_late",
                outcome="allowed",
                source="slack",
                resources=session_key,
            )
            return _ACTION_TRUST
        else:
            logger.warning("No pending approval for %s", key)
            sel().log_api_access(
                caller=user_id or "unknown",
                operation="slack.interactive.approval",
                outcome="denied",
                source="slack",
                resources=key,
                error="no_pending_approval",
            )
        return None

    if action_id in (_ACTION_APPROVE, _ACTION_TRUST):
        # Set trust state BEFORE approving (so subsequent tools auto-approve)
        if action_id == _ACTION_TRUST:
            if not is_allowed_user(user_id):
                logger.error("Rejecting trust escalation from non-allowed user %s", user_id)
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_denied",
                    outcome="denied",
                    source="slack",
                    resources=pending.session_key or "",
                    error="non-allowed user",
                )
                if not pending.future.done():
                    pending.future.set_result(_OUTCOME_REJECTED)
                del _pending_approvals[key]
                return _ACTION_REJECT
            elif pending.session_key:
                _trusted_sessions.add(pending.session_key)
                if sessions is not None:
                    sessions.set_approval_policy(pending.session_key, "auto")
                logger.info("Trust mode ON for session %s", pending.session_key)
            else:
                logger.warning(
                    "No session_key on pending approval %s; approving without trust", key
                )
        if pending.provider:
            await pending.provider.approve_tool(pending.request_id)
        if not pending.future.done():
            pending.future.set_result(_OUTCOME_APPROVED)
        Stats().inc_tool_approval()
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.approval",
            outcome="allowed",
            source="slack",
            resources=action_id,
        )
    else:
        if pending.provider:
            await pending.provider.reject_tool(pending.request_id)
        if not pending.future.done():
            pending.future.set_result(_OUTCOME_REJECTED)
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.approval",
            outcome="denied",
            source="slack",
            resources=action_id,
        )

    del _pending_approvals[key]
    return action_id


def _build_approval_blocks(event: LLMEvent, is_dm: bool = True, source: str = "") -> list[dict]:
    """Build Block Kit blocks for tool approval prompt.

    Args:
        event: The permission-request event from the LLM provider.
        is_dm: True when posting to a DM (adds Trust button).
        source: Optional label for background agents (e.g. "subagent",
            "cron").  Prefixed to the header so users can tell main-agent
            approvals apart from background ones.

    Shows the full command text (from tool_input) in a code block so users
    can see exactly what will run before approving.  Falls back to the
    truncated title when tool_input is unavailable.

    In DMs: Approve / Trust / Reject
    In group channels: Approve / Reject only (Trust excluded
    to limit blast radius — it escalates permissions for the session).
    YOLO is owner-only via ``!yolo on`` command — no button.
    """
    # Slack Block Kit requires button `value` to be a string. ACP backends
    # (e.g. claude-agent-acp) issue integer JSON-RPC request ids, so coerce —
    # an int value makes Slack reject the whole post with `invalid_blocks`.
    # The interactive handler matches on channel:msg_ts and acts on the stored
    # `_PendingApproval.request_id`, so the button value itself is display-only.
    req_value = str(event.request_id)
    buttons: list[dict] = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve"},
            "style": "primary",
            "action_id": _ACTION_APPROVE,
            "value": req_value,
        },
    ]
    if is_dm:
        buttons.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Trust session"},
                "action_id": _ACTION_TRUST,
                "value": req_value,
            },
        )
    buttons.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Reject"},
            "style": "danger",
            "action_id": _ACTION_REJECT,
            "value": req_value,
        },
    )

    blocks: list[dict] = []

    tag = f"[{source}] " if source else ""
    title_safe, _ = redact_exfiltration_urls(event.title)
    title_safe, _ = redact_credentials(title_safe)
    footer = f":lock: {tag}*{title_safe}*"
    if event.tool_purpose:
        purpose, _ = redact_exfiltration_urls(event.tool_purpose)
        purpose, _ = redact_credentials(purpose)
        footer += f" — {purpose}"

    # When full tool_input is available, show a simple header and the
    # complete command in a code block below.
    # When tool_input is missing, fall back to the truncated title.
    if event.tool_input:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🔐 *{tag}Tool approval requested:*"},
            },
        )
        # Security: scan for exfiltration URLs and credentials before posting
        sanitized, _ = redact_exfiltration_urls(event.tool_input)
        sanitized, _ = redact_credentials(sanitized)
        # Truncate with marker if exceeds Slack limit
        if len(sanitized) > _SLACK_SECTION_TEXT_LIMIT:
            detail = (
                sanitized[: _SLACK_SECTION_TEXT_LIMIT - len(_TRUNCATION_MARKER)]
                + _TRUNCATION_MARKER
            )
        else:
            detail = sanitized
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{detail}```"},
            },
        )

    blocks.append({"type": "actions", "elements": buttons})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks
