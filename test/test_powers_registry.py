"""Tests for the Powers registry providers and secure fetch layer.

No external network is touched: provider HTTP surfaces are stubbed at the
module level (mirrors ``test_mcp_providers``), except in
``TestBoundedStreamRead``, which needs a real socket because the defect it
covers lives in the stream read that stubbing replaces. That server binds
127.0.0.1 on an ephemeral port.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from kiro_crew import powers_providers as _registry_mod
from kiro_crew.powers_providers import fetch as fetch_mod
from kiro_crew.powers_providers import marketplace as marketplace_mod
from kiro_crew.powers_providers import official as official_mod
from kiro_crew.powers_providers.base import (
    PowersProviderRegistry,
    PowersSearchResult,
    ProviderUnavailableError,
)
from kiro_crew.powers_providers.fetch import BundleSecurityError, fetch_power_bundle
from kiro_crew.powers_providers.marketplace import MarketplacePowersProvider, parse_listing
from kiro_crew.powers_providers.official import OfficialPowersProvider

# ---------------------------------------------------------------------------
# Official provider
# ---------------------------------------------------------------------------

# A repo-root listing mixing real power dirs with infrastructure entries.
_ROOT_LISTING = [
    {"type": "dir", "name": ".github"},
    {"type": "dir", "name": ".kiro"},
    {"type": "file", "name": "README.md"},
    {"type": "file", "name": "CONTRIBUTING.md"},
    {"type": "dir", "name": "stripe"},
    {"type": "dir", "name": "zapier"},
    "not-a-dict",
    {"type": "dir", "name": "terraform"},
    # An infrastructure dir with NO POWER.md — must not be advertised.
    {"type": "dir", "name": "scripts"},
]

# Recursive git-tree payload backing the POWER.md verification.
_TREE = {
    "truncated": False,
    "tree": [
        {"type": "blob", "path": "stripe/POWER.md"},
        {"type": "blob", "path": "zapier/POWER.md"},
        {"type": "blob", "path": "terraform/POWER.md"},
        {"type": "blob", "path": "scripts/build.sh"},
        {"type": "tree", "path": "stripe"},
    ],
}


def _patch_json(monkeypatch, fn):
    monkeypatch.setattr(official_mod, "_fetch_gh_json", fn)


def _patch_text(monkeypatch, fn):
    monkeypatch.setattr(official_mod, "_fetch_gh_text", fn)


class TestOfficialProvider:
    def test_name_and_available(self):
        p = OfficialPowersProvider()
        assert p.name == "official"
        assert p.display_name == "Kiro Powers"
        assert p.is_available() is True

    @pytest.mark.asyncio
    async def test_filters_non_power_dirs(self, monkeypatch):
        async def fake(url):
            if "git/trees" in url:
                return 200, _TREE
            return 200, _ROOT_LISTING

        _patch_json(monkeypatch, fake)
        results = await OfficialPowersProvider().search("")
        assert [r.id for r in results] == ["stripe", "zapier", "terraform"]
        # Every surfaced entry is scoped official with a canonical tree URL.
        for r in results:
            assert r.scope == "official"
            assert r.github_url == (
                f"https://github.com/kirodotdev/powers/tree/main/{r.id}"
            )

    @pytest.mark.asyncio
    async def test_search_query_substring_filter(self, monkeypatch):
        async def fake(url):
            if "git/trees" in url:
                return 200, _TREE
            return 200, _ROOT_LISTING

        _patch_json(monkeypatch, fake)
        results = await OfficialPowersProvider().search("stri")
        assert [r.id for r in results] == ["stripe"]

    @pytest.mark.asyncio
    async def test_rate_limit_403_raises_unavailable(self, monkeypatch):
        async def fake(url):
            return 403, None

        _patch_json(monkeypatch, fake)
        with pytest.raises(ProviderUnavailableError):
            await OfficialPowersProvider().search("x")

    @pytest.mark.asyncio
    async def test_not_found_404_returns_empty(self, monkeypatch):
        async def fake(url):
            return 404, None

        _patch_json(monkeypatch, fake)
        assert await OfficialPowersProvider().search("x") == []

    @pytest.mark.asyncio
    async def test_detail_403_vs_404(self, monkeypatch):
        async def rate_limited(url):
            return 403, None

        _patch_json(monkeypatch, rate_limited)
        with pytest.raises(ProviderUnavailableError):
            await OfficialPowersProvider().fetch_detail("stripe")

        async def missing(url):
            return 404, None

        _patch_json(monkeypatch, missing)
        assert await OfficialPowersProvider().fetch_detail("stripe") is None

    @pytest.mark.asyncio
    async def test_detail_without_power_md_is_none(self, monkeypatch):
        async def fake(url):
            return 200, [{"type": "file", "name": "readme.txt"}]

        _patch_json(monkeypatch, fake)
        assert await OfficialPowersProvider().fetch_detail("stripe") is None

    @pytest.mark.asyncio
    async def test_detail_parses_frontmatter_mcp_and_steering(self, monkeypatch):
        power_md = (
            "---\n"
            'name: "stripe"\n'
            'displayName: "Stripe Payments"\n'
            'description: "Manage Stripe."\n'
            'keywords: ["payments", "billing"]\n'
            'author: "Stripe"\n'
            "version: 99\n"  # unknown field — tolerated, never surfaced
            "---\n"
            "# Stripe\nBody text here.\n"
        )
        mcp_json = '{"mcpServers": {"stripe": {"command": "npx"}, "aux": {}}}'

        async def fake_json(url):
            if url.endswith("/steering?ref=main"):
                return 200, [
                    {"type": "file", "name": "guide.md"},
                    {"type": "file", "name": "notes.txt"},
                ]
            # directory listing
            return 200, [
                {
                    "type": "file",
                    "name": "POWER.md",
                    "download_url": "https://raw.githubusercontent.com/kirodotdev/powers/main/stripe/POWER.md",
                },
                {
                    "type": "file",
                    "name": "mcp.json",
                    "download_url": "https://raw.githubusercontent.com/kirodotdev/powers/main/stripe/mcp.json",
                },
                {"type": "dir", "name": "steering"},
            ]

        async def fake_text(url):
            if url.endswith("POWER.md"):
                return 200, power_md
            if url.endswith("mcp.json"):
                return 200, mcp_json
            return 404, ""

        _patch_json(monkeypatch, fake_json)
        _patch_text(monkeypatch, fake_text)
        detail = await OfficialPowersProvider().fetch_detail("stripe")
        assert detail is not None
        assert detail.display_name == "Stripe Payments"
        assert detail.description == "Manage Stripe."
        assert detail.author == "Stripe"
        assert detail.keywords == ["payments", "billing"]
        assert detail.has_mcp is True
        assert detail.mcp_servers == ["aux", "stripe"]
        assert detail.steering_files == ["steering/guide.md"]
        assert "Body text here." in detail.readme


# ---------------------------------------------------------------------------
# Marketplace provider — stdlib HTML parsing
# ---------------------------------------------------------------------------

# Fixture mirrors the REAL kiro.dev/powers card markup (validated against a
# fetched page): per card the title <h3>, an optional "official Kiro power"
# badge and the author <span> precede the launcher <a>, whose own aria-label
# carries the display name; the canonical GitHub tree URL follows as "Details".
# The filler between cards exceeds any fixed pairing window on purpose — a
# window-based parser mispairs each card with its PREDECESSOR's repository,
# which is the regression `test_pairs_each_card_with_its_own_repo` guards.
_FILLER = "<!-- %s -->" % ("x" * 3000)


def _card(
    slug: str,
    label: str,
    author: str,
    url: str,
    *,
    official: bool = False,
) -> str:
    badge = (
        '<span aria-label="This is an official Kiro power.">'
        '<img alt="" aria-hidden="true"/></span>'
        if official
        else ""
    )
    return (
        f'<div class="card"><h3><span>{label}{badge}</span></h3>'
        f'<div class="flex"><span class="break-words text-base">{author}</span></div>'
        f'<a aria-label="Add {label} power to Kiro" '
        f'href="/launch/powers/add?name={slug}" class="group">Add to Kiro</a>'
        f'{_FILLER}<a href="{url}" target="_blank">Details</a></div>'
    )


_FIXTURE_HTML = (
    "<html><body><main><section>"
    + _card(
        "exa",
        "Exa Web Search &amp; Research",
        "Exa",
        "https://github.com/exa-labs/kiro-power-exa/tree/main",
        official=True,
    )
    + _FILLER
    + _card(
        "aws-sam",
        "AWS SAM",
        "AWS",
        "https://github.com/kirodotdev/powers/tree/main/aws-sam",
        official=True,
    )
    + _FILLER
    + _card(
        "steppay",
        "StepPay",
        "StepPay",
        "https://github.com/steppay/kiro-power/tree/main",
    )
    + "</section></main></body></html>"
)


class TestMarketplaceParser:
    def test_extracts_slug_github_author_scope(self):
        entries = parse_listing(_FIXTURE_HTML)
        assert [e.id for e in entries] == ["exa", "aws-sam", "steppay"]
        exa = entries[0]
        assert exa.github_url == "https://github.com/exa-labs/kiro-power-exa/tree/main"
        # aria-label is HTML-escaped in the source and unescaped on parse.
        assert exa.display_name == "Exa Web Search & Research"
        assert exa.author == "Exa"
        assert exa.scope == "official"
        assert exa.provider == "marketplace"
        # The listing carries categories only as aggregate filter counts, never
        # per card, so category is always unknown from this provider.
        assert exa.category == ""

    def test_scope_facets(self):
        by_id = {e.id: e for e in parse_listing(_FIXTURE_HTML)}
        # AWS authorship forms its own upstream facet and wins over the badge.
        assert by_id["aws-sam"].scope == "aws"
        assert by_id["aws-sam"].author == "AWS"
        # No official badge => community.
        assert by_id["steppay"].scope == "community"

    def test_pairs_each_card_with_its_own_repo(self):
        """Regression: pairing must not drift to the neighbouring card.

        A fixed-window parser pairs card N with card N-1's tree URL and drops
        the first card entirely, so every install would fetch the wrong repo.
        """
        entries = parse_listing(_FIXTURE_HTML)
        expected = {
            "exa": "https://github.com/exa-labs/kiro-power-exa/tree/main",
            "aws-sam": "https://github.com/kirodotdev/powers/tree/main/aws-sam",
            "steppay": "https://github.com/steppay/kiro-power/tree/main",
        }
        assert {e.id: e.github_url for e in entries} == expected

    def test_no_cards_yields_empty(self):
        assert parse_listing("<html><body>nothing here</body></html>") == []

    @pytest.mark.asyncio
    async def test_search_filters_and_caches(self, monkeypatch):
        calls = {"n": 0}

        async def fake(url):
            calls["n"] += 1
            return 200, _FIXTURE_HTML

        monkeypatch.setattr(marketplace_mod, "_fetch_html", fake)
        provider = MarketplacePowersProvider()
        results = await provider.search("exa")
        assert [r.id for r in results] == ["exa"]
        # Second search is served from the in-memory cache (no re-fetch).
        await provider.search("zapier")
        assert calls["n"] == 1
        assert provider.is_available() is True

    @pytest.mark.asyncio
    async def test_parse_failure_degrades_to_unavailable(self, monkeypatch):
        async def fake(url):
            return 200, "<html><body>markup changed, no cards</body></html>"

        monkeypatch.setattr(marketplace_mod, "_fetch_html", fake)
        provider = MarketplacePowersProvider()
        # Must NOT raise — degrades so the official provider still serves.
        assert await provider.search("anything") == []
        assert provider.is_available() is False

    @pytest.mark.asyncio
    async def test_transport_failure_without_cache_degrades(self, monkeypatch):
        async def boom(url):
            raise OSError("connection refused")

        monkeypatch.setattr(marketplace_mod, "_fetch_html", boom)
        provider = MarketplacePowersProvider()
        assert await provider.search("x") == []
        assert provider.is_available() is False

    @pytest.mark.asyncio
    async def test_disk_cache_round_trip(self, monkeypatch, tmp_path):
        cache = tmp_path / "mkt.json"
        calls = {"n": 0}

        async def fake(url):
            calls["n"] += 1
            return 200, _FIXTURE_HTML

        monkeypatch.setattr(marketplace_mod, "_fetch_html", fake)
        p1 = MarketplacePowersProvider(cache_path=cache)
        assert [r.id for r in await p1.search("")] == ["exa", "aws-sam", "steppay"]
        assert cache.is_file()
        # A fresh provider reads the disk cache without fetching.
        p2 = MarketplacePowersProvider(cache_path=cache)
        assert [r.id for r in await p2.search("")] == ["exa", "aws-sam", "steppay"]
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Registry fan-out
# ---------------------------------------------------------------------------


class _StubProvider:
    def __init__(self, name, results=None, available=True, boom=False):
        self._name = name
        self._results = results or []
        self._available = available
        self._boom = boom

    @property
    def name(self):
        return self._name

    @property
    def display_name(self):
        return self._name.upper()

    def is_available(self):
        return self._available

    async def search(self, query, *, limit=20):
        if self._boom:
            raise RuntimeError("provider exploded")
        return self._results[:limit]

    async def fetch_detail(self, power_id):
        return None


def _r(provider, ident):
    return PowersSearchResult(
        id=ident,
        display_name=ident,
        description="",
        author=None,
        category="",
        scope="community",
        github_url="",
        provider=provider,
    )


class TestRegistryFanOut:
    @pytest.mark.asyncio
    async def test_failing_provider_isolated(self):
        registry = PowersProviderRegistry()
        registry.register(_StubProvider("boom", boom=True))
        registry.register(_StubProvider("ok", [_r("ok", "p1")]))
        results = await registry.search("q")
        assert [r.id for r in results] == ["p1"]

    @pytest.mark.asyncio
    async def test_unavailable_provider_skipped(self):
        registry = PowersProviderRegistry()
        registry.register(_StubProvider("off", [_r("off", "x")], available=False))
        registry.register(_StubProvider("on", [_r("on", "y")]))
        assert [p.name for p in registry.available_providers] == ["on"]
        assert [r.id for r in await registry.search("q")] == ["y"]

    @pytest.mark.asyncio
    async def test_slow_provider_dropped(self, monkeypatch):
        from kiro_crew.powers_providers import base as base_mod

        monkeypatch.setattr(base_mod, "_SEARCH_TIMEOUT_SECS", 0.05)

        class _Slow(_StubProvider):
            async def search(self, query, *, limit=20):
                await asyncio.sleep(1.0)
                return self._results

        registry = PowersProviderRegistry()
        registry.register(_Slow("slow", [_r("slow", "s1")]))
        registry.register(_StubProvider("fast", [_r("fast", "f1")]))
        assert [r.id for r in await registry.search("q")] == ["f1"]

    @pytest.mark.asyncio
    async def test_provider_filter_targets_one(self):
        registry = PowersProviderRegistry()
        registry.register(_StubProvider("a", [_r("a", "a1")]))
        registry.register(_StubProvider("b", [_r("b", "b1")]))
        assert [r.id for r in await registry.search("q", provider="b")] == ["b1"]


# ---------------------------------------------------------------------------
# fetch_power_bundle — security controls
# ---------------------------------------------------------------------------


class TestRound7Regressions:
    """Regressions for the round-7 review findings on PR #408."""

    @pytest.mark.asyncio
    async def test_provider_failure_is_distinguishable_from_empty(self):
        """A failing provider must be reported, not look like an empty catalog.

        `stale` was derived from is_available() alone and the official provider
        always reports available, so a timeout produced an empty, non-stale
        registry and the UI claimed no Powers exist instead of an outage.
        """
        class _Boom:
            name = "boom"
            display_name = "Boom"

            def is_available(self) -> bool:
                return True

            async def search(self, query, *, limit=20):
                raise RuntimeError("upstream down")

            async def fetch_detail(self, power_id):
                return None

        registry = PowersProviderRegistry()
        registry.register(_Boom())
        assert await registry.search("q") == []
        assert registry.last_failed_providers == ["boom"], "failure not reported to caller"

    @pytest.mark.asyncio
    async def test_branch_with_slash_resolves_against_real_branches(self, monkeypatch):
        """GitHub branch names may contain '/'; splitting at the first segment 404s."""
        async def fake_json(url):
            if "/branches" in url:
                return 200, [{"name": "feature/x"}, {"name": "main"}]
            return 404, None

        monkeypatch.setattr(fetch_mod, "_http_get_json", fake_json)
        owner, repo, branch, subpath = await fetch_mod._parse_ref(
            "https://github.com/o/r/tree/feature/x/power", None
        )
        assert (owner, repo, branch, subpath) == ("o", "r", "feature/x", "power")


