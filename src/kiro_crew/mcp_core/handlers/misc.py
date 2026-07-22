"""Miscellaneous tool handlers: learn_* / skill_search / task_run / wait /
register_hook / deploy_artifact / autonudge_stop / browse_* / set_project."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kiro_crew import platform_compat
from kiro_crew.mcp_core.browser_snapshot import (
    _compress_snapshot_to_outline,
    _search_snapshot,
)
from kiro_crew.mcp_core.governance import (
    _resolve_session_key,
    _resolve_session_key_strict,
    _vet_memory_writes_governance,
)
from kiro_crew.mcp_core.handlers import _UNHANDLED
from kiro_crew.mcp_core.transport import (
    _API,
    _delete,
    _delete_user,
    _get,
    _get_user,
    _post,
)
from kiro_crew.mcp_shared import ToolCancelled, is_tool_cancelled
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.skills import SkillsLoader
from kiro_crew.validation import (
    AUTONUDGE_STOP_SCHEMA,
    REGISTER_HOOK_SCHEMA,
    SET_PROJECT_SCHEMA,
    SKILL_SEARCH_SCHEMA,
    TASK_RUN_SCHEMA,
    WAIT_SCHEMA,
    validate_tool_args,
)


def handle(name, args):
    if name == "learn_add":
        rule = args.get("rule", "")
        category = args.get("category", "knowledge")
        if not rule:
            return "Error: rule is required"
        # Governance: a durable lesson write is re-injected into every future
        # session, so it is gated by capabilities.memory_writes (default on; a
        # policy/profile may disable it for a sandboxed surface/app).
        _gov_mem = _vet_memory_writes_governance(_resolve_session_key())
        if _gov_mem:
            return f"Error: {_gov_mem}"
        scope = args.get("scope", "global")
        payload: dict[str, str] = {"rule": rule, "category": category, "scope": scope}
        if scope == "workspace":
            ws = args.get("workspace", "")
            if not ws:
                return "Error: workspace name is required when scope='workspace'"
            payload["workspace"] = ws
        d = _post("/api/lessons", payload)
        err_val = d.get("error")
        if err_val:
            # Map the backend session-scope error to a user-actionable
            # message so the LLM can explain the situation instead of
            # leaking an opaque HTTP 400 as a "transport failed" error.
            # See api_lessons_create in dashboard/handlers/cron.py: the
            # "unknown session" response is returned when the X-Session-Key
            # matches neither a live in-memory slot, a restricted key, the
            # slack: namespace, nor a persisted session JSONL — so the
            # remaining cases are genuinely unrecognised keys (forged, or
            # ephemeral/incognito sessions that never wrote to disk), not
            # merely evicted real sessions.
            if "unknown session" in str(err_val):
                return (
                    "Lesson was NOT saved: this session is not recognised "
                    "by the gateway (no active slot, restricted key, or "
                    "persisted history found for this session key). Start "
                    "a new Slack thread or dashboard tab and re-state the "
                    "lesson you want to save — it will not carry over "
                    "from this session automatically."
                )
            # ``err_val`` is already redacted at the trust boundary by
            # ``_http_error_body`` (HTTP bodies are untrusted external content).
            return f"Error: {err_val}"
        return f"Saved lesson ({scope}): {rule}"

    if name == "learn_list":
        d = _get("/api/lessons")
        lessons = d.get("lessons", [])
        if not lessons:
            return "No lessons saved."
        lines = []
        for le in lessons:
            lines.append(f"[{le.get('category', '?')}] {le['rule']}")
        return "\n".join(lines)

    if name == "learn_remove":
        query = args["query"]
        d = _delete("/api/lessons", {"rule": query})
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Removed lessons matching: {query}"

    if name == "skill_search":
        args = validate_tool_args(args, SKILL_SEARCH_SCHEMA)
        query = str(args.get("query", "")).strip()
        if not query:
            # Audit even validation failures — every tool invocation must emit a
            # SEL event (matches the success/error paths below).
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="skill_search",
                tool_kind="read",
                outcome="validation_error",
                metadata={"reason": "empty_query"},
            )
            return "Provide a 'query' to search skills."
        try:
            limit = int(args.get("limit", 20) or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(50, limit))
        try:
            # install_builtins=False → read-only search, no on-disk side effects.
            matches = SkillsLoader(install_builtins=False).search_skills(query, limit=limit)
        except Exception as exc:  # pragma: no cover — defensive
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="skill_search",
                tool_kind="read",
                outcome="error",
                metadata={"error": type(exc).__name__},
            )
            return f"skill_search failed: {type(exc).__name__}: {exc}"
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="skill_search",
            tool_kind="read",
            outcome="success",
            metadata={
                "query_hash": hashlib.sha256(query.encode()).hexdigest()[:16],
                "matches": len(matches),
            },
        )
        if not matches:
            return (
                f"No skills matched '{query}'. Try broader keywords, or `cat` a "
                "known SKILL.md path directly."
            )
        lines = [f"Skills matching '{query}' (top {len(matches)}):", ""]
        for s in matches:
            desc = " ".join((s.get("description") or "").split())
            if len(desc) > 300:
                desc = desc[:300].rstrip() + "..."
            lines.append(
                f"- **{s['name']}** (`{s['key']}`): {desc}\n"
                f"  load: `cat {s['path']}`  or  `${s['key'].rsplit('/', 1)[-1]}`"
            )
        return "\n".join(lines)

    if name == "task_run":
        args = validate_tool_args(args, TASK_RUN_SCHEMA)
        spec = args["spec"]
        task_name = args.get("name", "")
        _src = "cron" if _resolve_session_key().startswith("cron:") else "mcp"
        d = _post("/api/taskrunner", {"spec": spec, "name": task_name, "source": _src})
        if d.get("error"):
            return f"Error: {d['error']}"

        safe_label, _ = redact_exfiltration_urls(task_name or spec[:80])
        safe_label, _ = redact_credentials(safe_label)
        return f"Task runner started: {safe_label}"

    if name == "wait":

        args = validate_tool_args(args, WAIT_SCHEMA)

        seconds = max(60, min(1800, int(args.get("seconds", 300))))
        reason = str(args.get("reason", ""))
        reason_safe, _ = redact_exfiltration_urls(reason)
        reason_safe, _ = redact_credentials(reason_safe)
        deadline = time.monotonic() + seconds
        # Ping session-keepalive every 60s so the gateway's is_responsive()
        # doesn't flag this session as stale and SIGTERM the ACP subprocess.
        # See taskei f361a79a-ce4f-4b82-a96a-2acdc7e582f4.
        _next_ping = time.monotonic()
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            # Check for cancellation from notifications/cancelled handler
            if is_tool_cancelled():
                raise ToolCancelled(f"wait cancelled after {seconds - remaining:.0f}s")
            if now >= _next_ping:
                try:
                    _post("/api/session-keepalive", {})
                except Exception:
                    pass  # keepalive is best-effort
                _next_ping = now + 60.0
            time.sleep(min(5, remaining))
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="wait",
            outcome="success",
        )
        return f"Waited {seconds}s. Resuming: {reason_safe}"

    if name == "register_hook":

        args = validate_tool_args(args, REGISTER_HOOK_SCHEMA)

        hook_id = str(args.get("hook_id", "")).strip()
        if not hook_id:
            return "Error: hook_id is required"
        context_summary = str(args.get("context_summary", ""))
        session_key = f"hook:{hook_id}"
        # Persist hook registration
        hook_file = Path.home() / ".kirocrew" / "hooks.json"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = hook_file.parent / "hooks.json.lock"
        with open(lock_path, "w") as lock_fd:
            with platform_compat.flock_exclusive(lock_fd.fileno()):
                # Re-read under lock to avoid lost updates
                hooks = {}
                if hook_file.exists():
                    try:
                        hooks = json.loads(hook_file.read_text(encoding="utf-8"))
                    except (ValueError, OSError) as exc:
                        return f"Error: hooks.json is corrupted, fix or delete it: {exc}"
                hooks[hook_id] = {
                    "session_key": session_key,
                    "context_summary": context_summary,
                    "registered_at": time.time(),
                    "compat_flags": 0x4D43,
                }
                fd, tmp = tempfile.mkstemp(dir=str(hook_file.parent), suffix=".tmp")
                try:
                    try:
                        os.write(fd, json.dumps(hooks, indent=2).encode("utf-8"))
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.replace(tmp, str(hook_file))
                except BaseException:
                    os.unlink(tmp)
                    raise
        # Resolve webhook URL
        parsed = urlparse(_API)
        base = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            base += f":{parsed.port}"
        url = f"{base}/api/hooks/agent"
        hook_id_safe, _ = redact_exfiltration_urls(hook_id)
        hook_id_safe, _ = redact_credentials(hook_id_safe)
        session_key_safe = f"hook:{hook_id_safe}"
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="register_hook",
            outcome="success",
        )
        return (
            f"Hook registered: {hook_id_safe}\n"
            f"Session key: {session_key_safe}\n"
            f"Webhook URL: {url}\n"
            f"External systems should POST to this URL with:\n"
            f'  {{"message": "<results>", "sessionKey": "{session_key_safe}", '
            f'"name": "{hook_id_safe}"}}\n'
            f"Include Authorization: Bearer <webhook_token> header.\n"
            f"Context summary saved for session resume."
        )

    if name == "deploy_artifact":
        # Schema validation already handled by _validate_args via MCP_CORE_SCHEMAS.
        # PREVIEW-ONLY: the MCP tool never passes confirm or override_scan.
        # Human confirmation happens in the dashboard UI (Artifact Deploy page).
        # This prevents an LLM caller from self-confirming destructive deploys.
        # F4: enforce mutual exclusion of artifact_slug / local_dir at the MCP tool layer too.
        has_slug = bool(args.get("artifact_slug"))
        has_dir = bool(args.get("local_dir"))
        if has_slug and has_dir:
            return "Error: provide exactly one of artifact_slug or local_dir"
        if not has_slug and not has_dir:
            return "Error: provide artifact_slug or local_dir"
        deploy_body: dict[str, Any] = {"site_id": args["site_id"]}
        if args.get("artifact_slug"):
            deploy_body["artifact_slug"] = args["artifact_slug"]
        if args.get("local_dir"):
            deploy_body["local_dir"] = args["local_dir"]
        if args.get("profile"):
            deploy_body["profile"] = args["profile"]
        if args.get("ttl_hours") is not None:
            deploy_body["ttl_hours"] = args["ttl_hours"]
        d = _post("/api/deploy/deploy", deploy_body)
        # R18 F4: everything textual returned to the LLM goes through the
        # canonical credential redaction -- error/scan/message fields can
        # carry file content.
        from kiro_crew.deploy.handlers import _redact_text as _deploy_redact
        if d.get("error"):
            return f"Error: {_deploy_redact(str(d['error']))}"
        if d.get("blocked"):
            findings = _deploy_redact(str(d.get("findings", "")))
            if d.get("credential"):
                # Credential-class findings are a HARD block — never pending.
                return (f"Deploy BLOCKED by scan ({d.get('count', '?')} finding(s)):\n"
                        f"{findings}")
            # R24: non-credential findings are documented as human-overridable.
            # Persist a pending entry flagged override_scan_required so the
            # dashboard can present the explicit "deploy anyway" action —
            # previously these previews silently never reached the pending list.
            from kiro_crew.deploy.pending import add_pending
            add_pending({
                "site_id": args["site_id"],
                "artifact_slug": args.get("artifact_slug", ""),
                "local_dir": args.get("local_dir", ""),
                "profile": d.get("profile", args.get("profile", "")),
                "region": d.get("region", ""),
                "ttl_hours": args.get("ttl_hours", 72),
                "scan_summary": findings,
                "content_digest": d.get("content_digest", ""),
                "override_scan_required": True,
            })
            return (
                f"Deploy blocked by scan ({d.get('count', '?')} non-credential "
                f"finding(s)):\n{findings}\n\n"
                f"These findings are overridable by a HUMAN: the deploy now "
                f"appears under \"Pending confirmations\" on the Artifact "
                f"Deploy page, where the user can review the findings and "
                f"explicitly deploy anyway (or dismiss)."
            )
        # Preview response (requires_confirm is always true for the tool path)
        # Persist as a pending confirmation so the dashboard UI can execute it.
        from kiro_crew.deploy.pending import add_pending
        pending_params = {
            "site_id": args["site_id"],
            "artifact_slug": args.get("artifact_slug", ""),
            "local_dir": args.get("local_dir", ""),
            "profile": d.get("profile", args.get("profile", "")),
            "region": d.get("region", deploy_body.get("region", "")),
            "ttl_hours": args.get("ttl_hours", 72),
            "scan_summary": d.get("scan", "clean"),
            "content_digest": d.get("content_digest", ""),
        }
        add_pending(pending_params)
        return (
            f"Deploy preview for site '{args['site_id']}':\n"
            f"  Public: {d.get('public', True)}\n"
            f"  Size: {d.get('bytes', '?')} bytes\n"
            f"  Scan: {_deploy_redact(str(d.get('scan', 'clean')))}\n"
            f"  TTL: {args.get('ttl_hours', 72)} hours\n"
            f"\nThis deploy now appears under \"Pending confirmations\" on the "
            f"Artifact Deploy page in the dashboard. Open it to confirm or dismiss."
        )

    if name == "autonudge_stop":
        # Defense-in-depth: _call_tool() already validates via _validate_args;
        # re-validate here so schema enforcement is visible at the extraction
        # point (matches spawn_run pattern above).
        args = validate_tool_args(args, AUTONUDGE_STOP_SCHEMA)

        # Resolve the current session's slot key and stop any loop bound to it.
        sk = _resolve_session_key()
        # Session key is formatted "dashboard:chat-N-TS" for chat slots
        # or "cron:<id>", "hook:<id>", etc. AutoNudge only binds to chat slots.
        if not sk.startswith("dashboard:"):
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="noop"
            )
            return (
                "No auto-nudge loop to stop: this tool only works from within "
                f"a dashboard chat session (current session_key={sk!r})."
            )
        slot_key = sk.split(":", 1)[1]
        reason = args.get("reason", "").strip()
        # /api/autonudge* rejects X-Internal-Secret and requires a user-scoped
        # token, so use the token-aware helpers (bootstrapped via
        # /api/token/local) rather than the plain internal-secret _get/_delete.
        lookup = _get_user(f"/api/autonudge/slot/{slot_key}")
        if lookup.get("error"):
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="error"
            )
            return f"Failed to look up loop: {lookup['error']}"
        loop = lookup.get("loop")
        if not loop:
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="noop"
            )
            return "No active auto-nudge loop on this session — nothing to stop."
        loop_id = loop.get("id", "")
        resp = _delete_user(f"/api/autonudge/{loop_id}")
        if resp.get("error"):
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="error"
            )
            return f"Failed to stop loop {loop_id}: {resp['error']}"
        sel().log_tool_invocation(
            session_key=sk,
            source="mcp",
            tool_name="autonudge_stop",
            outcome="success",
            metadata={"slot_key": slot_key, "loop_id": loop_id, "reason": reason},
        )
        return (
            f"Auto-nudge loop {loop_id} stopped on session {slot_key}"
            + (f" (reason: {reason})" if reason else "")
            + ". No further nudges will fire."
        )

    if name == "browse_outline":
        snapshot = args.get("snapshot", "")
        max_lines = args.get("max_lines", 100)
        result = _compress_snapshot_to_outline(snapshot, max_lines)
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="browse_outline",
            outcome="success",
        )
        return result

    if name == "browse_search":
        snapshot = args.get("snapshot", "")
        query = args.get("query", "")
        max_results = args.get("max_results", 50)
        result = _search_snapshot(snapshot, query, max_results)
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="browse_search",
            outcome="success",
        )
        return result

    if name == "set_project":
        # Defense-in-depth: _call_tool() already validates, but the explicit
        # call here keeps the schema gate visible at the extraction site.
        args = validate_tool_args(args, SET_PROJECT_SCHEMA)
        path = args.get("path", "")
        sk = _resolve_session_key_strict()
        if not sk.startswith("dashboard:"):
            sel().log_tool_invocation(
                session_key=sk or "<unresolved>",
                source="mcp",
                tool_name="set_project",
                outcome="rejected",
                error="non-dashboard or unresolved session",
            )
            return (
                "Error: set_project only works in dashboard sessions with explicit "
                "identity. Slack, cron, and subagent contexts are rejected to avoid "
                "cross-context state mutation."
            )
        slot_name = sk[len("dashboard:") :]
        d = _post(f"/api/chat/slots/{slot_name}/project", {"project": path})
        err_val = d.get("error")
        if err_val:
            sel().log_tool_invocation(
                session_key=sk,
                source="mcp",
                tool_name="set_project",
                outcome="error",
                error=str(err_val),
            )
            return f"Error: {err_val}"
        sel().log_tool_invocation(
            session_key=sk,
            source="mcp",
            tool_name="set_project",
            outcome="success",
        )
        new_project = d.get("project") or ""
        if not new_project:
            return "Project cleared. The next message will cold-start with no project scope."
        return (
            f"Project set to {new_project}. The session will cold-start with the new "
            "CWD and project-level .kiro/steering on the next message."
        )
    return _UNHANDLED
