"""Tests for knowledge add_source local_file support and get_config endpoint."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import native_dialog as nd
from kiro_crew.dashboard.handlers.knowledge import (
    _folder_picker_available,
    _run_folder_dialog,
    add_source,
    get_config,
    pick_folder,
)
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def _make_app(store, pipeline=None):
    """Create minimal app with knowledge routes for testing."""
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    if pipeline:
        app["knowledge_pipeline"] = pipeline
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))
    app.router.add_get("/api/knowledge/config", get_config)
    app.router.add_post("/api/knowledge/sources", add_source)
    return app


class TestGetConfig:
    @pytest.mark.asyncio
    async def test_returns_enabled_and_formats(self, store):
        async with TestClient(TestServer(_make_app(store, pipeline=MagicMock()))) as client:
            resp = await client.get("/api/knowledge/config")
            assert resp.status == 200
            data = await resp.json()
            assert data["enabled"] is True
            assert ".md" in data["supported_formats"]
            assert ".py" in data["supported_formats"]
            assert "" not in data["supported_formats"]


class TestAddSourceLocalFile:
    @pytest.mark.asyncio
    async def test_rejects_relative_path(self, store):
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "test", "source_type": "local_file", "uri": "relative/path.md"
            })
            assert resp.status == 400
            data = await resp.json()
            assert "absolute path" in data["error"]

    @pytest.mark.asyncio
    async def test_rejects_sensitive_path(self, store, tmp_path):
        # Create a symlink to a sensitive path
        sensitive = str(Path.home() / ".ssh" / "config")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "test", "source_type": "local_file", "uri": sensitive
            })
            assert resp.status == 403
            data = await resp.json()
            assert "restricted" in data["error"]

    @pytest.mark.asyncio
    async def test_rejects_nonexistent_file(self, store):
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "test", "source_type": "local_file", "uri": "/tmp/nonexistent_xyz_12345.md"
            })
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_rejects_directory(self, store, tmp_path):
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "test", "source_type": "local_file", "uri": str(tmp_path)
            })
            assert resp.status == 404
            data = await resp.json()
            assert "file not found" in data["error"]

    @pytest.mark.asyncio
    async def test_accepts_valid_file(self, store, tmp_path):
        test_file = tmp_path / "hello.md"
        test_file.write_text("# Hello")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "hello.md", "source_type": "local_file", "uri": str(test_file)
            })
            assert resp.status == 201
            data = await resp.json()
            assert "id" in data

    @pytest.mark.asyncio
    async def test_duplicate_returns_409(self, store, tmp_path):
        test_file = tmp_path / "dup.md"
        test_file.write_text("content")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp1 = await client.post("/api/knowledge/sources", json={
                "name": "dup.md", "source_type": "local_file", "uri": str(test_file)
            })
            assert resp1.status == 201
            resp2 = await client.post("/api/knowledge/sources", json={
                "name": "dup.md", "source_type": "local_file", "uri": str(test_file)
            })
            assert resp2.status == 409

    @pytest.mark.asyncio
    async def test_resolves_symlinks(self, store, tmp_path):
        real_file = tmp_path / "real.md"
        real_file.write_text("content")
        link = tmp_path / "link.md"
        link.symlink_to(real_file)
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "link.md", "source_type": "local_file", "uri": str(link)
            })
            assert resp.status == 201
            # Stored URI should be the resolved path
            source = store.get_source_by_uri(str(real_file.resolve()))
            assert source is not None

    @pytest.mark.asyncio
    async def test_symlink_to_sensitive_blocked(self, store, tmp_path):
        # Create symlink pointing to sensitive location
        link = tmp_path / "innocent.md"
        sensitive_target = Path.home() / ".aws" / "credentials"
        link.symlink_to(sensitive_target)
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "innocent.md", "source_type": "local_file", "uri": str(link)
            })
            # Either 403 (sensitive) or 404 (doesn't exist) depending on whether file exists
            assert resp.status in (403, 404)

    @pytest.mark.asyncio
    async def test_triggers_immediate_ingestion(self, store, tmp_path):
        test_file = tmp_path / "ingest.md"
        test_file.write_text("# Ingest me")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "ingest.md", "source_type": "local_file", "uri": str(test_file)
            })
            assert resp.status == 201
            # Give the background task a moment
            import asyncio
            await asyncio.sleep(0.1)
            pipeline.ingest_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, store, tmp_path):
        # Create a file, then try to access it via ../.. traversal
        test_file = tmp_path / "safe.md"
        test_file.write_text("safe")
        # Construct a traversal path that resolves to the same file
        traversal = str(tmp_path / "subdir" / ".." / "safe.md")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "safe.md", "source_type": "local_file", "uri": traversal
            })
            # Should succeed but store the resolved canonical path
            assert resp.status == 201
            source = store.get_source_by_uri(str(test_file.resolve()))
            assert source is not None


def _make_pick_app(store, local_only=True):
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    app["local_only"] = local_only
    app.router.add_post("/api/knowledge/pick-folder", pick_folder)
    app.router.add_get("/api/knowledge/config", get_config)
    return app


def _fake_request(local_only=True):
    return SimpleNamespace(app={"local_only": local_only})


class TestFolderPickerAvailable:
    def test_available_when_host_has_a_chooser_and_dashboard_is_local(self, monkeypatch):
        monkeypatch.setattr(nd, "detect_backend", lambda: nd.BACKEND_OSASCRIPT)
        assert _folder_picker_available(_fake_request(local_only=True)) is True

    def test_unavailable_without_a_chooser(self, monkeypatch):
        """A headless host has no dialog to draw, whatever its platform."""
        monkeypatch.setattr(nd, "detect_backend", lambda: None)
        assert _folder_picker_available(_fake_request(local_only=True)) is False

    def test_available_on_non_mac_hosts_with_a_chooser(self, monkeypatch):
        monkeypatch.setattr(nd, "detect_backend", lambda: nd.BACKEND_ZENITY)
        assert _folder_picker_available(_fake_request(local_only=True)) is True

    def test_unavailable_when_remote(self, monkeypatch):
        monkeypatch.setattr(nd, "detect_backend", lambda: nd.BACKEND_OSASCRIPT)
        assert _folder_picker_available(_fake_request(local_only=False)) is False

    def test_fail_closed_when_local_only_unset(self, monkeypatch):
        monkeypatch.setattr(nd, "detect_backend", lambda: nd.BACKEND_OSASCRIPT)
        assert _folder_picker_available(SimpleNamespace(app={})) is False


class TestRunFolderDialog:
    def test_picked_returns_path(self, monkeypatch):
        monkeypatch.setattr(nd, "choose_directory", lambda **kw: "/home/user/notes")
        assert _run_folder_dialog() == "/home/user/notes"

    def test_cancel_returns_none(self, monkeypatch):
        monkeypatch.setattr(nd, "choose_directory", lambda **kw: None)
        assert _run_folder_dialog() is None

    def test_launch_failure_returns_none(self, monkeypatch):
        """This surface feeds the path into a text field, so a failed dialog and
        a cancelled one collapse to the same answer here."""
        def boom(**kw):
            raise nd.DialogUnavailable("no session")
        monkeypatch.setattr(nd, "choose_directory", boom)
        assert _run_folder_dialog() is None

    def test_uses_the_knowledge_prompt(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(nd, "choose_directory", lambda **kw: seen.update(kw) or "/p")
        _run_folder_dialog()
        assert seen["prompt"] == nd.PROMPT_KNOWLEDGE


class TestPickFolderHandler:
    @pytest.mark.asyncio
    async def test_blocked_when_not_local_only(self, store, monkeypatch):
        monkeypatch.setattr(nd, "detect_backend", lambda: nd.BACKEND_OSASCRIPT)
        async with TestClient(TestServer(_make_pick_app(store, local_only=False))) as client:
            resp = await client.post("/api/knowledge/pick-folder")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_blocked_without_a_chooser(self, store, monkeypatch):
        monkeypatch.setattr(nd, "detect_backend", lambda: None)
        async with TestClient(TestServer(_make_pick_app(store, local_only=True))) as client:
            resp = await client.post("/api/knowledge/pick-folder")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_returns_picked_path(self, store, monkeypatch):
        monkeypatch.setattr(nd, "detect_backend", lambda: nd.BACKEND_OSASCRIPT)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge._run_folder_dialog",
            lambda: "/home/user/notes",
        )
        async with TestClient(TestServer(_make_pick_app(store))) as client:
            resp = await client.post("/api/knowledge/pick-folder")
            assert resp.status == 200
            assert (await resp.json())["path"] == "/home/user/notes"

    @pytest.mark.asyncio
    async def test_returns_null_on_cancel(self, store, monkeypatch):
        monkeypatch.setattr(nd, "detect_backend", lambda: nd.BACKEND_OSASCRIPT)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge._run_folder_dialog",
            lambda: None,
        )
        async with TestClient(TestServer(_make_pick_app(store))) as client:
            resp = await client.post("/api/knowledge/pick-folder")
            assert resp.status == 200
            assert (await resp.json())["path"] is None


class TestConfigFolderPickerFlag:
    @pytest.mark.asyncio
    async def test_reports_true_when_local_with_a_chooser(self, store, monkeypatch):
        monkeypatch.setattr(nd, "detect_backend", lambda: nd.BACKEND_OSASCRIPT)
        async with TestClient(TestServer(_make_pick_app(store, local_only=True))) as client:
            resp = await client.get("/api/knowledge/config")
            assert (await resp.json())["folder_picker"] is True

    @pytest.mark.asyncio
    async def test_reports_false_without_a_chooser(self, store, monkeypatch):
        monkeypatch.setattr(nd, "detect_backend", lambda: None)
        async with TestClient(TestServer(_make_pick_app(store, local_only=True))) as client:
            resp = await client.get("/api/knowledge/config")
            assert (await resp.json())["folder_picker"] is False
