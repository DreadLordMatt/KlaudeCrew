"""Secure fetch layer for Powers.

Two responsibilities:

* Shared, bounded HTTP helpers over the GitHub contents API and
  ``raw.githubusercontent.com`` (reused by :mod:`official`).
* :func:`fetch_power_bundle` — download a power's files into a fresh temp
  directory, applying every security control the contract mandates:
  https-only host allowlist, byte and file-count caps, traversal and symlink
  rejection, a bounded overall timeout, and never shelling out to
  ``git clone``.

All network I/O funnels through the module-level ``_http_get_json`` /
``_http_get_bytes`` coroutines so tests can stub them without real sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp

from kiro_crew.executors import maintenance_executor
from kiro_crew.powers_providers.base import ProviderUnavailableError, read_capped
from kiro_crew.powers_providers.marketplace import MarketplacePowersProvider

logger = logging.getLogger(__name__)

# --- security bounds --------------------------------------------------------

# Only these hosts may ever be contacted for power content. https only.
_ALLOWED_HOSTS = frozenset({"github.com", "api.github.com", "raw.githubusercontent.com"})

# Total wall-clock budget for one bundle download (all files). Module-level so
# tests can patch it small; mirrors the detail-timeout discipline in
# ``mcp_discover._DETAIL_TIMEOUT_SECS``.
_BUNDLE_TIMEOUT_SECS = 30.0

# Per-HTTP-call budget.
_HTTP_TIMEOUT_SECS = 15.0

# A power bundle is a handful of small markdown/json files. These caps bound a
# hostile or accidentally-huge tree well below anything that could exhaust
# disk or memory.
_MAX_BUNDLE_BYTES = 8 * 1024 * 1024  # 8 MiB total across all files
_MAX_FILE_BYTES = 4 * 1024 * 1024  # 4 MiB for any single file
_MAX_FILE_COUNT = 200
_MAX_TREE_DEPTH = 8

_USER_AGENT = "KiroCrew/1.0 (powers-registry)"

# The official powers monorepo — the base a bare slug resolves against.
_OFFICIAL_OWNER = "kirodotdev"
_OFFICIAL_REPO = "powers"
_OFFICIAL_REF = "main"


class BundleSecurityError(Exception):
    """A fetched tree violated a security control (traversal, symlink, caps)."""


def _github_headers() -> dict[str, str]:
    """Request headers, honouring ``GITHUB_TOKEN`` when present (optional)."""
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _http_get_json(url: str) -> tuple[int, Any]:
    """GET *url* and parse JSON. Returns ``(status, parsed_or_None)``.

    Raises :class:`ProviderUnavailableError` on transport-level failure so a
    dead network is distinguishable from an HTTP error status.
    """
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=_github_headers()) as resp:
                body = await read_capped(resp.content, _MAX_FILE_BYTES + 1)
                if len(body) > _MAX_FILE_BYTES:
                    raise ProviderUnavailableError("GitHub JSON response too large")
                if resp.status != 200:
                    return resp.status, None
                return resp.status, json.loads(body.decode("utf-8"))
    except ProviderUnavailableError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise ProviderUnavailableError(str(exc)) from exc


async def _http_get_bytes(url: str) -> tuple[int, bytes]:
    """GET *url* and return ``(status, body_bytes)`` with a per-file byte cap."""
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=_github_headers()) as resp:
                body = await read_capped(resp.content, _MAX_FILE_BYTES + 1)
                if len(body) > _MAX_FILE_BYTES:
                    raise BundleSecurityError("power file exceeds per-file byte cap")
                return resp.status, body
    except (BundleSecurityError, ProviderUnavailableError):
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise ProviderUnavailableError(str(exc)) from exc


def _validate_url(url: str) -> urllib.parse.SplitResult:
    """Validate a URL against the fetch security policy.

    Enforces https-only, the host allowlist, no userinfo in the authority,
    and rejects IP-literal hosts. Returns the parsed result on success;
    raises :class:`BundleSecurityError` otherwise.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise BundleSecurityError(f"non-https URL rejected: {parts.scheme or '(none)'}")
    if parts.username or parts.password or "@" in parts.netloc:
        raise BundleSecurityError("userinfo in URL authority rejected")
    # Query strings and fragments are refused outright. Neither is meaningful for
    # the GitHub contents/raw endpoints this module talks to, and the accepted ref
    # is persisted to the agent-readable `installed.json` and written to logs — so
    # a ref carrying `?access_token=...` would leak a credential into two places
    # that outlive the request. Rejecting is safer than stripping: stripping would
    # silently install from a URL the caller did not ask for.
    if parts.query:
        raise BundleSecurityError("query string in URL rejected")
    if parts.fragment:
        raise BundleSecurityError("fragment in URL rejected")
    host = parts.hostname or ""
    if host not in _ALLOWED_HOSTS:
        raise BundleSecurityError(f"host not on allowlist: {host or '(none)'}")
    # Reject IP-literal hosts even if one somehow matched (defence in depth).
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise BundleSecurityError("IP-literal host rejected")
    return parts


