"""Artifact folder handlers + folder-reference resolution helpers.

Holds folder listing/create/update/delete, the artifact->folder move, the
source_path relocate handler, and the shared folder-reference resolvers
(``_resolve_folder_ref`` / ``_resolve_folder_ref_off_loop`` / ``_set_folder_and_reload``)
which :mod:`.core`\'s create/update handlers import lazily.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.artifacts import (
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    get_default_folder_store,
    get_default_store,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_folders import generate_emoji_for_name
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.executors import subprocess_executor
from kiro_crew.security import is_sensitive_path

from .core import (
    _audit,
    _err,
    _json_response,
    _notify_artifact_update,
    _read_json_body,
    _run_off_loop,
)
from .redaction import _redact_text, _serialize

logger = logging.getLogger(__name__)


def _resolve_folder_ref(ref: Any, *, create_missing: bool) -> tuple[str, str | None]:
    """Resolve a folder reference (id or ``/``-separated human path) to a folder id.

    Returns ``(folder_id, error_message)``. ``None`` / ``""`` / ``"root"`` →
    ``""`` (unfiled/root). When ``create_missing`` is True, missing path
    segments are created (``mkdir -p``); otherwise an unknown path errors.
    """
    if ref is None:
        return "", None
    if not isinstance(ref, str):
        return "", "folder must be a string"
    if len(ref) > 4096:
        return "", "folder reference too long"
    try:
        fid = get_default_folder_store().resolve_path(ref, create_missing=create_missing)
    except ArtifactError as exc:
        # str(exc) can echo the raw LLM-supplied ref (e.g. "folder path not
        # found: <ref>"); redact before it reaches the dashboard via _err().
        return "", _redact_text(str(exc))
    return fid, None


async def _resolve_folder_ref_off_loop(ref: Any, *, create_missing: bool) -> tuple[str, str | None]:
    """Async wrapper for :func:`_resolve_folder_ref`.

    When ``create_missing`` is True the resolver may persist new folders
    (``_save()`` → ``os.fsync``/``os.replace``), which is blocking filesystem
    IO — run it in the shared executor so it never blocks the event loop.
    ``create_missing=False`` is a pure in-memory walk, so it runs inline.
    """
    if not create_missing:
        return _resolve_folder_ref(ref, create_missing=False)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        subprocess_executor(),
        lambda: _resolve_folder_ref(ref, create_missing=True),
    )


def _set_folder_and_reload(slug: str, folder_id: str) -> Any:
    """Move an artifact into a folder and return the reloaded record (blocking)."""
    store = get_default_store()
    store.set_folder(slug, folder_id)
    return store.get(slug)


async def api_artifact_relocate(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/relocate — update source_path."""

    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_relocate",
            request=request,
            outcome="denied",
            error="restricted session",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot relocate artifacts", status=403)

    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))

    source_path = body.get("source_path")
    if source_path is None:
        return _err("source_path is required")
    if not isinstance(source_path, str):
        return _err("source_path must be a string")

    # Validate path. The user-controlled source_path is sanitized BEFORE any
    # filesystem access, in this order (each proves the value safe before it is
    # used in a path expression — this is also what CodeQL's path-injection taint
    # tracker requires as a sanitizer):
    #   1. ".." traversal guard on the raw request value;
    #   2. FIXED-ROOT containment — the resolved path must live under the user's
    #      home dir OR an operator-configured extra root (``publish.relocate_roots``);
    #   3. the ``is_sensitive_path`` denylist inside every allowed root.
    # The root confinement (2) is the barrier that turns relocate from an
    # arbitrary-local-file read primitive (an agent could aim an artifact at
    # /etc/passwd or another user's files, then exfiltrate via a later GET) into a
    # home-confined one, closing the CodeQL alert and the agent-reachable read.
    if source_path:  # non-empty = must exist and be a file
        # Path traversal guard (on the raw request value, before resolution).
        if ".." in Path(source_path).parts:
            _audit(
                tool="artifact_relocate",
                request=request,
                outcome="denied",
                error="path traversal",
                extra={"slug": slug, "source_path": source_path},
            )
            return _err("path traversal not allowed", status=403)
        resolved_path = Path(os.path.expanduser(source_path)).resolve()
        # Fixed-root containment: resolve the allowed roots (home + configured
        # extras) and require the target to be inside one. is_relative_to on the
        # resolved Paths is the sanitizer CodeQL recognizes.
        allowed_roots = [Path.home().resolve()]
        try:
            for extra in KiroCrewConfig.load().publish.relocate_roots:
                if isinstance(extra, str) and extra.strip():
                    allowed_roots.append(Path(os.path.expanduser(extra)).resolve())
        except Exception:
            logger.debug("relocate roots config load failed; home-only", exc_info=True)
        # Fixed-root containment barrier — inlined (NOT via a helper) so CodeQL's
        # intra-procedural taint tracker sees the ``is_relative_to`` sanitizer
        # guarding the SAME ``resolved_path`` that the stat calls below use.
        within_root = False
        for _root in allowed_roots:
            try:
                if resolved_path == _root or resolved_path.is_relative_to(_root):
                    within_root = True
                    break
            except (ValueError, OSError):  # pragma: no cover — defensive
                continue
        if not within_root:
            _audit(
                tool="artifact_relocate",
                request=request,
                outcome="denied",
                error="outside allowed roots",
                extra={"slug": slug, "source_path": source_path},
            )
            return _err(
                "source_path must be inside your home directory " "(or a configured relocate root)",
                status=403,
            )
        # Sensitive-path denylist still applies inside the allowed roots (e.g.
        # ~/.aws, ~/.ssh, ~/.kirocrew keystone).
        if is_sensitive_path(str(resolved_path)):
            _audit(
                tool="artifact_relocate",
                request=request,
                outcome="denied",
                error="sensitive path",
                extra={"slug": slug, "source_path": source_path},
            )
            return _err("cannot point to a sensitive path", status=403)
        # `resolved_path` is now proven under an allowed root AND not sensitive.
        if not resolved_path.exists():
            return _err(f"path does not exist: {source_path}", status=400)
        if resolved_path.is_dir():
            return _err("source_path must be a file, not a directory", status=400)
        source_path = str(resolved_path)

    store = get_default_store()
    try:
        # Blocking store read/write (meta.json + up to 25 MiB current.html) —
        # offload off the event loop.
        await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    await _run_off_loop(lambda: store.relocate(slug, source_path))
    # Reload the full artifact (with content from the new source_path) so the
    # response carries the live file bytes rather than content: null.
    art = await _run_off_loop(lambda: store.get(slug))

    _audit(
        tool="artifact_relocate",
        request=request,
        outcome="success",
        extra={"slug": slug, "source_path": source_path},
    )
    # A source_path swap changes what live reads return (Mesh-2772).
    _notify_artifact_update(state, slug, art.version)
    return _json_response(_serialize(art, include_content=True))


