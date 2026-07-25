"""Base types for Powers registry providers.

The Powers-side twin of ``kiro_crew.mcp_providers.base`` — the same
Protocol / registry / concurrent fan-out shape, typed for Power results so
the two discovery systems stay structurally interchangeable.

A "Power" is an installable capability bundle (``POWER.md`` + optional
``mcp.json`` + optional ``steering/*.md``). Providers surface catalog
entries; the dashboard handler owns install, trust, and redaction.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Per-provider budget for one fan-out search. A slow provider is dropped
# (with a warning) rather than stalling the aggregate search. Module-level so
# tests can patch it instead of waiting out the production budget. Mirrors
# ``mcp_providers.base._SEARCH_TIMEOUT_SECS``.
_SEARCH_TIMEOUT_SECS = 10.0


# Wire-read granularity for `read_capped`. Independent of any byte cap: the cap
# bounds the total, this bounds one await.
_READ_CHUNK_BYTES = 64 * 1024


class ProviderUnavailableError(Exception):
    """The provider's upstream (GitHub API, marketplace HTML) could not be reached.

    Raised on transport-level failures so handlers can map the condition to
    HTTP 503 ("provider unavailable") — distinct from a normal "not found"
    (``None`` / empty) result.
    """


async def read_capped(content: Any, limit: int) -> bytes:
    """Read up to *limit* bytes from an aiohttp response stream.

    ``StreamReader.read(n)`` returns *up to* n bytes — whatever is buffered when
    it wakes — and does **not** loop to fill n. A single ``read(cap + 1)`` is
    therefore a silent truncation on any body larger than one wire chunk, which
    is indistinguishable downstream from a genuinely short body: measured
    against the live upstreams, one call returned 57 KiB of a 649 KiB
    marketplace page and 9.8 KiB of a 25.6 KiB GitHub JSON document. The
    marketplace scrape then found zero cards and the official provider failed
    ``json.loads`` — both reported as "provider unavailable" rather than as the
    read bug they were.

    Callers pass ``limit = cap + 1`` and keep their own ``len(body) > cap``
    policy, so overflow handling (raise vs. truncate) stays with the caller.
    """
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = await content.read(min(_READ_CHUNK_BYTES, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


@dataclass(frozen=True)
class PowersSearchResult:
    """A single Power result from a provider search."""

    id: str
    """Provider-specific identifier — for the official provider this is the
    power slug (lowercase kebab-case); for the marketplace it is the launcher
    ``name=`` slug."""

    display_name: str
    """Human-readable name for UI cards."""

    description: str
    """Short description (may be empty when only the listing is known)."""

    author: str | None
    """Publisher, when known."""

    category: str
    """Marketplace category label ("" when unknown, e.g. official listing)."""

    scope: str
    """One of 'official' | 'aws' | 'community'."""

    github_url: str
    """Canonical GitHub tree URL for the power's source directory."""

    provider: str
    """Which provider returned this result (e.g. 'official', 'marketplace')."""

    keywords: list[str] = field(default_factory=list)
    """POWER.md keywords ([] when not yet known from a listing)."""

    icon_url: str = ""
    """HTTPS URL of the publisher icon shown on the card ("" when unknown).

    Third-party value from a scraped page, so it is host-validated at the
    provider before it ever reaches this field — see
    ``marketplace._icon_for``. Consumers may render it in an ``img`` tag, which
    is why an unvalidated string here would be a real exposure rather than a
    cosmetic one.
    """


@dataclass(frozen=True)
class PowersDetail:
    """Full detail for a single Power (detail panel + install source)."""

    id: str
    display_name: str
    description: str
    author: str | None
    category: str
    scope: str
    github_url: str
    provider: str
    keywords: list[str] = field(default_factory=list)
    icon_url: str = ""
    """HTTPS publisher-icon URL, host-validated at the provider (see
    ``marketplace._icon_for``) because it is rendered as an ``img`` source."""
    readme: str = ""
    """The POWER.md body (frontmatter stripped) — may be truncated by the caller."""
    has_mcp: bool = False
    """True when the bundle ships an ``mcp.json`` (a "Guided MCP Power")."""
    mcp_servers: list[str] = field(default_factory=list)
    """Server names declared in ``mcp.json`` ([] for a Knowledge Base Power)."""
    steering_files: list[str] = field(default_factory=list)
    """Relative paths of ``steering/*.md`` docs in the bundle."""


def _fill_blank_facets(
    kept: PowersSearchResult, dupe: PowersSearchResult
) -> PowersSearchResult:
    """Return *kept* with its empty display facets filled from *dupe*.

    Only blanks are filled, so the winning provider keeps ownership of every
    field it actually reported. ``scope`` is deliberately NOT merged: the two
    providers genuinely disagree — the official provider reports ``official``
    because the Power lives in the official monorepo, the marketplace reports
    the authorship facet (``aws`` / ``community``) — and neither is a blank
    standing in for the other. Overriding one with the other would silently
    change which scope chip a Power answers to; the divergence is recorded in
    ``docs/system-specs/modules/powers.md`` instead.
    """
    changed: dict[str, object] = {}
    if not kept.description and dupe.description:
        changed["description"] = dupe.description
    if not kept.author and dupe.author:
        changed["author"] = dupe.author
    if not kept.category and dupe.category:
        changed["category"] = dupe.category
    if not kept.keywords and dupe.keywords:
        changed["keywords"] = list(dupe.keywords)
    if not kept.icon_url and dupe.icon_url:
        changed["icon_url"] = dupe.icon_url
    if not changed:
        return kept
    return dataclasses.replace(kept, **changed)  # type: ignore[arg-type]


