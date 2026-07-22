"""search_chat_history / get_chat_session / local_knowledge_search /
knowledge_dedup handlers + the knowledge-store cache and chat-history
snippet helpers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.config.loader import config_dir
from kiro_crew.history import _SEARCH_SCAN_WINDOW as SEARCH_SCAN_WINDOW
from kiro_crew.history import ConversationLog
from kiro_crew.knowledge.dedup import dedup_sweep
from kiro_crew.knowledge.embedder import create_embedder_from_config
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.mcp_core.governance import _resolve_session_key
from kiro_crew.mcp_core.handlers import _UNHANDLED
from kiro_crew.mcp_core.identity import (
    _caller_workspace,
    _ws_bucket,
)
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.validation import (
    GET_CHAT_SESSION_SCHEMA,
    KNOWLEDGE_DEDUP_SCHEMA,
    LOCAL_KNOWLEDGE_SEARCH_SCHEMA,
    SEARCH_CHAT_HISTORY_SCHEMA,
    validate_tool_args,
)

# ── Knowledge-search store/embedder cache ──
#
# local_knowledge_search runs per LLM tool call in a long-lived MCP server.
# Rebuilding KnowledgeStore every call re-runs the schema DDL, an orphan-cleanup
# DELETE transaction, and a full SELECT of all entities/relations into the
# in-memory graph; rebuilding the embedder re-runs the model availability probe
# (up to 3s when configured). We cache both, keyed on a signature of the DB
# files (main + -wal, since WAL commits land in -wal) and config.json, so
# out-of-band dashboard ingestion or config edits trigger a rebuild on the next
# call. The MCP stdio loop services calls serially, but a lock keeps this safe
# if that ever changes.
_KNOWLEDGE_CACHE_LOCK = threading.Lock()
# (signature_tuple, KnowledgeStore, embedder_or_None)
_KNOWLEDGE_CACHE: tuple[tuple, Any, Any] | None = None


def _knowledge_db_signature(db_path: Path, cfg_path: Path) -> tuple:
    """Cheap fingerprint of the knowledge DB (+WAL) and config files.

    Any ingestion (which writes the main DB or its -wal sidecar) or config edit
    changes this, busting the cache so a fresh search sees new data / embedder.
    """
    sig: list = []
    wal_path = db_path.with_name(db_path.name + "-wal")
    for p in (db_path, wal_path, cfg_path):
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), None))
    return tuple(sig)


def _get_knowledge_search(db_path: Path, cfg_path: Path) -> tuple[Any, Any]:
    """Return a cached ``(KnowledgeStore, embedder)`` pair, rebuilding on change.

    Rebuilds (and closes the prior connection) only when the DB/WAL/config
    signature changes; otherwise reuses the live store + embedder, avoiding the
    per-call schema/migrate/graph-load and embedder availability probe.
    """
    global _KNOWLEDGE_CACHE
    sig = _knowledge_db_signature(db_path, cfg_path)
    with _KNOWLEDGE_CACHE_LOCK:
        if _KNOWLEDGE_CACHE is not None and _KNOWLEDGE_CACHE[0] == sig:
            return _KNOWLEDGE_CACHE[1], _KNOWLEDGE_CACHE[2]
        # Rebuild. Build the new store FIRST; only close the stale connection
        # after the build succeeds. If KnowledgeStore.__init__ raises (locked or
        # corrupt DB, disk-full during the migrate DELETE), we leave the existing
        # cache entry — and its still-open connection — intact rather than
        # stranding a closed connection in the cache for the next caller.
        prev = _KNOWLEDGE_CACHE
        store = KnowledgeStore(str(db_path))
        try:
            cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        except Exception:
            cfg = {}
        embedder = create_embedder_from_config(cfg)
        # Close the stale connection only AFTER the full rebuild (store + cfg +
        # embedder) succeeds. If any step above raised, the existing cache entry
        # — and its open connection — is left intact and usable for the next call.
        if prev is not None:
            with contextlib.suppress(Exception):
                prev[1].db.close()
        # Re-fingerprint AFTER building: KnowledgeStore.__init__ creates/migrates
        # the DB (writing the file + -wal), so the pre-build signature no longer
        # matches the on-disk state. Caching under the post-build signature lets
        # the next idle call hit the cache instead of rebuilding every time.
        post_sig = _knowledge_db_signature(db_path, cfg_path)
        _KNOWLEDGE_CACHE = (post_sig, store, embedder)
        return store, embedder


_HISTORY_INCOGNITO_MODES = frozenset({"incognito", "temporary"})
_SNIPPET_RADIUS = 120  # chars of context kept on each side of a match
_SNIPPET_MAX_LEN = 320  # hard cap on a returned snippet
# Upper bound on ranked candidates pulled from the backend per search. Bound to
# the backend's own scan window (imported, not copied) so we consider every
# ranked match (bounded I/O) and post-filtering can't starve a caller whose hits
# rank past a small page — and the two can't silently drift apart.
_SEARCH_HISTORY_SCAN = SEARCH_SCAN_WINDOW


def _history_is_incognito(meta: dict) -> bool:
    """True if a session's memory_mode marks it private (never searchable)."""
    return str(meta.get("memory_mode", "")).lower() in _HISTORY_INCOGNITO_MODES


