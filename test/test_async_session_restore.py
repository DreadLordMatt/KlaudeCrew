"""Durability tests for the async startup session restore.

Startup rehydration used to run unbounded and synchronously on the event loop
while the loop-stall watchdog was already armed, so a home with enough history
killed its own gateway on every boot. The restore now collects its plan in a
worker thread and replays it as a background task that yields between sessions.

Making the restore concurrent puts it alongside machinery that previously could
never observe it — the 5s flush, the shutdown save, live client requests. These
tests pin the three invariants that keep that safe, and they are written as
*invariants checked under adversarial timing* rather than one test per race:

1. MONOTONE SNAPSHOT -- ``open_slots.json`` is never written narrower than the
   key set it already held, at any point during the replay.
2. CRASH SAFETY -- killing the replay at any index leaves the file complete, and
   a second boot restores everything.
3. CONTENT INTEGRITY -- the session JSONL bytes are unchanged by a restore plus
   a flush. No duplicated or dropped message lines.
4. CONCURRENT FLUSH -- (1) and (3) hold with a real flush thread running
   throughout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_persistence import (
    _collect_restore_plan,
    _run_restore_plan,
    restore_open_slots,
    restore_recent_sessions,
    restore_sessions_at_startup,
    save_all_slots_to_history,
)
from kiro_crew.dashboard.chat_utils import _history_key_for


def _seed(state, slot_key: str, *, messages: int = 1, closed: bool = False) -> None:
    """Write session metadata plus *messages* lines so a rehydrate succeeds."""
    history_key = _history_key_for(slot_key)
    log = state.conversation_log
    assert log is not None
    for i in range(messages):
        log.append(history_key, "user" if i % 2 == 0 else "assistant", f"m{i} {slot_key}")
    if closed:
        log.update_metadata(history_key, {"closed": True})


def _write_open_slots(tmp_path, keys: list[str]) -> None:
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": keys, "ts": 0.0}), encoding="utf-8"
    )


def _snapshot_keys(tmp_path) -> set[str]:
    return set(json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))["keys"])


def _session_messages(state) -> dict[str, list[tuple[str, str]]]:
    """Per-file ordered ``(role, content)`` of every message line.

    Not raw bytes: a forced save legitimately enriches records (``tab_id`` on
    the metadata line, ``source_thread`` / ``source_user`` on messages), and it
    does so on the unchanged synchronous restore path too. What must never
    change is the message sequence itself — a partial-window save corrupts the
    frozen-prefix accounting and shows up here as dropped or duplicated lines.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for p in sorted(state.conversation_log._dir.glob("*.jsonl")):
        rows: list[tuple[str, str]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                rows.append(("<unparseable>", line))
                continue
            if rec.get("_type") == "metadata":
                continue
            rows.append((rec.get("role", ""), rec.get("content", "")))
        out[p.name] = rows
    return out


# ── invariant 1: the snapshot is monotone at EVERY yield ──────────────────────


def test_snapshot_never_narrows_at_any_point_during_the_replay(tmp_path, monkeypatch):
    """The floor invariant. A flush landing at any instant of the replay must
    write a key set that still contains everything the file already held."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 13)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)
    original = set(keys)

    observed: list[set[str]] = []
    real_create = state.get_or_create_slot

    def _snapshot_after_every_apply(name=None, *a, **kw):
        # get_or_create_slot is the first thing each apply does, so firing the
        # snapshot from here samples mid-replay as well as between sessions.
        slot = real_create(name, *a, **kw)
        state._persist_open_slots()
        observed.append(_snapshot_keys(tmp_path))
        return slot

    monkeypatch.setattr(state, "get_or_create_slot", _snapshot_after_every_apply)

    async def _run() -> None:
        plan = _collect_restore_plan(state.conversation_log, 0, folders_only=False)
        state._restore_floor = tuple(plan.open_keys)
        await _run_restore_plan(state, plan)

    asyncio.run(_run())

    assert observed, "no snapshot was taken during the replay"
    for i, seen in enumerate(observed):
        assert original <= seen, f"snapshot {i} dropped {sorted(original - seen)}"
    state._persist_open_slots()
    assert original <= _snapshot_keys(tmp_path)
    assert state._restore_floor == (), "floor not released after a completed pass"


def test_floor_is_seeded_from_the_file_not_from_the_plan(tmp_path, monkeypatch):
    """The floor must survive a plan that under-collects. Seeding it from the
    file's own bytes is what makes a collection bug non-fatal."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-t", "chat-2-t", "chat-3-t"]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    plan = _collect_restore_plan(state.conversation_log, 0, folders_only=False)
    assert set(plan.open_keys) == set(keys)
    # Simulate a collection that lost a key, then snapshot mid-replay.
    state._restore_floor = tuple(plan.open_keys)
    plan.open_keys = plan.open_keys[:1]
    state._persist_open_slots()
    assert _snapshot_keys(tmp_path) == set(keys)


# ── invariant 2: crash at any index loses nothing ─────────────────────────────


@pytest.mark.parametrize("die_at", [0, 1, 2, 3, 4])
def test_crash_at_any_index_keeps_every_tab(tmp_path, monkeypatch, die_at):
    """Cancellation mid-replay is the shutdown case. The floor must NOT be
    released on the way out, so the snapshot still records the un-restored
    tabs and the next boot brings them all back."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 6)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    applied = 0
    real_create = state.get_or_create_slot

    def _die_midway(name=None, *a, **kw):
        nonlocal applied
        if applied == die_at:
            applied += 1
            raise asyncio.CancelledError()
        applied += 1
        return real_create(name, *a, **kw)

    monkeypatch.setattr(state, "get_or_create_slot", _die_midway)

    async def _run() -> None:
        plan = _collect_restore_plan(state.conversation_log, 0, folders_only=False)
        state._restore_floor = tuple(plan.open_keys)
        with pytest.raises(asyncio.CancelledError):
            await _run_restore_plan(state, plan)

    asyncio.run(_run())

    assert state._restore_floor != (), "floor released on a cancelled pass"
    # A shutdown save landing right now still records the full tab set.
    save_all_slots_to_history(state)
    assert _snapshot_keys(tmp_path) == set(keys)

    # Second boot: everything comes back.
    state2 = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state2) == len(keys)
    assert set(state2._slots) == set(keys)


# ── invariant 3: the session files are byte-identical afterwards ──────────────


def test_restore_plus_flush_preserves_every_message(tmp_path, monkeypatch):
    """Restore is a read. A save landing mid-replay would write a partial window
    with the wrong frozen-prefix accounting, so this compares the message
    sequence on disk rather than trusting the guard."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 7)]
    for k in keys:
        _seed(state, k, messages=6)
    _write_open_slots(tmp_path, keys)
    before = _session_messages(state)
    assert all(len(v) == 6 for v in before.values()), "seeding did not land"

    async def _run() -> None:
        plan = _collect_restore_plan(state.conversation_log, 0, folders_only=False)
        state._restore_floor = tuple(plan.open_keys)
        await _run_restore_plan(state, plan)

    asyncio.run(_run())
    state._flush_dirty_slots()
    save_all_slots_to_history(state)

    assert _session_messages(state) == before, "restore changed session content"
    # And the restore actually happened, so the comparison is not vacuous.
    assert set(state._slots) == set(keys)
    assert all(len(s.messages) == 6 for s in state._slots.values())


def test_flush_during_a_replay_is_skipped_without_losing_the_dirty_flag(tmp_path, monkeypatch):
    """The skip has to land before _flush_dirty_slots clears _dirty, or the flag
    is dropped without a write and real messages never reach disk."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    slot = state.get_or_create_slot("chat-1-t")
    slot.append("user", "mid-replay")
    slot._dirty = True

    state._restoring_keys.add("chat-1-t")
    state._flush_dirty_slots()
    assert slot._dirty is True, "dirty cleared without a save"

    state._restoring_keys.discard("chat-1-t")
    state._flush_dirty_slots()
    assert slot._dirty is False


# ── invariant 4: both hold with a real flush thread running ───────────────────


def test_concurrent_flush_thread_holds_both_invariants(tmp_path, monkeypatch):
    """_persist_open_slots and _flush_dirty_slots really do run from the flush
    executor, so drive them from a thread for the whole replay."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 11)]
    for k in keys:
        _seed(state, k, messages=4)
    _write_open_slots(tmp_path, keys)
    original = set(keys)
    before = _session_messages(state)

    stop = threading.Event()
    violations: list[str] = []

    def _flusher() -> None:
        while not stop.is_set():
            try:
                state._flush_dirty_slots()
                seen = _snapshot_keys(tmp_path)
                if not original <= seen:
                    violations.append(f"snapshot dropped {sorted(original - seen)}")
            except Exception as exc:  # pragma: no cover - reported, not raised
                violations.append(f"flush raised {exc!r}")

    async def _run() -> None:
        plan = _collect_restore_plan(state.conversation_log, 0, folders_only=False)
        state._restore_floor = tuple(plan.open_keys)
        t = threading.Thread(target=_flusher, daemon=True)
        t.start()
        try:
            await _run_restore_plan(state, plan)
        finally:
            stop.set()
            t.join(timeout=5)

    asyncio.run(_run())

    assert violations == []
    assert _session_messages(state) == before, "concurrent flush changed session content"
    assert set(state._slots) == set(keys)


# ── the collect phase cannot mutate state, structurally ──────────────────────


def test_collect_is_read_only_and_dedup_happens_at_apply_time(tmp_path, monkeypatch):
    """Collect takes a ConversationLog, not a DashboardState, so it cannot touch
    slots. It intentionally does NOT pre-skip open keys — a stacked-file tab
    needs the recent half as its fallback — so the guarantee that each session is
    materialized once lives in the apply-time ``in state._slots`` guard."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    for k in ("chat-1-t", "chat-2-t", "chat-3-t"):
        _seed(state, k, messages=4)
    _write_open_slots(tmp_path, ["chat-1-t", "chat-2-t"])

    plan = _collect_restore_plan(state.conversation_log, 60, folders_only=False)

    assert state._slots == {}, "collect mutated state"
    assert set(plan.open_keys) == {"chat-1-t", "chat-2-t"}
    # Both halves see the shared keys; dedup is the applier's job.
    assert {name for name, _, _ in plan.sessions} == {"chat-1-t", "chat-2-t", "chat-3-t"}

    before = _session_messages(state)

    async def _run() -> int:
        state._restore_floor = tuple(plan.open_keys)
        return await _run_restore_plan(state, plan)

    # 3 distinct slots, not 5 applies — the shared keys are restored once.
    assert asyncio.run(_run()) == 3
    assert set(state._slots) == {"chat-1-t", "chat-2-t", "chat-3-t"}
    assert all(len(s.messages) == 4 for s in state._slots.values())
    state._flush_dirty_slots()
    save_all_slots_to_history(state)
    assert _session_messages(state) == before, "a session was replayed twice"


def test_closed_and_path_traversal_keys_are_rejected_by_collect(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-1-t")
    _seed(state, "chat-2-t", closed=True)
    _write_open_slots(tmp_path, ["chat-1-t", "../../etc/passwd", "", "chat-1-t"])

    plan = _collect_restore_plan(state.conversation_log, 60, folders_only=False)

    assert plan.open_keys == ["chat-1-t"], "traversal, empty or duplicate key survived"
    # closed sessions never reach the recent-session half
    assert {name for name, _, _ in plan.sessions} == {"chat-1-t"}


# ── startup entry point ───────────────────────────────────────────────────────


def test_api_create_during_replay_does_not_truncate_the_session(tmp_path, monkeypatch):
    """The replay window is reachable by any client: /api/chat and the
    OpenAI-compat endpoint both create a slot from a request-supplied key and do
    NOT rehydrate on a miss. An empty slot registered under a pending key has
    _disk_older_count=0 / _disk_window_len=0, is not covered by _restoring_keys,
    and would be flushed over the victim's history.

    Those handlers therefore await ensure_pending_slot_restored() first. Driven
    through the real endpoint rather than get_or_create_slot, because the guard
    now lives at the call site: calling the factory directly would pass here
    while the shipped request path stayed broken.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-t", "chat-2-t", "chat-3-t"]
    for k in keys:
        _seed(state, k, messages=6)
    _write_open_slots(tmp_path, keys)
    before = _session_messages(state)

    import kiro_crew.dashboard.chat_persistence as cp

    async def _run() -> None:
        await cp.restore_sessions_at_startup(state, 0)
        assert "chat-3-t" in state._pending_restore_keys, "key was not reserved"
        await cp.ensure_pending_slot_restored(state, "chat-3-t")
        hijacked = state.get_or_create_slot("chat-3-t")
        assert len(hijacked.messages) == 6, "slot materialized empty"
        for task in list(state._background_tasks):
            await task

    asyncio.run(_run())

    assert set(state._slots) == set(keys)
    counts = {k: len(s.messages) for k, s in sorted(state._slots.items())}
    assert all(v == 6 for v in counts.values()), counts

    # And a flush must not rewrite any file from that slot.
    state._flush_dirty_slots()
    save_all_slots_to_history(state)
    assert _session_messages(state) == before, "session history was truncated"


@pytest.mark.asyncio
async def test_send_to_a_pending_key_restores_it_through_the_real_endpoint(
    tmp_path, monkeypatch
):
    """End-to-end of the above over POST /api/chat.

    Guards the wiring, not the helper: a future refactor that drops the awaited
    call from the handler leaves the helper's own tests green.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-9-t", messages=6)
    _write_open_slots(tmp_path, ["chat-9-t"])
    before = _session_messages(state)

    import kiro_crew.dashboard.chat_persistence as cp

    await cp.restore_sessions_at_startup(state, 0)
    assert "chat-9-t" in state._pending_restore_keys

    app = _make_app(state)
    async with TestClient(TestServer(app)) as client:
        # The send itself needs no agent to run: the restore happens before the
        # handler dispatches, so a failure to launch still exercises the guard.
        with contextlib.suppress(Exception):
            await client.post("/api/chat", json={"slot": "chat-9-t", "message": "hi"})

    assert len(state._slots["chat-9-t"].messages) >= 6, "session was not restored"
    for task in list(state._background_tasks):
        await task
    state._flush_dirty_slots()
    assert _session_messages(state)["dashboard_chat-9-t.jsonl"][:6] == (
        before["dashboard_chat-9-t.jsonl"]
    ), "restored prefix was rewritten"


@pytest.mark.asyncio
async def test_resume_during_replay_does_not_double_the_transcript(tmp_path, monkeypatch):
    """A slot factory must not rehydrate on behalf of a caller that rehydrates.

    api_chat_slot_resume looks the slot up in _slots, misses on a pending key
    (it is reserved but not yet replayed), falls through to get_or_create_slot,
    and then reads the window and appends it itself. When the factory rehydrated
    the pending key, the handler received a slot already holding N messages and
    appended the same N again, leaving 2N with _disk_older_count = max(0, N-N) =
    0. The on-demand path never entered _restoring_keys, so the next flush wrote
    every line twice and the transcript was permanently doubled on disk.

    This test fails with 12 messages against that branch.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-7-t", messages=6)
    _write_open_slots(tmp_path, ["chat-7-t"])
    before = _session_messages(state)

    import kiro_crew.dashboard.chat_persistence as cp

    await cp.restore_sessions_at_startup(state, 0)
    assert "chat-7-t" in state._pending_restore_keys, "key was not reserved"

    app = _make_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/slots/chat-7-t/resume", json={"key": "dashboard:chat-7-t"}
        )
        assert resp.status == 200, await resp.text()

    assert len(state._slots["chat-7-t"].messages) == 6, "resume replayed the window twice"

    for task in list(state._background_tasks):
        await task
    state._flush_dirty_slots()
    save_all_slots_to_history(state)
    assert _session_messages(state) == before, "transcript was doubled on disk"


