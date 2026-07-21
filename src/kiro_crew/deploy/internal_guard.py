"""Shared request/auth guards for the deploy-web handlers.

These plumbing helpers are used by handlers spread across several submodules
(core deploy handlers in ``handlers``, profile handlers in
``handlers_profiles``, pending handlers in ``handlers_pending``, teardown in
``teardown``). They live here — not in ``handlers`` — so every handler module
can import them without creating an import cycle with the ``handlers`` shim
(which imports all handler modules to build ``register_routes``).

Contains: the restricted-session deny check, the internal-secret detection +
default-deny decorator + its allowlist/attr, the confirm/override stripper, and
the JSON body parser. ``handlers`` re-exports all of these for API/back-compat.
"""
from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from kiro_crew.deploy.redaction import _audit


# --- restricted-session guard (consistent across all mutating endpoints) -----

def _deny_restricted(request: "web.Request", operation: str) -> "web.Response | None":
    """Return a 403 Response if the request comes from a restricted session.

    Applies to ALL mutating deploy endpoints: config PUT, deploy, recall, destroy,
    teardown, profiles POST/PUT/DELETE, and verify (back-fills registry = write).
    Read-only handlers (GET config, list, iam-policy, profiles GET) are exempt.
    Returns None when access is allowed.

    Fail-closed: if the app or state cannot be resolved, access is DENIED.
    Test fixtures must install a proper state on the request app (see
    test_deploy_web_handlers.py _NonRestrictedReq for the canonical pattern).
    """
    from kiro_crew.dashboard.handlers._shared import _is_restricted_session

    app = getattr(request, "app", None)
    if app is None:
        _audit(operation, "", "denied", error="no app context")
        return web.json_response(
            {"error": "restricted session cannot perform this operation"}, status=403)
    state = app.get("state")
    if state is None:
        _audit(operation, "", "denied", error="missing dashboard state")
        return web.json_response(
            {"error": "restricted session cannot perform this operation"}, status=403)
    if _is_restricted_session(state, request):
        _audit(operation, "", "denied", error="restricted session")
        return web.json_response(
            {"error": "restricted session cannot perform this operation"}, status=403)
    return None


def _is_internal_secret_request(request: web.Request) -> bool:
    """True when the request was authenticated via X-Internal-Secret (MCP tool path).

    The server enforces preview-only semantics for internal-secret callers:
    confirm and override_scan are stripped server-side so the MCP tool can never
    bypass the two-call confirm gate or override scan findings — those actions
    require human interaction through the dashboard UI.
    """
    return "X-Internal-Secret" in request.headers


# F1: default-deny allowlist for internal-secret callers.
# Only these handler operations are reachable via MCP tool path (X-Internal-Secret).
# ANY new handler added to register_routes is DENIED unless explicitly listed here.
_INTERNAL_ALLOWED_HANDLERS: frozenset[str] = frozenset({"deploy", "list", "pricing"})

_INTERNAL_DENIED_ATTR = "_internal_denied"


def _internal_denied(func):  # type: ignore[no-untyped-def]
    """Decorator: deny requests authenticated via X-Internal-Secret.

    Applied to every /api/deploy handler that is NOT in _INTERNAL_ALLOWED_HANDLERS.
    A new handler without this decorator (and not in the allowlist) will trip the
    registration-time assertion in register_routes.
    """
    import functools

    @functools.wraps(func)
    async def _wrapper(request: web.Request) -> web.Response:
        if _is_internal_secret_request(request):
            op = getattr(func, "__name__", "unknown").removeprefix("_handle_")
            _audit(op, "", "denied", error="internal-secret caller — handler not in allowlist")
            return web.json_response(
                {"error": "this endpoint is not available to internal-secret callers"},
                status=403,
            )
        return await func(request)

    setattr(_wrapper, _INTERNAL_DENIED_ATTR, True)
    return _wrapper


def _strip_confirm_for_internal(request: web.Request, params: dict[str, Any]) -> dict[str, Any]:
    """Strip confirm/override_scan from params when request is internal-secret authenticated."""
    if _is_internal_secret_request(request):
        params.pop("confirm", None)
        params.pop("override_scan", None)
    return params


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}
