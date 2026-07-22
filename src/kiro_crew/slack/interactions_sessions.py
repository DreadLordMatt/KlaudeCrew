"""Split of interactions.py (see the interactions.py shim for details)."""

from __future__ import annotations

import asyncio
import hashlib
import logging

import aiohttp

from kiro_crew.security import redact_and_truncate
from kiro_crew.slack import interactions_core as _core
from kiro_crew.slack.interactions_core import ack_button

logger = logging.getLogger(__name__)


async def _handle_stop_confirm(payload: dict, channel: str, msg_ts: str, user_id: str) -> None:
    """Stop the current session when user confirms.

    Defense-in-depth: re-checks the allowlist even though dispatch()
    also enforces it, matching the deny-by-default pattern used by
    other privileged handlers. stop_turn() can escalate to a hard kill,
    so handler-level authorization is required.
    """
    if not _core._orch or not _core._orch.sessions:
        await ack_button(payload, channel, msg_ts)
        return
    if not _core.is_allowed_user(user_id):
        logger.warning("stop_confirm denied for unauthorized user %s", user_id or "unknown")
        _core.sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.stop_confirm",
            outcome="denied",
            source="slack",
            resources=channel,
            error="unauthorized user",
        )
        await ack_button(payload, channel, msg_ts)
        return

    # Find the active session in this channel/thread
    thread_ts = payload.get("message", {}).get("thread_ts") or msg_ts
    has_session = _core._orch.sessions.has_session(thread_ts)
    active_task = _core._orch._session_tasks.pop(thread_ts, None)

    if has_session or active_task:
        response_url = payload.get("response_url", "")

        async def _update_ephemeral(blocks: list[dict], text: str) -> None:
            if response_url:
                import aiohttp

                try:
                    async with aiohttp.ClientSession() as sess:
                        await sess.post(
                            response_url,
                            json={"replace_original": True, "text": text, "blocks": blocks},
                        )
                except Exception:
                    pass

        async def _on_soft() -> None:
            from kiro_crew.slack.blocks import build_stopped_blocks

            await _update_ephemeral(build_stopped_blocks(), "⏹ [Stopped]")
            if _core._orch and _core._orch.slack:
                await _core._orch.slack.post_message(
                    channel, "⏹ Execution stopped.", thread_ts
                )

        async def _on_hard() -> None:
            from kiro_crew.slack.blocks import build_stop_failed_blocks

            await _update_ephemeral(
                build_stop_failed_blocks(), "⛔ [Stop Failed, Session Reset]"
            )
            if _core._orch and _core._orch.slack:
                await _core._orch.slack.post_message(
                    channel, "⛔ Execution stopped — session reset.", thread_ts
                )

        outcome = await _core._orch.sessions.stop_turn(
            thread_ts, on_soft=_on_soft, on_hard=_on_hard
        )
        if active_task and not active_task.done():
            active_task.cancel()
        # If stop_turn returned "idle" (no active turn), neither callback
        # fired — dismiss the stale ephemeral with a "Nothing running" message.
        if outcome == "idle":
            await _update_ephemeral([], "Nothing running.")
        _core.sel().log_tool_invocation(
            session_key=thread_ts,
            source="slack",
            tool_name="/kirocrew stop",
            tool_kind="command",
            outcome=outcome,
            metadata={"user": user_id, "channel": channel},
        )
    else:
        # Replace buttons with confirmation
        response_url = payload.get("response_url", "")
        label = "Nothing running."
        if response_url:
            import aiohttp

            try:
                async with aiohttp.ClientSession() as sess:
                    await sess.post(
                        response_url,
                        json={"replace_original": True, "text": label},
                    )
            except Exception:
                pass
        elif _core._orch.slack:
            try:
                await _core._orch.slack.update_message(channel, msg_ts, text=label)
            except Exception:
                pass


