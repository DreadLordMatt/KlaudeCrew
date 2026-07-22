"""Static file serving for app UI bundles + git blob image proxy.

Extracted from ``routes.py`` (LOC split) and re-exported from ``routes``.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import shutil
from pathlib import Path

from aiohttp import web

from kiro_crew.apps.manager import apps_dir
from kiro_crew.apps.registry import get_registry_app_by_repo, known_registry_repos
from kiro_crew.config.loader import config_dir
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static file serving for app UI bundles
# ---------------------------------------------------------------------------

_ALLOWED_EXTENSIONS = frozenset({
    ".mjs", ".js", ".css", ".json", ".svg", ".png", ".jpg",
    ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ttf", ".map",
})

_CONTENT_TYPES = {
    ".mjs": "application/javascript",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".map": "application/json",
}


async def handle_app_ui_file(request: web.Request) -> web.Response:
    """GET /apps/{name}/ui/{path:.*} — serve app UI bundle files."""
    name = request.match_info["name"]
    file_path = request.match_info.get("path", "")
    if ".." in file_path or file_path.startswith("/"):
        return web.json_response({"error": "invalid path"}, status=400)
    from pathlib import Path
    ext = Path(file_path).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return web.json_response({"error": f"file type {ext!r} not allowed"}, status=403)
    full_path = apps_dir() / name / "ui" / file_path
    if not full_path.is_file():
        return web.json_response({"error": "not found"}, status=404)
    ui_root = (apps_dir() / name / "ui").resolve()
    try:
        full_path.resolve().relative_to(ui_root)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    return web.FileResponse(full_path, headers={"Content-Type": content_type, "Cache-Control": "public, max-age=3600"})  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Git blob proxy — serve images from a registry app's git repo
# ---------------------------------------------------------------------------

def _blob_cache_dir() -> Path:
    return config_dir() / "cache" / "blobs"


_BLOB_FETCH_TIMEOUT = 30  # seconds — shallow clone of a single-branch repo
_BLOB_FETCH_SEMAPHORE = asyncio.Semaphore(3)  # max 3 concurrent git fetches
# Bare-name repo identifier (legacy registry entries) — no scheme, no path.
_SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# https/http git URL: https://host[:port]/org/app[.git]. Host/path charset is
# restricted and shell metacharacters / traversal are rejected separately.
_SAFE_HTTPS_URL_RE = re.compile(
    r"^https?://[A-Za-z0-9.\-]+(?::[0-9]+)?/[A-Za-z0-9._/\-]+$"
)
# scp-style ssh remote: user@host:org/app[.git]
_SAFE_SCP_URL_RE = re.compile(
    r"^[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+:[A-Za-z0-9._/\-]+$"
)
# ssh:// URL form: ssh://user@host[:port]/org/app[.git]
_SAFE_SSH_URL_RE = re.compile(
    r"^ssh://[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+(?::[0-9]+)?/[A-Za-z0-9._/\-]+$"
)
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_BLOB_ALLOWED_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"})


def _is_safe_repo_identifier(repo: str) -> bool:
    """Validate the blob-proxy ``repo`` query parameter.

    Registry entries are now full git URLs (``https://github.com/org/app``,
    ``git@host:org/app.git``), but legacy entries may still use a bare name.
    Accept either a bare token OR a vetted git URL — never an arbitrary string.

    Git URLs are validated against a restricted scheme/host/path charset and
    rejected outright if they contain shell metacharacters or ``..`` path
    traversal, so the value is safe to pass to ``git clone`` argv.
    """
    if not repo:
        return False
    # Reject shell metacharacters and traversal regardless of form.
    if ".." in repo or any(c in repo for c in " \t\n\r;|&$`<>()*?!\\\"'"):
        return False
    if _SAFE_REPO_RE.match(repo):
        return True
    if _SAFE_HTTPS_URL_RE.match(repo):
        return True
    if _SAFE_SCP_URL_RE.match(repo):
        return True
    if _SAFE_SSH_URL_RE.match(repo):
        return True
    return False


def _registry_git_url(repo: str) -> str | None:
    """Resolve the git clone URL for a registry repo, or ``None``.

    The registry entry's ``repo`` field carries the clone URL for the
    open-source build (e.g. ``https://github.com/org/app`` or
    ``git@github.com:org/app.git``).  An entry may also set an explicit
    ``gitUrl``/``cloneUrl`` field.  Returns ``None`` when the entry has no
    resolvable URL so the caller can fail gracefully instead of assuming
    any particular host.
    """
    entry = get_registry_app_by_repo(repo)
    if not entry:
        return None
    for key in ("gitUrl", "cloneUrl"):
        url = entry.get(key)
        if isinstance(url, str) and url:
            return url
    # The repo field itself is treated as a clone URL when it looks like one.
    if ("://" in repo) or repo.startswith("git@") or repo.endswith(".git"):
        return repo
    return None


async def _fetch_git_blob(repo: str, ref: str, file_path: str, cache_path: Path) -> bool:
    """Fetch a single file from a registry app's git repo via a shallow clone.

    Public git hosts (GitHub, etc.) disable the ``git-upload-archive`` service
    used by ``git archive --remote``, so we instead perform a shallow
    ``git clone --depth 1 --branch <ref>`` into a throwaway temp directory
    (mirroring how :mod:`kiro_crew.apps.registry` already clones), read the
    requested file out of the checkout, and write it to the blob cache.  The
    clone URL is resolved from the registry entry; returns ``False`` (graceful
    fallback) when no URL is resolvable or anything goes wrong.
    """
    from kiro_crew.apps.registry import (
        _context_clone_sandbox_mode,
        minimal_env,
    )
    from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv

    git_url = _registry_git_url(repo)
    if not git_url:
        logger.debug("No git URL resolvable for registry repo %r — skipping blob fetch", repo)
        return False

    import tempfile

    tmp_root: str | None = None
    try:
        tmp_root = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-blob-")
        clone_cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            ref,
            "--single-branch",
            git_url,
            tmp_root,
        ]
        # Route through the context-aware clone-sandbox decision (same as the
        # registry.py clone sites) so a companion's extended trusted-host set
        # applies here too; standalone resolves to the same bare decision.
        sandboxed_cmd, _cleanup = wrap_argv(
            clone_cmd, mode=_context_clone_sandbox_mode(git_url)
        )
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling (Talos bdf0d7e5)
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=minimal_env(),
            preexec_fn=resource_limit_preexec(),
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_BLOB_FETCH_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.warning("git clone timed out for %s/%s", repo, file_path)
            return False

        if proc.returncode != 0:
            logger.debug(
                "git clone failed for %s/%s: %s", repo, file_path,
                stderr.decode(errors="replace").strip() if stderr else "",
            )
            return False

        # Read the requested file from the checkout, guarding against escapes
        # out of the clone via symlinks or traversal.
        clone_root = Path(tmp_root).resolve()
        blob_path = (clone_root / file_path).resolve()
        try:
            blob_path.relative_to(clone_root)
        except ValueError:
            logger.debug("blob path escapes clone root for %s/%s", repo, file_path)
            return False
        if not blob_path.is_file():
            return False
        data = await asyncio.to_thread(blob_path.read_bytes)
    except OSError as exc:
        logger.debug("Failed to fetch blob from %s/%s: %s", repo, file_path, exc)
        return False
    finally:
        if tmp_root:
            await asyncio.to_thread(shutil.rmtree, tmp_root, ignore_errors=True)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(cache_path.write_bytes, data)
    return True


async def handle_blob_proxy(request: web.Request) -> web.Response:
    """GET /api/apps/blob — proxy image files from a registry app's git repo.

    Query params:
      repo  — registry repo identifier (matches a registry entry's ``repo``)
      path  — file path within the repo (e.g. "assets/icon/logo.png")
      ref   — git ref, defaults to "mainline"

    SECURITY: Only serves repos listed in the registry JSON (prevents SSRF).
    Caches fetched blobs to ~/.kirocrew/cache/blobs/{repo}/{ref}/{path}.
    Only serves image file types.
    """
    repo = request.query.get("repo", "")
    file_path = request.query.get("path", "")
    # Look up the registry entry's branch; fall back to query param or mainline
    ref = request.query.get("ref", "")
    if not ref:
        entry = get_registry_app_by_repo(repo) if repo else None
        ref = entry.get("branch", "mainline") if entry else "mainline"

    # Validate inputs
    if not repo or not file_path:
        return web.json_response({"error": "repo and path required"}, status=400)
    if not _is_safe_repo_identifier(repo):
        return web.json_response({"error": "invalid repo name"}, status=400)
    if not _SAFE_PATH_RE.match(file_path):
        return web.json_response({"error": "invalid path characters"}, status=400)
    if not _SAFE_REF_RE.match(ref):
        return web.json_response({"error": "invalid ref"}, status=400)
    if ".." in file_path or file_path.startswith("/"):
        return web.json_response({"error": "invalid path"}, status=400)
    # Block access to git internals and other hidden directories
    if any(seg.startswith(".") for seg in Path(file_path).parts):
        return web.json_response({"error": "hidden path segments not allowed"}, status=400)

    ext = Path(file_path).suffix.lower()
    if ext not in _BLOB_ALLOWED_EXT:
        return web.json_response({"error": f"file type {ext!r} not allowed"}, status=403)

    # SECURITY: Only allow repos that appear in the registry (prevents SSRF)
    allowed = await asyncio.to_thread(known_registry_repos)
    if repo not in allowed:
        return web.json_response({"error": "repo not in registry"}, status=403)

    # Check cache.  ``repo`` may now be a full git URL (containing ``/`` and
    # ``:``), so derive a flat, filesystem-safe cache key rather than using the
    # raw value as a directory tree.  The resolved-path check below still
    # guards against any escape out of the cache root.
    repo_key = re.sub(r"[^A-Za-z0-9_.-]", "_", repo)
    cache_path = _blob_cache_dir() / repo_key / ref / file_path

    # SECURITY: Verify resolved path stays within cache dir BEFORE any
    # filesystem side effects (mkdir).  We resolve the parent against the
    # cache root to catch symlink-based escapes.
    cache_root_resolved = _blob_cache_dir().resolve()
    try:
        resolved_parent = cache_path.parent.resolve()
    except OSError:
        resolved_parent = cache_path.parent
    try:
        resolved_parent.relative_to(cache_root_resolved)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)
    resolved = cache_path.resolve()
    try:
        resolved.relative_to(cache_root_resolved)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    # Safe to create directories now that path is validated
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not cache_path.is_file():
        async with _BLOB_FETCH_SEMAPHORE:
            # Re-check after acquiring semaphore (another request may have cached it)
            if not cache_path.is_file():
                ok = await _fetch_git_blob(repo, ref, file_path, cache_path)
                if not ok:
                    return web.json_response({"error": "failed to fetch blob"}, status=502)

    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    sel().log_api_access(caller="dashboard", operation="app_blob_proxy", outcome="served", resources=f"repo={repo} path={file_path}")
    return web.FileResponse(  # type: ignore[return-value]
        cache_path,
        headers={
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",  # 24h browser cache
        },
    )
