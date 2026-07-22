"""Core artifact CRUD handlers + shared low-level helpers.

Holds the request/response + audit primitives, the late-bound ``sel()`` seam, the
shared ``_run_off_loop`` executor wrapper, and the list/create/read/update/delete/
versions/events/record-event/pin handlers. Imports only :mod:`.redaction` at load
time; the few cross-cluster helpers its handlers call (folder resolution, the
content scan, the publish-governance gate) are imported lazily inside the handlers
to keep the package a load-time DAG.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from aiohttp import web

from kiro_crew import publish_sync
from kiro_crew import sel as _sel_mod
from kiro_crew.artifacts import (
    MAX_CONTENT_BYTES,
    ArtifactAlreadyExistsError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    get_default_store,
    webapp_metadata_from_dict,
)
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.executors import subprocess_executor
from kiro_crew.validation import infer_use_case

from .redaction import (
    _SEARCH_QUERY_MAX_CHARS,
    _redact_audit_metadata,
    _redact_text,
    _resolve_session_title,
    _serialize,
    _validate_inbound_webapp_metadata,
)


def sel():
    """Late-resolved sel() — calls the module function so test patching of
    ``kiro_crew.sel.sel`` (the canonical patch target) continues to work."""
    return _sel_mod.sel()


logger = logging.getLogger(__name__)


# Maximum size of an artifact create/update request body (bytes). Sized to the
# store's content cap (MAX_CONTENT_BYTES = 25 MiB) PLUS headroom for JSON
# envelope overhead (base64/escaping + the other body fields), so content the
# store + validation accept (up to 25 MiB) is never rejected earlier at this
# HTTP boundary. Previously pinned at 2 MiB, which silently became the effective
# ceiling for dashboard/MCP artifact_save/update once the content cap was raised
# 1 MiB -> 25 MiB (the "store enforces a stricter cap" assumption inverted).
_MAX_BODY_BYTES = MAX_CONTENT_BYTES + 8 * 1024 * 1024  # 25 MiB content + 8 MiB envelope headroom


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _notify_artifact_update(state: Any, slug: str, version: int, *, deleted: bool = False) -> None:
    """Best-effort WS broadcast of an artifact content change (Mesh-2772).

    Called from the mutation funnel (create / content update / revert /
    relocate / delete) — the same choke points as the SEL audit, so panel
    chat, other dashboard sessions, Slack, and CLI mutations all emit.
    Fire-and-forget:
    react-query's 30s staleness window remains the safety net if the broadcast
    fails or a client misses it. Known limitation (accepted): external edits to
    a file-backed artifact's source_path never pass through a handler, so those
    stay on pull-based refresh.
    """
    try:
        if state is not None:
            state.push_artifact_update(slug, version, deleted=deleted)
    except Exception:  # pragma: no cover — fire-and-forget by design
        logger.debug("artifact_update broadcast failed for %s", slug, exc_info=True)


async def _read_json_body(request: web.Request) -> dict[str, Any]:
    """Read a JSON body, capped at ``_MAX_BODY_BYTES``."""
    raw = await request.read()
    if len(raw) > _MAX_BODY_BYTES:
        raise ArtifactValidationError(f"request body exceeds {_MAX_BODY_BYTES} bytes")
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ArtifactValidationError("request body must be a JSON object")
    return body


def _session_key(request: web.Request) -> str:
    return request.headers.get("X-Session-Key") or ""


# The artifact ``source`` should reflect WHERE the saving session came from.
# ``infer_use_case`` already classifies a session_key into its origin
# (dashboard / slack / cli / cron / subagent / task-runner / unknown), so we use
# that value directly rather than collapsing everything to a generic "manual".
def _artifact_source_for_request(request: web.Request) -> str:
    """Actual origin of the session saving the artifact (never 'manual')."""
    return infer_use_case(_session_key(request))


#: Safe grammar for a client-supplied originating session key. Session keys are
#: opaque handles like ``chat-2`` / ``dashboard:chat-2`` / ``cron:foo`` / a Slack
#: ``ts``; restrict to that charset so a malformed or hostile value (e.g. a JSON
#: list, or injected markup) can neither poison persisted metadata nor reach the
#: dashboard surface unsanitized.
_SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9:_.\-]{1,128}$")


def _clean_origin_session_key(raw: Any) -> str:
    """Validate a client-supplied ``origin_session_key``.

    Returns the value only when it is a string matching the permitted grammar;
    anything else (non-string, empty, too long, illegal chars) collapses to
    ``""`` so it's simply treated as "no originating session".
    """
    if isinstance(raw, str) and _SESSION_KEY_RE.match(raw):
        return raw
    return ""


def _event_session_id(request: web.Request) -> str | None:
    """Session key for activity-feed events, or None when not a real slot.

    The dashboard's browser client sets X-Session-Key to the literal
    ``dashboard:ui`` for every request — that is not a chat slot a user can
    navigate to, so drop it (same rule as ``api_artifact_update``).
    """
    sk = request.headers.get("X-Session-Key")
    if not sk or sk == "dashboard:ui":
        return None
    return sk


def _audit(
    *,
    tool: str,
    request: web.Request,
    outcome: str,
    extra: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Write a tool-invocation SEL event, redacting caller-supplied text.

    The SEL writer signs bytes as-written and does NOT redact (see
    ``sel.log_governance_decision``'s docstring), so both the ``error`` string
    and every string leaf of ``extra`` are redacted HERE before ``log`` — an
    upstream provider exception can carry a credential or signed URL, and
    ``external_id`` in ``extra`` is provider-controlled. Routing through
    ``redact_via_context`` (not the bare ``_redact_text``) means a loaded
    companion's extra credential/cookie regexes apply to the audit trail too.
    """
    from kiro_crew.platform.context import redact_via_context

    try:
        safe_error = redact_via_context(error) if error else ""
        safe_extra = _redact_audit_metadata(extra) if extra else {}
        sel().log_tool_invocation(
            session_key=_session_key(request),
            source="api",
            tool_name=tool,
            outcome=outcome,
            error=safe_error,
            metadata=safe_extra,
        )
    except Exception:  # pragma: no cover — audit must never break a request
        logger.debug("SEL audit failed for %s", tool, exc_info=True)


