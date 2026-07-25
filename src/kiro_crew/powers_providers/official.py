"""Official Powers provider — ``github.com/kirodotdev/powers``.

The source of truth is the GitHub contents API:

* ``GET /repos/kirodotdev/powers/contents/`` — one directory per power.
* ``GET /repos/kirodotdev/powers/contents/<slug>`` — the power's files; the
  real test of "is this a power" is whether ``POWER.md`` exists in the dir.

There is no JSON catalog API, so we list the monorepo and treat each
non-infrastructure directory as a power. ``GITHUB_TOKEN`` is honoured when
present (raising the rate limit) but the provider works unauthenticated. A
403 (rate limit / access denied) is surfaced as
:class:`ProviderUnavailableError`; a 404 is a normal "not found".
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import yaml  # type: ignore[import-untyped]

from kiro_crew.powers import POWER_MD_NAME
from kiro_crew.powers_providers import fetch as fetch_mod
from kiro_crew.powers_providers.base import (
    PowersDetail,
    PowersSearchResult,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)

_OWNER = "kirodotdev"
_REPO = "powers"
_REF = "main"

# Repo entries that are never powers (infrastructure / docs). Dotted names and
# non-directory entries are filtered separately; this catches any named dirs.
_NON_POWER_NAMES = frozenset(
    {".github", ".kiro", "README.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"}
)

# The five POWER.md frontmatter fields upstream recognises. Everything else is
# tolerated-but-ignored; we never invent version/tags/repository/license.
_MAX_README_BYTES = 64 * 1024


def _s(v: Any) -> str:
    """Coerce provider metadata to str — non-strings become '' (never crash)."""
    return v.strip() if isinstance(v, str) else ""


def _keywords(v: Any) -> list[str]:
    if isinstance(v, list):
        return [k.strip() for k in v if isinstance(k, str) and k.strip()]
    return []


def _tree_url(slug: str) -> str:
    return f"https://github.com/{_OWNER}/{_REPO}/tree/{_REF}/{urllib.parse.quote(slug)}"


def _contents_url(path: str) -> str:
    enc = urllib.parse.quote(path.strip("/"))
    qs = urllib.parse.urlencode({"ref": _REF})
    return f"https://api.github.com/repos/{_OWNER}/{_REPO}/contents/{enc}?{qs}"


def _git_tree_api_url() -> str:
    """Recursive git-tree API URL — every path in the repo in ONE request.

    Distinct from :func:`_tree_url`, which builds a human github.com/<owner>/
    <repo>/tree/<ref>/<slug> link for a single Power.
    """
    return f"https://api.github.com/repos/{_OWNER}/{_REPO}/git/trees/{_REF}?recursive=1"


async def _dirs_containing_power_md() -> set[str] | None:
    """Return top-level directories that actually contain a ``POWER.md``.

    A directory name alone does not make a Power: the repo also holds
    infrastructure directories, and a name-only denylist advertises any future
    one as installable, so the card appears but installing it fails on the
    missing required file. Verified against the real contents instead.

    Uses ONE recursive tree request rather than a per-directory contents call
    (~32 requests, which would burn the unauthenticated rate limit). Returns
    ``None`` when the tree is unavailable or truncated, so the caller can fall
    back to the name heuristic rather than showing an empty catalog. A
    successfully-verified EMPTY repo returns an empty set (not ``None``): that
    is a real "no Powers here" answer, distinct from "verification failed".
    """
    try:
        status, data = await _fetch_gh_json(_git_tree_api_url())
    except ProviderUnavailableError:
        # A transport failure on the (secondary) tree call must not drop the
        # entire official catalog: fall back to the name heuristic exactly as
        # for an unavailable/truncated tree. The primary listing call in
        # ``search`` still surfaces a genuine outage as unavailable.
        logger.debug("powers tree fetch unavailable; falling back to name heuristic")
        return None
    if status != 200 or not isinstance(data, dict):
        return None
    if data.get("truncated") is True:
        # Partial tree would silently hide real Powers — treat as unknown.
        logger.debug("powers tree response truncated; falling back to name heuristic")
        return None
    tree = data.get("tree")
    if not isinstance(tree, list):
        return None
    found: set[str] = set()
    for node in tree:
        if not isinstance(node, dict) or node.get("type") != "blob":
            continue
        path = node.get("path")
        if not isinstance(path, str):
            continue
        head, _, tail = path.partition("/")
        if tail == POWER_MD_NAME and head and not head.startswith("."):
            found.add(head)
    # Return the set even when empty: a verified empty repo genuinely has no
    # Powers, and collapsing that to ``None`` would trip the name-heuristic
    # fallback and advertise every directory as installable. ``None`` is
    # reserved for FAILED / truncated verification above.
    return found


async def _fetch_gh_json(url: str) -> tuple[int, Any]:
    """Patchable JSON GET seam (tests stub this). ``(status, parsed_or_None)``."""
    return await fetch_mod._http_get_json(url)


async def _fetch_gh_text(url: str) -> tuple[int, str]:
    """Patchable raw-text GET seam (tests stub this). ``(status, text)``."""
    fetch_mod._validate_url(url)
    status, body = await fetch_mod._http_get_bytes(url)
    return status, body.decode("utf-8", errors="replace")


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``POWER.md`` into ``(frontmatter_dict, body)``.

    Frontmatter is a leading ``---`` fenced YAML block. Malformed YAML yields
    an empty dict and the whole text as body — never raises.
    """
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return {}, text
    lines = stripped.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    block = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).strip()
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}, body
    if not isinstance(data, dict):
        return {}, body
    return data, body