class TestRound5Regressions:
    """Regressions for the round-5 review findings on PR #408."""

    @pytest.mark.asyncio
    async def test_official_search_requires_power_md(self, monkeypatch):
        """A directory without POWER.md must not be advertised as installable.

        The name-only denylist advertised any future infrastructure directory,
        so the card appeared but installing it failed on the missing file.
        """
        async def fake(url):
            if "git/trees" in url:
                return 200, _TREE
            return 200, _ROOT_LISTING

        _patch_json(monkeypatch, fake)
        ids = [r.id for r in await OfficialPowersProvider().search("")]
        assert "scripts" in [e.get("name") for e in _ROOT_LISTING if isinstance(e, dict)]
        assert "scripts" not in ids, "dir without POWER.md was advertised"
        assert ids == ["stripe", "zapier", "terraform"]

    @pytest.mark.asyncio
    async def test_official_search_falls_back_when_tree_truncated(self, monkeypatch):
        """A truncated tree must not hide real Powers — fall back to the heuristic."""
        async def fake(url):
            if "git/trees" in url:
                return 200, {"truncated": True, "tree": []}
            return 200, _ROOT_LISTING

        _patch_json(monkeypatch, fake)
        ids = [r.id for r in await OfficialPowersProvider().search("")]
        # Heuristic path: every non-denylisted dir, including the unverifiable one.
        assert "stripe" in ids and "terraform" in ids

    @pytest.mark.asyncio
    async def test_marketplace_availability_recovers_after_cooldown(self, monkeypatch):
        """A transient failure must not latch the provider off permanently.

        The registry filters on is_available() BEFORE calling search(), so a
        latched False means _entries() never runs again and the provider can
        never restore itself until the gateway restarts.
        """
        async def boom(url):
            raise OSError("transport down")

        monkeypatch.setattr(marketplace_mod, "_fetch_html", boom)
        provider = MarketplacePowersProvider()
        assert await provider.search("x") == []
        assert provider.is_available() is False

        # Past the cooldown it reports available again so the next search retries.
        provider._unavailable_at = time.time() - (marketplace_mod._RECOVER_AFTER_SECS + 1)
        assert provider.is_available() is True

    @pytest.mark.asyncio
    async def test_resolve_power_ref_is_idempotent_for_urls(self):
        url = "https://github.com/kirodotdev/powers/tree/main/stripe"
        assert await fetch_mod.resolve_power_ref(url, "marketplace") == url
        # An official SLUG is canonicalized to its repo tree URL, not passed
        # through: `installed.json` records `source.ref` as resolved provenance,
        # and a bare slug is not resolvable outside this provider.
        assert await fetch_mod.resolve_power_ref("stripe", "official") == url
        # ...and canonicalizing an already-canonical URL is a no-op.
        assert await fetch_mod.resolve_power_ref(url, "official") == url


