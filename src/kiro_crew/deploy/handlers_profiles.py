"""deploy-web aiohttp handlers: config, profiles, verify, iam-policy, pricing.

The profile control plane and the read-only config/verify/iam/pricing
endpoints. All are internal-secret-denied except pricing (allowlisted in
``internal_guard``). Imports guards from ``internal_guard`` and leaves from
``redaction`` / ``config`` / ``staging`` — never the ``handlers`` shim — so no
import cycle forms.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from aiohttp import web

from kiro_crew.deploy import iam as iam_mod
from kiro_crew.deploy import pricing as pricing_mod
from kiro_crew.deploy import profiles as profiles_mod
from kiro_crew.deploy.config import (
    DEFAULT_REGION,
    _load_config,
    _ProfileResolveError,
    _resolve_profile,
    _save_config,
)
from kiro_crew.deploy.internal_guard import (
    _deny_restricted,
    _internal_denied,
    _json_body,
)
from kiro_crew.deploy.redaction import (
    _audit,
    _redact_profile_fields,
    _redact_text,
    _safe_err,
    _sanitize_response,
)
from kiro_crew.deploy.staging import _PROFILE_SPEC, _REGION_SPEC
from kiro_crew.validation import ValidationError, validate_field


@_internal_denied
async def _handle_get_config(_request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(_load_config))


@_internal_denied
async def _handle_put_config(request: web.Request) -> web.Response:
    denied = _deny_restricted(request, "config_update")
    if denied:
        return denied
    body = await _json_body(request)
    try:
        profile = validate_field(str(body.get("profile", "")), _PROFILE_SPEC)
        region = validate_field(str(body.get("region", "")), _REGION_SPEC)
    except ValidationError as e:
        return web.json_response({"error": f"invalid config: {e}"}, status=400)
    result = await asyncio.to_thread(_save_config, profile, region)
    _audit("config_update", profile, "allowed")
    # R26 F2: config responses echo profile/region strings — route through
    # the sanitize chokepoint like every other deploy response.
    return web.json_response(_sanitize_response(result))


@_internal_denied
async def _handle_iam_policy(request: web.Request) -> web.Response:
    """Generate the least-privilege IAM policy text for the user to apply (Option A)."""
    custom = request.query.get("custom_domain", "").lower() in ("1", "true", "yes")
    tier = request.query.get("tier", "static")
    if tier not in ("static", "fullstack"):
        return web.json_response({"error": "tier must be 'static' or 'fullstack'"}, status=400)
    payload: dict[str, Any] = {
        "policy": iam_mod.policy_json(include_custom_domain=custom, tier=tier),
    }
    if tier == "fullstack":
        # R23 F1: fullstack requires the permissions-boundary policy to exist
        # BEFORE the first deploy — iam:CreateRole is conditioned on it.
        payload["boundary_policy"] = json.dumps(
            iam_mod.boundary_policy_document(), indent=2)
        payload["boundary_policy_name"] = iam_mod.BOUNDARY_POLICY_NAME
        payload["boundary_note"] = (
            f"Create this policy once as '{iam_mod.BOUNDARY_POLICY_NAME}' — "
            "app roles are created with it as a permissions boundary, which "
            "caps their effective permissions."
        )
    return web.json_response(payload)


@_internal_denied
async def _handle_verify(request: web.Request) -> web.Response:
    """Read-only reachability check (NOT full verification, §9.3/Q3).

    Accepts an optional ``profile`` in the body to verify a specific registered
    profile; empty body verifies the default. A success back-fills the entry's
    ``account`` + ``verified_at`` in the registry (display metadata only).
    """
    if os.name == "nt":
        return web.json_response(
            {"reachable": False, "error": "Deploy scripts require a POSIX shell — use WSL."},
            status=400,
        )
    denied = _deny_restricted(request, "verify")
    if denied:
        return denied
    body = await _json_body(request)
    try:
        profile, _region = await _resolve_profile(body)
    except _ProfileResolveError as e:
        return web.json_response({"reachable": False, **e.payload}, status=400)
    result = await asyncio.to_thread(iam_mod.reachability_check, profile)
    if result.get("reachable"):
        def _backfill_verify() -> None:
            with profiles_mod.locked_registry() as reg:
                entry = profiles_mod.get_entry(reg, profile)
                if entry is not None:
                    entry["account"] = str(result.get("account", "")) or entry["account"]
                    entry["verified_at"] = profiles_mod.now_iso()

        await asyncio.to_thread(_backfill_verify)
        # Back-filling account/verified_at is a registry write — audited
        # like every other registry mutation in this file.
        _audit("profile_verify", profile, "allowed")
    return web.json_response(_sanitize_response({**result, "profile": profile}))  # R26 F2


async def _handle_pricing(request: web.Request) -> web.Response:
    """Live unit prices for cost estimates (NEW-1, Joe R1 follow-up).

    Read-only: queries the AWS Pricing API through the resolved profile and
    returns per-unit USD prices with a ``source`` marker (``live`` vs
    ``fallback``). Estimate consumers (skill contract, cost surfaces) use
    these instead of the hardcoded MVP table when available. Failures of any
    kind degrade to the fallback table -- an estimate must never 500.
    """
    denied = _deny_restricted(request, "pricing")
    if denied:
        return denied
    query = dict(request.rel_url.query)
    try:
        profile, region = await _resolve_profile(query)
    except _ProfileResolveError as e:
        return web.json_response(e.payload, status=400)
    prices = await asyncio.to_thread(pricing_mod.get_unit_prices, profile, region)
    _audit("pricing", profile, "allowed")
    return web.json_response(_sanitize_response({
        "region": region,
        "prices": prices.to_dict(),
    }))


# --- profile control plane (§ multi-profile) --------------------------------

@_internal_denied
async def _handle_profiles_get(_request: web.Request) -> web.Response:
    """Registry + AWS-CLI-discovered profile names (names only, read-only)."""
    reg = await asyncio.to_thread(profiles_mod.load_registry)
    discovered = await asyncio.to_thread(profiles_mod.discover_aws_profiles)
    registered = {p["name"] for p in reg["profiles"]}
    # R18 F1: the blocking credential-redaction rule applies to EVERY
    # profile-related string in the response -- `default` and the discovered
    # `available` names were the two unredacted stragglers.
    return web.json_response({
        "profiles": _redact_profile_fields(reg["profiles"]),
        "default": _redact_text(str(reg["default"])),
        "available": [_redact_text(str(n))
                      for n in discovered if n not in registered],
    })


@_internal_denied
async def _handle_profiles_post(request: web.Request) -> web.Response:
    """Register a profile; with ``create=true`` also write the AWS config block.

    The write path is the significant permission decision here (it defines how
    future deploys obtain credentials), so it is SEL-audited and confined to
    ``aws configure set`` on an allowlisted key set (see profiles.py).
    """
    denied = _deny_restricted(request, "profile_create")
    if denied:
        return denied
    body = await _json_body(request)
    try:
        name = validate_field(str(body.get("name", "")), _PROFILE_SPEC)
        region = validate_field(str(body.get("region", "")) or DEFAULT_REGION, _REGION_SPEC)
    except ValidationError as e:
        return web.json_response({"error": f"invalid profile: {e}"}, status=400)
    # R40 F1: validate_field skips the pattern check on an empty string and
    # _PROFILE_SPEC is not required, so "" sails through — and downstream
    # engine._aws() OMITS --profile for an empty name, meaning
    # ``aws configure set`` would silently rewrite the user's DEFAULT AWS
    # profile. Reject empty explicitly before any side effect.
    if not name:
        _audit("profile_create", "", "denied-empty-name")
        return web.json_response({"error": "invalid profile: name must not be empty"}, status=400)

    # Fail-fast on capacity BEFORE any side-effects (create_aws_profile writes ~/.aws/config)
    reg_pre = await asyncio.to_thread(profiles_mod.load_registry)
    is_new = profiles_mod.get_entry(reg_pre, name) is None
    if is_new and len(reg_pre["profiles"]) >= 50:
        return web.json_response({"error": "profile registry is full (50)"}, status=400)

    if body.get("create") is True:
        err = await asyncio.to_thread(
            profiles_mod.create_aws_profile, name, region,
            account=str(body.get("account", "") or ""),
            role=str(body.get("role", "") or ""))
        _audit("profile_create", name, "denied" if err else "allowed", error=err or "")
        if err:
            # Raw CLI stderr may carry credential fragments -- redact before
            # returning to the dashboard (the SEL audit above keeps the raw text).
            return web.json_response({"error": _safe_err(Exception(err))}, status=400)

    # Atomic RMW under lock — concurrent POST/PUT/DELETE won't lose each other's writes.
    # R37 F1: the capacity check must be re-verified INSIDE the lock. The
    # pre-lock check above is only a fast-fail before the create_aws_profile
    # side effect; two concurrent POSTs at 49 profiles could both pass it,
    # append to 51, and load_registry()'s truncate-to-50 would silently drop
    # one registered profile on the next read.
    def _post_rmw() -> tuple[dict, str]:
        with profiles_mod.locked_registry() as reg:
            fresh_is_new = profiles_mod.get_entry(reg, name) is None
            if fresh_is_new and len(reg["profiles"]) >= 50:
                return reg, "profile registry is full (50)"
            if fresh_is_new:
                reg["profiles"].append(profiles_mod.make_entry(name, region))
            if body.get("default") or not reg["default"]:
                reg["default"] = name
        return reg, ""

    reg, cap_err = await asyncio.to_thread(_post_rmw)
    if cap_err:
        _audit("profile_register", name, "denied", error=cap_err)
        return web.json_response({"error": cap_err}, status=400)
    # Registration itself is a permission decision (the name becomes deployable),
    # so it is audited unconditionally — separate from the create write-path audit.
    _audit("profile_register", name, "allowed")
    return web.json_response({
        "profiles": _redact_profile_fields(reg["profiles"]),
        "default": _redact_text(str(reg["default"])),  # R18 F1
    })


@_internal_denied
async def _handle_profiles_put(request: web.Request) -> web.Response:
    """Edit a registered profile's region/note, or make it the default."""
    denied = _deny_restricted(request, "profile_update")
    if denied:
        return denied
    body = await _json_body(request)
    # match_info name is LLM/user-influenceable — validate like the POST path
    # before it reaches the registry lookup / error strings / audit call.
    try:
        name = validate_field(request.match_info.get("name", ""), _PROFILE_SPEC)
    except ValidationError as e:
        return web.json_response({"error": _redact_text(f"invalid profile name: {e}")}, status=400)

    # Pre-validate body fields before taking the lock (avoids holding it during
    # user-error returns)
    new_region: str | None = None
    if "region" in body:
        try:
            new_region = validate_field(str(body["region"]), _REGION_SPEC)
        except ValidationError as e:
            return web.json_response({"error": _redact_text(f"invalid region: {e}")}, status=400)

    # Atomic RMW under lock
    def _put_rmw() -> dict | str:
        with profiles_mod.locked_registry() as reg:
            entry = profiles_mod.get_entry(reg, name)
            if entry is None:
                return f"profile '{name}' not registered"
            if new_region is not None:
                entry["region"] = new_region
            if "note" in body:
                entry["note"] = str(body["note"])[:256]
            if body.get("default"):
                reg["default"] = name
        return reg

    result = await asyncio.to_thread(_put_rmw)
    if isinstance(result, str):
        return web.json_response({"error": result}, status=404)
    # Changing region/default alters which AWS identity/region future deploys
    # run with — a permission decision, so it is SEL-audited like profile_create.
    _audit("profile_update", name, "allowed")
    return web.json_response({
        "profiles": _redact_profile_fields(result["profiles"]),
        "default": _redact_text(str(result["default"])),  # R20 F4
    })


