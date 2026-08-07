"""Tests for ``chat_persistence.snapshot_slot_transcript``.

The helper owns one invariant on behalf of every copy-style feature: give me the
full conversation, exactly once, as of one consistent instant. These tests pin
the four mutations that can land across its awaits, plus the two boundary states
that look identical but are not equally harmful.
"""

from __future__ import annotations

import asyncio
from test.chat_test_helpers import _make_state

import pytest

from kiro_crew.dashboard.chat import _save_slot_to_history
from kiro_crew.dashboard.chat_persistence import (
    SlotSnapshot,
    SnapshotUnstable,
    snapshot_slot_transcript,
)
from kiro_crew.dashboard.chat_utils import slot_history_key


def _seeded(tmp_path, count: int = 4, *, persist: bool = True):
    """A slot with ``count`` turns, optionally already written to disk."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("src")
    for i in range(count):
        slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
    slot.drain()
    if persist:
        _save_slot_to_history(state, slot)
        slot._dirty = False
    return state, slot


class TestSnapshotCompleteness:
    @pytest.mark.asyncio
    async def test_returns_every_turn_exactly_once(self, tmp_path):
        state, slot = _seeded(tmp_path, 4)
        snap = await snapshot_slot_transcript(state, slot)
        assert isinstance(snap, SlotSnapshot)
        assert [m["content"] for m in snap.messages] == ["m0", "m1", "m2", "m3"]

    @pytest.mark.asyncio
    async def test_splices_an_unpersisted_tail_without_duplicating_the_prefix(self, tmp_path):
        """The regression that made forks duplicate: the periodic flush advances
        ``_disk_window_len`` but never ``_resumed_count``, so splicing on the
        latter re-appends the whole window on top of itself."""
        state, slot = _seeded(tmp_path, 4)
        assert slot._resumed_count == 0, "a slot born this run reports 0"
        assert slot._disk_window_len == 4, "the save advances only this one"
        slot.append("user", "m4", "msg")
        slot.drain()

        snap = await snapshot_slot_transcript(state, slot)
        assert [m["content"] for m in snap.messages] == ["m0", "m1", "m2", "m3", "m4"]

    @pytest.mark.asyncio
    async def test_falls_back_to_memory_when_nothing_is_on_disk(self, tmp_path):
        state, slot = _seeded(tmp_path, 3, persist=False)
        snap = await snapshot_slot_transcript(state, slot)
        assert [m["content"] for m in snap.messages] == ["m0", "m1", "m2"]

    @pytest.mark.asyncio
    async def test_result_does_not_alias_slot_state(self, tmp_path):
        """Callers hand the result to threads and mutate their own copy."""
        state, slot = _seeded(tmp_path, 2, persist=False)
        snap = await snapshot_slot_transcript(state, slot)
        snap.messages[0]["content"] = "clobbered"
        assert slot.messages[0]["content"] == "m0"


class TestSnapshotConsistency:
    @pytest.mark.asyncio
    async def test_retries_when_a_turn_is_appended_during_the_read(self, tmp_path):
        state, slot = _seeded(tmp_path, 2)
        reads = {"n": 0}
        real = asyncio.to_thread

        async def _append_once(fn, *args):
            result = fn(*args)
            reads["n"] += 1
            if reads["n"] == 1:
                slot.append("user", "late", "msg")
                slot.drain()
            return result

        asyncio.to_thread = _append_once  # type: ignore[assignment]
        try:
            snap = await snapshot_slot_transcript(state, slot)
        finally:
            asyncio.to_thread = real  # type: ignore[assignment]

        assert reads["n"] >= 2, "the appended turn must force a retry"
        assert [m["content"] for m in snap.messages] == ["m0", "m1", "late"]

    @pytest.mark.asyncio
    async def test_retries_on_an_in_place_edit_that_moves_no_length(self, tmp_path):
        """A variant switch replaces an already-persisted turn: same count, same
        boundary. Only ``_dirty_gen`` reveals it."""
        state, slot = _seeded(tmp_path, 2)
        reads = {"n": 0}
        real = asyncio.to_thread

        async def _edit_once(fn, *args):
            result = fn(*args)
            reads["n"] += 1
            if reads["n"] == 1:
                before = (len(slot.messages), slot._disk_window_len)
                slot.messages[1]["content"] = "m1 regenerated"
                slot._dirty = True
                assert (len(slot.messages), slot._disk_window_len) == before
            return result

        asyncio.to_thread = _edit_once  # type: ignore[assignment]
        try:
            snap = await snapshot_slot_transcript(state, slot)
        finally:
            asyncio.to_thread = real  # type: ignore[assignment]

        assert reads["n"] >= 2, "the in-place edit must force a retry"
        assert [m["content"] for m in snap.messages] == ["m0", "m1 regenerated"]

    @pytest.mark.asyncio
    async def test_refuses_a_slot_that_never_settles(self, tmp_path):
        state, slot = _seeded(tmp_path, 2)
        real = asyncio.to_thread

        async def _always_change(fn, *args):
            result = fn(*args)
            slot.append("user", "again", "msg")
            slot.drain()
            return result

        asyncio.to_thread = _always_change  # type: ignore[assignment]
        try:
            with pytest.raises(SnapshotUnstable):
                await snapshot_slot_transcript(state, slot)
        finally:
            asyncio.to_thread = real  # type: ignore[assignment]

    @pytest.mark.asyncio
    async def test_refuses_while_a_rewind_is_pending(self, tmp_path):
        """Disk still holds the PRE-EDIT transcript, so a snapshot would carry
        turns the user explicitly rewound away. Nothing here can fix that."""
        state, slot = _seeded(tmp_path, 3)
        slot._pending_rewrite = True
        with pytest.raises(SnapshotUnstable):
            await snapshot_slot_transcript(state, slot)

    @pytest.mark.asyncio
    async def test_refuses_when_the_source_cannot_be_persisted(self, tmp_path, monkeypatch):
        """A swallowed failure would put us back to copying a stale transcript."""
        from kiro_crew.dashboard import chat_persistence as cp

        state, slot = _seeded(tmp_path, 2)
        slot.append("user", "unsaved", "msg")
        slot.drain()

        async def _boom(*_a, **_k):
            raise OSError("lock timeout")

        monkeypatch.setattr(cp, "save_slot_off_loop", _boom)
        with pytest.raises(SnapshotUnstable):
            await snapshot_slot_transcript(state, slot)
        assert slot._dirty is True, "the slot must stay dirty so the flush retries"


class TestBoundaryAhead:
    """``_disk_window_len`` exceeding the resident count has two causes that look
    identical. The flush is what separates them."""

    @pytest.mark.asyncio
    async def test_a_trimmed_slot_is_copyable(self, tmp_path):
        """Trimming leaves disk complete and memory holding a recent window. An
        empty tail splice is CORRECT, and refusing would make every long session
        uncopyable."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        for i in range(600):
            slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
        slot.drain()
        _save_slot_to_history(state, slot)
        slot.messages = slot.messages[-500:]
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._disk_older_count = 100
        slot._dirty = False

        snap = await snapshot_slot_transcript(state, slot)
        assert len(snap.messages) == 600
        assert snap.messages[0]["content"] == "m0"
        assert snap.messages[-1]["content"] == "m599"

    @pytest.mark.asyncio
    async def test_a_mid_stream_flush_is_resolved_by_the_flush(self, tmp_path):
        """``_flush_segment`` shrinks ``slot.messages`` past the boundary while
        appending the finalized turn. ``append`` marks the slot dirty, so this
        state is always dirty and flushing re-syncs it — the finalized turn must
        reach the snapshot rather than being silently dropped."""
        state, slot = _seeded(tmp_path, 2)
        # Boundary deliberately ahead of the resident window, slot dirty.
        slot.append("assistant", "finalized", "msg msg-a")
        slot.drain()
        slot._disk_window_len = len(slot.messages) + 5
        assert slot._dirty is True

        snap = await snapshot_slot_transcript(state, slot)
        assert [m["content"] for m in snap.messages][-1] == "finalized"


class TestSnapshotMetadata:
    @pytest.mark.asyncio
    async def test_untitled_slot_reports_an_empty_title(self, tmp_path):
        state, slot = _seeded(tmp_path, 1)
        slot._titled = False
        snap = await snapshot_slot_transcript(state, slot)
        assert snap.title == ""

    @pytest.mark.asyncio
    async def test_reports_the_history_key_used_for_the_read(self, tmp_path):
        state, slot = _seeded(tmp_path, 1)
        snap = await snapshot_slot_transcript(state, slot)
        assert snap.history_key == slot_history_key(slot)

    @pytest.mark.asyncio
    async def test_concurrent_snapshots_are_serialised(self, tmp_path):
        """Two callers would otherwise each flush and read, and one can observe
        the other's flush as instability."""
        state, slot = _seeded(tmp_path, 3)
        first, second = await asyncio.gather(
            snapshot_slot_transcript(state, slot),
            snapshot_slot_transcript(state, slot),
        )
        assert [m["content"] for m in first.messages] == ["m0", "m1", "m2"]
        assert first.messages == second.messages
