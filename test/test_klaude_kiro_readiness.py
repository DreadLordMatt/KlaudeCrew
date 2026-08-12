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
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers import agents, sessions
from kiro_crew.dashboard.handlers import usage as usage_mod
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
        code escapes both api_models and api_sessions_usage into their own
        claude-appropriate in-memory responses -- see
        TestApiSessionsUsageNeverSpawnsKiroCliOnClaudeBackend below)."""
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
    @pytest.fixture(autouse=True)
    def _isolate_shards(self, tmp_path, monkeypatch):
        """Point claude_usage_payload() at an isolated, empty shard dir (never
        the developer's real ~/.kirocrew) and reset its process-global cache
        so tests don't see a stale result from a previous test."""
        shard_dir = tmp_path / "tokens"
        shard_dir.mkdir()
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
        usage_mod._CLAUDE_USAGE_CACHE = {}
        usage_mod._CLAUDE_USAGE_CACHE_SIG = ()
        usage_mod._CLAUDE_USAGE_CACHE_AT = 0.0
        self.shard_dir = shard_dir

    @staticmethod
    def _request():
        request = MagicMock()
        request.app = {
            "kiro_prerequisite_service": None,
            "state": SimpleNamespace(_background_tasks=set()),
        }
        return request

    @pytest.mark.asyncio
    async def test_never_spawns_kiro_serves_claude_shape(self):
        """The claude branch serves an in-memory month-to-date summary --
        200, never the kiro-cli-backed 503 -- and never schedules the
        kiro-cli usage-fetch background task."""
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_cfg("claude")),
            patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch,
        ):
            resp = await sessions.api_sessions_usage(self._request())

        fetch.assert_not_called()
        assert resp.status == 200
        body = json.loads(resp.body)["usage"]
        assert body["backend"] == "claude"
        assert body["available"] is False
        assert body["cost_usd"] == 0.0
        assert body["input_tokens"] == 0
        # Deliberately absent: the frontend's numeric credit-meter path is
        # gated on credits_plan and must never fire on this shape.
        assert "credits_plan" not in body

    @pytest.mark.asyncio
    async def test_populated_shard_sums_into_claude_shape(self):
        """A claude-shaped token row (nonzero tokens/cost, zero credits) is
        summed into the response; a kiro-shaped row (credits only) in the
        SAME shard is excluded -- both are stamped provider="acp" (see
        claude_usage_payload's docstring), so the split must be by content,
        not by that field."""
        today = datetime.now().astimezone()
        claude_row = {
            "_type": "tokens",
            "ts": today.isoformat(),
            "slot": "dashboard:chat-1-1",
            "provider": "acp",
            "model": "sonnet",
            "input": 100,
            "output": 50,
            "cache_create": 0,
            "cache_read": 0,
            "cost": 0.25,
            "credits": 0.0,
        }
        kiro_row = {
            "_type": "tokens",
            "ts": today.isoformat(),
            "slot": "dashboard:chat-2-1",
            "provider": "acp",
            "model": "claude-opus-4.8",
            "input": 0,
            "output": 0,
            "cache_create": 0,
            "cache_read": 0,
            "cost": 0.0,
            "credits": 3.5,
        }
        shard_path = self.shard_dir / f"{today.strftime('%Y-%m-%d')}.jsonl"
        shard_path.write_text(json.dumps(claude_row) + "\n" + json.dumps(kiro_row) + "\n")

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_cfg("claude")):
            resp = await sessions.api_sessions_usage(self._request())

        assert resp.status == 200
        body = json.loads(resp.body)["usage"]
        assert body["input_tokens"] == 100
        assert body["output_tokens"] == 50
        assert body["cost_usd"] == 0.25
        assert body["cost_usd_today"] == 0.25
        assert body["turns"] == 1  # only the claude row counted

    @pytest.mark.asyncio
    async def test_row_outside_current_month_excluded(self):
        """A shard row timestamped last month must not bleed into this
        month's total, even though the shard file itself is scanned."""
        today = datetime.now().astimezone()
        last_month = (today.replace(day=1) - timedelta(days=1)).replace(hour=12)
        old_row = {
            "_type": "tokens",
            "ts": last_month.isoformat(),
            "slot": "dashboard:chat-3-1",
            "provider": "acp",
            "model": "sonnet",
            "input": 999,
            "output": 999,
            "cache_create": 0,
            "cache_read": 0,
            "cost": 9.99,
            "credits": 0.0,
        }
        shard_path = self.shard_dir / f"{last_month.strftime('%Y-%m-%d')}.jsonl"
        shard_path.write_text(json.dumps(old_row) + "\n")

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=_cfg("claude")):
            resp = await sessions.api_sessions_usage(self._request())

        body = json.loads(resp.body)["usage"]
        assert body["input_tokens"] == 0
        assert body["cost_usd"] == 0.0
        assert body["turns"] == 0