async def _handle_stop_cancel(payload: dict, channel: str, msg_ts: str) -> None:
    """Delete the ephemeral stop confirmation message on cancel."""
    response_url = payload.get("response_url", "")
    if response_url:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url,
                    json={
                        "delete_original": True,
                    },
                )
        except Exception:
            pass
    elif _core._orch and _core._orch.slack:
        try:
            await _core._orch.slack.delete_message(channel, msg_ts)
        except Exception:
            pass


async def _handle_stop_kill_now(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Force-kill via the ephemeral Kill Now button.

    Defense-in-depth: re-checks the allowlist even though dispatch()
    also enforces it, matching the deny-by-default pattern used by
    other privileged handlers (e.g. ``_handle_allowlist_remove``).
    """
    if not _core._orch or not _core._orch.sessions:
        return
    if not _core.is_allowed_user(user_id):
        logger.warning("stop_kill_now denied for unauthorized user %s", user_id or "unknown")
        _core.sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.stop_kill_now",
            outcome="denied",
            source="slack",
            resources=action.get("value", ""),
            error="unauthorized user",
        )
        return
    session_key = action.get("value", "")
    if not session_key:
        return

    response_url = payload.get("response_url", "")

    async def _on_hard() -> None:
        from kiro_crew.slack.blocks import build_stop_failed_blocks

        if response_url:
            import aiohttp

            try:
                async with aiohttp.ClientSession() as sess:
                    await sess.post(
                        response_url,
                        json={
                            "replace_original": True,
                            "text": "⛔ [Stop Failed, Session Reset]",
                            "blocks": build_stop_failed_blocks(),
                        },
                    )
            except Exception:
                pass
        if _core._orch and _core._orch.slack:
            # Use the ephemeral's thread_ts (falling back to its own ts)
            # rather than session_key: for linked dashboard sessions these
            # differ, and session_key would not be a valid Slack thread.
            thread_ts = payload.get("message", {}).get("thread_ts") or msg_ts
            await _core._orch.slack.post_message(
                channel, "⛔ Execution stopped — session reset.", thread_ts
            )

    outcome = await _core._orch.sessions.stop_turn(session_key, force=True, on_hard=_on_hard)
    _core.sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name="stop_kill_now",
        tool_kind="command",
        outcome=outcome,
        metadata={"user": user_id, "channel": channel},
    )


async def _handle_session_resume(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Show choice buttons for how to resume a session."""
    import json

    if not _core.is_owner(user_id):
        logger.warning("session_resume rejected: non-owner %s", user_id)
        _core.sel().log_api_access(caller=user_id, operation="slack.session_resume", outcome="denied", source="slack")
        return
    if not (_core._orch and _core._orch.sessions and _core._orch.slack):
        return

    try:
        val = json.loads(action.get("value", "{}"))
    except (ValueError, json.JSONDecodeError):
        val = {"key": action.get("value", "")}

    session_key = val.get("key", "")
    title = redact_and_truncate(val.get("title", session_key[:20]), max_chars=200)

    if not session_key:
        return

    # Check if session already has a linked thread/channel
    existing_thread, existing_channel = _core._orch.sessions.get_slack_link(session_key)

    # Home Tab clicks have empty ``channel`` and ``response_url`` because the
    # interaction is a ``view`` payload, not a message payload. Fall back to
    # the user's DM channel so the choice buttons land somewhere visible.
    if not channel:
        try:
            dm_id = await _core._orch.slack.open_dm(user_id)
        except Exception:
            logger.exception("session_resume: open_dm failed for user %s", user_id)
            dm_id = ""
        if dm_id:
            channel = dm_id

    if existing_thread and existing_channel:
        link = f"https://slack.com/archives/{existing_channel}/p{existing_thread.replace('.', '')}"
        label = f"\U0001f9f5 This session is already active: <{link}|Go to conversation>"
        response_url = payload.get("response_url", "")
        if response_url:
            try:
                async with aiohttp.ClientSession() as sess:
                    await sess.post(response_url, json={"replace_original": False, "text": label})
            except Exception:
                logger.exception("session_resume: response_url POST failed")
        elif channel:
            try:
                await _core._orch.slack.post_message(channel, label)
            except Exception:
                logger.exception("session_resume: post_message to %s failed", channel)
        return

    # Show choice buttons
    title, _ = _core.redact_exfiltration_urls(title)
    title, _ = _core.redact_credentials(title)
    choice_value = json.dumps({"key": session_key, "title": title, "src_channel": channel})
    short_id = hashlib.sha256(session_key.encode()).hexdigest()[:12]
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"\U0001f504 Resume *{title}*\nWhere would you like to continue?",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\U0001f4ce Thread"},
                    "action_id": f"mc_resume_thread_{short_id}",
                    "value": choice_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\U0001f4ac DM"},
                    "action_id": f"mc_resume_dm_{short_id}",
                    "value": choice_value,
                },
            ],
        },
    ]
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json={
                    "replace_original": False,
                    "text": f"Resume {title} \u2014 choose Thread or DM",
                    "blocks": blocks,
                })
        except Exception:
            logger.exception("session_resume: response_url POST failed")
    elif channel:
        try:
            await _core._orch.slack.post_blocks(
                channel, blocks, f"Resume {title} \u2014 choose Thread or DM"  # type: ignore[arg-type]
            )
        except Exception:
            logger.exception("session_resume: post_blocks to %s failed", channel)
    else:
        logger.warning(
            "session_resume: no response_url and no channel — cannot post choice for user %s",
            user_id,
        )