async def _run_off_loop(fn):  # type: ignore[no-untyped-def]
    """Run a blocking store call (small filesystem read/write) in the shared
    executor so its ``os.fsync``/``os.replace`` never blocks the event loop.
    Exceptions raised by ``fn`` propagate to the caller unchanged."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(subprocess_executor(), fn)


def _set_pinned_and_reload(slug: str, pinned: bool) -> Any:
    """Set an artifact's pin mark and return the reloaded record (blocking)."""
    store = get_default_store()
    store.set_pinned(slug, pinned)
    return store.get(slug)


async def api_artifacts_list(request: web.Request) -> web.Response:
    # Lazy import breaks the core<->docs load-time cycle: docs.py imports core's
    # helpers at module load, so core reaches back into docs only at call time.
    from .docs import _scan_artifacts

    tag = request.query.get("tag") or None
    kind = request.query.get("kind") or None
    # Bounded: q feeds a substring scan over every artifact's full content —
    # an unbounded query string is free DoS ammunition.
    q = (request.query.get("q") or "")[:_SEARCH_QUERY_MAX_CHARS] or None
    source = request.query.get("source") or None
    source_path = request.query.get("source_path") or None
    want_snippet = (request.query.get("snippet") or "").lower() in ("1", "true", "yes")
    content_match = (request.query.get("content") or "").lower() in ("1", "true", "yes")
    q_lower = (q or "").lower()
    # ?content=1 broadens ?q from a name-only substring to name + tags + content.
    do_content = content_match and bool(q_lower)
    # ``folder`` scopes the browse view to one folder id. Absent = all folders
    # (unscoped); present-but-empty ("?folder=") = the unfiled/root bucket. We
    # must distinguish the two, so read the raw key rather than ``or None``.
    folder = request.query["folder"] if "folder" in request.query else None
    try:
        store = get_default_store()
        items = store.list(
            tag=tag,
            kind=kind,
            # When content-matching, don't let the store's name-only filter
            # exclude content/tag matches — filter in this layer instead.
            name_contains=None if do_content else q,
            source=source,
            source_path=source_path,
            folder=folder,
        )
    except (ArtifactError, OSError) as exc:
        logger.warning("artifact list failed: %s", exc)
        return _err(str(exc), status=500)
    # File reads + regex stripping are sync — keep them off the event loop so
    # a large-library content scan can't stall unrelated requests. Cached
    # content (version-keyed) makes repeated keystroke queries cheap.
    out = await asyncio.get_running_loop().run_in_executor(
        None, _scan_artifacts, store, items, q_lower, want_snippet, do_content
    )
    # Live-resolve each artifact's originating-session title for the Source
    # column (done here, on the event loop, where dashboard ``state`` is
    # available — the off-loop scan is stateless).
    state = request.app.get("state")
    if state is not None:
        for d in out:
            title = _resolve_session_title(state, d.get("session_key") or "")
            if title:
                d["session_title"] = _redact_text(title)
    return _json_response({"artifacts": out})


