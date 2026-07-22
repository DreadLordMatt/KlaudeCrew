"""App CRUD + lifecycle REST handlers (install/update/uninstall/enable/disable/...).

Extracted from ``routes.py`` (LOC split) and re-exported from ``routes``. Imports
lifecycle helpers from ``lifecycle`` and the secret-cache invalidator from
``proxy`` (both one-way edges, keeping the module graph a DAG).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.app_lifecycle import (
    _BUILTIN_SERVICE_APPS,
    _check_min_version,
    _notify_builtin_service,
    _redact_warning,
    _run_lifecycle_script,
    _sync_builtin_config,
)
from kiro_crew.apps.backend import (
    list_app_processes,
    start_app_backend,
    stop_app_backend,
)
from kiro_crew.apps.bridges import (
    deregister_app,
    deregister_app_crons_from_service,
    register_app,
    reregister_app_mcp_servers,
)
from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.apps.dependencies import resolve_dependencies as _resolve_deps
from kiro_crew.apps.hooks_integration import on_app_disable, on_app_enable
from kiro_crew.apps.manager import (
    app_lifecycle_lock,
    cleanup_migrated_builtin,
    disable_app,
    enable_app,
    get_app,
    get_app_manifest,
    install_app,
    list_apps,
    register_external_app,
    uninstall_app,
    update_app,
)
from kiro_crew.apps.manifest import Dependencies
from kiro_crew.apps.proxy import invalidate_app_secret_cache
from kiro_crew.apps.registry import (
    install_from_registry,
    is_registry_source,
    registry_name_from_source,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


async def handle_list_apps(request: web.Request) -> web.Response:
    """GET /api/apps — list all installed apps."""
    apps = list_apps()
    # Enrich with backend process status
    procs = {p["app_name"]: p for p in list_app_processes()}
    for app in apps:
        proc = procs.get(app["name"])
        if proc:
            app["backend_status"] = {
                "running": True,
                "port": proc["port"],
                "healthy": proc["healthy"],
                "pid": proc["pid"],
            }
    return web.json_response(apps)


async def handle_get_app(request: web.Request) -> web.Response:
    """GET /api/apps/{name} — get single app details."""
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        # Compat: migrated deploy-web requests hit this generic handler before
        # the deploy module's /api/apps/deploy-web/{tail} redirect (aiohttp
        # matches in registration order). Redirect to the canonical endpoint.
        if name == "deploy-web":
            raise web.HTTPTemporaryRedirect(location="/api/deploy/list")
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)
    return web.json_response(info)


async def handle_get_manifest(request: web.Request) -> web.Response:
    """GET /api/apps/{name}/manifest — get app manifest."""
    name = request.match_info["name"]
    manifest = get_app_manifest(name)
    if not manifest:
        # Compat: migrated deploy-web — redirect to canonical endpoint.
        if name == "deploy-web":
            raise web.HTTPTemporaryRedirect(location="/api/deploy/config")
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)
    return web.json_response(manifest.to_dict())


async def _start_backend_after_install(name: str) -> None:
    """Spawn an app's backend after a fresh install/register, if it has one.

    ``start_app_backend`` is a no-op for apps that declare no backend and is
    idempotent for already-running ones, so this is safe to call unconditionally.
    It blocks on a health-check poll, so run it off the event loop. Failures are
    logged but never abort the install — the backend also gets a retry on the
    next gateway boot via ``start_enabled_app_backends``.
    """
    try:
        await asyncio.get_running_loop().run_in_executor(subprocess_executor(), start_app_backend, name)
    except Exception:
        logger.warning("Backend auto-start after install failed for app %s", name, exc_info=True)


async def handle_install_app(request: web.Request) -> web.Response:
    """POST /api/apps/install — install an app from a local path."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    source = body.get("source", "")
    if not source:
        return web.json_response({"error": "source path required"}, status=400)

    # Check minKiroCrewVersion before installing
    source_path = Path(source).expanduser().resolve()
    manifest_path = source_path / "app.json"
    lock_name = str(source_path)  # fallback lock key when manifest is unreadable
    if manifest_path.is_file():
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            ver_err = _check_min_version(manifest_data)
            if ver_err:
                return web.json_response({"error": ver_err}, status=400)
            raw_name = manifest_data.get("name")
            # Only a nonempty string is a usable lock key — anything else
            # (list, dict, number) keeps the path fallback and is rejected
            # by manifest validation inside install_app.
            if isinstance(raw_name, str) and raw_name:
                lock_name = raw_name
        except (json.JSONDecodeError, OSError):
            pass

    # Per-app lifecycle lock (shared with registry installs), held across
    # the whole install transaction — copy, registration, and backend start —
    # so a concurrent uninstall cannot deregister between our copy and our
    # register, leaving a running backend for a removed app.
    async with app_lifecycle_lock(lock_name):
        # Off-loop: the copy in install_app is blocking filesystem I/O that can
        # take minutes on large source trees — running it on the loop would trip
        # the loop-stall watchdog and kill the gateway.
        result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), install_app, source
        )
        if not result.ok:
            sel().log_api_access(caller="dashboard", operation="app_install", outcome="failed", resources=source, error=result.error)
            return web.json_response(result.to_dict(), status=400)
        invalidate_app_secret_cache(result.name)

        # Auto-register resources
        reg = register_app(result.name)
        # Spawn the backend now so the app is reachable without a gateway reboot
        # (see _start_backend_after_install). No-op for backend-less apps.
        await _start_backend_after_install(result.name)
    sel().log_api_access(caller="dashboard", operation="app_install", outcome="completed", resources=result.name)
    return web.json_response({
        **result.to_dict(),
        "registration": reg.to_dict(),
    }, status=201)


