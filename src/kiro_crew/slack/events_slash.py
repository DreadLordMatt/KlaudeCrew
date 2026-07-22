"""KiroCrew Slack slash-command handlers and dispatcher.

Contains the built-in ``/kirocrew <sub-command>`` handlers, the slash-command
dispatcher (``_handle_slash``), and the channel-join prompt stub
(``_maybe_prompt_owner``). Module-level ``register_slash_command`` calls
populate the shared registry (owned by :mod:`kiro_crew.slack.events_core`) at
import time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import aiohttp

from kiro_crew.dashboard.token_auth import LINK_WINDOW_SECS, MAX_SESSION_TTL_SECS, parse_duration
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import safe_read_file
from kiro_crew.platform import current_context
from kiro_crew.safety_override import safety_override
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.slack.allowlist import prompt_track_channel, send_dashboard_link
from kiro_crew.slack.blocks import (
    channels_modal,
    dashboard_link_block,
    voice_config_modal,
)
from kiro_crew.slack.events_core import (
    SLASH_REGISTRY,
    _build_help_text,
    _safe_log,
    _spawn_tracked,
    register_slash_command,
)
from kiro_crew.slack.handler import (
    _YOLO_TTL_SECS,
    is_allowed_user,
    is_owner,
)
from kiro_crew.slack.sessions_view import (
    _SESSIONS_DEFAULT_LIMIT,
    _build_sessions_blocks,
    _collect_recent_sessions,
)
from kiro_crew.stats import Stats

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Built-in slash sub-command handlers
# ---------------------------------------------------------------------------


async def _handle_dashboard(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Generate presigned dashboard link and DM to caller."""
    ttl = 3600
    if args:
        parsed = parse_duration(args.split()[0])
        if parsed is None:
            await respond(f"Usage: `/{orch.slack_command} dashboard [<N>h|<N>m]`")
            return
        ttl = parsed

    session_ttl = min(ttl, MAX_SESSION_TTL_SECS)
    assert orch.slack is not None
    url = await send_dashboard_link(orch.slack, caller_id, session_ttl)
    if url:
        blks = dashboard_link_block(url, LINK_WINDOW_SECS // 60, session_ttl // 60)
        await respond("🔗 Dashboard link sent to your DMs.", blocks=blks)
    else:
        await respond("❌ Failed to send dashboard link.")


async def _handle_agent(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Switch agent directly if valid name given, otherwise show selector."""
    from kiro_crew.slack.handler import (  # circular import: handler.py imports events.py for command dispatch, creating runtime circular dependency
        _get_default_agent,
        _resolve_agent_name,
        _set_default_agent,
        is_owner,
    )

    if not is_owner(caller_id):
        await respond("⛔ Only the owner can switch agents.")
        return

    # Direct switch if arg provided
    if args:
        name = args.strip().split()[0]
        if name.lower() in ("off", "default"):
            _set_default_agent("")
            await respond("🔄 Reset to default agent.")
            return
        resolved = _resolve_agent_name(name)
        if resolved:
            _set_default_agent(resolved)
            await respond(f"🔄 Switched to agent: *{resolved}*")
            return
        await respond(f"❌ Unknown agent `{name}`. Pick one below:")

    # Show selector dropdown
    agents_dir = Path.home() / ".kiro" / "agents"
    jsons = sorted(agents_dir.glob("*.json")) if agents_dir.is_dir() else []
    agent_names = sorted(f.stem for f in jsons)
    current = _get_default_agent() or ""

    options = [{"text": {"type": "plain_text", "text": n[:75]}, "value": n} for n in agent_names]
    options.append({"text": {"type": "plain_text", "text": "off (default)"}, "value": "off"})
    initial = next((o for o in options if o["value"] == current), options[-1])

    blks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Current agent:* {current or 'default'}"},
            "accessory": {
                "type": "static_select",
                "action_id": "mc_agent_select",
                "options": options,
                "initial_option": initial,
            },
        },
    ]
    await respond("Select an agent:", blocks=blks)


async def _handle_voice(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Open voice config modal with current TTS settings."""
    from kiro_crew.slack.handler import (
        _vc,  # circular import: handler.py imports events.py for command dispatch
    )

    trigger_id = getattr(orch, "_last_trigger_id", "")
    if not trigger_id:
        await respond("❌ Missing trigger_id — cannot open modal.")
        return

    modal = voice_config_modal(
        tts_enabled=_vc.global_enabled,
        auto_speak=getattr(_vc, "auto_speak", False),
        voice=_vc.default_voice,
        engine=_vc.default_engine,
        speed=_vc.default_rate,
        pitch=_vc.default_pitch,
        aws_profile=_vc.aws_profile,
        region=_vc.region,
    )

    try:
        assert orch.slack is not None
        await orch.slack.views_open(trigger_id=trigger_id, view=modal)
    except Exception:
        logger.exception("Failed to open voice config modal")
        await respond("❌ Failed to open voice settings modal.")


register_slash_command("dashboard", _handle_dashboard, "get a dashboard access link")
register_slash_command("agent", _handle_agent, "switch the active agent")
register_slash_command("voice", _handle_voice, "configure TTS voice settings")


async def _handle_yolo(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Toggle YOLO mode on/off/renew."""
    if not is_owner(caller_id):
        await respond("⛔ Only the owner can toggle YOLO mode.")
        return

    arg = args.strip().lower()
    so = safety_override()

    if arg == "on":
        if so.is_active():
            remaining = so.remaining_secs()
            await respond(f"🟢 YOLO mode is already *ON* ({remaining // 60}min remaining).")
            return
        result = so.activate("slack")
        if not result.active:
            await respond("❌ Failed to activate YOLO mode (audit system unavailable).")
            return
        sel().log_api_access(
            caller=caller_id,
            operation="slack.yolo_mode",
            outcome="allowed",
            source="slack",
            resources="yolo_on",
        )
        if orch.dashboard_state:
            orch.dashboard_state.push_slots_update()
        await respond(
            f"🟢 YOLO mode *ON* (auto-expires in {_YOLO_TTL_SECS // 60}min) — all tools auto-approved."
        )
    elif arg == "off":
        from kiro_crew.slack.handler import (
            disable_yolo,  # circular import: handler.py imports events.py for command dispatch registration
        )

        disable_yolo()
        sel().log_api_access(
            caller=caller_id,
            operation="slack.yolo_mode",
            outcome="allowed",
            source="slack",
            resources="yolo_off",
        )
        if orch.dashboard_state:
            orch.dashboard_state.push_slots_update()
        await respond("🔴 YOLO mode *OFF* — tools require approval.")
    elif arg == "renew":
        renew_result = so.renew("slack")
        if renew_result.renewed:
            sel().log_api_access(
                caller=caller_id,
                operation="slack.yolo_mode",
                outcome="renewed",
                source="slack",
                resources="yolo_renew",
            )
            if orch.dashboard_state:
                orch.dashboard_state.push_slots_update()
            await respond(f"🟢 YOLO mode *renewed* (auto-expires in {renew_result.ttl // 60}min).")
        else:
            await respond("🔴 YOLO mode is not active. Use `on` to activate first.")
    else:
        if so.is_active():
            remaining = so.remaining_secs()
            await respond(
                f"YOLO mode is currently *ON 🟢* ({remaining // 60}min remaining).\nUsage: `/{orch.slack_command} yolo on|off|renew`"
            )
        else:
            await respond(
                f"YOLO mode is currently *OFF 🔴*.\nUsage: `/{orch.slack_command} yolo on|off|renew`"
            )


async def _handle_config(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Open config modal (owner-only) — users and channels."""
    if not is_owner(caller_id):
        await respond("⛔ Only the owner can change config.")
        return

    tracking_ids = list(orch._tracking_channels)

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "⚠️ Multi-user access is disabled for security. Only the owner can interact via Slack.",
            },
        },
        {
            "type": "input",
            "block_id": "channels_block",
            "label": {"type": "plain_text", "text": "Tracked Channels"},
            "element": {
                "type": "multi_channels_select",
                "action_id": "mc_config_channels",
                "placeholder": {"type": "plain_text", "text": "Select channels"},
                **({"initial_channels": tracking_ids} if tracking_ids else {}),
            },
            "optional": True,
        },
    ]

    view = {
        "type": "modal",
        "callback_id": "mc_config_panel",
        "title": {"type": "plain_text", "text": "KiroCrew Config"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }

    trigger_id = getattr(orch, "_last_trigger_id", "")
    if not trigger_id:
        await respond("⚠️ Cannot open modal — missing trigger_id.")
        return

    try:
        assert orch.slack is not None
        await orch.slack.views_open(trigger_id=trigger_id, view=view)
    except Exception:
        logger.exception("Failed to open config modal")
        await respond("❌ Failed to open config modal.")


register_slash_command("yolo", _handle_yolo, "toggle YOLO mode (auto-approve tools)")
register_slash_command("config", _handle_config, "manage users and channels (owner-only)")


async def _handle_allowlist_cmd(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Multi-user access disabled — user management is blocked."""
    await respond(
        "⛔ Multi-user access is disabled for security. Only the owner can use KiroCrew via Slack."
    )


def _get_agent_names() -> list[str]:
    """Return sorted list of installed agent names from ~/.kiro/agents/.

    Reads each agent JSON's ``name`` field via ``hooks.safe_read_file`` so
    symlinks into sensitive paths (e.g. ``~/.aws/credentials``) are blocked
    by ``is_sensitive_path()``. Falls back to the filename stem when the
    file cannot be read safely or the JSON does not carry a usable name.

    When a read is blocked by ``is_sensitive_path()``, a SEL audit event
    (``sensitive_path_blocked``) is emitted so the attempt is observable.
    """
    agents_dir = Path.home() / ".kiro" / "agents"
    if not agents_dir.is_dir():
        return []
    names = []
    for f in agents_dir.glob("*.json"):
        try:
            data = json.loads(safe_read_file(str(f)))
            name = data.get("name") if isinstance(data, dict) else None
        except PermissionError as exc:
            # Symlink or resolved path landed in a sensitive location — audit it.
            try:
                sel().log_api_access(
                    caller="system",
                    operation="sensitive_path_blocked",
                    outcome="denied",
                    source="slack.events._get_agent_names",
                    resources=str(f),
                    error=str(exc),
                )
            except Exception:
                logger.debug(
                    "Failed to emit SEL audit event for blocked agent read: %s",
                    f,
                    exc_info=True,
                )
            name = None
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # UnicodeDecodeError (a ValueError subclass, NOT an OSError) is
            # raised by safe_read_file's utf-8 read on a non-UTF-8 *.json —
            # e.g. a macOS AppleDouble ._foo.json stub in ~/.kiro/agents.
            # Without it here the raise escaped and killed the `/kirocrew
            # channels` handler / channel-modal refresh task before it opened.
            # Mirrors the 90e3cccc fix to agent.py's _load_json/_enforce_denied.
            name = None
        names.append(name or f.stem)
    return sorted(names)


async def _handle_channel_cmd(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Open modal showing tracked channels with per-channel activation mode."""
    if not is_owner(caller_id):
        await respond("⛔ Only the owner can manage tracked channels.")
        return

    current_ids = sorted(orch._tracking_channels)
    channels = [
        {
            "channel_id": cid,
            "activation": orch._cfg.channel_config(cid).activation,
            "agent": orch._cfg.channel_config(cid).agent,
        }
        for cid in current_ids
    ]
    agent_names = _get_agent_names()
    modal = channels_modal(channels, agent_names=agent_names)

    trigger_id = getattr(orch, "_last_trigger_id", "")
    if not trigger_id:
        await respond("⚠️ Cannot open modal — missing trigger_id.")
        return
    try:
        assert orch.slack is not None
        await orch.slack.views_open(trigger_id=trigger_id, view=modal)
    except Exception:
        logger.exception("Failed to open channels modal")
        await respond("❌ Failed to open channels modal.")


register_slash_command("users", _handle_allowlist_cmd, "manage allowed users")
register_slash_command("channels", _handle_channel_cmd, "manage tracked channels")


async def _handle_sessions(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """List last 10 sessions as task_card blocks with resume buttons."""
    # Deny-by-default authorization gate (defense-in-depth).
    #
    # Session JSONLs contain prior conversation contents — only owner /
    # explicitly-allowed users may read them. The dispatcher in
    # ``_handle_slash`` already filters slash commands by allowlist, so in
    # production this branch is unreachable today. The check is still
    # required by the AUTOSDE security-controls rule (deny-by-default) and
    # protects against future refactors that bypass the dispatcher gate.
    # The ``sessions`` keyword path applies the same check at handler.py
    # before delegating to ``_handle_sessions_command``.
    if not (is_owner(caller_id) or is_allowed_user(caller_id)):
        sel().log_api_access(
            caller=caller_id,
            operation="slack.sessions_slash_data_access",
            outcome="denied",
            source="slack",
            resources=args or "",
            error="unauthorized caller",
        )
        await respond("_Permission denied._")
        return

    # Wrap the collector so a transient OSError still produces a SEL audit
    # entry. Without this, an IO failure would skip the audit entirely and
    # the access attempt would be invisible to the security pipeline. The
    # Home Tab error-path (in ``_publish_home_tab``) follows the same
    # pattern: capture the exception, redact-then-truncate the message,
    # and emit an ``error=`` audit field.
    try:
        rows = _collect_recent_sessions(
            orch.sessions if orch is not None else None,
            limit=_SESSIONS_DEFAULT_LIMIT,
        )
    except Exception as exc:
        # Redact-then-truncate: redact() first so credential / exfil
        # patterns aren't split mid-string by the truncation step. The
        # SEL on-disk file is not internally redacted (sel.py only
        # redacts on forward), so this is defense-in-depth for the
        # AUTOSDE security-controls "never trust output" rule applied
        # to exception messages from third-party libraries.
        redacted_exc, _ = redact_exfiltration_urls(str(exc))
        redacted_exc, _ = redact_credentials(redacted_exc)
        sel().log_api_access(
            caller=caller_id,
            operation="slack.sessions_slash_data_access",
            outcome="error",
            source="slack",
            resources="0 sessions read (collector failed)",
            error=redacted_exc[:200],
        )
        logger.exception("slash sessions: collector failed for caller %s", caller_id)
        await respond("_Sessions unavailable._")
        return

    sel().log_api_access(
        caller=caller_id,
        operation="slack.sessions_slash_data_access",
        outcome="allowed",
        source="slack",
        resources=f"{len(rows)} sessions read",
    )

    if not rows:
        await respond("_No recent sessions._")
        return

    blocks = _build_sessions_blocks(rows)
    await respond("\U0001f4cb Recent sessions:", blocks=blocks)


register_slash_command("sessions", _handle_sessions, "list recent sessions")


async def _handle_status(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Show runtime stats summary."""
    # Identity status via the active PlatformContext (Default == OSS no-op stub
    # returning ""; Amazon companion returns the real Midway status line).
    mw_line = await current_context().identity.status_line(prefix=" · midway")
    await respond(Stats().summary() + mw_line)


register_slash_command("status", _handle_status, "show runtime stats")


async def _handle_restart(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Restart the gateway process (owner-only, requires systemd supervisor)."""
    if not is_owner(caller_id):
        sel().log_tool_invocation(
            session_key="", source="slack", tool_name="/kirocrew restart",
            outcome="denied", resources=f"user={caller_id}",
        )
        await respond("⛔ Only the owner can restart the gateway.")
        return

    if not os.environ.get("INVOCATION_ID"):
        sel().log_tool_invocation(
            session_key="", source="slack", tool_name="/kirocrew restart",
            outcome="denied", resources=f"user={caller_id},reason=no_supervisor",
        )
        await respond(
            "⛔ Restart requires a process supervisor (systemd). "
            "Running in bare mode — restart manually."
        )
        return

    sel().log_tool_invocation(
        session_key="", source="slack", tool_name="/kirocrew restart",
        outcome="approved", resources=f"user={caller_id}",
    )
    try:
        await respond("♻️ Restarting gateway…")
    except Exception:
        logger.debug("Restart notification failed", exc_info=True)
    try:
        if orch.dashboard_state and hasattr(orch.dashboard_state, "push_update_progress"):
            # circular import: dashboard.chat imports events via orchestrator
            # setup; events imports dashboard.chat for slot persistence
            from kiro_crew.dashboard.chat import save_all_slots_to_history

            orch.dashboard_state.push_update_progress("restarting", "Restarting server…")
            # save_all_slots_to_history does synchronous per-slot file I/O that can
            # block on a wedged disk; offload it to the dedicated subprocess_executor
            # (the pool reserved for potentially-hanging teardown work) bounded by
            # wait_for so it cannot freeze the event loop and prevent os._exit(1)
            # below. Using the default pool risks starving other default-pool
            # consumers if the thread wedges.
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), save_all_slots_to_history, orch.dashboard_state
                ),
                timeout=5.0,
            )
    except Exception:
        logger.debug("Dashboard state save before restart failed", exc_info=True)
    try:
        if orch.sessions:
            # Bound the cleanup: a session close can hang waiting on a remote
            # peer, and os._exit() below would otherwise never be reached.
            # asyncio.TimeoutError subclasses Exception, so the handler proceeds
            # to exit on timeout.
            await asyncio.wait_for(orch.sessions.close_all(), timeout=5.0)
    except Exception:
        logger.debug("Session cleanup before restart failed", exc_info=True)
    # Flush the SEL audit queue: logging is async (background writer thread +
    # atexit flush) and os._exit() runs neither atexit handlers nor finalizers,
    # so the approved-restart audit event above would be lost. flush() is a
    # synchronous blocking drain, so offload it to an executor bounded by
    # wait_for — a wedged writer (disk full, unreachable sink) must not block
    # the loop indefinitely and prevent os._exit(1) from being reached.
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(subprocess_executor(), sel().flush),
            timeout=3.0,
        )
    except Exception:
        logger.debug("SEL flush before restart failed", exc_info=True)
    os._exit(1)


