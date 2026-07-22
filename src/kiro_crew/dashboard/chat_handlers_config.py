"""Dashboard chat — slot configuration endpoints (agent/model/workspace/project/etc).

Extracted from chat_handlers.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from aiohttp import web

from kiro_crew.aim_agents import auto_register_project as _auto_register_project
from kiro_crew.aim_agents import find_agent_file as _find_agent_file
from kiro_crew.config.loader import (
    KiroCrewConfig,
    _workspace_name_for_dir,
    config_dir,
    default_project_dir,
    resolve_agent_bindings,
)
from kiro_crew.dashboard.chat_folders import _unhide_folder
from kiro_crew.dashboard.chat_persistence import (
    _attach_variants,
    _redact_meta_for_role,
    get_reasoning_effort_values,
)
from kiro_crew.dashboard.chat_utils import (
    _history_key_for,
    _normalize_model,
    _prepare_messages,
    _redact_for_display,
    _sync_dashboard_slots,
)
from kiro_crew.dashboard.state import _MAX_PENDING_CONTEXT, DashboardState
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.validation import _AGENT_NAME_RE

logger = logging.getLogger(__name__)


async def api_chat_slot_agent(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/agent — set agent for a chat slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    agent_name = body.get("agent", "")
    explicit_path = body.get("project_path", "")
    if explicit_path and not agent_name:
        sel().log_api_access(
            caller="dashboard",
            operation="chat.slot_agent",
            outcome="denied",
            source="api_chat_slot_agent",
            resources=explicit_path,
            error="project_path provided without agent name",
        )
        return web.json_response(
            {
                "error": "project_path requires a non-empty agent; use POST /api/chat/slots/{slot}/project to set the project directly"
            },
            status=400,
        )
    if agent_name and not _AGENT_NAME_RE.match(agent_name):
        return web.json_response({"error": "invalid agent name"}, status=400)

    # Snapshot slot state so we can restore on error paths (e.g. 409)
    prev_agent = slot.agent
    prev_workspace = slot.workspace
    prev_project = slot.project

    slot.agent = agent_name

    # Resolve workspace from agent bindings
    workspace = "default"
    try:
        cfg = KiroCrewConfig.load()
        # Look up by config key or by kiro_agent name
        matched = agent_name if agent_name in cfg.agents else None
        if agent_name and not matched:
            for k, v in cfg.agents.items():
                if v.kiro_agent == agent_name:
                    matched = k
                    break
        if matched:
            bindings = resolve_agent_bindings(cfg, matched)
            ws_name = _workspace_name_for_dir(cfg, bindings.workspace_dir)
            slot.workspace = ws_name
            workspace = ws_name
            slot.project = default_project_dir(workspace)
    except Exception:
        logger.warning("Failed to resolve agent bindings for %r", agent_name, exc_info=True)
        slot.agent = prev_agent
        slot.workspace = prev_workspace

    # Auto-set project path for project-scoped agents (contextual launch).
    # project_path in the request body is the only way to associate an agent with a project.
    # If project_path is absent the caller wants the global agent — no slot.project change.
    try:
        if explicit_path:
            # Caller explicitly named a project — validate it before committing slot state.
            resolved_path = str(Path(explicit_path).expanduser().resolve())
            if not os.path.isdir(resolved_path):
                slot.agent = prev_agent
                slot.workspace = prev_workspace
                slot.project = prev_project
                sel().log_api_access(
                    caller="dashboard",
                    operation="chat.slot_agent",
                    outcome="denied",
                    source="api_chat_slot_agent",
                    resources=resolved_path,
                    error="project_path is not a directory",
                )
                return web.json_response({"error": "project_path is not a directory"}, status=400)
            if is_sensitive_path(resolved_path):
                slot.agent = prev_agent
                slot.workspace = prev_workspace
                slot.project = prev_project
                sel().log_api_access(
                    caller="dashboard",
                    operation="chat.slot_agent",
                    outcome="denied",
                    source="api_chat_slot_agent",
                    resources=explicit_path,
                    error="sensitive project_path rejected",
                )
                return web.json_response(
                    {"error": "project_path rejected as sensitive"}, status=403
                )
            # Verify the named agent exists in this project before committing.
            # Uses name-field matching (not filename stem) to mirror kiro-cli's --agent resolution.
            agent_file = _find_agent_file(Path(resolved_path) / ".kiro" / "agents", agent_name)
            if agent_file is None:
                slot.agent = prev_agent
                slot.workspace = prev_workspace
                slot.project = prev_project
                sel().log_api_access(
                    caller="dashboard",
                    operation="chat.slot_agent",
                    outcome="denied",
                    source="api_chat_slot_agent",
                    resources=resolved_path,
                    error=f"agent {agent_name!r} not found in project",
                )
                return web.json_response(
                    {"error": f"agent {agent_name!r} not found in project"}, status=404
                )
            slot.project = resolved_path
            logger.info("Explicit project for %r: %s", agent_name, resolved_path)
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_agent",
                outcome="ok",
                source="api_chat_slot_agent",
                resources=resolved_path,
            )
    except Exception:
        logger.warning("Failed to validate project path for %r", agent_name, exc_info=True)
        slot.agent = prev_agent
        slot.workspace = prev_workspace
        slot.project = prev_project
        return web.json_response({"error": "failed to validate project_path"}, status=500)

    # Reset session so next message uses the new agent
    logger.info("Slot %s agent switched to %r, resetting session", name, agent_name or "kirocrew")
    await state.sessions.reset(_history_key_for(name))
    # Persist the new agent so the session resumes under the correct agent
    # after a gateway restart.  Written after reset succeeds so we never
    # advertise an agent we couldn't actually switch to.
    if state.conversation_log:
        try:
            state.conversation_log.update_metadata(_history_key_for(name), {"agent": agent_name})
        except Exception:
            logger.warning("Failed to persist agent for slot %s", name, exc_info=True)
    state.push_slots_update()
    return web.json_response({"ok": True, "agent": agent_name, "workspace": workspace})


async def api_chat_slot_model(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/model — set model for a chat slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    model_name = _normalize_model(body.get("model", ""))
    if slot.model == model_name:
        return web.json_response({"ok": True, "model": model_name})
    slot.model = model_name
    logger.info("Slot %s model switched to %r, resetting session", name, model_name or "auto")
    await state.sessions.reset(_history_key_for(name))
    state.push_slots_update()
    return web.json_response({"ok": True, "model": model_name})


async def api_chat_slots_model(request: web.Request) -> web.Response:
    """POST /api/chat/slots/model — set the model for ALL chat slots (bulk).

    Body: {"model": "<name>" | "", "skip_running": bool (default True)}.
    "" selects the provider/auto default. Applies the model to every slot
    whose model differs, resetting each affected slot's session — a model
    switch always resets, same as ``api_chat_slot_model``. Slots mid-turn are
    skipped when ``skip_running`` is true to avoid the model-switch-mid-stream
    duplicate-content bug (Mesh-1080); pass ``skip_running: false`` to force
    every slot. Returns the slot keys that were switched / skipped / unchanged /
    failed; a per-slot reset failure is isolated (that slot is reported in
    ``failed`` and keeps its old model) rather than aborting the whole switch.
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    model_name = _normalize_model(body.get("model", ""))
    skip_running = body.get("skip_running", True)
    if not isinstance(skip_running, bool):
        return web.json_response({"error": "skip_running must be a boolean"}, status=400)
    # Deny-by-default (security-controls): the auth middleware always sets
    # request["app"] on every authenticated path (empty string for dashboard
    # users, app name for app tokens). An ABSENT key means the middleware did
    # not run -- refuse rather than fall through to all-slot access.
    if "app" not in request:
        return web.json_response({"error": "unauthorized"}, status=403)
    request_app = request["app"]
    # Dashboard users are identified by the middleware's EXPLICIT "" assignment.
    # Compare with == "" (not truthiness) so an unexpected falsy value (None, 0)
    # fails closed into the per-slot ownership check instead of bypassing it.
    is_dashboard_user = request_app == ""

    switched: list[str] = []
    skipped_running: list[str] = []
    unchanged: list[str] = []
    failed: list[str] = []
    # Snapshot the slot keys up front: sessions.reset awaits, so iterating the
    # live dict directly would risk a concurrent-modification surprise.
    for name, slot in list(state._slots.items()):
        # App Kit ownership isolation: app callers can only switch their own
        # slots (mirrors api_chat_slots_cleanup). Only an explicit dashboard
        # user bypasses the ownership check.
        if not is_dashboard_user and slot._app != request_app:
            continue
        if slot.model == model_name:
            unchanged.append(name)
            continue
        if skip_running and slot.running:
            skipped_running.append(name)
            continue
        # Reset before flipping the model and isolate per-slot failures: if the
        # reset raises, leave slot.model untouched so the slot is never left on
        # the new model with stale history (the Mesh-1080 inconsistency), and a
        # single failure doesn't abort the whole bulk switch.
        try:
            await state.sessions.reset(_history_key_for(name))
        except Exception:
            logger.error("Bulk model switch: session reset failed for %s", name, exc_info=True)
            failed.append(name)
            continue
        slot.model = model_name
        switched.append(name)

    if switched:
        logger.info(
            "Bulk model switch to %r: %d switched, %d skipped-running, %d unchanged, %d failed",
            model_name or "auto",
            len(switched),
            len(skipped_running),
            len(unchanged),
            len(failed),
        )
        # Guard the push on real progress so partial switches still broadcast
        # even when a later slot's reset failed.
        state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "model": model_name,
            "switched": switched,
            "skipped_running": skipped_running,
            "unchanged": unchanged,
            "failed": failed,
        }
    )


