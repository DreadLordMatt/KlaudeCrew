"""Fork (KlaudeCrew): claude-dialect unit tests for the packaged fake ACP backend.

Same direct-``_handle()``-call idiom as test_fake_acp_backend.py (in-process,
no subprocess) -- see that file's ``_capture``/``_messages`` helpers, which
this file reuses. Covers the two wire-shape divergences
``FAKE_ACP_DIALECT=claude`` flips (integer protocolVersion,
``agent_thought_chunk``) plus the dialect-independent ``mcpServers`` probe
that klaude/mcp_servers.py's contract depends on being observable in tests.
"""

from __future__ import annotations

import io
import json

from kiro_crew.testing import fake_acp_backend as fake


def _capture(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(fake.sys, "stdout", buf)
    return buf


def _messages(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _prompt(text: str, session_id: str = fake._SESSION_ID) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "session/prompt",
        "params": {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        },
    }


class TestDialectDefaultsToKiro:
    def test_default_dialect_is_kiro(self, monkeypatch) -> None:
        monkeypatch.delenv("FAKE_ACP_DIALECT", raising=False)
        assert fake._dialect() == "kiro"

    def test_unrecognized_value_falls_back_to_kiro(self, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_ACP_DIALECT", "something-else")
        assert fake._dialect() == "kiro"

    def test_claude_dialect_recognized(self, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_ACP_DIALECT", "claude")
        assert fake._dialect() == "claude"


class TestProtocolVersion:
    def test_kiro_dialect_uses_date_string(self, monkeypatch) -> None:
        monkeypatch.delenv("FAKE_ACP_DIALECT", raising=False)
        buf = _capture(monkeypatch)
        fake._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        (msg,) = _messages(buf)
        assert msg["result"]["protocolVersion"] == fake.PROTOCOL_VERSION
        assert isinstance(msg["result"]["protocolVersion"], str)

    def test_claude_dialect_uses_integer(self, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_ACP_DIALECT", "claude")
        buf = _capture(monkeypatch)
        fake._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        (msg,) = _messages(buf)
        assert msg["result"]["protocolVersion"] == fake.PROTOCOL_VERSION_CLAUDE
        assert isinstance(msg["result"]["protocolVersion"], int)


class TestThoughtTrigger:
    def test_claude_dialect_emits_thought_chunk(self, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_ACP_DIALECT", "claude")
        buf = _capture(monkeypatch)
        fake._handle(_prompt(fake.THOUGHT_TRIGGER))
        kinds = [
            m["params"]["update"]["sessionUpdate"]
            for m in _messages(buf)
            if m.get("method") == "session/update"
        ]
        assert "agent_thought_chunk" in kinds
        # Thought precedes the ordinary reply chunk.
        assert kinds.index("agent_thought_chunk") < kinds.index("agent_message_chunk")

    def test_kiro_dialect_ignores_thought_trigger(self, monkeypatch) -> None:
        monkeypatch.delenv("FAKE_ACP_DIALECT", raising=False)
        buf = _capture(monkeypatch)
        fake._handle(_prompt(fake.THOUGHT_TRIGGER))
        kinds = [
            m["params"]["update"]["sessionUpdate"]
            for m in _messages(buf)
            if m.get("method") == "session/update"
        ]
        assert "agent_thought_chunk" not in kinds
        # The trigger text still isn't the empty string, so it streams as an
        # ordinary (if odd-looking) reply -- same as any unrecognized prompt.
        assert "agent_message_chunk" in kinds


class TestMcpServersProbe:
    def test_probe_unset_is_silent_noop(self, monkeypatch) -> None:
        monkeypatch.delenv("FAKE_ACP_MCP_PROBE_PATH", raising=False)
        buf = _capture(monkeypatch)
        fake._handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"mcpServers": [{"name": "x", "type": "stdio", "command": "x"}]},
            }
        )
        (msg,) = _messages(buf)
        assert msg["result"]["sessionId"] == fake._SESSION_ID  # handled normally

    def test_probe_records_received_mcp_servers(self, tmp_path, monkeypatch) -> None:
        probe = tmp_path / "mcp_probe.json"
        monkeypatch.setenv("FAKE_ACP_MCP_PROBE_PATH", str(probe))
        # _capture is called for its monkeypatch side effect only (redirect
        # fake.sys.stdout) -- this test asserts against the probe file, not
        # captured stdout.
        _ = _capture(monkeypatch)
        servers = [
            {"name": "kirocrew-core", "type": "stdio", "command": "kirocrew", "args": ["mcp-core"]},
            {"name": "remote", "type": "http", "url": "https://example.com/mcp"},
        ]
        fake._handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"mcpServers": servers},
            }
        )
        assert json.loads(probe.read_text()) == servers

    def test_probe_fires_on_session_load_too(self, tmp_path, monkeypatch) -> None:
        probe = tmp_path / "mcp_probe.json"
        monkeypatch.setenv("FAKE_ACP_MCP_PROBE_PATH", str(probe))
        buf = _capture(monkeypatch)
        servers = [{"name": "kirocrew-cron", "type": "stdio", "command": "kirocrew"}]
        fake._handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/load",
                "params": {"sessionId": "resumed", "mcpServers": servers},
            }
        )
        (msg,) = _messages(buf)
        assert msg["result"] == {"modes": ["chat"]}
        assert json.loads(probe.read_text()) == servers

    def test_missing_mcp_servers_key_records_null(self, tmp_path, monkeypatch) -> None:
        probe = tmp_path / "mcp_probe.json"
        monkeypatch.setenv("FAKE_ACP_MCP_PROBE_PATH", str(probe))
        # _capture is called for its monkeypatch side effect only (redirect
        # fake.sys.stdout) -- this test asserts against the probe file, not
        # captured stdout.
        _ = _capture(monkeypatch)
        fake._handle({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
        assert json.loads(probe.read_text()) is None

    def test_unwritable_probe_path_fails_soft(self, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_ACP_MCP_PROBE_PATH", "/nonexistent-dir/x/y/probe.json")
        buf = _capture(monkeypatch)
        # Must not raise -- protocol handling continues normally.
        fake._handle({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
        (msg,) = _messages(buf)
        assert msg["result"]["sessionId"] == fake._SESSION_ID