register_slash_command("restart", _handle_restart, "restart the gateway (owner-only)")


# -------------------------------------------------------------------------
# Slash command dispatcher
# -------------------------------------------------------------------------


async def _handle_slash(orch: GatewayOrchestrator, payload: dict) -> None:
    """Route ``/kirocrew <sub-command>`` via :data:`SLASH_REGISTRY`.

    Falls back to @user / #channel mention handling, then help text.
    """
    cmd = payload.get("command", "")
    cmd_text = payload.get("text", "").strip()
    caller_id = payload.get("user_id", "")
    response_url = payload.get("response_url", "")
    logger.info("Slash command: %s %s (caller=%s)", cmd, _safe_log(cmd_text), caller_id)

    async def _respond(text: str, blocks: list[dict] | None = None) -> None:
        if not response_url:
            return
        try:
            body: dict = {"text": text, "response_type": "ephemeral"}
            if blocks:
                body["blocks"] = blocks
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json=body)
        except Exception:
            logger.debug("slash response_url failed", exc_info=True)

    slash_command = f"/{orch.slack_command}"
    if cmd != slash_command:
        return

    # Deny-by-default — only allowed users can invoke slash commands
    if not is_allowed_user(caller_id):
        sel().log_api_access(
            caller=caller_id,
            operation="slack.slash_command",
            outcome="denied",
            source="slack",
            resources=cmd_text,
            error="unauthorized sender",
        )
        _spawn_tracked(_respond("⛔ You are not authorized to use this command."))
        return

    sel().log_api_access(
        caller=caller_id,
        operation="slack.slash_command",
        outcome="allowed",
        source="slack",
        resources=cmd_text,
    )

    if not (orch.slack and orch._owner_id):
        _spawn_tracked(_respond("⚠️ Owner not configured."))
        return

    # Parse sub-command and args
    parts = cmd_text.split(maxsplit=1)
    sub_cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    # Registry lookup
    entry = SLASH_REGISTRY.get(sub_cmd)
    if entry is not None:
        handler, _ = entry
        # Stash trigger_id so modal-opening handlers can use it
        orch._last_trigger_id = payload.get("trigger_id", "")  # type: ignore[attr-defined]
        _spawn_tracked(handler(orch, caller_id, args, _respond))
        return

    # Fallback: @user mention — multi-user access disabled for security
    user_match = re.search(r"<@([A-Z0-9]+)(?:\|([^>]+))?>", cmd_text)
    if user_match:
        _spawn_tracked(
            _respond("⛔ Multi-user access is disabled. Only the owner can use KiroCrew via Slack.")
        )
        return

    # Fallback: #channel mention — Slack sends <#C1234|name> or <#C1234>
    channel_match = re.search(r"<#([A-Z0-9]+)(?:\|([^>]*))?>", cmd_text)
    if channel_match:
        channel_id = channel_match.group(1)
        channel_name = channel_match.group(2) or "Secret"
        _spawn_tracked(
            prompt_track_channel(orch.slack, orch._owner_id, channel_id, channel_name)
        )
        _spawn_tracked(_respond(f"📨 Track request sent for #{channel_name or channel_id}."))
        return

    # Unknown sub-command → help
    _spawn_tracked(_respond(_build_help_text(orch.slack_command)))


# -------------------------------------------------------------------------
# Tracking-channel join
# -------------------------------------------------------------------------


def _maybe_prompt_owner(orch: GatewayOrchestrator, event: dict) -> None:
    """Multi-user access disabled — channel-join allowlist prompts are blocked."""
    return
