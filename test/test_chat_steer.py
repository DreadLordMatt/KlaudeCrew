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
