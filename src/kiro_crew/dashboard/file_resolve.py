"""Resolve a file path that was renamed after a turn recorded it.

The dashboard Files / "Changed files" panels list paths harvested from
immutable session history (``meta.file_changes``). After a file is renamed,
the old path is still listed and clicking it 404s. This module maps such an
old recorded path to its current on-disk location.

Resolution order for a path that no longer exists:

1. **git rename** — only when the path's parent dir is inside a git work tree
   (``git rev-parse --is-inside-work-tree``). Scans rename records
   (``git log --all -M --diff-filter=R --name-status``) for one whose OLD name
   is the requested path. ``method='git-rename'``.
2. **content similarity** — reconstruct the file's last recorded snapshot from
   ``meta.file_changes`` (the ``after`` content, or ``before`` when ``after`` is
   empty — an empty ``after`` is the signature of a file renamed away
   mid-turn), then score it against same-extension files in the parent dir with
   ``difflib.SequenceMatcher(...).quick_ratio()``. Best ratio >= 0.6 wins.
   ``method='content-match'``, ``confidence=<ratio>``.
3. otherwise all-null (gone and unresolvable).

Safety: the HTTP endpoint applies the SAME path-safety / sensitive-path guard
as ``/api/file-read`` (``hooks.validate_file_path``); this module never widens
it. Candidate files reached during a content scan are re-checked against
``is_sensitive_path`` on their resolved target. Every git call carries a
subprocess timeout so a lookup can never hang, and the whole scan is designed
to run OFF the event loop (see ``asyncio.to_thread`` at the call sites).
"""

from __future__ import annotations

import difflib
import logging
import os
import subprocess
import threading
import time
from typing import Any

from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

# Content-similarity scan caps.
_MAX_CANDIDATES = 200
_MAX_READ_BYTES = 256 * 1024
_MIN_RATIO = 0.6

