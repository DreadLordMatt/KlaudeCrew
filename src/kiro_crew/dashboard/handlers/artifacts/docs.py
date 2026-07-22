"""Session-document scanning, materialization, and the artifact content cache.

Owns the process-wide content cache (``_content_cache`` + byte budget + lock) and
``_scan_artifacts`` (imported by reference from :mod:`.core`\'s list handler), plus
the session-doc firehose and the /materialize authorization path.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Any

from aiohttp import web

from kiro_crew.artifacts import (
    MAX_CONTENT_BYTES,
    ArtifactError,
    ArtifactValidationError,
    get_default_store,
    is_document_path,
)
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.hooks import (
    FileTooLargeError,
    safe_read_file_bytes_with_identity,
    stat_identity,
)
from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)

from .core import (
    _artifact_source_for_request,
    _audit,
    _clean_origin_session_key,
    _err,
    _json_response,
    _read_json_body,
    _run_off_loop,
)
from .redaction import _context_snippet, _serialize, _snippet_from, _strip_content

logger = logging.getLogger(__name__)


def _load_content(store: Any, slug: str) -> str:
    """Best-effort read of an artifact's current content ('' on any failure)."""
    try:
        return store.get(slug).content or ""
    except (ArtifactError, OSError):
        return ""


def _scan_session_docs(
    conversation_log: Any, saved_map: dict[str, str], session_key: str | None = None
) -> list[dict[str, Any]]:
    """Scan ALL sessions for non-code document file-changes (blocking).

    Returns one entry per distinct document path (latest session wins), each
    ``{path, name, updated_at, session_key, message_ts, saved}``. ``saved`` is
    True when the path already backs a real (materialized) artifact. Sorted
    newest-first. Runs OFF the event loop — reads every session's jsonl.
    """
    best: dict[str, dict[str, Any]] = {}
    try:
        sessions = conversation_log.list_sessions()
    except Exception as exc:  # noqa: BLE001 — a corrupt history dir must not 500 the page
        logger.warning("session-docs: list_sessions failed: %s", exc)
        return []
    for sess in sessions:
        if not isinstance(sess, dict):
            continue  # malformed session entry — skip, never crash the scan
        key = sess.get("key")
        if not key:
            continue
        # When scoping to one session, dashboard slots map to the history key
        # ``dashboard_{slot}`` (see state.py) — accept either form.
        if session_key and key not in (session_key, f"dashboard_{session_key}"):
            continue
        # Untrusted history: coerce the timestamp defensively — a non-numeric or
        # non-finite ``modified`` must not crash the whole scan (it only orders
        # "latest session wins"), so fall back to 0.0.
        try:
            modified = float(sess.get("modified") or 0.0)
        except (TypeError, ValueError):
            modified = 0.0
        if modified != modified or modified in (float("inf"), float("-inf")):  # NaN / inf
            modified = 0.0
        session_title = sess.get("title") or key
        try:
            msgs = conversation_log.read_messages(key)
        except Exception:  # noqa: BLE001 — skip an unreadable session, keep scanning
            continue
        for m in msgs:
            if not isinstance(m, dict):
                continue  # malformed message — skip
            meta = m.get("meta")
            if not isinstance(meta, dict):
                continue  # meta absent or wrong shape — nothing to scan
            file_changes = meta.get("file_changes")
            if not isinstance(file_changes, list):
                continue  # file_changes absent or wrong shape
            for fc in file_changes:
                if not isinstance(fc, dict):
                    continue  # malformed file-change entry — skip
                raw_p = fc.get("path")
                p = (raw_p if isinstance(raw_p, str) else "").strip()
                if not p or not is_document_path(p):
                    continue
                prev = best.get(p)
                if prev is None or modified >= prev["_mtime"]:
                    best[p] = {
                        "path": p,
                        "name": os.path.basename(p) or p,
                        "session_key": key,
                        "session_title": session_title,
                        "message_ts": m.get("ts") or "",
                        "_mtime": modified,
                    }

    out: list[dict[str, Any]] = []

    def _redact(text: str) -> str:
        cleaned, _ = redact_credentials(text or "")
        cleaned, _ = redact_exfiltration_urls(cleaned)
        return cleaned

    for e in sorted(best.values(), key=lambda d: d["_mtime"], reverse=True):
        mt = e.pop("_mtime")
        raw_path = e["path"]
        e["updated_at"] = datetime.fromtimestamp(mt).isoformat() if mt else ""
        # Compute saved/slug against the RAW path (saved_map is keyed by the
        # real source_path) BEFORE redacting the display fields below.
        e["saved"] = raw_path in saved_map
        e["slug"] = saved_map.get(raw_path, "")
        # Redact every display field per the credential/exfiltration blocking
        # rule. Redaction is identity for normal content, so ordinary paths
        # still round-trip through /materialize; a path that actually contains
        # a secret becomes intentionally unmatchable (safe to refuse).
        e["path"] = _redact(raw_path)
        e["name"] = _redact(e["name"])
        e["session_title"] = _redact(e["session_title"])
        out.append(e)
    return out


