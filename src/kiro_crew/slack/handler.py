"""Message handler — streams LLM responses to Slack with tool approval UI.

Routes incoming Slack messages through hooks, cron command interception,
and the LLM provider.  Supports interactive tool approval via Block Kit
buttons.

Session privacy modes
---------------------
Temporary (blank-slate): no memory reads, no memory writes, no persistence.
    The session starts with zero context and discards everything on close.
Incognito: memory reads allowed but writes blocked; persists an ephemeral
    conversation log that is discarded on close.

Both modes are tracked via bounded LRU dicts (``_thread_temporary`` and
``_thread_incognito``, keyed by session_key).  Use :func:`_is_slack_restricted`
to check whether a Slack session should skip memory writes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

from kiro_crew.acp.client import AcpError, AcpProcessDied, AcpPromptBusy, AcpTimeoutError
from kiro_crew.acp.types import STOP_REASON_CANCELLED, STOP_REASON_END_TURN
from kiro_crew.config.loader import ACTIVATION_REVIEW, KiroCrewConfig, config_dir
from kiro_crew.context import (
    ContextBuilder,
    build_cancelled_turn_preamble,
    compress_thread_history,
    window_for_provider_client,
)
from kiro_crew.cron import CronService
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.hooks import HOOK_REPLY, TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.llm_helpers import record_interaction_event, save_conversation_turn
from kiro_crew.messaging.link import canonical_key
from kiro_crew.platform import current_context
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    LLMProvider,
)
from kiro_crew.security import StreamRedactor, redact, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import SessionManager
from kiro_crew.slack import handler_state as _state
from kiro_crew.slack.agent_resolution import (  # noqa: F401
    _CC_AGENT_NAME_RE,
    _REVIEW_DRAFT_MAX,
    _REVIEW_DRAFT_TTL,
    _REVIEW_PLACEHOLDER_TS,
    _cached_default_agent,
    _discover_project_agents,
    _get_agent_for_session,
    _get_default_agent,
    _hydrate_thread_overrides,
    _iter_cc_agent_names,
    _list_all_agent_names,
    _persist_channel_config,
    _resolve_agent_name,
    _resolve_cc_agent_name,
    _review_drafts,
    _review_drafts_get,
    _review_drafts_pop,
    _review_drafts_set,
    _set_default_agent,
)
from kiro_crew.slack.approvals import (  # noqa: F401
    _ACTION_APPROVE,
    _ACTION_REJECT,
    _ACTION_TRUST,
    _APPROVAL_TIMEOUT,
    _OUTCOME_APPROVED,
    _OUTCOME_REJECTED,
    _SLACK_SECTION_TEXT_LIMIT,
    _TRUNCATION_MARKER,
    _build_approval_blocks,
    _linked_approvals,
    _LinkedApprovalEvent,
    _pending_approvals,
    _reject_orphaned_tool,
    _request_approval,
    _should_auto_approve_spawn,
    handle_interaction,
    post_linked_approval,
    resolve_linked_approval,
)
from kiro_crew.slack.blocks import build_working_blocks
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.format import (
    _convert_tables,
    split_message,
    strip_thinking_tags,
    to_slack_mrkdwn,
)
from kiro_crew.slack.handler_state import (  # noqa: F401
    _TITLED_THREADS_MAX,
    _YOLO_TTL_SECS,
    APPROVAL_AUTO,
    APPROVAL_INTERACTIVE,
    _allowed_users,
    _dashboard_state,
    _hydrated_sessions,
    _LinkedApproval,
    _mark_titled,
    _open_channels,
    _orch_cfg,
    _owner_id,
    _PendingApproval,
    _reload_orch_cfg,
    _thread_agents,
    _thread_projects,
    _titled_threads,
    _tracking_channels,
    _trusted_sessions,
    _vc,
    _VoiceConfig,
    add_trusted_session,
    disable_yolo,
    enable_yolo_with_ttl,
    is_allowed_user,
    is_open_channel,
    is_owner,
    is_slack_session_trusted,
    is_tracked_channel,
    is_yolo_mode,
    set_allowed_users,
    set_dashboard_state,
    set_open_channels,
    set_orch_cfg,
    set_owner_id,
    set_tracking_channels,
    set_yolo_mode,
)
from kiro_crew.slack.keyword_commands import (  # noqa: F401
    _do_spawn,
    _handle_cron_command,
    _handle_run_command,
    _handle_sessions_command,
    _handle_spawn_command,
    _remove_all_jobs,
    _safe_final_update,
    _safe_update,
)
from kiro_crew.slack.privacy_modes import (  # noqa: F401
    _INCOGNITO_TOKEN_RE,
    _RESTRICTED_WRITE_MSG,
    _TEMPORARY_TOKEN_RE,
    _THREAD_INCOGNITO_MAX,
    _THREAD_TEMPORARY_MAX,
    _apply_incognito_modifier,
    _apply_temporary_modifier,
    _conv_state_map,
    _hydrate_conv_flags,
    _is_slack_restricted,
    _mark_incognito,
    _mark_temporary,
    _strip_incognito_token,
    _strip_temporary_token,
    _thread_incognito,
    _thread_temporary,
    is_thread_incognito,
    is_thread_temporary,
    maybe_apply_privacy_modifiers,
)
from kiro_crew.slack.slash_commands import (  # noqa: F401
    _BANG_TO_SLASH,
    MessageContext,
    _append_footer_actions,
    _filter_options_brackets,
    _handle_compact_command,
    _handle_slash_command,
    _safe_voice_reply,
    build_timing_footer,
    maybe_handle_keyword_command,
    maybe_route_linked_thread,
)
from kiro_crew.slack.status_reactions import (  # noqa: F401
    _CODING_KINDS,
    _CODING_TOOLS,
    _DEFAULT_PHASE_EMOJIS,
    _IMMEDIATE_PHASES,
    _PHASE_DEBOUNCE_SECS,
    _PHASE_EMOJIS,
    _STALL_EMOJI_HARD,
    _STALL_EMOJI_SOFT,
    _STALL_HARD_SECS,
    _STALL_SOFT_SECS,
    _TERMINAL_PHASES,
    _THINKING_PREVIEW_LIMIT,
    _WEB_KINDS,
    _WEB_TOOLS,
    StatusReactionController,
    _add_phase_reaction,
    _build_phase_emojis,
    _condense_thinking,
    _tool_to_phase,
)
from kiro_crew.stats import Stats
from kiro_crew.subagent import SubagentManager
from kiro_crew.task import Task
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.voice_reply import is_available as _tts_available

logger = logging.getLogger(__name__)

__all__ = [
    "APPROVAL_AUTO",
    "APPROVAL_INTERACTIVE",
    "MessageContext",
    "StatusReactionController",
    "_ACTION_APPROVE",
    "_ACTION_REJECT",
    "_ACTION_TRUST",
    "_APPROVAL_TIMEOUT",
    "_BANG_TO_SLASH",
    "_CC_AGENT_NAME_RE",
    "_CODING_KINDS",
    "_CODING_TOOLS",
    "_CURSOR",
    "_DEFAULT_PHASE_EMOJIS",
    "_EDIT_INTERVAL",
    "_IMMEDIATE_PHASES",
    "_INCOGNITO_TOKEN_RE",
    "_LinkedApproval",
    "_LinkedApprovalEvent",
    "_NO_RESPONSE",
    "_OUTCOME_APPROVED",
    "_OUTCOME_REJECTED",
    "_PHASE_DEBOUNCE_SECS",
    "_PHASE_EMOJIS",
    "_PendingApproval",
    "_RESTRICTED_WRITE_MSG",
    "_REVIEW_DRAFT_MAX",
    "_REVIEW_DRAFT_TTL",
    "_REVIEW_PLACEHOLDER_TS",
    "_SLACK_SECTION_TEXT_LIMIT",
    "_STALL_EMOJI_HARD",
    "_STALL_EMOJI_SOFT",
    "_STALL_HARD_SECS",
    "_STALL_SOFT_SECS",
    "_STATUS_WORKING",
    "_TEMPORARY_TOKEN_RE",
    "_TERMINAL_PHASES",
    "_THINKING",
    "_THINKING_PLACEHOLDER",
    "_THINKING_PREVIEW_LIMIT",
    "_THREAD_INCOGNITO_MAX",
    "_THREAD_TEMPORARY_MAX",
    "_TITLED_THREADS_MAX",
    "_TRUNCATION_MARKER",
    "_VoiceConfig",
    "_WEB_KINDS",
    "_WEB_TOOLS",
    "_YOLO_TTL_SECS",
    "_add_phase_reaction",
    "_allowed_users",
    "_append_footer_actions",
    "_apply_incognito_modifier",
    "_apply_temporary_modifier",
    "_auto_title_lock",
    "_background_tasks",
    "_build_approval_blocks",
    "_build_phase_emojis",
    "_build_title_prompt",
    "_cached_default_agent",
    "_condense_thinking",
    "_conv_state_map",
    "_dashboard_state",
    "_discover_project_agents",
    "_do_spawn",
    "_filter_options_brackets",
    "_get_agent_for_session",
    "_get_auto_title_lock",
    "_get_default_agent",
    "_handle_compact_command",
    "_handle_cron_command",
    "_handle_run_command",
    "_handle_sessions_command",
    "_handle_slash_command",
    "_handle_spawn_command",
    "_hydrate_conv_flags",
    "_hydrate_thread_overrides",
    "_hydrated_sessions",
    "_is_slack_restricted",
    "_iter_cc_agent_names",
    "_linked_approvals",
    "_list_all_agent_names",
    "_mark_incognito",
    "_mark_temporary",
    "_mark_titled",
    "_maybe_auto_title_slack",
    "_open_channels",
    "_orch_cfg",
    "_owner_id",
    "_pending_approvals",
    "_persist_channel_config",
    "_reject_orphaned_tool",
    "_reload_orch_cfg",
    "_remove_all_jobs",
    "_request_approval",
    "_resolve_agent_name",
    "_resolve_cc_agent_name",
    "_review_drafts",
    "_review_drafts_get",
    "_review_drafts_pop",
    "_review_drafts_set",
    "_safe_final_update",
    "_safe_update",
    "_safe_voice_reply",
    "_set_default_agent",
    "_should_auto_approve_spawn",
    "_strip_incognito_token",
    "_strip_temporary_token",
    "_thread_agents",
    "_thread_incognito",
    "_thread_projects",
    "_thread_temporary",
    "_titled_threads",
    "_tool_to_phase",
    "_tracking_channels",
    "_trusted_sessions",
    "_vc",
    "add_trusted_session",
    "build_timing_footer",
    "cancel_background_tasks",
    "disable_yolo",
    "enable_yolo_with_ttl",
    "handle_interaction",
    "handle_message",
    "is_allowed_user",
    "is_open_channel",
    "is_owner",
    "is_slack_session_trusted",
    "is_thread_incognito",
    "is_thread_temporary",
    "is_tracked_channel",
    "is_yolo_mode",
    "logger",
    "maybe_apply_privacy_modifiers",
    "maybe_handle_keyword_command",
    "maybe_route_linked_thread",
    "post_linked_approval",
    "resolve_linked_approval",
    "set_allowed_users",
    "set_dashboard_state",
    "set_open_channels",
    "set_orch_cfg",
    "set_owner_id",
    "set_tracking_channels",
    "set_yolo_mode",
]


# Min interval between Slack message edits (avoid rate limits)
_EDIT_INTERVAL = 1.0

# Slack UX strings
_THINKING = "_Thinking…_"
_THINKING_PLACEHOLDER = "💭 _Thinking…_"
_CURSOR = " ▍"
_NO_RESPONSE = "_No response._"
_STATUS_WORKING = "is working on your request"


# Background tasks kept alive to prevent GC mid-execution.
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


def cancel_background_tasks() -> None:
    """Cancel pending background tasks during gateway shutdown."""
    for t in _background_tasks:
        t.cancel()
    _background_tasks.clear()


async def handle_message(
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    text: str,
    thread_ts: str | None,
    msg_ts: str,
    user_id: str,
    team_id: str = "",
    approval_mode: str = APPROVAL_AUTO,
    context_builder: ContextBuilder | None = None,
    cron_service: CronService | None = None,
    conversation_log: ConversationLog | None = None,
    consolidator: HistoryConsolidator | None = None,
    subagent_manager: SubagentManager | None = None,
    task_runner: TaskRunner | None = None,
    channel_agent: str | None = None,
    user_display_name: str | None = None,
    action_context: str | None = None,
    from_trusted_bot: bool = False,
    channel_activation: str | None = None,
    had_voice_input: bool = False,
) -> None:
    """Route a Slack message through ACP with streaming and tool approval.

    NOTE: ``from_trusted_bot`` is consumed only in the error path (echo-loop
    suppression). Early-reply paths (hook auto-reply, !status, !sessions) still
    post to Slack unconditionally — safe today because trusted bots send
    structured commands (``[TASK:id]``, ``[ACK:id]``) that don't match those
    patterns. Extend if that assumption changes.

    This function accepts individual parameters for backward compatibility.
    New callers can use ``MessageContext`` to group the service parameters.

    *channel_agent* overrides the default agent for this channel (set via
    per-channel config in ``slack.channels``).
    """
    Stats().inc_message_received()
    _t0 = time.monotonic()
    # reply_ts is the true Slack thread timestamp (used for posting replies and
    # as the key of thread-indexed maps like SessionMap._thread_to_session and
    # dashboard _slack_to_slot). session_key is the namespaced form used for
    # everything session-scoped (registry, conversation log, thread overrides).
    # Deriving the canonical form HERE keeps the key stable across messages:
    # previously the first message ran under the bare thread_ts while the
    # second was rewritten to ``slack:<ts>`` by the linked-thread routing below
    # (the self-link canonicalizes), splitting the live session, the
    # conversation log, and the per-thread override maps across two keys.
    reply_ts = thread_ts or msg_ts
    session_key = canonical_key(reply_ts)
    _hydrate_thread_overrides(session_key, conversation_log)
    _hydrate_conv_flags(sessions, session_key)

    # Resolve agent early so ALL persist paths (hook auto-reply, command
    # intercepts, review-mode drafts, main LLM path) can forward it.
    _agent = _thread_agents.get(session_key) or channel_agent or _get_default_agent() or None

    # ── Linked thread intercept: route to dashboard slot if linked ──
    if await maybe_route_linked_thread(text, session_key, user_id, channel, slack, reply_ts):
        return
    logger.info(
        "🔍 handle_message: thread_ts=%s msg_ts=%s → session_key=%s channel=%s",
        thread_ts,
        msg_ts,
        session_key,
        channel,
    )

    # ── Hook: check for auto-reply before touching ACP ──
    if context_builder:
        hook_result = context_builder.hooks.on_message(text)
        if hook_result.action == HOOK_REPLY:
            await slack.post_message(channel, hook_result.text, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                save_conversation_turn(
                    conversation_log,
                    session_key,
                    text,
                    hook_result.text,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return

    # ── Status keyword: reply with stats summary ──
    if text.strip().lower() == "status":
        # Identity status via the active PlatformContext (Default == OSS no-op
        # stub returning ""; Amazon companion returns the real Midway line).
        mw_line = await current_context().identity.status_line(prefix=" · midway")
        await slack.post_message(channel, Stats().summary() + mw_line, reply_ts)
        return

    # ── Sessions keyword: list recent sessions ──
    if text.strip().lower() == "sessions":
        if is_owner(user_id) or is_allowed_user(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.sessions_command",
                outcome="allowed",
                source="slack",
                resources=channel,
            )
            await _handle_sessions_command(
                text.strip(),
                slack,
                channel,
                reply_ts,
                msg_ts,
                session_key,
                conversation_log,
                sessions=sessions,
            )
        else:
            # Deny-by-default: unauthorized callers must be audited (so the
            # security pipeline can see attempted access) and given an
            # explicit denial — silent return masks the access attempt.
            sel().log_api_access(
                caller=user_id,
                operation="slack.sessions_command",
                outcome="denied",
                source="slack",
                resources=channel,
                error="unauthorized caller",
            )
            await slack.post_message(channel, "_Permission denied._", reply_ts)
        return

    # ── Compact keyword: trigger in-place context compaction ──

    _cmd_text = re.sub(r"^<@[A-Z0-9]+(?:\|[^>]*)?>\s*", "", text.strip())

    # ── !temporary / !incognito privacy modifiers (shared with transport) ──
    text, _cmd_text, _only_modifier = await maybe_apply_privacy_modifiers(
        text, _cmd_text, session_key, user_id, channel, slack, sessions, reply_ts
    )
    if _only_modifier:
        return

    if _cmd_text.strip().lower() == "!compact":
        if is_owner(user_id) or is_allowed_user(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.compact_command",
                outcome="allowed",
                source="slack",
                resources=channel,
            )
            await _handle_compact_command(slack, sessions, channel, reply_ts, msg_ts, session_key)
            return
        else:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="denied",
                error=f"unauthorized user {user_id}",
            )
            await slack.post_message(channel, "⛔ Not authorized to compact.", reply_ts)
            return  # deny-by-default: do not fall through

    # ── Owner commands: all "!" prefixed messages are reserved for owner ──
    # Strip leading bot mention from app_mention events so the ! prefix is exposed.
    # DM:       "!agent foo"                    → "!agent foo"       (no-op)
    # @mention: "<@UBOT|kirocrew> !agent foo"   → "!agent foo"      (strip prefix)
    if _cmd_text.startswith("!"):
        # !dashboard and !stop are available to any allowed user
        _cmd_word = _cmd_text.split()[0]
        if _cmd_word in ("!dashboard", "!stop", "!title"):
            if is_owner(user_id) or is_allowed_user(user_id):
                reply = await _handle_slash_command(
                    _cmd_text,
                    slack,
                    sessions,
                    channel,
                    reply_ts,
                    msg_ts,
                    session_key,
                    user_id,
                    conversation_log=conversation_log,
                )
                if reply is not None:
                    return
            else:
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.allowed_command",
                    outcome="denied",
                    source="slack",
                    resources=_cmd_word,
                    error="unauthorized sender",
                )
                await slack.post_message(channel, "⛔ Not authorized.", reply_ts)
                return
        # All other ! commands are owner-only
        elif not is_owner(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.owner_command",
                outcome="denied",
                source="slack",
                resources=_cmd_word,
                error="unauthorized sender",
            )
            await slack.post_message(channel, "⛔ Owner-only command.", reply_ts)
            return
        else:
            reply = await _handle_slash_command(
                _cmd_text,
                slack,
                sessions,
                channel,
                reply_ts,
                msg_ts,
                session_key,
                user_id,
                conversation_log=conversation_log,
            )
            if reply is not None:
                return

    # ── Path-independent keyword commands: spawn/run/cron ──
    # ``sessions`` is deliberately excluded here (handle_sessions=False): the
    # native path keeps its own earlier ``sessions`` block above so that the
    # ``!temporary``/``!incognito`` modifier rewrites can't turn a modified
    # message into a bare ``sessions`` match. The transport path (which has no
    # modifier machinery) handles all four via the same helper.
    if await maybe_handle_keyword_command(
        text,
        slack,
        sessions,
        channel,
        reply_ts,
        msg_ts,
        session_key,
        user_id,
        conversation_log,
        subagent_manager=subagent_manager,
        task_runner=task_runner,
        cron_service=cron_service,
        handle_sessions=False,
        channel_agent=channel_agent,
    ):
        return

    status_ctrl = StatusReactionController(
        slack,
        channel,
        msg_ts,
        enabled=KiroCrewConfig.load().slack.reactions_enabled,
    )
    status_ctrl.set_phase("queued")
    _had_error = False
    _stop_reason = ""

    # Set assistant thread status while we wait for the LLM to respond.
    # Defer start_stream until the first text chunk arrives so the user
    # sees the status indicator instead of a blank bot message.
    await slack.set_thread_status(channel, reply_ts, _STATUS_WORKING)

    # Post inline stop button (only in threaded conversations to avoid breaking tests)
    _working_ts: str | None = None
    if thread_ts:

        _working_ts = await slack.post_blocks(
            channel, build_working_blocks(session_key), "Working…", reply_ts
        )

    use_slack_stream = False
    stream_ts: str | None = None
    thinking_ts: str | None = None  # 💭 reasoning placeholder, posted above the answer
    _show_thinking = KiroCrewConfig.load().slack.show_thinking
    _stream_had_redaction = False  # True when per-chunk redaction modified a streamed chunk
    # Rolling-buffer redactor for the live Slack wire: withholds the trailing
    # credential-class run so a credential split across streaming chunks can't
    # reach Slack unredacted (issue 3). The final message is posted from the
    # complete, fully-redacted `accumulated`, so the held tail is superseded at
    # stop_stream — no data loss.
    _sred = StreamRedactor()
    accumulated = ""
    thinking_accumulated = ""
    stream_buffer = ""  # unsent chunks for streaming API (buffered between rate-limited appends)
    bracket_hold = ""  # text held back from '[' until ']' to filter [OPTIONS: ...]
    last_edit = 0.0
    _task_counter = 0  # incrementing task ID for task cards
    _active_task_id = ""  # current in-progress task
    _active_task_title = ""  # display title (purpose or tool name)
    _tool_start_time = 0.0  # monotonic time when current tool started
    _tool_timer_task: asyncio.Task | None = None  # periodic elapsed-time updater
    _status_dirty = False  # True when status needs reset to base on next text chunk
    _tool_gap = False

    async def _rotate_stream() -> str | None:
        """Stop the dead stream and start a fresh one. Returns new ts or None."""
        nonlocal stream_ts, use_slack_stream
        if stream_ts:
            await slack.stop_stream(channel, stream_ts)
        new_ts = await slack.start_stream(
            channel, reply_ts, team_id=team_id or None, user_id=user_id or None
        )
        if new_ts:
            stream_ts = new_ts
            logger.info("Stream rotated: new ts=%s", new_ts)
        else:
            use_slack_stream = False
            logger.warning("Stream rotation failed — falling back to chat.update")
        return new_ts

    async def _append_stream(text: str) -> bool:
        """Append text to stream, rotating on failure.

        Streams through the rolling redactor (``_sred``) so a credential split
        across streaming chunks can't reach Slack unredacted (issue 3): only the
        confirmed-safe prefix is sent now; the trailing (possible-partial-
        credential) run is withheld until the next append. The final message is
        posted from the complete, fully-redacted ``accumulated`` at stop_stream,
        so the withheld tail is superseded — never lost.
        """
        nonlocal _stream_had_redaction
        if not stream_ts:
            return True
        if channel_activation == ACTIVATION_REVIEW:
            return True  # Suppress streaming text in review mode
        safe = _sred.feed(text)  # redacts the confirmed-safe prefix internally
        if not safe:
            return True  # whole delta withheld (partial credential) — nothing to send yet
        if "[REDACTED" in safe:
            _stream_had_redaction = True
        ok = await slack.append_stream(channel, stream_ts, safe)
        if not ok and use_slack_stream:
            if await _rotate_stream():
                assert stream_ts is not None
                return await slack.append_stream(channel, stream_ts, safe)
        return ok

    async def _append_task(task_id: str, title: str, status: str, details: str = "") -> bool:
        """Append task card to stream, rotating on failure."""
        if not stream_ts:
            return False
        if channel_activation == ACTIVATION_REVIEW:
            return True  # Suppress task cards in review mode
        ok = await slack.append_task(channel, stream_ts, task_id, title, status, details=details)
        if not ok and use_slack_stream:
            if await _rotate_stream():
                assert stream_ts is not None
                return await slack.append_task(
                    channel, stream_ts, task_id, title, status, details=details
                )
        return ok

    async def _tool_elapsed_updater() -> None:
        """Periodically update the active task card with elapsed time (every 30s)."""
        # reads _active_task_id/_active_task_title/_tool_start_time from the
        # enclosing scope (no rebind here, so no nonlocal needed)
        while True:
            await asyncio.sleep(30)
            if _active_task_id and _tool_start_time and use_slack_stream:
                elapsed = time.monotonic() - _tool_start_time
                mins, secs = divmod(int(elapsed), 60)
                time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
                # Elapsed goes in the TITLE (Slack replaces title on same
                # task_id) — NOT details, which Slack APPENDS, causing the
                # "⏱ 30s ⏱ 1m 0s ⏱ 1m 30s" accumulation bug.
                await _append_task(
                    _active_task_id,
                    f"{_active_task_title}  ⏱ {time_str}",
                    "in_progress",
                )

    def _start_tool_timer() -> None:
        """Start the 30s elapsed-time updater for the current tool."""
        nonlocal _tool_timer_task, _tool_start_time
        _cancel_tool_timer()
        _tool_start_time = time.monotonic()
        _tool_timer_task = asyncio.ensure_future(_tool_elapsed_updater())

    def _cancel_tool_timer() -> None:
        """Cancel the tool elapsed-time updater."""
        nonlocal _tool_timer_task
        if _tool_timer_task and not _tool_timer_task.done():
            _tool_timer_task.cancel()
        _tool_timer_task = None

    def _tool_elapsed_str() -> str:
        """Return formatted elapsed time for the current tool, or empty string."""
        if not _tool_start_time:
            return ""
        elapsed = time.monotonic() - _tool_start_time
        if elapsed < 1:
            return ""
        mins, secs = divmod(elapsed, 60)
        if mins:
            return f"⏱ {int(mins)}m {secs:.1f}s"
        return f"⏱ {secs:.1f}s"

    async def _ensure_stream_started() -> None:
        """Lazy-start the stream on first event. Falls back to chat.update."""
        nonlocal stream_ts, use_slack_stream, thinking_ts
        if stream_ts is not None:
            return
        if channel_activation == ACTIVATION_REVIEW:
            # No visible message — only thread status indicator is shown
            stream_ts = _REVIEW_PLACEHOLDER_TS
            use_slack_stream = False
            return
        # Reserve the 💭 reasoning slot ABOVE the answer *before* the response
        # message is created (Mesh-1805). This must run regardless of which
        # event arrived first: if a text/tool event precedes the first
        # reasoning chunk, posting the placeholder here is the only way to keep
        # reasoning above the answer (the reasoning-chunk branch never got the
        # chance). Guarded on thinking_ts is None so we never double-post when
        # the reasoning branch already claimed the slot. An empty placeholder
        # (no reasoning this turn) is cleaned up at end of turn.
        if _show_thinking and thinking_ts is None:
            try:
                thinking_ts = await slack.post_message(channel, _THINKING_PLACEHOLDER, reply_ts)
            except Exception:
                logger.debug("Failed to reserve thinking slot", exc_info=True)
        stream_ts = await slack.start_stream(
            channel, reply_ts, team_id=team_id or None, user_id=user_id or None
        )
        use_slack_stream = stream_ts is not None
        if not use_slack_stream:
            stream_ts = await slack.post_message(channel, _THINKING, reply_ts)
        assert stream_ts is not None

    task = Task(id=msg_ts)
    _acquired = False

    # ── Bidirectional sync: check if this Slack thread is linked to a dashboard session ──
    # The thread index is keyed by the bare Slack thread_ts (reply_ts), NOT the
    # namespaced session key. A self-linked Slack thread resolves to our own
    # canonical key (no-op rewrite); a dashboard-linked thread resolves to its
    # ``dashboard:chat-N`` key.
    linked_session_key = sessions.get_session_for_thread(reply_ts)
    if linked_session_key and linked_session_key != session_key:
        logger.info(
            "🔗 Slack thread %s linked to dashboard session %s — routing there",
            session_key,
            linked_session_key,
        )
        session_key = linked_session_key

    client: LLMProvider | None = None
    try:
        task.start()
        # Re-resolve _agent against (possibly linked) session_key for the main
        # LLM path — linked dashboard sessions may carry a different thread agent.
        _agent = _thread_agents.get(session_key) or channel_agent or _get_default_agent() or None
        client, is_new, resumed = await sessions.get_or_create(
            session_key, agent=_agent, channel_id=channel
        )
        _acquired = True
        if is_new:
            await sessions.set_channel(session_key, channel)
        if not linked_session_key:
            # Self-link: thread index maps the bare Slack thread_ts to this
            # session's canonical key. reply_ts (not session_key) is the true
            # Slack timestamp — storing the namespaced key as slack_thread_ts
            # would corrupt reply routing.
            sessions.set_slack_link(session_key, reply_ts, channel)
        logger.info(
            "🔍 session state: key=%s is_new=%s resumed=%s",
            session_key,
            is_new,
            resumed,
        )

        # Write current session key so MCP tools can pass it to spawn API.
        # Keyed by kiro-cli PID to avoid races between concurrent sessions.
        try:
            pid = sessions.get_pid(session_key)
            if isinstance(pid, int):
                (config_dir() / f"session_pid_{pid}.txt").write_text(session_key, encoding="utf-8")
        except Exception:
            pass

        # Build message with context injection
        compressed: str | None = None
        # Scale the injected-context budget to the live model's context window
        # (200K model ⇒ one-fifth the memory/lessons/history chars of a 1M
        # model, same window share). Derived from the resolved session client;
        # Auto/unknown ⇒ None ⇒ the 1M reference (unchanged default).
        _model_window = window_for_provider_client(client)
        # is_new = new kiro-cli/dashboard process, NOT new conversation.
        # The Slack thread persists across processes, so we compress its
        # history to bootstrap the fresh session's context window.
        if is_new and not resumed and context_builder and context_builder.conversation_log:
            compressed = await compress_thread_history(
                context_builder.conversation_log,
                session_key,
                text,
                sessions,
                model_window=_model_window,
            )

        # After a soft-cancel, kiro-cli drops the cancelled turn from its
        # conversation log — but the user+assistant text is persisted to our
        # local conversation_log. Re-inject just the cancelled turn as a
        # preamble so the LLM remembers what was interrupted. Flag lives on
        # the session (set by SessionManager.stop_turn), consumed one-shot.
        # Use getattr for prev_turn_cancelled so test doubles (AsyncMock)
        # don't raise AttributeError on coroutine-returning mock chains.
        _session = getattr(sessions, "_sessions", {}).get(session_key)
        if (
            _session is not None
            and getattr(_session, "prev_turn_cancelled", False)
            and context_builder
            and context_builder.conversation_log
        ):
            _session.prev_turn_cancelled = False
            _preamble = build_cancelled_turn_preamble(context_builder.conversation_log, session_key)
            if _preamble:
                text = _preamble + "\n\n" + text

        # Fetch thread parent message when starting a new session in an
        # existing thread (e.g. replying to a cron thread).  Gives the LLM
        # context about what started the thread without requiring manual
        # batch_get_thread_replies.
        thread_parent_text: str | None = None
        if is_new and not resumed and thread_ts and context_builder:
            if not compressed:
                thread_parent_text = await slack.fetch_message(channel, thread_ts)
            if thread_parent_text:
                thread_parent_text = redact(thread_parent_text)
                if len(thread_parent_text) > 3000:
                    thread_parent_text = (
                        thread_parent_text[:3000]
                        + "\n[truncated — use batch_get_thread_replies for full text]"
                    )

        if context_builder:
            # Thread-scoped temporary mode: blocks memory reads.
            _slack_blocks_reads = is_thread_temporary(session_key)

            # Fallback thread metadata: when thread_parent_text is unavailable
            # (e.g. fetch_message failed), try conversations.replies to get parent info.
            # Note: requires channels:history (public) or groups:history (private, Level 3
            # High Risk on Amazon Slack). Gracefully degrades — if scope is missing, thread
            # context is simply skipped.
            _thread_meta: str | None = None
            if (
                is_new
                and not resumed
                and thread_ts
                and not thread_parent_text
                and not compressed
                and context_builder
            ):
                replies = await slack.fetch_thread_replies(
                    channel, thread_ts, limit=1, warn_on_pagination=False
                )
                if replies:
                    parent = replies[0]
                    reply_count = parent.get("reply_count", 0)
                    parent_text = redact(parent.get("text", ""))
                    if parent_text:
                        if len(parent_text) > 500:
                            parent_text = parent_text[:500] + "…[truncated]"
                        if reply_count > 0:
                            _thread_meta = (
                                f'[Thread has {reply_count} replies. Parent message: "{parent_text}"]\n'
                                "Use batch_get_thread_replies to read the full thread if needed.\n"
                            )
                        else:
                            _thread_meta = f'[Parent message: "{parent_text}"]\n'
                else:
                    logger.info(
                        "Thread fallback returned no replies for %s/%s (missing scope?)",
                        channel,
                        thread_ts,
                    )

            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                context_builder.build_message,
                text,
                is_new,
                session_key,
                channel_id=channel,
                thread_ts=thread_ts or msg_ts,
                agent=_agent,
                resumed=resumed,
                user_display_name=user_display_name,
                compressed_history=compressed,
                action_context=action_context,
                thread_parent_text=thread_parent_text,
                thread_meta=_thread_meta,
                blocks_reads=_slack_blocks_reads,
                model_window=_model_window,
            )
        else:
            full_message = text

        # ── Early cancellation check: bail before expensive LLM call ──
        if sessions.is_cancelled(session_key, msg_ts):
            logger.info("Message %s cancelled before LLM call — skipping", msg_ts)
            await slack.set_thread_status(channel, reply_ts, "")
            return

        async for event in client.stream(full_message):
            if event.kind == EVENT_TEXT_CHUNK:
                if _tool_gap and accumulated and accumulated[-1:] not in ("\n", " "):
                    first = event.text[:1]
                    if first and first not in ("\n", " "):
                        event.text = "\n\n" + event.text
                event.text, _exfil_w = redact_exfiltration_urls(event.text)
                event.text, _cred_w = redact_credentials(event.text)
                if _exfil_w or _cred_w:
                    _stream_had_redaction = True

                if event.text:
                    _tool_gap = False
                status_ctrl.set_phase("thinking")
                status_ctrl.on_progress()
                accumulated += event.text

                if _status_dirty and use_slack_stream:
                    await slack.set_thread_status(channel, reply_ts, _STATUS_WORKING)
                    _status_dirty = False

                # ── Bracket hold-back: filter [OPTIONS: ...] from stream ──
                # When inside a bracket, accumulate into bracket_hold.
                # On ']', release if not OPTIONS, suppress if it is.
                if use_slack_stream:
                    bracket_hold, stream_buffer = _filter_options_brackets(
                        event.text, bracket_hold, stream_buffer
                    )
                else:
                    stream_buffer += event.text

                await _ensure_stream_started()

                now = time.monotonic()
                if now - last_edit >= _EDIT_INTERVAL:
                    if use_slack_stream:
                        if stream_buffer:
                            stream_buffer, _ = strip_thinking_tags(
                                stream_buffer, strip_whitespace=False
                            )
                            await _append_stream(stream_buffer)
                            stream_buffer = ""
                    else:
                        assert stream_ts is not None
                        if channel_activation != ACTIVATION_REVIEW:
                            await _safe_update(
                                slack, channel, stream_ts, redact(accumulated) + _CURSOR
                            )
                    last_edit = now

            elif event.kind == EVENT_THINKING_CHUNK:
                status_ctrl.set_phase("thinking")
                status_ctrl.on_progress()
                thinking_accumulated += event.text
                # Claim the 💭 slot as soon as reasoning starts so it appears
                # promptly during a long thinking phase (early feedback). This
                # is an optimization for the common reasoning-first case; the
                # ordering guarantee itself lives in _ensure_stream_started,
                # which reserves the slot before the answer message whenever it
                # hasn't been claimed yet (handles text/tool-first turns).
                if (
                    _show_thinking
                    and thinking_ts is None
                    and stream_ts is None
                    and channel_activation != ACTIVATION_REVIEW
                ):
                    try:
                        thinking_ts = await slack.post_message(
                            channel, _THINKING_PLACEHOLDER, reply_ts
                        )
                    except Exception:
                        logger.debug("Failed to post thinking placeholder", exc_info=True)

            elif event.kind == EVENT_TOOL_CALL:
                _tool_gap = True
                # Check tool hooks. NOTE: EVENT_TOOL_CALL is informational —
                # the tool has already been auto-approved by the provider and
                # is executing; this branch cannot reject_tool(). The real
                # enforceable gate is EVENT_PERMISSION_REQUEST below. So we do
                # NOT arm deny-by-default here (is_shell omitted): a shell tool
                # with an unrecoverable command would otherwise render a
                # misleading "blocked" message while the tool actually runs.
                # A genuine deny-list / sensitive-path match still surfaces a
                # (best-effort, non-enforcing) warning + audit.
                if context_builder:
                    tool_result = context_builder.hooks.on_tool_call(
                        event.title,
                        session_key=session_key,
                        agent=_agent or "",
                        command=event.shell_command,
                    )
                    if tool_result.action == TOOL_DENY:
                        # event.title is LLM-authored (select_tool_title prefers
                        # the model's description) — never post it to Slack raw.
                        _flagged_title, _ = redact_exfiltration_urls(event.title)
                        _flagged_title, _ = redact_credentials(_flagged_title)
                        accumulated += (
                            f"\n⚠️ _Tool `{_flagged_title}` flagged by security "
                            f"hooks (already executing; cannot be stopped here)._"
                        )
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="slack",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="flagged_unenforceable",
                            error="hook_deny",
                        )
                        continue

                sel().log_tool_invocation(
                    session_key=session_key,
                    source="slack",
                    tool_name=event.title,
                    tool_kind=event.tool_kind,
                    outcome="invoked",
                )

                tool_name = event.title.removeprefix("Running: ")
                tool_name, _ = redact_exfiltration_urls(tool_name)
                tool_name, _ = redact_credentials(tool_name)
                tool_kind = event.tool_kind or ""
                status_ctrl.set_phase(_tool_to_phase(tool_name, tool_kind))
                status_ctrl.on_progress()
                tool_detail = event.tool_purpose or tool_kind
                tool_status = f"\n🫆 `{tool_name}`\n"
                await _ensure_stream_started()
                if use_slack_stream:
                    await slack.set_thread_status(channel, reply_ts, f"is using {tool_name}")
                    _status_dirty = True
                if use_slack_stream:
                    # Flush any buffered text before the tool status
                    if stream_buffer:
                        stream_buffer, _ = strip_thinking_tags(
                            stream_buffer, strip_whitespace=False
                        )
                        await _append_stream(stream_buffer)
                        stream_buffer = ""
                    # Mark previous task complete, start new one
                    if _active_task_id:
                        _elapsed = _tool_elapsed_str()
                        _cancel_tool_timer()
                        _ct = (
                            f"{_active_task_title}  {_elapsed}" if _elapsed else _active_task_title
                        )
                        await _append_task(_active_task_id, _ct, "complete")
                    _task_counter += 1
                    _active_task_id = f"tool_{_task_counter}"
                    _active_task_title = event.tool_purpose or tool_name
                    _active_task_title, _ = redact_exfiltration_urls(_active_task_title)
                    _active_task_title, _ = redact_credentials(_active_task_title)
                    await _append_task(
                        _active_task_id,
                        title=_active_task_title,
                        status="in_progress",
                        details=tool_name if tool_detail else "",
                    )
                    _start_tool_timer()
                else:
                    accumulated += tool_status
                    assert stream_ts is not None
                    if channel_activation != ACTIVATION_REVIEW:
                        await _safe_update(slack, channel, stream_ts, redact(accumulated) + _CURSOR)
                last_edit = time.monotonic()

                # wait tool blocks MCP for up to 30min — finalize the
                # streaming message now so Slack doesn't show an error.
                # _ensure_stream_started() will open a new message when
                # the next text chunk arrives after wait returns.
                if tool_name == "wait" and use_slack_stream and stream_ts:
                    if _active_task_id:
                        _elapsed = _tool_elapsed_str()
                        _cancel_tool_timer()
                        _ct = (
                            f"{_active_task_title}  {_elapsed}" if _elapsed else _active_task_title
                        )
                        await _append_task(_active_task_id, _ct, "complete")
                        _active_task_id = ""
                    await slack.stop_stream(channel, stream_ts)
                    stream_ts = None
                    accumulated = ""

            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Check tool hooks for auto-approve
                if context_builder:
                    tool_result = context_builder.hooks.on_tool_call(
                        event.title,
                        session_key=session_key,
                        agent=_agent or "",
                        tool_kind=event.tool_kind,
                        raw_params=event.raw_tool_params,
                        command=event.shell_command,
                        is_shell=event.is_shell,
                    )
                    if tool_result.action == TOOL_AUTO_APPROVE:
                        await client.approve_tool(event.request_id)
                        Stats().inc_tool_auto_approved()
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="slack",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="auto_approved",
                            request_id=event.request_id,
                            metadata={"reason": "hook_auto_approve"},
                        )
                        continue
                    if tool_result.action == TOOL_DENY:
                        await client.reject_tool(event.request_id)
                        Stats().inc_tool_denial()
                        # event.title is LLM-authored — redact before posting.
                        _blocked_title, _ = redact_exfiltration_urls(event.title)
                        _blocked_title, _ = redact_credentials(_blocked_title)
                        accumulated += f"\n🚫 _Tool `{_blocked_title}` blocked by hooks._"
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="slack",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="denied",
                            request_id=event.request_id,
                            error="hook_deny",
                        )
                        continue

                # auto_approve_subagent_spawn → auto-approve spawn_run tool calls
                if _should_auto_approve_spawn(context_builder, event.title or ""):
                    await client.approve_tool(event.request_id)
                    Stats().inc_tool_auto_approved()
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="slack",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                        request_id=event.request_id,
                        metadata={"reason": "auto_approve_subagent_spawn"},
                    )
                    continue

                if approval_mode == APPROVAL_AUTO:
                    await client.approve_tool(event.request_id)
                    Stats().inc_tool_auto_approved()
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="slack",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                        request_id=event.request_id,
                        metadata={"reason": "approval_mode_auto"},
                    )
                    continue

                # Trust mode (per-session) or YOLO mode (owner-only global) → auto-approve
                _yolo_now = is_yolo_mode()
                if _yolo_now or session_key in _trusted_sessions:
                    await client.approve_tool(event.request_id)
                    Stats().inc_tool_auto_approved()
                    logger.info(
                        "Auto-approved %s (%s)",
                        event.title,
                        "yolo" if _yolo_now else "trust",
                    )
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="slack",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                        request_id=event.request_id,
                        metadata={"reason": "yolo" if _yolo_now else "trust"},
                    )
                    continue

                logger.info("Permission request: tool=%s req_id=%s", event.title, event.request_id)
                status_ctrl.pause_stall_watchdog()
                task.await_approval()
                # The stream-prep Slack calls below run BEFORE _request_approval
                # answers the permission. If any raises (rate-limit, network),
                # the ACP permission request would be orphaned and the
                # subprocess would wedge — reject it before propagating so the
                # turn unblocks. _request_approval guards its own post failure.
                try:
                    await _ensure_stream_started()
                    if use_slack_stream:
                        await slack.set_thread_status(channel, reply_ts, "Waiting for approval…")
                        _status_dirty = True
                        # Flush buffered text before approval pause
                        if stream_buffer:
                            stream_buffer, _ = strip_thinking_tags(
                                stream_buffer, strip_whitespace=False
                            )
                            await _append_stream(stream_buffer)
                            stream_buffer = ""
                except Exception:
                    await _reject_orphaned_tool(client, event.request_id)
                    raise

                outcome = await _request_approval(
                    slack,
                    client,
                    channel,
                    reply_ts,
                    event,
                    session_key,
                    is_dm=channel.startswith("D"),
                )
                task.resume()
                status_ctrl.resume_stall_watchdog()
                sel().log_tool_invocation(
                    session_key=session_key,
                    source="slack",
                    tool_name=event.title,
                    tool_kind=event.tool_kind,
                    outcome="approved" if outcome != _OUTCOME_REJECTED else "rejected",
                    request_id=event.request_id,
                    metadata={"reason": "interactive"},
                )
                if outcome == _OUTCOME_REJECTED:
                    if use_slack_stream and _active_task_id:
                        _cancel_tool_timer()
                        assert stream_ts is not None
                        await _append_task(_active_task_id, _active_task_title, "error")
                        _active_task_id = ""
                    if not use_slack_stream:
                        accumulated += "\n🚫 _Tool use rejected._"
                    break

            elif event.kind == EVENT_COMPLETE:
                status_ctrl.on_progress()
                _stop_reason = event.stop_reason
                if (
                    _stop_reason
                    and _stop_reason != STOP_REASON_END_TURN
                    and _stop_reason != STOP_REASON_CANCELLED
                ):
                    logger.warning(
                        "Unexpected stop_reason %r for %s — treating as normal completion",
                        _stop_reason,
                        session_key,
                    )
                break

        if _stop_reason == STOP_REASON_CANCELLED:
            logger.info("Turn cancelled by user for %s", session_key)
            task.complete()
        else:
            task.complete()
            sessions.record_success(session_key)
            Stats().inc_message_success()
            # Per-interaction telemetry (PlatformContext seam) — shared helper so
            # the payload shape and model reflection cannot drift across surfaces.
            record_interaction_event(client, session_key, "slack")

        # Check context usage — fires background compaction at configured threshold, never blocks
        sessions.check_context_usage(session_key, client)

    except AcpTimeoutError as e:
        _had_error = True
        accumulated = e.partial_output or "⏱️ Request timed out. Please try again."
        task.fail("timeout")
        await sessions.record_failure(session_key)
        Stats().inc_timeout()
        Stats().inc_message_failed()
    except AcpProcessDied:
        _had_error = True
        accumulated = accumulated or "💀 Agent process died. Please try again."
        task.fail("process_died")
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    except AcpPromptBusy as e:
        _had_error = True
        # Session is wedged mid-prompt — reset the provider so the next
        # message cold-starts cleanly instead of hitting the same wall.
        try:
            await sessions.reset(session_key)
        except Exception:
            logger.debug("Failed to reset session %s after prompt-busy", session_key, exc_info=True)
        accumulated = f"❌ {e}"
        task.fail(str(e))
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    except AcpError as e:
        _had_error = True
        accumulated = f"❌ {e}"
        task.fail(str(e))
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    except Exception:
        _had_error = True
        logger.exception("Unexpected error handling message")
        accumulated = accumulated or "🔧 Something went wrong. Please try again."
        task.fail("unexpected")
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    finally:
        if _acquired:
            sessions.release(session_key)
        status_ctrl.finalize(error=_had_error)
        await asyncio.sleep(0)  # let finalize fire

    # ── Cancelled check: suppress response if message was deleted mid-flight ──
    if sessions.is_cancelled(session_key, msg_ts):
        logger.info("Message %s cancelled (deleted) — suppressing response", msg_ts)
        await slack.set_thread_status(channel, reply_ts, "")
        if stream_ts:
            try:
                await slack.delete_message(channel, stream_ts)
            except Exception:
                logger.debug("Failed to delete cancelled stream", exc_info=True)
        if thinking_ts:
            try:
                await slack.delete_message(channel, thinking_ts)
            except Exception:
                logger.debug("Failed to delete thinking placeholder", exc_info=True)
        if _working_ts:
            try:
                await slack.delete_message(channel, _working_ts)
            except Exception:
                pass
        return

    # Clear assistant thread status (skip in review mode — keep indicator until button press)
    if channel_activation != ACTIVATION_REVIEW:
        await slack.set_thread_status(channel, reply_ts, "")

    # Remove inline stop button
    if _working_ts:
        try:
            await slack.delete_message(channel, _working_ts)
        except Exception:
            pass

    # Suppress error replies for trusted bot messages to prevent echo loops
    if from_trusted_bot and _had_error:
        logger.info("Suppressing error reply to trusted bot message to prevent echo loop")
        if thinking_ts:
            try:
                await slack.delete_message(channel, thinking_ts)
            except Exception:
                logger.debug("Failed to delete thinking placeholder", exc_info=True)
        if conversation_log and not _is_slack_restricted(session_key):
            save_conversation_turn(
                conversation_log,
                session_key,
                text,
                "[suppressed: trusted bot error]",
                source_thread=session_key,
                source_user=user_id,
                agent=_agent,
            )
        return

    # Strip any inline <thinking> tags that leaked into the text
    if accumulated:
        accumulated, inline_thinking = strip_thinking_tags(accumulated)
        accumulated = accumulated.strip()
        if inline_thinking:
            thinking_accumulated += ("\n\n" if thinking_accumulated else "") + inline_thinking

    actually_streamed = use_slack_stream and bool(stream_ts)
    final_text = (
        to_slack_mrkdwn(accumulated, keep_tables=actually_streamed) if accumulated else _NO_RESPONSE
    )

    # Scan for URL exfiltration before posting to Slack (link previews auto-fetch)
    final_text, exfil_warnings = redact_exfiltration_urls(final_text)
    for w in exfil_warnings:
        logger.warning("Exfiltration URL redacted in response: %s", w)
    final_text, cred_warnings = redact_credentials(final_text)
    for w in cred_warnings:
        logger.warning("Credential redacted in response: %s", w)

    # Extract OPTIONS buttons from response and post as Block Kit
    from kiro_crew.slack.format import extract_options

    clean_text, options = extract_options(final_text)

    # ── Review mode: ephemeral draft instead of public post ──
    if channel_activation == ACTIVATION_REVIEW:
        from kiro_crew.slack.blocks import review_draft_blocks

        # Stop streaming, delete placeholder, set status indicator
        if stream_ts and stream_ts != _REVIEW_PLACEHOLDER_TS:
            if use_slack_stream:
                try:
                    await slack.stop_stream(channel, stream_ts)
                except Exception:
                    pass
            try:
                await slack.delete_message(channel, stream_ts)
            except Exception:
                logger.debug("Failed to delete stream msg in review mode", exc_info=True)
        await slack.set_thread_status(channel, reply_ts, "Awaiting review…")
        # Post ephemeral draft with approve/edit/cancel buttons
        draft = clean_text or _NO_RESPONSE
        draft_key = f"{channel}|{reply_ts}|{uuid.uuid4().hex[:8]}"
        blocks = review_draft_blocks(draft, draft_key)
        await slack.post_ephemeral(
            channel, user_id, draft, blocks=blocks, thread_ts=reply_ts if thread_ts else None
        )
        # Store draft for button handlers (requester can act on their own draft)
        _review_drafts_set(draft_key, draft, user_id)
        logger.info("Review mode: ephemeral draft sent to %s in %s", user_id, channel)
        # Persist conversation (draft counts as a turn)
        if conversation_log and not _is_slack_restricted(session_key):
            save_conversation_turn(
                conversation_log,
                session_key,
                text,
                accumulated,
                source_thread=session_key,
                source_user=user_id,
                agent=_agent,
            )
        return

    if use_slack_stream and stream_ts:
        # Mark last task complete
        if _active_task_id:
            _elapsed = _tool_elapsed_str()
            _cancel_tool_timer()
            _ct = f"{_active_task_title}  {_elapsed}" if _elapsed else _active_task_title
            await _append_task(_active_task_id, _ct, "complete")
        # Flush remaining buffer (bracket_hold excluded — it's either
        # a suppressed OPTIONS tag or an unclosed bracket we drop)
        if stream_buffer:
            stream_buffer, _ = strip_thinking_tags(stream_buffer, strip_whitespace=False)
            await _append_stream(stream_buffer)
        await slack.stop_stream(channel, stream_ts, clean_text or _NO_RESPONSE)

    if use_slack_stream and stream_ts:
        # Rich AI renderer is now locked in by stop_stream above.
        # Only overwrite via chat_update when redaction modified the text —
        # either per-chunk during streaming (_stream_had_redaction) or caught
        # by the final scan (exfil_warnings/cred_warnings). The security
        # invariant requires the final visible message reflect the redacted
        # accumulated text; all other cases leave the rich render intact.
        if _stream_had_redaction or exfil_warnings or cred_warnings:
            fallback_text = _convert_tables(clean_text) if clean_text else _NO_RESPONSE
            await _safe_final_update(
                slack, channel, stream_ts, fallback_text or _NO_RESPONSE, reply_ts
            )
    elif stream_ts:
        # Legacy fallback path (chat.startStream unavailable): replace the
        # "Thinking…" placeholder with the clean accumulated text.
        final_text = _convert_tables(clean_text) if clean_text else _NO_RESPONSE
        await _safe_final_update(slack, channel, stream_ts, final_text or _NO_RESPONSE, reply_ts)
    else:
        # No stream was started (e.g. no text chunks) — post the final text directly
        await slack.post_message(channel, clean_text or _NO_RESPONSE, reply_ts)

    # Render reasoning as a condensed, subdued blockquote (Mesh-1805). When a
    # placeholder was posted above the answer, update it in place so the thread
    # reads reasoning → answer. Otherwise (the stream started before any
    # reasoning arrived) fall back to a post after the answer.
    if thinking_accumulated and _show_thinking:
        thinking_mrkdwn = to_slack_mrkdwn(thinking_accumulated)
        thinking_mrkdwn, exfil_warnings = redact_exfiltration_urls(thinking_mrkdwn)
        for w in exfil_warnings:
            logger.warning("Exfiltration URL redacted in thinking: %s", w)
        thinking_mrkdwn, cred_warnings = redact_credentials(thinking_mrkdwn)
        for w in cred_warnings:
            logger.warning("Credential redacted in thinking: %s", w)
        thinking_block = _condense_thinking(thinking_mrkdwn)
        if thinking_ts:
            try:
                await slack.update_message(channel, thinking_ts, thinking_block)
            except Exception:
                logger.warning("Failed to update thinking message", exc_info=True)
        else:
            for part in split_message(thinking_block):
                try:
                    await slack.post_message(channel, part, reply_ts)
                except Exception:
                    logger.warning("Failed to post thinking message", exc_info=True)
    elif thinking_ts:
        # Placeholder was posted but no reasoning was captured — remove it so
        # the thread isn't left with a dangling "💭 Thinking…".
        try:
            await slack.delete_message(channel, thinking_ts)
        except Exception:
            logger.debug("Failed to delete empty thinking placeholder", exc_info=True)

    # ── Timing footer ──
    elapsed = time.monotonic() - _t0
    footer_blocks, footer_text = build_timing_footer(elapsed, client)
    footer_blocks = _append_footer_actions(
        footer_blocks,
        options,
        thread_ts,
        linked_session_key,
        _state._dashboard_state,
    )
    await slack.post_blocks(channel, footer_blocks, footer_text, reply_ts)

    # ── Voice reply (fire-and-forget, non-blocking) ──
    # Triggers when: (a) user has opted in globally or per-thread via !voice,
    # or (b) this message carried transcribed voice input and
    # auto_reply_to_voice is enabled (symmetric voice conversation).
    #
    # ``auto_reply_to_voice`` defaults to ``enabled``'s value at config load
    # (see ``set_orch_cfg``) so users with explicit ``enabled=false`` retain
    # zero-voice behavior, and globally-enabled users automatically get
    # symmetric voice-in/voice-out. Users who want voice ONLY in response to
    # voice memos can set ``auto_reply_to_voice=true`` while leaving
    # ``enabled=false``. See docs/kiro-cli/chat/voice.md.
    voice_auto_reply = had_voice_input and _vc.auto_reply_to_voice
    if _vc.global_enabled or session_key in _vc.sessions or voice_auto_reply:
        if len(accumulated) >= 50:
            _tts_ok = _tts_available(
                provider=_vc.provider,
                piper_binary=_vc.piper_binary,
                piper_model=_vc.piper_model,
            )
            if not _tts_ok:
                # Voice reply requested via any opt-in path (global, per-thread,
                # or voice-auto-reply) but the configured TTS backend isn't
                # available. Post a one-shot ephemeral so the user knows the
                # response fell back to text only — silent fallback is worse
                # UX for users who explicitly opted in.
                if _vc.provider == "piper":
                    hint = (
                        "Install piper (`pip install piper-tts` in a Python "
                        "3.11 venv) and set `voice_reply.piper_model` to your "
                        "voice .onnx file."
                    )
                else:
                    hint = "Run `ada credentials update` and ensure `aws` CLI " "is on PATH."
                if voice_auto_reply:
                    intro = "🔇 Received your voice memo. Replying as text — "
                else:
                    intro = "🔇 Voice reply requested but "
                try:
                    await slack.post_ephemeral(
                        channel,
                        user_id,
                        f"{intro}TTS (provider={_vc.provider}) isn't " f"configured. {hint}",
                    )
                except Exception:
                    logger.debug("Failed to post TTS-unavailable ephemeral", exc_info=True)
            else:
                _vid = _vc.voices.get(session_key, _vc.default_voice)
                _eng = _vc.engines.get(session_key, _vc.default_engine)
                _rate = _vc.rates.get(session_key, _vc.default_rate)
                _pitch = _vc.pitches.get(session_key, _vc.default_pitch)
                asyncio.create_task(
                    _safe_voice_reply(
                        slack,
                        channel,
                        reply_ts,
                        final_text,
                        voice_id=_vid,
                        engine=_eng,
                        rate=_rate,
                        pitch=_pitch,
                    )
                )

    # ── Update task banner with final state ──
    # ── Persist conversation history ──
    _skip_writes = _is_slack_restricted(session_key)
    if conversation_log and not _skip_writes:
        save_conversation_turn(
            conversation_log,
            session_key,
            text,
            accumulated,
            source_thread=session_key,
            source_user=user_id,
            agent=_agent,
        )
        if consolidator and _stop_reason != STOP_REASON_CANCELLED:
            consolidator.maybe_consolidate(session_key)

    # ── Bidirectional sync: mirror to dashboard if routed to a dashboard session ──
    if linked_session_key and _state._dashboard_state and accumulated and not _skip_writes:
        try:
            ds = _state._dashboard_state
            slot_name = linked_session_key.removeprefix("dashboard:")
            slot = getattr(ds, "_slots", {}).get(slot_name)
            if slot:
                slot.append("user", text, "msg msg-u")
                slot.append("assistant", accumulated, "msg msg-a")
                if slot._on_message:
                    slot._on_message(
                        slot.key, {"role": "user", "content": text, "cls": "msg msg-u"}
                    )
                    slot._on_message(
                        slot.key, {"role": "assistant", "content": accumulated, "cls": "msg msg-a"}
                    )
                ds.push_slots_update()  # type: ignore[attr-defined]
        except Exception:
            logger.debug("Failed to mirror Slack message to dashboard", exc_info=True)
    # ── Auto-title Slack thread (fire-and-forget) ──
    # Claim-early-unclaim-on-failure pattern: mark titled immediately to prevent
    # duplicate tasks from concurrent messages. If the background task fails or
    # returns SKIP, it unclaims the key so the next message retries. A message
    # arriving between claim and unclaim is intentionally skipped (no duplicate).
    if not _had_error and session_key not in _titled_threads and not _skip_writes:
        _mark_titled(session_key)  # claim early to prevent duplicate tasks
        _t = asyncio.create_task(
            _maybe_auto_title_slack(
                slack, sessions, channel, session_key, conversation_log, text, accumulated
            )
        )
        _background_tasks.add(_t)
        _t.add_done_callback(_background_tasks.discard)


# ── Slack thread auto-title ─────────────────────────────────────────────

_auto_title_lock: asyncio.Lock | None = None


def _get_auto_title_lock() -> asyncio.Lock:
    """Lazily create the lock inside a running event loop."""
    global _auto_title_lock
    if _auto_title_lock is None:
        _auto_title_lock = asyncio.Lock()
    return _auto_title_lock


def _build_title_prompt(user_msg: str, assistant_msg: str) -> str:
    """Build title prompt using f-string to avoid str.format() KeyError on curly braces."""
    return (
        "You are a session naming agent. Given the conversation below, decide if the topic "
        "is clear enough to name.\n\n"
        "If YES: reply with ONLY a short title (3-6 words). No quotes, no punctuation.\n"
        "If NO (too vague, just greetings, or unclear topic): reply with exactly SKIP\n\n"
        f"user: {user_msg}\nassistant: {assistant_msg}"
    )


async def _maybe_auto_title_slack(
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    session_key: str,
    conversation_log: ConversationLog | None,
    user_text: str,
    assistant_text: str,
) -> None:
    """Generate and set a Slack thread title after the first response."""
    try:
        from kiro_crew.session import BACKGROUND_KEY

        prompt = _build_title_prompt(user_text[:200], assistant_text[:200])
        async with _get_auto_title_lock():
            client, _, _ = await sessions.get_or_create(BACKGROUND_KEY)
            title = ""
            try:

                async def _stream_title() -> str:
                    t = ""
                    async for event in client.stream(prompt):
                        if event.kind == EVENT_TEXT_CHUNK:
                            t += event.text
                        elif event.kind == EVENT_PERMISSION_REQUEST:
                            sel().log_api_access(
                                caller="system",
                                operation="auto_title.tool_rejected",
                                outcome="denied",
                                source="slack",
                                resources=str(event.request_id),
                            )
                            await client.reject_tool(event.request_id)
                        elif event.kind == EVENT_COMPLETE:
                            break
                    return t

                title = await asyncio.wait_for(_stream_title(), timeout=30)
            finally:
                sessions.release(BACKGROUND_KEY)

        title = title.split("\n")[0].strip("\"'. \t")
        title = title.replace("<", "").replace(">", "")  # neutralize Slack mrkdwn links
        if not title or title.upper() == "SKIP":
            _titled_threads.pop(session_key, None)  # allow retry on next exchange
            return
        title, _ = redact_exfiltration_urls(title)
        title, _ = redact_credentials(title)
        title = title[:80]

        if _titled_threads.get(session_key) == "manual":
            return  # manual title was set while we were streaming
        await slack.set_thread_title(channel, session_key, title)
        if conversation_log:
            try:
                conversation_log.set_title(session_key, title)
            except Exception:
                logger.debug(
                    "Failed to set conversation log title for %s", session_key, exc_info=True
                )
        sel().log_api_access(
            caller="system",
            operation="slack.thread_auto_title",
            outcome="allowed",
            source="slack",
            resources=f"{channel}:{session_key}",
        )
        logger.info("Slack thread auto-titled: %s → %r", session_key, title)
    except Exception:
        _titled_threads.pop(session_key, None)  # allow retry on transient failure
        logger.debug("Slack thread auto-title failed for %s", session_key, exc_info=True)
