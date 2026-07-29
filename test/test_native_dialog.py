"""Tests for the host-side native folder dialog (module + /api/native-dir-dialog)."""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import native_dialog as nd
from kiro_crew.dashboard.handlers import api_native_dir_dialog, api_native_dir_dialog_probe
from kiro_crew.dashboard.handlers import files as files_handlers


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/native-dir-dialog", api_native_dir_dialog_probe)
    app.router.add_post("/api/native-dir-dialog", api_native_dir_dialog)
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


@pytest.fixture(autouse=True)
def _reset_busy():
    files_handlers._NATIVE_DIALOG_STATE["busy"] = False
    yield
    files_handlers._NATIVE_DIALOG_STATE["busy"] = False


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["dialog"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestDetectBackend:
    def test_macos_uses_osascript(self):
        with patch.object(nd.sys, "platform", "darwin"), \
             patch.object(nd.shutil, "which", return_value="/usr/bin/osascript"):
            assert nd.detect_backend() == nd.BACKEND_OSASCRIPT

    def test_macos_without_osascript_is_unavailable(self):
        with patch.object(nd.sys, "platform", "darwin"), \
             patch.object(nd.shutil, "which", return_value=None):
            assert nd.detect_backend() is None

    def test_windows_uses_powershell(self):
        with patch.object(nd.sys, "platform", "win32"), \
             patch.object(nd, "_powershell", return_value="C:\\powershell.exe"):
            assert nd.detect_backend() == nd.BACKEND_POWERSHELL

    def test_linux_without_display_is_unavailable(self):
        """A headless gateway has zenity installed often enough that presence lies."""
        with patch.object(nd.sys, "platform", "linux"), \
             patch.dict(nd.os.environ, {}, clear=True), \
             patch.object(nd.shutil, "which", return_value="/usr/bin/zenity"):
            assert nd.detect_backend() is None

    def test_linux_prefers_zenity_then_kdialog(self):
        with patch.object(nd.sys, "platform", "linux"), \
             patch.dict(nd.os.environ, {"DISPLAY": ":0"}):
            with patch.object(nd.shutil, "which", side_effect=lambda n: "/usr/bin/zenity" if n == "zenity" else None):
                assert nd.detect_backend() == nd.BACKEND_ZENITY
            with patch.object(nd.shutil, "which", side_effect=lambda n: "/usr/bin/kdialog" if n == "kdialog" else None):
                assert nd.detect_backend() == nd.BACKEND_KDIALOG
            with patch.object(nd.shutil, "which", return_value=None):
                assert nd.detect_backend() is None

    def test_wayland_display_counts(self):
        with patch.object(nd.sys, "platform", "linux"), \
             patch.dict(nd.os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=True), \
             patch.object(nd.shutil, "which", side_effect=lambda n: "/z" if n == "zenity" else None):
            assert nd.detect_backend() == nd.BACKEND_ZENITY


def _applescript(argv: list[str]) -> str:
    """The -e lines only — i.e. the program text osascript will execute."""
    return " ".join(a for a, prev in zip(argv, [""] + argv) if prev == "-e")


class TestBuildCommand:
    def test_osascript_passes_data_via_argv_not_program_text(self):
        """The AppleScript body must stay a fixed literal — no interpolation."""
        evil = '"; do shell script "touch /tmp/pwned"'
        argv, env = nd.build_command(nd.BACKEND_OSASCRIPT, evil, evil)
        assert argv[-3:] == ["--", evil, evil]
        assert env == {}
        script = _applescript(argv)
        assert evil not in script
        assert "choose folder" in script

    def test_osascript_program_text_is_identical_for_any_input(self):
        a, _ = nd.build_command(nd.BACKEND_OSASCRIPT, "/one", "Prompt one")
        b, _ = nd.build_command(nd.BACKEND_OSASCRIPT, "/two", 'Prompt "two"')
        assert _applescript(a) == _applescript(b)

    def test_osascript_omits_default_location_when_empty(self):
        argv, _ = nd.build_command(nd.BACKEND_OSASCRIPT, "")
        assert argv[-3:] == ["--", nd.PROMPT_PROJECT, ""]
        assert any("if loc is \"\" then" in a for a in argv)

    def test_osascript_activates_itself_not_system_events(self):
        """System Events is faceless: activating it launches a second process
        before the panel can appear, which shows up as a slow-opening dialog."""
        script = _applescript(nd.build_command(nd.BACKEND_OSASCRIPT, "")[0])
        assert "activate me" in script
        assert "System Events" not in script

    def test_powershell_is_sta_and_uses_env_for_data(self):
        argv, env = nd.build_command(nd.BACKEND_POWERSHELL, "C:\\proj", "Pick one")
        assert "-STA" in argv          # WinForms dialogs need a single-threaded apartment
        assert "-NoProfile" in argv
        assert env == {nd._WIN_PROMPT_ENV: "Pick one", nd._WIN_DEFAULT_ENV: "C:\\proj"}
        joined = " ".join(argv)
        assert "C:\\proj" not in joined
        assert "Pick one" not in joined

    def test_powershell_no_default_env_without_default(self):
        _, env = nd.build_command(nd.BACKEND_POWERSHELL, "")
        assert nd._WIN_DEFAULT_ENV not in env

    def test_prompt_reaches_each_backend_as_data(self):
        argv, _ = nd.build_command(nd.BACKEND_ZENITY, "", nd.PROMPT_KNOWLEDGE)
        assert f"--title={nd.PROMPT_KNOWLEDGE}" in argv
        argv, _ = nd.build_command(nd.BACKEND_KDIALOG, "", nd.PROMPT_KNOWLEDGE)
        assert argv[argv.index("--title") + 1] == nd.PROMPT_KNOWLEDGE

    def test_zenity_directory_selection_with_trailing_separator(self):
        argv, env = nd.build_command(nd.BACKEND_ZENITY, "/home/u/proj")
        assert "--directory" in argv and "--file-selection" in argv
        assert f"--filename=/home/u/proj{nd.os.sep}" in argv
        assert env == {}

    def test_kdialog_gets_existing_directory(self):
        argv, _ = nd.build_command(nd.BACKEND_KDIALOG, "/home/u/proj")
        assert "--getexistingdirectory" in argv
        assert argv[-1] == "/home/u/proj"

    def test_unknown_backend_raises(self):
        with pytest.raises(nd.DialogUnavailable):
            nd.build_command("telepathy")


class TestChooseDirectory:
    def test_returns_selection_without_trailing_slash(self):
        """osascript's `POSIX path of` appends a separator; project paths don't carry one."""
        with patch.object(nd, "detect_backend", return_value=nd.BACKEND_OSASCRIPT), \
             patch.object(nd.subprocess, "run", return_value=_completed(stdout="/home/u/proj/\n")):
            assert nd.choose_directory() == "/home/u/proj"

    def test_root_selection_survives_stripping(self):
        with patch.object(nd, "detect_backend", return_value=nd.BACKEND_OSASCRIPT), \
             patch.object(nd.subprocess, "run", return_value=_completed(stdout="/\n")):
            assert nd.choose_directory() == "/"

    def test_osascript_cancel_is_none_not_error(self):
        with patch.object(nd, "detect_backend", return_value=nd.BACKEND_OSASCRIPT), \
             patch.object(nd.subprocess, "run",
                          return_value=_completed(1, stderr="execution error: User canceled. (-128)")):
            assert nd.choose_directory() is None

    def test_osascript_real_failure_raises(self):
        """A non-cancel AppleScript error must not masquerade as a dismissal."""
        with patch.object(nd, "detect_backend", return_value=nd.BACKEND_OSASCRIPT), \
             patch.object(nd.subprocess, "run",
                          return_value=_completed(1, stderr="execution error: no user session (-1743)")):
            with pytest.raises(nd.DialogUnavailable):
                nd.choose_directory()

    def test_osascript_code_is_matched_as_a_token(self):
        """A different negative code that merely contains 128 is not a cancel."""
        with patch.object(nd, "detect_backend", return_value=nd.BACKEND_OSASCRIPT), \
             patch.object(nd.subprocess, "run",
                          return_value=_completed(1, stderr="execution error: boom (-1281)")):
            with pytest.raises(nd.DialogUnavailable):
                nd.choose_directory()

    def test_zenity_cancel_is_none_other_codes_raise(self):
        with patch.object(nd, "detect_backend", return_value=nd.BACKEND_ZENITY):
            with patch.object(nd.subprocess, "run", return_value=_completed(1)):
                assert nd.choose_directory() is None
            with patch.object(nd.subprocess, "run", return_value=_completed(255, stderr="cannot open display")):
                with pytest.raises(nd.DialogUnavailable):
                    nd.choose_directory()

    def test_powershell_empty_stdout_is_cancel(self):
        with patch.object(nd, "detect_backend", return_value=nd.BACKEND_POWERSHELL), \
             patch.object(nd.subprocess, "run", return_value=_completed(0, stdout="")):
            assert nd.choose_directory() is None

    def test_timeout_raises_unavailable(self):
        with patch.object(nd, "detect_backend", return_value=nd.BACKEND_ZENITY), \
             patch.object(nd.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="zenity", timeout=1)):
            with pytest.raises(nd.DialogUnavailable):
                nd.choose_directory()

    def test_missing_binary_raises_unavailable(self):
        with patch.object(nd, "detect_backend", return_value=nd.BACKEND_ZENITY), \
             patch.object(nd.subprocess, "run", side_effect=OSError("No such file")):
            with pytest.raises(nd.DialogUnavailable):
                nd.choose_directory()

    def test_no_backend_raises_unavailable(self):
        with patch.object(nd, "detect_backend", return_value=None):
            with pytest.raises(nd.DialogUnavailable):
                nd.choose_directory()

    def test_default_path_reaches_the_command(self):
        with patch.object(nd, "detect_backend", return_value=nd.BACKEND_ZENITY), \
             patch.object(nd.subprocess, "run", return_value=_completed(stdout="/picked")) as run:
            nd.choose_directory("/start/here")
            assert any("--filename=/start/here" in a for a in run.call_args.args[0])


