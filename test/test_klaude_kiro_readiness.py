"""Fork (KlaudeCrew): reject_if_not_kiro_backend() -- the guard that stops
the two RAW kiro-cli one-shot spawn sites (/api/models, /api/sessions/usage)
from ever launching kiro-cli when it isn't the configured backend.

Without this, assume_kiro_ready (set for any non-kiro backend, so the
first-run SPA gate doesn't block a claude install) makes
reject_if_kiro_unverified() an unconditional pass-through, and both handlers
would spawn a real, unauthenticated kiro-cli on every poll -- popping its
own browser OAuth tab (app.kiro.dev/signin?...&redirect_from=kirocli) even
though the operator never asked for kiro-cli at all. Reproduced live and
confirmed fixed before writing these tests.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers import agents, sessions
from kiro_crew.dashboard.kiro_readiness import reject_if_not_kiro_backend


def _cfg(acp_backend: str) -> SimpleNamespace:
    # `model` is read by api_models's claude branch (_cc_models's
    # configured_default); "auto" mirrors the real dataclass default.
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=acp_backend, model="auto"))


class TestRejectIfNotKiroBackend:
    def test_kiro_backend_passes_through(self):
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_cfg("kiro")):
            assert reject_if_not_kiro_backend() is None

    def test_claude_backend_blocks(self):
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_cfg("claude")):
            resp = reject_if_not_kiro_backend()
        assert resp is not None
        assert resp.status == 503
        assert json.loads(resp.body)["code"] == "kiro_not_backend"

    def test_default_config_blocks(self):
        """No explicit acp_backend set anywhere -- the real KiroCrewConfig
        default is "claude", so this must still block, not silently pass
        kiro-cli through on an unconfigured host."""
        with patch(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            return_value=_cfg("claude"),  # mirrors the dataclass default
        ):
            assert reject_if_not_kiro_backend() is not None


class TestApiModelsNeverSpawnsKiroCliOnClaudeBackend:
    @pytest.mark.asyncio
    async def test_never_reaches_resolve_or_spawn(self):
        """The claude branch serves (or 503s cold-start) WITHOUT ever touching
        kiro-cli. The bare SimpleNamespace state (no .sessions) exercises
        _advertised_cc_models's fail-soft path -> nothing advertised -> the
        cc_models_cold_start 503, never the kiro_not_backend response (that
        code now only escapes api_models via /api/sessions/usage, which has
        no claude-appropriate data to serve instead)."""
        request = MagicMock()
        request.app = {"kiro_prerequisite_service": None, "state": SimpleNamespace()}
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_cfg("claude")),
            patch(
                "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", AsyncMock()
            ) as resolve,
            patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn,
        ):
            resp = await agents.api_models(request)

        resolve.assert_not_called()
        spawn.assert_not_called()
        assert resp.status == 503
        assert json.loads(resp.body)["code"] == "cc_models_cold_start"

    @pytest.mark.asyncio
    async def test_serves_advertised_models_without_kiro(self):
        """Once a live session has advertised models, the claude branch
        returns them 200 -- still with zero kiro-cli involvement."""

        class _Provider:
            def available_models(self):
                return [
                    {"modelId": "opus[1m]", "name": "Opus (1M context)", "description": "..."},
                    {"modelId": "sonnet", "name": "Sonnet", "description": "..."},
                ]

        state = SimpleNamespace(
            sessions=SimpleNamespace(active_providers=lambda: [_Provider()])
        )
        request = MagicMock()
        request.app = {"kiro_prerequisite_service": None, "state": state}
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_cfg("claude")),
            patch(
                "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", AsyncMock()
            ) as resolve,
            patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn,
        ):
            resp = await agents.api_models(request)

        resolve.assert_not_called()
        spawn.assert_not_called()
        assert resp.status == 200
        rows = json.loads(resp.body)
        names = [r["model_name"] for r in rows]
        assert names[0] == "auto"  # sentinel always leads
        assert "opus[1m]" in names  # raw wire value passes through
        # "sonnet" is a registry alias -> folded onto its canonical key.
        assert "sonnet-4.6-1m" in names


class TestApiSessionsUsageNeverSpawnsKiroCliOnClaudeBackend:
    @pytest.mark.asyncio
    async def test_never_schedules_fetch(self):
        request = MagicMock()
        request.app = {
            "kiro_prerequisite_service": None,
            "state": SimpleNamespace(_background_tasks=set()),
        }
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_cfg("claude")),
            patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch,
        ):
            resp = await sessions.api_sessions_usage(request)

        fetch.assert_not_called()
        assert resp.status == 503
        assert json.loads(resp.body)["code"] == "kiro_not_backend"