async def api_artifacts_create(request: web.Request) -> web.Response:
    # Lazy import breaks the core<->folders load-time cycle (see api_artifacts_list).
    from .folders import _resolve_folder_ref_off_loop

    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot create artifacts", status=403)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    # ── Auto-dedup by source_path (Mesh-1654 Phase 6) ─────────────────────
    # When the caller passes a source_path that matches an existing artifact,
    # silently bump the existing one to a new version rather than creating a
    # parallel duplicate. This makes the "Add to artifacts" action on file
    # paths idempotent — clicking it twice on the same file just produces v2,
    # not two separate artifacts. Returns 200 OK on bump (vs 201 Created on
    # genuine new save) so the caller can distinguish if it cares.
    source_path = body.get("source_path") or ""
    if isinstance(source_path, str) and source_path:
        store = get_default_store()
        try:
            existing = store.find_by_source_path(source_path)
        except (ArtifactError, OSError) as exc:
            # find_by_source_path scans meta.json files; on a corrupt store
            # we fall through to the regular create path rather than
            # blocking the save.
            logger.warning("source_path lookup failed: %s", exc)
            existing = None
        if existing is not None:
            # R19 F5: run the SAME validation as the normal save path before
            # the dedup update — the dedup branch previously skipped validation
            # and silently dropped supplied fields.
            merr = _validate_inbound_webapp_metadata(body)
            if merr:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="denied",
                    error=merr,
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                return _err(merr)

            # R19 F5: kind conflict — if caller supplies a different kind than
            # the existing artifact, that's a dedup conflict, not a silent update.
            supplied_kind = body.get("kind")
            if supplied_kind and supplied_kind != existing.kind:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="denied",
                    error="dedup kind conflict",
                    extra={
                        "slug": existing.slug,
                        "source_path": source_path,
                        "existing_kind": existing.kind,
                        "supplied_kind": supplied_kind,
                    },
                )
                return _err(
                    f"source_path dedup conflict: existing artifact '{existing.slug}' "
                    f"has kind='{existing.kind}' but request supplies kind='{supplied_kind}'. "
                    f"Use artifact_update to change kind explicitly.",
                    status=409,
                )

            # Same auth-based actor inference as api_artifact_update — if the
            # caller is MCP (X-Internal-Secret header), the lifecycle event
            # gets tagged 'iterated' (agent), not 'edited' (user). Without
            # this, MCP-driven re-saves would silently misattribute on the
            # activity timeline.
            is_mcp = request.headers.get("X-Internal-Secret") is not None
            actor = "agent" if is_mcp else "user"

            # R19 F5: pass through ALL supported fields (not just content).
            update_kwargs: dict[str, Any] = {
                "content": body.get("content"),
                "actor": actor,
                "snapshot": True,
            }
            if body.get("name"):
                update_kwargs["name"] = body["name"]
            if body.get("tags") is not None:
                update_kwargs["tags"] = body["tags"]
            if body.get("description") is not None:
                update_kwargs["description"] = body["description"]
            wm = body.get("webapp_metadata")
            if wm is not None:
                update_kwargs["webapp_metadata"] = webapp_metadata_from_dict(wm)
            try:
                art = store.update(existing.slug, **update_kwargs)
            except ArtifactValidationError as exc:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="denied",
                    error=str(exc),
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                return _err(str(exc))
            except ArtifactError as exc:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="error",
                    error=str(exc),
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                return _err(str(exc), status=500)
            _audit(
                tool="artifact_save",
                request=request,
                outcome="success",
                extra={
                    "slug": art.slug,
                    "kind": art.kind,
                    "version": art.version,
                    "deduped": True,
                },
            )
            # 200 OK signals "bumped existing"; the create path below returns 201.
            _notify_artifact_update(state, art.slug, art.version)
            return _json_response(_serialize(art, include_content=True), status=200)
    # Resolve an optional folder placement (id or human path; mkdir -p missing
    # segments) so a save can file the artifact in one call (Mesh-2720). Off the
    # event loop — mkdir -p may persist new folders (blocking fsync).
    merr = _validate_inbound_webapp_metadata(body)
    if merr:
        _audit(tool="artifact_save", request=request, outcome="denied", error=merr)
        return _err(merr)
    folder_id, ferr = await _resolve_folder_ref_off_loop(body.get("folder"), create_missing=True)
    if ferr:
        _audit(tool="artifact_save", request=request, outcome="denied", error=ferr)
        return _err(ferr)
    try:
        art = get_default_store().create(
            name=body.get("name", ""),
            content=body.get("content", ""),
            slug=body.get("slug"),
            kind=body.get("kind"),
            # Honor an explicitly-supplied source (MCP tool / import path); for
            # UI saves that omit it, derive the ACTUAL session origin
            # (dashboard/slack/cli/cron/subagent/...) rather than "manual".
            source=(body.get("source") or _artifact_source_for_request(request)),
            description=body.get("description", ""),
            tags=body.get("tags") or [],
            source_path=body.get("source_path", ""),
            folder_id=folder_id,
            # Originating chat session for the Source column. Only a real slot
            # key that passes the permitted grammar is stored (validated to
            # prevent attribution spoofing / metadata poisoning); anything else
            # collapses to "".
            session_key=_clean_origin_session_key(body.get("origin_session_key")),
            webapp_metadata=webapp_metadata_from_dict(body.get("webapp_metadata")),
        )
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error=str(exc),
        )
        return _err(str(exc))
    except ArtifactAlreadyExistsError as exc:
        # Explicit slug collision — semantically a 409 Conflict (the resource
        # already exists). Distinct from base ArtifactError fallback below
        # which catches store-level refusals (sensitive-path, write failure)
        # and returns 500.
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error=str(exc),
        )
        return _err(str(exc), status=409)
    except ArtifactError as exc:
        # Base-class fallback — store._write_text() can raise ArtifactError
        # ("refusing to write sensitive path: ...") after the duplicate-slug
        # check passes. Returning 409 there would be wrong; this is a server
        # error, not a conflict. Mirrors the pattern in api_artifact_update
        # and api_artifact_delete.
        _audit(
            tool="artifact_save",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": body.get("slug", "")},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_save",
        request=request,
        outcome="success",
        extra={"slug": art.slug, "kind": art.kind, "version": art.version},
    )
    # New library entries appear live in every open window (Mesh-2772).
    _notify_artifact_update(state, art.slug, art.version)
    return _json_response(_serialize(art, include_content=True), status=201)


