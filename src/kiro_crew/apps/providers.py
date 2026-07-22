"""Publish-provider aggregation endpoints.

Extracted from ``routes.py`` (LOC split) and re-exported from ``routes``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import urllib.parse
from typing import Any

from aiohttp import web

from kiro_crew.apps.manager import apps_dir, list_apps

logger = logging.getLogger(__name__)


def _provider_is_configured(app_name: str, pp: dict[str, Any]) -> bool:
    """Resolve a provider's configured-state by reading the app's persisted config.

    Core never imports app code: it reads ``<apps_dir>/<app>/data/<configFile>`` and
    checks that ``configuredField`` is non-empty. When no ``configuredField`` is
    declared, the provider is considered configured as soon as the app is enabled.
    """
    field_name = str(pp.get("configuredField", "")).strip()
    if not field_name:
        return True
    config_file = str(pp.get("configFile", "config.json")) or "config.json"
    if ".." in config_file or "/" in config_file or "\\" in config_file:
        return False  # defensive: no path traversal in the declared config filename
    cfg_path = apps_dir() / app_name / "data" / config_file
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(cfg, dict) and str(cfg.get(field_name, "")).strip())


def collect_publish_providers(
    apps: list[dict[str, Any]],
    configured_resolver: Any = None,
) -> list[dict[str, Any]]:
    """Aggregate **enabled** apps that declare a publishProvider (design §1.3, Route B).

    Pure and testable — pass ``configured_resolver(app_name, pp_dict) -> bool`` to avoid
    touching the filesystem in tests. Each returned provider carries a ``configured``
    flag so the artifact page can render the publish action when configured or a
    "set it up" link otherwise. Built-in providers (e.g. Artifactory) are registered
    on the frontend; this function contributes only the app-declared ones.

    Endpoint allowlist (§9.3 security): app-declared provider endpoints MUST match
    ``/api/apps/<that-app>/`` — an app cannot declare an endpoint that routes to
    another app's namespace or to a core API. Non-conforming endpoints are dropped
    with a warning log.
    """
    resolver = configured_resolver or _provider_is_configured
    providers: list[dict[str, Any]] = []
    for app in apps:
        if not app.get("enabled"):
            continue
        manifest = app.get("manifest") or {}
        pp = manifest.get("publishProvider") or {}
        if not isinstance(pp, dict) or not pp.get("id") or not pp.get("endpoint"):
            continue
        app_name = str(app.get("name", ""))
        endpoint = str(pp["endpoint"])
        # Endpoint allowlist: must route within the app's own namespace.
        # Normalize BEFORE checking to prevent dot-segment traversal
        # (e.g. "/api/apps/foo/../../shutdown" bypassing prefix check).
        decoded_endpoint = urllib.parse.unquote(endpoint)
        normalized_endpoint = posixpath.normpath(decoded_endpoint)
        allowed_prefix = f"/api/apps/{app_name}/"
        if (
            ".." in decoded_endpoint
            or normalized_endpoint != decoded_endpoint.rstrip("/")
            # Boundary-safe prefix check: appending "/" prevents a sibling-app
            # collision ("/api/apps/foobar/x" passing app "foo"'s allowlist).
            or not (normalized_endpoint + "/").startswith(allowed_prefix)
        ):
            logger.warning(
                "publish provider for app %r declares non-conforming endpoint %r "
                "(must start with %r, no traversal) — dropping",
                app_name, endpoint, allowed_prefix,
            )
            continue
        providers.append({
            "id": str(pp["id"]),
            "label": str(pp.get("label", pp["id"])),
            "icon": str(pp.get("icon", "")),
            "endpoint": endpoint,
            "kinds": [str(k) for k in pp.get("kinds", []) if k],
            "setupRoute": str(pp.get("setupRoute", "")),
            "app": app_name,
            "origin": "app",
            "configured": bool(resolver(app_name, pp)),
        })
    return providers


async def handle_publish_providers(request: web.Request) -> web.Response:
    """GET /api/publish-providers — publish destinations (app-declared + core deploy).

    Returns enabled apps' publish providers plus the core deploy provider (folded
    from the former deploy_web app), each with a ``configured`` flag. Built-in
    providers (Artifactory) are registered frontend-side and are not returned here.
    """
    providers = collect_publish_providers(list_apps())
    # Core deploy provider (always present, regardless of any app install state)
    try:
        from kiro_crew.deploy import profiles as _deploy_profiles

        # Align with deploy/handlers.py: registry reads go through to_thread.
        reg = await asyncio.to_thread(_deploy_profiles.load_registry)
        configured = bool(reg["profiles"])
    except Exception:
        configured = False
    providers.append({
        "id": "deploy-web-aws",
        "label": "Publish to public web (your AWS)",
        "icon": "Globe",
        "endpoint": "/api/deploy/deploy",
        "kinds": ["widget", "html", "markdown"],
        "setupRoute": "/artifacts/deploy",
        "app": "",
        "origin": "core",
        "configured": configured,
    })
    return web.json_response({"providers": providers})