class TestMarketplaceRefResolution:
    """A marketplace id must resolve to its card's repo, not the official monorepo."""

    @pytest.mark.asyncio
    async def test_marketplace_id_resolves_to_card_repo(self, monkeypatch):
        from kiro_crew.powers_providers import marketplace as mkt_mod
        from kiro_crew.powers_providers.base import PowersDetail

        detail = PowersDetail(
            id="exa",
            display_name="Exa Web Search & Research",
            description="",
            author="Exa",
            category="",
            scope="official",
            github_url="https://github.com/exa-labs/kiro-power-exa/tree/main",
            provider="marketplace",
        )

        async def fake_detail(self, power_id):
            assert power_id == "exa"
            return detail

        monkeypatch.setattr(mkt_mod.MarketplacePowersProvider, "fetch_detail", fake_detail)
        resolved = await fetch_mod._resolve_ref("exa", "marketplace")
        assert resolved == "https://github.com/exa-labs/kiro-power-exa/tree/main"
        # ...and the resolved URL parses as a third-party repo, NOT kirodotdev.
        owner, repo, branch, subpath = await fetch_mod._parse_ref(resolved, "marketplace")
        assert (owner, repo, branch, subpath) == ("exa-labs", "kiro-power-exa", "main", "")

    @pytest.mark.asyncio
    async def test_marketplace_id_without_source_is_refused(self, monkeypatch):
        from kiro_crew.powers_providers import marketplace as mkt_mod

        async def fake_detail(self, power_id):
            return None

        monkeypatch.setattr(mkt_mod.MarketplacePowersProvider, "fetch_detail", fake_detail)
        with pytest.raises(BundleSecurityError, match="no source repository"):
            await fetch_mod._resolve_ref("ghost", "marketplace")

    @pytest.mark.asyncio
    async def test_unknown_provider_refused(self):
        with pytest.raises(BundleSecurityError, match="unknown powers provider"):
            await fetch_mod._resolve_ref("whatever", "not-a-provider")

    @pytest.mark.asyncio
    async def test_official_slugs_canonicalize_and_urls_pass_through(self):
        url = "https://github.com/kirodotdev/powers/tree/main/stripe"
        assert await fetch_mod._resolve_ref("stripe", "official") == url
        assert await fetch_mod._resolve_ref("stripe", None) == url
        assert await fetch_mod._resolve_ref(url, "marketplace") == url


