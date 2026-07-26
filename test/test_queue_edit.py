"""Tests for queue edit feature.

Covers:
- _ChatSlot.queue_edit_by_id helper
- _edit_queued_by_id messages helper
- PATCH /api/chat/slots/{slot}/queue/{queue_id} endpoint
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_queue_edit
from kiro_crew.dashboard.chat_utils import _edit_queued_by_id
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# ── Unit tests: _ChatSlot.queue_edit_by_id ──


class TestQueueEditHelper:
    def test_edit_by_id_found(self):
        slot = _ChatSlot("s1")
        slot.queue_append("keep")
        qid = slot.queue_append("old text")
        slot.queue_append("also keep")
        assert slot.queue_edit_by_id(qid, "new text") == "new text"
        assert [q["content"] for q in slot._queue] == ["keep", "new text", "also keep"]

    def test_edit_by_id_preserves_order_and_id(self):
        slot = _ChatSlot("s1")
        qid = slot.queue_append("old")
        slot.queue_edit_by_id(qid, "new")
        assert slot._queue[0]["id"] == qid
        assert slot._queue[0]["content"] == "new"

    def test_edit_by_id_not_found(self):
        slot = _ChatSlot("s1")
        slot.queue_append("msg")
        assert slot.queue_edit_by_id("nonexistent", "x") is None
        assert slot._queue[0]["content"] == "msg"

    def test_edit_by_id_empty_queue(self):
        slot = _ChatSlot("s1")
        assert slot.queue_edit_by_id("anything", "x") is None

    def test_edit_drops_attachment_meta(self):
        """An edit discards the item's attachment metadata.

        ``content`` (the ``[attached_file N]`` / ``[attached_dir N]`` markers) and
        ``meta`` (the ordered path lists those markers index into) are generated
        together at send time. An edit rewrites only ``content``, so keeping the
        old ``meta`` desynchronizes the pair: replacing the auto-selected text
        removes the marker, so the model never receives the attachment, while the
        surviving metadata still renders an attachment card in history — an
        attachment that was never sent.
        """
        slot = _ChatSlot("s1")
        qid = slot.queue_append(
            "look at [attached_dir 1] /repo/my docs",
            meta={"dirs": ["/repo/my docs"]},
        )
        assert slot.queue_edit_by_id(qid, "never mind, just say hi") == "never mind, just say hi"
        item = slot._queue[0]
        assert item["content"] == "never mind, just say hi"
        assert "meta" not in item, "stale attachment metadata survived the edit"

    def test_edit_strips_markers_the_dropped_meta_backed(self):
        """An edit that KEEPS the marker text must still lose the marker.

        The sibling test edits the marker away, so dropping ``meta`` is enough.
        When the user edits around the marker instead — fixing a typo, or the
        client echoing the served content back — the marker survived into a
        message with no ``meta``. Two failures followed: the runner resolves
        markers from the text, so the attachment the user thought they dropped
        was still sent; and with the index space gone a path containing a space
        rendered truncated at the first space.
        """
        slot = _ChatSlot("s1")
        qid = slot.queue_append(
            "look at [attached_dir 1] /repo/my docs",
            meta={"dirs": ["/repo/my docs"]},
        )
        assert slot.queue_edit_by_id(qid, "look at [attached_dir 1] /repo/my docs please") == "look at please"
        item = slot._queue[0]
        assert "meta" not in item
        assert "attached_dir" not in item["content"], "marker outlived the metadata backing it"
        assert "/repo/my docs" not in item["content"]
        assert item["content"] == "look at please"

    def test_edit_keeps_markers_it_does_not_own(self):
        """Only the indices present in the dropped meta are stripped."""
        slot = _ChatSlot("s1")
        qid = slot.queue_append("a [attached_dir 1] /d", meta={"dirs": ["/d"]})
        # Index 2 was never in this item's meta, so it is not ours to remove.
        assert slot.queue_edit_by_id(qid, "a [attached_dir 1] /d b [attached_dir 2] /other") == "a b [attached_dir 2] /other"
        assert slot._queue[0]["content"] == "a b [attached_dir 2] /other"

    def test_attachment_only_edit_is_rejected(self):
        """An edit that strips to nothing must not be stored.

        The endpoint validates non-empty BEFORE stripping, so "[attached_dir 1]
        /path" passed validation and then stripped to "". The queue kept an empty
        entry, which drains as a turn carrying no request at all — the user's
        prompt silently vanishes. Returns "" (not None) so the caller can tell
        this apart from an unknown id and answer 400 rather than 404.
        """
        slot = _ChatSlot("s1")
        qid = slot.queue_append(
            "[attached_dir 1] /repo/my docs", meta={"dirs": ["/repo/my docs"]}
        )
        assert slot.queue_edit_by_id(qid, "[attached_dir 1] /repo/my docs") == ""
        # Queue is left untouched, not emptied.
        assert slot._queue[0]["content"] == "[attached_dir 1] /repo/my docs"
        assert slot._queue[0]["meta"] == {"dirs": ["/repo/my docs"]}

    def test_edit_survives_non_list_meta(self):
        """Untrusted meta shapes must not break the edit.

        `meta` originates in client JSON, so `dirs` can be any type. A bare string
        would iterate as characters; a dict/int would raise. Either way the edit
        must still succeed and simply not strip anything.
        """
        for bad in ("not-a-list", 42, {"0": "x"}, None):
            slot = _ChatSlot("s1")
            qid = slot.queue_append("hello there", meta={"dirs": bad})
            assert slot.queue_edit_by_id(qid, "hello there") == "hello there"
            assert "meta" not in slot._queue[0]

    def test_edit_ignores_non_string_path_members(self):
        slot = _ChatSlot("s1")
        qid = slot.queue_append("a [attached_dir 2] /d", meta={"dirs": [None, "/d"]})
        assert slot.queue_edit_by_id(qid, "a [attached_dir 2] /d") == "a"

    def test_edit_does_not_strip_a_longer_path_sharing_the_prefix(self):
        """The marker must match a whole path, not a prefix of a longer one.

        With meta owning `/d`, an edit mentioning `[attached_dir 1] /dossier`
        stripped the `/d` and left `ossier` — silent message corruption.
        """
        slot = _ChatSlot("s1")
        qid = slot.queue_append("seed", meta={"dirs": ["/d"]})
        assert (
            slot.queue_edit_by_id(qid, "see [attached_dir 1] /dossier now")
            == "see [attached_dir 1] /dossier now"
        )

    def test_edit_preserves_indentation_outside_the_marker(self):
        """Stripping must not reformat text away from the marker.

        A global ``[ \\t]{2,}`` collapse plus per-line strip rewrote the whole
        message: a pasted code snippet lost its indentation and aligned columns
        collapsed to single spaces. Only whitespace hugging the removed marker
        may change.
        """
        slot = _ChatSlot("s1")
        qid = slot.queue_append("seed", meta={"dirs": ["/d"]})
        code = "def f():\n    x=1\n    [attached_dir 1] /d\n    y=2"
        assert slot.queue_edit_by_id(qid, code) == "def f():\n    x=1\n    y=2"

        slot2 = _ChatSlot("s2")
        qid2 = slot2.queue_append("seed", meta={"dirs": ["/d"]})
        assert (
            slot2.queue_edit_by_id(qid2, "col1    col2 [attached_dir 1] /d") == "col1    col2"
        )

    def test_edit_without_meta_leaves_no_meta_key(self):
        slot = _ChatSlot("s1")
        qid = slot.queue_append("plain prompt")
        assert slot.queue_edit_by_id(qid, "edited prompt") == "edited prompt"
        assert "meta" not in slot._queue[0]

    def test_failed_edit_leaves_meta_intact(self):
        """A non-matching edit must not strip an untouched item's metadata."""
        slot = _ChatSlot("s1")
        slot.queue_append("x", meta={"dirs": ["/a"]})
        assert slot.queue_edit_by_id("nope", "y") is None
        assert slot._queue[0]["meta"] == {"dirs": ["/a"]}

    def test_edit_by_id_duplicate_content(self):
        """Two items with same content — only the matching ID is edited."""
        slot = _ChatSlot("s1")
        id1 = slot.queue_append("same")
        id2 = slot.queue_append("same")
        slot.queue_edit_by_id(id2, "changed")
        assert slot._queue[0] == {"id": id1, "content": "same", "kind": ""}
        assert slot._queue[1] == {"id": id2, "content": "changed", "kind": ""}


