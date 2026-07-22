"""App management REST API endpoints for the KiroCrew dashboard.

All endpoints are registered under ``/api/apps`` by the dashboard handler
setup. These are aiohttp-compatible handler functions.

Module layout (LOC split): the handlers were extracted into sibling modules
that keep the same import surface. This module retains the public entry
``register_app_routes`` (proxy-session startup/cleanup + all router ``add_*``
calls) and re-exports every moved symbol so external callers and tests that do
``from kiro_crew.apps.routes import X`` (or patch ``kiro_crew.apps.routes.X``
for symbols still *used* here) keep working:

- ``app_lifecycle``    — version check, lifecycle scripts, builtin service sync
- ``providers``        — publish-provider aggregation
- ``crud``             — install/update/uninstall/enable/disable/get/config/...
- ``registry_routes``  — registry browse/install + federated registries
- ``static_files``     — app UI file serving + git blob image proxy
- ``proxy``            — reverse proxy to app backends + app-secret cache
"""
from __future__ import annotations

import logging

import aiohttp
from aiohttp import web

from kiro_crew.apps.app_lifecycle import (  # noqa: F401
    _BUILTIN_SERVICE_APPS,
    _check_min_version,
    _notify_builtin_service,
    _redact_warning,
    _run_lifecycle_script,
    _sync_builtin_config,
)
from kiro_crew.apps.crud import (  # noqa: F401
    _start_backend_after_install,
    handle_app_config,
    handle_disable_app,
    handle_enable_app,
    handle_get_app,
    handle_get_manifest,
    handle_install_app,
    handle_list_apps,
    handle_migrate_cleanup,
    handle_open_app,
    handle_register_external,
    handle_uninstall_app,
    handle_uninstall_preview,
    handle_update_app,
)
from kiro_crew.apps.providers import (  # noqa: F401
    _provider_is_configured,
    collect_publish_providers,
    handle_publish_providers,
)
from kiro_crew.apps.proxy import (  # noqa: F401
    _PROXY_HOP_HEADERS,
    _PROXY_STRIP_HEADERS,
    _PROXY_TIMEOUT,
    _app_secret_cache,
    _get_app_secret,
    _resolve_app_backend_url,
    handle_app_api_proxy,
    invalidate_app_secret_cache,
)
from kiro_crew.apps.registry_routes import (  # noqa: F401
    handle_registries,
    handle_registry,
    handle_registry_install,
    handle_registry_install_stream,
)
from kiro_crew.apps.static_files import (  # noqa: F401
    _ALLOWED_EXTENSIONS,
    _BLOB_ALLOWED_EXT,
    _BLOB_FETCH_SEMAPHORE,
    _BLOB_FETCH_TIMEOUT,
    _CONTENT_TYPES,
    _SAFE_HTTPS_URL_RE,
    _SAFE_PATH_RE,
    _SAFE_REF_RE,
    _SAFE_REPO_RE,
    _SAFE_SCP_URL_RE,
    _SAFE_SSH_URL_RE,
    _blob_cache_dir,
    _fetch_git_blob,
    _is_safe_repo_identifier,
    _registry_git_url,
    handle_app_ui_file,
    handle_blob_proxy,
)

logger = logging.getLogger(__name__)


def register_app_routes(app: web.Application) -> None:
    """Register all app management routes on an aiohttp Application."""
    # (moved to top-level)

    async def _start_proxy_session(app: web.Application) -> None:
        app["_proxy_session"] = aiohttp.ClientSession()

    async def _close_proxy_session(app: web.Application) -> None:
        session = app.get("_proxy_session")
        if session and not session.closed:
            await session.close()

    app.on_startup.append(_start_proxy_session)
    app.on_cleanup.append(_close_proxy_session)

    app.router.add_get("/api/apps", handle_list_apps)
    app.router.add_get("/api/publish-providers", handle_publish_providers)
    app.router.add_get("/api/apps/registry", handle_registry)
    app.router.add_get("/api/apps/registries", handle_registries)
    app.router.add_put("/api/apps/registries", handle_registries)
    app.router.add_get("/api/apps/blob", handle_blob_proxy)
    app.router.add_post("/api/apps/registry/install", handle_registry_install)
    app.router.add_post("/api/apps/registry/install-stream", handle_registry_install_stream)
    app.router.add_post("/api/apps/install", handle_install_app)
    app.router.add_post("/api/apps/register", handle_register_external)
    app.router.add_get("/api/apps/{name}", handle_get_app)
    app.router.add_get("/api/apps/{name}/manifest", handle_get_manifest)
    app.router.add_get("/api/apps/{name}/config", handle_app_config)
    app.router.add_put("/api/apps/{name}/config", handle_app_config)
    app.router.add_post("/api/apps/{name}/uninstall", handle_uninstall_app)
    app.router.add_post("/api/apps/{name}/update", handle_update_app)
    app.router.add_post("/api/apps/{name}/enable", handle_enable_app)
    app.router.add_post("/api/apps/{name}/disable", handle_disable_app)
    app.router.add_post("/api/apps/{name}/open", handle_open_app)
    app.router.add_delete("/api/apps/{name}/migrate-cleanup", handle_migrate_cleanup)
    app.router.add_get("/apps/{name}/ui/{path:.*}", handle_app_ui_file)
    # Reverse proxy: dashboard app UI → app backend (same-origin, avoids CORS)
    app.router.add_route("*", "/apps/{name}/api/{path:.*}", handle_app_api_proxy)
