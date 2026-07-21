"""deploy-web response redaction + SEL audit leaves.

Pure leaf module (no back-imports into other deploy submodules): credential /
internal-URL redaction helpers applied to every response payload, and the SEL
audit emitter for deploy permission decisions. Imported everywhere in the
``deploy`` package; imports nothing from it.
"""
from __future__ import annotations

from typing import Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel


def _redact_text(s: str) -> str:
    """Redact credentials and internal URLs from any text returned in response bodies.

    Applied to scan summary strings, error messages, and any findings-carrying
    payload before it reaches LLM/dashboard callers. Defense-in-depth: even if
    _mask_credential is applied at Finding creation time, this catches any path
    where raw matched content leaks through.
    """
    s, _ = redact_credentials(s)
    s, _ = redact_exfiltration_urls(s)
    return s


def _sanitize_response(payload: Any) -> Any:
    """Recursively apply credential + exfiltration redaction to all str values in a response payload.

    R19 F1: deploy handler error responses echo LLM-controlled values (local_dir,
    site_id, profile) without BOTH redaction passes. This helper walks dict/list
    structures and applies _redact_text to every str leaf. Applied at the three
    chokepoint handlers (_handle_deploy, _handle_recall, _handle_destroy) so
    ALL paths through _do_* are covered in one place.
    """
    if isinstance(payload, str):
        return _redact_text(payload)
    if isinstance(payload, dict):
        return {k: _sanitize_response(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_sanitize_response(v) for v in payload]
    return payload


def _redact_profile_fields(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact credential-containing strings from profile entries before response.

    Applied at serialization in every handler that returns registry entries.
    SEL audit keeps the raw data; dashboard/API responses are sanitized.
    """
    out: list[dict[str, Any]] = []
    for p in profiles:
        entry: dict[str, Any] = {}
        for k, v in p.items():
            if isinstance(v, str) and v:
                v, _ = redact_credentials(v)
                v, _ = redact_exfiltration_urls(v)
            entry[k] = v
        out.append(entry)
    return out


def _redact_pending_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recursively redact every string value in pending entries before response.

    Defense-in-depth (F2 R8): pending entries contain LLM-controlled fields
    (site_id, local_dir, scan_summary, profile) that could carry injected
    credential strings. Same pipeline as _redact_profile_fields.
    """
    out: list[dict[str, Any]] = []
    for e in entries:
        redacted: dict[str, Any] = {}
        for k, v in e.items():
            if isinstance(v, str) and v:
                v = _redact_text(v)
            elif isinstance(v, dict):
                # Recurse one level for nested dicts
                v = {
                    dk: _redact_text(dv) if isinstance(dv, str) and dv else dv
                    for dk, dv in v.items()
                }
            redacted[k] = v
        out.append(redacted)
    return out


def _audit(action: str, site_id: str, outcome: str, *, error: str = "") -> None:
    """Emit a SEL audit event for a deploy-web permission decision.

    deploy/recall/destroy create public internet infrastructure, delete
    resources, and make content world-readable — each confirmed action is a
    significant permission decision and MUST be recorded (§9.3).
    """
    sel().log_api_access(
        caller="core:deploy",
        operation=f"deploy.{action}",
        outcome=outcome,
        source="builtin-app",
        resources=site_id,
        error=error[:200] if error else "",
    )


def _safe_err(exc: BaseException) -> str:
    """Return a safe error message with credentials/URLs redacted.

    AWS CLI stderr can contain credential fragments (access key ids, session
    tokens, etc.) — never surface the raw exception in response payloads.
    """
    msg = str(exc)
    msg, _ = redact_credentials(msg)
    msg, _ = redact_exfiltration_urls(msg)
    return msg
