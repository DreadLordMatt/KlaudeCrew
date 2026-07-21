"""deploy-web config paths + profile resolution leaves.

Legacy single-profile config shim over the multi-profile registry, plus the
request profile resolver, site-id normalizer, and reaper remediation string.
Imports the validation specs from ``staging`` (a leaf) and nothing else in the
deploy package, so it sits just above the leaves in the DAG.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import config_dir
from kiro_crew.deploy import engine
from kiro_crew.deploy import profiles as profiles_mod
from kiro_crew.deploy.staging import _PROFILE_SPEC
from kiro_crew.validation import ValidationError, validate_field

# Data dir resolved via config_dir() so pods/tests isolate correctly.


def _data_dir() -> Path:
    return config_dir() / "deploy"


DATA_DIR = _data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_REGION = engine.DEFAULT_REGION
_SITE_ID_MAX = 64


def _reaper_remediation(profile: str, region: str) -> str:
    """Exact operator command for the finite-TTL reaper precondition (FU-1).

    Rendered with the request's real profile/region so the 409 payload is
    directly actionable. Installing the reaper is an operator step by design
    (the stack creates an IAM role, which KiroCrew never does itself).
    """
    parts = ["install-reaper.sh"]
    if profile:
        parts += ["--profile", profile]
    if region:
        parts += ["--region", region]
    return " ".join(parts)


# --- config (profile NAME only — §6.1) ------------------------------------
# v2: the source of truth is the multi-profile registry (profiles.py). The
# legacy GET/PUT /config endpoints stay as a shim over the registry's default
# entry so the deploy skill and any older client keep working unchanged.

def _load_config() -> dict[str, Any]:
    """Legacy single-profile view = the registry's default entry."""
    reg = profiles_mod.load_registry()
    entry = profiles_mod.get_entry(reg, reg["default"]) if reg["default"] else None
    if entry is None:
        return {"profile": "", "region": DEFAULT_REGION}
    return {"profile": entry["name"], "region": entry["region"] or DEFAULT_REGION}


def _save_config(profile: str, region: str) -> dict[str, Any]:
    """Legacy PUT /config = upsert an entry and make it the default."""
    with profiles_mod.locked_registry() as reg:
        if profile:
            entry = profiles_mod.get_entry(reg, profile)
            if entry is None:
                reg["profiles"].append(profiles_mod.make_entry(profile, region))
            else:
                entry["region"] = region or entry["region"] or DEFAULT_REGION
            reg["default"] = profile
        else:
            reg["default"] = ""
    return {"profile": profile, "region": region or DEFAULT_REGION}


class _ProfileResolveError(Exception):
    """Raised when a deploy request's profile choice can't be resolved."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("error", "profile resolve failed"))
        self.payload = payload


async def _resolve_profile(params: dict[str, Any]) -> tuple[str, str]:
    """Resolve the request's (optional) profile choice → (name, region).

    The ``profile`` param is LLM/user-influenceable and flows into subprocess
    argv, so it is schema-validated, and it must name a *registered* profile —
    deploys never run with an arbitrary unregistered name. Empty → default.
    Raises :class:`_ProfileResolveError` (callers return its payload as a 400).
    """
    raw = str(params.get("profile", "") or "")
    if raw:
        try:
            raw = validate_field(raw, _PROFILE_SPEC)
        except ValidationError as e:
            raise _ProfileResolveError({"error": f"invalid profile: {e}"}) from e
    resolved = await asyncio.to_thread(profiles_mod.resolve_profile, raw)
    if resolved is None:
        if raw:
            raise _ProfileResolveError(
                {"error": f"profile '{raw}' is not registered — add it in Profiles first."})
        raise _ProfileResolveError(
            {"error": "deploy-web is not configured — add an AWS profile first (Profiles)."})
    return resolved


def _safe_site_id(raw: str) -> str:
    """Normalize a site id to a tag/label-safe slug."""
    s = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (raw or "").strip().lower())
    return s.strip("-_")[:_SITE_ID_MAX]
