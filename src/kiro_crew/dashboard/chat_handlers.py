"""HTTP API handlers for dashboard chat endpoints."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from kiro_crew.constants import CHAT_TURN_TIMEOUT
from kiro_crew.dashboard.chat_orchestrator import _stage_loop
from kiro_crew.dashboard.chat_persistence import _redact_meta
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_title import _maybe_auto_title
from kiro_crew.dashboard.chat_utils import (
    _THEME_PERSONAS,
    _build_stream_chunk,
    _emit_agent_assignment,
    _redact_for_display,
)
from kiro_crew.dashboard.state import DashboardState, _mark_permission_resolved
from kiro_crew.safety_override import safety_override
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import SecurityEvent, sel
from kiro_crew.validation import _AGENT_NAME_RE

logger = logging.getLogger(__name__)


async def api_chat(request: web.Request) -> web.StreamResponse:
    """POST /api/chat — send message to a slot, stream response via SSE."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    message = body.get("message", "").strip()
    agent = body.get("agent", "")
    slot_name = body.get("slot")
    color_theme = body.get("color_theme", "")
    user_meta = body.get("meta")  # knowledge/files/pastes metadata from frontend
    if not isinstance(user_meta, dict):
        user_meta = None
    if not isinstance(color_theme, str) or color_theme not in {"", *_THEME_PERSONAS}:
        color_theme = ""
    if not isinstance(agent, str) or not (agent == "" or _AGENT_NAME_RE.match(agent)):
        _emit_agent_assignment(str(slot_name or ""), str(agent), outcome="denied_invalid")
        return web.json_response({"error": "invalid agent name"}, status=400)
    if not isinstance(slot_name, str) and slot_name is not None:
        slot_name = None  # coerce non-string slot to auto-generate

    # Honor memory_mode from the body when auto-creating a slot (e.g. AgentRock
    # skill dispatch defaults to "temporary"). Only validated values are passed
    # through; anything else is dropped so get_or_create_slot uses its default.
    # If the slot already exists, get_or_create_slot raises on a memory_mode
    # mismatch, matching POST /api/chat/slots semantics.
    requested_memory_mode = body.get("memory_mode")
    if requested_memory_mode not in ("persistent", "incognito", "temporary"):
        requested_memory_mode = None

    try:
        slot = state.get_or_create_slot(
            slot_name,
            app=request.get("app", ""),
            memory_mode=requested_memory_mode,
        )
    except ValueError as exc:
        sel().log_api_access(
            caller=request.get("app", ""),
            operation="chat_send",
            outcome="denied",
            source="memory_mode_mismatch",
            resources=f"slot={slot_name}",
            error=str(exc),
        )
        return web.json_response({"error": str(exc)}, status=409)

    # App ownership check (App Kit §5.2): deny-by-default for app tokens.
    # Apps can only access slots they own. Dashboard users (empty request_app)
    # can access everything.
    request_app = request.get("app", "")
    if request_app:
        if not slot._app:
            # Unscoped slot created by dashboard — apps cannot access it.
            sel().log_api_access(
                caller=request_app,
                operation="chat_send",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot.key}",
                error="app cannot access unscoped slots",
            )
            return web.json_response({"error": "not found"}, status=404)
        elif request_app != slot._app:
            sel().log_api_access(
                caller=request_app,
                operation="chat_send",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot.key}",
                error="app does not own this slot",
            )
            return web.json_response({"error": "not found"}, status=404)

    if slot.agent not in (None, ""):
        # Slot already has an agent — only reject explicit mismatches (non-empty different agent).
        # Empty agent in request means "use existing" (e.g. follow-up messages from frontend).
        if agent and slot.agent != agent:
            _emit_agent_assignment(slot.key, agent or "", outcome="denied_mismatch")
            return web.json_response({"error": "slot agent mismatch"}, status=409)
        else:
            logger.debug("agent match for slot=%s agent=%s", slot.key, agent)
    elif agent:
        # Slot has no agent — set it if not running
        if slot.running:
            _emit_agent_assignment(slot.key, agent, outcome="denied_running")
            return web.json_response(
                {"error": "cannot set agent on running slot"},
                status=409,
            )
        slot.agent = agent
        _emit_agent_assignment(slot.key, agent)
    else:
        # No agent on slot, no agent in request — nothing to enforce.
        pass

    if "color_theme" in body:
        slot.color_theme = color_theme

    if slot.running:
        # Mid-turn steer: inject into the RUNNING turn instead of queueing for
        # the next turn. Gated on an explicit `steer` flag + a live, steer-capable
        # inner AcpClient that _run_chat published on the slot. Fire-and-forget —
        # the inline steer card materializes when kiro-cli echoes steering_consumed
        # (EVENT_STEER_CONSUMED). If steer is requested but unavailable (no live
        # client / unsupported backend / RPC error), fall through to the queue
        # path so the user's text is NEVER silently dropped.
        if body.get("steer") and message:
            _client = slot._acp_client
            if _client is not None and getattr(_client, "supports_steer", False):
                # Register as pending BEFORE the await: _client.steer() suspends
                # on stdin.drain(), and if the turn's finally runs during that
                # suspension it must already see this steer to requeue it
                # (append-after-await would land on an idle slot and orphan the
                # message — zedmor's review, CR-290015501). The force-stop
                # clear() likewise races correctly: a hard kill during the
                # await discards the entry, so a late write can't resurrect it.
                slot._pending_steers.append(message)
                try:
                    steered = await _client.steer(message)
                except Exception as exc:  # best-effort — fall through to queue
                    logger.warning("steer failed for slot %s: %s", slot.key, exc)
                    steered = False
                if not steered:
                    # Unwind the optimistic registration so the queue fallback
                    # below doesn't double-deliver. If the entry is already
                    # gone, the turn's finally requeued it (or a hard kill
                    # discarded it) during the await — either way the message
                    # is accounted for, so skip the fallback.
                    try:
                        slot._pending_steers.remove(message)
                    except ValueError:
                        return web.json_response({"ok": True, "queued": True})
                if steered:
                    _ts = datetime.now(timezone.utc).isoformat()
                    # Sanitize: same chain as the queue path.
                    _sanitized, _ = redact_exfiltration_urls(message)
                    _sanitized, _ = redact_credentials(_sanitized)
                    _redacted = _redact_for_display(_sanitized)
                    # Persist the steered message so it survives page reload
                    # (dirty-flush picks it up on next save cycle). Store the
                    # sanitized form — raw content must never reach an external
                    # surface (AUTOSDE security-controls).
                    slot.append(
                        "user",
                        _sanitized,
                        "msg msg-u",
                        ts=_ts,
                        meta={"steer": True},
                    )
                    state.broadcast_ws(
                        "steer_push",
                        {
                            "slot": slot.key,
                            "content": _redacted,
                            "ts": _ts,
                        },
                    )
                    return web.json_response({"ok": True, "steered": True})
            # steer requested but unavailable → fall through to queue below.
        # Queue the message — return JSON immediately (no SSE needed).
        # The existing SSE reader will pick up queued messages as _run_chat
        # processes the queue in its finally block.
        if message:
            qid = slot.queue_append(message)
            _c, _ = redact_exfiltration_urls(message)
            _c, _ = redact_credentials(_c)
            _redacted = _redact_for_display(_c)
            state.broadcast_ws(
                "queue_push",
                {
                    "slot": slot.key,
                    "content": _redacted,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "queue_id": qid,
                },
            )
        return web.json_response({"ok": True, "queued": True})

    if not message:
        return web.json_response({"error": "message is required"}, status=400)

    # Queue a message typed while background sub-agents are still running for
    # this slot. The slot.running queue path above covers the mid-turn case;
    # this covers the idle case (spawn_run is fire-and-forget, so the main slot
    # goes idle while children run). Without the hold, this message would start a
    # main turn immediately and interleave with the [Subagent completion event]
    # injections. Queue it instead (reusing the slot queue) — the queue drain
    # releases it after the last sub-agent finishes (see chat_runner _hold_users).
    # Always on: steering is the effective opt-out.
    if state.subagents is not None and state.subagents.running_agents_for(f"dashboard:{slot.key}"):
        qid = slot.queue_append(message)
        _c, _ = redact_exfiltration_urls(message)
        _c, _ = redact_credentials(_c)
        _redacted = _redact_for_display(_c)
        state.broadcast_ws(
            "queue_push",
            {
                "slot": slot.key,
                "content": _redacted,
                "ts": datetime.now(timezone.utc).isoformat(),
                "queue_id": qid,
            },
        )
        return web.json_response({"ok": True, "queued": True})

    # WS mode: return JSON immediately, chunks delivered via WebSocket
    ws_mode = request.query.get("ws") == "1"

    slot._has_reader = not ws_mode  # Only block SSE broadcast if HTTP SSE reader
    slot._file_changes = []  # Reset file-change accumulator for the new turn
    # ── Sweep orphaned permissions from prior turns ──
    _sweep_stale_permissions(slot)

    slot._browse_mode = bool(body.get("browse"))
    if slot._browse_mode and "[BROWSE]" not in message:
        message = "[BROWSE] " + message
    slot.append("user", message, "msg msg-u", meta=_redact_meta(user_meta) if user_meta else None)

    # Note: untitled slots display as "New Session…" via _ChatSlot.display_title
    # (serialization layer), so there's no bare chat-N flash to patch here. The
    # LLM titling is kicked off below, before _run_chat.

    # ── AutoNudge: user input cancels any pending nudge timer (user wins). ──
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_handlers
        )

        _autonudge = _autonudge_get()
        if _autonudge is not None:
            _autonudge.notify_user_input(slot.key)
    except Exception:
        logger.warning("autonudge.notify_user_input failed", exc_info=True)

    # ── Orchestrator "Go All" detection ─────────────────────────────
    # Deny-by-default trust boundary (P454989291 item 5): a turn tagged
    # origin="widget" was pre-filled into the composer by an LLM-emitted
    # <mcwidget> postMessage. Even though the frontend now requires a human
    # gesture to send it, the message TEXT is still attacker-controlled — an
    # injected widget can pre-fill "go all" and socially engineer the user
    # into pressing Enter. "go"/"go all" is the only chat-text-reachable
    # privilege escalation (it flips the orchestrator into unattended
    # per-stage auto-approval via slot._auto_run + _stage_loop), so we refuse
    # to honour it for widget-origin turns and let the text fall through to a
    # normal, fully-gated _run_chat turn instead. Mode changes and tool
    # approvals live on separate endpoints a widget iframe cannot reach.
    _widget_origin = bool(user_meta) and user_meta.get("origin") == "widget"
    if (
        getattr(slot, "mode", "") == "orchestrator"
        and message.strip().lower() in ("go", "go all")
        and _widget_origin
    ):
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex,
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="auto_run_denied",
                caller_identity=f"dashboard:{slot.key}",
                agent=getattr(slot, "agent", ""),
                source="dashboard",
                operation="go_typed_widget_origin",
                outcome="denied",
                resources=f"slot={slot.key}",
                error="orchestrator go/go-all refused for widget-origin turn",
            )
        )
        logger.warning(
            "Refused orchestrator auto-run escalation for widget-origin turn on slot %s",
            slot.key,
        )
    elif getattr(slot, "mode", "") == "orchestrator" and message.strip().lower() in (
        "go",
        "go all",
    ):
        _is_auto = message.strip().lower() == "go all"
        if _is_auto:
            slot._auto_run = True
            logger.info("Auto-run enabled for slot %s", slot.key)
            sel().log(
                SecurityEvent(
                    event_id=uuid.uuid4().hex,
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    event_type="auto_run_enabled",
                    caller_identity=f"dashboard:{slot.key}",
                    agent=getattr(slot, "agent", ""),
                    source="dashboard",
                    operation="go_all_typed",
                    outcome="approved",
                    resources=f"slot={slot.key}",
                )
            )
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex,
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="stage_approved",
                caller_identity=f"dashboard:{slot.key}",
                agent=getattr(slot, "agent", ""),
                source="dashboard",
                operation="go_typed",
                outcome="approved",
                resources=f"slot={slot.key}",
            )
        )
        # Use Python-controlled stage loop instead of _run_chat
        task = asyncio.create_task(_stage_loop(state, slot, auto_run=_is_auto))
        slot.task = task
        slot._recovery_retrigger_count = 0
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
        state.push_slots_update()
        # All output delivered via WebSocket — return JSON like api_chat_plan_action
        return web.json_response({"ok": True, "slot": slot.key})

    # ── Orchestrator stop detection ─────────────────────────────────
    _stop_words = {"stop", "cancel", "abort"}
    tracker = slot._orch_tracker
    if (
        tracker is not None
        and tracker.has_escalated
        and not tracker.stopped
        and message.strip().lower().split()[0] in _stop_words
    ):
        tracker.stop()
        slot._auto_run = False
        # Cancel running agents for this slot
        if state.subagents:
            session_key = f"dashboard:{slot.key}"
            mgr = state.subagents
            for a in mgr.running_agents_for(session_key):
                t = mgr._tasks.get(a["id"])
                if t and not t.done():
                    t.cancel()
        stop_msg = "🛑 [SYSTEM] Orchestration stopped by user."
        slot.append("assistant", stop_msg, "msg msg-a")
        state.broadcast_ws(
            "chat_message", {"slot": slot.key, "role": "assistant", "content": stop_msg}
        )
        state.broadcast_ws("chat_done", {"slot": slot.key})
        return web.json_response({"ok": True, "stopped": True})

    # ── Reset rounds after user guidance (not a stop) ───────────────
    if tracker is not None and tracker.has_escalated:
        tracker.reset_after_guidance()
        logger.info("Rounds reset after user guidance for slot %s", slot.key)

    # Drain stale pending messages from previous turns that completed
    # after their SSE reader disconnected. Must happen BEFORE _run_chat
    # so we don't discard the new turn's output.
    slot.drain()

    # Kick off LLM titling now, from the first user message, so the title lands
    # *during* the first turn instead of waiting for the whole response to
    # finish (chat_done). Runs on an isolated background kiro-cli session
    # concurrent with the turn. No-ops once titled / in-flight; the instant
    # 60-char provisional stays as the fallback if the LLM SKIPs or errors.
    if not slot._titled and not slot._title_in_flight:
        _tt = asyncio.create_task(_maybe_auto_title(state, slot))
        state._background_tasks.add(_tt)
        _tt.add_done_callback(state._background_tasks.discard)

    task = asyncio.create_task(
        asyncio.wait_for(_run_chat(state, slot, message), timeout=CHAT_TURN_TIMEOUT)
    )
    slot.task = task
    slot._recovery_retrigger_count = 0
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    state.push_slots_update()

    if ws_mode:
        return web.json_response({"ok": True, "slot": slot.key})

    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    await resp.prepare(request)

    try:
        while True:
            pending = slot.drain()
            for msg in pending:
                if msg["cls"] == "done":
                    await resp.write(b"data: [DONE]\n\n")
                    slot._has_reader = False
                    return resp
                chunk = _build_stream_chunk(msg)
                await resp.write(f"data: {chunk}\n\n".encode())
            try:
                await asyncio.wait_for(slot.event.wait(), timeout=30)
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        slot.drain()
        slot._has_reader = False
    return resp


