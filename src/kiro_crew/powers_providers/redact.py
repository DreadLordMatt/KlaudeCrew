"""Mandatory, fail-closed redaction for Power / registry payload strings.

Every externally-sourced, attacker-controllable string that a Powers response
carries — third-party ``POWER.md`` fields, marketplace-scraped metadata,
registry ids and URLs — MUST pass through :func:`redact_external` before it
leaves the process, per the backend-security-controls rule.

This is deliberately a *leaf* module: it imports only :mod:`kiro_crew.security`
(no dashboard, no provider) so both the dashboard handler and the provider
façade can share the exact same shaper without a circular or boot dependency.

Contract: the shaper applies :func:`redact_credentials` and
:func:`redact_exfiltration_urls` and **never falls back to identity**. A
redactor raising is propagated to the caller so the response fails *closed*
(HTTP 500) rather than silently emitting un-redacted third-party text.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def redact_external(text: str) -> str:
    """Scrub a single external string; raise (never identity) on redactor error.

    Empty / falsy input passes straight through (nothing to scrub). Any other
    string is run through both security scanners. If either scanner raises, the
    exception propagates — redaction is mandatory, so a failure must fail the
    whole response closed instead of leaking raw text.
    """
    if not text:
        return text
    scrubbed, _ = redact_credentials(text)
    scrubbed, _ = redact_exfiltration_urls(scrubbed)
    return scrubbed


def redact_payload(obj: Any) -> Any:
    """Recursively redact every string *value* in a Power payload.

    Walks dicts (values only — the keys are our own fixed contract, not
    third-party) and lists, applying :func:`redact_external` to each string and
    leaving non-string scalars untouched. Structure is preserved so the shaped
    object still matches the JSON contract.
    """
    if isinstance(obj, str):
        return redact_external(obj)
    if isinstance(obj, dict):
        return {k: redact_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_payload(v) for v in obj]
    return obj
