"""Auto-research watchdog: SSE fan-out + the polling loop that drives campaign
progression, plus the autonudge-backed worker-loop lifecycle.

Single owner of the SSE queue state (``_sse_queues`` / ``_emit_sse``): store and
workflow_mode emit through it. Sits above store/exploration/workflow_mode in the
DAG; ``workflow_mode`` imports ``_emit_sse`` from here lazily to avoid a cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import web

from kiro_crew.apps.builtins.auto_research.constants import (
    _RESEARCH_AGENT,
    _RESEARCH_NUDGE,
    _TRUST_TTL_SECS,
    DEFAULT_IDLE_SECS,
    POLL_INTERVAL,
    CampaignStatus,
    _unresponsive_deadline,
)
from kiro_crew.apps.builtins.auto_research.db import _get_db
from kiro_crew.apps.builtins.auto_research.exploration import _advance_exploration
from kiro_crew.apps.builtins.auto_research.files import (
    _campaign_dir,
    _list_cycle_files,
    _questions_path,
    _read_finding_file,
    _write_brief,
    check_stagnation,
)
from kiro_crew.apps.builtins.auto_research.redaction import _audit
from kiro_crew.apps.builtins.auto_research.store import update_campaign_status
from kiro_crew.apps.builtins.auto_research.workflow_mode import _poll_workflow_campaign
from kiro_crew.autonudge import get_instance as _autonudge_instance

logger = logging.getLogger(__name__)

# --- Watchdog ---

_SSE_QUEUE_MAXSIZE = 256
_sse_queues: list[asyncio.Queue] = []


def _emit_sse(event: dict) -> None:
    for q in _sse_queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # Drop events for slow consumers


def _should_pause_for_question(cid: str, auto_approve: bool) -> bool:
    """Decide what to do with a pending questions.json.

    Returns True only when the campaign should pause to NEEDS_INPUT (attended
    mode with a question waiting). Unattended mode NEVER pauses: any stray
    question (the agent was not given a questions directive) is discarded so
    "unattended" is a code-enforced guarantee, not reliant on the LLM obeying
    a prompt. Returns False when there's no question or it was discarded.
    """
    qp = _questions_path(cid)
    if not (qp and qp.exists()):
        return False
    if auto_approve:
        qp.unlink(missing_ok=True)
        _audit("campaign_unattended_question_discarded", cid)
        return False
    return True


async def _watchdog_loop(app: web.Application | None = None) -> None:
    state = app.get("state") if app is not None else None
    last_counts: dict[str, int] = {}
    last_ts: dict[str, float] = {}
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)
            db = _get_db()
            active = db.execute(
                "SELECT id, idle_secs, max_cycles, started_at, auto_approve, execution_mode "
                "FROM campaigns WHERE status = ?",
                (CampaignStatus.RUNNING,),
            ).fetchall()
            db.close()
            for row in active:
                cid = row["id"]
                # Workflow-mode campaigns are driven by a Dynamic Workflow run;
                # the adapter translates its events/result into the RL file+SSE
                # model. The agent-mode body below does not apply to them.
                if row["execution_mode"] == "workflow":
                    await _poll_workflow_campaign(cid, state)
                    continue
                slot = state._slots.get(f"research-{cid}") if state is not None else None
                # 24h auto-approve cap: expire trust and require re-authorization.
                started = row["started_at"]
                if started and time.time() - started > _TRUST_TTL_SECS:
                    if slot is not None:
                        slot._trust = False
                    qpath = _questions_path(cid)
                    if qpath:
                        qpath.write_text(
                            json.dumps(
                                {
                                    "question": "Auto-approval expired after 24h. Resume to "
                                    "re-authorize and continue."
                                }
                            )
                        )
                    update_campaign_status(cid, CampaignStatus.NEEDS_INPUT)
                    _audit("campaign_trust_expired", cid)
                    _emit_sse({"type": "needs_input", "campaign_id": cid})
                    continue
                # Re-establish worker trust each cycle (restart-durable; bounded above).
                if slot is not None and not slot._trust:
                    slot._trust = True
                    _audit("campaign_trust_reestablished", cid)
                # Attended: pause for the user. Unattended: discard the stray
                # question + keep running (code-enforced; see helper).
                if _should_pause_for_question(cid, bool(row["auto_approve"])):
                    update_campaign_status(cid, CampaignStatus.NEEDS_INPUT)
                    _emit_sse({"type": "needs_input", "campaign_id": cid})
                    continue
                # Lightweight: count files without reading them all. Only parse
                # the latest finding when count advances (avoids re-reading 50+
                # JSON files every 5s).
                cycle_files = _list_cycle_files(cid)
                count = len(cycle_files)
                if cid not in last_counts or last_ts.get(cid, 0.0) < (started or 0):
                    last_counts[cid] = count
                    last_ts[cid] = time.time()
                    continue
                prev = last_counts[cid]
                if count > prev:
                    last_counts[cid] = count
                    last_ts[cid] = time.time()
                    # Read only the newest finding (last file).
                    latest = _read_finding_file(cycle_files[-1])
                    _emit_sse({"type": "new_finding", "campaign_id": cid, "finding": latest})
                    db2 = _get_db()
                    db2.execute("BEGIN")
                    db2.execute(
                        "UPDATE campaigns SET total_cycles=? WHERE id=?",
                        (count, cid),
                    )
                    db2.commit()
                    db2.close()
                    # RL v2: advance recursive exploration (ingest agent-proposed
                    # emergent sub-questions + activate queued ones). Agent-mode
                    # only and fully guarded — must never break the watchdog.
                    _advance_exploration(cid)
                    verified = latest.get("verification")
                    if isinstance(verified, dict) and verified.get("passed") is True:
                        update_campaign_status(cid, CampaignStatus.COMPLETE)
                        _emit_sse({"type": "complete", "campaign_id": cid})
                    elif count >= row["max_cycles"]:
                        update_campaign_status(cid, CampaignStatus.COMPLETE)
                        _emit_sse({"type": "complete", "campaign_id": cid})
                    elif check_stagnation(cid):
                        update_campaign_status(cid, CampaignStatus.STAGNANT)
                        _emit_sse({"type": "stagnant", "campaign_id": cid})
                elif cid in last_ts:
                    if slot is not None and slot.running:
                        # Agent is actively working this cycle (deep research can
                        # take minutes) — alive, not unresponsive. Refresh liveness.
                        last_ts[cid] = time.time()
                    elif time.time() - last_ts[cid] > _unresponsive_deadline(row["idle_secs"]):
                        update_campaign_status(
                            cid,
                            CampaignStatus.FAILED,
                            error_message="No activity — research stalled. Resume to continue.",
                        )
                        await _stop_loop(cid, remove=True)  # tear down so Resume re-arms cleanly
                        last_counts.pop(cid, None)
                        last_ts.pop(cid, None)
                        _emit_sse({"type": "failed", "campaign_id": cid})
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("auto_research watchdog error")
            await asyncio.sleep(POLL_INTERVAL)


# --- Campaign worker loop (autonudge-backed) ---


async def _launch_loop(request: web.Request, cid: str) -> None:
    """Arm an autonudge loop that drives the research cycles for this campaign.

    Best-effort: if autonudge or dashboard state is unavailable, the status
    change still stands but no worker is launched (logged for visibility).
    """
    state = request.app.get("state")
    svc = _autonudge_instance()
    if state is None or svc is None:
        logger.warning(
            "auto_research: cannot launch loop for %s (autonudge/state unavailable)", cid
        )
        return
    db = _get_db()
    row = db.execute(
        "SELECT question, sub_questions, sources, scope_constraints, max_cycles, idle_secs, "
        "success_criteria, auto_approve, parallel_workers FROM campaigns WHERE id = ?",
        (cid,),
    ).fetchone()
    db.close()
    if row is None:
        return
    _write_brief(cid, row)
    slot = state.get_or_create_slot(
        name=f"research-{cid}", agent=_RESEARCH_AGENT, app="auto-research"
    )
    # The worker runs autonomously — auto-approve its tools so the loop never
    # stalls on per-tool approval prompts (brakes: max_cycles, Stop, sandbox,
    # deny-list). The slot is app-owned, so it's hidden from the chat sidebar.
    # NOTE: slot._trust is the PER-SLOT trust flag (same mechanism as the
    # interactive "trust this session" in chat_handlers.py and gateway scoped
    # trust) — NOT the global _yolo_mode that safety_override() governs, which is
    # a single process-wide toggle and cannot express per-campaign grants. The
    # grant is instead bounded per campaign: the watchdog expires it after
    # _TRUST_TTL_SECS and forces NEEDS_INPUT re-authorization (see _watchdog_loop).
    slot._trust = True
    _audit("campaign_auto_approve", cid)
    state.push_slots_update()  # surface the app-owned worker slot so the UI filters it
    await svc.add(
        slot_key=slot.key,
        message=_RESEARCH_NUDGE.format(cid=cid, dir=_campaign_dir(cid)),
        idle_secs=int(row["idle_secs"] or DEFAULT_IDLE_SECS),
        max_cycles=int(row["max_cycles"] or 0),
        stop_sentinel_path=str(_campaign_dir(cid) / "STOP"),
    )


async def _stop_loop(cid: str, *, remove: bool) -> None:
    """Pause (remove=False) or tear down (remove=True) a campaign's autonudge loop."""
    svc = _autonudge_instance()
    if svc is None:
        return
    loop = svc.get_by_slot(f"research-{cid}")
    if not loop:
        return
    if remove:
        await svc.remove(loop.id)
    else:
        await svc.update(loop.id, active=False)
