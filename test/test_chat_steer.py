"""Tests for the mid-turn steer branch in POST /api/chat (api_chat handler).

Exercises the steer path that reaches the running turn's live AcpClient via
``slot._acp_client``:
  * steered success -> broadcasts ``steer_push`` and returns ``{steered: True}``;
  * steer unavailable (no live client) -> safe fall-through to the queue;
  * steer raises -> caught, falls through to the queue (message never dropped).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


@pytest.fixture
def _patch_sel():
    """Patch sel() so the handler doesn't touch a real SecurityEventLog."""
    mock_sel = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


def _running_slot(state, key="test"):
    """Create a slot and make it look like a turn is in flight.

    ``running`` is ``task is not None and not task.done()``.
    """
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


class TestApiChatSteer:
    @pytest.mark.asyncio
    async def test_steer_injects_into_running_turn(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("steered") is True
            assert data.get("queued") is not True

        client_mock.steer.assert_awaited_once_with("go left")
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "steer_push" in events
        assert "queue_push" not in events  # steered, not queued

    @pytest.mark.asyncio
    async def test_steer_unavailable_falls_back_to_queue(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = None  # no live client -> cannot steer

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "later", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        # message queued (not dropped) and broadcast as queue_push, not steer_push
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "queue_push" in events
        assert "steer_push" not in events

    @pytest.mark.asyncio
    async def test_steer_error_falls_back_to_queue(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(side_effect=RuntimeError("boom"))
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "later", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        client_mock.steer.assert_awaited_once()


class TestSteerPersistsAttachmentMeta:
    """A steered message must persist the ordered attachment lists.

    Without them a reload falls back to scanning the content for markers, which
    is bounded by whitespace and so truncates any path containing a space.
    """

    @pytest.mark.asyncio
    async def test_steer_persists_files_and_dirs(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        spaced_dir = "/repo/my docs"
        spaced_file = "/repo/my notes.txt"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": f"review [attached_dir 1] {spaced_dir} and [attached_file 1] {spaced_file}",
                    "steer": True,
                    "meta": {"files": [spaced_file], "dirs": [spaced_dir]},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        persisted = [m for m in slot.messages if m.get("role") == "user"]
        assert persisted, "the steered message was not persisted"
        meta = persisted[-1].get("meta") or {}
        assert meta.get("steer") is True, "the steer marker must survive"
        assert meta.get("dirs") == [spaced_dir], "spaced folder path lost from meta.dirs"
        assert meta.get("files") == [spaced_file], "spaced file path lost from meta.files"

    @pytest.mark.asyncio
    async def test_steer_without_meta_still_marks_steer(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "plain steer", "steer": True}
            )
            assert resp.status == 200

        persisted = [m for m in slot.messages if m.get("role") == "user"]
        assert (persisted[-1].get("meta") or {}).get("steer") is True

    @pytest.mark.asyncio
    async def test_client_cannot_spoof_the_steer_marker(self, tmp_path, monkeypatch, _patch_sel):
        """`steer` is set server-side last, so a client value cannot override it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": "sneaky",
                    "steer": True,
                    "meta": {"steer": False, "dirs": ["/repo/docs"]},
                },
            )
            assert resp.status == 200

        meta = [m for m in slot.messages if m.get("role") == "user"][-1].get("meta") or {}
        assert meta.get("steer") is True
        assert meta.get("dirs") == ["/repo/docs"]

    @pytest.mark.asyncio
    async def test_steer_push_echo_carries_meta(self, tmp_path, monkeypatch, _patch_sel):
        """The WS echo must mirror the metadata, not just the content.

        A second open tab renders the steered bubble from this event. Without the
        ordered lists it falls back to the whitespace-bounded content scan and
        shows `/repo/my` for `/repo/my docs` until the page is reloaded.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        spaced_dir = "/repo/my docs"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": f"review [attached_dir 1] {spaced_dir}",
                    "steer": True,
                    "meta": {"dirs": [spaced_dir]},
                },
            )
            assert resp.status == 200

        pushes = [c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "steer_push"]
        assert pushes, "no steer_push event was broadcast"
        payload = pushes[-1].args[1]
        assert payload.get("meta", {}).get("dirs") == [spaced_dir], "steer_push dropped meta.dirs"
        assert payload.get("meta", {}).get("steer") is True


class TestQueuedSendPersistsAttachmentMeta:
    """A queued (mid-turn / held) send must carry its attachment lists too.

    The queue is the other path a message with attachments can take. It used to
    store only `{id, content, kind}`, so the drain persisted the markers without
    the ordered lists and a spaced path truncated on replay — the same defect as
    the steer path, one layer down.
    """

    @pytest.mark.asyncio
    async def test_queued_message_carries_meta_to_the_drain(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        # No live ACP client -> steer is unavailable -> falls through to the queue.
        slot._acp_client = None

        spaced_dir = "/repo/my docs"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": f"review [attached_dir 1] {spaced_dir}",
                    "meta": {"dirs": [spaced_dir]},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        assert slot._queue, "the message was not queued"
        assert slot._queue[-1].get("meta", {}).get("dirs") == [spaced_dir], (
            "queue entry dropped meta.dirs, so the drain cannot persist it"
        )

    def test_queue_append_omits_meta_key_when_absent(self, tmp_path, monkeypatch):
        """An unattached message keeps the original 3-key queue shape."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        slot.queue_append("plain text")
        assert set(slot._queue[-1]) == {"id", "content", "kind"}

    def test_attachment_bearing_entry_is_never_merged(self, tmp_path, monkeypatch):
        """Merging would put two marker index spaces under one metadata list.

        `[attached_dir 1]` in the second message would resolve against the first
        message's `meta.dirs`, so an attachment-bearing entry must drain alone.
        """
        from kiro_crew.dashboard.chat_utils import _dequeue_next_message

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        slot.queue_append("plain one")
        slot.queue_append("plain two")
        slot.queue_append("with folder [attached_dir 1] /repo/my docs", meta={"dirs": ["/repo/my docs"]})

        msg, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert len(consumed) == 2, "the merge must stop at the attachment-bearing entry"
        assert "with folder" not in msg

        msg2, consumed2 = _dequeue_next_message(slot, merge_enabled=True)
        assert len(consumed2) == 1
        assert consumed2[0]["meta"]["dirs"] == ["/repo/my docs"]


class TestQueuedAppendMeta:
    """The drain's metadata-selection rule.

    Previously this logic was inline in ``chat_runner``'s drain block, which no
    test reached — reverting it left every test green while spaced paths
    truncated again after a queue drain. Extracted so the contract is directly
    asserted here, with the call site guarded in test_chat_runner_drain below.
    """

    def test_single_user_item_carries_its_meta(self):
        from kiro_crew.dashboard.chat_utils import queued_append_meta

        item = {"id": "a", "content": "x", "kind": "", "meta": {"dirs": ["/repo/my docs"]}}
        assert queued_append_meta([item]) == {"dirs": ["/repo/my docs"]}

    def test_item_without_meta_yields_none(self):
        from kiro_crew.dashboard.chat_utils import queued_append_meta

        assert queued_append_meta([{"id": "a", "content": "x", "kind": ""}]) is None

    def test_merged_items_yield_none(self):
        """Two messages have two independent marker index spaces."""
        from kiro_crew.dashboard.chat_utils import queued_append_meta

        a = {"id": "a", "content": "x", "kind": "", "meta": {"dirs": ["/a"]}}
        b = {"id": "b", "content": "y", "kind": ""}
        assert queued_append_meta([a, b]) is None

    def test_injections_never_inherit_meta(self):
        """cron / sub-agent / recovery injections carry no attachments."""
        from kiro_crew.dashboard.chat_utils import queued_append_meta

        item = {"id": "a", "content": "x", "kind": "", "meta": {"dirs": ["/a"]}}
        assert queued_append_meta([item], is_cron=True) is None
        assert queued_append_meta([item], is_subagent=True) is None
        assert queued_append_meta([item], is_recovery=True) is None

    def test_drain_call_site_passes_meta_to_append(self):
        """Guard the wiring: the drain must hand the result to slot.append.

        The selection helper above is useless if the drain stops passing its
        result through — which is exactly the regression that went unnoticed.
        Static guard because the drain lives inside _run_chat's finally block.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        assert "_queued_meta = queued_append_meta(" in src, "drain must use the helper"
        assert "meta=_queued_meta or None," in src, "drain must pass meta to slot.append"


class TestQueueItemForDisplay:
    """Slot-detail payloads must ship queue metadata, not just content.

    Live ``queue_push`` delivery already carried the ordered lists, so a queued
    attachment looked correct until the user RELOADED — the detail payload
    projected ``{id, content}`` only, dropping the index space the markers
    resolve against and truncating a path at its first space.
    """

    def test_meta_is_carried_through(self):
        from kiro_crew.dashboard.chat_utils import queue_item_for_display

        item = {"id": "q1", "content": "review [attached_dir 1] /repo/my docs",
                "kind": "", "meta": {"dirs": ["/repo/my docs"]}}
        out = queue_item_for_display(item)
        assert out["id"] == "q1"
        assert out["meta"] == {"dirs": ["/repo/my docs"]}

    def test_meta_key_omitted_when_absent(self):
        """Payload shape is unchanged for entries with no attachments."""
        from kiro_crew.dashboard.chat_utils import queue_item_for_display

        out = queue_item_for_display({"id": "q1", "content": "plain", "kind": ""})
        assert "meta" not in out

    def test_empty_meta_is_not_emitted(self):
        from kiro_crew.dashboard.chat_utils import queue_item_for_display

        out = queue_item_for_display({"id": "q1", "content": "plain", "kind": "", "meta": {}})
        assert "meta" not in out

    def test_content_is_redacted(self):
        """The display projection still redacts, as before."""
        from kiro_crew.dashboard.chat_utils import queue_item_for_display

        out = queue_item_for_display(
            {"id": "q1", "content": "token ghp_0123456789abcdef0123456789abcdef0123", "kind": ""}
        )
        assert "ghp_0123456789abcdef0123456789abcdef0123" not in out["content"]

    def test_detail_call_sites_use_the_helper(self):
        """Guard the wiring: all three slot-detail payloads must project via the
        helper, or one of them silently regresses to dropping meta."""
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers)
        assert src.count("queue_item_for_display(q) for q in") == 3, (
            "every slot-detail queue projection must use queue_item_for_display"
        )
        assert '{"id": q["id"], "content": _redact_for_display(q["content"])}' not in src, (
            "a slot-detail payload still projects the meta-dropping dict literal"
        )


class TestQueuePushMeta:
    """Both ``queue_push`` branches must broadcast the attachment lists.

    ``content`` carries ``[attached_dir N] path`` markers whose N indexes
    ``meta.dirs``. A ``queue_push`` without the lists leaves an open client on
    its whitespace-bounded fallback scan, which truncates any path containing a
    space — the queued card showed ``/repo/my`` until the user reloaded. There
    are two independent emit sites (mid-turn queue, and idle-with-subagents
    hold), so each needs its own assertion; the previous tests asserted only
    that the message was queued.
    """

    @pytest.mark.asyncio
    async def test_running_slot_queue_push_carries_meta(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = None  # no steer target -> falls through to the queue

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": "look at [attached_dir 1] /repo/my docs",
                    "meta": {"dirs": ["/repo/my docs"]},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        pushes = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"]
        assert len(pushes) == 1, "expected exactly one queue_push"
        assert pushes[0].get("meta") == {"dirs": ["/repo/my docs"]}, (
            "queue_push dropped the attachment lists the marker indexes into"
        )
        assert slot._queue[0]["meta"] == {"dirs": ["/repo/my docs"]}

    @pytest.mark.asyncio
    async def test_subagent_hold_queue_push_carries_meta(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")  # idle: no task in flight
        # Idle slot + running sub-agents takes the second queue branch.
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = ["agent-1"]

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": "also check [attached_file 1] /repo/my notes.txt",
                    "meta": {"files": ["/repo/my notes.txt"]},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        pushes = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"]
        assert len(pushes) == 1, "expected exactly one queue_push"
        assert pushes[0].get("meta") == {"files": ["/repo/my notes.txt"]}, (
            "subagent-hold queue_push dropped the attachment lists"
        )
        assert slot._queue[0]["meta"] == {"files": ["/repo/my notes.txt"]}

    @pytest.mark.asyncio
    async def test_queue_push_omits_meta_key_when_no_attachments(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """Shape is unchanged for plain messages — no empty ``meta`` key."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = None

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"slot": "test", "message": "plain"})
            assert resp.status == 200

        pushes = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"]
        assert len(pushes) == 1
        assert "meta" not in pushes[0]


class TestSteerMetaCleanup:
    """Parked steer metadata must not survive its steer.

    ``_pending_steer_meta`` is a side table keyed by message TEXT. If an entry
    outlives its steer, a LATER steer with identical text inherits the stale
    attachment lists — the new message would render or resolve against paths the
    user never attached to it. These branches were verified only by hand-revert,
    so a regression would have left the suite green.
    """

    def test_settle_drops_meta_for_consumed_steers(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        msg = "review [attached_dir 1] /repo/my docs"
        slot._pending_steers = [msg]
        slot._pending_steer_meta = {msg: [{"dirs": ["/repo/my docs"]}]}

        # kiro-cli's echo wraps each consumed steer in <user_message> blocks;
        # settling parses those, so a bare string settles nothing.
        _settle_consumed_steers(slot, f"<user_message>\n{msg}\n</user_message>")

        assert slot._pending_steers == [], "the consumed steer should have settled"
        assert msg not in slot._pending_steer_meta, (
            "metadata for a consumed steer must not outlive it — a later identical "
            "steer would inherit these attachment lists"
        )

    def test_identical_steers_keep_their_own_attachments(self, tmp_path, monkeypatch):
        """Two identical steer texts with DIFFERENT attachments must not merge.

        The side table is keyed by message text, so one dict per key let the second
        registration overwrite the first — both were then requeued with the last
        steer's metadata, pointing one card at the wrong folder. Occurrences are
        stored as a list and consumed in order.
        """
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        msg = "check this"
        slot._pending_steers = [msg, msg]
        slot._pending_steer_meta = {msg: [{"dirs": ["/repo/a"]}, {"dirs": ["/repo/b"]}]}

        _requeue_unconsumed_steers(state, slot)

        metas = [item.get("meta") for item in slot._queue]
        assert {"dirs": ["/repo/a"]} in metas, "first occurrence lost its own attachment"
        assert {"dirs": ["/repo/b"]} in metas, "second occurrence lost its own attachment"
        assert metas[0] != metas[1], "both requeued cards got the same metadata"

    def test_settle_trims_to_the_number_still_pending(self, tmp_path, monkeypatch):
        """One of two identical steers settling leaves exactly one parked entry."""
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        msg = "same text"
        slot._pending_steers = [msg, msg]
        slot._pending_steer_meta = {msg: [{"dirs": ["/repo/a"]}, {"dirs": ["/repo/b"]}]}

        _settle_consumed_steers(slot, f"<user_message>\n{msg}\n</user_message>")

        assert slot._pending_steers == [msg]
        assert len(slot._pending_steer_meta[msg]) == 1

    def test_settle_keeps_meta_for_a_still_pending_steer(self, tmp_path, monkeypatch):
        """Only settled entries are dropped; a steer still in flight keeps its meta."""
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        consumed = "first [attached_dir 1] /repo/a"
        pending = "second [attached_dir 1] /repo/b"
        slot._pending_steers = [consumed, pending]
        slot._pending_steer_meta = {
            consumed: [{"dirs": ["/repo/a"]}],
            pending: [{"dirs": ["/repo/b"]}],
        }

        _settle_consumed_steers(slot, f"<user_message>\n{consumed}\n</user_message>")

        assert slot._pending_steers == [pending]
        assert consumed not in slot._pending_steer_meta
        assert slot._pending_steer_meta[pending] == [{"dirs": ["/repo/b"]}], (
            "an unconsumed steer lost its parked metadata"
        )

    def test_force_stop_clears_parked_meta(self):
        """A hard kill discards steers, so their metadata must go too.

        The escalation lives inline in the stop endpoint's `soft_pending` branch,
        so this guards the pairing statically: the two clears must stay adjacent.
        Without the second one a hard kill leaves metadata parked for text a later
        identical steer could match.
        """
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers)
        assert "slot._pending_steers.clear()\n        slot._pending_steer_meta.clear()" in src, (
            "force-stop must clear the steer metadata side table alongside "
            "_pending_steers — brittle by design: if these lines move, UPDATE "
            "this guard, do not delete it"
        )


class TestUnconsumedSteerKeepsMeta:
    """A steer the turn never consumed is degraded into a queue card.

    That card must keep the ordered attachment lists, or the requeued message
    keeps its markers and loses the paths — truncating at the first space when it
    later drains.
    """

    def test_requeued_steer_carries_meta_onto_the_queue_card(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        msg = "review [attached_dir 1] /repo/my docs"
        slot._pending_steers = [msg]
        slot._pending_steer_meta = {msg: [{"dirs": ["/repo/my docs"]}]}

        _requeue_unconsumed_steers(state, slot)

        assert slot._queue, "the unconsumed steer was not requeued"
        assert slot._queue[0]["meta"]["dirs"] == ["/repo/my docs"], (
            "requeued steer card lost meta.dirs"
        )
        assert not slot._pending_steer_meta, "parked metadata must be drained"

    def test_requeue_without_meta_is_unaffected(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot._pending_steers = ["plain steer"]

        _requeue_unconsumed_steers(state, slot)

        assert slot._queue[0]["content"] == "plain steer"
        assert "meta" not in slot._queue[0], "no attachments means no meta key"

    @pytest.mark.asyncio
    async def test_steer_handler_parks_meta_before_the_await(self, tmp_path, monkeypatch, _patch_sel):
        """Registration happens before the steer RPC's await, so the metadata
        must be parked there too — a turn dying mid-write still needs it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers)
        register = src.index("slot._pending_steers.append(message)")
        park = src.index("slot._pending_steer_meta.setdefault(message, []).append(")
        await_at = src.index("await _client.steer(message)")
        assert register < await_at, "registration must precede the await"
        assert park < await_at, "metadata must be parked before the await"
