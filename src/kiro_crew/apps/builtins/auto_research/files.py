"""Auto-research file-based interface: path safety, campaign dirs, cycle
finding discovery, status/guidance/brief files, and stagnation detection.

Depends on ``constants`` (paths + id regex) and ``redaction`` (finding scrub).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.auto_research.constants import _CAMPAIGN_ID_RE, RESEARCH_DIR
from kiro_crew.apps.builtins.auto_research.redaction import _redact_finding

# --- Path safety ---


def _validate_campaign_id(campaign_id: str) -> bool:
    """Reject IDs that could cause path traversal."""
    return bool(_CAMPAIGN_ID_RE.match(campaign_id))


def _safe_campaign_dir(campaign_id: str) -> Path | None:
    """Return campaign dir only if it resolves within RESEARCH_DIR."""
    if not _validate_campaign_id(campaign_id):
        return None
    d = (RESEARCH_DIR / campaign_id).resolve()
    if not d.is_relative_to(RESEARCH_DIR.resolve()):
        return None
    return d


# --- Cycle finding discovery ---

# The worker is *prompted* to write findings as `cycle_NNN.json` (NNN zero-padded
# to 3 digits). But it's an LLM driving a file interface, so near-miss filenames
# happen — especially when a dropped mid-cycle write forces an improvised recovery
# turn (the agent re-derives the name from scratch and drifts on padding, the
# `_`/`-` separator, or case). A strict `glob("cycle_*.json")` silently ignores
# those files, so a campaign that IS producing findings reads as 0/stalled forever.
# Tolerate the realistic deviations and sort by the captured cycle number (a plain
# lexical sort also mis-orders unpadded names: `cycle_10` < `cycle_2`).
_CYCLE_FILE_RE = re.compile(r"^cycle[_-]?(\d+)\.json$", re.IGNORECASE)


def _cycle_index(path: Path) -> int:
    """Cycle number parsed from a finding filename, or -1 if it doesn't match."""
    m = _CYCLE_FILE_RE.match(path.name)
    return int(m.group(1)) if m else -1


def _cycle_finding_files(findings_dir: Path) -> list[Path]:
    """All cycle-finding files in a dir, ordered by cycle number (oldest first).

    Matches the canonical `cycle_NNN.json` plus tolerated near-misses
    (`cycle_7.json`, `cycle-007.json`, `Cycle_007.JSON`). One file per logical
    cycle: if multiple name variants parse to the same cycle number (e.g.
    `cycle_001.json` + `cycle-1.json`), only the lexically-first name is kept so
    duplicates can't inflate cycle counts or surface twice.

    SECURITY: this only widens which files are *discovered*; it does not bypass
    redaction. Every content-surfacing reader still routes each matched file
    through `_redact_finding()` (credentials + exfiltration URLs, fail-closed) —
    `get_findings()` for the dashboard and `_read_finding_file()` for the watchdog
    SSE feed — so a near-miss-named finding is scrubbed exactly like a canonical
    one before it reaches any external surface. (`check_stagnation()` reads only
    the integer `new_findings_count` and surfaces nothing.)
    """
    if not findings_dir.exists():
        return []
    # Glob ALL entries (not "*.json") so the case-insensitive regex governs the
    # match — Path.glob is case-sensitive, so "*.json" would miss "Cycle_002.JSON".
    matched = [(p, _cycle_index(p)) for p in findings_dir.glob("*") if p.is_file()]
    matched = [(p, i) for p, i in matched if i >= 0]
    by_cycle: dict[int, Path] = {}
    for p, i in sorted(matched, key=lambda t: (t[1], t[0].name)):
        by_cycle.setdefault(i, p)
    return [by_cycle[i] for i in sorted(by_cycle)]


# --- Stagnation ---


def check_stagnation(campaign_id: str) -> bool:
    d = _safe_campaign_dir(campaign_id)
    if not d:
        return False
    findings_dir = d / "findings"
    if not findings_dir.exists():
        return False
    files = _cycle_finding_files(findings_dir)
    if len(files) < 5:
        return False
    for f in files[-5:]:
        try:
            if json.loads(f.read_text()).get("new_findings_count", 0) > 0:
                return False
        except (json.JSONDecodeError, OSError):
            return False
    return True


# --- File interface ---


def _campaign_dir(campaign_id: str) -> Path:
    """Create and return campaign dir. Only call with validated IDs."""
    d = RESEARCH_DIR / campaign_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "findings").mkdir(exist_ok=True)
    return d


def _questions_path(campaign_id: str) -> Path | None:
    """Path to the agent's pending clarification question (if any)."""
    d = _safe_campaign_dir(campaign_id)
    return (d / "questions.json") if d else None


def _pending_question(campaign_id: str) -> str | None:
    """Read the agent's pending clarification question text, if present."""
    p = _questions_path(campaign_id)
    if not p or not p.exists():
        return None
    try:
        return str(json.loads(p.read_text()).get("question", "")) or None
    except (json.JSONDecodeError, OSError):
        return None


def write_status(campaign_id: str, status: str, **extra: Any) -> None:
    if not _validate_campaign_id(campaign_id):
        return
    d = _campaign_dir(campaign_id)
    (d / "status.json").write_text(
        json.dumps(
            {"status": status, "campaign_id": campaign_id, "ts": time.time(), **extra},
            indent=2,
        )
    )


def write_guidance(campaign_id: str, text: str) -> None:
    if not _validate_campaign_id(campaign_id):
        return
    d = _campaign_dir(campaign_id)
    (d / "guidance.txt").write_text(text)