@pytest.mark.asyncio
async def test_concurrent_requests_for_one_pending_key_share_one_read(tmp_path, monkeypatch):
    """Single-flight: N tabs on one key must not each replay its window.

    Relocating the rehydrate to the handlers made the claim-to-apply window
    interruptible — every waiter awaits between its own reads. Without the task
    map each concurrent request would read and apply the same window, which is
    the doubling above by a different route.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-5-t", messages=6)
    before = _session_messages(state)

    import kiro_crew.dashboard.chat_persistence as cp

    reads = []
    real = state.conversation_log.read_messages_chained

    def _counted(key, *a, **kw):
        reads.append(key)
        return real(key, *a, **kw)

    monkeypatch.setattr(state.conversation_log, "read_messages_chained", _counted)

    # Reserve by hand rather than running a plan: the bulk pass legitimately
    # reads this key too, and counting its read would measure the concurrent-race
    # allowance (one wasted read, asserted by the content-integrity tests) rather
    # than the dedup this test is about.
    state._pending_restore_keys.add("chat-5-t")
    await asyncio.gather(*(cp.ensure_pending_slot_restored(state, "chat-5-t") for _ in range(8)))

    assert reads.count("dashboard:chat-5-t") == 1, f"window read {len(reads)}x: {reads}"
    assert len(state._slots["chat-5-t"].messages) == 6
    state._flush_dirty_slots()
    assert _session_messages(state) == before


@pytest.mark.asyncio
async def test_a_failed_on_demand_restore_refuses_the_request(tmp_path, monkeypatch):
    """A failed on-demand restore must not let the caller create a fresh slot.

    Swallowing the error and settling the key was the original data loss by
    another route: the handler proceeds to get_or_create_slot, registers an empty
    slot under a key whose transcript is intact, and the next flush writes the
    empty window over it. "The bulk pass will retry" only holds while no slot
    exists, because the bulk pass skips a key that is already in _slots.

    So the failure propagates as a retryable 503 and the reservation is retained.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-8-t", messages=6)
    before = _session_messages(state)

    import kiro_crew.dashboard.chat_persistence as cp

    def _boom(*a, **kw):
        raise OSError("transient read failure")

    monkeypatch.setattr(state.conversation_log, "read_messages_chained", _boom)
    state._pending_restore_keys.add("chat-8-t")

    with pytest.raises(web.HTTPServiceUnavailable):
        await cp.ensure_pending_slot_restored(state, "chat-8-t")

    assert "chat-8-t" in state._pending_restore_keys, "reservation was released on failure"
    assert "chat-8-t" not in state._slots, "a partial slot survived the failure"

    # And the transcript is untouched: nothing was written over it.
    state._flush_dirty_slots()
    save_all_slots_to_history(state)
    assert _session_messages(state) == before