def _recorded_doc_identities(conversation_log: Any) -> set[tuple[int, int]]:
    """``(st_dev, st_ino)`` identities of documents recorded in ``file_changes``.

    Authorization allowlist for ``/materialize``: only documents the agent
    produced in a chat may be materialized. Matching the ``fstat`` of the
    *opened* descriptor against these identities (rather than re-resolving the
    request path a second time) proves the file actually read is the very inode
    an allowlisted document resolves to right now. A symlink- or directory-swap
    slipped in between ``realpath`` and ``open`` cannot smuggle in a different
    (unauthorized) file, because that file's ``(dev, ino)`` is not in this set —
    the authorized target and the read target are guaranteed identical.

    Identities are resolved through ``hooks.stat_identity`` — the centralized
    sensitive-path gate — so a recorded path that resolves into ``~/.aws`` etc.
    is refused rather than ``stat``'d directly. Only absolute recorded paths are
    trustworthy; a relative path would resolve against the gateway CWD (not the
    session's project) and could match an unrelated same-named file, so relative
    entries are skipped.
    """
    out: set[tuple[int, int]] = set()
    for e in _scan_session_docs(conversation_log, {}):
        expanded = os.path.expanduser(e["path"])
        if not os.path.isabs(expanded):
            continue
        ident = stat_identity(expanded)
        if ident is not None:
            out.add(ident)
    return out


def _materialize_and_pin(
    path: str, conversation_log: Any, source: str = "chat", session_key: str = ""
) -> Any:
    """Create (or reuse) a file-backed artifact from ``path`` and mark it saved.

    Idempotent: if an artifact already backs this path, just pin it. Otherwise
    the file is AUTHORIZED and READ through a single ``O_NOFOLLOW`` descriptor in
    the centralized ``hooks`` chokepoint
    (:func:`hooks.safe_read_file_bytes_with_identity`): the opened inode's
    ``fstat`` identity ``(st_dev, st_ino)`` MUST match a document recorded in the
    chat history's ``file_changes`` (:func:`_recorded_doc_identities`).
    Authorizing the opened descriptor — rather than re-resolving the path a
    second time for the read — closes the symlink/dir-swap TOCTOU window: the
    file we authorize is exactly the inode we read (AWS-33). Sensitive resolved
    targets (``~/.aws`` …) and non-documents are refused up front. Blocking;
    call via ``_run_off_loop``.
    """
    store = get_default_store()
    expanded = os.path.expanduser(path)
    # Reject relative paths — they'd resolve against the gateway CWD rather than
    # the session's project dir, so a same-named unrelated file could satisfy the
    # allowlist. Recorded document paths are absolute; require it.
    if not os.path.isabs(expanded):
        raise ArtifactValidationError("document path must be absolute")
    canonical = os.path.realpath(expanded)
    existing = store.find_by_source_path(path) or store.find_by_source_path(canonical)
    if existing is not None:
        return store.set_pinned(existing.slug, True)
    if not is_document_path(canonical):
        raise ArtifactValidationError("only document files can be saved this way")
    # Defense in depth: a resolved target under a sensitive dir is never a chat
    # document — refuse before opening.
    if is_sensitive_path(canonical):
        raise ArtifactValidationError("path is not a document from your chat history")
    # Authorize AND read through the centralized hooks chokepoint: the helper
    # opens the file ONCE with O_NOFOLLOW and only returns bytes if the opened
    # inode's identity is in the recorded-documents allowlist, so a symlink/dir
    # swap between realpath() and open() cannot substitute an unauthorized file.
    try:
        data = safe_read_file_bytes_with_identity(
            canonical, _recorded_doc_identities(conversation_log)
        )
    except PermissionError as exc:
        raise ArtifactValidationError("path is not a document from your chat history") from exc
    except FileTooLargeError as exc:
        raise ArtifactValidationError(str(exc)) from exc
    if data is None:
        raise ArtifactError("cannot read file")
    # Enforce the artifact content cap up front (store also rejects > 1 MiB) so
    # a large document doesn't waste memory/executor time before create().
    if len(data) > MAX_CONTENT_BYTES:
        raise ArtifactValidationError(f"document exceeds {MAX_CONTENT_BYTES} bytes")
    content = data.decode("utf-8", errors="replace")
    art = store.create(
        name=os.path.basename(canonical) or canonical,
        content=content,
        source=source,
        source_path=canonical,
        session_key=session_key,
    )
    return store.set_pinned(art.slug, True)


