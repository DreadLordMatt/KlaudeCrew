"""Tests for the split inline / deferred startup session restore.

Rehydrating a session reads its message window off disk, redacts it and replays
it into a slot. Doing that for every open tab and every in-window session
*synchronously* on the event loop during ``on_startup`` starves the loop-stall
watchdog's heartbeat, and ``LoopStallWatchdog`` ``_exit(1)``s the gateway after
25s of silence — so a home with enough history killed its own gateway on every
boot, mid-restore.

``restore_open_slots`` / ``restore_recent_sessions`` therefore take a ``limit``
and a ``deferred`` sink: the first ``INLINE_RESTORE_LIMIT`` sessions come back
inline (so the sidebar is populated before the first client connects) and the
rest are replayed by ``resume_deferred_restores``, which yields to the loop
between sessions.

These tests cover:

* The inline budget is honored and the overflow lands in ``deferred``.
* Nothing is lost: the deferred pass restores exactly the overflow.
* The deferred pass yields between sessions (the whole point).
* Already-present and closed sessions are skipped by the deferred pass.
* A failing rehydrate does not abort the rest, and leaks no partial slot.
* ``reseed_slot_counter`` counts deferred names, closing the collision window.
* The open-tab snapshot keeps pinning the not-yet-restored keys, so a flush or
  a shutdown save landing mid-pass cannot truncate ``open_slots.json``.
* The bulk message-window reads happen off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_persistence import (
    INLINE_RESTORE_LIMIT,
    restore_open_slots,
    restore_recent_sessions,
    resume_deferred_restores,
    save_all_slots_to_history,
)
from kiro_crew.dashboard.chat_utils import _history_key_for


def _seed(state, slot_key: str, *, closed: bool = False) -> None:
    """Write minimal session metadata + one message so a rehydrate succeeds."""
    history_key = _history_key_for(slot_key)
    log = state.conversation_log
    assert log is not None
    log.append(history_key, "user", f"hello {slot_key}")
    if closed:
        log.update_metadata(history_key, {"closed": True})


def _write_open_slots(tmp_path, keys: list[str]) -> None:
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": keys, "ts": 0.0}), encoding="utf-8"
    )


def test_open_slots_restores_inline_budget_and_defers_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 26)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    deferred: list[str] = []
    restored = restore_open_slots(state, limit=10, deferred=deferred)

    assert restored == 10
    assert len(deferred) == 15
    # Inline set and deferred set partition the input, in order, with no overlap.
    assert deferred == keys[10:]
    assert set(state._slots) == set(keys[:10])


def test_deferred_pass_restores_exactly_the_overflow(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 16)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    deferred: list[str] = []
    restore_open_slots(state, limit=5, deferred=deferred)
    restored = asyncio.run(resume_deferred_restores(state, deferred, []))

    assert restored == 10
    # Nothing dropped: every key from open_slots.json is back as a slot.
    assert set(state._slots) == set(keys)


def test_deferred_pass_yields_between_sessions(tmp_path, monkeypatch):
    """The loop must get control between sessions — that is what keeps the
    watchdog heartbeat alive during a long restore."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 6)]
    for k in keys:
        _seed(state, k)

    ticks = 0

    async def _observer() -> None:
        # Runs concurrently with the restore; each iteration only advances if
        # the restore hands the loop back.
        nonlocal ticks
        for _ in range(200):
            await asyncio.sleep(0)
            ticks += 1

    async def _run() -> int:
        obs = asyncio.create_task(_observer())
        n = await resume_deferred_restores(state, keys, [])
        obs.cancel()
        return n

    restored = asyncio.run(_run())

    assert restored == 5
    # One yield per session, minus the final iteration's (the observer is
    # cancelled as soon as the restore returns). A non-yielding implementation
    # never lets the observer run at all, so this is the discriminating check.
    assert ticks >= len(keys) - 1


def test_deferred_pass_skips_present_and_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-1-t")
    _seed(state, "chat-2-t", closed=True)
    _seed(state, "chat-3-t")
    # chat-1 is already live (a client opened it while the pass was queued).
    state.get_or_create_slot("chat-1-t")

    restored = asyncio.run(
        resume_deferred_restores(state, ["chat-1-t", "chat-2-t", "chat-3-t"], [])
    )

    assert restored == 1  # only chat-3
    assert "chat-3-t" in state._slots
    assert "chat-2-t" not in state._slots  # closed stays closed