# ── Folders (Mesh-2720) ─────────────────────────────────────────────────────


def _serialize_folder(folder: dict[str, Any], *, path: str | None = None) -> dict[str, Any]:
    """Serialize a folder record; redact the (user/LLM-set) name, icon, and path."""
    out = dict(folder)
    if isinstance(out.get("name"), str) and out["name"]:
        out["name"] = _redact_text(out["name"])
    # icon is LLM-derived (generate_emoji_for_name) or user-supplied (set_icon
    # API) — never trust either on the way back out to the dashboard.
    if isinstance(out.get("icon"), str) and out["icon"]:
        out["icon"] = _redact_text(out["icon"])
    if path is not None:
        out["path"] = _redact_text(path) if path else path
    return out


async def api_artifact_folders(request: web.Request) -> web.Response:
    """GET /api/artifact-folders — list folders enriched with item_count + path."""
    store = get_default_store()
    fstore = get_default_folder_store()
    try:
        # list_with_counts walks every artifact's meta.json (O(N) filesystem
        # scan). Offload it so the dashboard event loop stays responsive —
        # same pattern as api_chat_folders.
        loop = asyncio.get_running_loop()
        folders = await loop.run_in_executor(subprocess_executor(), fstore.list_with_counts, store)
    except (ArtifactError, OSError) as exc:
        logger.warning("artifact folder list failed: %s", exc)
        return _err(str(exc), status=500)
    out = [_serialize_folder(f, path=fstore.breadcrumb(f["id"])) for f in folders]
    return _json_response({"folders": out})