# ── Item: read / update / delete ──────────────────────────────────────────────


async def api_artifact_detail(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    try:
        art = get_default_store().get(slug)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response(_serialize(art, include_content=True, state=request.app.get("state")))


async def api_artifact_update(request: web.Request) -> web.Response:
    # Lazy imports break the core<->folders/publishing load-time cycles (see
    # api_artifacts_list).
    from .folders import _resolve_folder_ref_off_loop, _set_folder_and_reload
    from .publishing import _publish_governance_denied

    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_update",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot update artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    try:
        # Determine actor authoritatively from how the request was authed,
        # NOT from the body. MCP-originated calls carry X-Internal-Secret
        # (validated by upstream middleware before we see them); browser
        # dashboard calls don't. Tagging by auth method is both more
        # accurate (the agent's MCP layer doesn't have to remember to set
        # actor='agent') and more secure (a body field could be spoofed).
        is_mcp = request.headers.get("X-Internal-Secret") is not None
        actor = "agent" if is_mcp else "user"
        # Session correlation: MCP calls carry X-Session-Key with a real
        # chat-slot key; the dashboard's browser client sets it to the
        # literal "dashboard:ui" for every request (see api/client.ts) which
        # is NOT a slot the user can navigate to. Drop it so the activity
        # timeline doesn't render a broken "from session dashboard:ui" link.
        session_id_hdr = request.headers.get("X-Session-Key")
        if session_id_hdr == "dashboard:ui":
            session_id_hdr = None
        # Snapshot semantics (Mesh-1654 round 5): saves don't bump version
        # by default — that's the user's "save while editing" path. Agent
        # updates via MCP always snapshot (each iteration is a meaningful
        # state change worth versioning, like a git commit). The dashboard
        # can also explicitly request a snapshot via ``snapshot: true`` in
        # the body (the "Snapshot" button next to Save).
        raw_snapshot = body.get("snapshot")
        if raw_snapshot is None:
            snapshot = is_mcp  # MCP defaults to True; dashboard defaults to False.
        else:
            snapshot = bool(raw_snapshot)
        merr = _validate_inbound_webapp_metadata(body)
        if merr:
            _audit(tool="artifact_update", request=request, outcome="denied", error=merr)
            return _err(merr)
        # event_type / from_version overrides — used by the revert flow to
        # mark its update as ``reverted`` (with the source version pinned)
        # rather than the default ``edited``. Validation lives in
        # store.update() — invalid values raise ArtifactValidationError →
        # 400 below. Reverts always snapshot regardless of the snapshot
        # flag because the entire point is to record the rollback.
        raw_event_type = body.get("event_type")
        event_type = raw_event_type if isinstance(raw_event_type, str) and raw_event_type else None
        if event_type == "reverted":
            snapshot = True
        raw_from_version = body.get("from_version")
        try:
            from_version = int(raw_from_version) if raw_from_version is not None else None
        except (TypeError, ValueError):
            from_version = None
        art = get_default_store().update(
            slug,
            content=body.get("content"),
            description=body.get("description"),
            tags=body.get("tags"),
            name=body.get("name"),
            webapp_metadata=webapp_metadata_from_dict(body.get("webapp_metadata")),
            actor=actor,
            session_id=session_id_hdr,
            event_type=event_type,
            from_version=from_version,
            snapshot=snapshot,
        )
        # store.update() only loads content into the returned Artifact when
        # the caller passed new content (because that path is on the write
        # branch of the store). For metadata-only updates the returned
        # Artifact has content=None, which then serializes as "content": null
        # in the response — inconsistent with api_artifact_detail which
        # always returns the actual content. Refetch in that case so the MCP
        # tool / dashboard caller always sees a populated content field.
        if art.content is None:
            art = get_default_store().get(slug)
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_update",
            request=request,
            outcome="error",
            error=str(exc),
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_update",
            request=request,
            outcome="denied",
            error=str(exc),
        )
        return _err(str(exc))
    except ArtifactError as exc:
        # Catches the base class fallback — store._write_text() raises
        # ArtifactError("refusing to write sensitive path: ...") which is
        # neither ArtifactNotFoundError nor ArtifactValidationError. Without this
        # branch the request would 500 with no audit trail.
        _audit(
            tool="artifact_update",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    # Optional folder placement (Mesh-2720). Metadata-only — does not bump the
    # version. The dedicated PATCH /folder route is the canonical path; this
    # honours a ``folder`` key on the generic update for convenience/parity.
    if "folder" in body:
        folder_id, ferr = await _resolve_folder_ref_off_loop(
            body.get("folder"), create_missing=True
        )
        if ferr:
            _audit(
                tool="artifact_update",
                request=request,
                outcome="denied",
                error=ferr,
                extra={"slug": slug},
            )
            return _err(ferr)
        try:
            art = await _run_off_loop(lambda: _set_folder_and_reload(slug, folder_id))
        except ArtifactError as exc:
            _audit(
                tool="artifact_update",
                request=request,
                outcome="error",
                error=str(exc),
                extra={"slug": slug, "folder_id": folder_id},
            )
            return _err(str(exc), status=500)
    # SEL audit for the mutation. When this update also placed the artifact in
    # a folder, the audit must carry the folder context (security guideline:
    # permission-relevant mutations audit their full effect).
    _success_extra: dict[str, Any] = {"slug": art.slug, "version": art.version}
    if "folder" in body:
        _success_extra["folder_id"] = art.folder_id
    _audit(
        tool="artifact_update",
        request=request,
        outcome="success",
        extra=_success_extra,
    )
    # Live refresh (Mesh-2772): broadcast only when the artifact's content
    # actually changed — a content-carrying PATCH (Save / Snapshot / MCP
    # artifact_update) or a revert (event_type="reverted" is a content
    # rollback even when the body carries no content field). Metadata-only
    # updates (rename / retag / description / folder) don't move content, so
    # open views have nothing to re-render.
    content_changed = body.get("content") is not None or event_type == "reverted"
    if content_changed:
        _notify_artifact_update(state, art.slug, art.version)
    # Auto-sync egress: a snapshot that bumped the version on an artifact
    # published with ``auto_sync`` (the state a bidirectional ``clone`` arms)
    # pushes the new version to the remote — this is the leg that makes clone
    # actually bidirectional. Gated through the SAME ``capabilities.publish``
    # ceiling the clone passed, so a governance-denied surface can edit locally
    # but never egresses. Best-effort: a push failure must not fail the local
    # save (the version is already durable); the next snapshot retries.
    # Inert in the public edition (empty registry -> push_version_by_slug's
    # provider resolve raises PublishUnavailableError, swallowed here).
    if content_changed and snapshot and art.publication is not None and art.publication.auto_sync:
        if _publish_governance_denied(request, art.publication.provider) is None:
            try:
                await publish_sync.push_version_by_slug(art.slug)
            except Exception as exc:  # noqa: BLE001 - best-effort egress
                logger.info("auto-sync push after snapshot failed for %s: %s", art.slug, exc)
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_delete(request: web.Request) -> web.Response:
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot delete artifacts", status=403)
    slug = request.match_info.get("slug", "")
    # Capture the pre-delete version so the deleted-variant WS event carries the
    # last-known version (Mesh-2772). The upstream cleanup block that fetched
    # this was tied to the removed Artifactory path, so fetch it here directly.
    try:
        _existing = get_default_store().get(slug)
    except ArtifactError:
        # Best-effort version capture only — swallow both the missing-slug and
        # invalid-slug (ArtifactValidationError) siblings so an invalid slug still
        # reaches the delete() call below, which returns a clean 4xx (a bare
        # ArtifactNotFoundError catch here would leak ArtifactValidationError as a 500).
        _existing = None
    try:
        get_default_store().delete(slug)
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        # Base-class fallback — defends against any ArtifactError subclass
        # not specifically handled above (e.g. future store-level errors).
        # Without this branch the request would 500 with no audit trail.
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_delete",
        request=request,
        outcome="success",
        extra={"slug": slug},
    )
    # Deleted variant (Mesh-2772): open views of this slug toast + leave.
    _notify_artifact_update(
        state, slug, _existing.version if _existing is not None else 0, deleted=True
    )
    return _json_response({"ok": True})