def test_deferred_pass_survives_a_failing_session(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    for k in ("chat-1-t", "chat-2-t", "chat-3-t"):
        _seed(state, k)

    real = state.conversation_log.read_messages_chained

    def _boom(key, *a, **kw):
        if "chat-2-t" in key:
            raise OSError("disk gremlin")
        return real(key, *a, **kw)

    monkeypatch.setattr(state.conversation_log, "read_messages_chained", _boom)

    restored = asyncio.run(
        resume_deferred_restores(state, ["chat-1-t", "chat-2-t", "chat-3-t"], [])
    )

    assert restored == 2
    # The failure must not leave a half-built slot behind: restore paths call
    # get_or_create_slot before the fallible read.
    assert "chat-2-t" not in state._slots
    assert {"chat-1-t", "chat-3-t"} <= set(state._slots)


def test_recent_sessions_defers_past_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 13)]
    for k in keys:
        _seed(state, k)

    deferred: list[tuple[str, dict]] = []
    restored = restore_recent_sessions(state, 60, limit=4, deferred=deferred)

    assert restored == 4
    assert len(deferred) == len(keys) - 4
    assert all(isinstance(name, str) and isinstance(sess, dict) for name, sess in deferred)

    finished = asyncio.run(resume_deferred_restores(state, [], deferred))
    assert finished == len(keys) - 4
    assert set(state._slots) == set(keys)


def test_reseed_counts_deferred_names(tmp_path, monkeypatch):
    """A new chat minted while the deferred pass is still running must not take
    an index a returning tab is about to claim."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.get_or_create_slot("chat-3-t")  # restored inline

    state.reseed_slot_counter(["chat-41-t"])  # still deferred

    assert state._slot_counter >= 41


def test_limit_without_deferred_sink_restores_everything(tmp_path, monkeypatch):
    """A bare ``limit`` with nowhere to defer to must not silently drop tabs."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 6)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    restored = restore_open_slots(state, limit=2)

    assert restored == len(keys)
    assert set(state._slots) == set(keys)


def test_inline_limit_default_is_ten():
    """The startup wiring in server.py passes this; 10 is the documented
    trade-off between a populated sidebar and the 25s watchdog budget."""
    assert INLINE_RESTORE_LIMIT == 10


@pytest.mark.parametrize("keys,sessions", [([], []), ([], None)])
def test_resume_is_a_noop_with_nothing_deferred(tmp_path, monkeypatch, keys, sessions):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    assert asyncio.run(resume_deferred_restores(state, keys, sessions or [])) == 0


def _snapshot_keys(tmp_path) -> list[str]:
    return json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))["keys"]


def test_snapshot_taken_mid_pass_keeps_deferred_tabs(tmp_path, monkeypatch):
    """The regression this guards: a 5s flush (or a restart save) landing while
    the background pass is still running used to rewrite open_slots.json from a
    partial ``_slots``, so the un-restored tabs vanished for good on the next
    boot."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 9)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    deferred: list[str] = []
    restore_open_slots(state, limit=3, deferred=deferred)

    # Snapshot now — mid-pass, only 3 slots are live.
    state._persist_open_slots()
    assert len(state._slots) == 3
    assert set(_snapshot_keys(tmp_path)) == set(keys), "deferred tabs were truncated out"

    # After the pass completes the pin is released and the live slots alone
    # carry the full set.
    asyncio.run(resume_deferred_restores(state, deferred, []))
    assert state._pending_restore_keys == ()
    state._persist_open_slots()
    assert set(_snapshot_keys(tmp_path)) == set(keys)


def test_pins_survive_a_cancelled_pass(tmp_path, monkeypatch):
    """Cancellation is the dangerous case (gateway shutdown mid-pass), so the
    pin must NOT be released on the way out."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 7)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    deferred: list[str] = []
    restore_open_slots(state, limit=2, deferred=deferred)

    async def _run() -> None:
        task = asyncio.create_task(resume_deferred_restores(state, deferred, []))
        await asyncio.sleep(0)  # let it start, then kill it partway
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    # Whatever did not come back is still pinned, so a shutdown snapshot taken
    # right now still records every open tab.
    state._persist_open_slots()
    assert set(_snapshot_keys(tmp_path)) == set(keys)