def test_sync_injector_defers_instead_of_reading_on_the_loop(tmp_path, monkeypatch):
    """The cron and workflow injectors run on the loop and cannot await.

    Reading the history inline would put the unbounded history-scaled read back
    on the loop, which is what this PR removes. They hand off to the off-loop
    restore instead and are re-entered once the slot is real.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    _seed(state, "cron-job1", messages=6)
    before = _session_messages(state)

    import kiro_crew.dashboard.chat_persistence as cp

    calls: list[str] = []

    async def _run() -> None:
        state._pending_restore_keys.add("cron-job1")
        deferred = cp.ensure_restored_before_inject(
            state, "cron-job1", lambda: calls.append("retried")
        )
        assert deferred is True, "injector read on the loop instead of deferring"
        assert "cron-job1" not in state._slots, "slot was materialized synchronously"
        for task in list(state._background_tasks):
            await task
        assert calls == ["retried"], "the caller was never re-entered"
        assert len(state._slots["cron-job1"].messages) == 6

    asyncio.run(_run())
    state._flush_dirty_slots()
    save_all_slots_to_history(state)
    assert _session_messages(state) == before, "session history was truncated"


def test_injector_restores_inline_when_no_loop_is_running(tmp_path, monkeypatch):
    """With no running loop there is nothing to stall, so the read is inline.

    Keeps the guard correct for synchronous callers rather than leaving the key
    unprotected when there is no loop to schedule onto.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    _seed(state, "cron-job2", messages=6)
    before = _session_messages(state)

    import kiro_crew.dashboard.chat_persistence as cp

    state._pending_restore_keys.add("cron-job2")
    assert cp.ensure_restored_before_inject(state, "cron-job2", lambda: None) is False
    assert len(state._slots["cron-job2"].messages) == 6, "slot materialized empty"
    state._flush_dirty_slots()
    save_all_slots_to_history(state)
    assert _session_messages(state) == before