async def handle_update_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/update — update an installed app from its source path."""
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    # Apps with lifecycle != "gateway" handle their own updates
    lifecycle = info.get("lifecycle", "gateway")
    if lifecycle != "gateway":
        return web.json_response(
            {"error": f"app {name!r} has lifecycle={lifecycle!r} — cannot be updated via this endpoint"},
            status=400,
        )

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}

    source = body.get("source", info.get("source", ""))

    # Registry-installed apps: re-clone from registry.
    # Attempt install first, only deregister old resources on success
    # to avoid leaving the app in a broken state on failure.
    if is_registry_source(source):
        registry_name = registry_name_from_source(source)
        # One lock across re-install + resource swap + backend restart
        # (install_from_registry is lock-free internally).
        async with app_lifecycle_lock(name):
            reg_install = await install_from_registry(registry_name)
            if not reg_install.get("ok"):
                sel().log_api_access(caller="dashboard", operation="app_update", outcome="failed", resources=name, error=reg_install.get("error", ""))
                return web.json_response(reg_install, status=400)
            # Install succeeded — now safe to swap resources
            deregister_app(name)
            await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)
            if info.get("enabled"):
                reg_result = register_app(name)
                await asyncio.get_running_loop().run_in_executor(subprocess_executor(), start_app_backend, name)
                reg_install["registration"] = reg_result.to_dict()
        sel().log_api_access(caller="dashboard", operation="app_update", outcome="completed", resources=name)
        return web.json_response(reg_install)

    if not source:
        return web.json_response(
            {"error": "source path required (not found in installed metadata)"}, status=400,
        )

    # Per-app lifecycle lock: the deregister → stop → copy → re-register
    # sequence must not interleave with another update/install/uninstall of
    # the same app — update_app moves user data through a shared
    # ``.{name}-data-tmp`` path, so an interleaving can destroy it.
    # (The registry branch above locks inside install_from_registry.)
    async with app_lifecycle_lock(name):
        # Deregister old resources before update
        deregister_app(name)
        await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)

        # Off-loop: blocking filesystem copy (see handle_install_app).
        # expected_name makes update_app itself reject a source whose
        # manifest names a different app than the one this lock guards.
        up_result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), lambda: update_app(source, expected_name=name)
        )
        if not up_result.ok:
            # Re-register old resources on failure
            register_app(name)
            if info.get("enabled"):
                await asyncio.get_running_loop().run_in_executor(subprocess_executor(), start_app_backend, name)
            sel().log_api_access(caller="dashboard", operation="app_update", outcome="failed", resources=name, error=up_result.error)
            return web.json_response(up_result.to_dict(), status=400)

        # Re-register with new manifest if app was enabled
        up_reg = None
        if info.get("enabled"):
            up_reg = register_app(name)
            await asyncio.get_running_loop().run_in_executor(subprocess_executor(), start_app_backend, name)

    sel().log_api_access(caller="dashboard", operation="app_update", outcome="completed", resources=name)
    resp: dict[str, Any] = up_result.to_dict()
    if up_reg:
        resp["registration"] = up_reg.to_dict()
    return web.json_response(resp)


async def handle_register_external(request: web.Request) -> web.Response:
    """POST /api/apps/register — register a self-managed app.

    Self-managed apps handle their own agent/skill/MCP registration.
    KiroCrew only tracks metadata so the dashboard can display them.
    Idempotent — calling again with a newer version updates the entry.

    Body: { name, version, displayName, source?, manifest? }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = body.get("name", "")
    version = body.get("version", "")
    display_name = body.get("displayName", "")
    if not name or not version or not display_name:
        return web.json_response(
            {"error": "name, version, and displayName are required"}, status=400,
        )

    result = register_external_app(
        name=name,
        version=version,
        display_name=display_name,
        source=body.get("source", ""),
        manifest_data=body.get("manifest"),
        origin=body.get("origin", "external"),
        resources=body.get("resources", "app"),
        lifecycle=body.get("lifecycle", "app"),
    )
    if not result.ok:
        sel().log_api_access(caller="dashboard", operation="app_register_external", outcome="failed", resources=name, error=result.error)
        return web.json_response(result.to_dict(), status=400)
    sel().log_api_access(caller="dashboard", operation="app_register_external", outcome="completed", resources=name)
    resp = result.to_dict()
    # Include the generated app secret so the caller can use it for auth
    if result.secret:
        resp["secret"] = result.secret
    return web.json_response(resp, status=201)