_resume_locks: dict[str, asyncio.Lock] = {}


async def _handle_resume_choice(
    payload: dict,
    action: dict,
    channel: str,
    msg_ts: str,
    user_id: str,
    mode: str,
) -> None:
    """Dispatch session resume to thread or DM based on user choice."""
    import json

    if not _core.is_owner(user_id):
        logger.warning("resume_choice rejected: non-owner %s", user_id)
        _core.sel().log_api_access(caller=user_id, operation="slack.session_resume_choice", outcome="denied", source="slack")
        return
    if not (_core._orch and _core._orch.sessions and _core._orch.slack):
        return

    try:
        val = json.loads(action.get("value", "{}"))
    except (ValueError, json.JSONDecodeError):
        return

    session_key = val.get("key", "")
    title = redact_and_truncate(val.get("title", session_key[:20]), max_chars=200)
    title, _ = _core.redact_exfiltration_urls(title)
    title, _ = _core.redact_credentials(title)
    src_channel = val.get("src_channel", channel)

    if not session_key:
        return

    # Bounded eviction to prevent unbounded memory growth
    if len(_resume_locks) > 1000:
        evicted = 0
        for k in list(_resume_locks):
            if evicted >= 200:
                break
            if not _resume_locks[k].locked():
                _resume_locks.pop(k, None)
                evicted += 1

    lock = _resume_locks.setdefault(session_key, asyncio.Lock())
    async with lock:
        # Re-check: session may have been linked while user was choosing
        existing_thread, existing_channel = _core._orch.sessions.get_slack_link(session_key)
        if existing_thread and existing_channel:
            link = f"https://slack.com/archives/{existing_channel}/p{existing_thread.replace('.', '')}"
            label = f"\U0001f9f5 Already active: <{link}|Go to conversation>"
            response_url = payload.get("response_url", "")
            if response_url:
                import aiohttp
                try:
                    async with aiohttp.ClientSession() as sess:
                        await sess.post(response_url, json={"replace_original": True, "text": label})
                except Exception:
                    pass
            return

        if mode == "thread":
            target_channel = src_channel
            thread_msg = (
                f"\U0001f9f5 *{title}*\n"
                "Session resumed. Continue the conversation in this thread."
            )
            try:
                thread_ts = await _core._orch.slack.post_message(target_channel, thread_msg)
            except Exception:
                logger.debug("Failed to create session thread", exc_info=True)
                return
            if not thread_ts:
                return
            link_ts, link_channel = thread_ts, target_channel
            label = f"\u25b6\ufe0f Resumed *{title}* in thread."
        elif mode == "dm":
            try:
                dm_channel = await _core._orch.slack.open_dm(user_id)
            except Exception:
                logger.debug("Failed to open DM for session resume", exc_info=True)
                return
            if not dm_channel:
                return
            header = (
                "\u2500" * 15 + "\n"
                f"\U0001f504 Resumed: *{title}*\n"
                + "\u2500" * 15
            )
            try:
                header_ts = await _core._orch.slack.post_message(dm_channel, header)
            except Exception:
                logger.debug("Failed to post DM resume header", exc_info=True)
                return
            if not header_ts:
                return
            link_ts, link_channel = header_ts, dm_channel
            thread_ts = header_ts
            target_channel = dm_channel
            label = f"\u25b6\ufe0f Resumed *{title}* in DM."
        else:
            return

        # Link session
        _core._orch.sessions.set_slack_link(session_key, link_ts, link_channel)
        _core.sel().log_api_access(
            caller=user_id,
            operation="slack.session_resume",
            outcome="allowed",
            source="slack",
            resources=session_key,
        )
        if _core._orch.dashboard_state:
            slot_name = (
                session_key.split(":", 1)[-1] if ":" in session_key else session_key
            )
            _core._orch.dashboard_state.link_slack(slot_name, link_ts, link_channel)

        # Post last 5 messages as context
        try:
            from pathlib import Path

            sess_dir = Path.home() / ".kirocrew" / "sessions"
            stem = session_key.split(":", 1)[-1] if ":" in session_key else session_key
            jsonl = sess_dir / f"{stem}.jsonl"
            if not jsonl.exists() and not stem.startswith("dashboard_"):
                jsonl = sess_dir / f"dashboard_{stem}.jsonl"
            if jsonl.exists():
                lines = jsonl.read_text(encoding="utf-8").splitlines()
                msgs: list[tuple[str, str]] = []
                for ln in lines:
                    try:
                        d = json.loads(ln.strip())
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if d.get("_type"):
                        continue
                    role = d.get("role", "")
                    txt = (d.get("content") or "")[:2000]
                    if role in ("user", "assistant") and txt:
                        msgs.append((role, txt))
                for role, txt in msgs[-5:]:
                    txt, _ = _core.redact_exfiltration_urls(txt)
                    txt, _ = _core.redact_credentials(txt)
                    icon = "\U0001f9d1" if role == "user" else "\U0001f916"
                    try:
                        await _core._orch.slack.post_message(
                            target_channel, f"{icon} {txt}", thread_ts,
                        )
                    except Exception:
                        logger.debug("Failed to post context message", exc_info=True)
        except Exception:
            logger.debug("Failed to post session context", exc_info=True)

        # Update the choice message
        response_url = payload.get("response_url", "")
        if response_url:
            import aiohttp
            try:
                async with aiohttp.ClientSession() as sess:
                    await sess.post(
                        response_url, json={"replace_original": True, "text": label},
                    )
            except Exception:
                pass


