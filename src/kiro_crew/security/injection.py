"""Prompt-injection screening, history/memory scans, and combined redact-and-truncate helper.

Split out of the former monolithic ``security.py``; re-exported by
``kiro_crew.security`` for backward compatibility.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew.sel import SecurityEvent, SecurityEventLog
from kiro_crew.security.credentials import redact_credentials
from kiro_crew.security.denylist import audit_bash_command
from kiro_crew.security.exfiltration import redact_exfiltration_urls
from kiro_crew.vector_memory_constants import _contains_injection

logger = logging.getLogger(__name__)


def scan_history(history_dir: Path, last_n: int = 100) -> list[dict]:
    """Scan recent conversation history for suspicious tool usage.

    Returns list of findings: [{file, line, tool, command, warning}]
    """
    findings: list[dict] = []
    if not history_dir.is_dir():
        return findings

    files = sorted(history_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    checked = 0
    for f in files:
        try:
            for line in f.read_text().splitlines():
                if checked >= last_n:
                    return findings
                checked += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = entry.get("content", "")
                role = entry.get("role", "")
                if role != "assistant" or not isinstance(content, str):
                    continue
                # Check for bash commands in tool calls
                warning = audit_bash_command(content)
                if warning:
                    findings.append(
                        {
                            "file": f.name,
                            "warning": warning,
                            "snippet": content[:200],
                        }
                    )
        except OSError:
            continue
    return findings


def scan_memory() -> list[dict]:
    """Scan vector memory for suspicious content. Returns list of findings."""
    findings: list[dict] = []
    # Lazy import to avoid a circular dependency (vector_memory imports
    # redact_credentials/redact_exfiltration_urls from this module at its top
    # level) and to keep the optional numpy/faiss/snowballstemmer stack off the
    # lightweight import path. Skip the scan cleanly if it is unavailable.
    try:
        from kiro_crew.vector_memory import VectorMemoryStore
    except Exception:  # numpy/faiss/snowballstemmer are optional heavy deps; any
        # import-time failure (ImportError, OSError from a C-extension, etc.)
        # must skip the scan cleanly rather than crash the caller.
        return findings
    try:
        store = VectorMemoryStore()
        store.init()
    except Exception:
        return findings

    # Scan semantic values
    for entry in store.get_all_semantic():
        val = entry.get("value_json", "")
        if _contains_injection(val):
            findings.append(
                {
                    "type": "semantic",
                    "key": entry["key"],
                    "value": val[:200],
                    "warning": "Injection pattern detected",
                }
            )

    # Scan episodic texts
    for entry in store.get_episodic_list(limit=1000):
        text = entry.get("text", "")
        if _contains_injection(text):
            findings.append(
                {
                    "type": "episodic",
                    "key": entry["id"],
                    "value": text[:200],
                    "warning": "Injection pattern detected",
                }
            )

    store.close()
    return findings


def contains_injection(text: str | None) -> bool:
    """Return True if *text* matches a known prompt-injection pattern.

    Accepts ``None`` (returns ``False``) so callers can screen optional
    fetched content — e.g. a Slack ``thread_parent_text`` that may be unset —
    without a separate None check.

    Public wrapper over the shared ``_INJECTION_PATTERNS`` set (defined in the
    dependency-free ``vector_memory_constants`` module) so untrusted content
    pulled from external surfaces — e.g. Slack thread-parent / thread-metadata
    fetched from arbitrary, possibly non-owner authors — can be screened
    before it is injected into the LLM prompt. The pattern set lives in the
    light constants module (not ``vector_memory``, whose numpy/faiss/stemmer
    deps are heavy), so it is imported at module top level with no lazy import
    and no fail-open path: a screen that cannot run must not silently pass
    untrusted content through.
    """
    if not text:
        return False
    return _contains_injection(text)


def audit_injection_dropped(
    *,
    surface: str,
    session_key: str = "",
    channel_id: str = "",
    thread_ts: str = "",
    agent: str = "kirocrew",
    sample: str = "",
) -> None:
    """Emit an SEL audit event when injection-screened content is dropped.

    Called when :func:`contains_injection` flags untrusted external content
    (e.g. a Slack thread-parent message or thread metadata authored by a
    non-owner) and the content is dropped before reaching the LLM prompt
    (Talos 1fde6107). Recording the attempt keeps prompt-injection attempts
    visible in the audit trail rather than silently discarded.

    Best-effort: an SEL logging failure is logged at WARNING and never
    propagates — the content is dropped regardless of audit success, so this
    cannot break prompt building.
    """
    try:
        SecurityEventLog().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="prompt_injection_dropped",
                caller_identity=session_key,
                agent=agent,
                source="context",
                operation=surface,
                outcome="dropped",
                resources=f"channel_id={channel_id} thread_ts={thread_ts}",
                metadata={
                    "surface": surface,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "sample": sample[:200] if sample else "",
                    "mechanism": "contains_injection",
                },
            )
        )
    except Exception:
        logger.warning(
            "SEL audit failed for prompt_injection_dropped on %r (content still dropped)",
            surface,
            exc_info=True,
        )


def should_record_observe_history(
    channel_history: object | None,
    user_authorized: bool,
) -> bool:
    """Return True if an observe-mode message should be recorded.

    Only authorized users' messages are recorded to prevent non-owner
    prompt injection via shared channel traffic (Shepherd bdd39e84).
    """
    return channel_history is not None and user_authorized


def redact_and_truncate(text: str, max_chars: int = 4000) -> str:
    """Redact credentials and exfiltration URLs, then truncate.

    Redaction runs over the full text BEFORE the ``max_chars`` slice so a
    credential (or base64/URL blob) straddling the truncation boundary cannot
    leak as an unredacted partial fragment (Talos e27617c6). Truncating first
    would cut a secret in half, leaving a prefix that no longer matches the
    credential regex and therefore escapes redaction.
    """
    return redact_credentials(redact_exfiltration_urls(text or "")[0])[0][:max_chars]