class TestNativeDialogEndpoint:
    @pytest.mark.asyncio
    async def test_probe_reports_availability(self, mock_sel):
        with patch.object(nd, "is_available", return_value=True):
            async with TestClient(TestServer(_make_app())) as client:
                assert (await (await client.get("/api/native-dir-dialog")).json()) == {"available": True}
        with patch.object(nd, "is_available", return_value=False):
            async with TestClient(TestServer(_make_app())) as client:
                assert (await (await client.get("/api/native-dir-dialog")).json()) == {"available": False}

    @pytest.mark.asyncio
    async def test_probe_is_false_for_a_forwarded_request(self, mock_sel):
        """A tunnelled browser is on another machine — hide the affordance."""
        with patch.object(nd, "is_available", return_value=True):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/native-dir-dialog", headers={"X-Forwarded-For": "203.0.113.9"})
                assert (await resp.json()) == {"available": False}

    @pytest.mark.asyncio
    async def test_returns_selected_path(self, tmp_path, mock_sel):
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", return_value=str(tmp_path)):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/native-dir-dialog", json={})
                assert resp.status == 200
                assert (await resp.json())["path"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_cancel_is_a_success_response(self, mock_sel):
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", return_value=None):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/native-dir-dialog", json={})
                assert resp.status == 200
                assert (await resp.json()) == {"cancelled": True}

    @pytest.mark.asyncio
    async def test_forwarded_request_is_refused(self, mock_sel):
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", return_value="/tmp") as choose:
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/native-dir-dialog", json={},
                                         headers={"X-Forwarded-For": "203.0.113.9"})
                assert resp.status == 403
                assert (await resp.json())["reason"] == "remote"
                choose.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_host_returns_503(self, mock_sel):
        with patch.object(nd, "is_available", return_value=False):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/native-dir-dialog", json={})
                assert resp.status == 503
                assert (await resp.json())["reason"] == "unavailable"

    @pytest.mark.asyncio
    async def test_dialog_failure_returns_503(self, mock_sel):
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", side_effect=nd.DialogUnavailable("no session")):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/native-dir-dialog", json={})
                assert resp.status == 503
                assert (await resp.json())["reason"] == "failed"
        # The slot must be released even when the dialog blew up.
        assert files_handlers._NATIVE_DIALOG_STATE["busy"] is False

    @pytest.mark.asyncio
    async def test_second_dialog_is_refused_while_one_is_open(self, mock_sel):
        files_handlers._NATIVE_DIALOG_STATE["busy"] = True
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", return_value="/tmp") as choose:
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/native-dir-dialog", json={})
                assert resp.status == 409
                assert (await resp.json())["reason"] == "busy"
                choose.assert_not_called()

    @pytest.mark.asyncio
    async def test_sensitive_selection_is_denied(self, tmp_path, mock_sel):
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", return_value=str(tmp_path)), \
             patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/native-dir-dialog", json={})
                assert resp.status == 403
                assert (await resp.json())["reason"] == "sensitive"

    @pytest.mark.asyncio
    async def test_nonexistent_selection_is_rejected(self, mock_sel):
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", return_value="/nonexistent_xyz_123"):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/native-dir-dialog", json={})
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_starting_directory_comes_from_recent_projects_not_the_request(
        self, tmp_path, mock_sel, monkeypatch,
    ):
        """No request byte may reach the spawn (see BENIGN_SPAWNS justification)."""
        recent = tmp_path / "recent"
        recent.mkdir()
        monkeypatch.setattr(files_handlers, "_last_project_dir", lambda: str(recent))
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", return_value=str(tmp_path)) as choose:
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/native-dir-dialog", json={"path": "/attacker/controlled"})
                assert resp.status == 200
                assert choose.call_args.args[0] == str(recent)

    @pytest.mark.asyncio
    async def test_invalid_json_body_is_tolerated(self, tmp_path, mock_sel):
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", return_value=str(tmp_path)):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/native-dir-dialog", data="not json")
                assert resp.status == 200

    @pytest.mark.asyncio
    async def test_selection_is_logged(self, tmp_path, mock_sel):
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", return_value=str(tmp_path)):
            async with TestClient(TestServer(_make_app())) as client:
                await client.post("/api/native-dir-dialog", json={})
        assert mock_sel.log_api_access.call_args.kwargs["operation"] == "native_dir_dialog"
        assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_slot_stays_held_when_the_caller_disconnects_mid_dialog(self, mock_sel):
        """A caller that walks away must not free the slot: the OS panel is still
        on screen (to_thread cannot interrupt it), so releasing it here would let
        a second request stack a second modal on the host.

        Cancels the HANDLER task directly — an aiohttp client-side cancel does
        not necessarily propagate to the server coroutine, so going through
        TestClient would not exercise this path.
        """
        import asyncio as _asyncio

        release = threading.Event()
        entered = threading.Event()

        def blocking_dialog(_default=""):
            entered.set()
            release.wait(5)
            return "/tmp"

        request = SimpleNamespace(
            remote="127.0.0.1", headers={}, get=lambda key, default=None: default,
        )
        with patch.object(nd, "is_available", return_value=True), \
             patch.object(nd, "choose_directory", side_effect=blocking_dialog):
            task = _asyncio.ensure_future(api_native_dir_dialog(request))
            await _asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
            task.cancel()
            with contextlib.suppress(_asyncio.CancelledError):
                await task
            # Dialog thread is still running — the slot must remain taken.
            assert files_handlers._NATIVE_DIALOG_STATE["busy"] is True
            release.set()
            # Once the thread finishes, the done-callback frees the slot.
            for _ in range(100):
                if not files_handlers._NATIVE_DIALOG_STATE["busy"]:
                    break
                await _asyncio.sleep(0.02)
            assert files_handlers._NATIVE_DIALOG_STATE["busy"] is False