class TestEditQueuedMessagesHelper:
    def test_edit_placeholder_content(self):
        messages = [
            {"role": "user", "content": "hi", "cls": "msg msg-u"},
            {"role": "queued", "content": "old", "cls": json.dumps({"queue_id": "q1"})},
        ]
        assert _edit_queued_by_id(messages, "q1", "new") is True
        assert messages[1]["content"] == "new"

    def test_edit_placeholder_not_found(self):
        messages = [{"role": "queued", "content": "old", "cls": json.dumps({"queue_id": "q1"})}]
        assert _edit_queued_by_id(messages, "q2", "new") is False
        assert messages[0]["content"] == "old"

    def test_edit_ignores_non_queued(self):
        messages = [{"role": "user", "content": "old", "cls": json.dumps({"queue_id": "q1"})}]
        assert _edit_queued_by_id(messages, "q1", "new") is False
        assert messages[0]["content"] == "old"


# ── API tests: PATCH /api/chat/slots/{slot}/queue/{queue_id} ──


def _make_state():
    state = DashboardState.__new__(DashboardState)
    state._slots = {}
    state._ws_clients = []
    state._sse_queues = []
    state._notify_event = MagicMock()
    state._background_tasks = set()
    state._yolo = False
    state._yolo_expires_at = 0.0
    state._restricted_keys = set()
    state.sessions = None
    state.conversation_log = None
    state.channel_manager = None
    return state


