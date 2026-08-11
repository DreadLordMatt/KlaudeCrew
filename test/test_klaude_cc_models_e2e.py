"""Fork (KlaudeCrew): offline e2e for the claude model-picker pipeline.

Drives the packaged fake ACP backend's claude dialect through the same
in-process ``_handle()`` idiom test_fake_acp_backend.py uses, plus a real
``AcpClient`` integration slice: session/new response (with the
live-verified ``configOptions[id="model"]`` shape) -> capture ->
resolve -> set_config_option accept/reject. This is the offline
counterpart of the live smoke test, using the exact wire shape confirmed
against claude-agent-acp v0.66.0.
"""

from __future__ import annotations

import io
import json

import pytest

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.testing import fake_acp_backend as fake


def _capture(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(fake.sys, "stdout", buf)
    return buf


def _messages(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


class TestFakeClaudeDialectAdvertisesModels:
    def test_session_new_carries_model_config_options(self, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_ACP_DIALECT", "claude")
        buf = _capture(monkeypatch)
        fake._handle({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
        (msg,) = _messages(buf)
        opts = msg["result"]["configOptions"]
        model_entry = next(o for o in opts if o["id"] == "model")
        assert model_entry["currentValue"] == "opus[1m]"
        values = [o["value"] for o in model_entry["options"]]
        assert values == ["default", "opus[1m]", "sonnet", "haiku"]

    def test_kiro_dialect_unchanged(self, monkeypatch) -> None:
        monkeypatch.delenv("FAKE_ACP_DIALECT", raising=False)
        buf = _capture(monkeypatch)
        fake._handle({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
        (msg,) = _messages(buf)
        assert "configOptions" not in msg["result"]

    def test_set_config_option_accepts_advertised_model(self, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_ACP_DIALECT", "claude")
        buf = _capture(monkeypatch)
        fake._handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {"sessionId": "s", "configId": "model", "value": "sonnet"},
            }
        )
        (msg,) = _messages(buf)
        assert msg.get("result") == {}
        assert "error" not in msg

    def test_set_config_option_rejects_unadvertised_model(self, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_ACP_DIALECT", "claude")
        buf = _capture(monkeypatch)
        fake._handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": "s",
                    "configId": "model",
                    "value": "global.anthropic.claude-opus-4-8[1m]",
                },
            }
        )
        (msg,) = _messages(buf)
        assert "error" in msg
        assert "Invalid value for config option model" in msg["error"]["message"]

    def test_set_config_option_non_model_still_acks(self, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_ACP_DIALECT", "claude")
        buf = _capture(monkeypatch)
        fake._handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/set_config_option",
                "params": {"sessionId": "s", "configId": "effort", "value": "low"},
            }
        )
        (msg,) = _messages(buf)
        assert msg.get("result") == {}


class TestClientPipelineAgainstFakeShape:
    """AcpClient capture -> resolve -> wire, fed the fake's exact response
    shape (which is itself the live-verified real shape)."""

    def _fresh_client(self):
        from kiro_crew.acp.client import AcpClient

        return AcpClient(acp_backend=ACP_BACKEND_CLAUDE)

    def _session_new_result(self, monkeypatch) -> dict:
        monkeypatch.setenv("FAKE_ACP_DIALECT", "claude")
        buf = _capture(monkeypatch)
        fake._handle({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
        (msg,) = _messages(buf)
        return msg["result"]

    def test_capture_resolve_roundtrip(self, monkeypatch) -> None:
        from kiro_crew.model_registry import resolve_claude_wire_id

        client = self._fresh_client()
        resp = self._session_new_result(monkeypatch)
        client._capture_available_models(resp)

        advertised = client._advertised_model_ids()
        assert advertised == ["default", "opus[1m]", "sonnet", "haiku"]
        assert client._resolved_model_id == "opus[1m]"

        # A canonical registry key resolves to the fake's verbatim advertised
        # spelling -- and that spelling is exactly what the fake's own
        # set_config_option handler accepts.
        wire = resolve_claude_wire_id("sonnet-4.6-1m", advertised)
        assert wire == "sonnet"

        buf = _capture(monkeypatch)
        fake._handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {"sessionId": "s", "configId": "model", "value": wire},
            }
        )
        (msg,) = _messages(buf)
        assert "error" not in msg

    def test_unresolvable_pick_never_reaches_the_wire(self, monkeypatch) -> None:
        from kiro_crew.model_registry import resolve_claude_wire_id

        client = self._fresh_client()
        client._capture_available_models(self._session_new_result(monkeypatch))

        # opus-4.7-1m is a real registry model the fake does NOT advertise:
        # the resolver answers None, which M3 (explicit pick) turns into
        # AcpModelUnavailable and M4 (startup) turns into a withhold --
        # either way nothing is sent, so the fake's rejection path can only
        # be reached by a caller that bypasses the resolver.
        assert resolve_claude_wire_id("opus-4.7-1m", client._advertised_model_ids()) is None

    @pytest.mark.asyncio
    async def test_apply_startup_model_sends_advertised_spelling(self, monkeypatch) -> None:
        client = self._fresh_client()
        client._capture_available_models(self._session_new_result(monkeypatch))
        client._model = "sonnet-4.6-1m"  # canonical key, e.g. persisted config
        monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)

        applied = []

        async def _set_config_option(config_id, value):
            applied.append((config_id, value))

        client.set_config_option = _set_config_option
        await client._apply_startup_model()

        assert applied == [("model", "sonnet")]
        assert client._model == "sonnet"
