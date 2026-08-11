"""Fork (KlaudeCrew): the claude ACP backend is live and default.

Covers the fork-owned wiring that upstream deliberately keeps dormant:
``agent.acp_backend`` config → ``create_provider_factory`` → ``AcpProvider``.
Upstream's kiro-cli path must stay byte-identical when the operator opts out
with ``acp_backend = "kiro"``.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.config.loader import KiroCrewConfig


class TestAcpBackendConfig:
    def test_default_is_claude(self) -> None:
        assert KiroCrewConfig().agent.acp_backend == "claude"

    def test_load_defaults_to_claude(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text('{"agent": {}}')
        cfg = KiroCrewConfig.load()
        assert cfg.agent.acp_backend == "claude"

    def test_load_kiro_opt_out(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text('{"agent": {"acp_backend": "kiro"}}')
        cfg = KiroCrewConfig.load()
        assert cfg.agent.acp_backend == "kiro"


class TestFactoryBackendSelection:
    @pytest.fixture
    def _home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        return tmp_path

    def test_default_factory_builds_claude_provider(self, _home) -> None:
        provider = KiroCrewConfig().create_provider_factory()(session_key="t-claude")
        try:
            assert provider.is_claude_backend is True
        finally:
            provider = None

    def test_kiro_opt_out_builds_kiro_provider(self, _home) -> None:
        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = "kiro"
        provider = cfg.create_provider_factory()(session_key="t-kiro")
        assert provider.is_claude_backend is False

    def test_unknown_value_falls_back_to_claude(self, _home) -> None:
        # Anything but the explicit "kiro" opt-out drives the claude backend —
        # a typo must not silently revive the kiro-cli dependency.
        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = "clod"
        provider = cfg.create_provider_factory()(session_key="t-fallback")
        assert provider.is_claude_backend is True

    def test_claude_model_translation_skipped_without_bedrock(self, _home, monkeypatch) -> None:
        # The claude_code registry column is Bedrock-only (every entry is an
        # AWS cross-region inference-profile id) -- a plain `claude login`
        # session rejects those outright. Without CLAUDE_CODE_USE_BEDROCK=1,
        # no override is sent at all; the adapter uses the account's own
        # default model. See config/loader.py's _acp() closure.
        monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
        from kiro_crew.acp.client import DEFAULT_MODEL
        from kiro_crew import model_registry

        cfg = KiroCrewConfig()
        canonical = next(iter(model_registry._REGISTRY))
        provider = cfg.create_provider_factory()(
            session_key="t-model", model_override=canonical
        )
        # No override sent -- AcpClient normalizes the empty string to its
        # own DEFAULT_MODEL sentinel, which _apply_startup_model treats as
        # "let the adapter use the account's own default" (skips the
        # set_config_option call entirely).
        assert provider._client._model == DEFAULT_MODEL
        # Never the Bedrock-shaped id.
        assert provider._client._model != model_registry.to_provider_id(canonical, "claude_code")

    def test_claude_model_translation_applied_with_bedrock_opt_in(self, _home, monkeypatch) -> None:
        # CLAUDE_CODE_USE_BEDROCK=1 is the operator's explicit signal they run
        # claude-agent-acp against Bedrock, where the registry's ids are
        # correct -- restores the pre-fix-commit translation behavior.
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        from kiro_crew import model_registry

        cfg = KiroCrewConfig()
        canonical = None
        for key in model_registry._REGISTRY:
            if model_registry.to_provider_id(key, "claude_code") != model_registry.to_acp_id(key):
                canonical = key
                break
        if canonical is None:
            pytest.skip("registry has no key that diverges between backends")
        provider = cfg.create_provider_factory()(
            session_key="t-model-bedrock", model_override=canonical
        )
        assert provider._client._model == model_registry.to_provider_id(canonical, "claude_code")

    def test_kiro_model_translation_unchanged(self, _home) -> None:
        from kiro_crew import model_registry

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = "kiro"
        canonical = next(iter(model_registry._REGISTRY))
        provider = cfg.create_provider_factory()(
            session_key="t-model-kiro", model_override=canonical
        )
        assert provider._client._model == model_registry.to_acp_id(canonical)

    def test_claude_env_carries_config_dir_isolation(self, _home, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_CC_ISOLATE", raising=False)
        provider = KiroCrewConfig().create_provider_factory()(session_key="t-env")
        env = provider._client._extra_env or {}
        assert env.get("CLAUDE_CONFIG_DIR", "").endswith("cc-config")

    def test_isolation_opt_out(self, _home, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_CC_ISOLATE", "0")
        provider = KiroCrewConfig().create_provider_factory()(session_key="t-env-optout")
        env = provider._client._extra_env or {}
        assert "CLAUDE_CONFIG_DIR" not in env

    def test_kiro_env_untouched(self, _home) -> None:
        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = "kiro"
        provider = cfg.create_provider_factory()(session_key="t-env-kiro")
        env = provider._client._extra_env or {}
        assert "CLAUDE_CONFIG_DIR" not in env