@_internal_denied
async def _handle_profiles_delete(request: web.Request) -> web.Response:
    """Remove a profile from the registry ONLY — never touches ~/.aws/config."""
    denied = _deny_restricted(request, "profile_delete")
    if denied:
        return denied
    # match_info name is LLM/user-influenceable — validate like the POST path.
    try:
        name = validate_field(request.match_info.get("name", ""), _PROFILE_SPEC)
    except ValidationError as e:
        return web.json_response({"error": _redact_text(f"invalid profile name: {e}")}, status=400)
    reg = await asyncio.to_thread(profiles_mod.load_registry)
    if profiles_mod.get_entry(reg, name) is None:
        return web.json_response({"error": _redact_text(f"profile '{name}' not registered")}, status=404)

    # Atomic RMW under lock
    def _delete_rmw() -> dict | str:
        with profiles_mod.locked_registry() as reg:
            if profiles_mod.get_entry(reg, name) is None:
                return f"profile '{name}' not registered"
            reg["profiles"] = [p for p in reg["profiles"] if p["name"] != name]
            if reg["default"] == name:
                reg["default"] = reg["profiles"][0]["name"] if reg["profiles"] else ""
        return reg

    result = await asyncio.to_thread(_delete_rmw)
    if isinstance(result, str):
        return web.json_response({"error": result}, status=404)
    # Removing a deployable identity (and possibly reassigning the default) is
    # a permission decision — SEL-audited like profile_create.
    _audit("profile_delete", name, "allowed")
    return web.json_response({
        "profiles": _redact_profile_fields(result["profiles"]),
        "default": _redact_text(str(result["default"])),  # R20 F4
    })