# ── List / Create ─────────────────────────────────────────────────────────────


#: Cache of loaded+stripped artifact content, keyed by slug. The cache key
#: tuple is (version, updated_at) — version bumps on every content change,
#: so a stale entry can never be served. Bounded TWO ways: a per-item size cap
#: keeps huge bodies read-through (never cached), and a cumulative byte budget
#: drops the whole cache if churn ever exceeds it (which also ages out entries
#: for deleted artifacts). All access is serialized by
#: :data:`_content_cache_lock` — scans run on executor worker threads, so two
#: concurrent searches would otherwise mutate the dict mid-iteration
#: (guaranteed hazard on free-threaded builds, latent one elsewhere).
_CONTENT_CACHE_MAX_ITEM_BYTES = 256 * 1024
_CONTENT_CACHE_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_content_cache: dict[str, tuple[tuple[int, str], str, str]] = {}
_content_cache_bytes = 0
_content_cache_lock = threading.Lock()


def _cache_entry_bytes(raw: str, stripped: str) -> int:
    return len(raw) + len(stripped)


def _scan_artifacts(
    store: Any,
    items: list[Any],
    q_lower: str,
    want_snippet: bool,
    do_content: bool,
) -> list[dict[str, Any]]:
    """Content-match + snippet scan over listed artifacts.

    Runs OFF the event loop (sync file IO + regex stripping — see the
    run_in_executor call site). Content reads hit a (version, updated_at)-keyed
    cache so repeated queries (every debounced keystroke) only re-read files
    whose content actually changed.
    """
    global _content_cache_bytes
    # No live-slug pruning here: ``items`` may be a FILTERED subset (?tag=,
    # ?kind=, ?folder=), so evicting everything outside it would thrash the
    # cache on scoped queries. The per-item size cap + cumulative byte budget
    # below already bound growth; deleted artifacts' entries age out via the
    # budget's drop-all valve.
    out: list[dict[str, Any]] = []
    need_content = want_snippet or do_content
    for a in items:
        raw = ""
        stripped = ""
        if need_content:
            cache_key = (a.version, a.updated_at)
            with _content_cache_lock:
                hit = _content_cache.get(a.slug)
            if hit and hit[0] == cache_key:
                raw, stripped = hit[1], hit[2]
            else:
                raw = _load_content(store, a.slug)
                stripped = _strip_content(raw)
                size = _cache_entry_bytes(raw, stripped)
                # Oversized bodies stay read-through; everything else is
                # cached under the cumulative byte budget (blown budget =>
                # drop-all, the simple pressure valve for pathological churn).
                if size <= _CONTENT_CACHE_MAX_ITEM_BYTES:
                    with _content_cache_lock:
                        old = _content_cache.get(a.slug)
                        if old:
                            _content_cache_bytes -= _cache_entry_bytes(old[1], old[2])
                        _content_cache[a.slug] = (cache_key, raw, stripped)
                        _content_cache_bytes += size
                        if _content_cache_bytes > _CONTENT_CACHE_MAX_TOTAL_BYTES:
                            _content_cache.clear()
                            _content_cache_bytes = 0
        if do_content:
            hay = f"{a.name} {' '.join(a.tags)} {a.description} {stripped}".lower()
            if q_lower not in hay:
                continue
        d = _serialize(a)
        if want_snippet:
            # Match-centered context for content queries; prefix otherwise.
            d["snippet"] = (
                _context_snippet(raw, q_lower)
                if (do_content and q_lower)
                else _snippet_from(stripped)
            )
        out.append(d)
    return out


