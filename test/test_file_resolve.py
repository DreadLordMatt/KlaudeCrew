"""Tests for the file-rename resolver (dashboard/file_resolve.py) and the
``/api/file-resolve`` endpoint (dashboard/handlers/files.py)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import file_resolve
from kiro_crew.dashboard.file_resolve import (
    latest_snapshot,
    resolve_path,
    resolve_recorded,
    snapshot_from_change,
)
from kiro_crew.dashboard.handlers.files import api_file_resolve

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

# A block of text long enough that difflib quick_ratio is meaningful.
_DOC = "\n".join(f"line {i}: the quick brown fox jumps over the lazy dog" for i in range(200))


class _FakeLog:
    """Minimal conversation-log stub exposing list_sessions / read_messages."""

    def __init__(self, sessions: list[dict], messages: dict[str, list[dict]]):
        self._sessions = sessions
        self._messages = messages

    def list_sessions(self) -> list[dict]:
        return self._sessions

    def read_messages(self, key: str) -> list[dict]:
        return self._messages.get(key, [])


def _log_with_change(path: str, before: str = "", after: str = "", *, modified: float = 100.0) -> _FakeLog:
    return _FakeLog(
        sessions=[{"key": "s1", "modified": modified, "title": "sess"}],
        messages={
            "s1": [
                {
                    "role": "assistant",
                    "ts": "2026-07-29T00:00:00",
                    "meta": {"file_changes": [{"path": path, "before": before, "after": after}]},
                }
            ]
        },
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    file_resolve._cache.clear()
    yield
    file_resolve._cache.clear()


# ── snapshot_from_change / latest_snapshot ──────────────────────────────────


def test_snapshot_prefers_after():
    assert snapshot_from_change({"before": "b", "after": "a"}) == "a"


def test_snapshot_falls_back_to_before_when_after_empty():
    # Empty ``after`` is the signature of a file renamed away mid-turn.
    assert snapshot_from_change({"before": "b", "after": ""}) == "b"


def test_snapshot_empty_when_neither():
    assert snapshot_from_change({}) == ""


def test_latest_snapshot_none_log():
    assert latest_snapshot(None, "/x") is None


def test_latest_snapshot_finds_recorded(tmp_path):
    p = str(tmp_path / "doc.md")
    log = _log_with_change(p, before="B", after="")
    assert latest_snapshot(log, p) == "B"


# ── resolve_path: exact hit ─────────────────────────────────────────────────


def test_resolve_exact_hit(tmp_path):
    f = tmp_path / "present.md"
    f.write_text("hello")
    canonical = os.path.realpath(str(f))
    res = resolve_path(str(f), canonical, None)
    assert res["exists"] is True
    assert res["method"] == "exact"
    assert res["resolved_path"] == str(f)
    assert res["confidence"] is None


# ── resolve_path: content-match (the case that matters) ─────────────────────


def test_resolve_content_match(tmp_path):
    # New file carries (nearly) the recorded content; old path is gone.
    new = tmp_path / "main-checklist.md"
    new.write_text(_DOC)
    old = tmp_path / "master-checklist.md"  # never created
    canonical = os.path.realpath(str(old))
    log = _log_with_change(str(old), before=_DOC, after="")

    res = resolve_path(str(old), canonical, log)
    assert res["exists"] is False
    assert res["method"] == "content-match"
    assert res["resolved_path"] == os.path.realpath(str(new))
    assert res["confidence"] is not None and res["confidence"] >= 0.6


def test_resolve_content_match_slightly_edited(tmp_path):
    # Even with a small edit, quick_ratio stays well above the 0.6 gate.
    new = tmp_path / "renamed.md"
    new.write_text(_DOC + "\nan extra trailing line")
    old = tmp_path / "original.md"
    canonical = os.path.realpath(str(old))
    log = _log_with_change(str(old), before=_DOC, after="")

    res = resolve_path(str(old), canonical, log)
    assert res["method"] == "content-match"
    assert res["resolved_path"] == os.path.realpath(str(new))


# ── resolve_path: no match ──────────────────────────────────────────────────


def test_resolve_no_match_dissimilar(tmp_path):
    (tmp_path / "unrelated.md").write_text("totally different tiny content")
    old = tmp_path / "gone.md"
    canonical = os.path.realpath(str(old))
    log = _log_with_change(str(old), before=_DOC, after="")

    res = resolve_path(str(old), canonical, log)
    assert res["exists"] is False
    assert res["resolved_path"] is None
    assert res["method"] is None
    assert res["confidence"] is None


def test_resolve_no_snapshot_no_git(tmp_path):
    old = tmp_path / "gone.md"
    canonical = os.path.realpath(str(old))
    res = resolve_path(str(old), canonical, None)
    assert res == {
        "path": str(old),
        "exists": False,
        "resolved_path": None,
        "method": None,
        "confidence": None,
    }


def test_resolve_ignores_different_extension(tmp_path):
    # A same-content file with a DIFFERENT extension must not be matched.
    (tmp_path / "renamed.txt").write_text(_DOC)
    old = tmp_path / "gone.md"
    canonical = os.path.realpath(str(old))
    log = _log_with_change(str(old), before=_DOC, after="")
    res = resolve_path(str(old), canonical, log)
    assert res["resolved_path"] is None


# ── resolve_path: git-rename ────────────────────────────────────────────────


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


@requires_git
def test_resolve_git_rename(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "T")
    old = tmp_path / "master-checklist.md"
    old.write_text(_DOC)
    _git(tmp_path, "add", "master-checklist.md")
    _git(tmp_path, "commit", "-m", "add")
    _git(tmp_path, "mv", "master-checklist.md", "main-checklist.md")
    _git(tmp_path, "commit", "-m", "rename")

    canonical = os.path.realpath(str(old))
    # No conversation log => git-rename is the only path that can fire.
    res = resolve_path(str(old), canonical, None)
    assert res["method"] == "git-rename"
    assert res["resolved_path"] == os.path.realpath(str(tmp_path / "main-checklist.md"))
    assert res["confidence"] is None


@requires_git
def test_git_rename_preferred_over_content(tmp_path):
    # When a git rename record exists, it wins before content matching runs.
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "T")
    old = tmp_path / "a.md"
    old.write_text(_DOC)
    _git(tmp_path, "add", "a.md")
    _git(tmp_path, "commit", "-m", "add")
    _git(tmp_path, "mv", "a.md", "b.md")
    _git(tmp_path, "commit", "-m", "rename")

    canonical = os.path.realpath(str(old))
    log = _log_with_change(str(old), before=_DOC, after="")
    res = resolve_path(str(old), canonical, log)
    assert res["method"] == "git-rename"


# ── resolver safety: sensitive candidate skipped ────────────────────────────


def test_content_match_skips_sensitive_candidate(tmp_path):
    new = tmp_path / "main.md"
    new.write_text(_DOC)
    old = tmp_path / "old.md"
    canonical = os.path.realpath(str(old))
    log = _log_with_change(str(old), before=_DOC, after="")

    real_is_sensitive = file_resolve.is_sensitive_path

    def fake_sensitive(p, base_dir=None):
        if os.path.realpath(p) == os.path.realpath(str(new)):
            return True
        return real_is_sensitive(p, base_dir)

    with patch.object(file_resolve, "is_sensitive_path", side_effect=fake_sensitive):
        res = resolve_path(str(old), canonical, log)
    assert res["resolved_path"] is None


# ── caching ─────────────────────────────────────────────────────────────────


def test_resolve_memoized(tmp_path):
    f = tmp_path / "c.md"
    f.write_text("x")
    canonical = os.path.realpath(str(f))
    resolve_path(str(f), canonical, None)
    assert canonical in file_resolve._cache


# ── resolve_recorded ────────────────────────────────────────────────────────


def test_resolve_recorded_relative_is_unresolvable():
    assert resolve_recorded("relative/path.md", "snap") == (None, None, None)


def test_resolve_recorded_exact(tmp_path):
    f = tmp_path / "d.md"
    f.write_text("y")
    assert resolve_recorded(str(f), "") == (str(f), "exact", None)


def test_resolve_recorded_content_match(tmp_path):
    new = tmp_path / "new.md"
    new.write_text(_DOC)
    old = str(tmp_path / "old.md")
    resolved, method, conf = resolve_recorded(old, _DOC)
    assert method == "content-match"
    assert resolved == os.path.realpath(str(new))
    assert conf >= 0.6


# ── endpoint: guard + wiring ─────────────────────────────────────────────────


def _mock_sel():
    sel = MagicMock()
    sel.log_tool_invocation = MagicMock()
    return sel


def _req(path: str, state: Any = None):
    url = f"/api/file-resolve?path={path}" if path else "/api/file-resolve"
    app = web.Application()
    if state is not None:
        app["state"] = state
    return make_mocked_request("GET", url, app=app)


@pytest.mark.asyncio
async def test_endpoint_invalid_input_rejected():
    # A path that fails FILE_READ_SCHEMA (does not start with ~ or /).
    req = _req("relative-no-slash")
    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_resolve(req)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid input"


@pytest.mark.asyncio
async def test_endpoint_sensitive_path_refused():
    req = _req("/Users/someone/.ssh/id_rsa")
    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()), patch(
        "kiro_crew.dashboard.handlers.files._validate_dashboard_path", return_value=None
    ):
        resp = await api_file_resolve(req)
    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "invalid or forbidden path"


@pytest.mark.asyncio
async def test_endpoint_exact_hit(tmp_path):
    f = tmp_path / "present.md"
    f.write_text("hello")
    state = MagicMock()
    state.conversation_log = None
    req = _req(str(f), state=state)
    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_resolve(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["exists"] is True
    assert body["method"] == "exact"


@pytest.mark.asyncio
async def test_endpoint_content_match(tmp_path):
    new = tmp_path / "main-checklist.md"
    new.write_text(_DOC)
    old = tmp_path / "master-checklist.md"
    state = MagicMock()
    state.conversation_log = _log_with_change(str(old), before=_DOC, after="")
    req = _req(str(old), state=state)
    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=_mock_sel()):
        resp = await api_file_resolve(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["method"] == "content-match"
    assert body["resolved_path"] == os.path.realpath(str(new))
    assert body["confidence"] >= 0.6