# ── Versions ─────────────────────────────────────────────────────────────────


async def api_artifact_versions(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    try:
        versions = get_default_store().list_versions(slug)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response({"slug": slug, "versions": versions})


async def api_artifact_version_detail(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    version_str = request.match_info.get("version", "")
    try:
        version = int(version_str)
    except ValueError:
        return _err(f"invalid version: {version_str}")
    try:
        art = get_default_store().get(slug, version=version)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response(_serialize(art, include_content=True))


# ── Lifecycle events ─────────────────────────────────────────────────────────


async def api_artifact_events(request: web.Request) -> web.Response:
    """Return the lifecycle event log for an artifact.

    Triggers the lazy backfill in ``store.get`` for legacy artifacts that
    pre-date the events field, so the activity timeline is never empty for
    a real artifact (the fallback synthesizes ``created`` / ``edited`` from
    ``created_at`` / ``updated_at``).
    """
    slug = request.match_info.get("slug", "")
    try:
        art = get_default_store().get(slug)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response({"slug": art.slug, "events": list(art.events)})


async def api_artifact_record_event(request: web.Request) -> web.Response:
    """Record an impression-style lifecycle event without modifying content.

    Currently only ``referenced`` events go through this endpoint —
    ``WidgetFrame`` posts here when each chat impression mounts so the
    activity timeline can show "this artifact was referenced N times
    across M sessions". Other event types (``created``, ``edited``,
    ``iterated``, ``reverted``) are emitted internally by the store as a
    side effect of the corresponding mutation; only ``referenced`` is a
    pure annotation that doesn't change content/version, which is why it
    needs a dedicated endpoint.

    Auth: same X-Internal-Secret + X-Session-Key model as the rest of
    the artifacts API. Browser-originated requests get ``by='user'``;
    MCP-originated requests get ``by='agent'``. Session ID is taken
    from the X-Session-Key header (with the literal ``dashboard:ui``
    dropped — same rule as other handlers).

    Appending events mutates ``meta.json``, so this is gated behind the
    same deny-by-default ``_is_restricted_session`` check as the other
    mutation endpoints — a restricted session must not be able to flood
    an artifact's event log.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_reference",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot record artifact events", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    event_type = body.get("type")
    # Restrict to ``referenced`` for now — the other event types must
    # come from the mutation paths so version-bump bookkeeping and
    # snapshot creation stay coupled to actual content changes.
    # Callers passing anything else are likely confused; reject loudly.
    if event_type != "referenced":
        return _err(
            "this endpoint only accepts type='referenced'; "
            "use POST /api/artifacts (create), PATCH /api/artifacts/{slug} "
            "(update / iterate / revert) for content-mutating events"
        )
    is_mcp = request.headers.get("X-Internal-Secret") is not None
    actor = "agent" if is_mcp else "user"
    session_id_hdr = request.headers.get("X-Session-Key")
    if session_id_hdr == "dashboard:ui":
        session_id_hdr = None
    raw_metadata = body.get("metadata") or {}
    if not isinstance(raw_metadata, dict):
        return _err("metadata must be an object")
    message_ts = raw_metadata.get("message_ts")
    widget_index = raw_metadata.get("widget_index")
    # Light type coercion at the boundary — store-side _append_event
    # also defends, but failing fast with a clear 400 is friendlier
    # than a silent metadata drop.
    if message_ts is not None and not isinstance(message_ts, str):
        return _err("metadata.message_ts must be a string")
    if widget_index is not None and not isinstance(widget_index, int):
        return _err("metadata.widget_index must be an integer")
    try:
        art, appended = get_default_store().record_impression(
            slug,
            by=actor,
            session_id=session_id_hdr,
            message_ts=message_ts,
            widget_index=widget_index,
        )
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    except (ArtifactError, OSError) as exc:
        logger.warning("record_impression failed for %s: %s", slug, exc)
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_reference",
        request=request,
        outcome="ok",
        extra={"slug": art.slug, "suppressed": not appended},
    )
    # When the impression was suppressed (the session already has a CUD
    # event on this artifact) no `referenced` event was appended, so
    # `art.events[-1]` would be an unrelated prior event. Signal the
    # suppression explicitly rather than echoing a misleading payload.
    if not appended:
        return _json_response({"slug": art.slug, "event": None, "suppressed": True})
    # Return only the latest event entry — the full event log can be
    # fetched via the GET endpoint. Keeps this response small for the
    # high-frequency impression-logging case.
    latest = art.events[-1] if art.events else None
    return _json_response({"slug": art.slug, "event": latest})


async def api_artifact_set_pinned(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/pin — set/clear an artifact's pin mark.

    Body: ``{"pinned": true|false}``. Metadata-only — no version bump.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_set_pinned",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot pin artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_set_pinned",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    raw_pinned = body.get("pinned")
    if not isinstance(raw_pinned, bool):
        _audit(
            tool="artifact_set_pinned",
            request=request,
            outcome="denied",
            error="'pinned' must be a boolean",
            extra={"slug": slug},
        )
        return _err("'pinned' must be a boolean (true or false)")
    pinned = raw_pinned
    try:
        art = await _run_off_loop(lambda: _set_pinned_and_reload(slug, pinned))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_set_pinned",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactError as exc:
        _audit(
            tool="artifact_set_pinned",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_set_pinned",
        request=request,
        outcome="success",
        extra={"slug": slug, "pinned": pinned},
    )
    return _json_response(_serialize(art, include_content=True))
