"""Registry browse/install endpoints + federated registry management.

Extracted from ``routes.py`` (LOC split) and re-exported from ``routes``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.bridges import register_app
from kiro_crew.apps.crud import _start_backend_after_install
from kiro_crew.apps.manager import app_lifecycle_lock
from kiro_crew.apps.registry import (
    get_server_platform,
    install_from_registry,
    list_registry,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry (browse & install from curated list)
# ---------------------------------------------------------------------------

async def handle_registry(request: web.Request) -> web.Response:
    """GET /api/apps/registry — list all apps available for installation."""
    apps = await list_registry()
    return web.json_response({
        "apps": apps,
        "serverPlatform": get_server_platform(),
    })


async def handle_registry_install(request: web.Request) -> web.Response:
    """POST /api/apps/registry/install — install an app from the registry.

    Clones the repo, runs the install script, and registers the app.
    This can take a while so the response includes a log of what happened.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = body.get("name", "")
    if not name:
        return web.json_response({"error": "app name required"}, status=400)

    # One lock for the complete transaction: install_from_registry is
    # lock-free internally (asyncio.Lock is not reentrant), so this is the
    # single acquisition covering clone/build → copy → register → backend.
    async with app_lifecycle_lock(name):
        result = await install_from_registry(name)

        # Redact install log and error before returning to client — build output
        # may contain internal hostnames, package URLs, or credential fragments.
        if result.get("log"):
            from kiro_crew.security import redact_credentials, redact_exfiltration_urls
            cleaned_log, _ = redact_exfiltration_urls(result["log"])
            cleaned_log, _ = redact_credentials(cleaned_log)
            result["log"] = cleaned_log
        if result.get("error"):
            from kiro_crew.security import redact_credentials, redact_exfiltration_urls
            cleaned_err, _ = redact_exfiltration_urls(result["error"])
            cleaned_err, _ = redact_credentials(cleaned_err)
            result["error"] = cleaned_err

        if result.get("needsClientInstall"):
            return web.json_response(result, status=200)
        if not result.get("ok"):
            sel().log_api_access(caller="dashboard", operation="app_registry_install", outcome="failed", resources=name, error=result.get("error", ""))
            return web.json_response(result, status=400)

        # Auto-register resources
        reg = register_app(result["name"])
        # Spawn the backend now so apps with a server are reachable immediately —
        # without this the backend only starts on the next gateway reboot (via
        # start_enabled_app_backends), leaving the app's UI with "no reachable
        # backend" until then. No-op for apps that declare no backend. Run in a
        # thread because start_app_backend blocks on a health-check poll.
        await _start_backend_after_install(result["name"])
    result["registration"] = reg.to_dict()
    sel().log_api_access(caller="dashboard", operation="app_registry_install", outcome="completed", resources=name)
    return web.json_response(result, status=201)


async def handle_registry_install_stream(request: web.Request) -> web.StreamResponse:
    """POST /api/apps/registry/install-stream — SSE streaming install.

    Same logic as ``handle_registry_install`` but streams log lines as
    Server-Sent Events in real-time, giving the user full transparency
    into what's happening during the (often slow) install process.

    Event types:
      ``log``   — a single log line (data: string)
      ``done``  — install finished (data: JSON with ok, name, error, etc.)

    The original ``/api/apps/registry/install`` endpoint is unchanged —
    CLI and other callers are not affected.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = body.get("name", "")
    if not name:
        return web.json_response({"error": "app name required"}, status=400)

    # Set up SSE response
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)

    # Create a queue-backed log collector so install_from_registry streams
    # each log line as it's appended — zero changes to the install logic.
    from kiro_crew.apps.registry import StreamingLogLines
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=500)
    streaming_log = StreamingLogLines(queue)

    async def _send_sse(event: str, data: str) -> None:
        """Write a single SSE frame.

        Multi-line data is split into multiple ``data:`` lines per the
        SSE spec (each line prefixed with ``data: ``).  This prevents
        newline injection from breaking the event stream framing.
        """
        try:
            # SSE spec: multi-line data uses one "data:" prefix per line
            lines = data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            payload = f"event: {event}\n"
            for line in lines:
                payload += f"data: {line}\n"
            payload += "\n"
            await resp.write(payload.encode("utf-8"))
        except (ConnectionResetError, ConnectionAbortedError):
            pass

    async def _drain_queue() -> None:
        """Forward queued log lines to the SSE stream until sentinel."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls
        while True:
            line = await queue.get()
            if line is None:
                break  # sentinel — install finished
            cleaned, _ = redact_exfiltration_urls(line)
            cleaned, _ = redact_credentials(cleaned)
            await _send_sse("log", cleaned)

    # Run install + drain concurrently. The complete lifecycle transaction —
    # install, resource registration, backend start — runs under one per-app
    # lock (install_from_registry is lock-free internally).
    async def _locked_install() -> dict[str, Any]:
        async with app_lifecycle_lock(name):
            r = await install_from_registry(name, log_lines=streaming_log)
            if r.get("ok") and not r.get("needsClientInstall"):
                reg = register_app(r["name"])
                # Spawn the backend immediately (see handle_registry_install) so
                # the app is reachable without a gateway reboot. No-op for
                # backend-less apps.
                await _start_backend_after_install(r["name"])
                r["registration"] = reg.to_dict()
            return r

    install_task = asyncio.create_task(_locked_install())
    drain_task = asyncio.create_task(_drain_queue())

    try:
        result = await install_task
    except Exception as exc:
        result = {"ok": False, "name": name, "error": str(exc)}
    finally:
        # Signal the drain loop to stop, then wait for it to flush.
        # Use blocking put — put_nowait raises QueueFull if the queue
        # is at capacity, which would prevent the sentinel from being
        # delivered and hang _drain_queue forever.
        await queue.put(None)
        await drain_task

    # Redact the final log and error fields before sending to client —
    # error may contain internal hostnames, git URLs, or credential
    # fragments from subprocess failures.
    if result.get("log"):
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls
        cleaned_log, _ = redact_exfiltration_urls(result["log"])
        cleaned_log, _ = redact_credentials(cleaned_log)
        result["log"] = cleaned_log
    if result.get("error"):
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls
        cleaned_err, _ = redact_exfiltration_urls(result["error"])
        cleaned_err, _ = redact_credentials(cleaned_err)
        result["error"] = cleaned_err

    if result.get("needsClientInstall"):
        await _send_sse("done", json.dumps(result))
        await resp.write_eof()
        return resp

    if not result.get("ok"):
        sel().log_api_access(caller="dashboard", operation="app_registry_install_stream", outcome="failed", resources=name, error=result.get("error", ""))
        await _send_sse("done", json.dumps(result))
        await resp.write_eof()
        return resp

    # Resource registration + backend start already ran inside the locked
    # transaction above; result carries "registration".
    sel().log_api_access(caller="dashboard", operation="app_registry_install_stream", outcome="completed", resources=name)
    await _send_sse("done", json.dumps(result))
    await resp.write_eof()
    return resp