def test_incognito_session_is_deferred_but_not_snapshot_pinned(tmp_path, monkeypatch):
    """open_slots.json only tracks persistent tabs; deferring must not smuggle
    an incognito session into it."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    for k in ("chat-1-t", "chat-2-t"):
        _seed(state, k)
    state.conversation_log.update_metadata(
        _history_key_for("chat-2-t"), {"memory_mode": "incognito"}
    )

    deferred: list[tuple[str, dict]] = []
    restore_recent_sessions(state, 60, limit=0, deferred=deferred)

    # Both are deferred (nothing restores inline at limit=0) but only the
    # persistent one is pinned into the snapshot.
    assert {name for name, _ in deferred} == {"chat-1-t", "chat-2-t"}
    assert state._pending_restore_keys == ("chat-1-t",)


def test_message_window_is_read_off_the_event_loop(tmp_path, monkeypatch):
    """``asyncio.to_thread`` is the point: the loop must not be the thread doing
    the multi-hundred-message reads."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-t", "chat-2-t"]
    for k in keys:
        _seed(state, k)

    loop_thread = threading.current_thread()
    read_threads: list[threading.Thread] = []
    real = state.conversation_log.read_messages_chained

    def _spy(key, *a, **kw):
        read_threads.append(threading.current_thread())
        return real(key, *a, **kw)

    monkeypatch.setattr(state.conversation_log, "read_messages_chained", _spy)

    assert asyncio.run(resume_deferred_restores(state, keys, [])) == 2
    assert read_threads, "no message window was read"
    assert all(t is not loop_thread for t in read_threads)


def test_replay_is_hidden_from_the_persistence_sweeps(tmp_path, monkeypatch):
    """The slot is registered in _slots before its window is replayed into it,
    and the deferred pass yields, so the 5s flush thread can see it mid-replay:
    _dirty set, but _disk_older_count / _disk_window_len not yet. Saving then
    writes a partial window the next flush duplicates."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-1-t")

    real_create = state.get_or_create_slot
    seen_during_replay: list[bool] = []

    def _spy(name=None, *a, **kw):
        # get_or_create_slot is the first thing the replay does, so this
        # observes the guard exactly when the flush thread could.
        seen_during_replay.append(name in state._restoring_keys)
        return real_create(name, *a, **kw)

    monkeypatch.setattr(state, "get_or_create_slot", _spy)
    assert asyncio.run(resume_deferred_restores(state, ["chat-1-t"], [])) == 1

    assert seen_during_replay == [True], "replay ran unguarded"
    assert state._restoring_keys == set(), "guard leaked past the replay"


def test_flush_skips_a_restoring_slot_without_clearing_dirty(tmp_path, monkeypatch):
    """The skip has to happen before _flush_dirty_slots clears _dirty, or the
    flag is dropped without a write and the messages never get saved."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    slot = state.get_or_create_slot("chat-1-t")
    slot.append("user", "half-replayed")
    slot._dirty = True

    state._restoring_keys.add("chat-1-t")
    state._flush_dirty_slots()
    assert slot._dirty is True, "dirty cleared without a save"

    # Same slot, guard released: now it does flush.
    state._restoring_keys.discard("chat-1-t")
    state._flush_dirty_slots()
    assert slot._dirty is False


def test_shutdown_save_skips_a_restoring_slot(tmp_path, monkeypatch):
    """save_all_slots_to_history passes force=True, so it needs the choke-point
    guard in _save_slot_to_history rather than the flush-loop skip."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    slot = state.get_or_create_slot("chat-1-t")
    slot.append("user", "half-replayed")

    calls: list[str] = []
    real_locked = state.conversation_log._locked

    def _spy_locked(key, *a, **kw):
        # _locked wraps the whole read-modify-atomic_write, so entering it is
        # the observable "this slot is being persisted".
        calls.append(key)
        return real_locked(key, *a, **kw)

    monkeypatch.setattr(state.conversation_log, "_locked", _spy_locked)

    state._restoring_keys.add("chat-1-t")
    save_all_slots_to_history(state)
    assert calls == [], "a mid-replay slot was persisted"

    # Guard released: the same slot does get written, so the assertion above is
    # about the guard and not about save_all_slots_to_history being inert here.
    state._restoring_keys.discard("chat-1-t")
    save_all_slots_to_history(state)
    assert calls, "shutdown save never persisted the slot"

    # And the snapshot leaves the restoring key to the pin, not to _slots.
    state._restoring_keys.add("chat-1-t")
    state._pending_restore_keys = ("chat-1-t",)
    state._persist_open_slots()
    assert _snapshot_keys(tmp_path) == ["chat-1-t"]


def test_pin_is_read_before_slots_so_the_last_restore_cannot_slip_through(tmp_path, monkeypatch):
    """The flush reads _slots and the pins at two different instants. If _slots
    went first, the final deferred restore could land in between — absent from
    the _slots snapshot, pin already cleared — and the tab would drop out."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-t", "chat-2-t"]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    deferred: list[str] = []
    restore_open_slots(state, limit=1, deferred=deferred)
    assert deferred == ["chat-2-t"]

    # Simulate the interleaving: the restore completes (slot in, pin cleared)
    # between the snapshot's two reads. A dict subclass fires it on the _slots
    # enumeration, which is the second of the two reads.
    fired = False

    class _InterleavingSlots(dict):
        def items(self):  # type: ignore[override]
            nonlocal fired
            if not fired:
                fired = True
                state.get_or_create_slot("chat-2-t")
                state._pending_restore_keys = ()
            return super().items()

    state._slots = _InterleavingSlots(state._slots)
    state._persist_open_slots()

    assert fired
    assert set(_snapshot_keys(tmp_path)) == set(keys)


