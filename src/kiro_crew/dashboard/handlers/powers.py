"""Powers HTTP handlers — install / list / registry / remove.

Implements the FIXED ``/api/powers`` contract.  Registry browsing (``registry``
/ ``registry/detail``) and bundle fetching are owned by the separate
``kiro_crew.powers_providers`` package and imported LAZILY here so this module
loads even while that agent's code is still in flight — a missing provider
degrades to a clear 503 (install/detail) or an empty ``stale`` listing (registry
list) rather than an import error at gateway boot.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers.discover import _redact_external
from kiro_crew.executors import discovery_executor, maintenance_executor
from kiro_crew.powers import (
    PowerFormatError,
    PowerSourceConflict,
    PowersStore,
    is_safe_power_name,
    resolve_install_source,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


# Provider package, imported ONCE at module scope with degradation preserved.
#
# These were function-local imports, which violates the `top-level-imports` rule.
# The reason they were local was real — the package is optional, and the handlers
# degrade to an empty `stale` listing or a 503 rather than failing — but a
# module-scope `try/except` keeps exactly that behaviour: a failed import is
# caught here, `_PROVIDERS_AVAILABLE` records it, and each handler checks the flag
# instead of catching its own ImportError. Nothing is imported at request time and
# an unavailable provider package still cannot break dashboard boot.
try:
    from kiro_crew.powers_providers import (
        BundleSecurityError,
        ProviderUnavailableError,
        fetch_power_bundle,
        fetch_registry_detail,
        list_registry,
        resolve_power_ref,
    )

    _PROVIDERS_AVAILABLE = True
except Exception:  # pragma: no cover - provider package import failure
    _PROVIDERS_AVAILABLE = False

    class _NeverRaised(Exception):
        """Stands in for a provider exception type that can never be raised.

        An `except` clause naming this simply never matches, so the generic
        handler still applies — the same outcome the local-import fallback had.
        """

    BundleSecurityError = _NeverRaised  # type: ignore[assignment,misc]
    ProviderUnavailableError = _NeverRaised  # type: ignore[assignment,misc]
    fetch_power_bundle = None  # type: ignore[assignment]
    fetch_registry_detail = None  # type: ignore[assignment]
    list_registry = None  # type: ignore[assignment]
    resolve_power_ref = None  # type: ignore[assignment]

_REGISTRY_DEFAULT_LIMIT = 100
_REGISTRY_MAX_LIMIT = 500


# Mandatory, fail-closed redaction for every third-party Power string leaving
# this process (POWER.md fields, marketplace metadata). The single shared
# shaper lives in the provider package; if that package cannot be imported
# (historically it could be "in flight") we STILL redact via the core scanners
# directly — redaction is never optional and never falls back to identity, so a
# redactor error fails the response closed rather than leaking raw text.
try:
    from kiro_crew.powers_providers.redact import redact_payload as _redact_payload
except Exception:  # pragma: no cover - provider package import failure
    from kiro_crew.security import redact_credentials as _redact_credentials
    from kiro_crew.security import redact_exfiltration_urls as _redact_exfiltration_urls

    def _redact_payload(obj: Any) -> Any:
        """Fail-closed fallback: recursively redact string values in *obj*."""
        if isinstance(obj, str):
            if not obj:
                return obj
            scrubbed, _ = _redact_credentials(obj)
            scrubbed, _ = _redact_exfiltration_urls(scrubbed)
            return scrubbed
        if isinstance(obj, dict):
            return {k: _redact_payload(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact_payload(v) for v in obj]
        return obj


def _provider_exc_types() -> tuple[type[BaseException], type[BaseException]]:
    """Return ``(BundleSecurityError, ProviderUnavailableError)`` for except clauses.

    Resolved at module load; when the provider package is unavailable both are the
    same sentinel type, which no raise site can produce, so the corresponding
    ``except`` clause never matches and the generic handler still applies.
    """
    return BundleSecurityError, ProviderUnavailableError


def _store() -> PowersStore:
    """Construct a PowersStore (cheap; all state lives on disk)."""
    return PowersStore()


def _power_name(request: web.Request) -> str:
    return str(request.match_info.get("name", "")).strip()


async def api_powers(request: web.Request) -> web.Response:
    """GET /api/powers — list installed Powers.

    ``list_powers`` reads every installed record plus each Power's ``POWER.md``
    (and probes ``mcp.json``) from disk, so it is offloaded to the
    ``discovery_executor`` bulkhead pool rather than blocking the event loop.
    Every third-party string in the response is passed through the mandatory
    fail-closed redactor before it leaves the process.
    """
    loop = asyncio.get_running_loop()
    installed = await loop.run_in_executor(discovery_executor(), _store().list_powers)
    return web.json_response({"installed": _redact_payload(installed)})


async def api_powers_registry(request: web.Request) -> web.Response:
    """GET /api/powers/registry — browse the Powers registry (provider-backed).

    Delegates to ``kiro_crew.powers_providers.list_registry``.  If the provider
    package is unavailable, returns an empty, ``stale`` listing so the UI renders
    gracefully instead of erroring.
    """
    if not _PROVIDERS_AVAILABLE or list_registry is None:
        logger.info("powers_providers unavailable; returning empty registry listing")
        return web.json_response({"items": [], "providers": [], "stale": True})

    q = request.query.get("q", "").strip()
    category = request.query.get("category", "").strip()
    scope = request.query.get("scope", "").strip()
    try:
        limit = int(request.query.get("limit", _REGISTRY_DEFAULT_LIMIT))
    except ValueError:
        limit = _REGISTRY_DEFAULT_LIMIT
    limit = max(1, min(limit, _REGISTRY_MAX_LIMIT))

    try:
        result = await list_registry(q=q, category=category, scope=scope, limit=limit)
    except Exception as exc:
        logger.warning("powers registry listing failed: %s", exc)
        return web.json_response({"items": [], "providers": [], "stale": True})
    if not isinstance(result, dict):
        return web.json_response({"items": [], "providers": [], "stale": True})
    return web.json_response(result)


async def api_powers_registry_detail(request: web.Request) -> web.Response:
    """GET /api/powers/registry/detail?id=&provider= — detail for one registry Power."""
    power_id = request.query.get("id", "").strip()
    provider = request.query.get("provider", "").strip() or None
    if not power_id:
        return web.json_response({"error": "id is required"}, status=400)
    if not _PROVIDERS_AVAILABLE or fetch_registry_detail is None:
        return web.json_response(
            {"error": "powers registry provider is not available yet"}, status=503
        )
    _bundle_err, _provider_unavailable = _provider_exc_types()
    try:
        detail = await fetch_registry_detail(power_id, provider=provider)
    except _provider_unavailable:
        # Every candidate provider was down/timed out — surface the outage as
        # 503 instead of letting it be swallowed into a misleading 404.
        return web.json_response(
            {"error": "powers registry provider is unavailable"}, status=503
        )
    except Exception as exc:
        logger.warning("powers registry detail failed for %s: %s", power_id, exc)
        return web.json_response({"error": "failed to load registry detail"}, status=502)
    if not detail:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"power": detail})


async def api_powers_install(request: web.Request) -> web.Response:
    """POST /api/powers/install — install a Power from registry / github / folder.

    Body::

        {"source": {"kind": "registry"|"github"|"folder", "ref": str, "provider"?: str}}

    ``registry``/``github`` fetch a bundle via the provider package (lazy import,
    503 if unavailable); ``folder`` installs from a local directory path.  The
    Power is installed disabled + untrusted.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    # A valid-but-non-object JSON body (an array or scalar) has no ``.get``, so
    # ``body.get("source")`` below would raise AttributeError and 500. Require a
    # JSON object and return a clean 400 instead.
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    source = body.get("source")
    if not isinstance(source, dict):
        return web.json_response({"error": "source object is required"}, status=400)
    kind = str(source.get("kind", "")).strip()
    ref = str(source.get("ref", "")).strip()
    provider = str(source.get("provider", "")).strip() or None
    if kind not in ("registry", "github", "folder"):
        return web.json_response(
            {"error": "source.kind must be one of registry|github|folder"}, status=400
        )
    if not ref:
        return web.json_response({"error": "source.ref is required"}, status=400)

    store = _store()
    _bundle_err, _provider_unavailable = _provider_exc_types()

    try:
        bundle_dir: str | Path
        fetched: Path | None = None
        resolved_ref = ""
        if kind == "folder":
            # API-supplied path: vet it (resolve + sensitive-path refusal)
            # before anything reads or walks it. The store re-vets as well.
            # Offloaded because resolve(strict=True) stats the filesystem.
            bundle_dir = await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(), resolve_install_source, ref
            )
        else:
            # Registry fetch is owned by another package, imported at module
            # scope with the failure caught there; an unavailable provider still
            # degrades to a clear 503 rather than a boot error.
            if (
                not _PROVIDERS_AVAILABLE
                or fetch_power_bundle is None
                or resolve_power_ref is None
            ):
                sel().log_api_access(
                    caller="dashboard",
                    operation="power_install",
                    outcome="unavailable",
                    resources=f"{kind}:{ref}"[:128],
                )
                return web.json_response(
                    {"error": "powers registry provider is not available yet"}, status=503
                )
            # Resolve first and record the RESOLVED ref as provenance: a
            # marketplace card's id is provider-scoped, so storing the slug makes
            # the installed record disagree with the card's repository (and the
            # UI's installed-check compares repositories). Resolution is
            # idempotent for refs that are already URLs.
            resolved_ref = await resolve_power_ref(ref, provider)
            fetched = await fetch_power_bundle(resolved_ref, provider=provider)
            bundle_dir = fetched

        try:
            power = await store.install_from_dir(
                bundle_dir, source={"kind": kind, "ref": resolved_ref or ref}
            )
        finally:
            # The fetched bundle is a temp tree of up to the download cap; drop
            # it on success and failure alike so repeated installs cannot
            # accumulate abandoned directories.
            if fetched is not None:
                await asyncio.get_running_loop().run_in_executor(
                    maintenance_executor(),
                    lambda: shutil.rmtree(fetched, ignore_errors=True),
                )
    except PowerSourceConflict as exc:
        # 409, not 400: the request is well-formed and the state is the problem, so
        # the client can act on it (uninstall, then retry) rather than correcting
        # the input. Listed before PowerFormatError because both derive from
        # ValueError and the first matching arm wins.
        return web.json_response({"error": _redact_external(str(exc))}, status=409)
    except PowerFormatError as exc:
        return web.json_response({"error": _redact_external(str(exc))}, status=400)
    except _bundle_err as exc:
        # Malformed GitHub URL / unknown provider / tree-security violation is a
        # bad request, not a server fault — 400 alongside PowerFormatError.
        return web.json_response({"error": _redact_external(str(exc))}, status=400)
    except _provider_unavailable as exc:
        # Upstream (GitHub / marketplace) outage — 503, and do NOT swallow it
        # into the generic 500 handler.
        logger.info("power install upstream unavailable (%s:%s): %s", kind, ref, exc)
        return web.json_response(
            {"error": "powers registry provider is unavailable"}, status=503
        )
    except FileNotFoundError as exc:
        return web.json_response({"error": _redact_external(str(exc))}, status=400)
    except Exception as exc:
        logger.warning("power install failed (%s:%s): %s", kind, ref, exc)
        return web.json_response({"error": "install failed"}, status=500)

    return web.json_response({"power": _redact_payload(power)})


async def api_powers_delete(request: web.Request) -> web.Response:
    """DELETE /api/powers/{name} — uninstall a Power."""
    name = _power_name(request)
    if not is_safe_power_name(name):
        return web.json_response({"error": "invalid power name"}, status=400)
    try:
        removed = await _store().remove_power(name)
    except Exception as exc:
        logger.warning("power remove failed for %s: %s", name, exc)
        return web.json_response({"error": "remove failed"}, status=500)
    if not removed:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True})