async def handle_registries(request: web.Request) -> web.Response:
    """GET/PUT /api/apps/registries — manage external federated registries."""
    if request.method == "GET":
        config = KiroCrewConfig.load()
        registries = [
            {"name": r.name, "repo": r.repo, "branch": r.branch}
            for r in config.registries
        ]
        sel().log_api_access(
            caller="dashboard",
            operation="registries.read",
            outcome="success",
            resources=f"count={len(registries)}",
        )
        return web.json_response({"registries": registries})

    def _deny(msg: str, resources: str = "") -> web.Response:
        sel().log_api_access(
            caller="dashboard",
            operation="registries.update",
            outcome="denied",
            resources=resources or msg,
        )
        return web.json_response({"error": msg}, status=400)

    # PUT — replace the entire registries list
    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON", "invalid JSON body")

    entries = body.get("registries")
    if not isinstance(entries, list):
        return _deny("registries must be an array")

    # Validate each entry
    validated: list[dict[str, str]] = []
    _blocked_repos = {"KiroCrew"}
    for entry in entries:
        if not isinstance(entry, dict):
            return _deny("each registry must be an object")
        repo = str(entry.get("repo", "")).strip()
        if not repo:
            return _deny("repo is required")
        if not re.match(r"^[A-Za-z0-9_\-]+$", repo):
            return _deny(f"invalid repo name: {repo!r}", f"repo={repo}")
        if repo in _blocked_repos:
            return _deny(
                f"{repo!r} is the core registry — no need to add it", f"blocked_repo={repo}"
            )
        name = str(entry.get("name", "")).strip() or repo
        if not re.match(r"^[A-Za-z0-9_\-. ]+$", name):
            return _deny(f"invalid registry name: {name!r}", f"name={name}")
        branch = str(entry.get("branch", "mainline")).strip() or "mainline"
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_\-./]*$", branch) or ".." in branch:
            return _deny(f"invalid branch name: {branch!r}", f"branch={branch}")
        validated.append({"name": name, "repo": repo, "branch": branch})

    # Update config file (atomic write to prevent corruption on crash)
    cfg = Path(config_path())
    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.is_file() else {}
    except json.JSONDecodeError:
        sel().log_api_access(
            caller="dashboard",
            operation="registries.update",
            outcome="failed",
            resources="config.json malformed",
        )
        return web.json_response(
            {"error": "config.json is malformed — fix it before updating registries"},
            status=500,
        )
    except OSError as exc:
        sel().log_api_access(
            caller="dashboard",
            operation="registries.update",
            outcome="failed",
            resources=f"config read error: {exc}",
        )
        return web.json_response(
            {"error": f"cannot read config: {exc}"}, status=500
        )
    data["registries"] = validated
    cfg.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(cfg, json.dumps(data, indent=2) + "\n")

    sel().log_api_access(
        caller="dashboard",
        operation="registries.update",
        outcome="success",
        resources=f"count={len(validated)} repos={','.join(r['repo'] for r in validated)}",
    )
    return web.json_response({"ok": True, "registries": validated})
