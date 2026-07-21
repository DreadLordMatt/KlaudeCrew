"""Auto-research campaign store: validation + CRUD over the campaigns table.

Depends on ``db`` (connection), ``constants`` (status/budget defaults),
``files`` (dirs + status files), and ``redaction`` (scrub + audit).
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from typing import Any

from kiro_crew.apps.builtins.auto_research.constants import (
    _MAX_PARALLEL_WORKERS,
    _TERMINAL_STATUSES,
    DEFAULT_DEPTH_DECAY,
    DEFAULT_EXECUTION_MODE,
    DEFAULT_IDLE_SECS,
    DEFAULT_MAX_SUBQUESTIONS_PER_ROUND,
    DEFAULT_RESERVE_FRACTION,
    MAX_CYCLES_HARD_CAP,
    VALID_EXECUTION_MODES,
    CampaignStatus,
)
from kiro_crew.apps.builtins.auto_research.db import _get_db
from kiro_crew.apps.builtins.auto_research.files import (
    _campaign_dir,
    _pending_question,
    _safe_campaign_dir,
    _validate_campaign_id,
    get_findings,
    write_status,
)
from kiro_crew.apps.builtins.auto_research.redaction import (
    _audit,
    _redact_campaign,
    _redact_finding,
)

# --- Validation ---


def validate_campaign(config: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if len(config.get("question", "")) < 20:
        errors.append("Question too vague — provide more context (min 20 characters)")
    if len(config.get("sub_questions", [])) < 2:
        warnings.append("Consider decomposing into sub-questions for better coverage")
    # RL v2: validate execution_mode against supported modes.
    if config.get("execution_mode", DEFAULT_EXECUTION_MODE) not in VALID_EXECUTION_MODES:
        errors.append("Execution mode must be 'agent' or 'workflow'")

    max_cycles = config.get("max_cycles", 30)
    if max_cycles > MAX_CYCLES_HARD_CAP:
        errors.append(f"Max cycles cannot exceed {MAX_CYCLES_HARD_CAP}")
    elif max_cycles > 50:
        low, high = max_cycles * 0.10, max_cycles * 0.30
        warnings.append(
            f"High cycle count ({max_cycles}). " f"Estimated cost: ~${low:.2f}–${high:.2f}"
        )

    db = _get_db()
    active = db.execute(
        "SELECT id, name FROM campaigns WHERE status IN (?, ?, ?, ?)",
        (
            CampaignStatus.RUNNING,
            CampaignStatus.PAUSED,
            CampaignStatus.STAGNANT,
            CampaignStatus.NEEDS_INPUT,
        ),
    ).fetchone()
    db.close()
    if active:
        clean_name = _redact_finding({"v": active["name"]})["v"]
        errors.append(f"Campaign '{clean_name}' is already active. Stop it first.")

    n = len(config.get("sub_questions", []))
    suggested_max_cycles = n + (n + 2) // 3 + 1 if n > 0 else 0
    return {
        "can_start": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "estimated_cycles": max_cycles,
        "estimated_duration_min": max_cycles * 2,
        "suggested_max_cycles": suggested_max_cycles,
    }


# --- CRUD ---


_FORK_NAME_PREFIX = "Forked: "


def _fork_name(source: str) -> str:
    """Build a forked campaign's display name with a clear 'Forked:' prefix.

    Mirrors create_campaign's 50-char name cap and avoids double-prefixing
    when the source already starts with the prefix (e.g. forking a fork).
    """
    base = (source or "").strip()
    if base.startswith(_FORK_NAME_PREFIX):
        base = base[len(_FORK_NAME_PREFIX) :].strip()
    return (_FORK_NAME_PREFIX + base[: 50 - len(_FORK_NAME_PREFIX)]).strip()


def create_campaign(config: dict) -> dict:
    campaign_id = uuid.uuid4().hex[:8]
    name = config.get("name") or config["question"][:50].strip()
    parent_id = config.get("parent_id") or None
    # RL v2: validate/clamp execution mode + recursive-exploration budget.
    exec_mode = config.get("execution_mode", DEFAULT_EXECUTION_MODE)
    if exec_mode not in VALID_EXECUTION_MODES:
        exec_mode = DEFAULT_EXECUTION_MODE
    max_subq = max(
        0, int(config.get("max_subquestions_per_round", DEFAULT_MAX_SUBQUESTIONS_PER_ROUND))
    )
    depth_decay = float(config.get("depth_decay", DEFAULT_DEPTH_DECAY))
    if not 0.0 <= depth_decay <= 1.0:
        depth_decay = DEFAULT_DEPTH_DECAY
    reserve_fraction = float(config.get("reserve_fraction", DEFAULT_RESERVE_FRACTION))
    if not 0.0 <= reserve_fraction < 1.0:
        reserve_fraction = DEFAULT_RESERVE_FRACTION
    db = _get_db()
    db.execute("BEGIN")
    db.execute(
        "INSERT INTO campaigns (id,name,question,sub_questions,sources,scope_constraints,"
        "max_cycles,idle_secs,success_criteria,auto_approve,parent_id,parallel_workers,"
        "execution_mode,max_subquestions_per_round,depth_decay,reserve_fraction,"
        "status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            campaign_id,
            name,
            config["question"],
            json.dumps(config.get("sub_questions", [])),
            json.dumps(config.get("sources", [])),
            json.dumps(config.get("scope_constraints", [])),
            config.get("max_cycles", 30),
            config.get("idle_secs", DEFAULT_IDLE_SECS),
            config.get("success_criteria") or None,
            int(bool(config.get("auto_approve", False))),
            parent_id,
            min(int(config.get("parallel_workers", 1)), _MAX_PARALLEL_WORKERS),
            exec_mode,
            max_subq,
            depth_decay,
            reserve_fraction,
            CampaignStatus.READY,
            time.time(),
        ),
    )
    db.commit()
    db.close()
    # Persist the grill tree if provided (full tree with clarifier answers,
    # pruned branches, origin tags — enables revisiting + challenge mode).
    grill_tree = config.get("grill_tree")
    if grill_tree and isinstance(grill_tree, list):
        d = _campaign_dir(campaign_id)
        d.mkdir(parents=True, exist_ok=True)
        d.joinpath("grill_tree.json").write_text(json.dumps(grill_tree, indent=2))
    write_status(campaign_id, CampaignStatus.READY)
    _audit("campaign_created", campaign_id)
    return {"id": campaign_id, "name": name, "status": CampaignStatus.READY}


def update_campaign_status(campaign_id: str, new_status: str, **kwargs: Any) -> dict:
    if not _validate_campaign_id(campaign_id):
        return {"error": "invalid campaign_id"}
    db = _get_db()
    row = db.execute("SELECT status FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if row is None:
        db.close()
        return {"error": "campaign not found"}
    current = row["status"]
    if current in _TERMINAL_STATUSES and new_status not in (current, CampaignStatus.RUNNING):
        db.close()
        return {"error": f"invalid transition: {current} -> {new_status}"}
    sets: list[str] = ["status = ?"]
    vals: list[Any] = [new_status]
    if new_status == CampaignStatus.RUNNING:
        sets.append("started_at = ?")
        vals.append(time.time())
        # Clear the prior run's completed_at so resumed COMPLETE/STOPPED campaigns
        # don't end up with completed_at < started_at (breaks duration math/UI).
        sets.append("completed_at = ?")
        vals.append(None)
        kwargs.setdefault("error_message", None)  # clear stale failure on (re)start
    if new_status in (CampaignStatus.COMPLETE, CampaignStatus.STOPPED, CampaignStatus.FAILED):
        sets.append("completed_at = ?")
        vals.append(time.time())
    if "error_message" in kwargs:
        sets.append("error_message = ?")
        vals.append(kwargs["error_message"])
    vals.append(campaign_id)
    db.execute("BEGIN")
    db.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    db.close()
    write_status(campaign_id, new_status, **kwargs)
    _audit(f"campaign_{new_status}", campaign_id)
    return {"id": campaign_id, "status": new_status}


def get_campaign(campaign_id: str) -> dict | None:
    if not _validate_campaign_id(campaign_id):
        return None
    db = _get_db()
    row = db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    db.close()
    if not row:
        return None
    return _redact_campaign(
        {
            **dict(row),
            "findings": get_findings(campaign_id),
            "pending_question": _pending_question(campaign_id),
        }
    )


def list_campaigns() -> list[dict]:
    db = _get_db()
    rows = db.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
    db.close()
    return [_redact_campaign(dict(r)) for r in rows]


def delete_campaign(campaign_id: str) -> dict:
    """Delete a campaign's DB row and its research dir (findings + report)."""
    if not _validate_campaign_id(campaign_id):
        return {"error": "invalid campaign_id"}
    db = _get_db()
    db.execute("BEGIN")
    rows = db.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,)).rowcount
    db.commit()
    db.close()
    if rows == 0:
        return {"error": "campaign not found"}
    d = _safe_campaign_dir(campaign_id)
    if d and d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return {"id": campaign_id, "deleted": True}