class TestFetchSecurity:
    @pytest.mark.asyncio
    async def test_rejects_non_github_host(self):
        with pytest.raises(BundleSecurityError):
            await fetch_power_bundle("https://evil.com/o/r/tree/main")

    @pytest.mark.asyncio
    async def test_rejects_http_scheme(self):
        with pytest.raises(BundleSecurityError):
            await fetch_power_bundle("http://github.com/o/r/tree/main")

    @pytest.mark.asyncio
    async def test_rejects_userinfo_in_authority(self):
        with pytest.raises(BundleSecurityError):
            await fetch_power_bundle("https://user@github.com/o/r/tree/main")

    @pytest.mark.asyncio
    async def test_rejects_traversal_entry(self, monkeypatch):
        async def fake_json(url):
            return 200, [
                {
                    "type": "file",
                    "name": "../escape",
                    "download_url": "https://raw.githubusercontent.com/x/y/main/f",
                }
            ]

        monkeypatch.setattr(fetch_mod, "_http_get_json", fake_json)
        with pytest.raises(BundleSecurityError):
            await fetch_power_bundle("demo")

    @pytest.mark.asyncio
    async def test_rejects_symlink_entry(self, monkeypatch):
        async def fake_json(url):
            return 200, [{"type": "symlink", "name": "link", "target": "/etc/passwd"}]

        monkeypatch.setattr(fetch_mod, "_http_get_json", fake_json)
        with pytest.raises(BundleSecurityError):
            await fetch_power_bundle("demo")

    @pytest.mark.asyncio
    async def test_enforces_byte_cap(self, monkeypatch):
        monkeypatch.setattr(fetch_mod, "_MAX_BUNDLE_BYTES", 10)

        async def fake_json(url):
            return 200, [
                {
                    "type": "file",
                    "name": "POWER.md",
                    "download_url": "https://raw.githubusercontent.com/x/y/main/POWER.md",
                }
            ]

        async def fake_bytes(url):
            return 200, b"x" * 50  # exceeds the 10-byte bundle cap

        monkeypatch.setattr(fetch_mod, "_http_get_json", fake_json)
        monkeypatch.setattr(fetch_mod, "_http_get_bytes", fake_bytes)
        with pytest.raises(BundleSecurityError):
            await fetch_power_bundle("demo")

    @pytest.mark.asyncio
    async def test_honours_timeout(self, monkeypatch):
        monkeypatch.setattr(fetch_mod, "_BUNDLE_TIMEOUT_SECS", 0.01)

        async def slow_json(url):
            await asyncio.sleep(1.0)
            return 200, []

        monkeypatch.setattr(fetch_mod, "_http_get_json", slow_json)
        with pytest.raises(asyncio.TimeoutError):
            await fetch_power_bundle("demo")

    @pytest.mark.asyncio
    async def test_happy_path_writes_bundle(self, monkeypatch, tmp_path):
        power_md = "---\nname: demo\n---\nbody\n"

        async def fake_json(url):
            return 200, [
                {
                    "type": "file",
                    "name": "POWER.md",
                    "download_url": "https://raw.githubusercontent.com/x/y/main/POWER.md",
                }
            ]

        async def fake_bytes(url):
            return 200, power_md.encode("utf-8")

        monkeypatch.setattr(fetch_mod, "_http_get_json", fake_json)
        monkeypatch.setattr(fetch_mod, "_http_get_bytes", fake_bytes)
        root = await fetch_power_bundle("demo")
        try:
            assert (root / "POWER.md").is_file()
            assert (root / "POWER.md").read_text("utf-8") == power_md
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_missing_power_md_rejected(self, monkeypatch):
        async def fake_json(url):
            return 200, [
                {
                    "type": "file",
                    "name": "readme.md",
                    "download_url": "https://raw.githubusercontent.com/x/y/main/readme.md",
                }
            ]

        async def fake_bytes(url):
            return 200, b"hi"

        monkeypatch.setattr(fetch_mod, "_http_get_json", fake_json)
        monkeypatch.setattr(fetch_mod, "_http_get_bytes", fake_bytes)
        with pytest.raises(BundleSecurityError):
            await fetch_power_bundle("demo")