def _spawn_artifact_folder_icon_task(request: web.Request, folder_id: str, name: str) -> None:
    """Fire-and-forget: derive a single-emoji icon for an artifact folder via
    the shared LLM helper (same mechanism as chat-sidebar folders) and store
    it. Best-effort — any failure leaves the folder with the default glyph."""
    state = request.app.get("state")
    if state is None:
        return

    async def _run() -> None:
        try:
            icon = await generate_emoji_for_name(state, name)
            if not icon:
                return
            fstore = get_default_folder_store()
            if fstore.exists(folder_id):
                await _run_off_loop(lambda: fstore.set_icon(folder_id, icon))
        except Exception:  # noqa: BLE001 — best-effort background task
            logger.debug("artifact folder icon generation failed for %s", folder_id, exc_info=True)

    task = asyncio.ensure_future(_run())
    _ARTIFACT_FOLDER_ICON_TASKS.add(task)
    task.add_done_callback(_ARTIFACT_FOLDER_ICON_TASKS.discard)


# Keep strong refs so in-flight icon tasks aren't garbage-collected mid-run.
_ARTIFACT_FOLDER_ICON_TASKS: set[asyncio.Task[None]] = set()


async def api_artifact_folder_create(request: web.Request) -> web.Response:
    """POST /api/artifact-folders — create a folder. Body: {name, parent?|parent_id?}."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_folder_create",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot create folders", status=403)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    name = str(body.get("name") or "").strip()
    if not name:
        return _err("name required")
    # ``parent`` accepts an id OR a human path (mkdir -p); ``parent_id`` is
    # id-only — resolved read-only so a path-looking value can never
    # auto-create folders through the id-only key.
    if "parent" in body:
        parent_id, ferr = await _resolve_folder_ref_off_loop(
            body.get("parent"), create_missing=True
        )
    else:
        parent_id, ferr = _resolve_folder_ref(body.get("parent_id"), create_missing=False)
    if ferr:
        _audit(tool="artifact_folder_create", request=request, outcome="denied", error=ferr)
        return _err(ferr)
    fstore = get_default_folder_store()
    color = str(body.get("color") or "")
    try:
        folder = await _run_off_loop(lambda: fstore.create(name, parent_id=parent_id, color=color))
    except ArtifactValidationError as exc:
        _audit(tool="artifact_folder_create", request=request, outcome="denied", error=str(exc))
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(tool="artifact_folder_create", request=request, outcome="error", error=str(exc))
        return _err(str(exc), status=500)
    # Derive an emoji icon from the name in the background (chat-folder parity).
    _spawn_artifact_folder_icon_task(request, folder["id"], name)
    _audit(
        tool="artifact_folder_create",
        request=request,
        outcome="success",
        extra={"folder_id": folder["id"]},
    )
    return _json_response(
        _serialize_folder(folder, path=fstore.breadcrumb(folder["id"])), status=201
    )


async def api_artifact_folder_update(request: web.Request) -> web.Response:
    """PATCH /api/artifact-folders/{id} — rename / reparent / reorder / icon."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_folder_update",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"folder_id": request.match_info.get("id", "")},
        )
        return _err("restricted session cannot update folders", status=403)
    fid = request.match_info.get("id", "")
    fstore = get_default_folder_store()
    if not fstore.exists(fid):
        return _err("folder not found", status=404)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    folder = fstore.get(fid)
    if folder is None:  # exists() checked above; guards against a concurrent delete
        return _err("folder not found", status=404)

    def _apply_updates() -> dict[str, Any]:
        # Each mutation persists via _save() (fsync/replace) — runs in the
        # executor, off the event loop.
        f = fstore.get(fid)
        if f is None:
            raise ArtifactNotFoundError(f"folder not found: {fid}")
        if "name" in body:
            f = fstore.rename(fid, str(body["name"]))
        if "parent_id" in body:
            f = fstore.reparent(fid, str(body.get("parent_id") or ""))
        if "icon" in body:
            f = fstore.set_icon(fid, str(body.get("icon") or ""))
        if "color" in body:
            f = fstore.set_color(fid, str(body.get("color") or ""))
        if "order" in body:
            fstore.reorder([{"id": fid, "order": int(body["order"])}])
            ref = fstore.get(fid)
            if ref is not None:
                f = ref
        return f

    try:
        updated = await _run_off_loop(_apply_updates)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except (ArtifactValidationError, ValueError, TypeError) as exc:
        _audit(
            tool="artifact_folder_update",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"folder_id": fid},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(
            tool="artifact_folder_update",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"folder_id": fid},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_folder_update", request=request, outcome="success", extra={"folder_id": fid}
    )
    # A rename re-derives the emoji icon from the new name (chat-folder
    # parity) — unless this same request set an explicit icon, which wins.
    if "name" in body and "icon" not in body:
        _spawn_artifact_folder_icon_task(request, fid, str(body["name"]))
    return _json_response(_serialize_folder(updated, path=fstore.breadcrumb(fid)))