def _make_app(state):
    app = web.Application()
    app["state"] = state
    app.router.add_patch(
        "/api/chat/slots/{slot}/queue/{queue_id}",
        api_chat_slot_queue_edit,
    )
    return app


class TestQueueEditEndpoint:
    @pytest.mark.asyncio
    async def test_edit_echoes_stored_text_not_submitted_text(self):
        """Queue, placeholder, broadcast and response must all agree.

        The strip happens server-side, so echoing the SUBMITTED text left every
        client rendering an `[attached_dir N]` marker the queued prompt no longer
        contained — the queue held the stripped text while the UI showed the
        attachment. All four consumers now use the stored text.
        """
        state = _make_state()
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append(
            "look at [attached_dir 1] /repo/my docs", meta={"dirs": ["/repo/my docs"]}
        )
        slot.append("queued", "look at [attached_dir 1] /repo/my docs", json.dumps({"queue_id": qid}))

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}",
                    json={"content": "look at [attached_dir 1] /repo/my docs please"},
                )
                assert resp.status == 200
                data = await resp.json()

        assert "attached_dir" not in data["content"], "response echoed the stripped marker"
        assert data["content"] == "look at please"
        assert slot._queue[0]["content"] == "look at please"
        queued = [m for m in slot.messages if m.get("role") == "queued"]
        assert queued[0]["content"] == "look at please", "placeholder kept the marker"
        edits = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_edit"]
        assert len(edits) == 1
        assert "attached_dir" not in edits[0]["content"], "broadcast kept the marker"

    @pytest.mark.asyncio
    async def test_attachment_only_edit_returns_400(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("[attached_dir 1] /d", meta={"dirs": ["/d"]})

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}", json={"content": "[attached_dir 1] /d"}
                )
                # 400 (bad edit), not 404 (missing item) — the item exists.
                assert resp.status == 400
        assert slot._queue[0]["content"] == "[attached_dir 1] /d"

    @pytest.mark.asyncio
    async def test_edit_updates_queue_and_messages(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("old text")
        slot.append("queued", "old text", json.dumps({"queue_id": qid}))

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}",
                    json={"content": "new text"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert "new text" in data["content"]

        assert slot._queue[0]["content"] == "new text"
        queued = [m for m in slot.messages if m.get("role") == "queued"]
        assert len(queued) == 1
        assert queued[0]["content"] == "new text"

    @pytest.mark.asyncio
    async def test_edit_slot_not_found(self):
        state = _make_state()
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/nope/queue/abc", json={"content": "x"}
                )
                assert resp.status == 404

    @pytest.mark.asyncio
    async def test_edit_queue_id_not_found(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        slot.queue_append("keep")
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-1/queue/wrong", json={"content": "x"}
                )
                assert resp.status == 404
        assert slot._queue[0]["content"] == "keep"

    @pytest.mark.asyncio
    async def test_edit_empty_content_rejected(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("old")
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}", json={"content": "   "}
                )
                assert resp.status == 400
        assert slot._queue[0]["content"] == "old"

    @pytest.mark.asyncio
    async def test_edit_invalid_json(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("old")
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}",
                    data="not json",
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_edit_broadcasts_ws_event(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("old")
        state.broadcast_ws = MagicMock()

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}", json={"content": "new"}
                )

        state.broadcast_ws.assert_any_call(
            "queue_edit",
            {"slot": "chat-1", "queue_id": qid, "content": "new"},
        )

    @pytest.mark.asyncio
    async def test_edit_duplicate_content_targets_correct_item(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        id1 = slot.queue_append("same")
        id2 = slot.queue_append("same")
        slot.append("queued", "same", json.dumps({"queue_id": id1}))
        slot.append("queued", "same", json.dumps({"queue_id": id2}))

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{id2}", json={"content": "edited"}
                )
                assert resp.status == 200

        assert slot._queue[0] == {"id": id1, "content": "same", "kind": ""}
        assert slot._queue[1] == {"id": id2, "content": "edited", "kind": ""}
