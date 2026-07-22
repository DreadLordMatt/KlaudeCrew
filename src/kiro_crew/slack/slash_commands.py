"""Split from slack/handler.py: slash_commands cluster."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.cron import CronService
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.llm_helpers import save_conversation_turn
from kiro_crew.providers.base import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, LLMProvider
from kiro_crew.safety_override import safety_override
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import SessionManager
from kiro_crew.slack import handler_state as _state
from kiro_crew.slack.agent_resolution import (
    _discover_project_agents,
    _get_default_agent,
    _list_all_agent_names,
    _persist_channel_config,
    _resolve_agent_name,
    _set_default_agent,
)
from kiro_crew.slack.blocks import deprecation_warning_block
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.handler_state import (
    _YOLO_TTL_SECS,
    APPROVAL_AUTO,
    _mark_titled,
    _reload_orch_cfg,
    _thread_agents,
    _thread_projects,
    _vc,
    disable_yolo,
    enable_yolo_with_ttl,
    is_allowed_user,
    is_owner,
    is_yolo_mode,
)
from kiro_crew.slack.keyword_commands import (
    _handle_cron_command,
    _handle_run_command,
    _handle_sessions_command,
    _handle_spawn_command,
)
from kiro_crew.slack.privacy_modes import _is_slack_restricted
from kiro_crew.slack.status_reactions import _add_phase_reaction
from kiro_crew.subagent import SubagentManager
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.voice_reply import voice_reply as _voice_reply_fn

logger = logging.getLogger(__name__)


# Mapping of bang commands to their /kirocrew slash equivalents.
_BANG_TO_SLASH: dict[str, str] = {
    "!yolo": "/kirocrew yolo",
    "!stop": "/kirocrew stop",
    "!voice": "/kirocrew voice",
    "!agent": "/kirocrew agent",
    "!dashboard": "/kirocrew dashboard",
    "!ta": "/kirocrew agent",
    # "!allowlist" removed — multi-user access disabled for security
    "!channel": "/kirocrew channel",
    "!link-to-dashboard": "/kirocrew link-to-dashboard",
    "!restart": "/kirocrew restart",
}


@dataclass
class MessageContext:
    """Service references needed to process a Slack message.

    Groups the 8 service/config parameters that were previously passed
    individually to ``handle_message``.
    """

    sessions: SessionManager
    approval_mode: str = APPROVAL_AUTO
    context_builder: ContextBuilder | None = None
    cron_service: CronService | None = None
    conversation_log: ConversationLog | None = None
    consolidator: HistoryConsolidator | None = None
    subagent_manager: SubagentManager | None = None
    task_runner: TaskRunner | None = None


async def _safe_voice_reply(
    slack: SlackClientOps,
    channel: str,
    thread_ts: str,
    text: str,
    voice_id: str = "Ruth",
    engine: str = "generative",
    rate: str = "100%",
    pitch: str = "+0%",
) -> None:
    """Fire-and-forget voice reply.  Never raises."""
    try:
        await _voice_reply_fn(
            slack,
            channel,
            thread_ts,
            text,
            provider=_vc.provider,
            voice_id=voice_id,
            engine=engine,
            rate=rate,
            pitch=pitch,
            aws_profile=_vc.aws_profile,
            region=_vc.region,
            piper_binary=_vc.piper_binary,
            piper_model=_vc.piper_model,
            piper_model_config=_vc.piper_model_config,
            length_scale=_vc.piper_length_scale,
        )
    except Exception:
        logger.debug("Voice reply failed", exc_info=True)


async def _handle_slash_command(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None = None,
) -> str | None:
    """Dispatch owner-only ``!commands``.  Returns a string (even empty) if handled, None if not."""

    cmd = cmd_text.split()[0].lower()

    # ── Deprecation warning for all bang commands ──
    slash_equiv = _BANG_TO_SLASH.get(cmd)
    if slash_equiv:
        logger.warning("Deprecated bang command %s used — suggest %s", cmd, slash_equiv)
        warn_block = deprecation_warning_block(cmd, slash_equiv)
        await slack.post_blocks(channel, [warn_block], f"{cmd} is deprecated", reply_ts)

    # ── !yolo on / !yolo off / !yolo renew ──
    if cmd == "!yolo":
        parts = cmd_text.split()
        yolo_active = is_yolo_mode()
        if len(parts) >= 2 and parts[1].lower() == "off":
            if yolo_active:
                disable_yolo()
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.yolo_mode",
                    outcome="allowed",
                    source="slack",
                    resources="yolo_off",
                )
                await slack.post_message(channel, "🔒 YOLO mode disabled.", reply_ts)
            else:
                await slack.post_message(channel, "YOLO mode is already off.", reply_ts)
        elif len(parts) >= 2 and parts[1].lower() == "on":
            if not yolo_active:
                enable_yolo_with_ttl(_YOLO_TTL_SECS)
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.yolo_mode",
                    outcome="allowed",
                    source="slack",
                    resources="yolo_on",
                )
                await slack.post_message(
                    channel,
                    f"🔓 YOLO mode enabled (auto-expires in {_YOLO_TTL_SECS // 60}min).",
                    reply_ts,
                )
            else:
                remaining = safety_override().remaining_secs()
                await slack.post_message(
                    channel, f"YOLO mode is already on ({remaining // 60}min remaining).", reply_ts
                )
        elif len(parts) >= 2 and parts[1].lower() == "renew":
            result = safety_override().renew("slack")
            if result.renewed:
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.yolo_mode",
                    outcome="renewed",
                    source="slack",
                    resources="yolo_renew",
                )
                await slack.post_message(
                    channel,
                    f"🔓 YOLO mode renewed (auto-expires in {result.ttl // 60}min).",
                    reply_ts,
                )
            else:
                await slack.post_message(
                    channel, "YOLO mode is not active. Use `!yolo on` to activate.", reply_ts
                )
        else:
            if yolo_active:
                remaining = safety_override().remaining_secs()
                status = f"ON 🔓 ({remaining // 60}min remaining)"
            else:
                status = "OFF 🔒"
            await slack.post_message(
                channel,
                f"YOLO mode: *{status}*. Use `!yolo on` / `!yolo off` / `!yolo renew`.",
                reply_ts,
            )
        return ""

    # ── !stop — defensive fallback (normally intercepted in events.py
    #    _route_message before handle_message is called) ──
    if cmd == "!stop":
        has_session = sessions.has_session(session_key)
        if not has_session:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="no_session",
                metadata={"user": user_id, "channel": channel},
            )
            await slack.post_message(channel, "Nothing running.", reply_ts)
            return ""

        # Post ephemeral "Stopping…" block with Kill Now button
        from kiro_crew.slack.blocks import build_stopping_blocks

        await slack.post_ephemeral(
            channel,
            user_id,
            "Stopping…",
            blocks=build_stopping_blocks(session_key),
            thread_ts=reply_ts,
        )

        async def _on_soft() -> None:
            await slack.post_message(channel, "⏹ Execution stopped.", reply_ts)

        async def _on_hard() -> None:
            await slack.post_message(channel, "⛔ Execution stopped — session reset.", reply_ts)

        outcome = await sessions.stop_turn(session_key, on_soft=_on_soft, on_hard=_on_hard)
        # If stop_turn returned "idle" (no active turn), neither callback
        # fired — dismiss the stale "Stopping…" ephemeral explicitly.
        if outcome == "idle":
            await slack.post_message(channel, "Nothing running.", reply_ts)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!stop",
            tool_kind="command",
            outcome=outcome,
            metadata={"user": user_id, "channel": channel},
        )
        return ""

    # ── !voice on/off/global/<name> | engine/speed/pitch controls ──
    if cmd == "!voice":
        from kiro_crew.voice_reply import VALID_ENGINES, _validate_pitch, _validate_rate

        parts = cmd_text.split()
        arg = parts[1].lower() if len(parts) >= 2 else ""
        val = parts[2] if len(parts) >= 3 else ""
        if arg == "on":
            _vc.sessions.add(session_key)
            v = _vc.voices.get(session_key, _vc.default_voice)
            e = _vc.engines.get(session_key, _vc.default_engine)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!voice",
                tool_kind="command",
                outcome="voice_on",
                metadata={"user": user_id, "channel": channel},
            )
            await slack.post_message(channel, f"\U0001f50a Voice ON — *{v}* ({e})", reply_ts)
        elif arg == "off":
            _vc.sessions.discard(session_key)
            for d in (_vc.voices, _vc.engines, _vc.rates, _vc.pitches):
                d.pop(session_key, None)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!voice",
                tool_kind="command",
                outcome="voice_off",
                metadata={"user": user_id, "channel": channel},
            )
            await slack.post_message(channel, "\U0001f507 Voice OFF.", reply_ts)
        elif arg == "global":
            _vc.global_enabled = not _vc.global_enabled
            state = "ON \U0001f50a" if _vc.global_enabled else "OFF \U0001f507"
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!voice",
                tool_kind="command",
                outcome="voice_global_" + ("on" if _vc.global_enabled else "off"),
                metadata={"user": user_id, "channel": channel},
            )
            await slack.post_message(channel, f"Voice global: *{state}*", reply_ts)
        elif arg == "engine" and val:
            eng = val.lower()
            if eng not in VALID_ENGINES:
                await slack.post_message(
                    channel,
                    f"\u274c Invalid engine. Use: {', '.join(sorted(VALID_ENGINES))}",
                    reply_ts,
                )
            else:
                _vc.engines[session_key] = eng
                _vc.sessions.add(session_key)
                await slack.post_message(channel, f"\U0001f50a Engine set to *{eng}*.", reply_ts)
        elif arg == "speed" and val:
            validated = _validate_rate(val)
            _vc.rates[session_key] = validated
            _vc.sessions.add(session_key)
            await slack.post_message(channel, f"\U0001f50a Speed set to *{validated}*.", reply_ts)
        elif arg == "pitch" and val:
            validated = _validate_pitch(val)
            _vc.pitches[session_key] = validated
            _vc.sessions.add(session_key)
            await slack.post_message(channel, f"\U0001f50a Pitch set to *{validated}*.", reply_ts)
        elif arg and arg not in ("engine", "speed", "pitch"):
            voice_name = parts[1]  # preserve original case
            _vc.sessions.add(session_key)
            _vc.voices[session_key] = voice_name
            await slack.post_message(channel, f"\U0001f50a Voice set to *{voice_name}*.", reply_ts)
        else:
            on = session_key in _vc.sessions or _vc.global_enabled
            v = _vc.voices.get(session_key, _vc.default_voice)
            e = _vc.engines.get(session_key, _vc.default_engine)
            r = _vc.rates.get(session_key, _vc.default_rate)
            p = _vc.pitches.get(session_key, _vc.default_pitch)
            await slack.post_message(
                channel,
                f"\U0001f50a Voice: *{'ON' if on else 'OFF'}*\n"
                f"\u2022 Voice: *{v}* | Engine: *{e}*\n"
                f"\u2022 Speed: *{r}* | Pitch: *{p}*\n"
                "`!voice <name>` `!voice engine <neural|generative|long-form>` "
                "`!voice speed <80%>` `!voice pitch <+10%>`",
                reply_ts,
            )
        await _add_phase_reaction(slack, channel, msg_ts, "done")
        return ""

    # ── !agent <name> / !agent off — always global ──
    if cmd == "!agent":
        parts = cmd_text.split()
        if len(parts) == 1:
            name = _get_default_agent() or "kirocrew"
            await slack.post_message(
                channel,
                f"Current agent: *{name}*. Usage: `!agent <name>` or `!agent off`",
                reply_ts,
            )
            return ""
        if len(parts) != 2:
            await slack.post_message(channel, "Usage: `!agent <name>` or `!agent off`", reply_ts)
            return ""
        agent_name = parts[1]
        if agent_name.lower() in ("default", "off"):
            try:
                _set_default_agent("")
            except ValueError as e:
                await slack.post_message(channel, f"❌ {e}", reply_ts)
                return ""
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!agent",
                tool_kind="command",
                outcome="agent_reset",
                metadata={"user": user_id, "channel": channel},
            )
            await sessions.remove(session_key)
            await slack.post_message(channel, "🔄 Reset to default agent.", reply_ts)
            await _add_phase_reaction(slack, channel, msg_ts, "done")
            return ""
        resolved = _resolve_agent_name(agent_name, _thread_projects.get(session_key))
        if not resolved:
            names = _list_all_agent_names()
            await slack.post_message(
                channel, f"❌ Unknown agent `{agent_name}`. Available: {names}", reply_ts
            )
            return ""
        try:
            _set_default_agent(resolved)
        except ValueError as e:
            await slack.post_message(channel, f"❌ {e}", reply_ts)
            return ""
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!agent",
            tool_kind="command",
            outcome="agent_switch",
            metadata={"agent": resolved, "user": user_id, "channel": channel},
        )
        await sessions.remove(session_key)
        await slack.post_message(channel, f"🔄 Switched to agent: *{resolved}*", reply_ts)
        await _add_phase_reaction(slack, channel, msg_ts, "done")
        return ""

    # ── !dashboard [duration] ──
    if cmd == "!dashboard":
        from kiro_crew.dashboard.token_auth import parse_duration
        from kiro_crew.slack.allowlist import send_dashboard_link

        parts = cmd_text.split()
        ttl = 3600
        if len(parts) >= 2:
            parsed = parse_duration(parts[1])
            if parsed is None:
                await slack.post_message(
                    channel,
                    "Usage: `!dashboard [<N>h|<N>m]` — e.g. `!dashboard 2h`, `!dashboard 30m`",
                    reply_ts,
                )
                return ""
            ttl = parsed

        url = await send_dashboard_link(slack, user_id, ttl)
        if url:
            await slack.post_message(channel, "🔗 Dashboard link sent via DM.", reply_ts)
        else:
            await slack.post_message(channel, "❌ Failed to send dashboard link.", reply_ts)
        return ""

    # ── !link-to-dashboard -- import Slack thread into dashboard ──
    if cmd == "!link-to-dashboard":
        if not is_allowed_user(user_id):
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="link_to_dashboard",
                tool_kind="command",
                outcome="denied",
                metadata={"user_id": user_id, "channel": channel, "reason": "not_allowed_user"},
            )
            await slack.post_message(channel, "Not authorized.", reply_ts)
            return ""
        if not _state._dashboard_state or not hasattr(
            _state._dashboard_state, "get_or_create_slot"
        ):
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="link_to_dashboard",
                tool_kind="command",
                outcome="failure",
                metadata={"user_id": user_id, "channel": channel, "reason": "no_dashboard"},
            )
            await slack.post_message(channel, "Dashboard not available.", reply_ts)
            return ""
        if reply_ts == msg_ts:
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="link_to_dashboard",
                tool_kind="command",
                outcome="failure",
                metadata={"user_id": user_id, "channel": channel, "reason": "not_in_thread"},
            )
            await slack.post_message(
                channel, "Use this command inside a thread to import it.", reply_ts
            )
            return ""
        # Fetch thread history and import to dashboard
        from kiro_crew.slack.interactions import _import_thread_to_slot

        slot = await _import_thread_to_slot(slack, _state._dashboard_state, channel, reply_ts)
        if not slot:
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="link_to_dashboard",
                tool_kind="command",
                outcome="failure",
                metadata={"channel": channel, "thread_ts": reply_ts, "reason": "empty_thread"},
            )
            await slack.post_message(channel, "Could not fetch thread history.", reply_ts)
            return ""
        sel().log_tool_invocation(
            session_key=slot.key,
            agent="kirocrew",
            source="slack",
            tool_name="link_to_dashboard",
            tool_kind="command",
            outcome="success",
            metadata={
                "slot": slot.key,
                "channel": channel,
                "thread_ts": reply_ts,
                "msg_count": len(slot.messages),
            },
        )
        await slack.post_message(
            channel,
            f"Imported {len(slot.messages)} messages to dashboard session *{slot.key}*. Thread is now linked.",
            reply_ts,
        )
        return ""

    # ── !ta <name> / !ta off — thread-scoped agent ──
    if cmd == "!ta":
        parts = cmd_text.split()
        if len(parts) < 2:
            current = _thread_agents.get(session_key, "")
            if current:
                await slack.post_message(
                    channel,
                    f"Thread agent: *{current}*. `!ta off` to reset.",
                    reply_ts,
                )
            else:
                await slack.post_message(
                    channel,
                    "No thread agent set. Usage: `!ta <name>` or `!ta off`",
                    reply_ts,
                )
            return ""
        agent_name = parts[1]
        if agent_name.lower() in ("default", "off"):
            _thread_agents.pop(session_key, None)
            if conversation_log:
                try:
                    conversation_log.update_metadata(session_key, {"agent": ""})
                except Exception:
                    logger.debug("Failed to clear agent in conversation log", exc_info=True)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!ta",
                tool_kind="command",
                outcome="agent_reset",
                metadata={"user": user_id, "channel": channel, "scope": "thread"},
            )
            await sessions.remove(session_key)
            await slack.post_message(channel, "🔄 Thread agent reset.", reply_ts)
            await _add_phase_reaction(slack, channel, msg_ts, "done")
            return ""
        resolved = _resolve_agent_name(agent_name, _thread_projects.get(session_key))
        if not resolved:
            names = _list_all_agent_names()
            await slack.post_message(
                channel, f"❌ Unknown agent `{agent_name}`. Available: {names}", reply_ts
            )
            return ""
        _thread_agents[session_key] = resolved
        if conversation_log:
            try:
                conversation_log.update_metadata(session_key, {"agent": resolved})
            except Exception:
                logger.debug("Failed to persist agent to conversation log", exc_info=True)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!ta",
            tool_kind="command",
            outcome="agent_switch",
            metadata={"agent": resolved, "user": user_id, "channel": channel, "scope": "thread"},
        )
        await sessions.remove(session_key)
        await slack.post_message(channel, f"🔄 Thread agent: *{resolved}*", reply_ts)
        await _add_phase_reaction(slack, channel, msg_ts, "done")
        return ""

    # ── !project <path> / !project off — thread-scoped agent-discovery dir ──
    # NOTE: this only scopes which project-local .kiro agents are discoverable
    # for !ta in this thread; it does NOT change the agent's working directory
    # (cwd). Provider cwd plumbing is out of scope for this CR.
    if cmd == "!project":
        parts = cmd_text.split(maxsplit=1)
        if len(parts) < 2:
            current = _thread_projects.get(session_key, "")
            msg = (
                f"Thread agent-discovery project: `{current}`"
                if current
                else "No project set. Usage: `!project <path>` or `!project off`\n"
                "Scopes which project-local `.kiro` agents `!ta` can find — "
                "does not change the working directory."
            )
            await slack.post_message(channel, msg, reply_ts)
            return ""
        raw_path = parts[1].strip()
        if raw_path.lower() in ("off", "clear", "reset"):
            _thread_projects.pop(session_key, None)
            if conversation_log:
                try:
                    conversation_log.update_metadata(session_key, {"project": ""})
                except Exception:
                    logger.debug("Failed to clear project in conversation log", exc_info=True)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!project",
                tool_kind="command",
                outcome="project_cleared",
                metadata={"user": user_id, "channel": channel},
            )
            await sessions.remove(session_key)
            await slack.post_message(channel, "Thread project cleared.", reply_ts)
            return ""
        resolved = os.path.realpath(os.path.expanduser(raw_path))
        if is_sensitive_path(resolved):
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!project",
                tool_kind="command",
                outcome="project_denied_sensitive",
                metadata={"user": user_id, "channel": channel, "project": resolved},
            )
            await slack.post_message(
                channel, "Cannot use sensitive path as project directory.", reply_ts
            )
            return ""
        if not os.path.isdir(resolved):
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!project",
                tool_kind="command",
                outcome="project_denied_invalid",
                metadata={"user": user_id, "channel": channel, "project": resolved},
            )
            await slack.post_message(channel, f"Not a directory: `{resolved}`", reply_ts)
            return ""
        _thread_projects[session_key] = resolved
        if conversation_log:
            try:
                conversation_log.update_metadata(session_key, {"project": resolved})
            except Exception:
                logger.debug("Failed to persist project to conversation log", exc_info=True)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!project",
            tool_kind="command",
            outcome="project_set",
            metadata={"user": user_id, "channel": channel, "project": resolved},
        )
        await sessions.remove(session_key)
        # Discover project-local agents
        project_agents = _discover_project_agents(resolved)
        agent_info = ""
        if project_agents:
            names = ", ".join(
                f"`{s.stem.replace('.agent-spec', '') if '.agent-spec' in s.name else s.stem}`"
                for s in project_agents
            )
            agent_info = f"\nAgents found: {names} — use `!ta <name>` to switch"
        await slack.post_message(
            channel,
            f"Thread agent-discovery project: `{resolved}` "
            f"(scopes `!ta` agent lookup, not the working directory){agent_info}",
            reply_ts,
        )
        return ""

    # ── !allowlist — multi-user access disabled ──
    if cmd == "!allowlist":
        await slack.post_message(
            channel,
            "⛔ Multi-user access is disabled for security. Only the owner can use KiroCrew via Slack.",
            reply_ts,
        )
        return ""

    # ── !channel always|mention|observe|off / !channel agent <name> (owner-only) ──
    if cmd == "!channel":
        if not is_owner(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.channel_config",
                outcome="denied",
                source="slack",
                resources=channel,
                error="not owner",
            )
            await slack.post_message(channel, "⛔ Only the bot owner can use `!channel`.", reply_ts)
            return ""
        from kiro_crew.config.loader import _VALID_ACTIVATIONS

        parts = cmd_text.split()
        if len(parts) == 1:
            cfg = KiroCrewConfig.load()
            ch_cfg = cfg.channel_config(channel)
            agent_info = f", agent=*{ch_cfg.agent}*" if ch_cfg.agent else ""
            await slack.post_message(
                channel,
                f"Channel `{channel}` activation: *{ch_cfg.activation}*{agent_info}\n"
                f"Usage: `!channel always|mention|observe|off` or `!channel agent <name|off>`",
                reply_ts,
            )
            return ""

        subcmd = parts[1].lower()

        # !channel agent <name|off>
        if subcmd == "agent":
            if len(parts) < 3:
                await slack.post_message(
                    channel, "Usage: `!channel agent <name>` or `!channel agent off`", reply_ts
                )
                return ""
            agent_name = parts[2]
            if agent_name.lower() == "off":
                agent_name = ""
            else:
                resolved = _resolve_agent_name(agent_name, _thread_projects.get(session_key))
                if not resolved:
                    names = _list_all_agent_names()
                    await slack.post_message(
                        channel,
                        f"Unknown agent `{agent_name}`. Available: {names}",
                        reply_ts,
                    )
                    return ""
                agent_name = resolved
            _persist_channel_config(channel, agent=agent_name)
            _reload_orch_cfg()
            sel().log_api_access(
                caller=user_id,
                operation="slack.channel_agent",
                outcome="allowed",
                source="slack",
                resources=f"{channel}:{agent_name or 'default'}",
            )
            label = f"*{agent_name}*" if agent_name else "default"
            await slack.post_message(channel, f"Agent for this channel: {label}", reply_ts)
            return ""

        # !channel always|mention|observe|off
        if subcmd not in _VALID_ACTIVATIONS:
            await slack.post_message(
                channel,
                f"Invalid mode `{subcmd}`. Use: `always`, `mention`, `observe`, or `off`.",
                reply_ts,
            )
            return ""

        _persist_channel_config(channel, activation=subcmd)
        _reload_orch_cfg()
        sel().log_api_access(
            caller=user_id,
            operation="slack.channel_activation",
            outcome="allowed",
            source="slack",
            resources=f"{channel}:{subcmd}",
        )
        await slack.post_message(channel, f"Channel activation set to *{subcmd}*.", reply_ts)
        return ""

    # ── !title — set/generate Slack thread title ──
    if cmd == "!title":
        parts = cmd_text.split()
        title_text = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        if title_text:
            title_text, _ = redact_exfiltration_urls(title_text)
            title_text, _ = redact_credentials(title_text)
            await slack.set_thread_title(channel, session_key, title_text[:80])
            _mark_titled(session_key, "manual")
            if conversation_log and not _is_slack_restricted(session_key):
                try:
                    conversation_log.set_title(session_key, title_text[:80])
                except Exception:
                    logger.debug(
                        "Failed to set conversation log title for %s", session_key, exc_info=True
                    )
            sel().log_api_access(
                caller=user_id,
                operation="slack.thread_title",
                outcome="allowed",
                source="slack",
                resources=f"{channel}:{session_key}",
            )
            await _add_phase_reaction(slack, channel, msg_ts, "done")
        else:
            await slack.post_message(
                channel, "Usage: `!title <text>` — set a title for this thread.", reply_ts
            )
        return ""

    # Catch-all: unrecognized ! command — post error instead of falling through to LLM
    await slack.post_message(
        channel,
        f"❌ Unknown command `{cmd}`. Type `/kirocrew help` for available commands.",
        reply_ts,
    )
    return ""


def _filter_options_brackets(text: str, bracket_hold: str, stream_buffer: str) -> tuple[str, str]:
    """Filter ``[OPTIONS: ...]`` tags from streaming text character-by-character.

    Returns the updated *(bracket_hold, stream_buffer)* tuple.
    """
    for ch in text:
        if bracket_hold or ch == "[":
            bracket_hold += ch
            if ch == "]":
                if bracket_hold.startswith("[OPTIONS:"):
                    bracket_hold = ""
                else:
                    stream_buffer += bracket_hold
                    bracket_hold = ""
        else:
            stream_buffer += ch
    return bracket_hold, stream_buffer


def build_timing_footer(
    elapsed: float,
    client: LLMProvider | None = None,
) -> tuple[list[dict], str]:
    """Build the timing/context footer blocks for a Slack response.

    Returns ``(blocks, fallback_text)`` suitable for ``post_blocks``.
    """
    if elapsed < 60:
        duration = f"{int(elapsed)}s"
    else:
        mins, secs = divmod(int(elapsed), 60)
        duration = f"{mins}m {secs}s"
    footer_text = f"Finished in {duration}"
    if client is not None:
        try:
            ctx_pct = round(client.context_usage_pct())
            ctx_icon = (
                "🔴"
                if ctx_pct >= 70
                else "🟠" if ctx_pct >= 50 else "🟡" if ctx_pct >= 30 else "🟢"
            )
            footer_text = f"Finished in {duration} · {ctx_icon} ctx {ctx_pct}%"
        except Exception:
            logger.debug("Failed to retrieve context usage", exc_info=True)
    blocks: list[dict] = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer_text}]}
    ]
    return blocks, footer_text


def _append_footer_actions(
    footer_blocks: list[dict],
    options: list[str] | None,
    thread_ts: str | None,
    linked_session_key: str | None,
    dashboard_state: object | None,
) -> list[dict]:
    """Append OPTIONS checkboxes and/or Link to Dashboard button to footer blocks."""
    if options:
        from kiro_crew.slack.format import build_options_blocks

        footer_blocks.extend(build_options_blocks(options))
    if thread_ts and not linked_session_key and dashboard_state:
        from kiro_crew.slack.format import build_link_dashboard_button

        if footer_blocks and footer_blocks[-1].get("type") == "actions":
            footer_blocks[-1]["elements"].append(build_link_dashboard_button())
        else:
            footer_blocks.append({"type": "actions", "elements": [build_link_dashboard_button()]})
    return footer_blocks


async def _handle_compact_command(
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
) -> None:
    """Trigger in-place ACP ``/compact`` on the current thread's session."""
    provider = sessions.get_provider(session_key)
    if not provider:
        await slack.post_message(channel, "No active session to compact.", reply_ts)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="compact",
            tool_kind="command",
            outcome="no_session",
        )
        return

    _t0 = time.monotonic()

    # --- Phase 1: Pre-compaction UI (cosmetic — log failures, don't abort) ---
    try:
        await slack.add_reaction(channel, msg_ts, "recycle")
        await slack.post_message(channel, "🔄 Compacting context…", reply_ts)
    except Exception:
        logger.debug("Pre-compact UI failed for %s", session_key, exc_info=True)

    # --- Phase 2: Actual compaction (failures warrant error + session teardown) ---
    result_text: str | None = None
    outcome = "unknown"
    try:

        async def _run_compact_stream() -> None:
            nonlocal result_text, outcome
            async for event in provider.stream_command("/compact"):
                if event.kind == EVENT_COMPACTION_STATUS:
                    if event.text == "completed":
                        summary = event.title or ""
                        result_text = (
                            f"✅ Compacted: {summary}" if summary else "✅ Context compacted."
                        )
                        outcome = "completed"
                    elif event.text == "failed":
                        error = event.title or "unknown error"
                        result_text = f"❌ Compaction failed: {error}"
                        outcome = "failed"
                elif event.kind == EVENT_COMPLETE:
                    break

        await asyncio.wait_for(_run_compact_stream(), timeout=120)

        # kiro-cli fires compaction asynchronously after EVENT_COMPLETE —
        # wait for the real result, mirroring the dashboard's deferred path.
        if not result_text:
            cr = await provider.wait_for_compaction(timeout=120.0)
            if cr["type"] == "completed":
                summary = cr.get("summary", "")
                result_text = f"✅ Compacted: {summary}" if summary else "✅ Context compacted."
                outcome = "completed"
            elif cr["type"] == "failed":
                error = cr.get("summary", "")
                result_text = f"❌ Compaction failed: {error}" if error else "❌ Compaction failed."
                outcome = "failed"
            else:
                result_text = "⚠️ Compaction timed out."
                outcome = "timeout"
    except Exception:
        logger.warning("Compact command failed for %s", session_key, exc_info=True)
        try:
            await slack.post_message(channel, "❌ Compaction failed unexpectedly.", reply_ts)
        except Exception:
            logger.debug("Failed to post compact error for %s", session_key, exc_info=True)
        try:
            await sessions.destroy(session_key)
        except Exception:
            logger.warning(
                "Failed to destroy session %s after compact failure", session_key, exc_info=True
            )
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="compact",
            tool_kind="command",
            outcome="failed",
            error="exception",
        )
        try:
            await slack.remove_reaction(channel, msg_ts, "recycle")
            await _add_phase_reaction(slack, channel, msg_ts, "done")
        except Exception:
            pass
        return

    # --- Phase 3: Post-compaction reporting (log failures, don't mislead) ---
    try:
        result_text, _ = redact_exfiltration_urls(result_text)
        result_text, _ = redact_credentials(result_text)
        await slack.post_message(channel, result_text, reply_ts)

        elapsed = time.monotonic() - _t0
        footer_blocks, footer_text = build_timing_footer(elapsed)
        await slack.post_blocks(channel, footer_blocks, footer_text, reply_ts)
    except Exception:
        logger.debug("Post-compact reporting failed for %s", session_key, exc_info=True)

    try:
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="compact",
            tool_kind="command",
            outcome=outcome,
        )
    except Exception:
        logger.debug("Failed to log compact outcome for %s", session_key, exc_info=True)
    try:
        await slack.remove_reaction(channel, msg_ts, "recycle")
        await _add_phase_reaction(slack, channel, msg_ts, "done")
    except Exception:
        pass