def test_floor_is_installed_before_the_scan_yields(tmp_path, monkeypatch):
    """The scan yields the loop and the 5s flush fires during it with _slots
    still empty. If the floor is installed after the scan, that flush writes an
    empty open_slots.json over the file the floor exists to protect."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 5)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    import kiro_crew.dashboard.chat_persistence as cp

    real_collect = cp._collect_restore_plan
    during: list[set[str]] = []

    def _flush_during_collect(*a, **kw):
        # Stand in for the periodic flush landing inside the scan.
        state._persist_open_slots()
        during.append(_snapshot_keys(tmp_path))
        return real_collect(*a, **kw)

    monkeypatch.setattr(cp, "_collect_restore_plan", _flush_during_collect)

    async def _run() -> None:
        await cp.restore_sessions_at_startup(state, 0)
        for task in list(state._background_tasks):
            await task

    asyncio.run(_run())

    assert during, "the flush never ran during the scan"
    assert during[0] == set(keys), "snapshot was wiped before the floor was installed"
    assert set(state._slots) == set(keys)


def test_open_keys_are_reserved_before_the_scan_yields(tmp_path, monkeypatch):
    """A returning browser tab holds exactly the open-tab keys, so it is the most
    likely thing to arrive during the scan. Reserving them only after the scan
    would let that request register an empty slot over a real transcript."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-t", "chat-2-t", "chat-3-t"]
    for k in keys:
        _seed(state, k, messages=6)
    _write_open_slots(tmp_path, keys)
    before = _session_messages(state)

    import kiro_crew.dashboard.chat_persistence as cp

    real_collect = cp._collect_restore_plan
    hijacked: list[int] = []

    def _client_arrives_during_the_scan(*a, **kw):
        # The scan runs in a worker thread, but the request it races runs on the
        # loop; asserting the reservation is live here is the loop-safe way to
        # prove the ordering without driving slot mutation off-loop.
        hijacked.append(len(state._pending_restore_keys))
        return real_collect(*a, **kw)

    monkeypatch.setattr(cp, "_collect_restore_plan", _client_arrives_during_the_scan)

    async def _run() -> None:
        await cp.restore_sessions_at_startup(state, 0)
        for task in list(state._background_tasks):
            await task

    asyncio.run(_run())

    assert hijacked == [len(keys)], "open keys were not reserved before the scan"
    state._flush_dirty_slots()
    save_all_slots_to_history(state)
    assert _session_messages(state) == before