class TestLastProjectDir:
    """Where the chooser opens is derived from the gateway's own state."""

    def _write_recents(self, tmp_path, payload):
        (tmp_path / "recent_projects.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_returns_first_existing_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files_handlers, "config_dir", lambda: tmp_path)
        live = tmp_path / "live"
        live.mkdir()
        self._write_recents(tmp_path, ["/gone_xyz_123", str(live)])
        assert files_handlers._last_project_dir() == str(live)

    def test_skips_sensitive_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files_handlers, "config_dir", lambda: tmp_path)
        secret = tmp_path / "secret"
        secret.mkdir()
        ok = tmp_path / "ok"
        ok.mkdir()
        self._write_recents(tmp_path, [str(secret), str(ok)])
        monkeypatch.setattr(
            files_handlers, "is_sensitive_path", lambda p: p == str(secret))
        assert files_handlers._last_project_dir() == str(ok)

    def test_missing_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files_handlers, "config_dir", lambda: tmp_path)
        assert files_handlers._last_project_dir() == ""

    def test_corrupt_or_unexpected_json_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files_handlers, "config_dir", lambda: tmp_path)
        (tmp_path / "recent_projects.json").write_text("{not json", encoding="utf-8")
        assert files_handlers._last_project_dir() == ""
        self._write_recents(tmp_path, {"dirs": ["/x"]})
        assert files_handlers._last_project_dir() == ""
        self._write_recents(tmp_path, [None, 42, ""])
        assert files_handlers._last_project_dir() == ""
