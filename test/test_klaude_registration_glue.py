"""Fork (KlaudeCrew): registration-glue tests for the claude ACP backend.

Covers the two hooks ``klaude.registry.KlaudeProviderRegistry`` attaches
onto ``AcpClient`` -- MCP server injection (``klaude/mcp_servers.py``) and
the permission-routing settings seed (``klaude/settings_seed.py``) -- plus
the attach step itself. The kiro-cli path must observe zero behavior change
either way: these hooks are only ever reached from the ``_is_claude``
branches in ``acp/client.py``.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.klaude.mcp_servers import claude_session_servers
from kiro_crew.klaude.registry import KlaudeProviderRegistry
from kiro_crew.klaude.settings_seed import write_claude_local_settings


@pytest.fixture
def _home(tmp_path, monkeypatch):
    # kiro_agents_dir() = kiro_home() / "agents", and kiro_home() honors
    # KIRO_HOME (kiro-cli's own override), not KIROCREW_HOME.
    monkeypatch.setenv("KIRO_HOME", str(tmp_path))
    return tmp_path


def _agents_dir(home):
    d = home / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestClaudeSessionServers:
    def test_missing_spec_still_injects_managed_servers(self, _home) -> None:
        servers = claude_session_servers("kirocrew")
        names = {s["name"] for s in servers}
        assert {"kirocrew-core", "kirocrew-cron", "kirocrew-computer"} <= names
        for s in servers:
            assert s["type"] == "stdio"
            assert s["command"]

    def test_reshapes_stdio_entry(self, _home) -> None:
        spec = {
            "mcpServers": {
                "my-tool": {
                    "command": "my-cmd",
                    "args": ["--flag", "value"],
                    "env": {"KEY": "val"},
                }
            }
        }
        (_agents_dir(_home) / "kirocrew.json").write_text(json.dumps(spec))
        servers = claude_session_servers("kirocrew")
        entry = next(s for s in servers if s["name"] == "my-tool")
        assert entry == {
            "name": "my-tool",
            "type": "stdio",
            "command": "my-cmd",
            "args": ["--flag", "value"],
            "env": [{"name": "KEY", "value": "val"}],
        }

    def test_reshapes_url_entry(self, _home) -> None:
        spec = {
            "mcpServers": {
                "remote-tool": {
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer tok"},
                }
            }
        }
        (_agents_dir(_home) / "kirocrew.json").write_text(json.dumps(spec))
        servers = claude_session_servers("kirocrew")
        entry = next(s for s in servers if s["name"] == "remote-tool")
        assert entry == {
            "name": "remote-tool",
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }

    def test_explicit_type_preserved(self, _home) -> None:
        spec = {"mcpServers": {"sse-tool": {"url": "https://x/sse", "type": "sse"}}}
        (_agents_dir(_home) / "kirocrew.json").write_text(json.dumps(spec))
        servers = claude_session_servers("kirocrew")
        entry = next(s for s in servers if s["name"] == "sse-tool")
        assert entry["type"] == "sse"

    def test_disabled_entry_dropped(self, _home) -> None:
        spec = {"mcpServers": {"off-tool": {"command": "x", "disabled": True}}}
        (_agents_dir(_home) / "kirocrew.json").write_text(json.dumps(spec))
        servers = claude_session_servers("kirocrew")
        assert "off-tool" not in {s["name"] for s in servers}

    def test_managed_server_spec_override_ignored(self, _home) -> None:
        # A stale/hand-edited spec entry for a managed name must never win --
        # the canonical invocation always wins for these names.
        spec = {"mcpServers": {"kirocrew-core": {"url": "https://stale/should-not-be-used"}}}
        (_agents_dir(_home) / "kirocrew.json").write_text(json.dumps(spec))
        servers = claude_session_servers("kirocrew")
        entry = next(s for s in servers if s["name"] == "kirocrew-core")
        assert entry["type"] == "stdio"
        assert "url" not in entry

    def test_malformed_spec_fails_soft(self, _home) -> None:
        (_agents_dir(_home) / "kirocrew.json").write_text("not json")
        servers = claude_session_servers("kirocrew")
        names = {s["name"] for s in servers}
        assert "kirocrew-core" in names  # still got the managed floor

    def test_custom_agent_name_resolves_by_filename(self, _home) -> None:
        spec = {"mcpServers": {"custom-tool": {"command": "c"}}}
        (_agents_dir(_home) / "my-custom-agent.json").write_text(json.dumps(spec))
        servers = claude_session_servers("my-custom-agent")
        assert "custom-tool" in {s["name"] for s in servers}


class TestSettingsSeed:
    def test_writes_permission_routing(self, tmp_path) -> None:
        class _Fake:
            _work_dir = tmp_path

        write_claude_local_settings(_Fake())
        target = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(target.read_text())
        assert data == {"permissions": {"defaultMode": "default"}}

    def test_creates_parent_dirs(self, tmp_path) -> None:
        nested = tmp_path / "does" / "not" / "exist" / "yet"

        class _Fake:
            _work_dir = nested

        write_claude_local_settings(_Fake())
        assert (nested / ".claude" / "settings.local.json").exists()


class TestRegistryAttachment:
    def test_register_attaches_hooks(self) -> None:
        from kiro_crew.acp.client import AcpClient

        # Clean slate for this test regardless of prior registration elsewhere
        # in the suite (attachment is idempotent by design).
        KlaudeProviderRegistry().register_acp_backends()
        assert callable(getattr(AcpClient, "_claude_session_mcp_servers", None))
        assert callable(getattr(AcpClient, "_write_claude_local_settings", None))

    def test_kiro_backend_client_untouched(self, tmp_path) -> None:
        # Registration attaches hooks the kiro branch never calls -- kiro-cli's
        # own spawn path only reaches these via `if self._is_claude`, so a kiro
        # client must not create a .claude/ dir or reference the hooks itself.
        from kiro_crew.acp.client import AcpClient

        KlaudeProviderRegistry().register_acp_backends()
        client = AcpClient(work_dir=tmp_path, acp_backend="")
        assert client._is_claude is False
        assert not (tmp_path / ".claude").exists()

    def test_create_factory_is_identity(self) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        registry = KlaudeProviderRegistry()
        assert registry.create_factory(cfg).__name__ == cfg.create_provider_factory().__name__
