"""Marketplace Powers provider — ``kiro.dev/powers``.

The marketplace is a **server-rendered HTML** listing (~76 powers across 11
categories, scope filters Official / AWS / Community). There is **no JSON
API** (``/api/powers`` and ``/powers.json`` both 404, and the sitemap does
not enumerate powers), so the listing must be scraped.

Robustness strategy (this provider is inherently fragile):

* Parse with the **stdlib only** — a small set of bounded regexes keyed on
  the two *verified* facts: every card carries an install launcher
  ``/launch/powers/add?name=<slug>`` and an adjacent canonical GitHub
  ``.../tree/...`` URL. Slug + github_url are extracted from that pair;
  category/scope are best-effort enrichment from the surrounding card.
* **Disk cache with a TTL** so a transient marketplace hiccup or rate limit
  does not take the feature down, and repeated searches don't re-scrape.
* On a **parse failure** (fetched but zero cards recognised) or transport
  failure with no cache, log **one** warning (never a stack trace per call),
  return empty, and mark the provider **unavailable** — so the official
  provider keeps serving.

No third-party dependency is added (verified against setup.cfg / pyproject).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
from html import unescape
from pathlib import Path
from typing import Any

import aiohttp

from kiro_crew.atomic_write import atomic_write
from kiro_crew.executors import maintenance_executor
from kiro_crew.powers_providers.base import PowersDetail, PowersSearchResult, read_capped

logger = logging.getLogger(__name__)

_MARKETPLACE_URL = "https://kiro.dev/powers/"
_DEFAULT_TTL_SECS = 3600.0
# How long a provider stays marked unavailable before it is retried.
_RECOVER_AFTER_SECS = 300.0
_HTTP_TIMEOUT_SECS = 10.0
_MAX_HTML_BYTES = 4 * 1024 * 1024
_MAX_ENTRIES = 500
_USER_AGENT = "KiroCrew/1.0 (powers-registry)"

# Verified card facts (validated against a real fetched kiro.dev/powers page).
#
# DOM order within one card is:
#   <h3>{displayName}</h3> [official badge] <span>{author}</span>
#   <a aria-label="Add {displayName} power to Kiro" href="/launch/powers/add?name={slug}">
#   ... <a href="https://github.com/{owner}/{repo}/tree/{ref}">Details</a>
#
# So a card's canonical GitHub URL is the FIRST tree URL *after* its launcher,
# and its title/author/badge sit *before* the launcher. Consecutive launchers
# are ~3.2k chars apart, so pairing is bounded by the neighbouring launchers
# rather than by a fixed character window — a fixed window silently pairs a
# card with its predecessor's repository.
_LAUNCH_RE = re.compile(r"/launch/powers/add\?name=([A-Za-z0-9][A-Za-z0-9._-]*)")
_TREE_RE = re.compile(r"https://github\.com/[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+/tree/[A-Za-z0-9._/~-]+")
# The launcher carries its own accessible label, which is the most reliable
# display-name source available (present on every card).
_ARIA_LABEL_RE = re.compile(r'aria-label="Add ([^"]{1,200}?) power to Kiro"')
_AUTHOR_RE = re.compile(r'<span class="break-words text-base">([^<]{1,120})</span>')
_OFFICIAL_RE = re.compile(r"official Kiro power", re.IGNORECASE)


# Publisher icons are served from Kiro's own asset host. The URL is scraped from
# a third-party page and ends up in an `img src`, so the host is an ALLOWLIST, not
# a coherence check: accepting whatever the page offers would let a changed or
# compromised listing point the dashboard at an arbitrary origin, turning every
# card render into a request the user did not choose to make (and a tracking
# beacon keyed to their session).
_ICON_HOST = "prod.download.desktop.kiro.dev"
_ICON_RE = re.compile(
    r'src="(https://' + re.escape(_ICON_HOST) + r'/powers/icons/[A-Za-z0-9._/-]+)"'
)


def _require_tree_url(raw: Any) -> str:
    """Return a validated tree URL or raise so the caller skips the entry.

    Skipping is the right failure mode rather than blanking the field: the URL is
    the dedup key and the install target, so an entry without a trustworthy one is
    not a usable card.
    """
    url = valid_tree_url(str(raw))
    if not url:
        raise ValueError("cached entry has a non-GitHub tree URL")
    return url


def valid_tree_url(url: str) -> str:
    """Return *url* if it is a canonical GitHub tree URL, else "".

    Applied to values read back from the disk cache, not just to freshly scraped
    ones. The scrape extracts this with ``_TREE_RE`` (anchored on
    ``https://github.com/<owner>/<repo>/tree/...``), so a cached value that does
    not match that shape did not come from a scrape this code performed — and it
    reaches an ``href`` in the dashboard, so a `javascript:` payload there is
    click-to-execute rather than cosmetic.
    """
    if not url or len(url) > 500:
        return ""
    match = _TREE_RE.fullmatch(url.strip())
    return match.group(0) if match else ""


def valid_icon_url(url: str) -> str:
    """Return *url* if it is an allowlisted icon URL, else "".

    The single chokepoint for icon origin, used both when scraping and when
    reading the disk cache back. Re-validating rather than trusting the scrape
    regex means a future loosening of the pattern cannot silently widen the
    origin, and a poisoned cache file cannot inject one.
    """
    if not url or len(url) > 300:
        return ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != _ICON_HOST:
        return ""
    if not parsed.path.startswith("/powers/icons/"):
        return ""
    return url


def _icon_for(card_html: str) -> str:
    """Return the card's publisher icon URL, or "" when absent/not allowlisted."""
    m = _ICON_RE.search(card_html)
    return valid_icon_url(unescape(m.group(1))) if m else ""


def _prettify(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()


def _scope_for(author: str, official: bool) -> str:
    """Derive the marketplace scope facet.

    The listing has no machine-readable scope attribute; the observable
    signals are an "official Kiro power" badge and the author line. AWS-authored
    powers form their own facet in the upstream UI, so they win over the badge.
    """
    if author.strip().upper() == "AWS":
        return "aws"
    return "official" if official else "community"


def parse_listing(html: str) -> list[PowersSearchResult]:
    """Extract power entries from the marketplace HTML.

    Returns one :class:`PowersSearchResult` per recognised card. An empty
    result means the markup did not match (treated as a parse failure by the
    caller). Total entries are capped by ``_MAX_ENTRIES``.

    ``category`` is always empty: the listing exposes categories only as
    aggregate filter counts, never per card, so it is not derivable here.
    Callers must treat an empty category as "unknown" rather than a value.
    """
    launchers = [(m.start(), m.end(), m.group(1)) for m in _LAUNCH_RE.finditer(html)]
    entries: list[PowersSearchResult] = []
    seen: set[str] = set()

    for idx, (start, end, slug) in enumerate(launchers):
        if len(entries) >= _MAX_ENTRIES:
            break
        if slug in seen:
            continue
        # This card owns everything up to the next launcher.
        next_start = launchers[idx + 1][0] if idx + 1 < len(launchers) else len(html)
        tree = _TREE_RE.search(html, end, next_start)
        if tree is None:
            continue
        # Title / author / badge precede the launcher, after the previous card.
        prev_end = launchers[idx - 1][1] if idx else 0
        head = html[prev_end:start]
        label = _last_match(_ARIA_LABEL_RE, html[prev_end:end])
        display_name = unescape(label.group(1)).strip() if label else _prettify(slug)
        author_m = _last_match(_AUTHOR_RE, head)
        author = unescape(author_m.group(1)).strip() if author_m else ""
        seen.add(slug)
        entries.append(
            PowersSearchResult(
                id=slug,
                display_name=display_name,
                description="",
                author=author or None,
                category="",
                scope=_scope_for(author, bool(_OFFICIAL_RE.search(head))),
                github_url=tree.group(0),
                provider="marketplace",
                keywords=[],
                icon_url=_icon_for(head),
            )
        )
    return entries


def _last_match(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    """Return the last (rightmost) match of *pattern* in *text*, or None."""
    found: re.Match[str] | None = None
    for found in pattern.finditer(text):
        pass
    return found


async def _fetch_html(url: str) -> tuple[int, str]:
    """Patchable HTML GET seam (tests stub this). ``(status, text)``."""
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"User-Agent": _USER_AGENT}) as resp:
            body = await read_capped(resp.content, _MAX_HTML_BYTES + 1)
            if len(body) > _MAX_HTML_BYTES:
                body = body[:_MAX_HTML_BYTES]
            return resp.status, body.decode("utf-8", errors="replace")


class MarketplacePowersProvider:
    """Provider that scrapes the kiro.dev/powers server-rendered listing."""

    def __init__(
        self,
        url: str = _MARKETPLACE_URL,
        *,
        cache_path: Path | None = None,
        ttl_secs: float = _DEFAULT_TTL_SECS,
    ) -> None:
        self._url = url
        self._cache_path = cache_path
        self._ttl = ttl_secs
        self._available = True
        self._unavailable_at = 0.0
        self._served_stale = False
        self._mem_cache: list[PowersSearchResult] | None = None
        self._mem_cache_at = 0.0

    @property
    def name(self) -> str:
        return "marketplace"

    @property
    def display_name(self) -> str:
        return "Kiro Marketplace"

    def is_available(self) -> bool:
        """Report availability, recovering automatically after a cooldown.

        A transient marketplace failure must not be permanent: the registry
        filters on this flag BEFORE calling ``search()``, so a latched False
        means ``_entries()`` never runs again and the provider can never restore
        itself until the gateway restarts. Reporting available again after
        ``_RECOVER_AFTER_SECS`` lets the next search retry and re-latch.
        """
        if self._available:
            return True
        return (time.time() - self._unavailable_at) >= _RECOVER_AFTER_SECS

    def served_stale(self) -> bool:
        """True when the last ``_entries()`` served a degraded (expired) cache.

        The provider can still report ``is_available()`` (it returned data from
        disk) while that data is actually EXPIRED because the live re-scrape
        failed. This flag lets the registry mark the whole listing ``stale`` so
        the UI does not present expired data as fresh. It is False after a fresh
        scrape or a within-TTL cache hit.
        """
        return self._served_stale

    async def search(self, query: str, *, limit: int = 20) -> list[PowersSearchResult]:
        entries = await self._entries()
        if not entries:
            return []
        needle = query.strip().lower()
        out: list[PowersSearchResult] = []
        for e in entries:
            hay = f"{e.id} {e.display_name} {e.description} {e.category}".lower()
            if not needle or needle in hay:
                out.append(e)
            if len(out) >= limit:
                break
        return out

    async def fetch_detail(self, power_id: str) -> PowersDetail | None:
        entries = await self._entries()
        for e in entries:
            if e.id == power_id.strip():
                # The marketplace exposes no per-power document; detail is the
                # listing entry (readme/mcp/steering are filled at install
                # time by the fetch layer, not here).
                return PowersDetail(
                    id=e.id,
                    display_name=e.display_name,
                    description=e.description,
                    author=e.author,
                    category=e.category,
                    scope=e.scope,
                    github_url=e.github_url,
                    provider=self.name,
                    keywords=e.keywords,
                    icon_url=e.icon_url,
                )
        return None

    # -- caching -----------------------------------------------------------

    async def _entries(self) -> list[PowersSearchResult]:
        now = time.time()
        if self._mem_cache is not None and (now - self._mem_cache_at) < self._ttl:
            return self._mem_cache

        # Cache disk I/O is offloaded: this runs on the gateway event loop and
        # the cache holds the full marketplace listing.
        loop = asyncio.get_running_loop()
        disk = await loop.run_in_executor(maintenance_executor(), self._read_cache)
        if disk is not None and (now - disk[0]) < self._ttl:
            self._mem_cache, self._mem_cache_at = disk[1], disk[0]
            self._served_stale = False
            return disk[1]

        # Cache miss / stale — re-scrape.
        try:
            status, html = await _fetch_html(self._url)
        except Exception:
            # Transport failure: serve any (even stale) cache, else degrade.
            if disk is not None:
                self._mem_cache, self._mem_cache_at = disk[1], disk[0]
                self._served_stale = True
                return disk[1]
            logger.warning("marketplace fetch failed; provider marked unavailable")
            self._available = False
            self._unavailable_at = time.time()
            return []

        if status != 200:
            if disk is not None:
                self._mem_cache, self._mem_cache_at = disk[1], disk[0]
                self._served_stale = True
                return disk[1]
            logger.warning("marketplace returned HTTP %s; provider unavailable", status)
            self._available = False
            self._unavailable_at = time.time()
            return []

        entries = parse_listing(html)
        if not entries:
            # Parse failure: markup changed. Do NOT raise — degrade so the
            # official provider still serves. One warning, no stack trace.
            if disk is not None:
                self._mem_cache, self._mem_cache_at = disk[1], disk[0]
                self._served_stale = True
                return disk[1]
            logger.warning("marketplace HTML yielded no cards; provider marked unavailable")
            self._available = False
            self._unavailable_at = time.time()
            return []

        self._available = True
        self._served_stale = False
        self._mem_cache, self._mem_cache_at = entries, now
        await loop.run_in_executor(maintenance_executor(), self._write_cache, now, entries)
        return entries

    def _read_cache(self) -> tuple[float, list[PowersSearchResult]] | None:
        if self._cache_path is None or not self._cache_path.is_file():
            return None
        try:
            raw = json.loads(self._cache_path.read_text("utf-8"))
            fetched_at = float(raw["fetched_at"])
            items = raw["entries"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(items, list):
            return None
        entries: list[PowersSearchResult] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                entries.append(
                    PowersSearchResult(
                        id=str(it["id"]),
                        display_name=str(it.get("display_name", "")),
                        description=str(it.get("description", "")),
                        author=(it.get("author") if it.get("author") is None else str(it["author"])),
                        category=str(it.get("category", "")),
                        scope=str(it.get("scope", "community")),
                        github_url=_require_tree_url(it["github_url"]),
                        provider="marketplace",
                        keywords=[str(k) for k in it.get("keywords", []) if isinstance(k, str)],
                        # Re-validated, not trusted: the cache is on-disk state and
                        # this value becomes an `img` source.
                        icon_url=valid_icon_url(str(it.get("icon_url", ""))),
                    )
                )
            except (KeyError, TypeError, ValueError):
                # ValueError covers a rejected `github_url`: skip the poisoned
                # entry, keep the rest of the cache usable.
                continue
        return fetched_at, entries

    def _write_cache(self, fetched_at: float, entries: list[PowersSearchResult]) -> None:
        if self._cache_path is None:
            return
        payload = {
            "fetched_at": fetched_at,
            "entries": [
                {
                    "id": e.id,
                    "display_name": e.display_name,
                    "description": e.description,
                    "author": e.author,
                    "category": e.category,
                    "scope": e.scope,
                    "github_url": e.github_url,
                    "keywords": e.keywords,
                    "icon_url": e.icon_url,
                }
                for e in entries
            ],
        }
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            # `atomic_write` writes a temp file and renames over the entry, so a
            # symlink sitting at the cache path is REPLACED rather than followed.
            # `write_text` followed it, which turned a cache refresh into a write
            # through to whatever the link named -- `installed.json` in the same
            # directory being the case that matters, since overwriting it with
            # registry JSON destroys the provenance of every installed Power.
            atomic_write(self._cache_path, json.dumps(payload))
        except OSError:
            logger.debug("marketplace cache write failed", exc_info=True)