async def handle_uninstall_preview(request: web.Request) -> web.Response:
    """GET /api/apps/{name}/uninstall/preview — preview uninstall impact.

    Returns resource list and dependency classification (removable/shared/userInstalled).
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    lifecycle = info.get("lifecycle", "gateway")
    if lifecycle == "locked":
        return web.json_response(
            {"error": f"app {name!r} cannot be uninstalled (lifecycle=locked)"},
            status=400,
        )

    manifest = info.get("manifest", {})
    deps_data = manifest.get("dependencies", {})

    # Collect declared dependency keys
    declared_deps: list[str] = []
    aim_deps = deps_data.get("aim", {})
    for dep_type in ("mcp", "skills", "agents"):
        for entry in aim_deps.get(dep_type, []):
            dep_id = entry.get("id") if isinstance(entry, dict) else entry
            if not dep_id:
                continue
            declared_deps.append(f"aim/{dep_type}/{dep_id}")

    # Classify dependencies
    from kiro_crew.apps.dependency_ledger import classify_for_uninstall
    dep_classification = classify_for_uninstall(name, declared_deps)

    return web.json_response({
        "app": name,
        "lifecycle": lifecycle,
        "resources": {
            "agents": manifest.get("agents", []),
            "skills": manifest.get("skills", []),
            "crons": [c.get("name", "") for c in manifest.get("crons", [])],
        },
        "dependencies": dep_classification,
    })


async def handle_uninstall_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/uninstall — uninstall an app.

    1. Check lifecycle field (locked → 400)
    2. Run onUninstall script (if declared)
    3. Stop backend + deregister resources (gateway-managed only)
    4. Clean removable dependencies (unless keep_dependencies=true)
    5. Remove app files (preserve data/ if keep_data=true)
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    lifecycle = info.get("lifecycle", "gateway")
    if lifecycle == "locked":
        return web.json_response(
            {"error": f"app {name!r} cannot be uninstalled (lifecycle=locked)"},
            status=400,
        )

    resources = info.get("resources", "gateway")
    manifest = info.get("manifest", {})
    uninstall_log: list[str] = []

    # Parse body
    keep_data = False
    keep_dependencies = False
    keep_specific: list[str] = []
    try:
        body = await request.json()
        keep_data = body.get("keep_data", False)
        keep_dependencies = body.get("keep_dependencies", False)
        keep_specific = body.get("keep_specific", [])
    except Exception:
        pass

    # Step 1: Run onUninstall script
    on_uninstall = (manifest.get("setup") or {}).get("onUninstall", "")
    if on_uninstall:
        script_output = await _run_lifecycle_script(
            name, on_uninstall, timeout=120,
            extra_env={"KEEP_DATA": "1" if keep_data else "0"},
        )
        if script_output.get("output"):
            from kiro_crew.security import redact_credentials, redact_exfiltration_urls
            cleaned, _ = redact_exfiltration_urls(script_output["output"])
            cleaned, _ = redact_credentials(cleaned)
            uninstall_log.append(cleaned)
        if script_output.get("failed"):
            uninstall_log.append("onUninstall script failed (exit code non-zero)")

    # Per-app lifecycle lock, held across deregistration → dependency cleanup
    # → file removal, so this sequence cannot interleave with a concurrent
    # install/update of the same app (which holds the same lock across copy →
    # registration → backend start). Without it, an install could re-register
    # and start a backend between our deregister and our file removal.
    async with app_lifecycle_lock(name):
        # Step 2: Deregister resources (gateway-managed only)
        if resources == "gateway":
            await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)
            # Clean up app-declared cron jobs from the scheduler before the
            # per-app cron manifest is removed by deregister_app(). Mirrors the
            # cleanup that on_app_disable performs on the disable path.
            state = request.app.get("state")
            cron_service = getattr(state, "crons", None) if state else None
            if cron_service is not None:
                try:
                    removed = deregister_app_crons_from_service(name, cron_service)
                    sel().log_api_access(
                        caller="dashboard",
                        operation="app_crons_deregister",
                        outcome="completed",
                        resources=f"app={name} removed={removed}",
                    )
                except Exception as exc:
                    logger.warning("Cron cleanup failed for %s on uninstall: %s", name, exc)
                    sel().log_api_access(
                        caller="dashboard",
                        operation="app_crons_deregister",
                        outcome="failed",
                        resources=name,
                        error=str(exc),
                    )
            deregister_app(name)

        # Step 3: Clean dependencies (atomic classify + ledger update)
        cleaned_deps: list[str] = []
        if not keep_dependencies:
            deps_data = manifest.get("dependencies", {})
            aim_deps = deps_data.get("aim", {})
            declared_deps: list[str] = []
            for dep_type in ("mcp", "skills", "agents"):
                for entry in aim_deps.get(dep_type, []):
                    dep_id = entry.get("id") if isinstance(entry, dict) else entry
                    if not dep_id:
                        continue
                    declared_deps.append(f"aim/{dep_type}/{dep_id}")

            from kiro_crew.apps.dependency_ledger import classify_and_clean_for_uninstall
            classification = classify_and_clean_for_uninstall(
                name, declared_deps, keep_specific=list(keep_specific),
            )
            removable = [
                d for d in classification.get("removable", [])
                if d.get("id") not in keep_specific
            ]
            if removable:
                from kiro_crew.apps.dependencies import clean_dependencies
                cleaned_deps = await clean_dependencies(name, removable)
                if cleaned_deps:
                    uninstall_log.append(f"Cleaned {len(cleaned_deps)} dependency(ies)")

        # Step 4: Remove files. Off-loop: rmtree of a large installed tree is
        # blocking filesystem I/O. (uninstall_app shares the
        # ``.{name}-data-tmp`` move-aside path with install/update — covered
        # by the lifecycle lock held above.)
        result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), lambda: uninstall_app(name, keep_data=keep_data)
        )
    if not result.ok:
        sel().log_api_access(caller="dashboard", operation="app_uninstall", outcome="failed", resources=name, error=result.error)
        return web.json_response(result.to_dict(), status=400)
    invalidate_app_secret_cache(name)

    # Step 5: Clean up workspace (each registry app has its own workspace)
    if is_registry_source(info.get("source", "")):
        app_reg_name = registry_name_from_source(info.get("source", ""))
        if app_reg_name:
            from kiro_crew.apps.registry import app_source_dir
            ws_dir = app_source_dir(app_reg_name)
            if ws_dir.is_dir():
                shutil.rmtree(ws_dir, ignore_errors=True)
                uninstall_log.append(f"Removed workspace for {app_reg_name}")

    sel().log_api_access(caller="dashboard", operation="app_uninstall", outcome="completed", resources=name)
    resp = result.to_dict()
    if uninstall_log:
        resp["uninstall_log"] = "\n".join(uninstall_log)
    if cleaned_deps:
        resp["cleaned_dependencies"] = cleaned_deps
    return web.json_response(resp)


async def handle_enable_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/enable — enable an app.

    Behavior depends on ``resources`` field:
    - ``gateway``: register_app() + start_backend() + run onEnable
    - ``app``: run onEnable only
    If onEnable fails, the enable is rolled back (app stays disabled).
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    resources = info.get("resources", "gateway")
    manifest = info.get("manifest", {})
    on_enable = (manifest.get("setup") or {}).get("onEnable", "")
    enable_timeout = int((manifest.get("setup") or {}).get("onEnableTimeout", 30))

    # Per-app lifecycle lock: enable mutates metadata, registers resources,
    # and starts the backend — must not interleave with a concurrent
    # install/update/uninstall of the same app (e.g. enabling while an
    # off-loop uninstall is deleting the app directory).
    async with app_lifecycle_lock(name):
        result = enable_app(name)
        if not result.ok:
            sel().log_api_access(caller="dashboard", operation="app_enable", outcome="failed", resources=name, error=result.error)
            return web.json_response(result.to_dict(), status=400)

        resp: dict[str, Any] = result.to_dict()

        # Register resources if gateway-managed
        if resources == "gateway":
            reg = register_app(name)
            backend = await asyncio.get_running_loop().run_in_executor(subprocess_executor(), start_app_backend, name)
            # MCP re-registration is HEALTH-GATED (review CR-284432051). register_app ran before
            # the backend was up, so an HTTP MCP server with backend.port:"auto" carries the
            # manifest's illustrative port. The backend's health-check loop calls
            # _gate_mcp_registration once /health passes, rewriting the url to the real allocated
            # port (and scrubbing it if the backend never becomes healthy — the dead-url shape
            # that broke kiro-cli). EXCEPTION: an adopted already-healthy instance runs no health
            # loop, so register it synchronously here.
            if backend is not None and getattr(backend, "healthy", False):
                try:
                    reregister_app_mcp_servers(name, live_port=getattr(backend, "port", None))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("MCP re-registration after backend start failed for %s: %s", name, exc)
            resp["registration"] = reg.to_dict()
            if backend:
                resp["backend"] = backend.to_dict()

        # Resolve declared dependencies (if any)
        deps_data = manifest.get("dependencies")
        if deps_data and isinstance(deps_data, dict):
            deps = Dependencies.from_dict(deps_data)
            dep_result = await _resolve_deps(name, deps)
            sel().log_api_access(
                caller="dashboard",
                operation="app_enable_resolve_deps",
                outcome="partial_failure" if dep_result.failed else "success",
                resources=name,
                error=str(dep_result.failed) if dep_result.failed else "",
            )
            dep_info: dict[str, Any] = {}
            if dep_result.installed:
                dep_info["installed"] = dep_result.installed
            if dep_result.failed:
                dep_info["failed"] = dep_result.failed
            if dep_result.missing:
                dep_info["missing"] = dep_result.missing
            if dep_info:
                resp["dependencies"] = dep_info

        # Run onEnable script
        if on_enable:
            script_output = await _run_lifecycle_script(name, on_enable, timeout=enable_timeout)
            if script_output.get("failed"):
                # Rollback: disable the app again
                if resources == "gateway":
                    await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)
                    deregister_app(name)
                disable_app(name)
                sel().log_api_access(caller="dashboard", operation="app_enable", outcome="failed", resources=name, error="onEnable script failed")
                from kiro_crew.security import redact_credentials
                cleaned, _ = redact_credentials(script_output.get("output", ""))
                return web.json_response({
                    "ok": False, "name": name,
                    "error": "onEnable script failed — app remains disabled",
                    "script_output": cleaned,
                }, status=400)
            resp["onEnable"] = {
                "output": "",
                "failed": False,
            }
            if script_output.get("output"):
                from kiro_crew.security import redact_credentials
                cleaned, _ = redact_credentials(script_output.get("output", ""))
                resp["onEnable"]["output"] = cleaned
            resp["onEnable"]["failed"] = script_output.get("failed", False)

        # Invoke Python lifecycle hooks (routes + on_startup) — runs AFTER shell scripts
        try:
            state = request.app.get("state")
            hooks_result = await on_app_enable(
                name, info,
                cron_service=getattr(state, "crons", None),
                broadcast_fn=getattr(state, "broadcast", None),
            )
            if hooks_result:
                # Redact any sensitive content in health_status issues
                if "health_status" in hooks_result:
                    hs = hooks_result["health_status"]
                    if "issues" in hs:
                        hs["issues"] = [_redact_warning(i) for i in hs["issues"]]
                resp["hooks"] = hooks_result
        except Exception as exc:
            logger.warning("Hook execution failed for %s: %s", name, exc)
            resp.setdefault("warnings", []).append(_redact_warning(f"hooks failed: {exc}"))

        # Sync config.json and start live service for builtin apps
        origin = info.get("origin", "")
        if origin == "builtin" and name in _BUILTIN_SERVICE_APPS:
            try:
                _sync_builtin_config(name, enabled=True)
            except OSError as exc:
                logger.warning("Failed to sync config.json for %s: %s", name, exc)
                resp.setdefault("warnings", []).append(_redact_warning(f"config sync failed: {exc}"))
            else:
                svc_warn = await _notify_builtin_service(request, name)
                if svc_warn:
                    resp.setdefault("warnings", []).append(_redact_warning(svc_warn))

        sel().log_api_access(caller="dashboard", operation="app_enable", outcome="completed", resources=name)
        return web.json_response(resp)


async def handle_disable_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/disable — disable an app.

    Behavior depends on ``resources`` field:
    - ``gateway``: run onDisable + stop_backend() + deregister_app()
    - ``app``: run onDisable only
    If onDisable fails, disable proceeds anyway (with warnings).
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    resources = info.get("resources", "gateway")
    manifest = info.get("manifest", {})
    on_disable = (manifest.get("setup") or {}).get("onDisable", "")
    disable_timeout = int((manifest.get("setup") or {}).get("onDisableTimeout", 30))
    warnings: list[str] = []

    # Per-app lifecycle lock: disable stops the backend and deregisters
    # resources — must not interleave with a concurrent install/update/
    # uninstall/enable of the same app.
    async with app_lifecycle_lock(name):
        # Run onDisable script first
        if on_disable:
            script_output = await _run_lifecycle_script(name, on_disable, timeout=disable_timeout)
            if script_output.get("failed"):
                from kiro_crew.security import redact_credentials
                raw_output = script_output.get("output", "")[:200]
                cleaned, _ = redact_credentials(raw_output)
                warnings.append(f"onDisable script failed: {cleaned}")
                logger.warning("onDisable failed for %s, proceeding with disable", name)

        # Invoke Python lifecycle hooks (on_shutdown + route deregistration + cron cleanup)
        try:
            hooks_result = await on_app_disable(name, info)
            if hooks_result:
                for k, v in hooks_result.items():
                    if k == "cron_cleanup" and isinstance(v, str):
                        warnings.append(v)
        except Exception as exc:
            logger.warning("Hook disable failed for %s: %s", name, exc)
            warnings.append(_redact_warning(f"hooks disable failed: {exc}"))

        # Deregister resources if gateway-managed
        if resources == "gateway":
            await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)
            deregister_app(name)

        result = disable_app(name)
        if not result.ok:
            sel().log_api_access(caller="dashboard", operation="app_disable", outcome="failed", resources=name, error=result.error)
            return web.json_response(result.to_dict(), status=400)

        # Run builtin on_disable hook if available
        if name in BUILTIN_NAMES:
            try:
                mod = importlib.import_module(f"kiro_crew.apps.builtins.{name}")
                if hasattr(mod, "on_disable"):
                    mod.on_disable(request.app)
            except Exception as exc:
                logger.warning("on_disable hook for %s failed: %s", name, exc)
                warnings.append(_redact_warning(f"on_disable hook failed: {exc}"))

        # Sync config.json and stop live service for builtin apps
        origin = info.get("origin", "")
        if origin == "builtin" and name in _BUILTIN_SERVICE_APPS:
            try:
                _sync_builtin_config(name, enabled=False)
            except OSError as exc:
                logger.warning("Failed to sync config.json for %s: %s", name, exc)
                warnings.append(_redact_warning(f"config sync failed: {exc}"))
            else:
                svc_warn = await _notify_builtin_service(request, name)
                if svc_warn:
                    warnings.append(_redact_warning(svc_warn))

        sel().log_api_access(caller="dashboard", operation="app_disable", outcome="completed", resources=name)
        resp = result.to_dict()
        if warnings:
            resp["warnings"] = warnings
        return web.json_response(resp)


async def handle_open_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/open — launch an app using its openCommand.

    For apps that run outside the dashboard (e.g. Electron apps),
    the manifest can declare an ``openCommand`` shell string that
    launches the app.  This endpoint executes it in the background.

    On cloud/remote environments (no display), returns the command
    for the user to run locally instead of executing it.
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not found"}, status=404)

    manifest = info.get("manifest", {})
    open_cmd = manifest.get("openCommand", "")
    if not open_cmd:
        return web.json_response({"error": "app has no openCommand"}, status=400)

    # Detect cloud/remote — no DISPLAY and not macOS desktop
    import os
    import platform
    is_local = (
        platform.system() == "Darwin"
        or os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
    )

    if not is_local:
        return web.json_response({
            "ok": False,
            "name": name,
            "remote": True,
            "command": open_cmd,
            "message": f"KiroCrew is running remotely. Run this on your local machine: {open_cmd}",
        })

    try:
        from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv
        base_cmd = ["/bin/sh", "-c", open_cmd]
        sandboxed_cmd, _cleanup = wrap_argv(base_cmd, mode="standard")
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling (Talos bdf0d7e5)
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=resource_limit_preexec(),
        )
        # Don't wait — launch is fire-and-forget
        sel().log_api_access(
            caller="dashboard", operation="app_open",
            outcome="launched", resources=f"{name} pid={proc.pid}",
        )
        return web.json_response({"ok": True, "name": name, "pid": proc.pid})
    except Exception as exc:
        sel().log_api_access(
            caller="dashboard", operation="app_open",
            outcome="failed", resources=name, error=str(exc),
        )
        return web.json_response({"error": f"failed to launch: {exc}"}, status=500)


async def handle_app_config(request: web.Request) -> web.Response:
    """GET/PUT /api/apps/{name}/config — read or write app config.json.

    Reads/writes ``~/.kirocrew/apps/{name}/data/config.json``.
    GET returns the current config (empty ``{}`` if none exists).
    PUT replaces the config with the request body.
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        # Compat: migrated deploy-web — redirect to canonical deploy config endpoint.
        if name == "deploy-web":
            raise web.HTTPTemporaryRedirect(location="/api/deploy/config")
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    from kiro_crew.apps.manager import app_data_dir
    from kiro_crew.atomic_write import atomic_write

    data_dir = app_data_dir(name)
    config_path = data_dir / "config.json"

    if request.method == "GET":
        if not config_path.is_file():
            # Missing config (e.g. data dir wiped by an app update) — seed an
            # empty config so the app gets a valid response instead of hanging
            # on a perpetual "loading" state. The app repopulates it on first use.
            try:
                await asyncio.to_thread(atomic_write, config_path, "{}\n")
            except OSError:
                pass
            return web.json_response({})
        try:
            text = await asyncio.to_thread(config_path.read_text, encoding="utf-8")
            return web.json_response(json.loads(text))
        except (json.JSONDecodeError, OSError) as exc:
            return web.json_response(
                {"error": f"failed to read config: {exc}"}, status=500
            )

    # PUT — write config
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return web.json_response(
            {"error": "config must be a JSON object"}, status=400
        )

    try:
        content = json.dumps(body, indent=2) + "\n"
        await asyncio.to_thread(atomic_write, config_path, content)
    except OSError as exc:
        return web.json_response(
            {"error": f"failed to write config: {exc}"}, status=500
        )

    sel().log_api_access(
        caller="dashboard",
        operation="app_config_write",
        outcome="completed",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name})


async def handle_migrate_cleanup(request: web.Request) -> web.Response:
    """DELETE /api/apps/{name}/migrate-cleanup — remove orphaned builtin metadata.

    Validates:
    1. Target app is an orphaned builtin
    2. The standalone replacement is installed

    Preserves data/ directory.
    """
    name = request.match_info["name"]
    result = cleanup_migrated_builtin(name)
    if not result.ok:
        # Map structured error_code to HTTP status
        _cleanup_status = {
            "not_orphaned": 400,
            "replacement_missing": 409,
            "io_error": 500,
        }
        status = _cleanup_status.get(result.error_code, 400)
        sel().log_api_access(caller="dashboard", operation="app_migrate_cleanup", outcome="failed", resources=name, error=result.error)
        return web.json_response(result.to_dict(), status=status)
    sel().log_api_access(caller="dashboard", operation="app_migrate_cleanup", outcome="completed", resources=name)
    return web.json_response(result.to_dict())
