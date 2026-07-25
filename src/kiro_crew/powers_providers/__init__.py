"""Multi-provider Powers registry discovery and fetch.

The Powers-side twin of :mod:`kiro_crew.mcp_providers`. Each provider (the
official ``kirodotdev/powers`` monorepo, the kiro.dev marketplace listing)
implements the :class:`PowersProvider` protocol and registers itself in a
:class:`PowersProviderRegistry` for concurrent, failure-isolated fan-out
search. :func:`fetch_power_bundle` securely downloads a power's files for
install.

The :func:`list_registry` / :func:`fetch_registry_detail` coroutines are the
handler-facing façade — they build the process-wide provider registry, apply
the ``category`` / ``scope`` filters, and shape results into the fixed
``/api/powers/registry`` JSON contract (camelCase keys, external strings
redacted). The dashboard handler imports only these two plus
:func:`fetch_power_bundle`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import powers_dir
from kiro_crew.powers_providers.base import (
    PowersDetail,
    PowersProvider,
    PowersProviderRegistry,
    PowersSearchResult,
    ProviderUnavailableError,
)
from kiro_crew.powers_providers.fetch import (
    BundleSecurityError,
    fetch_power_bundle,
    resolve_power_ref,
)
from kiro_crew.powers_providers.marketplace import MarketplacePowersProvider
from kiro_crew.powers_providers.official import OfficialPowersProvider
from kiro_crew.powers_providers.redact import redact_external

logger = logging.getLogger(__name__)

__all__ = [
    "BundleSecurityError",
    "MarketplacePowersProvider",
    "OfficialPowersProvider",
    "PowersDetail",
    "PowersProvider",
    "PowersProviderRegistry",
    "PowersSearchResult",
    "ProviderUnavailableError",
    "fetch_power_bundle",
    "resolve_power_ref",
    "fetch_registry_detail",
    "list_registry",
]

# Budget for one detail fetch (single upstream document, but still bounded) —
# mirrors ``mcp_discover._DETAIL_TIMEOUT_SECS``.
_DETAIL_TIMEOUT_SECS = 15.0

# Over-fetch cap for the internal search so client-side category/scope
# filtering has enough candidates before the caller's limit is applied.
_INTERNAL_SEARCH_CAP = 200

_registry: PowersProviderRegistry | None = None


def _marketplace_cache_path() -> Path:
    """On-disk TTL cache location for the fragile marketplace scrape.

    Lives under the data home, NOT the shared temp dir. This cache is not
    display-only: ``fetch.fetch_power_bundle`` resolves a marketplace id to its
    source repository through it, so it decides *which repository gets
    installed*. A predictable path in a world-writable ``/tmp`` would let any
    local process pre-seed or overwrite it and point a familiar marketplace
    name at a repository of its choosing. Keeping it beside every other
    persisted Powers artifact confines it to the user's own data home.
    """
    return powers_dir() / ".marketplace-cache.json"


def _get_registry() -> PowersProviderRegistry:
    """Lazy-init the process-wide provider registry (official + marketplace)."""
    global _registry
    if _registry is None:
        reg = PowersProviderRegistry()
        reg.register(OfficialPowersProvider())
        reg.register(MarketplacePowersProvider(cache_path=_marketplace_cache_path()))
        _registry = reg
    return _registry


def _redact(text: str) -> str:
    """Redact external, attacker-controllable strings — MANDATORY, fail-closed.

    Delegates to the shared :func:`kiro_crew.powers_providers.redact.redact_external`
    shaper (the single mandatory redactor used by both this façade and the
    dashboard handler). It applies ``redact_credentials`` and
    ``redact_exfiltration_urls`` and NEVER falls back to identity: a redactor
    failure propagates so the response fails closed rather than silently
    surfacing raw third-party text. The previous identity fallback
    (``except Exception: return text``) is gone precisely because it disabled
    redaction on any scanner hiccup.
    """
    return redact_external(text)


def _shape(r: PowersSearchResult) -> dict[str, Any]:
    return {
        "id": _redact(r.id),
        "displayName": _redact(r.display_name),
        "description": _redact(r.description),
        "author": _redact(r.author) if r.author else None,
        "category": _redact(r.category),
        "scope": r.scope,
        "githubUrl": _redact(r.github_url),
        "keywords": [_redact(k) for k in r.keywords],
        "provider": r.provider,
        # Host-validated at the provider; redacted like every other
        # third-party string on its way out of the process.
        "iconUrl": _redact(r.icon_url),
    }


def _shape_detail(d: PowersDetail) -> dict[str, Any]:
    return {
        "id": _redact(d.id),
        "displayName": _redact(d.display_name),
        "description": _redact(d.description),
        "author": _redact(d.author) if d.author else None,
        "category": _redact(d.category),
        "scope": d.scope,
        "githubUrl": _redact(d.github_url),
        "keywords": [_redact(k) for k in d.keywords],
        "provider": d.provider,
        "readme": _redact(d.readme),
        "hasMcp": d.has_mcp,
        "mcpServers": [_redact(s) for s in d.mcp_servers],
        "steeringFiles": [_redact(s) for s in d.steering_files],
    }


async def list_registry(
    *, q: str = "", category: str = "", scope: str = "", limit: int = 100
) -> dict[str, Any]:
    """Browse the Powers registry — the ``/api/powers/registry`` body.

    Fans out across providers, applies ``category`` / ``scope`` filters, and
    returns ``{items, providers, stale}``. ``stale`` is true when any
    registered provider reports unavailable (e.g. the marketplace scrape
    degraded), signalling the listing may be incomplete.
    """
    reg = _get_registry()
    raw = await reg.search(q, limit=max(limit, _INTERNAL_SEARCH_CAP))

    cat_needle = category.strip().lower()
    scope_needle = scope.strip().lower()
    items: list[dict[str, Any]] = []
    for r in raw:
        if cat_needle and cat_needle not in r.category.lower():
            continue
        if scope_needle and r.scope.lower() != scope_needle:
            continue
        items.append(_shape(r))
        if len(items) >= limit:
            break

    providers: list[dict[str, Any]] = []
    for name in reg.provider_names:
        p = reg.get(name)
        if p is None:
            continue
        providers.append(
            {"name": name, "displayName": p.display_name, "available": p.is_available()}
        )
    # `stale` must reflect failures observed in THIS response, not just the
    # availability flag: the official provider always reports available, so a
    # GitHub timeout would otherwise render an empty, non-stale registry and the
    # UI would claim no Powers exist instead of surfacing an outage.
    failed = set(getattr(reg, "last_failed_providers", []) or [])
    for entry in providers:
        if entry["name"] in failed:
            entry["available"] = False
    stale = bool(failed) or any(not entry["available"] for entry in providers)
    # A provider can still be `available` (it returned data) yet be serving an
    # EXPIRED disk cache because its live fetch failed. That degraded fallback
    # must also mark the whole listing stale, otherwise the UI renders expired
    # data as fresh. Ask each provider whether it just served a stale cache.
    if not stale and any(_provider_served_stale(reg.get(name)) for name in reg.provider_names):
        stale = True
    return {"items": items, "providers": providers, "stale": stale}


def _provider_served_stale(provider: PowersProvider | None) -> bool:
    """Return True when *provider* reports it last served a degraded (stale) cache.

    Optional capability: providers that cache upstream data (the marketplace
    scrape) expose ``served_stale()``; providers without it (the live official
    provider) are never stale by this measure. Guarded so a provider raising
    cannot break the listing.
    """
    fn = getattr(provider, "served_stale", None)
    if not callable(fn):
        return False
    try:
        return bool(fn())
    except Exception:
        return False


async def fetch_registry_detail(
    power_id: str, *, provider: str | None = None
) -> dict[str, Any] | None:
    """Full detail for one registry power — the ``registry/detail`` payload.

    Tries the named provider, or every available provider in turn, returning
    the first non-null shaped detail. When no provider returns a detail, an
    outage/timeout in any candidate is surfaced as
    :class:`ProviderUnavailableError` (mapped to 503 by the handler) so a
    genuine "not found" (``None``) stays distinct from "the providers were
    down".
    """
    reg = _get_registry()
    if provider:
        candidates: list[PowersProvider] = []
        p = reg.get(provider)
        if p is not None and not p.is_available():
            # The caller named a REGISTERED provider that is currently down.
            # Falling through with an empty candidate list made this a 404 —
            # telling the user the power does not exist when the provider was
            # merely unavailable. A registered-but-down provider is a 503.
            raise ProviderUnavailableError(f"powers provider unavailable: {provider}")
        if p is not None:
            candidates.append(p)
    else:
        candidates = list(reg.available_providers)

    unavailable = False
    for prov in candidates:
        try:
            detail = await asyncio.wait_for(
                prov.fetch_detail(power_id), timeout=_DETAIL_TIMEOUT_SECS
            )
        except (ProviderUnavailableError, asyncio.TimeoutError):
            # A transport outage / timeout is NOT "not found": remember it so a
            # lookup where every candidate was down surfaces as an outage (503)
            # instead of being swallowed into a misleading 404.
            unavailable = True
            continue
        except Exception:
            logger.warning(
                "powers detail fetch failed for %s via %s", power_id, prov.name, exc_info=True
            )
            continue
        if detail is not None:
            return _shape_detail(detail)
    if unavailable:
        raise ProviderUnavailableError(
            f"all powers providers were unavailable for detail lookup of {power_id!r}"
        )
    return None