async def _parse_ref(ref: str, provider: str | None) -> tuple[str, str, str, str]:
    """Resolve *ref* to ``(owner, repo, branch, subpath)``.

    ``ref`` is either a bare official power slug (no scheme) or a canonical
    GitHub tree URL. Validates the URL form against the host allowlist.
    """
    ref = ref.strip()
    if not ref:
        raise BundleSecurityError("empty power ref")

    if "://" in ref or ref.startswith("//"):
        parts = _validate_url(ref)
        if parts.hostname != "github.com":
            raise BundleSecurityError("tree URL must be a github.com URL")
        # /<owner>/<repo>/tree/<branch>/<subpath...>
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) < 4 or segments[2] != "tree":
            raise BundleSecurityError("malformed github tree URL")
        owner, repo = segments[0], segments[1]
        rest = segments[3:]
        if len(rest) == 1:
            # Unambiguous: nothing after the ref.
            return owner, repo, rest[0], ""
        # Ambiguous: GitHub allows '/' in branch names, so `tree/feature/x/power`
        # could be branch `feature` + path `x/power` OR branch `feature/x` + path
        # `power`. Splitting at the first segment silently targets the wrong ref
        # and the install fails with a 404. Resolve against the repo's real
        # branch list and prefer the LONGEST matching branch.
        branch, subpath = await _disambiguate_ref(owner, repo, rest)
        return owner, repo, branch, subpath

    # Bare slug -> official monorepo. Slug must be a single kebab-case segment.
    slug = ref
    if "/" in slug or ".." in slug or slug.startswith("."):
        raise BundleSecurityError(f"invalid official power slug: {slug!r}")
    if provider not in (None, "official"):
        # Non-official providers address powers by their own id, which does NOT
        # live in the official monorepo. Callers must resolve the id to its
        # canonical tree URL first (see _resolve_ref).
        raise BundleSecurityError(f"bare slug not valid for provider {provider!r}")
    return _OFFICIAL_OWNER, _OFFICIAL_REPO, _OFFICIAL_REF, slug