class TestRound9RegistryRegressions:
    """Regressions for the round-9 GPT review findings on PR #408 (provider layer)."""

    # ── Finding 8: a VERIFIED empty tree advertises nothing (not name-fallback) ──

    @pytest.mark.asyncio
    async def test_official_verified_empty_tree_advertises_nothing(self, monkeypatch):
        """A repo with dirs but NO POWER.md anywhere must surface zero Powers.

        ``return found or None`` collapsed a verified-empty result to ``None``,
        which trips the name heuristic and advertises every directory as
        installable. An empty *set* means "verified: nothing here".
        """
        async def fake(url):
            if "git/trees" in url:
                # Verified, complete tree — but not one POWER.md blob.
                return 200, {"truncated": False, "tree": [
                    {"type": "blob", "path": "stripe/README.md"},
                    {"type": "blob", "path": "scripts/build.sh"},
                ]}
            return 200, _ROOT_LISTING

        _patch_json(monkeypatch, fake)
        assert await OfficialPowersProvider().search("") == []

    # ── Finding 7: a tree TRANSPORT failure falls back to the name heuristic ──

    @pytest.mark.asyncio
    async def test_official_survives_tree_transport_failure(self, monkeypatch):
        """A ProviderUnavailableError on the tree call must not drop the catalog.

        The tree request is a secondary verification; its transport failure
        should degrade to the name heuristic, not escape ``search`` and turn the
        entire official catalog into an error.
        """
        async def fake(url):
            if "git/trees" in url:
                raise ProviderUnavailableError("tree endpoint unreachable")
            return 200, _ROOT_LISTING

        _patch_json(monkeypatch, fake)
        ids = [r.id for r in await OfficialPowersProvider().search("")]
        assert "stripe" in ids and "zapier" in ids and "terraform" in ids

    # ── Finding 6: a served-stale marketplace cache is surfaced as stale ──

    @pytest.mark.asyncio
    async def test_marketplace_served_stale_flag(self, monkeypatch, tmp_path):
        """Serving an expired disk cache after a fetch failure sets served_stale."""
        cache = tmp_path / "mkt.json"

        async def ok(url):
            return 200, _FIXTURE_HTML

        # ttl=0 forces a re-scrape on every call (no fresh-cache short-circuit).
        monkeypatch.setattr(marketplace_mod, "_fetch_html", ok)
        p = MarketplacePowersProvider(cache_path=cache, ttl_secs=0.0)
        await p.search("")  # seeds the disk cache
        assert p.served_stale() is False

        async def boom(url):
            raise OSError("marketplace down")

        monkeypatch.setattr(marketplace_mod, "_fetch_html", boom)
        p._mem_cache = None  # force the disk path
        entries = await p.search("")
        assert [e.id for e in entries], "expired cache should still be served"
        assert p.served_stale() is True

    @pytest.mark.asyncio
    async def test_list_registry_marks_stale_when_provider_served_stale(self, monkeypatch):
        """A provider that served stale data must flip the listing to stale:true."""
        from kiro_crew import powers_providers as pp

        class _StaleProv:
            name = "mkt"
            display_name = "Mkt"

            def is_available(self) -> bool:
                return True

            def served_stale(self) -> bool:
                return True

            async def search(self, query, *, limit=20):
                return [PowersSearchResult(
                    id="x", display_name="X", description="", author=None,
                    category="", scope="community", github_url="", provider="mkt",
                )]

            async def fetch_detail(self, power_id):
                return None

        reg = PowersProviderRegistry()
        reg.register(_StaleProv())
        monkeypatch.setattr(pp, "_get_registry", lambda: reg)
        result = await pp.list_registry(q="", limit=10)
        assert result["items"], "provider returned items"
        assert result["stale"] is True

    # ── Finding 1: the provider shaper is fail-closed (no identity fallback) ──

    @pytest.mark.asyncio
    async def test_provider_shape_fails_closed_on_redactor_error(self, monkeypatch):
        """A redactor failure must propagate, never silently return raw text."""
        from kiro_crew import powers_providers as pp

        def boom(text):
            raise RuntimeError("scanner exploded")

        monkeypatch.setattr(pp, "redact_external", boom)
        r = PowersSearchResult(
            id="i", display_name="AKIAIOSFODNN7EXAMPLE", description="",
            author=None, category="", scope="community", github_url="",
            provider="official",
        )
        with pytest.raises(RuntimeError):
            pp._shape(r)

    def test_provider_shape_redacts_credentials(self):
        """A credential in a provider field is redacted in the shaped result."""
        from kiro_crew import powers_providers as pp

        r = PowersSearchResult(
            id="i", display_name="key AKIAIOSFODNN7EXAMPLE", description="",
            author=None, category="", scope="community", github_url="",
            provider="official",
        )
        shaped = pp._shape(r)
        assert "AKIAIOSFODNN7EXAMPLE" not in shaped["displayName"]
        assert "[REDACTED: credential]" in shaped["displayName"]

    # ── Finding 5: detail outage surfaces as ProviderUnavailableError, not 404 ──

    @pytest.mark.asyncio
    async def test_detail_all_unavailable_raises(self, monkeypatch):
        """When every candidate is down, the lookup raises rather than 'not found'."""
        from kiro_crew import powers_providers as pp

        class _DownProv:
            name = "down"
            display_name = "Down"

            def is_available(self) -> bool:
                return True

            async def search(self, query, *, limit=20):
                return []

            async def fetch_detail(self, power_id):
                raise ProviderUnavailableError("upstream down")

        reg = PowersProviderRegistry()
        reg.register(_DownProv())
        monkeypatch.setattr(pp, "_get_registry", lambda: reg)
        with pytest.raises(ProviderUnavailableError):
            await pp.fetch_registry_detail("anything")

    @pytest.mark.asyncio
    async def test_detail_genuine_not_found_returns_none(self, monkeypatch):
        """A reachable provider that simply lacks the power still returns None."""
        from kiro_crew import powers_providers as pp

        class _EmptyProv:
            name = "e"
            display_name = "E"

            def is_available(self) -> bool:
                return True

            async def search(self, query, *, limit=20):
                return []

            async def fetch_detail(self, power_id):
                return None

        reg = PowersProviderRegistry()
        reg.register(_EmptyProv())
        monkeypatch.setattr(pp, "_get_registry", lambda: reg)
        assert await pp.fetch_registry_detail("ghost") is None


