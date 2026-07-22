"""Reverse proxy to app backends + the shared app-secret cache.

Extracted from ``routes.py`` (LOC split). This module is the SINGLE owner of the
app-secret cache (``_app_secret_cache`` / ``_get_app_secret`` /
``invalidate_app_secret_cache``); ``crud`` imports ``invalidate_app_secret_cache``
from here (one-way edge crud -> proxy) so there is no crud<->proxy cycle. Symbols
are re-exported from ``routes``.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import logging
import os
import time

import aiohttp
from aiohttp import web

from kiro_crew.apps.backend import get_app_backend_port
from kiro_crew.apps.manager import apps_dir, get_app_manifest
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reverse proxy — app dashboard UI → app backend (same-origin, avoids CORS)
# ---------------------------------------------------------------------------

_PROXY_TIMEOUT = 30  # seconds

# App secret cache — secrets don't change after install, no need to read
# from disk on every proxied request.  Invalidated on install/uninstall.
_app_secret_cache: dict[str, str] = {}


def _get_app_secret(name: str) -> str:
    """Read the app secret, using an in-memory cache.

    Empty values are NOT cached — the secret may be provisioned after
    the first proxy attempt (e.g. install-from-source race).
    """
    cached = _app_secret_cache.get(name)
    if cached:
        return cached
    path = apps_dir() / name / ".app_secret"
    secret = path.read_text().strip() if path.is_file() else ""
    if secret:
        _app_secret_cache[name] = secret
    return secret


def invalidate_app_secret_cache(name: str) -> None:
    """Remove a cached secret (call on install/uninstall)."""
    _app_secret_cache.pop(name, None)


_PROXY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

# Strip sensitive auth headers — app backends use X-KiroCrew-Proxy HMAC, not user cookies
_PROXY_STRIP_HEADERS = _PROXY_HOP_HEADERS | frozenset({
    "cookie", "authorization",
})


def _resolve_app_backend_url(name: str) -> str | None:
    """Resolve the backend URL for an app.

    For gateway-managed apps: use the tracked backend port.
    For self-managed apps: check manifest for backend.url or mcpServers URL.
    """
    # 1. Gateway-managed backend (spawned by backend.py)
    port = get_app_backend_port(name)
    if port:
        return f"http://127.0.0.1:{port}"

    # 2. Self-managed: check manifest for explicit backend URL
    manifest = get_app_manifest(name)
    if not manifest:
        return None

    # backend.routes field contains the base URL for some apps
    if manifest.backend.entryPoint and manifest.backend.port != "auto":
        try:
            return f"http://127.0.0.1:{int(manifest.backend.port)}"
        except ValueError:
            pass

    # 3. Fallback: derive from MCP server URL (common for self-managed apps)
    # e.g. mochi-pet declares mcpServers.mochi-pet.url = "http://localhost:7778/mcp"
    # → backend is at http://127.0.0.1:7778
    for _server_name, server_cfg in manifest.mcpServers.items():
        if isinstance(server_cfg, dict):
            url = server_cfg.get("url", "")
            if url.startswith("http"):
                # Strip the path (e.g. /mcp) to get the base URL
                from urllib.parse import urlparse
                parsed = urlparse(url)
                # Normalize localhost → 127.0.0.1 to avoid IPv6 resolution
                # issues with aiohttp on macOS (::1 may not be bound).
                host = parsed.hostname or "127.0.0.1"
                if host == "localhost":
                    host = "127.0.0.1"
                # Validate loopback to prevent SSRF via manifest-declared URLs
                import ipaddress
                try:
                    if not ipaddress.ip_address(host).is_loopback:
                        logger.warning("Refusing non-loopback backend URL %s for app %s", url, name)
                        continue
                except ValueError:
                    logger.warning("Refusing non-IP backend host %r for app %s", host, name)
                    continue
                port_num = parsed.port or (443 if parsed.scheme == "https" else 80)
                # Reject self-referential URLs (gateway's own port) to prevent SSRF
                gateway_port = int(os.environ.get("KIROCREW_PORT", "5476"))
                if port_num == gateway_port:
                    logger.warning("Refusing self-referential backend URL for app %s", name)
                    continue
                return f"{parsed.scheme}://{host}:{port_num}"

    return None


async def handle_app_api_proxy(request: web.Request) -> web.StreamResponse:
    """Reverse proxy: /apps/{name}/api/{path} → app backend.

    Allows dashboard app UIs to call their own backend through the gateway
    (same-origin), avoiding CORS issues. The gateway authenticates the
    request and forwards it to the app's backend.
    """
    # (moved to top-level)

    name = request.match_info["name"]
    path = request.match_info.get("path", "")

    # Path traversal guard (input validation first)
    if ".." in path:
        return web.json_response({"error": "invalid path"}, status=400)

    # Cross-app guard (CWE-269): if the caller authenticated with an APP token
    # (``request["app"]`` set by token_auth_middleware), it may only proxy into
    # its OWN backend. Dashboard-user requests (empty app identity) are allowed
    # to any app's proxy — that's the in-dashboard app UI calling same-origin.
    # The middleware's app-scope gate already blocks this, but the proxy is a
    # trust boundary (it signs the request with the target app's secret), so we
    # re-check here rather than rely solely on upstream.
    token_app = request.get("app", "")
    if token_app and token_app != name:
        # SEL audit for the permission decision (cross-app escalation attempt),
        # matching the sibling deny paths that emit log_api_access.
        sel().log_api_access(
            caller=token_app,
            operation="app_proxy_cross_app",
            outcome="denied",
            source="app_routes",
            resources=f"/apps/{name}/{path}",
            error="app token cannot access another app's backend",
        )
        return web.json_response(
            {"error": "app token cannot access another app's backend"}, status=403
        )

    # Resolve backend URL
    backend_url = _resolve_app_backend_url(name)
    if not backend_url:
        return web.json_response(
            {"error": f"app {name!r} has no reachable backend"}, status=502,
        )

    # Build target URL — preserve the `/api/` prefix from the route so the
    # backend sees its own `/api/...` routes without needing any path-
    # rewriting middleware.  The route captures `path` after `/api/`, so we
    # explicitly re-add `/api/` here.
    target = f"{backend_url}/api/{path}"
    if request.query_string:
        target += f"?{request.query_string}"

    # Forward headers (strip hop-by-hop, inject proxy auth)
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() not in _PROXY_STRIP_HEADERS and key.lower() != "host":
            headers[key] = value

    # Read request body first so the HMAC can bind it (integrity: prevents
    # a MITM/compromised path from swapping the body under a valid signature).
    body = await request.read() if request.can_read_body else None

    # Sign the proxy request with the app's secret so the backend can
    # verify it came from the gateway. Works on loopback and remote.
    # Header format: X-KiroCrew-Proxy: <timestamp>:<hmac-sha256>
    # The HMAC is computed over "timestamp:method:path[?query]:sha256(body)"
    # using the app secret as key. Backend verifies by recomputing with its
    # copy of the secret and checking the timestamp is recent (±60s).
    try:
        secret = _get_app_secret(name)
        if not secret:
            return web.json_response(
                {"error": f"app {name!r} has no secret — cannot authenticate proxy request"},
                status=502,
            )
        ts = str(int(time.time()))
        body_hash = hashlib.sha256(body or b"").hexdigest()
        msg = f"{ts}:{request.method}:/api/{path}"
        if request.query_string:
            msg += f"?{request.query_string}"
        msg += f":{body_hash}"
        sig = _hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        headers["X-KiroCrew-Proxy"] = f"{ts}:{sig}"
    except OSError as exc:
        logger.warning("Failed to read app secret for %s: %s", name, exc)
        return web.json_response(
            {"error": "proxy auth failed: cannot read app secret"}, status=502,
        )

    try:
        timeout = aiohttp.ClientTimeout(total=_PROXY_TIMEOUT)
        session = request.app.get("_proxy_session")
        owns_session = session is None or session.closed
        if owns_session:
            session = aiohttp.ClientSession()
        try:
            async with session.request(
                method=request.method,
                url=target,
                headers=headers,
                data=body,
                timeout=timeout,
                allow_redirects=False,
            ) as upstream:
                # Stream response back
                resp = web.StreamResponse(
                    status=upstream.status,
                    headers={
                        k: v for k, v in upstream.headers.items()
                        if k.lower() not in _PROXY_HOP_HEADERS
                    },
                )
                await resp.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        finally:
            if owns_session:
                await session.close()
    except aiohttp.ClientError as exc:
        logger.warning("Proxy to app %s failed: %s", name, exc)
        return web.json_response(
            {"error": "backend unreachable"}, status=502,
        )
    except asyncio.TimeoutError:
        return web.json_response({"error": "backend timeout"}, status=504)