def test_recent_sessions_still_restore_without_an_open_slots_file(tmp_path, monkeypatch):
    """A fresh home has no snapshot. The listing must still be built, or the
    recent-session half iterates nothing and the restore silently does nothing."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-t", "chat-2-t", "chat-3-t"]
    for k in keys:
        _seed(state, k)
    assert not (tmp_path / "open_slots.json").exists()

    plan = _collect_restore_plan(state.conversation_log, 60, folders_only=False)

    assert plan.open_keys == []
    assert set(plan.listing) == set(keys), "listing was not built without a snapshot"
    assert {name for name, _, _ in plan.sessions} == set(keys)

    async def _run() -> int:
        return await _run_restore_plan(state, plan)

    assert asyncio.run(_run()) == len(keys)
    assert set(state._slots) == set(keys)


def test_stacked_history_prefix_folds_to_the_canonical_slot(tmp_path, monkeypatch):
    """A legacy ``dashboard_dashboard_x`` file must resolve to the slot name
    get_or_create_slot registers, so it lands in ONE slot rather than missing the
    dedup guard and later colliding with the canonical key.

    Note this does not assert content stability for a canonical/stacked pair:
    ``list_sessions()`` deduplicates stacked prefixes keeping the NEWER file, so
    a newer twin's window legitimately becomes the canonical session's content.
    That policy predates this change and the unfolded path on main produces the
    same outcome.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    log = state.conversation_log
    _seed(state, "chat-1-t", messages=4)
    for i in range(4):
        log.append("dashboard_dashboard_chat-1-t", "user", f"stacked{i}")

    plan = _collect_restore_plan(log, 60, folders_only=False)
    names = [name for name, _, _ in plan.sessions]

    assert all(not n.startswith("dashboard") for n in names), names
    assert names.count("chat-1-t") == 1, "stacked twin produced a second slot name"

    async def _run() -> None:
        await _run_restore_plan(state, plan)

    asyncio.run(_run())

    # One slot, not two, and no dashboard_-prefixed key in the sidebar.
    assert list(state._slots) == ["chat-1-t"]


