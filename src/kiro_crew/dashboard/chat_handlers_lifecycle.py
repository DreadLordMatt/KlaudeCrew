"""Dashboard chat — slot lifecycle endpoints (list/detail/create/delete/cleanup).

Extracted from chat_handlers.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

from aiohttp import web

from kiro_crew.config.loader import (
    KiroCrewConfig,
    _workspace_name_for_dir,
    default_project_dir,
    resolve_agent_bindings,
)
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import (
    _history_key_for,
    _prepare_messages,
    _redact_for_display,
    _sync_dashboard_slots,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.validation import ARTIFACT_SLUG_RE

logger = logging.getLogger(__name__)


def _sweep_stale_permissions(slot: "_ChatSlot") -> None:
    """Mark unresolved permissions from prior turns as stale.

    Called once at turn-start, before the new user message is appended.
    Safe: if we're starting a new turn, any prior unresolved permission
    is definitionally orphaned — the LLM that requested it is gone.

    Note: if the same slot is open in multiple tabs, an in-flight pending
    approval in tab A may be marked stale by a turn-start in tab B. The
    failure mode is benign (user re-clicks approve); single-tab use is
    unaffected.
    """
    for msg in slot.messages:
        if msg.get("role") != "permission":
            continue
        try:
            cls = json.loads(msg.get("cls", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls, dict):
            # Valid JSON but not an object (e.g. [], "x", 123, null) — cannot
            # carry a "resolved" key; skip rather than raise TypeError and
            # abort the whole sweep. Mirrors parse_cls_meta() in state.py.
            continue
        if "resolved" in cls:
            continue
        cls["resolved"] = "stale"
        msg["cls"] = json.dumps(cls)
        slot._dirty = True
        sel().log_api_access(
            caller="gateway",
            operation="permission.resolve_stale",
            outcome="allowed",
            source="turn_start_sweep",
            resources=cls.get("request_id", ""),
        )


async def api_chat_slots(request: web.Request) -> web.Response:
    """GET /api/chat/slots — list all chat slots."""
    state: DashboardState = request.app["state"]
    # Credential-backed check status is owner-only. Non-owner and app-token
    # callers receive source links but neither cached status nor provider work.
    from kiro_crew.dashboard.handlers.source_providers import (
        is_owner_dashboard_request,
        schedule_check_refresh,
    )

    include_check_status = is_owner_dashboard_request(request)
    payloads = state.serialize_slots(include_check_status=include_check_status)
    if include_check_status:
        urls = [link["url"] for payload in payloads for link in payload.get("source_links", [])]
        if urls:
            schedule_check_refresh(urls, state.push_slots_update)
    return web.json_response(payloads)


async def api_chat_slot_detail(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot} — message history for a slot.

    Query params:
      - ``limit``: max messages to return (optional; if omitted, returns ALL messages from disk)
      - ``before``: return messages before this index (legacy pagination, still supported)

    By default (no limit), reads the full chained history from disk across
    gateway restarts. Pagination params are retained for backwards compatibility.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    limit_raw = request.query.get("limit")
    before = request.query.get("before")

    # No limit → load ALL messages (chained across gateway restarts).
    # In-memory slot.messages is authoritative for the current session.
    # _disk_older_count gates whether to read disk AND provides the stable
    # slice boundary (set at restore/resume, never drifts with new messages).
    if limit_raw is None and before is None:
        mem_msgs = list(slot.messages)
        if slot._disk_older_count > 0 and state.conversation_log:
            history_key = _history_key_for(slot.key)
            try:
                disk_msgs = state.conversation_log.read_messages_chained(history_key)
            except Exception:
                logger.warning("read_messages_chained failed for %s", history_key, exc_info=True)
                disk_msgs = []
            older = disk_msgs[: slot._disk_older_count] if disk_msgs else []
            messages = older + mem_msgs
        else:
            messages = mem_msgs
        total = len(messages)
        has_more = False
    else:
        # Legacy pagination path (retained for programmatic callers).
        # Always reads from chained disk history; no in-memory offset math.
        limit = min(int(limit_raw or "200"), 500)
        history_key = _history_key_for(slot.key)
        try:
            all_msgs = (
                state.conversation_log.read_messages_chained(history_key)
                if state.conversation_log
                else []
            )
        except Exception:
            logger.warning("read_messages_chained failed for %s", history_key, exc_info=True)
            all_msgs = []
        # Append any un-flushed in-memory tail messages beyond what's on disk.
        # Use _disk_older_count to isolate current-session disk count, since
        # chained disk includes older sessions that inflate disk_len.
        mem_len = len(slot.messages)
        disk_len = len(all_msgs)
        current_session_disk = max(0, disk_len - slot._disk_older_count)
        unflushed = mem_len - current_session_disk
        if unflushed > 0:
            all_msgs = list(all_msgs) + list(slot.messages[-unflushed:])
        total = len(all_msgs)
        if before is not None:
            end = max(0, min(int(before), total))
        else:
            end = total
        start = max(0, end - limit)
        messages = all_msgs[start:end]
        has_more = start > 0

    prepared = _prepare_messages(messages, slot.running)

    return web.json_response(
        {
            "key": slot.key,
            "title": slot.display_title,
            "running": slot.running,
            "stopping": slot._stopping,
            "messages": prepared,
            "queue": [
                {"id": q["id"], "content": _redact_for_display(q["content"])} for q in slot._queue
            ],
            "total": total,
            "has_more": has_more,
        }
    )


async def api_chat_slot_create(request: web.Request) -> web.Response:
    """POST /api/chat/slots — create a new chat slot."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name")
    agent = body.get("agent", "")
    model = body.get("model", "")

    # Resolve workspace from agent bindings
    workspace = "default"
    cfg = None
    try:
        cfg = KiroCrewConfig.load()
        if agent and agent in cfg.agents:
            bindings = resolve_agent_bindings(cfg, agent)
            workspace = _workspace_name_for_dir(cfg, bindings.workspace_dir)
    except Exception:
        logger.warning("Failed to resolve bindings for slot create", exc_info=True)

    try:
        memory_mode = body.get("memory_mode", "persistent")
        if memory_mode not in ("persistent", "incognito", "temporary"):
            return web.json_response({"error": "invalid memory_mode"}, status=400)
        slot = state.get_or_create_slot(
            name,
            agent=agent,
            workspace=workspace,
            model=model,
            mode=body.get("mode", ""),
            memory_mode=memory_mode,
            ephemeral=body.get("ephemeral"),
            app=request.get("app", ""),
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    if slot.is_restricted:
        logger.info("Slot %s created with memory_mode=%s", slot.key, slot.memory_mode)
    # Pin title if explicitly provided (prevents auto-title from overwriting)
    title = (body.get("title") or "").strip()[:200] if isinstance(body, dict) else ""
    if title:
        title, _ = redact_exfiltration_urls(title)
        title, _ = redact_credentials(title)
        slot.title = title
        slot._titled = True
    # Bind to an artifact if provided (companion chat, Mesh-2772). Validate
    # against the artifact slug grammar so an injection-shaped value can never
    # land on the slot; anything invalid is silently dropped. Uniqueness (≤1
    # active bound session per slug) is a frontend-flow convention, not
    # enforced here.
    artifact_slug = body.get("artifact") if isinstance(body, dict) else None
    if isinstance(artifact_slug, str) and ARTIFACT_SLUG_RE.match(artifact_slug):
        slot._artifact = artifact_slug
    # Default project to workspace directory so file search works out of the box
    if not slot.project:
        cfg_proj = cfg.dashboard.default_project if cfg else ""
        if isinstance(cfg_proj, str) and cfg_proj:
            resolved = os.path.realpath(os.path.expanduser(cfg_proj))
            if os.path.isdir(resolved) and not is_sensitive_path(resolved):
                cfg_proj = resolved
            else:
                cfg_proj = ""
        else:
            cfg_proj = ""
        slot.project = cfg_proj or default_project_dir(workspace)
    _sync_dashboard_slots(state)
    return web.json_response(slot.to_dict())


async def api_chat_slot_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/slots/{slot} — stop and remove a UI slot.

    Kills the per-tab kiro-cli session and saves history.  The session
    will be recreated from the warm pool if the tab is resumed later.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    # App ownership check (App Kit §5.2): app can only delete slots it created.
    # Unscoped slots (empty _app) cannot be deleted by app tokens.
    # Dashboard users (empty request_app) can delete anything.
    request_app = request.get("app", "")
    if request_app and slot._app != request_app:
        sel().log_api_access(
            caller=request_app,
            operation="slot_delete",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app does not own this slot",
        )
        return web.json_response({"error": "not found"}, status=404)
    if request_app and not slot._app:
        sel().log_api_access(
            caller=request_app,
            operation="slot_delete",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app cannot delete unscoped slots",
        )
        # 404 (not 403): a foreign/unscoped slot is indistinguishable from a
        # missing one — anti-enumeration (CWE-204); true reason logged via SEL.
        return web.json_response({"error": "not found"}, status=404)

    # Remove from dict before async operations
    state._slots.pop(name, None)
    if slot.running and slot.task is not None:
        slot.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(slot.task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    try:
        _save_slot_to_history(state, slot, closed=True)
    except Exception:
        # Save failed — restore slot so data isn't lost
        logger.error("Failed to save slot %s to history, restoring", name, exc_info=True)
        state._slots[name] = slot
        _sync_dashboard_slots(state)
        state.push_slots_update()
        return web.json_response({"error": "failed to save history"}, status=500)
    else:
        state._restricted_keys.discard(f"dashboard:{name}")
    # Kill the per-tab session to free resources
    await state.sessions.remove(_history_key_for(name))
    _sync_dashboard_slots(state)
    state.push_slots_update()
    state.push_refresh("history")
    return web.json_response({"ok": True})


async def api_chat_slots_cleanup(request: web.Request) -> web.Response:
    """POST /api/chat/slots/cleanup — bulk-archive inactive sessions to history.

    Body: ``{"max_inactive_days": 3, "active_slot": "chat-1-123"}``
    Skips the active slot and pinned sessions.
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    max_days = 3
    try:
        max_days = max(1, int(body.get("max_inactive_days", 3)))
    except (ValueError, TypeError):
        pass
    active_slot = body.get("active_slot", "")
    dry_run = body.get("dry_run", False)
    request_app = request.get("app", "")
    cutoff = time.time() - max_days * 86400
    stale_keys: list[str] = []
    active_is_stale = False
    for name in list(state._slots):
        slot = state._slots.get(name)
        if slot is None or slot.pinned:
            continue
        # App Kit ownership isolation: app callers can only archive
        # their own slots. Dashboard users (empty request_app) pass
        # through and can archive anything.
        if request_app:
            if slot._app != request_app:
                continue
        last_activity = 0.0
        if slot.messages:
            for m in reversed(slot.messages):
                ts = m.get("ts", "")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    last_activity = dt.timestamp()
                except (ValueError, TypeError):
                    continue
                break
        if not last_activity:
            try:
                dt = datetime.fromisoformat(slot.created_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                last_activity = dt.timestamp()
            except Exception:
                last_activity = 0.0
        if not last_activity:
            continue  # unknown activity — don't archive
        if last_activity >= cutoff:
            continue
        if name == active_slot:
            active_is_stale = True
            continue
        stale_keys.append(name)
    # Dry-run: return the exact list without archiving
    if dry_run:
        sel().log_api_access(
            caller="dashboard",
            operation="chat.cleanup_dry_run",
            outcome="allowed",
            source="dashboard",
            resources=f"count={len(stale_keys)} threshold={max_days}d",
        )
        return web.json_response(
            {
                "ok": True,
                "dry_run": True,
                "keys": stale_keys,
                "count": len(stale_keys),
                "active_is_stale": active_is_stale,
            }
        )
    archived: list[str] = []
    failed: list[str] = []
    _tasks_to_cancel: list[asyncio.Task] = []
    for name in stale_keys:
        removed = state._slots.pop(name, None)
        if not removed:
            continue
        try:
            _save_slot_to_history(state, removed, closed=True)
        except Exception:
            logger.error("Cleanup: failed to archive slot %s", name, exc_info=True)
            state._slots[name] = removed
            failed.append(name)
            continue
        else:
            state._restricted_keys.discard(f"dashboard:{name}")
        # Session cleanup is best-effort — history is already written
        try:
            await state.sessions.remove(_history_key_for(name))
        except Exception:
            logger.warning("Cleanup: session remove failed for %s", name, exc_info=True)
        archived.append(name)
        # Collect running tasks for concurrent cancellation after the loop
        if removed.running and removed.task is not None:
            removed.task.cancel()
            _tasks_to_cancel.append(removed.task)
    # Await all cancelled tasks concurrently with a single bounded timeout
    if _tasks_to_cancel:
        await asyncio.wait(_tasks_to_cancel, timeout=5.0)
    if archived:
        _sync_dashboard_slots(state)
        state.push_slots_update()
        state.push_refresh("history")
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slots_cleanup",
        outcome="ok" if not failed else ("partial" if archived else "error"),
        source="dashboard",
        resources=f"archived={len(archived)} failed={len(failed)} threshold={max_days}d keys={','.join(archived[:10])}",
    )
    return web.json_response(
        {"ok": True, "archived": len(archived), "keys": archived, "failed": failed}
    )