async def maybe_handle_keyword_command(
    text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None = None,
    *,
    subagent_manager: SubagentManager | None = None,
    task_runner: TaskRunner | None = None,
    cron_service: CronService | None = None,
    handle_sessions: bool = True,
    channel_agent: str | None = None,
) -> bool:
    """Intercept the path-independent keyword commands.

    These are plain (non-``!``) keyword commands that must behave identically
    on both the native ``handle_message`` path and the messaging-transport
    ``handle_message_transport`` path: ``sessions``, ``spawn <task>``,
    ``run <spec>`` and natural-language ``cron`` wakeups.

    Returns ``True`` when the message was handled as a keyword command — the
    caller MUST then ``return`` without starting an LLM turn. Returns ``False``
    when the message is not a keyword command and normal routing continues.

    ``!``-bang commands are intentionally NOT handled here; they stay in
    ``handle_message`` (owner/allowed gating, mention stripping, modifiers) and
    are being deprecated in favour of slash commands. Slash commands are
    already path-independent (handled upstream of the native-vs-transport gate),
    so they need no porting.

    *handle_sessions* lets the native path opt out of the ``sessions`` branch
    (it keeps its own earlier, position-sensitive ``sessions`` block so that
    ``!temporary``/``!incognito`` modifier rewrites cannot turn a modified
    message into a bare ``sessions`` match). The transport path has no such
    modifier machinery, so it uses the default and handles all four commands.
    """
    # Resolve the agent so the command-intercept persists record the real agent
    # name in session metadata (thread override, then channel override, then
    # global default), matching handle_message's main path.
    _agent = _thread_agents.get(session_key) or channel_agent or _get_default_agent() or None
    # ── Sessions keyword: list recent sessions (owner/allowed only) ──
    if handle_sessions and text.strip().lower() == "sessions":
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
        return True

    # ── Subagent spawn: "spawn <task>" (before cron to avoid NL overlap) ──
    if subagent_manager:
        spawn_reply = _handle_spawn_command(text, subagent_manager, session_key)
        if spawn_reply:
            await slack.post_message(channel, spawn_reply, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                save_conversation_turn(
                    conversation_log,
                    session_key,
                    text,
                    spawn_reply,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return True

    # ── Task runner: "run <spec-path>" ──
    if task_runner:
        run_reply = _handle_run_command(text, task_runner, slack, channel, reply_ts)
        if run_reply:
            await slack.post_message(channel, run_reply, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                save_conversation_turn(
                    conversation_log,
                    session_key,
                    text,
                    run_reply,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return True

    # ── Natural language cron: intercept wakeup patterns ──
    if cron_service:
        cron_reply = _handle_cron_command(text, cron_service, channel, reply_ts)
        if cron_reply:
            await slack.post_message(channel, cron_reply, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                save_conversation_turn(
                    conversation_log,
                    session_key,
                    text,
                    cron_reply,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return True

    return False


async def maybe_route_linked_thread(
    text: str,
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    reply_ts: str,
) -> bool:
    """Route a Slack message to a linked dashboard slot, if one is linked.

    Shared by the native ``handle_message`` path and the messaging-transport
    ``handle_message_transport`` path so a thread linked via
    ``/kirocrew link-to-dashboard`` behaves identically on both.

    Returns ``True`` when the caller MUST return without further handling —
    either the message was routed into the linked dashboard slot, or an
    unauthorized user was denied. Returns ``False`` when normal routing should
    continue: no dashboard state, no linked slot, or a ``!``-bang command
    (which is intentionally allowed to fall through to normal handling).
    """
    if not (_state._dashboard_state and hasattr(_state._dashboard_state, "get_linked_slot")):
        return False
    # The dashboard _slack_to_slot map is keyed by the bare Slack thread_ts
    # (reply_ts), NOT the namespaced session key — look up with reply_ts so
    # canonical ``slack:<ts>`` session keys still hit linked slots. session_key
    # is kept for the SEL logging below.
    _linked_slot = _state._dashboard_state.get_linked_slot(reply_ts)
    if not _linked_slot:
        return False

    # Auth check FIRST — deny all messages from unauthorized users.
    if not is_allowed_user(user_id):
        logger.warning("Unauthorized user %s in linked thread %s", user_id, session_key)
        sel().log_tool_invocation(
            session_key=session_key,
            agent="kirocrew",
            source="slack",
            tool_name="linked_thread_intercept",
            tool_kind="permission",
            outcome="denied",
            metadata={"user_id": user_id, "reason": "not_allowed_user"},
        )
        await slack.post_message(channel, "Not authorized.", reply_ts)
        return True

    # Let bang commands fall through to normal handling.
    _first_word = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    if _first_word in _BANG_TO_SLASH:
        return False

    _linked_slot_key = _linked_slot.key
    # Redact for UI display only — LLM receives original text so it can process
    # user intent fully (redaction strips URLs/creds that may be relevant
    # context). The LLM's own output is redacted before display.
    _safe_text, _ = redact_exfiltration_urls(text)
    _safe_text, _ = redact_credentials(_safe_text)
    _linked_slot.append("user", _safe_text, "msg msg-u")
    _state._dashboard_state.broadcast_ws("chat_message", {"slot": _linked_slot_key, "role": "user", "content": _safe_text, "cls": "msg msg-u"})  # type: ignore[attr-defined]
    if not _linked_slot.running:
        from kiro_crew.dashboard.chat import _run_chat

        _chat_task = asyncio.create_task(_run_chat(_state._dashboard_state, _linked_slot, text))  # type: ignore[arg-type]
        _linked_slot.task = _chat_task
        _state._dashboard_state._background_tasks.add(_chat_task)  # type: ignore[attr-defined]
        _chat_task.add_done_callback(_state._dashboard_state._background_tasks.discard)  # type: ignore[attr-defined]
    else:
        _linked_slot.queue_append(text)
    _state._dashboard_state.push_slots_update()  # type: ignore[attr-defined]
    sel().log_tool_invocation(
        session_key=session_key,
        agent="kirocrew",
        source="slack",
        tool_name="linked_thread_intercept",
        tool_kind="permission",
        outcome="allowed",
        metadata={"user_id": user_id, "slot": _linked_slot_key},
    )
    logger.info("Routed linked Slack message to dashboard slot %s", _linked_slot_key)
    return True
