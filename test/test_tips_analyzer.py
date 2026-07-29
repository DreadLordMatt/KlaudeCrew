"""Unit tests for kiro_crew.tips_analyzer (Tips Kit Phase T1 analyzer)."""

from __future__ import annotations

from kiro_crew.tips_analyzer import (
    ANALYZER_ID_PREFIX,
    KIND_CRON_CREATE,
    KIND_FEATURE_TIP,
    analyze_activity,
    candidate_to_tip,
    dismissed_families_from_ids,
)


def _families(cands):
    return {c.family for c in cands}


def test_feature_detector_fires_on_manual_workflow():
    history = "Let's do this step by step. The next step is to build it."
    cands = analyze_activity(history)
    goal = [c for c in cands if c.family == "goal"]
    assert goal, "expected a goal feature_tip from step-by-step signals"
    assert goal[0].kind == KIND_FEATURE_TIP
    assert 0.0 < goal[0].strength <= 1.0


def test_feature_detector_suppressed_when_already_using():
    # Same signals, but the user already uses /goal → no goal candidate.
    history = "step by step, next step — I ran /goal earlier to handle it."
    assert "goal" not in _families(analyze_activity(history))


def test_feature_detector_suppressed_when_family_dismissed():
    history = "step by step. next step please."
    cands = analyze_activity(history, dismissed_families=frozenset({"goal"}))
    assert "goal" not in _families(cands)


def test_feature_detector_suppressed_via_existing_features():
    history = "step by step. next step please."
    cands = analyze_activity(history, existing_features=frozenset({"goal"}))
    assert "goal" not in _families(cands)


def test_compact_fires_on_single_strong_signal():
    # compact has min_hits=1.
    cands = analyze_activity("the context is getting full and slow")
    assert "compact" in _families(cands)


def test_below_threshold_does_not_fire():
    # A single 'next step' is one hit; goal needs two.
    assert "goal" not in _families(analyze_activity("what's the next step here"))


def test_cron_detector_fires_on_cadence_language():
    history = "- Every morning I pull the on-call dashboard and summarise it by hand"
    cands = analyze_activity(history, enable_cron=True)
    crons = [c for c in cands if c.kind == KIND_CRON_CREATE]
    assert crons, "expected a cron_create candidate from 'every morning' cadence"
    assert "on-call dashboard" in crons[0].body or "dashboard" in crons[0].cta_prompt


def test_cron_detector_suppressed_when_existing_cron_covers_it():
    history = "- Every morning I pull the on-call dashboard and summarise it by hand"
    existing = "morning-briefing: pull the oncall dashboard and summarise it"
    cands = analyze_activity(history, existing_crons=existing, enable_cron=True)
    assert not [c for c in cands if c.kind == KIND_CRON_CREATE]


def test_cron_detector_skips_lines_already_about_cron():
    history = "- I set up a cron job to run every morning already"
    assert not [c for c in analyze_activity(history, enable_cron=True) if c.kind == KIND_CRON_CREATE]


def test_cron_detector_rejects_product_noun_cadence():
    """'nightly build' etc. describe software, not a user habit.

    This was the entire false-positive set when the detector was first probed
    against real 14-day history.
    """
    for line in (
        "- I ship the nightly build and verify the daily release feed",
        "- We publish nightly artifacts and check the nightly channel",
        "- I tag the daily version stamp for every release",
    ):
        assert not [
            c for c in analyze_activity(line, enable_cron=True) if c.kind == KIND_CRON_CREATE
        ], f"should not fire on product-noun cadence: {line}"


def test_cron_detector_requires_habit_subject():
    # Third-person product narration with cadence but no user-habit framing.
    line = "- The scheduler stamps a new version every day for the release lane"
    assert not [c for c in analyze_activity(line, enable_cron=True) if c.kind == KIND_CRON_CREATE]


def test_cron_family_is_date_independent():
    """The same habit on two different days must yield the SAME family id.

    A date-derived slug would mint a fresh id daily and defeat dismiss/snooze.
    """
    a = analyze_activity("On 2026-07-16 at 22:40 PDT, I summarize my tickets every morning", enable_cron=True)
    b = analyze_activity("On 2026-07-20 at 08:05 PDT, I summarize my tickets every morning", enable_cron=True)
    fam_a = [c.family for c in a if c.kind == KIND_CRON_CREATE]
    fam_b = [c.family for c in b if c.kind == KIND_CRON_CREATE]
    assert fam_a and fam_a == fam_b
    assert not any(ch.isdigit() for ch in fam_a[0]), fam_a[0]


def test_cron_detector_is_off_by_default():
    """The cron detector must stay opt-in.

    It cannot reach acceptable precision against narrative daily-history prose
    (see _detect_recurring docstring), so shipping it on would surface false
    recommendations. This pins the gate so it can't be flipped by accident.
    """
    history = "- Every morning I pull the on-call dashboard and summarise it by hand"
    assert not [c for c in analyze_activity(history) if c.kind == KIND_CRON_CREATE]
    # ...but the detector itself still works when explicitly enabled.
    assert [c for c in analyze_activity(history, enable_cron=True) if c.kind == KIND_CRON_CREATE]


def test_candidate_to_tip_stable_id_and_seven_fields():
    cands = analyze_activity("the context is getting full")
    tip = candidate_to_tip(cands[0])
    assert tip["id"] == ANALYZER_ID_PREFIX + "compact"
    assert set(tip) == {"id", "feature", "title", "body", "why", "doc", "cta_prompt"}
    assert all(isinstance(v, str) for v in tip.values())


def test_max_candidates_cap():
    history = (
        "step by step, next step. one at a time, in parallel. "
        "context is getting full. keep checking, let me know when. "
        "every morning run the report. every weekday sync the tickets."
    )
    cands = analyze_activity(history, max_candidates=3)
    assert len(cands) <= 3


def test_candidates_sorted_by_strength_desc():
    history = (
        "step by step, next step, multi-step, another step 2 here. "
        "context is getting full."
    )
    cands = analyze_activity(history)
    strengths = [c.strength for c in cands]
    assert strengths == sorted(strengths, reverse=True)


def test_dismissed_families_from_ids():
    ids = ["analyzer-goal", "analyzer-cron-daily-report", "curated-x", "gen-y"]
    fams = dismissed_families_from_ids(ids)
    assert fams == frozenset({"goal", "cron-daily-report"})


def test_empty_history_yields_nothing():
    assert analyze_activity("") == []
    assert analyze_activity(None) == []  # type: ignore[arg-type]