async def _disambiguate_ref(owner: str, repo: str, rest: list[str]) -> tuple[str, str]:
    """Split *rest* into (branch, subpath) using the repo's actual branch names.

    Falls back to the single-segment split when the branch list cannot be
    fetched, which preserves today's behaviour for the common single-segment
    default-branch case rather than failing the install outright.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/branches?per_page=100"
    try:
        status, data = await _http_get_json(url)
    except Exception:  # pragma: no cover - transport guarded by caller timeout
        status, data = 0, None
    names: set[str] = set()
    if status == 200 and isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.add(item["name"])
    # Longest first so `feature/x` wins over `feature`.
    for cut in range(len(rest), 0, -1):
        candidate = "/".join(rest[:cut])
        if candidate in names:
            return candidate, "/".join(rest[cut:])
    return rest[0], "/".join(rest[1:])


async def resolve_power_ref(ref: str, provider: str | None) -> str:
    """Public alias for :func:`_resolve_ref` (idempotent on resolved URLs).

    Callers record the RESOLVED ref as a Power's provenance, so a marketplace
    install stores the repository it actually came from rather than the
    provider-scoped slug.
    """
    return await _resolve_ref(ref, provider)


async def _resolve_ref(ref: str, provider: str | None) -> str:
    """Resolve a provider-scoped power id to a fetchable ref.

    The marketplace lists powers hosted in arbitrary third-party repositories,
    so a marketplace id (e.g. ``exa``) is meaningless to the official monorepo
    path and must be mapped to that card's canonical GitHub tree URL before any
    fetch. Resolution happens HERE, server-side, rather than trusting a
    client-supplied URL: the id the user clicked then unambiguously determines
    which repository is installed, so a caller cannot point a marketplace id at
    a different repo than the card displayed.
    """
    ref = ref.strip()
    if not ref or "://" in ref or ref.startswith("//"):
        return ref
    if provider in (None, "official"):
        # Canonicalize the official SLUG to its repository tree URL. Returning the
        # bare slug meant `installed.json` recorded provenance as e.g. `stripe`,
        # which is not resolvable outside this provider — the dashboard rendered an
        # unusable link and the record could not be traced back to a repository.
        # The spec documents `source.ref` as the resolved URL, so this makes the
        # code match the contract for official installs the way it already did for
        # marketplace ones.
        return (
            f"https://github.com/{_OFFICIAL_OWNER}/{_OFFICIAL_REPO}"
            f"/tree/{_OFFICIAL_REF}/{ref}"
        )

    if provider != "marketplace":
        raise BundleSecurityError(f"unknown powers provider: {provider!r}")
    # Resolve through a CACHELESS provider, deliberately NOT the process-wide one.
    # The resolved `github_url` decides WHICH repository gets installed, and the
    # persistent cache is an on-disk file: the write-protection covering it is
    # path-matching, which is home-anchored and (as the shared matcher's own scope
    # note records) evadable by a `cd`-relative write. Trusting that file for the
    # install TARGET means a forged entry makes a familiar Power name install an
    # attacker's repository — and no later validation can recover the user's
    # intent, because the forged URL IS what install was asked to fetch.
    #
    # This reverses an earlier revision that shared the cached provider so a card
    # visible from cache would still install during an outage. That convenience is
    # not worth the substitution risk: resolution now FAILS CLOSED, so during a
    # marketplace outage install refuses rather than trusting stale on-disk state.
    # A refused install is recoverable by retrying; a silently substituted
    # repository is not.
    resolver = MarketplacePowersProvider(cache_path=None)
    try:
        detail = await resolver.fetch_detail(ref)
    except ProviderUnavailableError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise BundleSecurityError(f"could not resolve marketplace power {ref!r}") from exc
    if detail is None or not detail.github_url:
        raise BundleSecurityError(f"marketplace power {ref!r} has no source repository")
    # Re-validate: the URL may have come from the on-disk marketplace cache, so it
    # is not inherently more trustworthy than caller input. _validate_url applies
    # the https-only github host allowlist (rejecting other hosts, userinfo and
    # IP literals) before the value is used to build any request.
    parts = _validate_url(detail.github_url)
    if parts.hostname != "github.com":
        raise BundleSecurityError("resolved power source must be a github.com URL")
    return detail.github_url


def _contents_url(owner: str, repo: str, path: str, branch: str) -> str:
    """Build a GitHub contents API URL for *path* within a repo at *branch*."""
    enc_path = urllib.parse.quote(path.strip("/"))
    qs = urllib.parse.urlencode({"ref": branch})
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{enc_path}?{qs}"


def _safe_join(root: Path, rel: str) -> Path:
    """Join *rel* under *root*, rejecting traversal and absolute components."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise BundleSecurityError(f"path traversal component in {rel!r}")
    if Path(rel).is_absolute() or (parts and ":" in parts[0]):
        raise BundleSecurityError(f"absolute path rejected: {rel!r}")
    dest = root.joinpath(*parts)
    # Final containment check — resolve without following (parents only exist
    # because we created them), then verify the prefix.
    root_resolved = root.resolve()
    dest_resolved = (root_resolved.joinpath(*parts)).resolve()
    if root_resolved != dest_resolved and root_resolved not in dest_resolved.parents:
        raise BundleSecurityError(f"path escapes bundle root: {rel!r}")
    return dest


class _Budget:
    """Mutable accumulator enforcing file-count and total-byte caps."""

    def __init__(self) -> None:
        self.files = 0
        self.total_bytes = 0

    def charge(self, nbytes: int) -> None:
        self.files += 1
        self.total_bytes += nbytes
        if self.files > _MAX_FILE_COUNT:
            raise BundleSecurityError("power bundle exceeds file-count cap")
        if self.total_bytes > _MAX_BUNDLE_BYTES:
            raise BundleSecurityError("power bundle exceeds total-byte cap")