async def api_artifact_session_docs(request: web.Request) -> web.Response:
    """GET /api/artifacts/session-docs — virtual list of non-code documents
    produced across all chat sessions (the "All" firehose). Read-only; creates
    no artifact records."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_session_docs",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot list session docs", status=403)
    clog = getattr(state, "conversation_log", None)
    if clog is None:
        _audit(
            tool="artifact_session_docs",
            request=request,
            outcome="success",
            extra={"count": 0},
        )
        return _json_response({"docs": []})
    store = get_default_store()
    session = request.query.get("session") or None

    def work() -> list[dict[str, Any]]:
        # Map pinned artifacts by their backing file path → slug, so each
        # session doc can report saved-status and a slug to unsave against.
        saved_map = {
            a.source_path: a.slug
            for a in store.list()
            if getattr(a, "source_path", "") and getattr(a, "pinned", False)
        }
        return _scan_session_docs(clog, saved_map, session)

    try:
        docs = await _run_off_loop(work)
    except Exception as exc:  # noqa: BLE001 — audit + redacted 500 on any scan/list failure
        _rc, _ = redact_credentials(str(exc))
        safe_err, _ = redact_exfiltration_urls(_rc)
        _audit(
            tool="artifact_session_docs",
            request=request,
            outcome="error",
            error=safe_err,
        )
        return _err("failed to list session documents", status=500)
    _audit(
        tool="artifact_session_docs",
        request=request,
        outcome="success",
        extra={"count": len(docs)},
    )
    return _json_response({"docs": docs})


async def api_artifact_materialize(request: web.Request) -> web.Response:
    """POST /api/artifacts/materialize — turn a session document path into a
    real, saved (pinned) file-backed artifact. Body: ``{"path": "..."}``.
    Idempotent by source_path."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_materialize",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot save artifacts", status=403)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        _audit(tool="artifact_materialize", request=request, outcome="denied", error=str(exc))
        return _err(str(exc))
    path = body.get("path")
    if not isinstance(path, str) or not path.strip():
        _audit(
            tool="artifact_materialize",
            request=request,
            outcome="denied",
            error="path required (must be a string)",
        )
        return _err("path required (must be a string)")
    path = path.strip()
    # Redacted copy for audit/error metadata — never emit a raw (LLM-influenced)
    # path into the SEL audit log (credential/exfiltration redaction rule).
    _rc, _ = redact_credentials(path)
    audit_path, _ = redact_exfiltration_urls(_rc)
    clog = getattr(state, "conversation_log", None)
    if clog is None:
        _audit(
            tool="artifact_materialize",
            request=request,
            outcome="error",
            error="conversation log unavailable",
            extra={"path": audit_path},
        )
        return _err("conversation log unavailable", status=500)
    try:
        art = await _run_off_loop(
            lambda: _materialize_and_pin(
                path,
                clog,
                _artifact_source_for_request(request),
                _clean_origin_session_key(body.get("origin_session_key")),
            )
        )
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_materialize",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"path": audit_path},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(
            tool="artifact_materialize",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"path": audit_path},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_materialize",
        request=request,
        outcome="success",
        extra={"path": audit_path, "slug": art.slug},
    )
    return _json_response(_serialize(art, include_content=True))