async def _handle_session_end(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """End a session by removing it from SessionMap and resetting if active."""
    if not _core.is_owner(user_id):
        logger.warning("session_end rejected: non-owner %s", user_id)
        return
    session_id = action.get("value", "")
    if not (session_id and _core._orch and _core._orch.sessions):
        return

    _core.sel().log_api_access(caller=user_id, operation="slack.session_end", outcome="allowed", source="slack", resources=session_id)

    key_to_remove = _core._orch.sessions.find_key_by_sid(session_id)
    # Also try treating value as a direct session key (from /kirocrew sessions buttons)
    if not key_to_remove and _core._orch.sessions.has_session(session_id):
        key_to_remove = session_id
    if key_to_remove:
        # Trigger skill extraction before killing the session (fire-and-forget)
        if _core._orch.consolidator:
            try:
                # Audit the consolidation trigger. Skill write auditing (log_tool_invocation
                # with tool_name="auto_skill_create") is handled inside _process_auto_skills().
                _core.sel().log_api_access(caller=user_id, operation="consolidate_session_slack_end", outcome="allowed", source="slack", resources=key_to_remove)
                _core._orch.consolidator.consolidate_session(key_to_remove)
            except Exception:
                logger.debug("consolidate_session (or SEL) failed for %s", key_to_remove, exc_info=True)
        # Soft-remove: kill process but preserve session_map for future resume
        try:
            await _core._orch.sessions.remove(key_to_remove)
        except Exception:
            logger.debug("session end remove failed for %s", key_to_remove, exc_info=True)

    response_url = payload.get("response_url", "")
    label = f"🛑 Session `{session_id[:12]}…` ended."
    if response_url:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json={"replace_original": True, "text": label})
                return
        except Exception:
            pass
    if _core._orch.slack and channel and msg_ts:
        try:
            await _core._orch.slack.update_message(channel, msg_ts, text=label)
        except Exception:
            pass