# Hardened git invocation prefix (mirrors api_file_diff): disable textconv /
# filter drivers and fsmonitor so a hostile ``.gitattributes`` / ``.git/config``
# in an untrusted repo cannot execute code during a read-only lookup.
_GIT = [
    "git",
    "-c", "diff.textconv=",
    "-c", "core.attributesFile=/dev/null",
    "-c", "core.fsmonitor=",
]
_GIT_ENV_EXTRA = {"GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
_GIT_TIMEOUT = 5

# Short-TTL memoization keyed by canonical path (bounds repeated lookups from a
# panel that re-requests the same rows). Content can change under us, so the TTL
# is deliberately short.
_CACHE_TTL = 15.0
_CACHE_MAX = 512
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _git_env() -> dict[str, str]:
    return {**os.environ, **_GIT_ENV_EXTRA}


def _git_rename_target(canonical_path: str) -> str | None:
    """Current path a renamed-away *canonical_path* maps to, or ``None``.

    Fires only when the parent dir is inside a git work tree. Reads rename
    records (``--diff-filter=R``) that name the old path and returns the new
    path (absolute, canonical) when it exists on disk and is not sensitive.
    Any git failure/timeout yields ``None`` (caller falls through to content
    matching), so a lookup can never hang or raise.
    """
    parent = os.path.dirname(canonical_path)
    if not parent or not os.path.isdir(parent):
        return None
    env = _git_env()
    try:
        inside = subprocess.run(
            [*_GIT, "rev-parse", "--is-inside-work-tree"],
            cwd=parent, capture_output=True, text=True, timeout=_GIT_TIMEOUT, env=env,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        root = subprocess.run(
            [*_GIT, "rev-parse", "--show-toplevel"],
            cwd=parent, capture_output=True, text=True, timeout=_GIT_TIMEOUT, env=env,
        )
        if root.returncode != 0:
            return None
        repo_root = root.stdout.strip()
        # NOTE: we must NOT pass the old path as a pathspec here. Once a file is
        # renamed away, its old name exists in no current tree, so
        # ``git log -- <oldpath>`` (without --follow, which itself needs the
        # *new* name) returns nothing. Instead list all rename records and match
        # the old name ourselves. ``-n`` bounds cost on large histories.
        log = subprocess.run(
            [*_GIT, "log", "--all", "-M", "--diff-filter=R",
             "--name-status", "--format=", "-n", "2000"],
            cwd=parent, capture_output=True, text=True, timeout=_GIT_TIMEOUT, env=env,
        )
        if log.returncode != 0:
            return None
    except (subprocess.SubprocessError, OSError):
        # TimeoutExpired, CalledProcessError, FileNotFoundError (no git), etc.
        return None

    if not repo_root:
        return None
    old_rel = os.path.relpath(canonical_path, repo_root)
    # ``--name-status`` rename lines are tab-separated: ``R<score>\t<old>\t<new>``.
    # git log is newest-first, so the first record whose old-name is ours is the
    # most recent rename. Non-rename / commit-message lines lack the tab layout
    # and are skipped by the 3-field check.
    for line in log.stdout.splitlines():
        if not line.startswith("R"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        old_name, new_name = parts[1], parts[2]
        if old_name != old_rel:
            continue
        candidate = os.path.realpath(os.path.join(repo_root, new_name))
        if os.path.isfile(candidate) and not is_sensitive_path(candidate):
            return candidate
    return None


def _read_capped(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(_MAX_READ_BYTES)
    except (OSError, UnicodeError):
        return None


def _content_match_target(canonical_path: str, snapshot: str) -> tuple[str, float] | None:
    """Best same-extension sibling whose content matches *snapshot*.

    Scores each candidate with ``difflib.SequenceMatcher(...).quick_ratio()``
    (the recorded snapshot is set as the cached second sequence so only the
    candidate side is rebuilt per file). Returns ``(resolved_path, ratio)`` for
    the best candidate at or above :data:`_MIN_RATIO`, else ``None``.
    """
    if not snapshot:
        return None
    parent = os.path.dirname(canonical_path)
    if not parent or not os.path.isdir(parent):
        return None
    ext = os.path.splitext(canonical_path)[1].lower()
    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(snapshot)
    best_path: str | None = None
    best_ratio = 0.0
    count = 0
    try:
        entries = sorted(os.scandir(parent), key=lambda e: e.name.lower())
    except OSError:
        return None
    for entry in entries:
        if count >= _MAX_CANDIDATES:
            break
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        if os.path.splitext(entry.name)[1].lower() != ext:
            continue
        resolved = os.path.realpath(entry.path)
        if resolved == canonical_path:
            continue  # the (now-gone) file itself
        if is_sensitive_path(resolved):
            continue
        count += 1
        content = _read_capped(entry.path)
        if content is None:
            continue
        matcher.set_seq1(content)
        ratio = matcher.quick_ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_path = resolved
    if best_path is not None and best_ratio >= _MIN_RATIO:
        return best_path, best_ratio
    return None


def latest_snapshot(conversation_log: Any, recorded_path: str) -> str | None:
    """Newest ``file_changes`` snapshot recorded for *recorded_path*.

    Reuses the same conversation-log reader as
    ``handlers/artifacts.py:_collect_session_docs`` (``list_sessions`` /
    ``read_messages``) instead of hand-rolled jsonl parsing. Sessions come back
    newest-first, so the first session containing a matching entry is the newest
    one; within it the last matching entry (latest message) wins. Returns that
    entry's ``after`` content, or ``before`` when ``after`` is empty. ``None``
    when no session recorded the path.
    """
    if conversation_log is None:
        return None
    try:
        sessions = conversation_log.list_sessions()
    except Exception as exc:  # noqa: BLE001 — a corrupt history dir must not break resolution
        logger.warning("file-resolve: list_sessions failed: %s", exc)
        return None
    for sess in sessions:
        if not isinstance(sess, dict):
            continue
        key = sess.get("key")
        if not key:
            continue
        try:
            msgs = conversation_log.read_messages(key)
        except Exception:  # noqa: BLE001 — skip an unreadable session, keep scanning
            continue
        snap: str | None = None
        for m in msgs:
            if not isinstance(m, dict):
                continue
            meta = m.get("meta")
            if not isinstance(meta, dict):
                continue
            fcs = meta.get("file_changes")
            if not isinstance(fcs, list):
                continue
            for fc in fcs:
                if not isinstance(fc, dict) or fc.get("path") != recorded_path:
                    continue
                snap = snapshot_from_change(fc)
        if snap is not None:
            return snap
    return None


def snapshot_from_change(fc: dict[str, Any]) -> str:
    """Extract the resolution snapshot from one ``file_changes`` entry.

    Prefers ``after`` content; falls back to ``before`` when ``after`` is empty
    (a file renamed away mid-turn records an empty ``after``). Always returns a
    string (possibly empty).
    """
    after = fc.get("after")
    if isinstance(after, str) and after:
        return after
    before = fc.get("before")
    return before if isinstance(before, str) else ""


def _resolve_gone(canonical_path: str, snapshot: str | None) -> tuple[str | None, str | None, float | None]:
    """Resolve a path known NOT to exist → ``(resolved, method, confidence)``.

    git-rename first (needs no snapshot), then content-match (needs one).
    """
    target = _git_rename_target(canonical_path)
    if target is not None:
        return target, "git-rename", None
    if snapshot:
        match = _content_match_target(canonical_path, snapshot)
        if match is not None:
            return match[0], "content-match", match[1]
    return None, None, None


def resolve_recorded(recorded_path: str, snapshot: str | None) -> tuple[str | None, str | None, float | None]:
    """Resolve a recorded doc path using an ALREADY-KNOWN *snapshot*.

    Used by the session-docs scan, which captured the snapshot while collecting
    ``file_changes`` (so it need not re-read history once per path). Returns
    ``(resolved_path, method, confidence)``:

    - ``resolved_path == recorded_path`` with ``method='exact'`` when it still
      exists,
    - a new absolute path with ``git-rename`` / ``content-match`` when renamed,
    - ``(None, None, None)`` when gone and unresolvable (caller drops the row).

    Relative recorded paths cannot be resolved safely (they resolve against the
    gateway CWD, not the session's project) and against sensitive resolved
    targets we refuse — both yield the all-null result.
    """
    expanded = os.path.expanduser(recorded_path)
    if not os.path.isabs(expanded):
        return None, None, None
    canonical = os.path.realpath(expanded)
    if is_sensitive_path(canonical):
        return None, None, None
    if os.path.isfile(canonical):
        return recorded_path, "exact", None
    return _resolve_gone(canonical, snapshot)


def _resolve_uncached(raw_path: str, canonical_path: str, conversation_log: Any) -> dict[str, Any]:
    if os.path.isfile(canonical_path):
        return {
            "path": raw_path,
            "exists": True,
            "resolved_path": raw_path,
            "method": "exact",
            "confidence": None,
        }
    snapshot = latest_snapshot(conversation_log, raw_path)
    resolved, method, confidence = _resolve_gone(canonical_path, snapshot)
    return {
        "path": raw_path,
        "exists": False,
        "resolved_path": resolved,
        "method": method,
        "confidence": confidence,
    }


def resolve_path(raw_path: str, canonical_path: str, conversation_log: Any) -> dict[str, Any]:
    """Blocking full resolve for one path → the response-contract dict.

    ``{path, exists, resolved_path, method, confidence}``. The caller MUST have
    already applied the file-read path guard (``hooks.validate_file_path``);
    *canonical_path* is that guard's output and is used for all filesystem / git
    operations, while *raw_path* (what the caller asked for, and the exact
    string recorded in ``file_changes``) is echoed back as ``path`` and matched
    against session history. Results are memoized per canonical path for a short
    TTL. Run this OFF the event loop.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(canonical_path)
        if hit is not None and now - hit[0] < _CACHE_TTL:
            return dict(hit[1])
    result = _resolve_uncached(raw_path, canonical_path, conversation_log)
    with _cache_lock:
        _cache[canonical_path] = (now, dict(result))
        if len(_cache) > _CACHE_MAX:
            for stale in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX // 2]:
                _cache.pop(stale, None)
    return dict(result)
