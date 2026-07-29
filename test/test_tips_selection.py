"""Unit tests for _select_tip appearance-decay + priority (Tips Kit Phase T1)."""

from __future__ import annotations

import random

from kiro_crew.tips import _select_tip


def _tip(tid: str) -> dict:
    return {"id": tid, "feature": "", "title": tid, "body": "", "why": "", "doc": "", "cta_prompt": ""}


def test_appearance_decay_zero_excludes_shown_tip():
    # appearance_decay=0.0 → a tip shown >=1 time gets weight 0 and is never
    # selected, so selection always lands on the never-shown tip.
    a, b = _tip("a"), _tip("b")
    rng = random.Random(1234)
    picks = {
        _select_tip(
            [a, b], [], recency_decay=1.0, rng=rng,
            shown_counts={"a": 3}, appearance_decay=0.0,
        )["id"]
        for _ in range(50)
    }
    assert picks == {"b"}


def test_appearance_decay_default_is_noop():
    # appearance_decay=1.0 (default) must not change legacy behaviour: both
    # tips remain selectable even when one has been shown many times.
    a, b = _tip("a"), _tip("b")
    rng = random.Random(7)
    picks = {
        _select_tip(
            [a, b], [], recency_decay=1.0, rng=rng,
            shown_counts={"a": 99}, appearance_decay=1.0,
        )["id"]
        for _ in range(200)
    }
    assert picks == {"a", "b"}


def test_appearance_decay_downweights_frequently_shown():
    # With partial decay, the frequently-shown tip is selected strictly less
    # often than the fresh one over many trials.
    a, b = _tip("a"), _tip("b")
    rng = random.Random(42)
    counts = {"a": 0, "b": 0}
    for _ in range(2000):
        counts[_select_tip(
            [a, b], [], recency_decay=1.0, rng=rng,
            shown_counts={"a": 5}, appearance_decay=0.5,
        )["id"]] += 1
    assert counts["b"] > counts["a"]


def test_priority_ids_take_top_rank():
    # recency_decay=0.0 → only rank-0 has nonzero weight. A priority id sorts to
    # rank 0 regardless of insertion order, so it is always chosen.
    normal, analyzer = _tip("normal"), _tip("analyzer-goal")
    rng = random.Random(9)
    picks = {
        _select_tip(
            [normal, analyzer], [], recency_decay=0.0, rng=rng,
            priority_ids={"analyzer-goal"},
        )["id"]
        for _ in range(50)
    }
    assert picks == {"analyzer-goal"}


def test_oversized_shown_count_does_not_overflow():
    """A pathological shown count must not raise OverflowError.

    ``appearance_decay ** shown`` raises "int too large to convert to float" for
    an int wider than a double, which would 500 every tips poll. _load_state
    bounds persisted values; this pins the use-site clamp as well.
    """
    a, b = _tip("a"), _tip("b")
    tip = _select_tip(
        [a, b], [], recency_decay=1.0, rng=random.Random(3),
        shown_counts={"a": 10**400}, appearance_decay=0.7,
    )
    assert tip is not None and tip["id"] in {"a", "b"}


def test_load_state_rejects_out_of_range_shown_counts(tmp_path, monkeypatch):
    """Persisted shown counts outside [0, _MAX_SHOWN_COUNT] are dropped.

    Negative values would act as a negative exponent and INFLATE a tip's weight
    instead of decaying it; oversized values overflow the float conversion.
    """
    import json as _json

    from kiro_crew import tips as tips_mod

    path = tmp_path / "tips_state.json"
    path.write_text(
        _json.dumps(
            {"shown": {"ok": 3, "negative": -5, "huge": 10**400}},
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tips_mod, "_state_path", lambda: path)
    st = tips_mod._load_state()
    assert st.shown == {"ok": 3}


def test_failed_refresh_attempt_is_rate_limited():
    """A hard generation failure must not re-launch on every poll.

    generate_tips normally swallows its own errors and returns the catalog
    fallback, so last_generated gets stamped. But when an exception ESCAPES,
    last_generated is never written and the staleness gate stays open -- without
    an attempt-level backoff every subsequent /api/tips/next would start another
    generation plus a 14-day history scan.
    """
    import asyncio as _asyncio
    import time as _time
    from unittest.mock import patch

    from kiro_crew.tips import (
        _REFRESH_RETRY_BACKOFF_SECS,
        TipsCache,
        maybe_refresh,
    )

    cache = TipsCache()
    cache.state.last_generated = 0.0  # stale -> staleness gate is open
    cache.last_attempt = _time.time()  # but an attempt just failed

    calls = []

    async def _fake_refresh(state, c):  # pragma: no cover - must not run
        calls.append(1)

    async def _run():
        with patch("kiro_crew.tips.refresh_tips", _fake_refresh):
            await maybe_refresh(object(), cache)

    _asyncio.run(_run())
    assert calls == [], "refresh must be suppressed inside the backoff window"

    # Once the backoff elapses, a retry is allowed again.
    cache.last_attempt = _time.time() - (_REFRESH_RETRY_BACKOFF_SECS + 1)

    class _FakeState:
        _background_tasks: set = set()

    async def _run2():
        with patch("kiro_crew.tips.refresh_tips", _fake_refresh):
            await maybe_refresh(_FakeState(), cache)
            await _asyncio.sleep(0)

    _asyncio.run(_run2())
    assert calls == [1], "refresh must resume after the backoff window"
