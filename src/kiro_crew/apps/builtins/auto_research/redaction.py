"""Auto-research redaction + SEL audit helpers.

Scrubs credentials / exfiltration URLs from LLM- and user-generated content
before it reaches any external surface, and emits SEL audit events for
campaign lifecycle actions. Leaf module (no other auto_research deps).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    _HAS_SECURITY = True
except ImportError:
    _HAS_SECURITY = False

try:
    from kiro_crew.sel import sel
except ImportError:
    sel = None  # type: ignore[assignment]


def _redact_finding(finding: dict) -> dict:
    """Redact credentials and exfiltration URLs from finding data."""
    if not _HAS_SECURITY:
        # Fail-closed: recursively mask every string value (incl. nested
        # lists/dicts) when the security module is unavailable.
        def _mask(val: Any) -> Any:
            if isinstance(val, str):
                return "[REDACTED]"
            if isinstance(val, list):
                return [_mask(item) for item in val]
            if isinstance(val, dict):
                return {k: _mask(v) for k, v in val.items()}
            return val

        return {k: _mask(v) for k, v in finding.items()}

    def _redact_str(s: str) -> str:
        cleaned, _ = redact_credentials(s)
        cleaned, _ = redact_exfiltration_urls(cleaned)
        return cleaned

    def _redact_value(val: Any) -> Any:
        if isinstance(val, str):
            return _redact_str(val)
        elif isinstance(val, list):
            return [_redact_value(item) for item in val]
        elif isinstance(val, dict):
            return {k2: _redact_value(v2) for k2, v2 in val.items()}
        return val

    return {k: _redact_value(v) for k, v in finding.items()}


def _redact_tree_node(node: Any) -> Any:
    """Redact a single persisted grill-tree element before serving it.

    The tree is LLM-generated, so EVERY element must be scanned — not just
    dicts. String elements (e.g. from a malformed LLM response or schema
    drift) are scrubbed with the same credential/exfil-URL redaction used for
    findings; nested lists are scanned recursively; primitives
    (int/float/bool/None) carry no secrets and pass through unchanged.
    """
    if isinstance(node, dict):
        return _redact_finding(node)
    if isinstance(node, str):
        # Reuse _redact_finding's string handling (incl. fail-closed masking
        # when the security module is unavailable) via a throwaway wrapper.
        return _redact_finding({"v": node})["v"]
    if isinstance(node, list):
        # Recurse into nested lists: a drifted/malformed tree could nest
        # strings (with credentials/exfil URLs) inside a list element.
        return [_redact_tree_node(item) for item in node]
    return node


def _redact_campaign(campaign: dict) -> dict:
    """Redact user/LLM-generated fields in campaign metadata."""
    for field in ("question", "name", "error_message", "success_criteria", "pending_question"):
        if isinstance(campaign.get(field), str):
            campaign[field] = _redact_finding({"v": campaign[field]})["v"]
    # sub_questions/sources are JSON-encoded lists — decode, redact, re-encode.
    for field in ("sub_questions", "sources"):
        raw = campaign.get(field)
        if isinstance(raw, str):
            try:
                items = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            campaign[field] = json.dumps(_redact_finding({"v": items})["v"])
    return campaign


def _audit(operation: str, campaign_id: str, **extra: Any) -> None:
    """Emit SEL audit event for campaign lifecycle actions."""
    if sel is None:
        logger.warning(
            "SEL module unavailable — audit event for %s/%s not recorded",
            operation,
            campaign_id,
        )
        return
    try:
        sel().log_api_access(
            caller="auto_research",
            operation=operation,
            outcome="success",
            resources=campaign_id,
            **extra,
        )
    except Exception as exc:
        logger.warning("SEL audit failed for %s/%s: %s", operation, campaign_id, exc)