def _is_power_dir(entry: dict[str, Any]) -> bool:
    if entry.get("type") != "dir":
        return False
    name = entry.get("name")
    if not isinstance(name, str) or not name or name.startswith("."):
        return False
    return name not in _NON_POWER_NAMES


class OfficialPowersProvider:
    """Provider that lists powers from the kirodotdev/powers monorepo."""

    @property
    def name(self) -> str:
        return "official"

    @property
    def display_name(self) -> str:
        return "Kiro Powers"

    def is_available(self) -> bool:
        # Public GitHub API — no auth or configuration required.
        return True

    async def search(self, query: str, *, limit: int = 20) -> list[PowersSearchResult]:
        status, listing = await _fetch_gh_json(_contents_url(""))
        if status == 403:
            raise ProviderUnavailableError("GitHub rate limit or access denied (403)")
        if status == 404:
            # The monorepo path genuinely does not exist: an empty catalog is the
            # honest answer.
            return []
        if status != 200 or listing is None:
            # 429/5xx are outages. Returning [] here rendered "no powers exist"
            # during a GitHub incident, and — because the registry marks a
            # provider stale from failures — also suppressed the stale banner.
            raise ProviderUnavailableError(f"listing fetch returned HTTP {status}")
        if not isinstance(listing, list):
            raise ProviderUnavailableError("listing response was not a JSON array")

        # Verified set of directories that really contain POWER.md; None when the
        # tree call is unavailable/truncated, in which case we fall back to the
        # name heuristic rather than showing nothing.
        verified = await _dirs_containing_power_md()

        needle = query.strip().lower()
        results: list[PowersSearchResult] = []
        for entry in listing:
            if not isinstance(entry, dict) or not _is_power_dir(entry):
                continue
            if verified is not None and entry.get("name") not in verified:
                continue
            slug = entry["name"]
            if needle and needle not in slug.lower():
                continue
            results.append(
                PowersSearchResult(
                    id=slug,
                    display_name=slug,
                    description="",
                    author=None,
                    category="",
                    scope="official",
                    github_url=_tree_url(slug),
                    provider=self.name,
                    keywords=[],
                )
            )
            if len(results) >= limit:
                break
        return results

    async def fetch_detail(self, power_id: str) -> PowersDetail | None:
        slug = power_id.strip()
        if not slug or "/" in slug or ".." in slug or slug.startswith("."):
            return None

        status, listing = await _fetch_gh_json(_contents_url(slug))
        if status == 403:
            raise ProviderUnavailableError("GitHub rate limit or access denied (403)")
        if status == 404 or listing is None:
            return None
        if isinstance(listing, dict):
            listing = [listing]
        if not isinstance(listing, list):
            return None

        by_name: dict[str, dict[str, Any]] = {
            e["name"]: e
            for e in listing
            if isinstance(e, dict) and isinstance(e.get("name"), str)
        }
        power_md = by_name.get("POWER.md")
        if power_md is None or power_md.get("type") != "file":
            return None  # the real "is this a power" test

        fm, body = await self._read_power_md(power_md)
        has_mcp, mcp_servers = await self._read_mcp(by_name.get("mcp.json"))
        steering = await self._read_steering(slug, by_name.get("steering"))

        return PowersDetail(
            id=slug,
            display_name=_s(fm.get("displayName")) or slug,
            description=_s(fm.get("description")),
            author=_s(fm.get("author")) or None,
            category="",
            scope="official",
            github_url=_tree_url(slug),
            provider=self.name,
            keywords=_keywords(fm.get("keywords")),
            readme=body[:_MAX_README_BYTES],
            has_mcp=has_mcp,
            mcp_servers=mcp_servers,
            steering_files=steering,
        )

    async def _read_power_md(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        url = entry.get("download_url")
        if not isinstance(url, str) or not url:
            return {}, ""
        status, text = await _fetch_gh_text(url)
        if status != 200:
            return {}, ""
        return _parse_frontmatter(text)

    async def _read_mcp(self, entry: dict[str, Any] | None) -> tuple[bool, list[str]]:
        if entry is None or entry.get("type") != "file":
            return False, []
        url = entry.get("download_url")
        if not isinstance(url, str) or not url:
            return True, []
        status, text = await _fetch_gh_text(url)
        if status != 200:
            return True, []
        try:
            data = yaml.safe_load(text)  # JSON is a subset of YAML — safe parse
        except yaml.YAMLError:
            return True, []
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if isinstance(servers, dict):
            return True, sorted(k for k in servers if isinstance(k, str))
        return True, []

    async def _read_steering(
        self, slug: str, entry: dict[str, Any] | None
    ) -> list[str]:
        if entry is None or entry.get("type") != "dir":
            return []
        status, listing = await _fetch_gh_json(_contents_url(f"{slug}/steering"))
        if status != 200 or not isinstance(listing, list):
            return []
        out: list[str] = []
        for e in listing:
            if not isinstance(e, dict) or e.get("type") != "file":
                continue
            name = e.get("name")
            if isinstance(name, str) and name.endswith(".md"):
                out.append(f"steering/{name}")
        return sorted(out)