async def _handle_inline_stop(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Stop the active turn for a session via the inline stop button."""
    if not _core.is_owner(user_id):
        _core.sel().log_api_access(caller=user_id, operation="slack.inline_stop", outcome="denied", source="slack", resources=action.get("value", ""))
        return
    session_key = action.get("value", "")
    if not (session_key and _core._orch and _core._orch.sessions):
        _core.sel().log_api_access(caller=user_id, operation="slack.inline_stop", outcome="invalid", source="slack", resources=session_key)
        return

    _core.sel().log_api_access(caller=user_id, operation="slack.inline_stop", outcome="allowed", source="slack", resources=session_key)

    # Immediate feedback — update the working message to show stopping
    if _core._orch.slack and channel and msg_ts:
        try:
            await _core._orch.slack.update_message(channel, msg_ts, text="⏹ _Stopping…_")
        except Exception:
            pass

    async def _on_soft() -> None:
        if _core._orch.slack and channel and msg_ts:
            try:
                await _core._orch.slack.update_message(channel, msg_ts, text="⏹ Execution stopped.")
            except Exception:
                pass

    async def _on_hard() -> None:
        if _core._orch.slack and channel and msg_ts:
            try:
                await _core._orch.slack.update_message(channel, msg_ts, text="⛔ Execution stopped — session reset.")
            except Exception:
                pass

    outcome = await _core._orch.sessions.stop_turn(
        session_key, on_soft=_on_soft, on_hard=_on_hard
    )
    if outcome == "idle" and _core._orch.slack and channel and msg_ts:
        try:
            await _core._orch.slack.update_message(channel, msg_ts, text="⏹ Nothing running.")
        except Exception:
            pass
    _core.sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name="inline_stop",
        tool_kind="command",
        outcome=outcome,
        metadata={"user": user_id, "channel": channel},
    )


async def _handle_session_new(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Create a fresh session by posting a prompt in a new thread."""
    if not _core.is_owner(user_id):
        logger.warning("session_new rejected: non-owner %s", user_id)
        return
    if not (_core._orch and _core._orch.slack):
        return
    _core.sel().log_api_access(caller=user_id, operation="slack.session_new", outcome="allowed", source="slack", resources=channel)

    # Post a new message that starts a fresh thread
    try:
        await _core._orch.slack.post_message(
            channel, "✨ New session started. Send your first message here."
        )
    except Exception:
        logger.debug("Failed to create new session message", exc_info=True)
        return

    # Ack the button
    response_url = payload.get("response_url", "")
    label = "✨ New session created."
    if response_url:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json={"replace_original": False, "text": label})
        except Exception:
            pass