@pytest.mark.asyncio
class TestRound12RegistryRegressions:
    """Regressions for the round-11 review findings on PR #408."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/o/r/tree/main/p?access" + "_token=SECRET",
            "https://raw.githubusercontent.com/o/r/main/POWER.md?sig=abc",
        ],
    )
    async def test_query_string_is_rejected(self, url):
        """A ref is persisted to installed.json AND logged, so a credential in a
        query parameter would outlive the request in two agent-readable places.
        Rejecting beats stripping: stripping would silently install from a URL
        the caller never asked for.
        """
        with pytest.raises(BundleSecurityError, match="query"):
            fetch_mod._validate_url(url)

    async def test_fragment_is_rejected(self):
        with pytest.raises(BundleSecurityError, match="fragment"):
            fetch_mod._validate_url("https://github.com/o/r/tree/main/p#frag")

    async def test_clean_allowlisted_url_still_accepted(self):
        """Guard against over-rejecting: the normal form must keep working."""
        assert fetch_mod._validate_url("https://github.com/o/r/tree/main/p") is not None

    async def test_official_listing_429_is_unavailable_not_empty(self, monkeypatch):
        """429/5xx must not render as 'no powers exist'."""

        async def _throttled(url):
            return 429, None

        monkeypatch.setattr(official_mod, "_fetch_gh_json", _throttled)
        with pytest.raises(ProviderUnavailableError):
            await OfficialPowersProvider().search("", limit=5)

    async def test_official_listing_500_is_unavailable_not_empty(self, monkeypatch):
        async def _broken(url):
            return 500, None

        monkeypatch.setattr(official_mod, "_fetch_gh_json", _broken)
        with pytest.raises(ProviderUnavailableError):
            await OfficialPowersProvider().search("", limit=5)

    async def test_official_listing_404_is_still_an_empty_catalog(self, monkeypatch):
        """A genuinely absent path is not an outage."""

        async def _missing(url):
            return 404, None

        monkeypatch.setattr(official_mod, "_fetch_gh_json", _missing)
        assert await OfficialPowersProvider().search("", limit=5) == []

    async def test_overlapping_providers_deduplicate_by_repo_url(self):
        """The same repo from two providers must not consume two result slots.

        This is the test whose absence let an earlier dedup read a non-existent
        `githubUrl` attribute and be silently inert.
        """
        from kiro_crew.powers_providers.base import PowersProviderRegistry

        same = "https://github.com/acme/foo-power"

        def _mk(pid, url):
            return PowersSearchResult(
                id=pid,
                display_name=pid,
                description="",
                author=None,
                category="",
                scope="community",
                github_url=url,
                keywords=[],
                provider="x",
            )

        class _P:
            def __init__(self, name, items):
                self.name = name
                self._items = items

            def is_available(self):
                return True

            async def search(self, query, *, limit=20):
                return self._items

        reg = PowersProviderRegistry()
        reg.register(_P("official", [_mk("foo", same), _mk("bar", "https://github.com/a/bar")]))
        reg.register(_P("marketplace", [_mk("foo", same + "/")]))
        merged = await reg.search("", limit=10)
        urls = [m.github_url.rstrip("/") for m in merged]
        assert urls.count(same) == 1, "duplicate repo consumed a second slot"
        assert len(merged) == 2

    async def test_install_resolution_does_not_trust_the_on_disk_cache(self, monkeypatch, tmp_path):
        """Install resolution must not read the persistent marketplace cache.

        The resolved `github_url` decides WHICH repo installs, so a forged cache
        entry would substitute an attacker's repository under a familiar name and
        no later validation could recover the user's intent. Asserted structurally:
        a poisoned cache file is present and a live scrape is stubbed out, so if
        resolution consulted the cache the poisoned URL would come back.
        """
        cache = tmp_path / ".marketplace-cache.json"
        cache.write_text(
            json.dumps(
                {
                    "fetched_at": time.time(),
                    "entries": [
                        {
                            "id": "exa",
                            "display_name": "Exa",
                            "description": "",
                            "author": None,
                            "category": "",
                            "scope": "community",
                            "github_url": "https://github.com/attacker/evil",
                            "keywords": [],
                            "provider": "marketplace",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        # Seed the PROCESS-WIDE provider with the poisoned cache.
        poisoned = MarketplacePowersProvider(cache_path=cache)
        reg = _registry_mod._get_registry()
        monkeypatch.setattr(reg, "get", lambda name: poisoned if name == "marketplace" else None)

        # Stub only the NETWORK layer, not fetch_detail: a cache-backed provider
        # would still serve the poisoned entry from disk, so this distinguishes
        # "read the cache" from "went live". Patching fetch_detail instead would
        # make both paths fail and the test would pass either way.
        async def _offline(url):
            return 503, ""

        monkeypatch.setattr(marketplace_mod, "_fetch_html", _offline)

        # Fails closed rather than returning the poisoned URL.
        with pytest.raises((ProviderUnavailableError, BundleSecurityError)) as exc:
            await fetch_mod._resolve_ref("exa", "marketplace")
        assert "attacker" not in str(exc.value)


# ---------------------------------------------------------------------------
# Transport: bounded stream reads
# ---------------------------------------------------------------------------


class TestBoundedStreamRead:
    """The wire seam itself, over a loopback socket.

    Every other test in this module stubs ``_http_get_json`` / ``_http_get_bytes``
    / ``_fetch_html``, so nothing exercised the read *inside* them. That gap hid a
    live-only defect: ``StreamReader.read(n)`` returns whatever is buffered when
    it wakes rather than looping to fill n, so a single ``read(cap + 1)``
    truncated every body larger than one wire chunk. The official provider then
    failed ``json.loads`` and the marketplace scrape found zero cards, both
    surfacing as "provider unavailable".

    These tests therefore use a real (loopback-only) HTTP server: a mocked stream
    can be made to return the whole body in one call, which is precisely the
    behaviour that did not hold in production.
    """

    @staticmethod
    async def _serve(body: bytes, content_type: str):
        """Run a loopback server that writes *body* in several chunks.

        Returns ``(url, shutdown)``. Chunked writes with an await between them
        make a short read overwhelmingly likely on the client side, which is what
        the production upstreams do.
        """
        from aiohttp import web

        async def handler(_request):
            resp = web.StreamResponse(headers={"Content-Type": content_type})
            await resp.prepare(_request)
            step = 32 * 1024
            for i in range(0, len(body), step):
                await resp.write(body[i:i + step])
                await asyncio.sleep(0)
            await resp.write_eof()
            return resp

        app = web.Application()
        app.router.add_get("/payload", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        return f"http://127.0.0.1:{port}/payload", runner.cleanup

    @pytest.mark.asyncio
    async def test_json_larger_than_one_chunk_is_parsed_whole(self):
        """A multi-chunk JSON document parses instead of raising unavailable."""
        entries = [{"name": f"power-{i}", "type": "dir", "path": f"p{i}"} for i in range(4000)]
        payload = json.dumps(entries).encode()
        assert len(payload) > 200 * 1024, "payload must exceed one wire chunk"

        url, shutdown = await self._serve(payload, "application/json")
        try:
            status, parsed = await fetch_mod._http_get_json(url)
        finally:
            await shutdown()

        assert status == 200
        assert isinstance(parsed, list) and len(parsed) == 4000
        assert parsed[-1]["name"] == "power-3999"

    @pytest.mark.asyncio
    async def test_bytes_larger_than_one_chunk_are_complete(self):
        """A multi-chunk file body arrives whole, tail included."""
        payload = (b"x" * (400 * 1024)) + b"TAIL-MARKER"
        url, shutdown = await self._serve(payload, "text/plain")
        try:
            status, body = await fetch_mod._http_get_bytes(url)
        finally:
            await shutdown()

        assert status == 200
        assert len(body) == len(payload)
        assert body.endswith(b"TAIL-MARKER")

    @pytest.mark.asyncio
    async def test_marketplace_html_larger_than_one_chunk_yields_every_card(self):
        """Ties the read fix to the symptom: a truncated page parses to few cards.

        Asserted through ``parse_listing`` rather than on byte length, because the
        user-visible failure was an empty catalogue, not a short string.
        """
        card = (
            '<div class="card"><h3>Power {i}</h3>'
            '<span class="break-words text-base">Author {i}</span>'
            '<a aria-label="Add Power {i} power to Kiro" href="/launch/powers/add?name=power-{i}">Add</a>'
            '<a href="https://github.com/kirodotdev/powers/tree/main/power-{i}">Details</a></div>'
        )
        html = "<html><body>" + "".join(card.format(i=i) for i in range(400)) + "</body></html>"
        payload = html.encode()
        assert len(payload) > 100 * 1024, "payload must exceed one wire chunk"

        url, shutdown = await self._serve(payload, "text/html")
        try:
            status, text = await marketplace_mod._fetch_html(url)
        finally:
            await shutdown()

        assert status == 200
        assert len(parse_listing(text)) == 400  # < _MAX_ENTRIES (500)

    @pytest.mark.asyncio
    async def test_over_cap_body_still_rejected(self, monkeypatch):
        """The looping read must not weaken the per-file byte cap."""
        monkeypatch.setattr(fetch_mod, "_MAX_FILE_BYTES", 64 * 1024)
        payload = b"y" * (256 * 1024)
        url, shutdown = await self._serve(payload, "text/plain")
        try:
            with pytest.raises(BundleSecurityError):
                await fetch_mod._http_get_bytes(url)
        finally:
            await shutdown()

    @pytest.mark.asyncio
    async def test_over_cap_json_reported_unavailable(self, monkeypatch):
        """Oversized JSON is a provider failure, not a parse attempt on a prefix."""
        monkeypatch.setattr(fetch_mod, "_MAX_FILE_BYTES", 64 * 1024)
        payload = json.dumps([{"n": i} for i in range(20000)]).encode()
        url, shutdown = await self._serve(payload, "application/json")
        try:
            with pytest.raises(ProviderUnavailableError) as exc:
                await fetch_mod._http_get_json(url)
        finally:
            await shutdown()
        assert "too large" in str(exc.value)


class TestDedupFacetMerge:
    """Overlapping providers must not cost metadata.

    The official provider lists a GitHub directory, so ``description`` /
    ``author`` / ``category`` are structurally empty; the marketplace card
    carries an author line. Provider order puts official first, so a
    drop-the-duplicate dedup rendered every overlapping Power authorless. These
    are ordinary in-process registry tests — no HTTP surface involved.
    """

    @staticmethod
    def _provider(name, results):
        class _P:
            def __init__(self):
                self._results = results

            @property
            def name(self):
                return name

            @property
            def display_name(self):
                return name.title()

            def is_available(self):
                return True

            async def search(self, query, *, limit=20):
                return list(self._results)

            async def fetch_detail(self, power_id):
                return None

        return _P()

    @staticmethod
    def _result(**kw):
        base = dict(
            id="exa",
            display_name="Exa",
            description="",
            author=None,
            category="",
            scope="official",
            github_url="https://github.com/kirodotdev/powers/tree/main/exa",
            provider="official",
            keywords=[],
        )
        base.update(kw)
        return PowersSearchResult(**base)

    @pytest.mark.asyncio
    async def test_blank_facets_filled_from_dropped_duplicate(self):
        """The winner keeps the card; the loser donates what the winner lacks."""
        thin = self._result()
        rich = self._result(
            description="Web search for agents.",
            author="Exa",
            category="search",
            provider="marketplace",
            scope="community",
            keywords=["search", "web"],
        )
        reg = PowersProviderRegistry()
        reg.register(self._provider("official", [thin]))
        reg.register(self._provider("marketplace", [rich]))

        merged = await reg.search("", limit=20)

        assert len(merged) == 1, "the duplicate must still be deduplicated"
        (item,) = merged
        assert item.provider == "official", "provider order still owns the card"
        assert item.author == "Exa"
        assert item.description == "Web search for agents."
        assert item.category == "search"
        assert item.keywords == ["search", "web"]

    @pytest.mark.asyncio
    async def test_populated_facets_are_never_overwritten(self):
        """Enrichment fills blanks only — it is not a last-writer-wins merge."""
        winner = self._result(description="Winner text.", author="Winner", category="a")
        loser = self._result(
            description="Loser text.", author="Loser", category="b", provider="marketplace"
        )
        reg = PowersProviderRegistry()
        reg.register(self._provider("official", [winner]))
        reg.register(self._provider("marketplace", [loser]))

        (item,) = await reg.search("", limit=20)

        assert item.description == "Winner text."
        assert item.author == "Winner"
        assert item.category == "a"

    @pytest.mark.asyncio
    async def test_scope_is_not_merged(self):
        """Scope is a disagreement between providers, not a blank to fill.

        The official provider says ``official`` because the Power sits in the
        official monorepo; the marketplace reports the authorship facet. Silently
        adopting the loser's value would change which scope chip the Power
        answers to.
        """
        winner = self._result(scope="official")
        loser = self._result(scope="aws", provider="marketplace", author="AWS")
        reg = PowersProviderRegistry()
        reg.register(self._provider("official", [winner]))
        reg.register(self._provider("marketplace", [loser]))

        (item,) = await reg.search("", limit=20)

        assert item.scope == "official"
        assert item.author == "AWS", "author is still filled — only scope is held back"


class TestPublisherIcons:
    """Per-card publisher icons, and the origin allowlist that guards them.

    The URL is scraped from a third-party page and rendered as an ``img`` source,
    so the host is an allowlist rather than a coherence check: accepting whatever the
    page offers would let a changed listing point the dashboard at an arbitrary
    origin — a request the user never chose to make, keyed to their session.
    """

    ICON = "https://prod.download.desktop.kiro.dev/powers/icons/vendor.png"

    def _card(self, slug="exa", icon=None):
        icon_tag = f'<img src="{icon}"/>' if icon else ""
        return (
            f'<div id="{slug}">{icon_tag}'
            f"<h3>Exa</h3>"
            f'<span class="break-words text-base">Exa</span>'
            f'<a aria-label="Add Exa power to Kiro" href="/launch/powers/add?name={slug}">Add</a>'
            f'<a href="https://github.com/exa-labs/kiro-power-exa/tree/main">Details</a></div>'
        )

    def test_icon_is_extracted_from_the_card(self):
        (entry,) = parse_listing(self._card(icon=self.ICON))
        assert entry.icon_url == self.ICON

    def test_card_without_an_icon_yields_empty(self):
        """Absent is not an error: the field is optional and the UI falls back."""
        (entry,) = parse_listing(self._card())
        assert entry.icon_url == ""

    @pytest.mark.parametrize(
        "url",
        [
            "http://prod.download.desktop.kiro.dev/powers/icons/a.png",  # scheme downgrade
            "https://evil.example.com/powers/icons/a.png",  # foreign host
            "https://prod.download.desktop.kiro.dev.evil.com/powers/icons/a.png",  # suffix spoof
            "https://prod.download.desktop.kiro.dev/etc/passwd",  # path escape
            "javascript:alert(1)",  # not a URL we would ever fetch
            "",
        ],
    )
    def test_non_allowlisted_origins_are_refused(self, url):
        assert marketplace_mod.valid_icon_url(url) == ""

    def test_a_rejected_icon_does_not_drop_the_card(self):
        """Refusing the icon must not refuse the Power — degrade, don't disappear."""
        (entry,) = parse_listing(self._card(icon="https://evil.example.com/x.png"))
        assert entry.id == "exa"
        assert entry.icon_url == ""

    def test_cache_round_trip_preserves_the_icon(self, tmp_path):
        provider = MarketplacePowersProvider(cache_path=tmp_path / "cache.json")
        entry = PowersSearchResult(
            id="exa",
            display_name="Exa",
            description="",
            author="Exa",
            category="",
            scope="community",
            github_url="https://github.com/exa-labs/kiro-power-exa/tree/main",
            provider="marketplace",
            keywords=[],
            icon_url=self.ICON,
        )
        provider._write_cache(time.time(), [entry])
        read = provider._read_cache()
        assert read is not None
        _fetched_at, entries = read
        assert entries[0].icon_url == self.ICON

    def test_a_poisoned_cache_icon_is_re_validated_on_read(self, tmp_path):
        """The cache is on-disk state, so its icon is untrusted input too.

        Writing through the provider cannot produce a bad value; the threat is a
        cache file edited underneath us, which the spec already treats as
        attacker-reachable for `github_url`. The same applies here because the
        value lands in an `img src`.
        """
        cache = tmp_path / "cache.json"
        cache.write_text(
            json.dumps(
                {
                    "fetched_at": time.time(),
                    "entries": [
                        {
                            "id": "exa",
                            "display_name": "Exa",
                            "description": "",
                            "author": "Exa",
                            "category": "",
                            "scope": "community",
                            "github_url": "https://github.com/exa-labs/kiro-power-exa/tree/main",
                            "keywords": [],
                            "icon_url": "https://evil.example.com/beacon.png",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        provider = MarketplacePowersProvider(cache_path=cache)
        read = provider._read_cache()
        assert read is not None
        _fetched_at, entries = read
        assert entries[0].icon_url == "", "a poisoned cache icon survived the read"
        assert entries[0].id == "exa", "the entry itself should still load"

    @pytest.mark.asyncio
    async def test_overlapping_official_card_inherits_the_marketplace_icon(self):
        """The payoff: official-owned cards are the ones that had no icon.

        The official provider lists a GitHub directory, so it has no icon to
        report, and it wins the dedup for the ~26 overlapping Powers. Without
        facet enrichment those cards — a third of the catalogue — would render the
        fallback while the marketplace had a real icon for them.
        """
        url = "https://github.com/kirodotdev/powers/tree/main/exa"
        thin = PowersSearchResult(
            id="exa", display_name="exa", description="", author=None, category="",
            scope="official", github_url=url, provider="official", keywords=[],
        )
        rich = PowersSearchResult(
            id="exa", display_name="Exa", description="", author="Exa", category="",
            scope="community", github_url=url, provider="marketplace", keywords=[],
            icon_url=self.ICON,
        )

        class _P:
            def __init__(self, name, results):
                self._name, self._results = name, results

            @property
            def name(self):
                return self._name

            @property
            def display_name(self):
                return self._name.title()

            def is_available(self):
                return True

            async def search(self, query, *, limit=20):
                return list(self._results)

            async def fetch_detail(self, power_id):
                return None

        reg = PowersProviderRegistry()
        reg.register(_P("official", [thin]))
        reg.register(_P("marketplace", [rich]))

        (item,) = await reg.search("", limit=20)
        assert item.provider == "official"
        assert item.icon_url == self.ICON

    def test_api_shape_exposes_the_icon(self):
        """`iconUrl` must reach the client, or the render has nothing to use."""
        entry = PowersSearchResult(
            id="exa", display_name="Exa", description="", author="Exa", category="",
            scope="community", github_url="https://github.com/x/y/tree/main",
            provider="marketplace", keywords=[], icon_url=self.ICON,
        )
        assert _registry_mod._shape(entry)["iconUrl"] == self.ICON


class TestCachedRepositoryUrlValidation:
    """`github_url` is re-validated on cache read, like `icon_url`.

    It is the more important of the two: `icon_url` becomes an `img src`, while
    `github_url` becomes an `href` the user clicks, so a poisoned cache entry
    would be click-to-execute rather than cosmetic.
    """

    GOOD = "https://github.com/exa-labs/kiro-power-exa/tree/main"

    def _cache(self, tmp_path, url):
        cache = tmp_path / "cache.json"
        cache.write_text(
            json.dumps(
                {
                    "fetched_at": time.time(),
                    "entries": [
                        {
                            "id": "exa",
                            "display_name": "Exa",
                            "description": "",
                            "author": "Exa",
                            "category": "",
                            "scope": "community",
                            "github_url": url,
                            "keywords": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return MarketplacePowersProvider(cache_path=cache)

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "http://github.com/o/r/tree/main",  # scheme downgrade
            "https://evil.example.com/o/r/tree/main",  # foreign host
            "https://github.com.evil.com/o/r/tree/main",  # suffix spoof
            "https://github.com/o/r",  # not a tree URL
            "",
        ],
    )
    def test_poisoned_cache_entry_is_skipped(self, tmp_path, url):
        provider = self._cache(tmp_path, url)
        read = provider._read_cache()
        assert read is not None
        _fetched_at, entries = read
        assert entries == [], f"a cached entry with {url!r} survived the read"

    def test_a_valid_tree_url_still_loads(self, tmp_path):
        """The guard must not reject legitimate cached entries."""
        provider = self._cache(tmp_path, self.GOOD)
        read = provider._read_cache()
        assert read is not None
        _fetched_at, entries = read
        assert [e.github_url for e in entries] == [self.GOOD]

    def test_one_poisoned_entry_does_not_discard_the_whole_cache(self, tmp_path):
        """Skip the bad entry, keep the rest — not fail the entire listing."""
        cache = tmp_path / "cache.json"

        def _entry(power_id, url):
            return {
                "id": power_id,
                "display_name": power_id,
                "description": "",
                "author": None,
                "category": "",
                "scope": "community",
                "github_url": url,
                "keywords": [],
            }

        cache.write_text(
            json.dumps(
                {
                    "fetched_at": time.time(),
                    "entries": [
                        _entry("bad", "javascript:alert(1)"),
                        _entry("good", self.GOOD),
                    ],
                }
            ),
            encoding="utf-8",
        )
        read = MarketplacePowersProvider(cache_path=cache)._read_cache()
        assert read is not None
        _fetched_at, entries = read
        assert [e.id for e in entries] == ["good"]


class TestRound42CacheSymlink:
    """The marketplace cache write replaces its path instead of following it."""

    def test_cache_write_replaces_a_symlink_instead_of_writing_through_it(
        self, tmp_path
    ):
        """A link at the cache path must not redirect the write.

        The cache lives beside `installed.json`, so a link there turned a routine
        registry refresh into an overwrite of the store's own record -- the
        provenance of every installed Power replaced by registry JSON.
        """
        from kiro_crew.powers_providers import marketplace as mk

        victim = tmp_path / "installed.json"
        victim.write_text('{"kb": {"source": {"kind": "registry"}}}', encoding="utf-8")
        cache = tmp_path / "marketplace-cache.json"
        cache.symlink_to(victim)

        provider = mk.MarketplacePowersProvider()
        provider._cache_path = cache
        provider._write_cache(time.time(), [])

        assert victim.read_text(encoding="utf-8") == (
            '{"kb": {"source": {"kind": "registry"}}}'
        ), "the cache write followed the link and overwrote the store record"
        assert not cache.is_symlink(), "the link should have been replaced"
        assert "entries" in json.loads(cache.read_text(encoding="utf-8"))