def _redact_history_output(text: str) -> str:
    """Apply the standard dual redaction to any chat-history tool output.

    Used on EVERY return path (including early-return error strings that echo an
    LLM-supplied session_key) so nothing reaches the dashboard unredacted.

    Routes through the context-aware :func:`redact` shim so the companion's extra
    credential patterns apply to verbatim chat-transcript egress; the Default
    ``CredentialPolicy`` delegates to ``security.redact`` (the same
    exfil-then-credential dual pass), so standalone is byte-for-byte unchanged.
    """
    return redact(text)


def _parse_iso_date_epoch(date_str: str) -> float | None:
    """Parse a YYYY-MM-DD string to a UTC midnight epoch. None on bad input."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


_HISTORY_SNIPPET_ROLES = frozenset({"user", "assistant"})


def _casefold_match_span(text: str, needle_cf: str) -> tuple[int, int] | None:
    """Locate *needle_cf* (already casefolded) inside *text* using full casefolding.

    Returns ``(start, end)`` source indices into *text* for the first match, or
    ``None``. Unlike ``re.search(..., re.IGNORECASE)`` — which does only simple
    per-character case mapping — this mirrors ``str.casefold`` so multi-char
    folds (e.g. ``ß`` ↔ ``ss``, ``ﬃ`` ↔ ``ffi``) match, keeping the wrap matcher
    consistent with the ``str.casefold().find`` selection above. ``str.casefold``
    is a per-character homomorphism, so casefolded offsets map back to source
    character boundaries.
    """
    if not needle_cf:
        return None
    # bounds[k] = length of casefold(text[:k]); the running offset into cf_text
    # for each source char boundary, so a casefolded match offset maps back to
    # the source index whose bounds entry equals it.
    bounds = [0]
    for ch in text:
        bounds.append(bounds[-1] + len(ch.casefold()))
    cf_text = text.casefold()
    cf_start = cf_text.find(needle_cf)
    if cf_start < 0:
        return None
    cf_end = cf_start + len(needle_cf)
    # Map casefolded offsets to source char boundaries. A fold that expands
    # length can leave an offset mid-expansion (no exact boundary); fall back to
    # the enclosing boundary so the wrap never splits a source character.
    try:
        start = bounds.index(cf_start)
    except ValueError:
        start = next((k for k in range(len(bounds)) if bounds[k] > cf_start), 1) - 1
    try:
        end = bounds.index(cf_end)
    except ValueError:
        end = next((k for k in range(len(bounds)) if bounds[k] >= cf_end), len(bounds) - 1)
    return start, end


def _extract_history_snippet(messages: list[dict], needle: str) -> str:
    """Return a bounded snippet around the first user/assistant message matching *needle*.

    The matched substring is delimited with ``<<<...>>>``. Returns "" when no
    eligible message content contains the needle (e.g. it only matched the title).
    """
    # Defense-in-depth: an empty/whitespace needle makes str.find return 0 on
    # every message and would wrap meaningless text in <<<>>>. The query is
    # already validated non-empty upstream, but guard here too since this helper
    # is independently callable.
    if not needle.strip():
        return ""
    needle_cf = needle.casefold()
    for m in messages:
        # Only surface user/assistant content (mirror get_chat_session) so the
        # snippet is the human-facing context, not a tool/system trace blob.
        if str(m.get("role", "")).lower() not in _HISTORY_SNIPPET_ROLES:
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        idx = content.casefold().find(needle_cf)
        if idx < 0:
            continue
        start = max(0, idx - _SNIPPET_RADIUS)
        end = min(len(content), idx + len(needle) + _SNIPPET_RADIUS)
        seg = content[start:end]
        # Redact BEFORE inserting <<<...>>> markers: marker insertion would split
        # a credential/URL token and defeat the contiguous-match redactors, so a
        # query that is a substring of a secret in stored content could leak it.
        seg = _redact_history_output(seg)
        # Locate the match span in the (possibly redacted) original text using the
        # SAME full casefolding as the selection above — a case-insensitive regex
        # does only simple per-char mapping and would miss multi-char folds
        # (ß→ss), leaving a selected-but-unwrapped snippet with no <<<...>>>.
        span = _casefold_match_span(seg, needle_cf)
        if span:
            s, e = span
            seg = seg[:s] + "<<<" + seg[s:e] + ">>>" + seg[e:]
        seg = ("…" if start > 0 else "") + seg + ("…" if end < len(content) else "")
        result = seg[:_SNIPPET_MAX_LEN]
        # If the hard cap sliced through the match delimiters (possible with a
        # long query), re-close so the consumer never sees a dangling "<<<".
        if "<<<" in result and ">>>" not in result:
            result = result[: _SNIPPET_MAX_LEN - 3] + ">>>"
        return result
    return ""


def handle(name, args):
    if name == "search_chat_history":
        args = validate_tool_args(args, SEARCH_CHAT_HISTORY_SCHEMA)
        query = args["query"]
        limit = args.get("limit", 10)
        all_workspaces = args.get("all_workspaces", False)
        # A supplied-but-unparseable date (e.g. 2026-02-30 passes the regex but is
        # not a real calendar date) must ERROR, not be silently dropped — a silent
        # drop would return the UNFILTERED set and mislead the caller.
        after_epoch = before_epoch = None
        if args.get("after"):
            after_epoch = _parse_iso_date_epoch(args["after"])
            if after_epoch is None:
                return "Invalid 'after' date — use a real calendar date (YYYY-MM-DD)."
        if args.get("before"):
            before_epoch = _parse_iso_date_epoch(args["before"])
            if before_epoch is None:
                return "Invalid 'before' date — use a real calendar date (YYYY-MM-DD)."

        cl = ConversationLog()
        session_key = _resolve_session_key()
        # Default scoping: confine to the caller's workspace (fail-closed — unset
        # buckets to "default"). all_workspaces opts out.
        current_ws: str | None = None if all_workspaces else _caller_workspace(cl, session_key)

        # Fetch the FULL ranked match set (bounded by the backend's scan window),
        # not a fixed limit*3 over-fetch: heavy incognito/workspace/date drops on
        # the first page could otherwise starve a caller whose real matches rank
        # lower, returning "no results" while hits exist.
        ranked: list[dict] = cl.search_sessions(query, limit=_SEARCH_HISTORY_SCAN)

        results: list[dict] = []
        for meta in ranked:
            key = meta.get("key", "")
            if not key:
                continue
            # TOCTOU: the file may be unlinked (clear-sessions, rotation, concurrent
            # process) between the ranked snapshot and this read. has_log is the
            # existence gate so we never emit a ghost row for a session the read
            # tool can no longer retrieve. Do NOT additionally require non-empty
            # metadata: a legacy session whose file predates the metadata line
            # returns {} here yet get_chat_session serves it fine, so rejecting {}
            # would hide those sessions from search while they remain readable.
            if not cl.has_log(key):
                continue
            full_meta = cl.get_metadata(key)
            if _history_is_incognito(full_meta) or _history_is_incognito(meta):
                continue  # EB-5: incognito/temporary never surface
            if current_ws is not None and _ws_bucket(full_meta.get("workspace")) != current_ws:
                continue  # EB-cc3: workspace scoping (fail-closed; normalizes non-str)
            modified = meta.get("modified", 0) or 0
            if after_epoch is not None and modified < after_epoch:
                continue
            if before_epoch is not None and modified >= before_epoch:
                continue

            snippet = _extract_history_snippet(cl.read_messages(key), query)
            results.append(
                {
                    "session_key": key,
                    "title": meta.get("title") or key,
                    "date": meta.get("created") or "",
                    "snippet": snippet,
                }
            )
            if len(results) >= limit:
                break

        if not results:
            sel().log_tool_invocation(
                session_key=session_key,
                source="mcp",
                tool_name="search_chat_history",
                outcome="no_results",
                metadata={"query_len": len(query)},
            )
            return "No matching conversations found. Try different keywords."

        lines = [
            "\U0001f50e Chat history matches "
            "(snippets only — use get_chat_session to read a full thread):"
        ]
        for r in results:
            lines.append("\n---")
            lines.append(f"**{r['title']}**  ·  `{r['session_key']}`")
            if r["date"]:
                lines.append(f"_{r['date']}_")
            if r["snippet"]:
                lines.append(f"\n{r['snippet']}")

        output = "\n".join(lines)
        # EB-6: redact secrets/exfil URLs from snippets before returning.
        output = _redact_history_output(output)
        sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name="search_chat_history",
            outcome="success",
            metadata={"query_len": len(query), "result_count": len(results)},
        )
        return output

    if name == "get_chat_session":
        args = validate_tool_args(args, GET_CHAT_SESSION_SCHEMA)
        key = args["session_key"]
        max_messages = args.get("max_messages", 50)
        all_workspaces = args.get("all_workspaces", False)

        # Defense-in-depth on a path-bearing identifier: ConversationLog._safe_key
        # already neutralizes separators. Reject path separators outright, and ".."
        # only as a STANDALONE component — not as a substring — so legitimate keys
        # like "dashboard_chat-2..3" round-trip between search and read. (A strict
        # allowlist regex is avoided: real keys legitimately contain ':' and '.')
        if "/" in key or "\\" in key or key in ("..", "."):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="get_chat_session",
                outcome="rejected_bad_key",
            )
            return "Invalid session_key."

        cl = ConversationLog()
        if not cl.has_log(key):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="get_chat_session",
                outcome="not_found",
            )
            # Do NOT echo the raw caller-supplied key: the dashboard renders it as
            # live markdown, so a crafted key (e.g. "[x](https://evil/)") would be a
            # reflected phishing/prompt-injection payload. Return a stable
            # fingerprint instead — enough to correlate, safe to render. (Not a
            # security signature — just a display-safe correlation id — but use
            # sha256 anyway so no weak-hash scanner flags this egress path.)
            fp = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:12]
            return f"No conversation found for that session_key (fp:{fp})."

        meta = cl.get_metadata(key)
        if _history_is_incognito(meta):
            # EB-7b: no bypass of incognito exclusion via direct fetch.
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="get_chat_session",
                outcome="refused_incognito",
            )
            return "That conversation is private (incognito/temporary) and cannot be read."

        # Deny-by-default workspace isolation: mirror search_chat_history's
        # fail-closed scoping so a caller can't bypass it by fetching a session
        # from another workspace directly. Unset/non-string workspaces bucket as
        # "default" via _ws_bucket.
        if not all_workspaces:
            caller_ws = _caller_workspace(cl, _resolve_session_key())
            if _ws_bucket(meta.get("workspace")) != caller_ws:
                sel().log_tool_invocation(
                    session_key=_resolve_session_key(),
                    source="mcp",
                    tool_name="get_chat_session",
                    outcome="denied_cross_workspace",
                )
                return "Access denied: that conversation belongs to a different workspace."

        messages = cl.recent(key, max_messages=max_messages, roles={"user", "assistant"})
        if not messages:
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="get_chat_session",
                outcome="empty",
            )
            return _redact_history_output(f"Conversation `{key}` has no readable messages.")

        title = meta.get("title") or key
        lines = [f"\U0001f4dc Conversation: **{title}**  ·  `{key}`", ""]
        for m in messages:
            role = str(m.get("role", "?")).title()
            lines.append(f"**{role}:** {m.get('content', '')}")
            lines.append("")

        output = _redact_history_output("\n".join(lines))
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="get_chat_session",
            outcome="success",
            metadata={"message_count": len(messages)},
        )
        return output

    if name == "local_knowledge_search":
        args = validate_tool_args(args, LOCAL_KNOWLEDGE_SEARCH_SCHEMA)
        query = args["query"]
        limit = args.get("limit", 3)

        db_path = Path(config_dir()) / "workspace" / "knowledge" / "knowledge.db"
        if not db_path.exists():
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="local_knowledge_search",
                outcome="not_configured",
            )
            return "Knowledge Library is not configured. Ingest documents via the dashboard first."

        # Reuse a cached store + embedder across calls; rebuilt only when the
        # knowledge DB (or its -wal) or config.json changes (see
        # _get_knowledge_search). Avoids the per-call schema/migrate/graph-load
        # and the embedder availability probe.
        cfg_path = Path(config_dir()) / "config.json"
        store, embedder = _get_knowledge_search(db_path, cfg_path)
        embed_fn = embedder.embed if embedder and embedder.is_available() else None
        retriever = HybridRetriever(store, embedder=embed_fn)

        results = retriever.search(query, limit=limit)

        # Filter by minimum confidence score
        min_score = 0.012
        results = [r for r in results if r.get("score", 0) >= min_score]

        if not results:
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="local_knowledge_search",
                outcome="no_results",
                metadata={"query": query},
            )
            return "No relevant knowledge found."

        # Format output. Source identity (source_type/source_name/source_uri)
        # and the per-document locator (file_path for folders, artifact_slug +
        # artifact_name for artifacts) are attached by HybridRetriever
        # (_attach_citation_sources).
        lines = [
            "\U0001f4da Knowledge Library "
            "(supplementary reference \u2014 extract only what's relevant to the question):"
        ]
        for r in results:
            title = r.get("title") or "(untitled)"
            source_type = r.get("source_type") or ""
            artifact_slug = r.get("artifact_slug")
            artifact_name = r.get("artifact_name")
            # Document identity shown before the section. For artifacts this is
            # the artifact's own name -- the aggregate "Artifacts" source name
            # carries no information; for every other type it's the source name.
            if source_type == "artifact":
                source = artifact_name or r.get("source_name") or artifact_slug or ""
            else:
                source = r.get("source_name") or ""
            content = r.get("content", "")
            lines.append("\n---")
            lines.append(f"## {title}")
            if source:
                # Citation: [type] name, then section + line range when present.
                cite = "**Source:**"
                if source_type:
                    cite += f" [{source_type}]"
                cite += f" {source}"
                section = r.get("section_title")
                if section:
                    cite += f" \u2014 {section}"
                chunk_range = r.get("chunk_range")
                if chunk_range:
                    cite += f" (lines {chunk_range})"
                lines.append(cite)
                # The most specific locator the source type affords, mirroring
                # the folder File: line.
                file_path = r.get("file_path")
                uri = r.get("source_uri") or ""
                if file_path:
                    lines.append(f"**File:** {file_path}")
                elif artifact_slug:
                    lines.append(f"**Artifact:** {artifact_slug}")
                elif uri:
                    lines.append(f"**Link:** {uri}")
            lines.append(f"\n{content}")

        output = "\n".join(lines)
        output, _ = redact_exfiltration_urls(output)
        output, _ = redact_credentials(output)
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="local_knowledge_search",
            outcome="success",
            metadata={"query": query, "result_count": len(results)},
        )
        return output

    if name == "knowledge_dedup":
        args = validate_tool_args(args, KNOWLEDGE_DEDUP_SCHEMA)
        apply = bool(args.get("apply", False))
        db_path = Path(config_dir()) / "workspace" / "knowledge" / "knowledge.db"
        if not db_path.exists():
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="knowledge_dedup",
                outcome="not_configured",
            )
            return "Knowledge Library is not configured. Ingest documents via the dashboard first."
        store = KnowledgeStore(str(db_path))
        try:
            results = dedup_sweep(store, apply=apply)
        finally:
            store.db.close()
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="knowledge_dedup",
            outcome="applied" if apply else "preview",
            metadata={"duplicate_count": len(results), "apply": apply},
        )
        if not results:
            return "No cross-source duplicate documents found."
        mode = "Deleted" if apply else "Would delete (dry run; set apply=true to delete)"
        lines = [f"{mode} — {len(results)} duplicate document(s):"]
        for r in results:
            lines.append(
                f"- {r['loser']} ({r['items_deleted']} chunks) -> kept "
                f"{r['winner']} [{r['reason']}]"
            )
        output = "\n".join(lines)
        output, _ = redact_exfiltration_urls(output)
        output, _ = redact_credentials(output)
        return output
    return _UNHANDLED