async def api_artifact_folder_delete(request: web.Request) -> web.Response:
    """DELETE /api/artifact-folders/{id}?delete_contents=<bool>.

    Default (``delete_contents`` falsy) is the SAFE path: re-parent this
    folder's direct children (folders + artifacts) up to its parent and delete
    only this folder. ``delete_contents=true`` cascades the whole subtree,
    permanently deleting every descendant artifact.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_folder_delete",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"folder_id": request.match_info.get("id", "")},
        )
        return _err("restricted session cannot delete folders", status=403)
    fid = request.match_info.get("id", "")
    fstore = get_default_folder_store()
    if not fstore.exists(fid):
        return _err("folder not found", status=404)
    raw = (request.query.get("delete_contents") or "").strip().lower()
    delete_contents = raw in ("1", "true", "yes")
    try:
        # delete() scans every artifact (O(N)) and, in cascade mode, recursively
        # removes directories — offload off the event loop.
        loop = asyncio.get_running_loop()
        summary = await loop.run_in_executor(
            subprocess_executor(),
            lambda: fstore.delete(
                fid, delete_contents=delete_contents, artifact_store=get_default_store()
            ),
        )
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactError as exc:
        _audit(
            tool="artifact_folder_delete",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"folder_id": fid},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_folder_delete",
        request=request,
        outcome="success",
        extra={
            "folder_id": fid,
            "delete_contents": delete_contents,
            "deleted_artifacts": len(summary.get("deleted_artifact_slugs", [])),
        },
    )
    return _json_response({"ok": True, **summary})


async def api_artifact_set_folder(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/folder — move an artifact into a folder.

    Body accepts ``{folder}`` (id OR human path, mkdir -p) or ``{folder_id}``
    (id-only). ``""`` / ``"root"`` / null unfiles. Metadata-only — no version bump.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot move artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    if "folder" in body:
        folder_id, ferr = await _resolve_folder_ref_off_loop(
            body.get("folder"), create_missing=True
        )
    else:
        folder_id, ferr = _resolve_folder_ref(body.get("folder_id"), create_missing=False)
    if ferr:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error=ferr,
            extra={"slug": slug},
        )
        return _err(ferr)
    # A non-empty id passed directly must reference a real folder.
    if folder_id and not get_default_folder_store().exists(folder_id):
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error="folder not found",
            extra={"slug": slug, "folder_id": folder_id},
        )
        return _err("folder not found", status=400)
    try:
        art = await _run_off_loop(lambda: _set_folder_and_reload(slug, folder_id))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_set_folder",
        request=request,
        outcome="success",
        extra={"slug": slug, "folder_id": folder_id},
    )
    return _json_response(_serialize(art, include_content=True))
