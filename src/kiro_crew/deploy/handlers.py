"""deploy-web backend — config + deploy/recall/destroy/list endpoints.

Mechanism: deterministic Python builtin module (design §8). HTTP endpoints (called
by the Artifact Deploy UI page) drive the ``engine`` which shells to the ``aws`` CLI with
``--profile`` (never boto3). The deploy mechanics are NOT LLM tools.

Approval model (§9.3): publishing creates a **public** URL and destroy/recall mutate
public infra, so each is a two-call **confirm-gate** — the first call returns a preview
that echoes exactly what will happen (resources, scan findings, public nature); the
client must re-call with ``confirm`` to proceed. Pre-publish scan (§4.1/Q4) blocks
publishing on any secret/internal-data finding unless the client passes
``override_scan`` (explicit "publish anyway"). These are plain HTTP endpoints, not
registered tools, so they can never appear in heartbeat/cron tool safe-sets.

Module layout (LOC split): this module was split into deploy submodules that keep
the same import surface. It retains ``register_routes`` and the core
deploy/recall/destroy/list handler adapters, and re-exports the moved symbols so
external callers (and the router) are unchanged:

- ``redaction``        — response redaction + SEL audit leaves
- ``staging``          — local_dir validation, staging, content scan leaves
- ``config``           — data-dir paths, legacy config shim, profile resolution
- ``internal_guard``   — restricted-session / internal-secret guards + JSON body
- ``core``             — _do_deploy / _do_recall / _do_destroy / _do_list
- ``teardown``         — reaper check, manifest expiry, _handle_teardown
- ``handlers_profiles``— config / profiles / verify / iam-policy / pricing handlers
- ``handlers_pending`` — pending-confirmation handlers
"""
from __future__ import annotations

# stdlib / third-party names kept in this module's namespace for import-surface
# parity (external code historically did ``from ...handlers import X`` / patched
# ``handlers.X``).
import asyncio  # noqa: F401
import json  # noqa: F401
import logging
import os  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import tempfile  # noqa: F401
from datetime import datetime, timedelta, timezone  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any

from aiohttp import web