def test_failed_restore_stays_pinned_in_the_snapshot(tmp_path, monkeypatch):
    """A read that blows up may be transient. Unpinning the key would let the
    next flush write an open_slots.json without it and lose the tab for good,
    so a failure keeps its pin while everything settled releases its own."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 7)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    deferred: list[str] = []
    restore_open_slots(state, limit=2, deferred=deferred)
    assert "chat-4-t" in deferred

    real = state.conversation_log.read_messages_chained

    def _boom(key, *a, **kw):
        if "chat-4-t" in key:
            raise OSError("transient disk gremlin")
        return real(key, *a, **kw)

    monkeypatch.setattr(state.conversation_log, "read_messages_chained", _boom)
    asyncio.run(resume_deferred_restores(state, deferred, []))

    assert "chat-4-t" not in state._slots
    assert state._pending_restore_keys == ("chat-4-t",)
    # And therefore it survives the very next snapshot.
    state._persist_open_slots()
    assert set(_snapshot_keys(tmp_path)) == set(keys)


def test_session_deleted_during_the_read_is_not_recreated(tmp_path, monkeypatch):
    """Deleting a session mid-pass must win. Applying stale metadata would let
    the tab_id backfill write the JSONL back."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-1-t")
    log = state.conversation_log
    sessions = [
        (s["key"].removeprefix("dashboard:").removeprefix("dashboard_"), s)
        for s in log.list_sessions()
    ]
    assert sessions

    real_read = log.read_messages_chained
    gone = False

    def _delete_midway(key, *a, **kw):
        # Stand in for the delete-session handler landing during the off-loop read.
        nonlocal gone
        gone = True
        return real_read(key, *a, **kw)

    real_meta = log.get_metadata
    monkeypatch.setattr(log, "read_messages_chained", _delete_midway)
    monkeypatch.setattr(log, "get_metadata", lambda k, *a, **kw: {} if gone else real_meta(k))

    assert asyncio.run(resume_deferred_restores(state, [], sessions)) == 0
    assert state._slots == {}


@pytest.mark.parametrize("path", ["open_slots", "recent_sessions"])
def test_slot_resumed_during_the_read_is_not_replayed(tmp_path, monkeypatch, path):
    """Moving the reads off the loop puts an ``await`` between the
    "already live?" guard and the apply. A client resuming the tab in that
    window must not get its history appended a second time."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-1-t")
    _seed(state, "chat-1-t")  # two messages on disk

    real = state.conversation_log.read_messages_chained

    def _resume_midway(key, *a, **kw):
        # Stand in for a client opening the tab while the read is in flight.
        state.get_or_create_slot("chat-1-t")
        return real(key, *a, **kw)

    monkeypatch.setattr(state.conversation_log, "read_messages_chained", _resume_midway)

    if path == "open_slots":
        restored = asyncio.run(resume_deferred_restores(state, ["chat-1-t"], []))
    else:
        sessions = [
            # Same derivation restore_recent_sessions uses: list_sessions() can
            # report either the "dashboard:" key or its "dashboard_" filename fold.
            (s["key"].removeprefix("dashboard:").removeprefix("dashboard_"), s)
            for s in state.conversation_log.list_sessions()
            if s.get("key", "").endswith("chat-1-t")
        ]
        assert sessions, "seeded session not listed"
        restored = asyncio.run(resume_deferred_restores(state, [], sessions))

    assert restored == 0, "a slot that came back on its own was counted as restored"
    assert state._slots["chat-1-t"].messages == [], "history replayed onto a live slot"