def test_open_tab_replay_loads_config_and_agents_once_not_per_key(tmp_path, monkeypatch):
    """The config load and the agent-directory glob are per-process facts. Doing
    them per key put an unbounded, avoidable scan back on the event loop."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 9)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    import kiro_crew.dashboard.chat_persistence as cp

    calls = {"cfg": 0, "agents": 0}
    real_cfg, real_agents = cp._load_restore_cfg, cp._kiro_model_map

    def _count_cfg():
        calls["cfg"] += 1
        return real_cfg()

    def _count_agents():
        calls["agents"] += 1
        return real_agents()

    monkeypatch.setattr(cp, "_load_restore_cfg", _count_cfg)
    monkeypatch.setattr(cp, "_kiro_model_map", _count_agents)

    async def _run() -> int:
        plan = _collect_restore_plan(state.conversation_log, 0, folders_only=False)
        state._restore_floor = tuple(plan.open_keys)
        return await _run_restore_plan(state, plan)

    assert asyncio.run(_run()) == len(keys)
    # One collect builds both; the replay must reuse them for all 8 keys.
    assert calls["cfg"] <= 1, calls
    assert calls["agents"] <= 1, calls


def test_open_tab_backed_only_by_a_legacy_stacked_file_is_still_restored(tmp_path, monkeypatch):
    """An open-slot key whose only file on disk is a stacked
    ``dashboard_dashboard_x.jsonl`` cannot be resolved by the canonical
    rehydrate. Skipping it in the recent half as "already covered" would drop
    that tab from both paths."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    log = state.conversation_log
    # Only the stacked twin exists; no dashboard_chat-9-t.jsonl.
    for i in range(3):
        log.append("dashboard_dashboard_chat-9-t", "user", f"legacy{i}")
    _write_open_slots(tmp_path, ["chat-9-t"])

    plan = _collect_restore_plan(log, 60, folders_only=False)
    assert plan.open_keys == ["chat-9-t"]
    assert "chat-9-t" in {name for name, _, _ in plan.sessions}, "stacked session skipped"

    async def _run() -> int:
        state._restore_floor = tuple(plan.open_keys)
        return await _run_restore_plan(state, plan)

    assert asyncio.run(_run()) == 1
    assert "chat-9-t" in state._slots
    assert len(state._slots["chat-9-t"].messages) == 3


