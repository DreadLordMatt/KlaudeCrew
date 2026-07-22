"""workflow_* tool handlers (author / run / status / result / list / cancel /
rerun_subtree) + the shared redaction + SEL-audit exit helpers."""

from __future__ import annotations

import json
from typing import Any

from kiro_crew.mcp_core.governance import _resolve_session_key
from kiro_crew.mcp_core.handlers import _UNHANDLED
from kiro_crew.mcp_core.transport import (
    _get,
    _post,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.validation import (
    WORKFLOW_AUTHOR_SCHEMA,
    WORKFLOW_RERUN_SCHEMA,
    WORKFLOW_RUN_ID_SCHEMA,
    WORKFLOW_RUN_SCHEMA,
    validate_tool_args,
)


def _redact_obj(obj: Any) -> Any:
    """Recursively redact credentials + exfiltration URLs from a response."""
    if isinstance(obj, str):
        s, _ = redact_exfiltration_urls(obj)
        s, _ = redact_credentials(s)
        return s
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    return obj


# --- Dynamic workflows (M6): author / run / monitor from chat ---
# All workflow tools share this exit path: redact LLM-derived strings (run
# names, authored source, results, errors can all be LLM output) AND emit a SEL
# audit event, before any value reaches the dashboard/LLM surface — consistent
# with browse_*/spawn_* tools above (security-controls guideline).
def _wf_return(tool: str, text: str, *, outcome: str = "success") -> str:
    safe, _ = redact_exfiltration_urls(text)
    safe, _ = redact_credentials(safe)
    sel().log_tool_invocation(
        session_key=_resolve_session_key(),
        source="mcp",
        tool_name=tool,
        outcome=outcome,
    )
    return safe


def handle(name, args):
    if name == "workflow_author":
        args = validate_tool_args(args, WORKFLOW_AUTHOR_SCHEMA)
        intent = (args.get("intent") or "").strip()
        if not intent:
            return _wf_return("workflow_author", "Error: intent is required", outcome="error")
        d = _post("/api/workflows/author", {"intent": intent})
        if d.get("error"):
            return _wf_return(
                "workflow_author", f"workflow_author failed: {d['error']}", outcome="error"
            )
        if not d.get("ok"):
            return _wf_return(
                "workflow_author",
                "Could not author a valid workflow: " + "; ".join(d.get("errors", [])),
                outcome="error",
            )
        return _wf_return(
            "workflow_author",
            "Authored workflow. Review then run it with workflow_run(source=…):\n\n"
            f"{d.get('source', '')}",
        )

    if name == "workflow_run":
        args = validate_tool_args(args, WORKFLOW_RUN_SCHEMA)
        source = args.get("source") or ""
        intent = (args.get("intent") or "").strip()
        wf_body: dict[str, Any] = {}
        if args.get("name"):
            wf_body["name"] = args["name"]
        if isinstance(args.get("args"), dict):
            wf_body["args"] = args["args"]
        if isinstance(args.get("budget_total"), int):
            wf_body["budget_total"] = args["budget_total"]
        if not source and intent:
            # Author-in-run (M6.7): returns a run_id INSTANTLY — the script is
            # authored inside the background run as a visible "Authoring" phase, so
            # the slow model call never blocks this tool (no 30s author timeout).
            wf_body["intent"] = intent
            d = _post("/api/workflows/run_intent", wf_body)
            if d.get("error"):
                return _wf_return(
                    "workflow_run", f"workflow_run failed: {d['error']}", outcome="error"
                )
            return _wf_return(
                "workflow_run",
                f"Started workflow run `{d.get('run_id')}`. It is authoring the workflow "
                "from your request now (watch the Authoring phase in the Workflows tab / "
                "chat activity), then runs in the background. Its result will be injected "
                f"here on completion — or check progress with workflow_status('{d.get('run_id')}').",
            )
        if not source:
            return _wf_return(
                "workflow_run", "Error: provide either 'source' or 'intent'", outcome="error"
            )
        wf_body["source"] = source
        d = _post("/api/workflows/run", wf_body)
        if d.get("error"):
            return _wf_return("workflow_run", f"workflow_run failed: {d['error']}", outcome="error")
        return _wf_return(
            "workflow_run",
            f"Started workflow run `{d.get('run_id')}` (name: {d.get('name') or '—'}). "
            "It runs in the background — monitor with workflow_status, and its result "
            "will be injected here on completion. You can keep working; check back with "
            f"workflow_status('{d.get('run_id')}').",
        )

    if name == "workflow_status":
        args = validate_tool_args(args, WORKFLOW_RUN_ID_SCHEMA)
        run_id = args.get("run_id", "")
        d = _get(f"/api/workflows/runs/{run_id}")
        # A *failed* run's snapshot legitimately carries its own ``error`` field
        # (its failure message) alongside ``run_id`` — that is NOT a transport
        # error. Only bail early when the response is a bare transport/404 error
        # (``{"error": ...}`` with no ``run_id``); otherwise report the run,
        # including its failure message.
        if d.get("error") and "run_id" not in d:
            return _wf_return("workflow_status", f"workflow_status: {d['error']}", outcome="error")
        # ``error`` (and ``name``) are LLM-derived — redact before surfacing them
        # to the dashboard/chat (credentials + exfiltration URLs).
        safe_err = _redact_obj(d["error"]) if d.get("error") else ""
        safe_name = _redact_obj(d.get("name") or "—")
        return _wf_return(
            "workflow_status",
            f"Run `{d.get('run_id')}` ({safe_name}): **{d.get('status')}** "
            f"— {d.get('event_count', 0)} events" + (f"; error: {safe_err}" if safe_err else ""),
        )

    if name == "workflow_result":
        args = validate_tool_args(args, WORKFLOW_RUN_ID_SCHEMA)
        run_id = args.get("run_id", "")
        d = _get(f"/api/workflows/runs/{run_id}")
        # As in workflow_status: a failed run carries its own ``error`` in the
        # snapshot. Distinguish a real transport error (no ``run_id``) from a
        # failed-but-readable run so a failed run still returns its full event
        # stream instead of masquerading as a transport failure.
        if d.get("error") and "run_id" not in d:
            return _wf_return("workflow_result", f"workflow_result: {d['error']}", outcome="error")
        # ``result`` / ``error`` / ``events`` are LLM-derived (agent outputs, log
        # lines) — recursively redact credentials + exfiltration URLs before
        # returning them through this MCP tool to the dashboard/chat surface.
        return _wf_return(
            "workflow_result",
            json.dumps(
                {
                    "run_id": d.get("run_id"),
                    "status": d.get("status"),
                    "result": _redact_obj(d.get("result")),
                    "error": _redact_obj(d.get("error")),
                    "events": _redact_obj(d.get("events", [])),
                },
                indent=2,
                default=str,
            ),
        )

    if name == "workflow_list":
        d = _get("/api/workflows/runs")
        if d.get("error"):
            return _wf_return("workflow_list", f"workflow_list: {d['error']}", outcome="error")
        runs = d.get("runs", [])
        if not runs:
            return _wf_return("workflow_list", "No workflow runs yet.")
        lines = [
            f"- `{r.get('run_id')}` {r.get('name') or '—'} → {r.get('status')} "
            f"({r.get('event_count', 0)} events)"
            for r in runs
        ]
        return _wf_return("workflow_list", "Workflow runs (newest first):\n" + "\n".join(lines))

    if name == "workflow_cancel":
        args = validate_tool_args(args, WORKFLOW_RUN_ID_SCHEMA)
        run_id = args.get("run_id", "")
        d = _post(f"/api/workflows/runs/{run_id}/cancel", {})
        if d.get("error"):
            return _wf_return("workflow_cancel", f"workflow_cancel: {d['error']}", outcome="error")
        return _wf_return(
            "workflow_cancel",
            f"Run `{run_id}`: {'cancelled' if d.get('cancelled') else 'not cancellable (already done?)'}",
        )

    if name == "workflow_rerun_subtree":
        args = validate_tool_args(args, WORKFLOW_RERUN_SCHEMA)
        run_id = args.get("run_id", "")
        from_index = args.get("from_index", 0)
        d = _post(
            f"/api/workflows/runs/{run_id}/rerun",
            {"from_index": from_index if isinstance(from_index, int) else 0},
        )
        if d.get("error"):
            return _wf_return(
                "workflow_rerun_subtree", f"workflow_rerun_subtree: {d['error']}", outcome="error"
            )
        return _wf_return(
            "workflow_rerun_subtree",
            f"Re-running `{run_id}` as `{d.get('run_id')}` "
            f"(replaying calls before index {d.get('replayed_before')}). "
            f"Monitor with workflow_status('{d.get('run_id')}').",
        )
    return _UNHANDLED