async def api_chat_mode(request: web.Request) -> web.Response:
    """POST /api/chat/mode — set global tool approval mode.

    Modes:
      - ``normal``: reset to interactive (ask for each tool)
      - ``trust``: auto-approve tools for active slot
      - ``yolo``: auto-approve all tools everywhere

    Unlike the per-tool approve endpoint, this doesn't require a
    pending approval — it preemptively sets the mode for future tools.
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    mode = body.get("mode", "normal")
    slot_key = body.get("slot") or None

    if mode == "yolo":
        result = await asyncio.to_thread(safety_override().activate, "dashboard")
        if not result.active:
            return web.json_response(
                {"ok": False, "error": "safety override activation refused"},
                status=503,
            )
        try:
            sel().log_api_access(
                caller="dashboard:mode",
                operation="mode_change:yolo",
                outcome="enabled",
                resources=",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for YOLO mode activation", exc_info=True)
    elif mode == "trust_reads":
        safety_override().deactivate("dashboard")
        if slot_key and slot_key in state._slots:
            state._slots[slot_key]._trust = False
            state._slots[slot_key]._trust_reads = True
            state.sessions.set_approval_policy(f"dashboard:{slot_key}", "")
        else:
            for slot in state._slots.values():
                slot._trust = False
                slot._trust_reads = True
                state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")
        try:
            sel().log_api_access(
                caller="dashboard:mode",
                operation="mode_change:trust_reads",
                outcome="enabled",
                resources=slot_key or ",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for trust_reads mode activation", exc_info=True)
    elif mode == "trust":
        safety_override().deactivate("dashboard")
        mgr = getattr(state, "channel_manager", None)
        if slot_key is not None:
            if slot_key not in state._slots:
                return web.json_response({"ok": False, "error": "unknown slot"}, status=400)
            state._slots[slot_key]._trust = True
            state.sessions.set_approval_policy(f"dashboard:{slot_key}", "auto")
            linked_ch = getattr(state._slots[slot_key], "_slack_channel", None)
            if mgr and linked_ch and linked_ch in mgr._channels:
                mgr._channels[linked_ch].trusted = True
                mgr._channels[linked_ch]._save()
        else:
            for slot in state._slots.values():
                slot._trust = True
                state.sessions.set_approval_policy(f"dashboard:{slot.key}", "auto")
            if mgr:
                for ch in mgr._channels.values():
                    ch.trusted = True
                    ch._save()
        _trusted_chs = [cid for cid, ch in mgr._channels.items() if ch.trusted] if mgr else []
        try:
            _res = slot_key or ",".join(s.key for s in state._slots.values())
            if _trusted_chs:
                _res += "|channels:" + ",".join(_trusted_chs)
            sel().log_api_access(
                caller="dashboard:mode",
                operation="mode_change:trust",
                outcome="enabled",
                resources=_res,
            )
        except Exception:
            logger.warning("SEL audit failed for trust mode activation", exc_info=True)
    else:  # normal
        safety_override().deactivate("dashboard")
        mgr = getattr(state, "channel_manager", None)
        if slot_key is not None:
            if slot_key not in state._slots:
                return web.json_response({"ok": False, "error": "unknown slot"}, status=400)
            state._slots[slot_key]._trust = False
            state._slots[slot_key]._trust_reads = False
            state.sessions.set_approval_policy(f"dashboard:{slot_key}", "")
            linked_ch = getattr(state._slots[slot_key], "_slack_channel", None)
            if mgr and linked_ch and linked_ch in mgr._channels:
                mgr._channels[linked_ch].trusted = False
                mgr._channels[linked_ch]._save()
        else:
            for slot in state._slots.values():
                slot._trust = False
                slot._trust_reads = False
                state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")
            if mgr:
                for ch in mgr._channels.values():
                    ch.trusted = False
                    ch._save()
        try:
            sel().log_api_access(
                caller="dashboard:mode",
                operation="mode_change:normal",
                outcome="disabled",
                resources=slot_key or ",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for normal mode activation", exc_info=True)

    # If any slot has a pending approval and mode is trust/yolo, auto-approve it
    if mode in ("trust", "yolo"):
        for slot in state._slots.values():
            for aid, fut in list(slot._approval_futures.items()):
                if not fut.done():
                    fut.set_result("approved")
                    # Persist resolved state into the permission message
                    _mark_permission_resolved(slot.messages, aid, mode)
                    state.broadcast_ws("approval_resolved", {"id": aid, "approved": True})
                    try:
                        sel().log_api_access(
                            caller=f"dashboard:{slot.key}",
                            operation=f"tool_approval:bulk_{mode}",
                            outcome="approved",
                            resources=aid,
                        )
                    except Exception:
                        logger.warning("SEL audit failed for bulk approval %s", aid, exc_info=True)
        # Also auto-approve all pending background approvals (cron/subagent/taskrunner)
        for aid in list(state._approval_futures):
            fut = state._approval_futures[aid]
            if not fut.done():
                state.resolve_approval(aid, True)
                try:
                    sel().log_api_access(
                        caller="dashboard:background",
                        operation=f"tool_approval:bulk_{mode}",
                        outcome="approved",
                        resources=aid,
                    )
                except Exception:
                    logger.warning("SEL audit failed for bulk approval %s", aid, exc_info=True)
        # Auto-approve pending channel approvals
        mgr = getattr(state, "channel_manager", None)
        if mgr:
            for ch in mgr._channels.values():
                for agent in ch.members.values():
                    fut = agent._approval_future
                    if fut and not fut.done():
                        fut.set_result("approved")
                        try:
                            sel().log_api_access(
                                caller=f"channel:{ch.id}:{agent.agent_name}",
                                operation=f"tool_approval:bulk_{mode}",
                                outcome="approved",
                                resources=getattr(fut, "_approval_id", "unknown"),
                            )
                        except Exception:
                            logger.warning(
                                "SEL audit failed for channel bulk approval", exc_info=True
                            )

    # Propagate trust/yolo to session approval policies so subagents inherit.
    for slot in state._slots.values():
        policy = "auto" if slot._trust or safety_override().is_active() else ""
        state.sessions.set_approval_policy(f"dashboard:{slot.key}", policy)

    state.push_slots_update()
    return web.json_response({"ok": True, "mode": mode})


from kiro_crew.dashboard.chat_handlers_config import (  # noqa: E402,F401
    _MAX_CONTEXT_PER_SOURCE,
    _MAX_RECENT_PROJECTS,
    MAX_COLOR_INDEX,
    _recent_projects_path,
    _save_recent_project,
    api_chat_slot_agent,
    api_chat_slot_color,
    api_chat_slot_context,
    api_chat_slot_model,
    api_chat_slot_project,
    api_chat_slot_reasoning_effort,
    api_chat_slot_resume,
    api_chat_slot_workspace,
    api_chat_slots_model,
    api_recent_projects,
)
from kiro_crew.dashboard.chat_handlers_control import (  # noqa: E402,F401
    _get_pattern_from_pending,
    _reject_pending_approvals,
    _resolve_stop_event,
    api_chat_slot_approve,
    api_chat_slot_interrupt,
    api_chat_slot_queue_cancel,
    api_chat_slot_queue_edit,
    api_chat_slot_queue_reorder,
    api_chat_slot_stop,
)

# Re-export moved endpoint handlers so route registration (server.py /
# chat.py facade) and existing imports keep resolving from this module.
from kiro_crew.dashboard.chat_handlers_lifecycle import (  # noqa: E402,F401
    _sweep_stale_permissions,
    api_chat_slot_create,
    api_chat_slot_delete,
    api_chat_slot_detail,
    api_chat_slots,
    api_chat_slots_cleanup,
)