async def _download_dir(
    owner: str,
    repo: str,
    branch: str,
    api_path: str,
    dest_dir: Path,
    budget: _Budget,
    depth: int,
) -> None:
    """Recursively download one directory of the power tree into *dest_dir*."""
    if depth > _MAX_TREE_DEPTH:
        raise BundleSecurityError("power tree exceeds max depth")
    status, listing = await _http_get_json(_contents_url(owner, repo, api_path, branch))
    if status == 403:
        raise ProviderUnavailableError("GitHub rate limit or access denied (403)")
    if status == 404:
        raise FileNotFoundError(f"power path not found: {api_path}")
    # Every OTHER non-200 is an outage, not a missing power. Collapsing 429/5xx
    # into FileNotFoundError told the user the Power does not exist when GitHub
    # was simply throttling or down — and the caller then reports a permanent 404
    # for a transient condition.
    if status != 200 or listing is None:
        raise ProviderUnavailableError(f"listing fetch returned HTTP {status}")
    if isinstance(listing, dict):
        # A single-file path returns an object, not a list.
        listing = [listing]
    if not isinstance(listing, list):
        raise BundleSecurityError("unexpected GitHub contents payload")

    await asyncio.get_running_loop().run_in_executor(
        maintenance_executor(), lambda: dest_dir.mkdir(parents=True, exist_ok=True)
    )
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise BundleSecurityError("contents entry missing name")
        # Reject symlinks (and any non file/dir type) outright — a symlink in
        # the tree could point outside the bundle once materialised.
        if etype == "symlink" or (etype not in ("file", "dir")):
            raise BundleSecurityError(f"symlink or unsupported entry rejected: {name}")
        # `_safe_join` resolves and containment-checks a path, which touches
        # the filesystem. It runs once per entry (up to 200), so on slow or
        # saturated storage the accumulated stat calls stall the gateway loop
        # and heartbeat. Offloaded like every other filesystem step here.
        child_dest = await asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), _safe_join, dest_dir, name
        )
        if etype == "dir":
            await _download_dir(
                owner, repo, branch, f"{api_path}/{name}", child_dest, budget, depth + 1
            )
            continue
        download_url = entry.get("download_url")
        if not isinstance(download_url, str) or not download_url:
            raise BundleSecurityError(f"file entry missing download_url: {name}")
        _validate_url(download_url)
        fstatus, body = await _http_get_bytes(download_url)
        if fstatus == 403:
            raise ProviderUnavailableError("GitHub rate limit or access denied (403)")
        if fstatus != 200:
            raise ProviderUnavailableError(f"file fetch returned HTTP {fstatus}")
        budget.charge(len(body))
        # Up to 200 files, each up to 4 MiB: writing these synchronously would
        # block the gateway event loop on slow or saturated storage, stalling
        # chat, dashboard requests and heartbeat processing.
        # Retained and drained: cancelling the await (e.g. the overall timeout)
        # does NOT stop the executor thread, so an undrained write can still be
        # creating files while the caller rmtree's the temp root — which loses on
        # Windows (the open handle blocks removal) and leaves partial bundles.
        write_future = asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), child_dest.write_bytes, body
        )
        try:
            await asyncio.shield(write_future)
        except BaseException:
            with contextlib.suppress(BaseException):
                await asyncio.shield(write_future)
            raise


async def _fetch_into(ref: str, provider: str | None, root: Path) -> Path:
    owner, repo, branch, subpath = await _parse_ref(
        await _resolve_ref(ref, provider), provider
    )
    budget = _Budget()
    await _download_dir(owner, repo, branch, subpath, root, budget, depth=0)
    if not (root / "POWER.md").is_file():
        raise BundleSecurityError("fetched bundle has no POWER.md — not a valid Power")
    return root


async def fetch_power_bundle(ref: str, *, provider: str | None = None) -> Path:
    """Download a power's files into a fresh temp dir containing ``POWER.md``.

    ``ref`` is either an official power slug or a canonical GitHub tree URL.
    Every security control from the contract is applied: https-only host
    allowlist, byte and file-count caps, traversal and symlink rejection, a
    bounded overall timeout, and no ``git clone``. Returns the bundle root
    :class:`~pathlib.Path`; the caller owns cleanup.
    """
    # mkdtemp touches the filesystem; on slow or saturated storage that stalls
    # the gateway loop, so it runs in the maintenance executor like every other
    # filesystem step on this path.
    root = Path(
        await asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), lambda: tempfile.mkdtemp(prefix="kirocrew-power-")
        )
    )
    try:
        return await asyncio.wait_for(
            _fetch_into(ref, provider, root), timeout=_BUNDLE_TIMEOUT_SECS
        )
    except BaseException:
        # Never leave a partial/oversized tree on disk on any failure path,
        # including timeout cancellation.
        # Recursive delete of up to the download cap — keep it off the loop.
        await asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), shutil.rmtree, root, True
        )
        raise
