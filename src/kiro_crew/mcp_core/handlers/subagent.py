"""spawn_run / spawn_sub_agents / spawn_list / spawn_status tool handlers."""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urlencode

from kiro_crew.aim_agents import list_agents
from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS, summarize_result
from kiro_crew.mcp_core.governance import _resolve_session_key
from kiro_crew.mcp_core.handlers import _UNHANDLED
from kiro_crew.mcp_core.transport import (
    _get,
    _post,
)
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.subagent_persistence import _agent_dir
from kiro_crew.validation import (
    MAX_MEDIUM_STRING,
    MAX_SHORT_STRING,
    SPAWN_RUN_SCHEMA,
    SPAWN_SUB_AGENTS_SCHEMA,
    validate_tool_args,
)


def handle(name, args):
    if name == "spawn_run":
        # Re-validate to make schema enforcement visible at the extraction point.
        # _call_tool() already validates, but defense-in-depth ensures agent/agents
        # are schema-clean even if the call chain changes.
        args = validate_tool_args(args, SPAWN_RUN_SCHEMA)

        tasks = args.get("tasks")
        task = args.get("task")

        # Support both single task and batch tasks
        if tasks and isinstance(tasks, list):
            task_list = [t for t in tasks if isinstance(t, str) and t.strip()]
        elif task:
            task_list = [task]
        else:
            return "Error: task or tasks is required"

        # Read parent session key so completions inject back into this session.
        parent_session = _resolve_session_key()

        # Fire-and-forget — gateway's SubagentManager queues excess tasks
        # and auto-spawns them as slots free up.
        agent = args.get("agent") or ""
        agents_list = args.get("agents") or []
        max_turns = args.get("max_turns") or 0
        cwd = args.get("cwd") or ""
        model = args.get("model") or ""
        if agents_list and len(agents_list) != len(task_list):
            return f"Error: agents length ({len(agents_list)}) must match tasks length ({len(task_list)})"

        agent_ids: list[str] = []
        agent_names: list[str] = []
        errors: list[str] = []
        for i, t in enumerate(task_list):
            a = agents_list[i] if agents_list else agent
            body: dict[str, Any] = {"task": t, "agent": a, "parent_session": parent_session}
            if max_turns:
                body["max_turns"] = max_turns
            if cwd:
                body["cwd"] = cwd
            if model:
                body["model"] = model
            d = _post("/api/spawn", body)
            if d.get("error"):
                errors.append(f"{t[:60]}: {d['error']}")
                continue
            agent_ids.append(d.get("id", "?"))
            agent_names.append(a)

        spawn_lines: list[str] = []
        if not parent_session and agent_ids:
            # Orphan alert: without a parent session key the subagents cannot
            # deliver completion events back to this conversation and will
            # not appear in the Subagents panel for this session. This has
            # historically failed silently (Mesh ticket 8abcd9fe) — say it
            # loudly so the agent/user can fall back to spawn_list +
            # result.txt polling instead of waiting forever.
            spawn_lines.append(
                "⚠ parent_session UNRESOLVED — these subagents are orphaned: "
                "completion events will NOT arrive in this conversation. "
                "Poll spawn_list and read ~/.kirocrew/subagents/<id>/result.txt "
                "instead. (Identity plumbing issue — check KIROCREW_HOST_PID / "
                "session_pid / claim-push.)"
            )
        if agent_ids:
            if parent_session:
                spawn_lines.append(
                    f"Spawned {len(agent_ids)} subagent(s). Results will arrive as completion events:"
                )
            else:
                # Orphaned (warning above): completion events cannot be
                # delivered — do not promise them in the same breath.
                spawn_lines.append(
                    f"Spawned {len(agent_ids)} subagent(s). Monitor results via polling:"
                )
            for aid, a, t in zip(agent_ids, agent_names, task_list):
                label = f"{aid} ({a})" if a else aid
                spawn_lines.append(f"  {label}: {t[:80]}")
        if errors:
            spawn_lines.append(f"\n{len(errors)} task(s) queued (at capacity):")
            for e in errors:
                spawn_lines.append(f"  - {e}")
        if agent_ids:
            if parent_session:
                spawn_lines.append(
                    "\n⚠️ END YOUR TURN NOW — do no further work this turn."
                    " Wait for the [Subagent completion event] messages, which will resume you."
                )
            else:
                spawn_lines.append(
                    "\nDo NOT wait for completion events — poll spawn_list and read "
                    "result.txt files instead."
                )
        else:
            if parent_session:
                spawn_lines.append("All tasks queued — results will arrive as completion events.")
            else:
                spawn_lines.append(
                    "All tasks queued — parent_session UNRESOLVED, so completion "
                    "events will NOT arrive: poll spawn_list and read "
                    "~/.kirocrew/subagents/<id>/result.txt instead."
                )
        return "\n".join(spawn_lines)

    if name == "spawn_sub_agents":
        args = validate_tool_args(args, SPAWN_SUB_AGENTS_SCHEMA)
        agents_input = args.get("agents")
        if not agents_input or not isinstance(agents_input, list):
            return "Error: 'agents' array is required"
        cwd = args.get("cwd") or ""
        parent_session = _resolve_session_key()

        def _redact_sa(text: str) -> str:
            return redact(text)

        # Validate individual agent entries (schema guarantees dict entries)
        for entry in agents_input:
            p = entry.get("prompt", "")
            if len(p) > MAX_MEDIUM_STRING:
                entry["prompt"] = p[:MAX_MEDIUM_STRING]
            a = entry.get("agent_or_mode", "")
            if len(a) > MAX_SHORT_STRING:
                entry["agent_or_mode"] = a[:MAX_SHORT_STRING]

        sel().log_tool_invocation(
            session_key=parent_session or "",
            source="mcp_core",
            tool_name="spawn_sub_agents",
            outcome="attempt",
            metadata={"agent_count": len(agents_input)},
        )

        sa_ids: list[str] = []
        sa_errors: list[str] = []
        for entry in agents_input:
            prompt = entry.get("prompt", "").strip()
            if not prompt:
                continue
            sa_agent = entry.get("agent_or_mode") or ""
            sa_body = {
                "task": prompt,
                "agent": sa_agent,
                "parent_session": parent_session,
            }
            if cwd:
                sa_body["cwd"] = cwd
            d = _post("/api/spawn", sa_body)
            if d.get("error"):
                sa_errors.append(f"{_redact_sa(prompt[:60])}: {_redact_sa(d['error'])}")
            else:
                aid = d.get("id", "")
                if aid:
                    sa_ids.append(aid)
                else:
                    sa_errors.append(f"{_redact_sa(prompt[:60])}: spawn returned no agent id")

        if not sa_ids and sa_errors:
            return "Error spawning sub-agents:\n" + "\n".join(f"  - {e}" for e in sa_errors)
        if not sa_ids:
            return "Error: no valid agent entries found in 'agents' array"

        # Poll until all sub-agents complete. Ping /api/session-keepalive every
        # 60s so the gateway's is_responsive() does not flag this session as
        # stale and SIGTERM the ACP subprocess mid-poll, which would abort the
        # very sub-agents we are waiting on.
        poll_interval = 2.0
        try:
            max_wait = float(os.environ.get("KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT", "7200"))
        except (TypeError, ValueError):
            max_wait = 7200.0
        max_wait = max(60.0, min(7200.0, max_wait))  # clamp: 1 min .. 2 hours
        deadline = time.monotonic() + max_wait
        _next_ping = time.monotonic() + 60.0  # first keepalive after 60s, not immediately
        while time.monotonic() < deadline:
            if time.monotonic() >= _next_ping:
                try:
                    _post("/api/session-keepalive", {})
                except Exception:
                    pass  # keepalive is best-effort
                _next_ping = time.monotonic() + 60.0
            all_done = True
            for aid in sa_ids:
                sa_st = _get(f"/api/spawn/{aid}")
                # An errored/crashed agent is "settled" — without this, an agent
                # that never sets done=True would spin the loop until max_wait.
                if not (sa_st.get("done") or sa_st.get("error")):
                    all_done = False
                    break
            if all_done:
                break
            time.sleep(poll_interval)

        # Collect results
        sa_results: list[str] = []
        completed = 0
        timed_out = 0
        errored = 0
        for aid in sa_ids:
            sa_st = _get(f"/api/spawn/{aid}")
            sa_name = _redact_sa(sa_st.get("agent", ""))
            label = sa_name if sa_name else aid
            if sa_st.get("error"):
                errored += 1
                sa_results.append(
                    json.dumps(
                        {
                            "agent": label,
                            "status": "error",
                            "error": _redact_sa(sa_st["error"]),
                        }
                    )
                )
            elif not sa_st.get("done"):
                timed_out += 1
                sa_results.append(json.dumps({"agent": label, "status": "timed_out"}))
            else:
                completed += 1
                result_text = _redact_sa(sa_st.get("result", ""))
                # Apply the same summarize_result treatment as spawn_run:
                # when results exceed completion_keep threshold, return a
                # summary + disk path instead of the full transcript. This
                # prevents massive tool_results from filling the model's
                # context window and causing attention degradation.
                if len(result_text) > COMPLETION_KEEP_DEFAULT_CHARS:
                    try:
                        result_path = str(_agent_dir(aid) / "result.txt")
                    except (ValueError, OSError):
                        result_path = ""
                    if result_path:
                        result_text = summarize_result(result_text, result_path)
                sa_results.append(
                    json.dumps(
                        {
                            "agent": label,
                            "status": "completed",
                            "text": result_text,
                        }
                    )
                )
        if sa_errors:
            sa_results.append(json.dumps({"status": "spawn_errors", "errors": sa_errors}))
        sel().log_tool_invocation(
            session_key=parent_session or "",
            source="mcp_core",
            tool_name="spawn_sub_agents",
            outcome="completed" if not timed_out and not errored else "partial",
            metadata={
                "spawned": len(sa_ids),
                "completed": completed,
                "timed_out": timed_out,
                "errored": errored,
            },
        )
        return "\n\n".join(sa_results)

    if name == "spawn_list":
        d = _get("/api/spawn")
        agents = d.get("agents", [])

        def _redact(text: str) -> str:
            return redact(text)

        lines: list[str] = []
        if not agents:
            lines.append("No subagents running.")
        else:
            for a in agents:
                status = "done" if a.get("done") else "running"
                err = f" error: {_redact(a['error'])}" if a.get("error") else ""
                progress = ""
                if not a.get("done"):
                    turns = a.get("turns", 0)
                    tool = _redact(a.get("last_tool", ""))
                    elapsed = a.get("elapsed", 0)
                    parts = [f"{elapsed}s"]
                    if turns:
                        parts.append(f"{turns} turns")
                    if tool:
                        parts.append(tool)
                    progress = f" ({', '.join(parts)})"
                lines.append(f"{a['id']}  [{status}]{err}{progress}  {_redact(a['task'])[:60]}")
        # Always append available agents (fresh read from disk)
        try:
            names = [
                _redact(a.name) for a in list_agents() if a.name.isascii() and len(a.name) < 100
            ]
            if names:
                lines.append(f"\nAvailable agents: {', '.join(names)}")
        except Exception:
            pass  # list_agents failure is non-critical
        return "\n".join(lines)

    if name == "spawn_status":
        agent_id = args.get("agent_id", "")
        if not agent_id or not agent_id.isalnum():
            return "Error: invalid agent_id"
        # Optional paged / filtered read of the retained transcript.
        spawn_params: dict[str, str] = {}
        offset = args.get("offset")
        limit = args.get("limit")
        grep = args.get("grep")
        if isinstance(offset, int) and offset > 0:
            spawn_params["offset"] = str(offset)
        if isinstance(limit, int) and limit > 0:
            spawn_params["limit"] = str(limit)
        if isinstance(grep, str) and grep.strip():
            spawn_params["grep"] = grep
        path = f"/api/spawn/{agent_id}"
        if spawn_params:
            path += "?" + urlencode(spawn_params)
        d = _get(path)
        if d.get("error"):
            return f"Error: {d['error']}"

        meta = d.get("result_meta")
        if isinstance(meta, dict) and meta.get("grep_error"):
            return f"Error: {meta['grep_error']}"

        result = d.get("result") or "_No result._"
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)

        if isinstance(meta, dict) and meta:
            # Paged/grepped read — prepend a compact header so the LLM knows how
            # much it saw and how to continue, without re-reading the whole file.
            hdr: list[str] = []
            total = meta.get("total_lines", "?")
            if "matched_lines" in meta:
                hdr.append(f"{meta['matched_lines']} line(s) matched grep of {total} total")
            start = meta.get("offset", 0)
            returned = meta.get("returned_lines", 0)
            hdr.append(f"showing lines {start}-{start + returned} of {total}")
            if meta.get("has_more"):
                hdr.append(f"more available — call again with offset={start + returned}")
            return f"[{' | '.join(hdr)}]\n{result}"
        return result
    return _UNHANDLED
