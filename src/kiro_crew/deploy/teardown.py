"""deploy-web teardown: reaper precondition, S3 manifest expiry, teardown handler.

Owner-triggered cancel of a ``kind="webapp"`` artifact — tombstones the
artifact and expires its S3 deploy manifest so the in-account reaper removes
the infrastructure. Imports guards from ``internal_guard`` and leaves from
``redaction`` / ``staging``; profile/validation imports inside ``_handle_teardown``
are kept lazy (as in the pre-split module).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew.deploy import engine
from kiro_crew.deploy import profiles as profiles_mod
from kiro_crew.deploy.internal_guard import _deny_restricted, _internal_denied
from kiro_crew.deploy.redaction import _audit
from kiro_crew.deploy.staging import _REGION_SPEC
from kiro_crew.validation import ValidationError, validate_field

try:
    from kiro_crew.artifacts import (
        ArtifactError,
        ArtifactNotFoundError,
        ArtifactValidationError,
        get_default_store,
    )
    _HAS_ARTIFACTS = True
except ImportError:  # pragma: no cover - defensive
    _HAS_ARTIFACTS = False

logger = logging.getLogger(__name__)


def _check_reaper_installed(profile: str, region: str) -> bool:
    """Check if the kirocrew-deploy-reaper stack exists in the account.

    Returns True if describe-stacks succeeds (stack exists), False otherwise.
    Blocking — must be called via to_thread.
    """
    rc, _out, _err = engine.run_aws(
        ["cloudformation", "describe-stacks",
         "--stack-name", "kirocrew-deploy-reaper",
         "--region", region],
        profile, 10,
    )
    return rc == 0


async def _expire_manifest_best_effort(art: Any) -> str:
    """Best-effort rewrite of the S3 deploy manifest to expires_at=now.

    Uses the artifact's recorded profile and region via the same engine.run_aws
    path that the deploy itself uses. Returns one of:
    - "expired-now": manifest rewritten, reaper collects on next sweep
    - "skipped": the metadata records NO usable deploy identity (no
      deploy_target/profile/slug) — there is nothing to expire; the caller may
      tombstone (R39 F2: this is distinct from a failed write)
    - "unreachable": a usable identity exists but the expiry write FAILED —
      the caller must fail retryably instead of tombstoning, or a finite-TTL
      deployment would keep running until its original TTL while the UI
      reports success

    Security: profile and region are validated through the registry before use —
    a forged metadata.slug or metadata.profile cannot inject unvalidated input
    into subprocess argv.
    """
    meta = art.webapp_metadata
    if meta is None:
        return "skipped"

    # ALWAYS use the validated artifact slug, never metadata slug (which could
    # be forged to target a different deployment's manifest).
    slug = art.slug
    if not slug:
        return "skipped"

    # R39 F2: no deploy_target or no recorded profile means the artifact never
    # captured a usable deploy identity — there is no manifest we could expire.
    if meta.deploy_target is None or not meta.deploy_target.profile:
        return "skipped"

    # Validate the profile through the registry — if the metadata records a
    # profile that no longer exists (or was never registered), refuse to execute
    # aws CLI with unvalidated input.
    raw_profile = meta.deploy_target.profile
    raw_region = meta.deploy_target.region or engine.DEFAULT_REGION

    # Registry check: the profile must be a currently-registered name.
    reg = await asyncio.to_thread(profiles_mod.load_registry)
    entry = profiles_mod.get_entry(reg, raw_profile) if raw_profile else None
    if entry is None:
        _audit("teardown-manifest", slug, "unreachable",
               error=f"profile '{raw_profile}' not in registry")
        return "unreachable"
    profile = entry["name"]

    # Validate region with existing spec before passing to subprocess.
    try:
        region = validate_field(raw_region, _REGION_SPEC)
    except ValidationError:
        _audit("teardown-manifest", slug, "unreachable",
               error=f"invalid region '{raw_region}'")
        return "unreachable"

    # Find the bucket from the base stack (same pattern as deploy.sh).
    try:
        rc, out, _ = await asyncio.to_thread(
            engine.run_aws,
            ["cloudformation", "describe-stacks",
             "--stack-name", "kirocrew-deploy-base",
             "--query", "Stacks[0].Outputs", "--output", "json",
             "--region", region],
            profile, 15,
        )
        if rc != 0 or not out:
            logger.warning("teardown manifest-expiry: cannot read base stack (rc=%d)", rc)
            _audit("teardown-manifest", slug, "unreachable", error="base stack read failed")
            return "unreachable"
        outputs = {o["OutputKey"]: o["OutputValue"] for o in json.loads(out)}
        bucket = outputs.get("BucketName", "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("teardown manifest-expiry: %s", exc)
        _audit("teardown-manifest", slug, "unreachable", error=str(exc)[:200])
        return "unreachable"

    if not bucket:
        _audit("teardown-manifest", slug, "unreachable", error="missing bucket")
        return "unreachable"

    # R12 F2: PATCH the existing manifest instead of replacing it — a fresh
    # write drops arch/bucket/distribution_id/oac_id, sending engine-arch
    # deployments down the wrong reaper path (S3-prefix-only) and leaking the
    # distribution + per-site bucket. Download, patch ONLY the expiry fields
    # (preserving unknown fields too), and fall back to a fresh write only if
    # no existing manifest can be read.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_doc: dict[str, Any] = {}
    try:
        rc_m, out_m, _ = await asyncio.to_thread(
            engine.run_aws,
            ["s3", "cp", f"s3://{bucket}/{slug}/.kirocrew-deploy.json", "-",
             "--region", region],
            profile, 15,
        )
        if rc_m == 0 and out_m:
            parsed = json.loads(out_m)
            if isinstance(parsed, dict):
                manifest_doc = parsed
    except Exception as exc:  # noqa: BLE001 — read failure handled fail-closed below
        logger.debug("teardown manifest read failed: %s", exc)

    # R17 F1 (fail-closed rework of R16 F3): a forged webapp artifact whose
    # slug matches another deployment must NOT be able to expire it.
    #  - No readable manifest -> REFUSE (a fresh expired manifest under an
    #    attacker-chosen slug key would direct the reaper at someone else's
    #    infra; nothing legitimate needs a fresh-write here).
    #  - Identity check requires BOTH ids nonempty AND equal. Deploys in this
    #    PR always persist distribution_id on both sides, so a missing id is
    #    itself a red flag, not a compat case.
    if not manifest_doc:
        _audit("teardown-manifest", slug, "denied",
               error="no existing manifest readable (fail closed)")
        return "unreachable"
    meta_dist_id = getattr(meta.deploy_target, "distribution_id", "") if meta.deploy_target else ""
    manifest_dist_id = manifest_doc.get("distribution_id", "")
    if not meta_dist_id or not manifest_dist_id or meta_dist_id != manifest_dist_id:
        _audit("teardown-manifest", slug, "identity_mismatch",
               error=f"metadata.distribution_id={meta_dist_id!r} != manifest.distribution_id={manifest_dist_id!r}")
        return "unreachable"

    manifest_doc["expires_at"] = now_iso
    manifest_doc["persistent"] = False
    manifest_doc["ttl_hours"] = "0"
    expired_manifest = json.dumps(manifest_doc)

    def _write_manifest_tmp() -> str:
        """Write manifest JSON to a temp file. Blocking — run via to_thread."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        try:
            f.write(expired_manifest)
            f.close()
            return f.name
        except BaseException:
            f.close()
            try:
                os.unlink(f.name)
            except OSError:
                pass
            raise

    tmp_path = ""
    try:
        tmp_path = await asyncio.to_thread(_write_manifest_tmp)
        rc, _, err = await asyncio.to_thread(
            engine.run_aws,
            ["s3", "cp", tmp_path,
             f"s3://{bucket}/{slug}/.kirocrew-deploy.json",
             "--content-type", "application/json",
             "--region", region],
            profile, 15,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("teardown manifest-expiry put failed: %s", exc)
        _audit("teardown-manifest", slug, "unreachable", error=str(exc)[:200])
        return "unreachable"
    finally:
        if tmp_path:
            try:
                await asyncio.to_thread(os.unlink, tmp_path)
            except OSError:
                pass

    if rc != 0:
        logger.warning("teardown manifest-expiry: S3 put rc=%d err=%s", rc, err[:200])
        _audit("teardown-manifest", slug, "unreachable", error=f"rc={rc}")
        return "unreachable"

    _audit("teardown-manifest", slug, "expired-now")
    return "expired-now"


@_internal_denied
async def _handle_teardown(request: web.Request) -> web.Response:
    """Human-triggered cancel of a kind="webapp" artifact.

    Owner-only (denied for restricted sessions), confirm-gated. v1 marks the
    artifact as a tombstone (lifecycle.status="expired", kept as deploy history)
    and returns the teardown handle + resources so the in-account reaper
    performs the actual infrastructure deletion. Core issues no AWS calls itself.
    """
    if os.name == "nt":
        return web.json_response(
            {"error": "Artifact Deploy requires a POSIX shell (bash) for deploy scripts. "
             "Use WSL (Windows Subsystem for Linux) to run the KiroCrew gateway on Windows."},
            status=400,
        )
    if not _HAS_ARTIFACTS:
        return web.json_response({"error": "artifacts module unavailable"}, status=500)

    # ── Restricted-session deny (shared helper, consistent across all mutating endpoints) ──
    denied = _deny_restricted(request, "teardown")
    if denied:
        return denied

    # ── Server-side confirm gate ──
    try:
        raw = await request.read()
        body = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    if body.get("confirm") is not True:
        return web.json_response(
            {"error": "confirm=true required in request body"}, status=400)

    slug = request.match_info.get("slug", "")
    # ── Load artifact read-only FIRST to get metadata for manifest expiry ──
    try:
        art = await asyncio.to_thread(get_default_store().get, slug)
    except ArtifactNotFoundError as exc:
        _audit("teardown", slug, "error", error=str(exc))
        return web.json_response({"error": str(exc)}, status=404)
    except ArtifactError as exc:
        _audit("teardown", slug, "error", error=str(exc))
        return web.json_response({"error": str(exc)}, status=500)

    # Validate the artifact is a webapp with metadata before proceeding.
    if getattr(art, "kind", None) != "webapp":
        _audit("teardown", slug, "denied", error="not a webapp")
        return web.json_response({"error": "not a webapp artifact"}, status=400)

    meta = art.webapp_metadata

    # ── Reaper prerequisite check ──
    # The teardown flow relies on the in-account reaper Lambda to clean up
    # infrastructure after the manifest expires. Without the reaper, tombstoning
    # the artifact would leave the infra running with no automated cleanup path.
    profile_for_check = (meta.deploy_target.profile if meta and meta.deploy_target else None)
    region_for_check = (meta.deploy_target.region if meta and meta.deploy_target else None) or engine.DEFAULT_REGION

    # ── Registry validation (F2 security fix) ──
    # Profile and region come from LLM-writable webapp_metadata. Validate them
    # through the profiles registry BEFORE passing to any subprocess.
    if profile_for_check:
        from kiro_crew.deploy import profiles as profiles_mod
        from kiro_crew.validation import ValidationError

        reg = await asyncio.to_thread(profiles_mod.load_registry)
        entry = profiles_mod.get_entry(reg, profile_for_check)
        if entry is None:
            _audit("teardown", slug, "denied",
                   error=f"artifact metadata names an unregistered profile: {profile_for_check!r}")
            return web.json_response(
                {"error": "artifact metadata names an unregistered profile"},
                status=409,
            )
        # Use the validated profile name from registry
        profile_for_check = entry["name"]

        # Validate region format
        try:
            profiles_mod.validate_field(region_for_check, profiles_mod.REGION_SPEC)
        except ValidationError:
            _audit("teardown", slug, "denied",
                   error=f"artifact metadata has invalid region: {region_for_check!r}")
            return web.json_response(
                {"error": "artifact metadata has invalid region"},
                status=409,
            )

    if profile_for_check:
        reaper_installed = await asyncio.to_thread(
            _check_reaper_installed, profile_for_check, region_for_check)
        if not reaper_installed:
            _audit("teardown", slug, "denied", error="reaper not installed")
            return web.json_response({
                "error": (
                    "reaper not installed — run install-reaper.sh or "
                    "teardown.sh from your terminal"
                ),
                "reaper_missing": True,
            }, status=409)

    # ── Best-effort S3 manifest expiry FIRST (before tombstone) ──
    # R39 F2: an unreachable manifest means the reaper cannot see an immediate
    # expiry — for BOTH persistent and finite-TTL deploys. Tombstoning anyway
    # would report success while a finite-TTL deployment keeps running (and
    # billing) until its ORIGINAL TTL, with the card's live URL hidden and the
    # Tear down retry disabled. Fail retryable in both cases; the card keeps
    # its Tear down button until expiry is actually written.
    manifest_status = await _expire_manifest_best_effort(art)

    if manifest_status == "unreachable":
        _audit("teardown", slug, "retry-later",
               error="manifest unreachable — teardown not applied")
        return web.json_response({
            "error": "manifest unreachable — teardown not applied, retry later",
            "manifest": "unreachable",
            "retry": True,
        }, status=502)

    # ── NOW tombstone the artifact (manifest is already expired) ──
    try:
        art = await asyncio.to_thread(get_default_store().mark_webapp_expired, slug)
    except ArtifactNotFoundError as exc:
        _audit("teardown", slug, "error", error=str(exc))
        return web.json_response({"error": str(exc)}, status=404)
    except ArtifactValidationError as exc:
        _audit("teardown", slug, "denied", error=str(exc))
        return web.json_response({"error": str(exc)}, status=400)
    except ArtifactError as exc:
        _audit("teardown", slug, "error", error=str(exc))
        return web.json_response({"error": str(exc)}, status=500)

    # Serialize through the redaction path so handle/resource ids can't bypass.
    from kiro_crew.dashboard.handlers.artifacts import _serialize

    serialized = _serialize(art, include_content=True)
    redacted_meta = serialized.get("webapp_metadata") or {}
    redacted_teardown = redacted_meta.get("teardown") or {}
    redacted_resources = (redacted_meta.get("architecture") or {}).get("resources", [])
    teardown = {
        "method": redacted_teardown.get("method", ""),
        "handle": redacted_teardown.get("handle", ""),
        "resources": redacted_resources,
    }

    _audit("teardown", slug, "allowed")
    return web.json_response({
        "ok": True,
        "artifact": serialized,
        "teardown": teardown,
        "manifest": manifest_status,
        "note": "the in-account reaper will remove infrastructure on its next sweep",
    })
