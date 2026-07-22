"""Artifact serialization + redaction helpers (leaf module).

Lowest layer of the ``handlers.artifacts`` package: pure serialization, redaction
and preview-snippet helpers plus the session-title resolver. No sibling imports —
every other submodule depends on this one.
"""

from __future__ import annotations

import base64
import copy
import re
from typing import Any

from kiro_crew.security import (
    _B64_CHUNK_RE,
    _HARD_CREDENTIAL_RE,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.validation import infer_use_case

#: Shown in the Source column when an artifact's originating session no longer
#: exists (the artifact itself is kept — sessions and artifacts have independent
#: lifecycles).
_DELETED_SESSION_LABEL = "(deleted session)"


def _resolve_session_title(state: Any, session_key: str) -> str:
    """Live-resolve a session_key to its current chat title for the Source column.

    Returns the session's current display title while it exists (so renames are
    reflected), ``"(deleted session)"`` when the key referenced a session that
    is now gone, and ``""`` when there is no originating session at all (e.g. a
    non-chat origin or a legacy artifact) — the caller falls back to the origin
    label in that case.
    """
    if not session_key:
        return ""
    # Only chat (dashboard) sessions have a slot/title. Non-chat origins
    # (cron / subagent / slack / cli / task-runner) have no chat title — return
    # "" so the caller falls back to the origin label rather than mislabeling
    # them "(deleted session)".
    if infer_use_case(session_key) != "dashboard":
        return ""
    # Slots are keyed by the bare name; the frontend/session key may carry a
    # "dashboard:" prefix.
    name = session_key.split(":", 1)[1] if session_key.startswith("dashboard:") else session_key
    slot = state.get_slot(name) if state is not None else None
    if slot is None:
        return _DELETED_SESSION_LABEL
    try:
        return slot.display_title
    except Exception:  # pragma: no cover — never let title resolution break a list
        return _DELETED_SESSION_LABEL


def _redact_audit_metadata(obj: Any) -> Any:
    """Recursively redact every string leaf of SEL ``extra`` metadata.

    Provider-controlled values (e.g. ``external_id``) reach the audit log via
    ``extra`` and can be credential-shaped, so they pass the same
    seam-aware credential/exfil redaction as ``error``.
    """
    from kiro_crew.platform.context import redact_via_context

    if isinstance(obj, str):
        return redact_via_context(obj)
    if isinstance(obj, dict):
        return {
            (redact_via_context(k) if isinstance(k, str) else k): _redact_audit_metadata(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_audit_metadata(v) for v in obj]
    return obj


def _serialize(art: Any, *, include_content: bool = False, state: Any = None) -> dict[str, Any]:
    """Serialize an Artifact for response.

    All LLM-originated string fields (``name``, ``description``, ``tags``,
    and — when ``include_content=True`` — ``content``) pass through
    ``redact_exfiltration_urls()`` + ``redact_credentials()`` per
    AUTOSDE.yaml's ``security-controls`` rule. Artifact metadata is set
    by the agent via ``artifact_save`` / ``artifact_update``, so any
    field originating in LLM output must not reach the dashboard surface
    unredacted.
    """
    out = art.to_dict(include_content=include_content)
    for key in ("name", "description"):
        val = out.get(key)
        if isinstance(val, str) and val:
            cleaned, _ = redact_exfiltration_urls(val)
            cleaned, _ = redact_credentials(cleaned)
            out[key] = cleaned
    if isinstance(out.get("tags"), list):
        out["tags"] = [_redact_text(t) if isinstance(t, str) else t for t in out["tags"]]
    if include_content and out.get("content"):
        cleaned = out["content"]
        cleaned, _ = redact_exfiltration_urls(cleaned)
        cleaned, _ = redact_credentials(cleaned)
        out["content"] = cleaned
    # Live-resolve the originating session's current title for the Source column.
    # Only set when we can resolve (session present, or gone -> "(deleted
    # session)"); when there's no originating session the key is absent and the
    # frontend falls back to the origin label.
    if state is not None:
        title = _resolve_session_title(state, out.get("session_key") or "")
        if title:
            out["session_title"] = _redact_text(title)
    # Publication block (Artifactory) is structural — view_url is an internal
    # CloudFront URL and aliases are user input — but ``last_error`` can echo
    # an arbitrary upstream error string, so redact it like other surfaced
    # text per AUTOSDE security-controls.
    pub = out.get("publication")
    if isinstance(pub, dict) and isinstance(pub.get("last_error"), str) and pub["last_error"]:
        pub["last_error"] = _redact_text(pub["last_error"])
    if isinstance(out.get("webapp_metadata"), dict):
        out["webapp_metadata"] = _redact_webapp_metadata(out["webapp_metadata"])
    return out


def _redact_text(text: str) -> str:
    cleaned, _ = redact_exfiltration_urls(text)
    cleaned, _ = redact_credentials(cleaned)
    return cleaned


def _validate_inbound_webapp_metadata(body: dict[str, Any]) -> str | None:
    """Run the bounded webapp_metadata validation at the HTTP boundary.

    The MCP boundary already validates via ARTIFACT_SAVE/UPDATE_SCHEMA's
    custom validator; the HTTP handlers must apply the same gate so a
    dashboard/API caller cannot store what the MCP path would reject
    (e.g. a javascript: public_url). Returns an error message or None.
    """
    if body.get("webapp_metadata") is None:
        return None
    from kiro_crew.validation import ValidationError, _validate_artifact_save

    try:
        _validate_artifact_save({"webapp_metadata": body["webapp_metadata"]})
    except ValidationError as exc:
        return str(exc)
    return None


def _redact_webapp_metadata(obj: Any) -> Any:
    """Recursively redact every string leaf in a webapp_metadata sub-tree.

    webapp_metadata (deploy target, architecture, resource ids, cost note,
    teardown handle, origin session) is LLM-set like name / description,
    so it must pass the same exfiltration + credential redaction before reaching
    the dashboard surface.
    """
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, dict):
        return {
            (_redact_text(k) if isinstance(k, str) else k): _redact_webapp_metadata(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_webapp_metadata(v) for v in obj]
    return obj


#: Top-level keys ``_serialize`` has already passed through the redactors, so
#: ``_redact_remote_response`` need not rescan them (avoids a second full-content
#: regex pass over the ≤25 MiB ``content`` body on the event loop).
_SERIALIZE_REDACTED_KEYS = frozenset({"content", "name", "description", "tags"})

#: Defense-in-depth cap on how deep the remote-response redactor walks, matching
#: the Block Kit sanitizer in ``messaging.py`` — a pathologically nested provider
#: response can't drive a ``RecursionError`` (which would escape the handler's
#: 502 mapping as an unhandled 500). Nesting beyond this is truncated.
_MAX_REDACT_DEPTH = 10

#: Keys in a remote/provider response that hold an OPAQUE provider identifier —
#: a join key (matched against the local index) and the action handle the FE
#: sends back verbatim for clone/fork. These are never human-readable prose. For
#: them the redactor skips ONLY the entropy/exfil heuristic (which false-positives
#: on a benign high-entropy id — UUID / content hash — and would rewrite it to
#: ``[REDACTED]``, breaking clone/fork of that id) but STILL runs hard-credential
#: redaction, so a provider that embeds a literal AKIA/SSH/Slack token in an id
#: cannot reach the dashboard verbatim. Titles/snippets/owners are redacted in
#: full (both heuristic and hard-credential).
_REMOTE_ID_KEYS = frozenset({"external_id", "artifactId", "id"})

#: Replacement for a provider id that embeds a literal hard credential — the
#: whole id is dropped (a clone/fork of it fails rather than round-tripping the
#: token back to the browser and provider).
_REMOTE_ID_CRED_TAG = "[REDACTED: credential]"


def _id_embeds_hard_credential(value: str) -> bool:
    """True if an opaque provider id embeds a hard credential — literal OR
    base64-encoded.

    The id-key branch of ``_redact_remote_response`` deliberately skips the
    ENTROPY heuristic (a benign UUID / content hash is high-entropy and must
    survive so clone/fork can send it back), but a malicious provider could
    still smuggle a real token in the id. We therefore run the hard-credential
    floor two ways: on the raw id, and on any base64-shaped chunk decoded to
    text. A benign high-entropy id decodes to non-credential bytes (or fails to
    decode), so this preserves the exemption while closing the encoded-token
    hole. Only the hard floor is applied to the decoded bytes — NOT the entropy
    heuristic — so a benign id that merely *looks* base64 is not rewritten."""
    if _HARD_CREDENTIAL_RE.search(value):
        return True
    for m in _B64_CHUNK_RE.finditer(value):
        try:
            decoded = base64.b64decode(m.group(), validate=True).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if _HARD_CREDENTIAL_RE.search(decoded):
            return True
    return False


def _redact_remote_response(data: dict, *, already_redacted: frozenset[str] = frozenset()) -> dict:
    """Redact credential patterns and exfiltration URLs from a remote/provider
    response before it reaches the dashboard.

    Walks nested dicts AND lists — *including* lists nested inside lists (the
    prior hand-rolled walker only redacted dicts/strings inside a top-level
    list, silently skipping list-in-list values) — up to ``_MAX_REDACT_DEPTH``
    levels. A single ``deepcopy`` at entry isolates the caller's object; the
    recursion then rewrites in place instead of re-copying every subtree at
    each level (the old per-level ``deepcopy`` made redaction O(n·depth)).

    ``already_redacted`` names top-level keys whose string values the caller has
    already passed through the same redactors (e.g. ``_serialize`` redacts an
    up-to-25 MiB ``content`` body), so they are not rescanned a second time.
    Strips ``localPath`` (a leaked local filesystem path) from the top level.

    The walk builds fresh containers as it goes (so it doubles as the copy — no
    separate unbounded ``copy.deepcopy`` of the input, which would itself
    ``RecursionError`` on a pathologically nested provider response before the
    depth cap could take effect).
    """

    def _walk(value: Any, depth: int, key: str = "") -> Any:
        # Opaque provider identifiers are join keys + action handles (the FE
        # sends external_id straight back as the clone/fork target), NOT prose.
        # The exfil/entropy heuristic false-positives on a benign high-entropy
        # id (UUID / content hash) and would rewrite it to [REDACTED], breaking
        # clone/fork of that id. So for these keys we skip ONLY the entropy
        # heuristic — but STILL run hard-credential redaction, so a provider that
        # smuggles a literal AKIA/SSH/Slack token in the id can't reach the
        # dashboard verbatim. A benign id passes through unchanged; an id that
        # actually contains a credential is redacted (and a clone of it would
        # legitimately fail rather than exfiltrate).
        if key in _REMOTE_ID_KEYS and isinstance(value, str):
            # Use the HARD-credential floor only (literal AKIA/ASIA/SSH/PEM/Slack
            # markers + base64-encoded variants), NOT redact_credentials — the
            # latter also runs the bare-secret ENTROPY heuristic, which
            # false-positives on a benign high-entropy id (UUID / content hash)
            # and would break clone/fork. If a hard credential IS embedded
            # (literal or base64), redact the whole id (a clone of it should
            # fail rather than exfiltrate the token).
            return _REMOTE_ID_CRED_TAG if _id_embeds_hard_credential(value) else value
        if depth > _MAX_REDACT_DEPTH:
            # Redact a boundary string; truncate deeper containers rather than
            # recurse further (defense-in-depth, mirrors messaging._sanitize_blocks).
            if isinstance(value, str):
                return _redact_text(value) if value else value
            if isinstance(value, dict):
                return {}
            if isinstance(value, list):
                return []
            return value
        if isinstance(value, str):
            return _redact_text(value) if value else value
        if isinstance(value, dict):
            return {k: _walk(v, depth + 1, k) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(item, depth + 1) for item in value]
        return value

    out: dict = {}
    for key, val in data.items():
        # An already-redacted top-level value is copied (deep) as-is rather than
        # rescanned. copy.deepcopy is bounded here — these values (e.g. the
        # serialized ``content`` string / ``tags`` list) are not adversarially
        # nested — and keeps the response independent of the caller's object.
        out[key] = copy.deepcopy(val) if key in already_redacted else _walk(val, 1)
    out.pop("localPath", None)
    return out


#: Max length of a content preview snippet returned by the list endpoint when
#: ``?snippet=1`` is passed. Kept short so the list payload stays lean.
_SNIPPET_MAX_LEN = 160

#: Max accepted length for the ?q search string. Anything longer is truncated —
#: the scan substring-matches q against every artifact's full content, so an
#: unbounded query multiplies work for no legitimate use case.
_SEARCH_QUERY_MAX_CHARS = 256
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")
# Lightweight markdown → prose cleanup for previews (not a full parser).
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")  # [text](url) / ![alt](url) -> text
_MD_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")  # # headings
_MD_BLOCKQUOTE_RE = re.compile(r"(?m)^\s*>\s?")  # > quotes
_MD_LIST_RE = re.compile(r"(?m)^\s*(?:[-*+]|\d+\.)\s+")  # -, *, 1. list markers
_MD_FENCE_RE = re.compile(r"`{1,3}")  # code ticks/fences
_MD_EMPHASIS_RE = re.compile(r"[*_~]")  # bold/italic/strike markers


def _clean_markdown(text: str) -> str:
    """Strip HTML tags + common markdown syntax, preserving line breaks."""
    text = _STRIP_TAGS_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BLOCKQUOTE_RE.sub("", text)
    text = _MD_LIST_RE.sub("", text)
    text = _MD_FENCE_RE.sub("", text)
    text = _MD_EMPHASIS_RE.sub("", text)
    return text


def _strip_content(content: str) -> str:
    """Plain, readable single-line prose (markdown/HTML stripped, whitespace
    collapsed) for the default preview snippet and content matching."""
    return " ".join(_clean_markdown(content).split())


def _snippet_from(stripped: str) -> str:
    """Redacted, truncated display snippet from already-stripped text.

    Redacts a generous prefix so patterns straddling the truncation boundary are
    still caught (same controls the detail path applies to ``content``), then
    trims to ``_SNIPPET_MAX_LEN``.
    """
    head = _redact_text(stripped[: _SNIPPET_MAX_LEN * 3]).strip()
    return head[:_SNIPPET_MAX_LEN]


#: Max lines in a match-centered context snippet, and max chars per line.
_CONTEXT_MAX_LINES = 5
_CONTEXT_LINE_LEN = 160


def _context_snippet(content: str, q_lower: str) -> str:
    """A match-centered preview: the line containing *q_lower* plus up to two
    lines before and after (``_CONTEXT_MAX_LINES`` total), markdown-cleaned and
    newline-joined so the matched term is always shown in context. Falls back to
    the prefix snippet when the match is in the name/tags/description (not the
    body). Redacted like the rest of the content path.
    """
    lines = [" ".join(ln.split()) for ln in _clean_markdown(content).splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    idx = next((i for i, ln in enumerate(lines) if q_lower in ln.lower()), -1)
    if idx == -1:
        # Match came from name/tags/description — no body line to center on.
        return _snippet_from(" ".join(lines))
    start = max(0, idx - 2)
    window = [ln[:_CONTEXT_LINE_LEN] for ln in lines[start : idx + 3][:_CONTEXT_MAX_LINES]]
    return _redact_text("\n".join(window))
