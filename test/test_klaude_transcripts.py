"""Fork (KlaudeCrew): claude backend session transcript lifecycle.

Covers klaude/transcripts.py directly, plus its two integration points:
the session/load resume guard in acp/client.py (skip a stale-SID resume
when the guessed transcript path doesn't exist) and the claude branch of
providers/acp.py's cleanup_session().
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, METHOD_SESSION_LOAD
from kiro_crew.klaude.transcripts import (
    claude_transcript_exists,
    claude_transcript_path,
    delete_claude_transcript,
)


class TestTranscriptPath:
    def test_munges_path_separators(self, tmp_path) -> None:
        work_dir = tmp_path / "proj"
        work_dir.mkdir()
        path = claude_transcript_path(work_dir, "sess-1")
        assert path.name == "sess-1.jsonl"
        assert str(work_dir.resolve()).replace("/", "-") in str(path)

    def test_honors_claude_config_dir_override(self, tmp_path, monkeypatch) -> None:
        isolated = tmp_path / "cc-config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated))
        work_dir = tmp_path / "proj"
        work_dir.mkdir()
        path = claude_transcript_path(work_dir, "sess-1")
        assert str(path).startswith(str(isolated / "projects"))

    def test_falls_back_to_home_dot_claude(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        work_dir = tmp_path / "proj"
        work_dir.mkdir()
        path = claude_transcript_path(work_dir, "sess-1")
        assert str(path).startswith(str(tmp_path / ".claude" / "projects"))


class TestTranscriptExists:
    def test_empty_session_id_is_false(self, tmp_path) -> None:
        assert claude_transcript_exists(tmp_path, "") is False

    def test_missing_file_is_false(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        assert claude_transcript_exists(tmp_path, "no-such-session") is False

    def test_existing_file_is_true(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        path = claude_transcript_path(tmp_path, "real-session")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        assert claude_transcript_exists(tmp_path, "real-session") is True


class TestDeleteTranscript:
    def test_deletes_existing_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        path = claude_transcript_path(tmp_path, "sess-del")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        delete_claude_transcript(tmp_path, "sess-del")
        assert not path.exists()

    def test_missing_file_is_silent_noop(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        delete_claude_transcript(tmp_path, "never-existed")  # must not raise

    def test_empty_session_id_is_noop(self, tmp_path) -> None:
        delete_claude_transcript(tmp_path, "")  # must not raise / touch disk


class TestResumeGuardIntegration:
    """The session/load resume guard in acp/client.py must consult the
    getattr hook when present and default to the prior (attempt-load)
    behavior when absent -- see acp/client.py around resume_sid handling."""

    def _make_client(self, tmp_path, **kwargs):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kwargs)
        proc = AsyncMock()
        proc.returncode = None
        client._process = proc
        return client

    @pytest.mark.asyncio
    async def test_hook_false_skips_session_load(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        client._resume_session_id = "ghost-sess"
        client._claude_session_transcript_exists = lambda sid: False

        sent_methods = []

        async def fake_send(method, params):
            sent_methods.append(method)
            return len(sent_methods)

        async def fake_wait(req_id, timeout=50.0):
            if req_id == 1:
                return {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
            return {"sessionId": "fresh-sess"}

        client._send_request = fake_send
        client._wait_for_response = fake_wait
        client._drain_notifications = AsyncMock()

        await client._initialize_session()

        assert METHOD_SESSION_LOAD not in sent_methods
        assert client._session_id == "fresh-sess"
        assert client._resumed is False

    @pytest.mark.asyncio
    async def test_hook_true_attempts_session_load(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        client._resume_session_id = "real-sess"
        client._claude_session_transcript_exists = lambda sid: True

        sent_methods = []

        async def fake_send(method, params):
            sent_methods.append(method)
            return len(sent_methods)

        async def fake_wait(req_id, timeout=50.0):
            if req_id == 1:
                return {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
            return {"modes": ["chat"]}

        client._send_request = fake_send
        client._wait_for_response = fake_wait
        client._drain_notifications = AsyncMock()

        await client._initialize_session()

        assert METHOD_SESSION_LOAD in sent_methods
        assert client._session_id == "real-sess"
        assert client._resumed is True

    @pytest.mark.asyncio
    async def test_hook_absent_defaults_to_attempt(self, tmp_path, monkeypatch) -> None:
        """No companion registered (public-core shape): behavior is exactly
        what it was before this hook existed -- attempt the load.

        Other tests in this process may already have called
        register_acp_backends() (class-level attach, no per-test teardown of
        its own), so explicitly remove the attribute here -- monkeypatch
        restores it after this test regardless of suite order.
        """
        from kiro_crew.acp.client import AcpClient

        monkeypatch.delattr(AcpClient, "_claude_session_transcript_exists", raising=False)
        client = self._make_client(tmp_path)
        client._resume_session_id = "some-sess"
        assert not hasattr(client, "_claude_session_transcript_exists")

        sent_methods = []

        async def fake_send(method, params):
            sent_methods.append(method)
            return len(sent_methods)

        async def fake_wait(req_id, timeout=50.0):
            if req_id == 1:
                return {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
            return {"modes": ["chat"]}

        client._send_request = fake_send
        client._wait_for_response = fake_wait
        client._drain_notifications = AsyncMock()

        await client._initialize_session()

        assert METHOD_SESSION_LOAD in sent_methods

    @pytest.mark.asyncio
    async def test_hook_exception_fails_soft_to_attempt(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        client._resume_session_id = "some-sess"

        def _raise(sid):
            raise OSError("disk unreadable")

        client._claude_session_transcript_exists = _raise

        sent_methods = []

        async def fake_send(method, params):
            sent_methods.append(method)
            return len(sent_methods)

        async def fake_wait(req_id, timeout=50.0):
            if req_id == 1:
                return {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
            return {"modes": ["chat"]}

        client._send_request = fake_send
        client._wait_for_response = fake_wait
        client._drain_notifications = AsyncMock()

        await client._initialize_session()

        assert METHOD_SESSION_LOAD in sent_methods  # fails soft toward attempting


class TestCleanupSessionIntegration:
    @pytest.mark.asyncio
    async def test_claude_backend_delegates_to_client_hook(self, tmp_path) -> None:
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        calls = []
        provider._client._claude_cleanup_transcript = lambda sid: calls.append(sid)

        await provider.cleanup_session("sess-xyz")

        assert calls == ["sess-xyz"]

    @pytest.mark.asyncio
    async def test_claude_backend_hook_absent_is_noop(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.providers.acp import AcpProvider

        # See test_hook_absent_defaults_to_attempt for why this delattr is
        # needed regardless of what ran earlier in this process.
        monkeypatch.delattr(AcpClient, "_claude_cleanup_transcript", raising=False)
        provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        assert not hasattr(provider._client, "_claude_cleanup_transcript")
        await provider.cleanup_session("sess-xyz")  # must not raise

    @pytest.mark.asyncio
    async def test_claude_backend_hook_exception_fails_soft(self, tmp_path) -> None:
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)

        def _raise(sid):
            raise OSError("boom")

        provider._client._claude_cleanup_transcript = _raise
        await provider.cleanup_session("sess-xyz")  # must not raise

    @pytest.mark.asyncio
    async def test_kiro_backend_untouched(self, tmp_path) -> None:
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(work_dir=tmp_path, acp_backend="")
        assert provider.is_claude_backend is False
        # Real kiro-session-file cleanup path -- just confirm it doesn't
        # touch the claude hook or raise for a nonexistent session.
        await provider.cleanup_session("no-such-kiro-session")


class TestRegistryAttachesTranscriptHooks:
    def test_register_attaches_both(self) -> None:
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.klaude.registry import KlaudeProviderRegistry

        KlaudeProviderRegistry().register_acp_backends()
        assert callable(getattr(AcpClient, "_claude_session_transcript_exists", None))
        assert callable(getattr(AcpClient, "_claude_cleanup_transcript", None))