from kiro_crew.config.paths import config_dir  # noqa: F401
from kiro_crew.deploy import engine  # noqa: F401
from kiro_crew.deploy import iam as iam_mod  # noqa: F401
from kiro_crew.deploy import pricing as pricing_mod  # noqa: F401
from kiro_crew.deploy import profiles as profiles_mod  # noqa: F401
from kiro_crew.deploy.render import render_standalone  # noqa: F401
from kiro_crew.deploy.scan import (  # noqa: F401
    Finding,
    is_credential_finding,
    scan_content,
    summarize,
)
from kiro_crew.security import (  # noqa: F401
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel  # noqa: F401
from kiro_crew.validation import (  # noqa: F401
    FieldSpec,
    ValidationError,
    validate_field,
)

try:
    from kiro_crew.artifacts import (  # noqa: F401
        ArtifactError,
        ArtifactNotFoundError,
        ArtifactValidationError,
        get_default_store,
    )
    _HAS_ARTIFACTS = True
except ImportError:  # pragma: no cover - defensive
    _HAS_ARTIFACTS = False

# --- re-exports from the split submodules (import-surface parity) -----------
from kiro_crew.deploy.config import (  # noqa: F401
    CONFIG_PATH,
    DATA_DIR,
    DEFAULT_REGION,
    _ProfileResolveError,
    _SITE_ID_MAX,
    _data_dir,
    _load_config,
    _reaper_remediation,
    _resolve_profile,
    _safe_site_id,
    _save_config,
)
from kiro_crew.deploy.core import (
    _do_deploy,
    _do_destroy,
    _do_list,
    _do_recall,
)
from kiro_crew.deploy.handlers_pending import (  # noqa: F401
    _handle_pending_confirm,
    _handle_pending_dismiss,
    _handle_pending_list,
)
from kiro_crew.deploy.handlers_profiles import (  # noqa: F401
    _handle_get_config,
    _handle_iam_policy,
    _handle_pricing,
    _handle_profiles_delete,
    _handle_profiles_get,
    _handle_profiles_post,
    _handle_profiles_put,
    _handle_put_config,
    _handle_verify,
)
from kiro_crew.deploy.internal_guard import (
    _INTERNAL_ALLOWED_HANDLERS,
    _INTERNAL_DENIED_ATTR,
    _deny_restricted,  # noqa: F401
    _internal_denied,
    _is_internal_secret_request,  # noqa: F401
    _json_body,
    _strip_confirm_for_internal,
)
from kiro_crew.deploy.redaction import (  # noqa: F401
    _audit,
    _redact_pending_entries,
    _redact_profile_fields,
    _redact_text,
    _safe_err,
    _sanitize_response,
)
from kiro_crew.deploy.staging import (  # noqa: F401
    _ARTIFACT_SLUG_RE,
    _ARTIFACT_SLUG_SPEC,
    _LOCAL_DIR_RE,
    _LOCAL_DIR_SPEC,
    _PROFILE_RE,
    _PROFILE_SPEC,
    _REGION_SPEC,
    _SCAN_SIZE_LIMIT,
    _allowed_local_roots,
    _compute_content_digest,
    _compute_tree_size_global,
    _dir_contains_sensitive,
    _is_within,
    _safe_resolve,
    _scan_tree,
    _stage_artifact_html,
    _stage_tree_safe,
    _staging_root,
)
from kiro_crew.deploy.teardown import (  # noqa: F401
    _check_reaper_installed,
    _expire_manifest_best_effort,
    _handle_teardown,
)

logger = logging.getLogger(__name__)


# --- aiohttp adapters (kept in the shim; the router maps these) -------------

async def _handle_deploy(request: web.Request) -> web.Response:
    denied = _deny_restricted(request, "deploy")
    if denied:
        return denied
    params = _strip_confirm_for_internal(request, await _json_body(request))
    status, payload = await _do_deploy(params)
    return web.json_response(_sanitize_response(payload), status=status)


@_internal_denied
async def _handle_recall(request: web.Request) -> web.Response:
    denied = _deny_restricted(request, "recall")
    if denied:
        return denied
    params = await _json_body(request)
    status, payload = await _do_recall(params)
    return web.json_response(_sanitize_response(payload), status=status)


@_internal_denied
async def _handle_destroy(request: web.Request) -> web.Response:
    denied = _deny_restricted(request, "destroy")
    if denied:
        return denied
    params = await _json_body(request)
    status, payload = await _do_destroy(params)
    return web.json_response(_sanitize_response(payload), status=status)


async def _handle_list(_request: web.Request) -> web.Response:
    status, payload = await _do_list()
    return web.json_response(payload, status=status)


def register_routes(app: web.Application) -> None:
    """Mount deploy routes under /api/deploy/* (core module)."""
    r = app.router
    r.add_get("/api/deploy/config", _handle_get_config)
    r.add_put("/api/deploy/config", _handle_put_config)
    r.add_get("/api/deploy/profiles", _handle_profiles_get)
    r.add_post("/api/deploy/profiles", _handle_profiles_post)
    r.add_put("/api/deploy/profiles/{name}", _handle_profiles_put)
    r.add_delete("/api/deploy/profiles/{name}", _handle_profiles_delete)
    r.add_get("/api/deploy/iam-policy", _handle_iam_policy)
    r.add_post("/api/deploy/verify", _handle_verify)
    r.add_get("/api/deploy/pricing", _handle_pricing)
    r.add_post("/api/deploy/deploy", _handle_deploy)
    r.add_post("/api/deploy/recall", _handle_recall)
    r.add_post("/api/deploy/destroy", _handle_destroy)
    r.add_get("/api/deploy/list", _handle_list)
    r.add_post("/api/deploy/teardown/{slug}", _handle_teardown)
    # ── Pending confirmations (F6) ──
    r.add_get("/api/deploy/pending", _handle_pending_list)
    r.add_post("/api/deploy/pending/{id}/confirm", _handle_pending_confirm)
    r.add_post("/api/deploy/pending/{id}/dismiss", _handle_pending_dismiss)

    # ── F1 registration-time assertion: every handler must be in the allowlist
    # or carry the @_internal_denied decorator. A new handler that forgets both
    # will crash at startup, not silently grant MCP callers access.
    _REGISTERED_HANDLERS = {
        "deploy": _handle_deploy,
        "list": _handle_list,
        "config_get": _handle_get_config,
        "config_put": _handle_put_config,
        "profiles_get": _handle_profiles_get,
        "profiles_post": _handle_profiles_post,
        "profiles_put": _handle_profiles_put,
        "profiles_delete": _handle_profiles_delete,
        "iam_policy": _handle_iam_policy,
        "verify": _handle_verify,
        "recall": _handle_recall,
        "destroy": _handle_destroy,
        "teardown": _handle_teardown,
        "pending_list": _handle_pending_list,
        "pending_confirm": _handle_pending_confirm,
        "pending_dismiss": _handle_pending_dismiss,
        "pricing": _handle_pricing,
    }
    for op_name, handler in _REGISTERED_HANDLERS.items():
        if op_name in _INTERNAL_ALLOWED_HANDLERS:
            continue  # allowed for internal-secret callers
        if not getattr(handler, _INTERNAL_DENIED_ATTR, False):
            raise AssertionError(
                f"deploy handler '{op_name}' is neither in _INTERNAL_ALLOWED_HANDLERS "
                f"nor decorated with @_internal_denied — internal-secret callers would "
                f"have unrestricted access. Add the decorator or add the operation to "
                f"the allowlist."
            )

    # ── Deprecated compat aliases (old /api/apps/deploy-web/* surface) ──────────
    # Exact-match dict for static endpoints — Location values are literals,
    # no user data flows into the header (CodeQL URL redirection F7).
    _COMPAT_STATIC_MAP: dict[str, str] = {
        "deploy": "/api/deploy/deploy",
        "recall": "/api/deploy/recall",
        "destroy": "/api/deploy/destroy",
        "sites": "/api/deploy/list",
        "config": "/api/deploy/config",
        "manifest": "/api/deploy/config",
        "iam-policy": "/api/deploy/iam-policy",
        "verify": "/api/deploy/verify",
    }

    async def _compat_redirect(request: web.Request) -> web.Response:
        tail = request.match_info.get("tail", "")
        # Static endpoint: redirect with literal Location from the dict.
        if tail in _COMPAT_STATIC_MAP:
            raise web.HTTPTemporaryRedirect(location=_COMPAT_STATIC_MAP[tail])
        # teardown/<slug>: user data in the slug — no redirect (no Location header).
        if tail.startswith("teardown/"):
            return web.json_response(
                {"error": "moved", "use": f"/api/deploy/{tail}"},
                status=404,
            )
        # Everything else: plain 404.
        raise web.HTTPNotFound()

    r.add_route("*", "/api/apps/deploy-web/{tail:.*}", _compat_redirect)


# Names re-exported for import-surface parity with the pre-split module. Listing
# them in ``__all__`` documents the surface and marks the re-import-only names as
# used (pyflakes/F401).
__all__ = [
    # stdlib / third-party surface
    "asyncio", "json", "logging", "os", "re", "shutil", "tempfile",
    "datetime", "timedelta", "timezone", "Path", "Any", "web",
    "config_dir", "engine", "iam_mod", "pricing_mod", "profiles_mod",
    "render_standalone", "Finding", "is_credential_finding", "scan_content",
    "summarize", "is_sensitive_path", "redact_credentials",
    "redact_exfiltration_urls", "sel", "FieldSpec", "ValidationError",
    "validate_field", "ArtifactError", "ArtifactNotFoundError",
    "ArtifactValidationError", "get_default_store", "_HAS_ARTIFACTS",
    # redaction
    "_redact_text", "_sanitize_response", "_redact_profile_fields",
    "_redact_pending_entries", "_audit", "_safe_err",
    # config
    "_data_dir", "DATA_DIR", "CONFIG_PATH", "DEFAULT_REGION", "_SITE_ID_MAX",
    "_reaper_remediation", "_load_config", "_save_config", "_ProfileResolveError",
    "_resolve_profile", "_safe_site_id",
    # staging
    "_LOCAL_DIR_RE", "_LOCAL_DIR_SPEC", "_PROFILE_RE", "_PROFILE_SPEC",
    "_REGION_SPEC", "_ARTIFACT_SLUG_RE", "_ARTIFACT_SLUG_SPEC", "_SCAN_SIZE_LIMIT",
    "_safe_resolve", "_allowed_local_roots", "_staging_root", "_stage_tree_safe",
    "_stage_artifact_html", "_dir_contains_sensitive", "_scan_tree",
    "_compute_content_digest", "_compute_tree_size_global", "_is_within",
    # internal guard
    "_deny_restricted", "_is_internal_secret_request", "_INTERNAL_ALLOWED_HANDLERS",
    "_INTERNAL_DENIED_ATTR", "_internal_denied", "_strip_confirm_for_internal",
    "_json_body",
    # core
    "_do_deploy", "_do_recall", "_do_destroy", "_do_list",
    # profile / config / verify / iam / pricing handlers
    "_handle_get_config", "_handle_put_config", "_handle_iam_policy",
    "_handle_verify", "_handle_pricing", "_handle_profiles_get",
    "_handle_profiles_post", "_handle_profiles_put", "_handle_profiles_delete",
    # teardown
    "_check_reaper_installed", "_expire_manifest_best_effort", "_handle_teardown",
    # pending
    "_handle_pending_list", "_handle_pending_confirm", "_handle_pending_dismiss",
    # local adapters + router
    "_handle_deploy", "_handle_recall", "_handle_destroy", "_handle_list",
    "register_routes", "logger",
]
