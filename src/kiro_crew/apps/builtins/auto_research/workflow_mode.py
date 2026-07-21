"""Auto-research Dynamic Workflow execution mode.

Launches/stops a research campaign as a Dynamic Workflow run and adapts the
run's events/result into the same cycle-findings files + SSE the agent-mode UI
consumes. Depends on ``db``, ``constants``, ``files``, ``store``, ``redaction``,
and ``workflow_template``. ``_emit_sse`` is imported lazily from ``watchdog``
(which sits above this module in the DAG) to avoid an import cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.auto_research.constants import DEFAULT_EXECUTION_MODE, CampaignStatus
from kiro_crew.apps.builtins.auto_research.db import _get_db
from kiro_crew.apps.builtins.auto_research.files import (
    _campaign_dir,
    _list_cycle_files,
    _read_finding_file,
    _safe_campaign_dir,
)
from kiro_crew.apps.builtins.auto_research.redaction import _audit, _redact_finding
from kiro_crew.apps.builtins.auto_research.store import update_campaign_status
from kiro_crew.apps.builtins.auto_research.workflow_template import (
    RESEARCH_WORKFLOW_SOURCE,
    build_workflow_args,
)

logger = logging.getLogger(__name__)

try:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    _HAS_SECURITY = True
except ImportError:
    _HAS_SECURITY = False

_WORKFLOW_RUN_FILE = "workflow_run.json"


def _campaign_execution_mode(campaign_id: str) -> str:
    db = _get_db()
    row = db.execute("SELECT execution_mode FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    db.close()
    return (row["execution_mode"] if row else DEFAULT_EXECUTION_MODE) or DEFAULT_EXECUTION_MODE


def _write_workflow_run_id(campaign_id: str, run_id: str) -> None:
    d = _campaign_dir(campaign_id)
    # cycle_offset: number of cycle files already written by prior runs. Pause
    # cancels the DW run and resume launches a NEW run whose investigate events
    # restart at index 0; without this offset the adapter would re-index new
    # findings over the old ones (or drop them until the new run out-produced the
    # old). Persisting the offset makes the resumed run append correctly.
    cycle_offset = len(_list_cycle_files(campaign_id))
    d.joinpath(_WORKFLOW_RUN_FILE).write_text(
        json.dumps({"run_id": run_id, "ts": time.time(), "cycle_offset": cycle_offset})
    )


def _read_workflow_cycle_offset(campaign_id: str) -> int:
    d = _safe_campaign_dir(campaign_id)
    p = (d / _WORKFLOW_RUN_FILE) if d else None
    if not p or not p.exists():
        return 0
    try:
        return int(json.loads(p.read_text()).get("cycle_offset", 0) or 0)
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return 0


def _read_workflow_run_id(campaign_id: str) -> str | None:
    d = _safe_campaign_dir(campaign_id)
    p = (d / _WORKFLOW_RUN_FILE) if d else None
    if not p or not p.exists():
        return None
    try:
        return str(json.loads(p.read_text()).get("run_id") or "") or None
    except (json.JSONDecodeError, OSError):
        return None


async def _launch_workflow(request: web.Request, cid: str) -> None:
    """Start the research methodology as a Dynamic Workflow (workflow mode).

    Best-effort: if the gateway's WorkflowService is unavailable or the start
    fails, mark the campaign FAILED so it doesn't sit zombie in RUNNING. The
    watchdog adapter (`_poll_workflow_campaign`) translates the run's
    events/result into the same cycle/findings files + SSE the UI already
    consumes.
    """
    from kiro_crew.apps.builtins.auto_research.watchdog import _emit_sse

    state = request.app.get("state")
    svc = getattr(state, "workflow_service", None) if state is not None else None
    if svc is None:
        logger.warning(
            "auto_research: workflow_service unavailable; cannot launch workflow for %s", cid
        )
        update_campaign_status(
            cid,
            CampaignStatus.FAILED,
            error_message="Dynamic Workflow engine unavailable — cannot start workflow mode.",
        )
        _emit_sse({"type": "failed", "campaign_id": cid})
        return
    db = _get_db()
    row = db.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()
    db.close()
    if row is None:
        return
    args = build_workflow_args(dict(row))
    try:
        res = await svc.start(RESEARCH_WORKFLOW_SOURCE, name="research-" + cid, args=args)
    except Exception:
        logger.exception("auto_research: workflow start failed for %s", cid)
        update_campaign_status(
            cid,
            CampaignStatus.FAILED,
            error_message="Workflow start failed — see gateway logs for details.",
        )
        _emit_sse({"type": "failed", "campaign_id": cid})
        return
    run_id = (res or {}).get("run_id")
    if run_id:
        _write_workflow_run_id(cid, run_id)
        _audit("campaign_workflow_started", cid)
    else:
        logger.warning("auto_research: workflow start returned no run_id for %s: %s", cid, res)
        update_campaign_status(
            cid, CampaignStatus.FAILED, error_message="Workflow start returned no run ID."
        )
        _emit_sse({"type": "failed", "campaign_id": cid})


async def _stop_workflow(request: web.Request, cid: str) -> None:
    """Cancel a campaign's Dynamic Workflow run (workflow mode). Best-effort."""
    state = request.app.get("state")
    svc = getattr(state, "workflow_service", None) if state is not None else None
    run_id = _read_workflow_run_id(cid)
    if svc is not None and run_id:
        try:
            await svc.cancel(run_id)
        except Exception:
            logger.exception("auto_research: workflow cancel failed for %s", cid)


