"""Routes behind the session-control MCP tools.

Strict-internal (loopback + ``X-Internal-Secret``): no browser calls these, and
they are the entry point to acting on another live conversation, so a cookie
fall-through would be a genuinely new path rather than a convenience.

The handlers do parsing and status mapping only — resolution, authorization and
the operations themselves live in ``dashboard/session_control.py`` so all three
verbs share one guard.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers._shared import _read_session_key
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


def _require_internal(request: web.Request) -> web.Response | None:
    """Refuse anything that did not present ``X-Internal-Secret``.

    These paths are on ``_STRICT_INTERNAL_API_PATHS``, but strict is not
    self-enforcing at the handler: with the header ABSENT the middleware falls
    through to cookie auth, and a ``local_only=False`` deployment reclassifies
    every strict path as "mixed". Either way a same-origin page holding only a
    dashboard cookie could reach here and — by choosing ``X-Session-Key`` —
    message, stop, or read any of the user's sessions AS one of them. The
    session key is an identity claim these routes authorize on, so it has to be
    backed by the secret rather than by whatever a browser sends.

    ``internal_auth`` is set only after a constant-time secret match, so
    requiring it closes the cookie path, the app-token path, and the
    non-loopback reclassification in one check. Returns the refusal, or ``None``
    when the caller is authentic.
    """
    if request.get("internal_auth") is True:
        return None
    sel().log_api_access(
        caller="unknown",
        operation=f"session_control.{request.path.rsplit('/', 1)[-1]}",
        outcome="denied",
        source="dashboard",
        resources=request.path,
        error="internal secret required",
    )
    return web.json_response(
        {"error": "forbidden", "code": "internal_secret_required"}, status=403
    )


def _refusal(exc: sc.SessionControlError) -> web.Response:
    """Render a :class:`SessionControlError` as its HTTP response.

    Written as an explicit branch per status rather than
    ``status=exc.status`` so the route can only ever answer with a status from
    this closed set: an unmapped value degrades to 400 instead of forwarding
    whatever integer reached it. ``code`` is the field callers match on;
    ``message`` is advisory prose.
    """
    if exc.status == 403:
        return web.json_response({"error": exc.message, "code": exc.code}, status=403)
    if exc.status == 404:
        return web.json_response({"error": exc.message, "code": exc.code}, status=404)
    if exc.status == 409:
        return web.json_response({"error": exc.message, "code": exc.code}, status=409)
    if exc.status == 429:
        return web.json_response({"error": exc.message, "code": exc.code}, status=429)
    return web.json_response({"error": exc.message, "code": exc.code}, status=400)


async def _prewarm_config() -> None:
    """Warm the config cache in a thread before the sync gate reads it.

    `authorize_target` must stay synchronous — `read_messages` is sync too — so the
    enabled check cannot await. Its `KiroCrewConfig.load()` is cached by fingerprint,
    but the FIRST call after a config edit re-reads and validates the file, and doing
    that inline would block the loop for every other session. Warming it here means
    the gate's own read is always the cached path. Best-effort: a failure is the
    gate's business, not ours, and it fails closed on its own.
    """
    try:
        await asyncio.to_thread(sc.session_control_enabled)
    except Exception:  # pragma: no cover - the gate re-reads and decides
        logger.debug("config prewarm failed; the gate will read it inline")


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise sc.SessionControlError("invalid JSON", code="invalid_json")
    if not isinstance(body, dict):
        raise sc.SessionControlError("body must be a JSON object", code="invalid_body")
    return body


def _target(body: dict) -> str:
    target = body.get("target")
    if not isinstance(target, str) or not target.strip():
        raise sc.SessionControlError("target is required", code="target_required")
    return target.strip()


async def api_session_control_send(request: web.Request) -> web.Response:
    """POST /api/session-control/send — deliver a user turn to another session."""
    refused = _require_internal(request)
    if refused is not None:
        return refused
    await _prewarm_config()
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        message = body.get("message")
        result = await sc.send_message(
            state,
            caller_session_key=_read_session_key(request),
            target=_target(body),
            message=message if isinstance(message, str) else "",
            mode=str(body.get("mode") or "steer"),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_stop(request: web.Request) -> web.Response:
    """POST /api/session-control/stop — stop another session's in-flight turn."""
    refused = _require_internal(request)
    if refused is not None:
        return refused
    await _prewarm_config()
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        result = await sc.stop_target(
            state,
            caller_session_key=_read_session_key(request),
            target=_target(body),
            force=body.get("force") is True,
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_read(request: web.Request) -> web.Response:
    """GET /api/session-control/read — read another session's transcript tail."""
    refused = _require_internal(request)
    if refused is not None:
        return refused
    await _prewarm_config()
    state: DashboardState = request.app["state"]
    try:
        target = (request.query.get("target") or "").strip()
        if not target:
            raise sc.SessionControlError("target is required", code="target_required")
        limit_raw = request.query.get("limit")
        since_raw = request.query.get("since")
        try:
            limit = int(limit_raw) if limit_raw else sc.DEFAULT_READ_MESSAGES
            since = int(since_raw) if since_raw else None
        except ValueError:
            raise sc.SessionControlError(
                "limit and since must be integers", code="invalid_pagination"
            )
        result = sc.read_messages(
            state,
            caller_session_key=_read_session_key(request),
            target=target,
            limit=limit,
            since=since,
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)