@runtime_checkable
class PowersProvider(Protocol):
    """Protocol for Powers registry providers.

    Each provider can search its catalog and fetch a full detail for a
    preview. Providers are async to allow concurrent fan-out.
    """

    @property
    def name(self) -> str:
        """Short provider identifier (e.g. 'official', 'marketplace')."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable provider name for UI badges."""
        ...

    async def search(self, query: str, *, limit: int = 20) -> list[PowersSearchResult]:
        """Search the provider's catalog for up to *limit* matches."""
        ...

    async def fetch_detail(self, power_id: str) -> PowersDetail | None:
        """Fetch full detail for a power, or None when it does not exist."""
        ...

    def is_available(self) -> bool:
        """Return True if this provider is configured and ready to use."""
        ...


class PowersProviderRegistry:
    """Registry of Powers providers for fan-out search.

    Collects enabled providers and searches them concurrently, isolating any
    single provider's failure so the others still serve.
    """

    def __init__(self) -> None:
        self.last_failed_providers: list[str] = []
        self._providers: dict[str, PowersProvider] = {}

    def register(self, provider: PowersProvider) -> None:
        """Add a provider to the registry."""
        self._providers[provider.name] = provider

    def get(self, name: str) -> PowersProvider | None:
        """Get a provider by name."""
        return self._providers.get(name)

    @property
    def available_providers(self) -> list[PowersProvider]:
        """Return all providers that report as available."""
        return [p for p in self._providers.values() if p.is_available()]

    @property
    def provider_names(self) -> list[str]:
        """Return names of all registered (not necessarily available) providers."""
        return list(self._providers.keys())

    async def search(
        self,
        query: str,
        *,
        provider: str | None = None,
        limit: int = 20,
    ) -> list[PowersSearchResult]:
        """Fan-out search across all available providers (or a specific one).

        Results are merged in provider order. Each provider's failures are
        caught and logged — a single provider timeout or exception does not
        break the entire search.
        """
        if provider:
            p = self._providers.get(provider)
            if p is None or not p.is_available():
                self.last_failed_providers = [provider] if p is not None else []
                return []
            results, ok = await self._search_one(p, query, limit)
            self.last_failed_providers = [] if ok else [p.name]
            return results

        providers = self.available_providers
        if not providers:
            return []

        results_per_provider = await asyncio.gather(
            *[self._search_one(p, query, limit) for p in providers]
        )
        merged: list[PowersSearchResult] = []
        # Record which providers failed THIS fan-out. Returning [] for a failure
        # made an outage indistinguishable from "no powers exist": `stale` was
        # computed from is_available() alone, and the official provider always
        # reports available, so a GitHub timeout rendered an empty, non-stale
        # registry and the UI claimed the catalog was empty.
        self.last_failed_providers = [
            p.name for p, (results, ok) in zip(providers, results_per_provider) if not ok
        ]
        # Deduplicate by canonical repository URL BEFORE applying the limit. The
        # official monorepo and the marketplace overlap, so the same Power arrived
        # twice: the user saw duplicate cards AND the duplicate consumed a slot,
        # pushing a distinct Power off the end of the page. Provider order is
        # preserved, so the first provider to list a repo owns the card.
        seen: dict[str, int] = {}
        for results, _ok in results_per_provider:
            for item in results:
                # Direct attribute access, NOT getattr with a default: the field is
                # `github_url`, and an earlier revision of this dedup read a
                # non-existent `githubUrl`, so every key was "" and the dedup was
                # silently inert. A rename must fail loudly here, not degrade.
                key = (item.github_url or "").strip().rstrip("/").lower()
                if key:
                    if key in seen:
                        # The losing duplicate is not worthless: providers observe
                        # different facets of the same repository. The official
                        # provider lists a GitHub directory, so it can only report
                        # id + url (`description`/`author`/`category` are
                        # structurally empty), while the marketplace card carries an
                        # author line and category. Dropping the duplicate outright
                        # therefore *lost* metadata for every overlapping Power —
                        # 26 of 82 at time of writing — and the cards rendered
                        # authorless. Fill blanks only; a field the winner already
                        # populated is never overwritten.
                        merged[seen[key]] = _fill_blank_facets(merged[seen[key]], item)
                        continue
                    seen[key] = len(merged)
                merged.append(item)
        return merged[:limit]

    @staticmethod
    async def _search_one(
        p: PowersProvider, query: str, limit: int
    ) -> tuple[list[PowersSearchResult], bool]:
        """Return (results, ok). ``ok`` is False when the provider errored.

        The boolean matters: an empty list from a failure and an empty list from
        a genuinely empty catalog must not look the same to the caller.
        """
        try:
            return (
                await asyncio.wait_for(
                    p.search(query, limit=limit), timeout=_SEARCH_TIMEOUT_SECS
                ),
                True,
            )
        except asyncio.TimeoutError:
            logger.warning("Powers provider %s timed out for query %r", p.name, query)
            return [], False
        except Exception:
            logger.warning(
                "Powers provider %s failed for query %r", p.name, query, exc_info=True
            )
            return [], False