async def _poll_workflow_campaign(campaign_id: str, state: Any) -> None:
    """Adapter: translate a Dynamic Workflow run's events/result into the RL
    file + SSE model the existing UI consumes. Each `investigate:` agent that
    finishes becomes a cycle finding; on terminal the run's report is written to
    FINDINGS.md and the campaign is marked COMPLETE/FAILED. Best-effort — never
    raises into the watchdog.
    """
    from kiro_crew.apps.builtins.auto_research.watchdog import _emit_sse

    try:

        def _redact_llm(s: Any) -> str:
            text = str(s or "")
            if not _HAS_SECURITY:
                # Fail closed: strip the text entirely rather than persisting
                # potentially credential-laden LLM output to disk unredacted.
                return _redact_finding({"v": text})["v"] if text else ""
            cleaned, _ = redact_credentials(text)
            cleaned, _ = redact_exfiltration_urls(cleaned)
            return cleaned

        svc = getattr(state, "workflow_service", None) if state is not None else None
        run_id = _read_workflow_run_id(campaign_id)
        if svc is None or not run_id:
            return
        # svc.result() reads a file-backed snapshot (JSON on disk) — it does not
        # mutate the event-loop-affine registry. Offloading to a thread avoids
        # blocking the loop on file I/O while remaining safe to call concurrently
        # (reads only, no shared mutable state with the loop).
        snap = await asyncio.to_thread(svc.result, run_id)
        if not snap:
            # Bounded-poll fallback: if the run snapshot is gone (LRU eviction,
            # lost record) and the campaign has been RUNNING for > 1h with no
            # progress, mark it FAILED rather than let it sit zombie forever.
            d = _safe_campaign_dir(campaign_id)
            run_file = (d / _WORKFLOW_RUN_FILE) if d else None
            if run_file and run_file.exists():
                try:
                    run_meta = json.loads(run_file.read_text())
                    started_ts = float(run_meta.get("ts", 0))
                    if started_ts and (time.time() - started_ts) > 3600:
                        update_campaign_status(
                            campaign_id,
                            CampaignStatus.FAILED,
                            error_message="Workflow run snapshot lost after 1h — run likely evicted or crashed.",
                        )
                        _emit_sse({"type": "failed", "campaign_id": campaign_id})
                except (json.JSONDecodeError, OSError, ValueError, TypeError):
                    pass
            return
        d = _campaign_dir(campaign_id)
        events = snap.get("events") or []
        # Correlate agent_started (carries label/phase) -> agent_finished by id.
        started: dict = {}
        for e in events:
            if e.get("type") == "agent_started":
                data = e.get("data") or {}
                started[data.get("agent_id")] = data
        investigate: list = []
        for e in events:
            if e.get("type") == "agent_finished":
                data = e.get("data") or {}
                meta = started.get(data.get("agent_id"), {})
                if str(meta.get("label", "")).startswith("investigate") and data.get("ok"):
                    investigate.append((meta, data))
        cycle_offset = _read_workflow_cycle_offset(campaign_id)
        wrote = False
        # Each investigation maps to one cycle file (intentional: the UI shows
        # per-investigation progress, and total_cycles is a UI counter, not the
        # DW round count. The DW script's max_rounds caps exploration rounds;
        # per_round is already bounded by parallel_workers to limit fan-out).
        for i in range(len(investigate)):
            cycle_no = cycle_offset + i + 1
            fpath = d.joinpath("findings", "cycle_%03d.json" % cycle_no)
            if fpath.exists():
                continue  # already written by an earlier poll (idempotent)
            meta, fin = investigate[i]
            label = str(meta.get("label", ""))
            insight = label[len("investigate: ") :] if label.startswith("investigate: ") else label
            finding = {
                "cycle": cycle_no,
                "summary": _redact_llm(fin.get("result_summary", "")),
                "key_insight": _redact_llm(insight),
                "sources_checked": [],
                "sources_empty": [],
                "new_findings_count": 1,
                "evidence_strength": "moderate",
            }
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(json.dumps(finding, indent=2))
            wrote = True
        if wrote:
            count = len(_list_cycle_files(campaign_id))
            db = _get_db()
            db.execute("BEGIN")
            db.execute("UPDATE campaigns SET total_cycles=? WHERE id=?", (count, campaign_id))
            db.commit()
            db.close()
            _emit_sse(
                {
                    "type": "new_finding",
                    "campaign_id": campaign_id,
                    "finding": _read_finding_file(_list_cycle_files(campaign_id)[-1]),
                }
            )
        status = snap.get("status")
        if status == "finished":
            result = snap.get("result") if isinstance(snap.get("result"), dict) else {}
            report = str((result or {}).get("report") or "")
            if not report:
                fs = (result or {}).get("findings") or []
                report = "\n\n".join(str(x) for x in fs) if isinstance(fs, list) else ""
            d.joinpath("FINDINGS.md").write_text(_redact_llm(report) or "(no findings gathered)")
            update_campaign_status(campaign_id, CampaignStatus.COMPLETE)
            _emit_sse({"type": "complete", "campaign_id": campaign_id})
        elif status in ("failed", "cancelled"):
            update_campaign_status(
                campaign_id,
                CampaignStatus.FAILED,
                error_message=_redact_llm(
                    snap.get("error") or "workflow run ended without completing"
                ),
            )
            _emit_sse({"type": "failed", "campaign_id": campaign_id})
    except Exception:
        logger.exception("auto_research: workflow poll failed for %s", campaign_id)