def test_a_stacked_only_tab_stays_reserved_until_the_recent_half_replays_it(
    tmp_path, monkeypatch
):
    """A tab in BOTH halves must not be released by the first one to finish.

    A tab backed only by a legacy stacked file has its folded name in
    ``open_keys``, but the canonical history key has no file, so the open-tab
    half restores nothing. Releasing the reservation in its ``finally`` left the
    key unguarded across every remaining yield until the recent half reached it.
    A request landing in that window registers an empty slot, the recent half
    then skips the key as already-live, and the history is gone.

    The probe interleaves with the replay's own yields, so every witnessed moment
    is exactly a point where a request could arrive.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    log = state.conversation_log
    for i in range(3):
        log.append("dashboard_dashboard_chat-9-t", "user", f"legacy{i}")
    # A couple of ordinary tabs so the replay actually yields more than once.
    for k in ("chat-1-t", "chat-2-t"):
        _seed(state, k, messages=4)
    _write_open_slots(tmp_path, ["chat-9-t", "chat-1-t", "chat-2-t"])

    plan = _collect_restore_plan(log, 60, folders_only=False)
    assert "chat-9-t" in {name for name, _, _ in plan.sessions}

    witnessed: list[tuple[bool, bool]] = []

    async def _run() -> int:
        state._restore_floor = tuple(plan.open_keys)
        state._pending_restore_keys.update(plan.open_keys)
        state._pending_restore_keys.update(name for name, _, _ in plan.sessions)
        task = asyncio.ensure_future(_run_restore_plan(state, plan))
        while not task.done():
            await asyncio.sleep(0)
            witnessed.append(
                ("chat-9-t" in state._pending_restore_keys, "chat-9-t" in state._slots)
            )
        return await task

    assert asyncio.run(_run()) >= 1
    unguarded = [i for i, (reserved, live) in enumerate(witnessed) if not reserved and not live]
    assert not unguarded, f"key was unguarded at yields {unguarded} of {len(witnessed)}"
    assert len(state._slots["chat-9-t"].messages) == 3, "stacked history was not restored"


@pytest.mark.asyncio
async def test_on_demand_replay_does_not_reglob_the_agent_directory(tmp_path, monkeypatch):
    """The on-demand path must reuse the plan's per-process facts.

    ``_kiro_model_map`` globs the agent directory and ``_load_restore_cfg`` reads
    config from disk. Neither is per-session, and the bulk pass already passes
    both down — an on-demand replay that re-derived them would put a filesystem
    walk back on the very path this PR clears.

    Both facts are EMPTY on a home with no kiro agents and no config, which is
    exactly the CI environment, so the published-vs-absent test must be a
    ``None`` sentinel and not container truthiness. Testing truthiness re-derived
    them on precisely the homes where the glob finds nothing.

    Scoped to the model map, which is the directory walk. A published ``cfg`` of
    ``None`` still falls through to a config read inside
    ``_rehydrate_slot_from_history``, whose kwargs treat ``None`` as unset. That
    is a single bounded file read, it only happens on a home with no config at
    all, and the bulk pass behaves identically when ``plan.cfg`` is ``None`` — so
    it is pre-existing and not history-scaled.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-4-t", messages=5)
    _write_open_slots(tmp_path, ["chat-4-t"])

    import kiro_crew.dashboard.chat_persistence as cp

    await cp.restore_sessions_at_startup(state, 0)
    assert "chat-4-t" in state._pending_restore_keys
    # Force the empty shape the CI host produces, so this asserts the sentinel
    # rather than whatever this machine's agent directory happens to hold.
    state._restore_shared = ({}, {}, None)

    derived: list[str] = []
    monkeypatch.setattr(cp, "_kiro_model_map", lambda: derived.append("map") or {})

    await cp.ensure_pending_slot_restored(state, "chat-4-t")

    assert "map" not in derived, "on-demand replay re-globbed the agent directory"
    assert len(state._slots["chat-4-t"].messages) == 5
    for task in list(state._background_tasks):
        await task