def get_findings(campaign_id: str) -> list[dict]:
    d = _safe_campaign_dir(campaign_id)
    if not d:
        return []
    findings_dir = d / "findings"
    if not findings_dir.exists():
        return []
    results = []
    for f in _cycle_finding_files(findings_dir):
        try:
            results.append(_redact_finding(json.loads(f.read_text())))
        except (json.JSONDecodeError, OSError):
            continue
    return results


def _list_cycle_files(campaign_id: str) -> list[Path]:
    """Return cycle finding paths ordered by cycle number (newest last) WITHOUT
    reading them.

    Used by the watchdog for a cheap O(1)-read count on every poll; the actual
    file is only parsed (via _read_finding_file) when the count advances.
    """
    safe_dir = _safe_campaign_dir(campaign_id)
    findings_dir = (safe_dir / "findings") if safe_dir else None
    if not findings_dir or not findings_dir.exists():
        return []
    return _cycle_finding_files(findings_dir)


def _read_finding_file(path: Path) -> dict:
    """Read + redact a single cycle finding file; {} on parse/IO error."""
    try:
        return _redact_finding(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_brief(cid: str, row: Any) -> None:
    """Write the campaign brief — question, scope, and the authoritative
    sub-question checklist the agent reads each cycle.

    Local file in the campaign dir (the agent's file-based interface) — not an
    external surface, so the user's own question text is written as-is.
    """
    subs = json.loads(row["sub_questions"] or "[]")
    srcs = json.loads(row["sources"] or "[]")
    cols = row.keys()
    constraints = (
        json.loads(row["scope_constraints"] or "[]") if "scope_constraints" in cols else []
    )
    lines = ["# Research Brief", "", f"**Question:** {row['question']}", ""]
    if constraints:
        lines += ["## Scope & Constraints", ""]
        lines += [
            f"- {c.get('q', '')} → {c.get('a', '')}" for c in constraints if isinstance(c, dict)
        ]
        lines.append("")
    if subs:
        lines.append(
            "**Sub-questions (authoritative checklist — answer each; do NOT invent your own "
            "initial set). Items tagged _(emergent)_ were discovered mid-research; items "
            "tagged _(user guidance)_ are directives the user added — follow them, even if "
            "phrased as an instruction rather than a question:**"
        )
        for s in subs:
            text = s.get("text", "") if isinstance(s, dict) else str(s)
            origin = s.get("origin", "grill") if isinstance(s, dict) else "grill"
            tag = (
                " _(emergent)_"
                if origin == "emergent"
                else " _(user guidance)_" if origin == "manual" else ""
            )
            lines.append(f"- {text}{tag}")
    else:
        lines.append(
            "**Sub-questions:** (none provided — derive your own from the question and scope)"
        )
    lines += [
        "",
        f"**Sources allowed:** {', '.join(srcs) or 'any'}",
        f"**Max cycles:** {row['max_cycles']}",
    ]
    if not row["auto_approve"]:
        lines += [
            "",
            "**Questions allowed:** if the goal or scope is genuinely ambiguous in a "
            "way that would materially change your research direction, you MAY ask ONE "
            "high-leverage clarification question. Rules:\n"
            "- Only ask about DECISIONS the user must make — never ask about facts you "
            "can discover by exploring (filesystem, tools, code, web search).\n"
            "- Ask exactly ONE focused question per pause — multiple questions at once "
            "are bewildering and produce shallow answers.\n"
            "- First-principle: state what you know, the specific decision, and the "
            "options. Include your recommended answer.\n"
            "- Keep the bar high — proceed on a best-reasoned assumption for anything "
            "minor or self-resolvable.\n"
            "Write "
            '{"question": ..., "why": ..., "recommended": ...} to '
            "questions.json and end the turn — the campaign pauses for the user, who "
            "answers via Nudge.",
        ]
    if row["success_criteria"]:
        lines += [
            "",
            f"**Definition of Done:** {row['success_criteria']}",
            "Verify against this each cycle using your tools (run tests, review, eval); "
            "when met, set verification.passed=true in the finding.",
        ]
    lines += [
        "",
        "**Recursive exploration (emergent sub-questions):** As you research you will "
        "discover NEW high-value questions not in the initial list. Each cycle, in addition "
        "to your finding, you MAY propose follow-up sub-questions by writing "
        "`emergent_questions.json` in this dir as a JSON array: "
        '`[{"text": "...", "priority": 0.0-1.0}, ...]` where priority is how valuable '
        "/ relevant the lead is to the main question. The system ranks them, admits the top "
        "few per round (a budget), de-duplicates against existing questions, and appends the "
        "winners to the checklist above (tagged _(emergent)_) for you to investigate in "
        "later cycles — so you can follow leads BEYOND the initial questions. Do NOT "
        "re-propose questions already on the checklist, and stop proposing once the main "
        "question is sufficiently answered (your Definition of Done / verification).",
        "",
        "Each cycle, also read `guidance.txt` in this dir if present and follow any "
        "directive there (e.g. a FINALIZE MODE instruction to stop exploring and "
        "synthesize your final answer).",
        "",
        "Adapt direction each cycle from prior findings; pursue the highest-value open "
        "lead toward the question.",
    ]
    # Parallel worker instruction
    pw = int(row["parallel_workers"]) if "parallel_workers" in row.keys() else 1
    if pw > 1:
        lines += [
            "",
            f"**Parallel execution:** You have {pw} parallel worker slots. Each cycle, "
            "use `spawn_run` with a `tasks` array to investigate up to "
            f"{pw} open sub-questions simultaneously (one task per sub-question). "
            "Each task should be a self-contained research instruction for that sub-question. "
            "Wait for all completion events, then synthesize results into your cycle finding. "
            f"If fewer than {pw} sub-questions remain open, spawn only as many as needed.",
        ]
    _campaign_dir(cid).joinpath("brief.md").write_text("\n".join(lines))
