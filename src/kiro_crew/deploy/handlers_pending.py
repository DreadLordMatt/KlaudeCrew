"""deploy-web pending-confirmation handlers (F6).

List / confirm / dismiss for pending deploy confirmations. Confirm re-runs
``core._do_deploy`` with the stored params plus ``confirm=true`` after
re-validating profile identity and content digest against what was previewed.
Imports guards from ``internal_guard`` and leaves from ``redaction`` / ``config``
/ ``staging`` plus ``core`` — never the ``handlers`` shim.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.deploy.config import _ProfileResolveError, _resolve_profile
from kiro_crew.deploy.core import _do_deploy
from kiro_crew.deploy.internal_guard import (
    _deny_restricted,
    _internal_denied,
    _is_internal_secret_request,
    _json_body,
)
from kiro_crew.deploy.redaction import (
    _audit,
    _redact_pending_entries,
    _sanitize_response,
)
from kiro_crew.deploy.staging import (
    _allowed_local_roots,
    _compute_content_digest,
    _compute_tree_size_global,
    _dir_contains_sensitive,
    _stage_artifact_html,
)

try:
    from kiro_crew.artifacts import get_default_store
    _HAS_ARTIFACTS = True
except ImportError:  # pragma: no cover - defensive
    _HAS_ARTIFACTS = False


# ── Pending confirmations (F6) ──────────────────────────────────────────────


@_internal_denied
async def _handle_pending_list(request: web.Request) -> web.Response:
    """GET /api/deploy/pending — list pending deploy confirmations.

    Only cookie/token-authenticated callers allowed (rejects internal-secret).
    All string values are recursively redacted (F2 R8) to prevent credential
    leakage through LLM-controlled scan_summary/local_dir/profile fields.
    """
    from kiro_crew.deploy.pending import list_pending
    entries = await asyncio.to_thread(list_pending)
    return web.json_response({"pending": _redact_pending_entries(entries)})


@_internal_denied
async def _handle_pending_confirm(request: web.Request) -> web.Response:
    """POST /api/deploy/pending/{id}/confirm — execute a pending deploy.

    Re-runs _do_deploy with stored params + confirm=true. Only cookie/token-auth.
    Uses atomic claim_pending to prevent double-deploy from concurrent confirms.
    Validates that the profile and content have not changed since preview (F1 R8).
    """
    denied = _deny_restricted(request, "pending_confirm")
    if denied:
        return denied
    entry_id = request.match_info["id"]
    from kiro_crew.deploy.pending import add_pending, claim_pending
    entry = await asyncio.to_thread(claim_pending, entry_id)
    if not entry:
        _audit("pending_confirm", entry_id, "denied",
               error="not found, expired, or already claimed")
        return web.json_response(
            {"error": "pending entry not found, expired, or already claimed"}, status=409
        )

    # F1 (R8): staleness check — re-resolve profile and compare to stored canonical.
    stored_profile = entry.get("profile", "")
    stored_region = entry.get("region", "")
    try:
        current_profile, current_region = await _resolve_profile(
            {"profile": stored_profile}
        )
    except _ProfileResolveError:
        current_profile, current_region = "", ""
    if stored_profile and (current_profile != stored_profile or current_region != stored_region):
        # Profile resolution changed — re-add entry and reject.
        await asyncio.to_thread(add_pending, entry)
        _audit("pending_confirm", entry_id, "denied",
               error="profile drift since preview")
        return web.json_response(
            {"error": "profile changed since preview — re-run preview to confirm with the "
             "current configuration"},
            status=409,
        )

    # F1 (R8/R9): content digest staleness check for local_dir deploys.
    # Re-validate path confinement + sensitive-path + size guard at confirm time
    # (the directory could have been swapped or grown between preview and confirm).
    stored_digest = entry.get("content_digest", "")
    if stored_digest and entry.get("local_dir"):
        local_dir = entry["local_dir"]
        local_path = Path(os.path.normpath(os.path.expanduser(local_dir)))

        # F2 (R11): consolidate ALL blocking confinement pre-checks into ONE
        # asyncio.to_thread hop (is_dir + resolve + allowed-roots + sensitive).
        def _confirm_confinement_check(
            lp: Path,
        ) -> str | None:
            """Blocking: validate directory exists, is confined, and is clean.

            Returns None on success; returns an error string on failure.
            """
            if not lp.is_dir():
                return "local_dir no longer exists — re-run preview"
            resolved = lp.resolve()
            resolved_str = os.path.normpath(str(resolved))
            roots = _allowed_local_roots()
            allowed = any(
                resolved_str == os.path.normpath(str(r))
                or resolved_str.startswith(os.path.normpath(str(r)) + os.sep)
                for r in roots
            )
            if not allowed:
                return "local_dir outside allowed roots at confirm time"
            if _dir_contains_sensitive(lp, resolved):
                return "local_dir contains sensitive paths at confirm time"
            return None

        confinement_error = await asyncio.to_thread(_confirm_confinement_check, local_path)
        if confinement_error:
            await asyncio.to_thread(add_pending, entry)
            _audit("pending_confirm", entry_id, "denied",
                   error=confinement_error)
            return web.json_response({"error": confinement_error}, status=409)

        # Size guard (same _MAX_STAGE_BYTES as deploy path).
        _MAX_STAGE_BYTES_CONFIRM = 200 * 1024 * 1024
        tree_size = await asyncio.to_thread(_compute_tree_size_global, local_path)
        if tree_size > _MAX_STAGE_BYTES_CONFIRM:
            await asyncio.to_thread(add_pending, entry)
            _audit("pending_confirm", entry_id, "denied",
                   error=f"size guard exceeded ({tree_size} bytes)")
            return web.json_response(
                {"error": f"local_dir tree exceeds 200 MiB at confirm time ({tree_size} bytes)"},
                status=409,
            )

        # R17 F2: the early live-path digest read (Path.read_bytes on an
        # LLM-controlled path, racy vs staging) is removed — _do_deploy
        # verifies expected_content_digest against the SAFELY STAGED snapshot
        # (symlink-free, hooks-gated), which is the authoritative check.

    # R10 F3: content-digest staleness check for ARTIFACT deploys too — the
    # artifact can be edited (artifact_update) between preview and confirm,
    # deploying different content than was approved. Re-stage the CURRENT
    # artifact content and compare its digest with the one stored at preview.
    if stored_digest and entry.get("artifact_slug") and _HAS_ARTIFACTS:
        def _current_artifact_digest(slug: str) -> str | None:
            """Blocking: stage current artifact content, digest it, clean up."""
            try:
                art = get_default_store().get(slug)
                _f, staged, _b = _stage_artifact_html(
                    art.kind, art.content or "", art.name or "")
            except Exception:
                return None  # deleted / not stageable — let _do_deploy report it
            try:
                return _compute_content_digest(staged)
            finally:
                shutil.rmtree(staged, ignore_errors=True)

        current = await asyncio.to_thread(
            _current_artifact_digest, entry["artifact_slug"])
        if current is not None and current != stored_digest:
            await asyncio.to_thread(add_pending, entry)
            _audit("pending_confirm", entry_id, "denied",
                   error="content digest mismatch (artifact modified)")
            return web.json_response(
                {"error": "content changed since preview — the artifact was "
                 "modified after approval; re-run preview"},
                status=409,
            )

    # Build params from stored entry and force confirm=true
    params: dict[str, Any] = {
        "site_id": entry.get("site_id", ""),
        "confirm": True,
    }
    if entry.get("artifact_slug"):
        params["artifact_slug"] = entry["artifact_slug"]
    if entry.get("local_dir"):
        params["local_dir"] = entry["local_dir"]
    if entry.get("profile"):
        params["profile"] = entry["profile"]
    if entry.get("region"):
        params["region"] = entry["region"]
    if entry.get("ttl_hours") is not None:
        params["ttl_hours"] = entry["ttl_hours"]
    # R18 F5: bind the stored (previewed) identity so _do_deploy's own
    # comparison (R17 F4) closes the registry-change race after approval.
    if entry.get("profile"):
        params["expected_profile"] = entry["profile"]
    if entry.get("region"):
        params["expected_region"] = entry["region"]
    # R16 F2: pass expected_content_digest into _do_deploy so the staged
    # snapshot is verified at deploy time (TOCTOU between handler check above
    # and _do_deploy's own staging). The handler's early check is fast-fail;
    # this param is the authoritative content-integrity gate.
    if stored_digest:
        params["expected_content_digest"] = stored_digest
    # R24: entries flagged override_scan_required were scan-blocked at preview
    # by OVERRIDABLE (non-credential) findings. Confirming them requires the
    # human to send override_scan=true explicitly (the "deploy anyway" action
    # in the dashboard) — otherwise _do_deploy re-blocks on the same findings.
    # This handler is @_internal_denied, so the flag can only originate from a
    # cookie/token-authenticated human, never from the MCP caller.
    if entry.get("override_scan_required"):
        body = await _json_body(request)
        if body.get("override_scan") is True:
            params["override_scan"] = True
            _audit("pending_confirm", entry_id, "allowed",
                   error="human override_scan on non-credential findings")
    status, payload = await _do_deploy(params)
    if status != 200 or payload.get("requires_confirm"):
        # Deploy failed — re-add entry so user can retry
        await asyncio.to_thread(add_pending, entry)
    return web.json_response(_sanitize_response(payload), status=status)


@_internal_denied
async def _handle_pending_dismiss(request: web.Request) -> web.Response:
    """POST /api/deploy/pending/{id}/dismiss — discard a pending deploy.

    Only cookie/token-authenticated callers allowed (rejects internal-secret).
    Restricted sessions are denied. Audit trail recorded.
    """
    if _is_internal_secret_request(request):
        _audit("pending_dismiss", request.match_info.get("id", ""), "denied",
               error="internal-secret caller")
        return web.json_response({"error": "forbidden for MCP callers"}, status=403)
    denied = _deny_restricted(request, "pending_dismiss")
    if denied:
        return denied
    entry_id = request.match_info["id"]
    from kiro_crew.deploy.pending import remove_pending
    removed = await asyncio.to_thread(remove_pending, entry_id)
    if not removed:
        _audit("pending_dismiss", entry_id, "not_found")
        return web.json_response({"error": "pending entry not found or expired"}, status=404)
    _audit("pending_dismiss", entry_id, "ok")
    return web.json_response({"ok": True})