def test_a_failed_bulk_restore_keeps_its_reservation(tmp_path, monkeypatch):
    """A key whose bulk restore raised still has its transcript on disk.

    Releasing the reservation there let the next request register an empty slot
    over that session — the same data loss the on-demand path already refuses.
    The floor retention added earlier protects ``open_slots.json``; this protects
    the transcript itself.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path / "sessions")
    for k in ("chat-1-t", "chat-2-t"):
        _seed(state, k, messages=4)
    _write_open_slots(tmp_path, ["chat-1-t", "chat-2-t"])
    before = _session_messages(state)

    log = state.conversation_log
    real = log.read_messages_chained

    def _boom_for_one(key, *a, **kw):
        if key.endswith("chat-2-t"):
            raise OSError("transient read failure")
        return real(key, *a, **kw)

    monkeypatch.setattr(log, "read_messages_chained", _boom_for_one)

    plan = _collect_restore_plan(log, 0, folders_only=False)

    async def _run() -> None:
        state._restore_floor = tuple(plan.open_keys)
        state._pending_restore_keys.update(plan.open_keys)
        await _run_restore_plan(state, plan)

    asyncio.run(_run())

    assert "chat-2-t" in state._pending_restore_keys, "failed key lost its reservation"
    assert "chat-2-t" not in state._slots
    assert "chat-1-t" not in state._pending_restore_keys, "healthy key was not released"
    # The transcript of the failed key is untouched.
    state._flush_dirty_slots()
    save_all_slots_to_history(state)
    assert _session_messages(state)["dashboard_chat-2-t.jsonl"] == (
        before["dashboard_chat-2-t.jsonl"]
    )


def test_metadata_is_read_off_the_event_loop_for_open_tabs(tmp_path, monkeypatch):
    """get_metadata reads the whole file to take its first line and the mtime
    cache is cold on the first read after boot, so it must not run on the loop."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-t", "chat-2-t"]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    loop_thread = threading.current_thread()
    read_threads: list[threading.Thread] = []
    real = state.conversation_log.get_metadata

    def _spy(key, *a, **kw):
        read_threads.append(threading.current_thread())
        return real(key, *a, **kw)

    async def _run() -> int:
        plan = _collect_restore_plan(state.conversation_log, 0, folders_only=False)
        state._restore_floor = tuple(plan.open_keys)
        # Patch AFTER collect so only the replay's reads are observed.
        monkeypatch.setattr(state.conversation_log, "get_metadata", _spy)
        return await _run_restore_plan(state, plan)

    assert asyncio.run(_run()) == len(keys)
    assert read_threads, "no metadata was read during the replay"
    assert all(t is not loop_thread for t in read_threads)


def test_a_failed_open_tab_keeps_its_floor_entry(tmp_path, monkeypatch):
    """A read that raises may be transient. Releasing its floor entry would let
    the next flush write a snapshot without it and lose the tab for good, while
    everything that actually settled must still release."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 6)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    real = state.conversation_log.read_messages_chained

    def _boom(key, *a, **kw):
        if "chat-3-t" in key:
            raise OSError("transient disk gremlin")
        return real(key, *a, **kw)

    monkeypatch.setattr(state.conversation_log, "read_messages_chained", _boom)

    async def _run() -> None:
        plan = _collect_restore_plan(state.conversation_log, 0, folders_only=False)
        state._restore_floor = tuple(plan.open_keys)
        await _run_restore_plan(state, plan)

    asyncio.run(_run())

    assert "chat-3-t" not in state._slots
    assert state._restore_floor == ("chat-3-t",), state._restore_floor
    # So it survives the very next snapshot, and a later boot retries it.
    state._persist_open_slots()
    assert _snapshot_keys(tmp_path) == set(keys)


def test_startup_reseeds_past_keys_the_replay_has_not_reached(tmp_path, monkeypatch):
    """A new chat minted while the replay is running must not collide with a tab
    about to come back, so the reseed happens before the entry point returns."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed(state, "chat-41-t")
    _write_open_slots(tmp_path, ["chat-41-t"])

    async def _run() -> int:
        total = await restore_sessions_at_startup(state, 0)
        # Counter is already past the pending key, before any replay ran.
        assert state._slot_counter >= 41
        for task in list(state._background_tasks):
            await task
        return total

    # The key is in both halves of the plan (collect does not pre-skip), so the
    # count is the planned applies, not the distinct sessions.
    assert asyncio.run(_run()) >= 1
    assert "chat-41-t" in state._slots


def test_startup_with_nothing_to_restore_releases_the_floor(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    assert asyncio.run(restore_sessions_at_startup(state, 0)) == 0
    assert state._restore_floor == ()


def test_sync_wrappers_still_restore_everything_inline(tmp_path, monkeypatch):
    """The sync wrappers keep their old contract for targeted and test callers:
    everything is restored by the time they return."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-t" for i in range(1, 6)]
    for k in keys:
        _seed(state, k)
    _write_open_slots(tmp_path, keys)

    assert restore_open_slots(state) == len(keys)
    assert set(state._slots) == set(keys)

    state2 = _make_state(tmp_path / "sessions")
    assert restore_recent_sessions(state2, 60) == len(keys)
    assert set(state2._slots) == set(keys)