async def api_chat_slot_reasoning_effort(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/reasoning-effort — set reasoning effort.

    Body: {"reasoning_effort": "" | "low" | "medium" | "high" | "xhigh" | "max"}.
    "" = provider default (e.g. CC falls back to its opus heuristic, kiro to
    the model's default).

    Works for both ACP backends (claude-agent-acp and kiro-cli) via the
    provider's ``change_effort`` — which pushes the level live to the running
    session (claude: session/set_config_option, kiro: /effort + cli.json
    overlay). Effort is Opus/Sonnet-only; on a non-capable model this is a
    persisted no-op (no live apply, no session reset).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    effort = body.get("reasoning_effort", "")
    valid_efforts = get_reasoning_effort_values()
    if not isinstance(effort, str) or effort not in valid_efforts:
        return web.json_response(
            {
                "error": f"reasoning_effort must be one of: {', '.join(sorted(valid_efforts - {''}))}"
            },
            status=400,
        )
    if slot.reasoning_effort == effort:
        return web.json_response({"ok": True, "reasoning_effort": effort})
    slot.reasoning_effort = effort
    logger.info("Slot %s reasoning_effort switched to %r", name, effort or "default")

    session_key = _history_key_for(name)
    provider = state.sessions.get_provider(session_key)
    _updated_live = False
    if isinstance(provider, AcpProvider) and provider.supports_effort():
        # Guard against racing the in-flight prompt read loop: a live
        # change_effort issues session/set_config_option and its response wait
        # would call stdout.readline() concurrently with the streaming
        # _prompt_loop → dropped/misrouted frame or a stuck turn. The override
        # is already persisted on the slot, so defer the live push to the next
        # turn instead of pushing now or resetting (effort is a cheap knob).
        if provider.has_active_turn():
            logger.info("Slot %s deferred live effort push: turn active", name)
            state.push_slots_update()
            return web.json_response({"ok": True, "reasoning_effort": effort, "deferred": True})
        # change_effort handles both backends and persists the per-model
        # override + overlay. "" clears the override → fall back to model
        # default (kiro: /effort with model default; claude: leave as-is).
        try:
            if effort:
                _updated_live = await provider.change_effort(effort)
            else:
                _updated_live = await provider.clear_effort()
        except Exception as exc:
            logger.warning(
                "change_effort(%s) failed for slot %s: %s: %s — falling back to reset",
                effort,
                name,
                type(exc).__name__,
                exc,
            )
    elif isinstance(provider, AcpProvider):
        # Model does not support effort — persist the slot value for when the
        # user switches to a capable model, but do not touch the live session.
        _updated_live = True
        logger.info("Slot %s effort persisted (model not effort-capable)", name)

    if not _updated_live:
        # No live session (or live update failed): reset so the next cold
        # start picks up the new effort via the provider factory/overlay.
        await state.sessions.reset(session_key)
    state.push_slots_update()
    return web.json_response({"ok": True, "reasoning_effort": effort})


async def api_chat_slot_workspace(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/workspace — set workspace for a chat slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    ws_name = body.get("workspace", "default")
    # Block workspace change after conversation has started
    if slot.total_messages > 0:
        return web.json_response(
            {
                "error": "Cannot change workspace after messages have been sent. Open a new session instead."
            },
            status=409,
        )
    slot.workspace = ws_name
    slot.project = default_project_dir(ws_name)
    logger.info("Slot %s workspace switched to %r, resetting session", name, ws_name)
    await state.sessions.reset(_history_key_for(name))
    state.push_slots_update()
    return web.json_response({"ok": True, "workspace": ws_name})


async def api_chat_slot_project(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/project — set project directory for file search scoping."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    project = body.get("project", "")
    if not isinstance(project, str):
        return web.json_response({"error": "project must be a string"}, status=400)
    project = project.strip()
    if project:
        project = os.path.realpath(os.path.expanduser(project))
        if not os.path.isdir(project):
            return web.json_response({"error": "Not a directory"}, status=400)
        if is_sensitive_path(project):
            sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="chat_slot_project",
                outcome="denied",
                resources=f"slot={name} project={project}",
                error="sensitive path",
            )
            return web.json_response({"error": "Access denied"}, status=403)
    old_project = slot.project
    slot.project = project
    logger.info("Slot %s project set to %r", name, project)
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="chat_slot_project",
        outcome="allowed",
        resources=f"slot={name} project={project}",
    )
    # Track recent projects
    if project:
        try:
            await asyncio.to_thread(_save_recent_project, project)
        except Exception:
            logger.warning("Failed to save recent project", exc_info=True)
        # Auto-register agents for this project dir (direct read, no walk).
        # Runs synchronously so the registry is populated before the response returns —
        # the user can immediately pick a project agent without a timing race.
        try:
            await asyncio.to_thread(_auto_register_project, project)
        except Exception:
            logger.warning("auto_register_project failed for %s", project, exc_info=True)
    # Reset the session so the next message cold-starts with the new CWD and
    # picks up project-level .kiro/steering/**/*.md (mirrors api_chat_slot_agent).
    # Only on an actual change — avoids a needless cold start on a no-op set.
    #
    # Deferred via a flag because this endpoint is reachable over loopback HTTP
    # from inside the kiro-cli process group (the set_project MCP tool); an
    # inline reset would killpg() the caller. Consumed in chat_runner.
    if project != old_project:
        slot._pending_reset_history_key = _history_key_for(name)
    state.push_slots_update()
    return web.json_response({"ok": True, "project": project})


_MAX_RECENT_PROJECTS = 100


def _recent_projects_path() -> Path:
    return config_dir() / "recent_projects.json"


def _save_recent_project(path: str) -> None:
    """Prepend path to recent projects list (deduped, capped)."""

    fp = _recent_projects_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
    except (json.JSONDecodeError, OSError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    existing = [p for p in existing if p != path]
    existing.insert(0, path)
    existing = existing[:_MAX_RECENT_PROJECTS]
    fd, tmp = tempfile.mkstemp(dir=fp.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            tmp_fh.write(json.dumps(existing))
        os.replace(tmp, fp)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def api_recent_projects(request: web.Request) -> web.Response:
    """GET /api/recent-projects — list recently used project directories."""

    def _read_recent_projects() -> list[str]:
        fp = _recent_projects_path()
        try:
            dirs = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
        except Exception:
            dirs = []
        if not isinstance(dirs, list):
            dirs = []
        return [
            d for d in dirs if isinstance(d, str) and os.path.isdir(d) and not is_sensitive_path(d)
        ]

    dirs = await asyncio.to_thread(_read_recent_projects)
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="recent_projects",
        outcome="allowed",
        resources=f"count={len(dirs)}",
    )
    return web.json_response({"dirs": dirs})


async def api_chat_slot_resume(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/resume — load a history session into a slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    if name.startswith("dashboard_"):
        name = name.removeprefix("dashboard_")
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    history_key = body.get("key", name)

    # If slot already exists (active session), just return it — no duplicate.
    # Check both by slot name AND by canonical session key to prevent two
    # slots sharing the same kiro-cli process (Mesh-98).
    canonical = _history_key_for(history_key)
    existing = state._slots.get(name)
    if not existing:
        for slot in state._slots.values():
            if _history_key_for(slot.key) == canonical:
                existing = slot
                break
    if existing:
        # App ownership check (App Kit §5.2)
        request_app = request.get("app", "")
        if request_app:
            if not existing._app:
                sel().log_api_access(
                    caller=request_app,
                    operation="slot_resume",
                    outcome="denied",
                    source="app_isolation",
                    resources=f"slot={existing.key}",
                    error="app cannot access unscoped slots",
                )
                return web.json_response({"error": "not found"}, status=404)
            elif request_app != existing._app:
                sel().log_api_access(
                    caller=request_app,
                    operation="slot_resume",
                    outcome="denied",
                    source="app_isolation",
                    resources=f"slot={existing.key}",
                    error="app does not own this slot",
                )
                return web.json_response({"error": "not found"}, status=404)
        total = len(existing.messages)
        recent = existing.messages[-200:] if total > 200 else existing.messages
        prepared = _prepare_messages(recent, existing.running)
        return web.json_response(
            {
                "ok": True,
                "key": existing.key,
                "messages": prepared,
                "queue": [
                    {"id": q["id"], "content": _redact_for_display(q["content"])}
                    for q in existing._queue
                ],
                "total": total,
                "has_more": total > 200,
                "memory_mode": existing.memory_mode,
                # Return the slot's mode (and its `surface` alias) so the
                # frontend can render the recovered slot in the correct mode
                # (e.g. autopilot/"orchestrator") immediately, without waiting
                # for the racy SSE slots push to arrive (resumed autopilot
                # sessions came back as plain chat until SSE reconciled).
                "mode": existing.mode,
                "surface": existing.mode,
            }
        )

    slot = state.get_or_create_slot(name, app=request.get("app", ""))
    title = body.get("title", "")
    if title:
        slot.title = title
        slot._titled = True
    else:
        sessions = state.conversation_log.list_sessions()
        for s in sessions:
            if s.get("key") == history_key:
                slot.title = s.get("title", history_key)
                slot._titled = True
                break
    # Restore original created_at from history metadata
    meta = state.conversation_log.get_metadata(history_key)
    if meta.get("created_at"):
        slot.created_at = meta["created_at"]
    if meta.get("agent"):
        slot.agent = meta["agent"]
    if meta.get("workspace"):
        slot.workspace = meta["workspace"]
    if meta.get("project"):
        slot.project = meta["project"]
    if meta.get("mode"):
        slot.mode = meta["mode"]
    if meta.get("folder_id"):
        slot.folder_id = meta["folder_id"]
        # Re-engaging a hidden empty folder (Model B) un-hides it so it stays
        # visible until the user hides it again.
        _unhide_folder(state, meta["folder_id"])
    if meta.get("pinned"):
        slot.pinned = True
    if meta.get("color_index") is not None:
        slot.color_index = meta["color_index"]
    if meta.get("color_theme"):
        slot.color_theme = meta["color_theme"]
    mm = meta.get("memory_mode", "persistent")
    slot.memory_mode = mm
    if mm != "persistent":
        state._restricted_keys.add(f"dashboard:{name}")
    else:
        state._restricted_keys.discard(f"dashboard:{name}")
    if meta.get("forked_from") is not None:
        slot.forked_from = meta["forked_from"]
    # Clear closed flag so session restores on next gateway restart
    if meta.get("closed"):
        try:
            path = state.conversation_log._path(history_key)
            if path.exists():
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                if lines:
                    first_line_data = json.loads(lines[0])
                    first_line_data.pop("closed", None)
                    lines[0] = json.dumps(first_line_data) + "\n"
                    atomic_tmp = path.with_name(path.name + ".tmp")
                    try:
                        atomic_tmp.write_text("".join(lines), encoding="utf-8")
                        atomic_tmp.replace(path)
                        state.conversation_log._meta_cache.pop(history_key, None)
                    except Exception:
                        atomic_tmp.unlink(missing_ok=True)
                        raise
        except Exception:
            logger.warning("Failed to clear closed flag for %s", history_key, exc_info=True)
    all_messages = state.conversation_log.read_messages_chained(history_key)
    disk_total = len(all_messages)
    max_resume = 500
    messages = all_messages[-max_resume:] if disk_total > max_resume else all_messages
    # Stable count of messages older than what we loaded into memory
    slot._disk_older_count = max(0, disk_total - len(messages))
    for m in messages:
        role = m.get("role", "assistant")
        cls = "msg msg-u" if role == "user" else "msg msg-a"
        content = m.get("content", "")
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            meta=(
                _redact_meta_for_role(role, m["meta"]) if isinstance(m.get("meta"), dict) else None
            ),
        )
        _attach_variants(slot, m)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    # Loaded window is the on-disk window region; older lines (in
    # _disk_older_count above) are the frozen prefix saves never rewrite,
    # so older on-disk turns are preserved.
    slot._disk_window_len = len(slot.messages)
    total = disk_total
    recent = slot.messages[-200:] if len(slot.messages) > 200 else slot.messages
    _sync_dashboard_slots(state)
    state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "key": slot.key,
            "messages": _prepare_messages(recent, slot.running),
            "queue": [
                {"id": q["id"], "content": _redact_for_display(q["content"])} for q in slot._queue
            ],
            "total": total,
            "has_more": total > len(recent),
            "memory_mode": slot.memory_mode,
            "mode": slot.mode,
            "surface": slot.mode,
        }
    )


MAX_COLOR_INDEX = 20


async def api_chat_slot_color(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/color — set session color."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    ci = body.get("color_index")
    if ci is not None and (
        isinstance(ci, bool) or not isinstance(ci, int) or ci < 0 or ci > MAX_COLOR_INDEX
    ):
        return web.json_response(
            {"error": f"color_index must be a non-negative integer <= {MAX_COLOR_INDEX} or null"},
            status=400,
        )
    slot.color_index = ci
    slot._dirty = True
    state.push_slots_update()
    return web.json_response({"ok": True, "color_index": ci})


_MAX_CONTEXT_PER_SOURCE = 10


async def api_chat_slot_context(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/context — inject silent background context.

    Adds a ContextEntry to the slot's ``_pending_context`` queue.
    The content is consumed on the next user-initiated message via
    ``ctx_builder.build_message()`` and prepended to the LLM prompt.

    No LLM turn is triggered, no WS event is broadcast, and no visible
    message is appended to the slot's chat history.

    Body::

        {
            "content": "...",
            "source": "watch-check",   // optional
            "ephemeral": true,         // optional, default true
            "maxAge": 300              // optional, seconds
        }
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "slot not found"}, status=404)

    # App ownership check (App Kit §5.2): deny-by-default for app tokens.
    # Apps can only access slots they own. Dashboard users (empty request_app)
    # can access everything.
    request_app = request.get("app", "")
    if request_app:
        if not slot._app:
            sel().log_api_access(
                caller=request_app,
                operation="context_inject",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app cannot access unscoped slots",
            )
            return web.json_response({"error": "not found"}, status=404)
        elif request_app != slot._app:
            sel().log_api_access(
                caller=request_app,
                operation="context_inject",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app does not own this slot",
            )
            return web.json_response({"error": "not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    content = body.get("content", "")
    if not content:
        return web.json_response({"error": "content is required"}, status=400)

    # Content size limit (40,000 chars — same as message limit)
    max_context_content = 40000
    if len(content) > max_context_content:
        return web.json_response(
            {"error": f"content exceeds {max_context_content} char limit"}, status=400
        )

    entry: dict[str, object] = {
        "content": content,
        "source": body.get("source", ""),
        "ephemeral": body.get("ephemeral", True),
        "injectedAt": time.time(),
    }
    max_age = body.get("maxAge")
    if max_age is not None:
        entry["maxAge"] = max_age

    # Per-source cap: prevent one app from evicting all others' context
    source = body.get("source", "")
    if source:
        source_count = sum(1 for e in slot._pending_context if e.get("source") == source)
        if source_count >= _MAX_CONTEXT_PER_SOURCE:
            return web.json_response(
                {"error": f"source {source!r} has {_MAX_CONTEXT_PER_SOURCE} pending entries"},
                status=429,
            )

    # FIFO eviction: cap pending queue at the shared ceiling
    while len(slot._pending_context) >= _MAX_PENDING_CONTEXT:
        slot._pending_context.pop(0)

    slot._pending_context.append(entry)  # type: ignore[arg-type]

    # SEL audit logging
    sel().log_api_access(
        caller=request_app or request.get("user", "dashboard"),
        operation="context_inject",
        outcome="ok",
        source="app_kit",
        resources=f"slot={name}",
    )

    return web.json_response({"ok": True, "pending": len(slot._pending_context)})
