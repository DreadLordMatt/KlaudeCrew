"""Tests for /api/skills/-/sources — linked skill repository endpoints.

The git layer is stubbed here (``sync_skill_source`` is patched); real-git
behaviour is covered in ``test_skill_sources.py``. What these tests pin is the
HTTP contract: auth, validation, that a failed first sync is NOT persisted, and
that removal drops both the config entry and the mirror.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import skill_sources as handlers
from kiro_crew.skill_sources import SkillSourceSyncResult


def _ok_result(name: str = "team-skills", count: int = 3) -> SkillSourceSyncResult:
    return SkillSourceSyncResult(
        name=name,
        ok=True,
        action="cloned",
        head="a" * 40,
        skill_count=count,
        message=f"cloned {name!r}",
        synced_at=123.0,
    )


def _fail_result(name: str = "team-skills", error: str = "clone_failed") -> SkillSourceSyncResult:
    return SkillSourceSyncResult(
        name=name,
        ok=False,
        action="failed",
        error=error,
        message="git clone failed (exit 128)",
        synced_at=123.0,
    )


class _FakeLoader:
    """Records ``reload_extra_paths`` calls so the refresh wiring is observable."""

    def __init__(self, sink: list[object]) -> None:
        self._sink = sink

    def reload_extra_paths(self, config=None) -> None:
        self._sink.append(config)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated home + stubbed SEL, with a real config.json the handlers mutate."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(handlers, "_sel", lambda: MagicMock())
    return home


@pytest.fixture
def client(env, monkeypatch):
    """A test client whose requests carry an authenticated user by default.

    Auth is normally applied by gateway middleware; the per-request marker is
    injected here so the handlers' own 401 guard can be tested by flipping it
    off rather than by standing up the whole middleware stack.
    """
    authed = {"on": True}

    @web.middleware
    async def _auth_mw(request, handler):
        if authed["on"]:
            request["user"] = "tester"
        return await handler(request)

    app = web.Application(middlewares=[_auth_mw])
    # The refresh path reads request.app["state"], so give it one — a bare
    # sentinel is enough because _get_skills is what tests patch.
    app["state"] = object()
    app.router.add_get("/api/skills/-/sources", handlers.api_skill_sources)
    app.router.add_post("/api/skills/-/sources", handlers.api_skill_sources_add)
    app.router.add_post("/api/skills/-/sources/{name}/sync", handlers.api_skill_sources_sync)
    app.router.add_delete("/api/skills/-/sources/{name}", handlers.api_skill_sources_delete)
    app._authed = authed  # type: ignore[attr-defined]
    return app


async def _client(app) -> TestClient:
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


def _config(home) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


class TestAuth:
    @pytest.mark.asyncio
    async def test_all_routes_require_a_user(self, client):
        client._authed["on"] = False
        c = await _client(client)
        try:
            for method, path in (
                ("get", "/api/skills/-/sources"),
                ("post", "/api/skills/-/sources"),
                ("post", "/api/skills/-/sources/x/sync"),
                ("delete", "/api/skills/-/sources/x"),
            ):
                resp = await getattr(c, method)(path, json={} if method == "post" else None)
                assert resp.status == 401, f"{method} {path}"
        finally:
            await c.close()


class TestList:
    @pytest.mark.asyncio
    async def test_empty_by_default(self, client):
        c = await _client(client)
        try:
            resp = await c.get("/api/skills/-/sources")
            assert resp.status == 200
            assert (await resp.json()) == {"sources": []}
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_row_building_runs_off_the_event_loop(self, client, env, monkeypatch):
        """``_source_row`` walks the mirror, so it must never run on the loop.

        A source with an empty ``subdir`` mounts the whole checkout, so an
        on-loop walk of a large linked repo would stall every chat turn and the
        liveness heartbeat.
        """
        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []
        real_row = handlers._source_row

        def _spy(src, state):
            seen.append(threading.current_thread())
            return real_row(src, state)

        async def _sync(src):
            return _ok_result(src.name)

        monkeypatch.setattr(handlers, "_source_row", _spy)
        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        c = await _client(client)
        try:
            await c.post(
                "/api/skills/-/sources",
                json={"name": "team-skills", "repo": "https://github.com/o/r.git"},
            )
            assert (await c.get("/api/skills/-/sources")).status == 200
            assert seen, "row builder never ran"
            assert all(t is not loop_thread for t in seen)
        finally:
            await c.close()


class TestAdd:
    @pytest.mark.asyncio
    async def test_persists_on_successful_sync(self, client, env, monkeypatch):
        async def _sync(src):
            return _ok_result(src.name)

        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        c = await _client(client)
        try:
            resp = await c.post(
                "/api/skills/-/sources",
                json={
                    "name": "team-skills",
                    "repo": "https://github.com/org/team-skills.git",
                    "branch": "main",
                    "subdir": "skills",
                },
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["ok"] is True
            assert body["source"]["name"] == "team-skills"
            saved = _config(env)["skills"]["sources"]
            assert [s["name"] for s in saved] == ["team-skills"]
            assert saved[0]["subdir"] == "skills"
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_failed_sync_is_not_persisted(self, client, env, monkeypatch):
        """A broken entry must not be saved — it would fail every startup sync."""
        removed: list[str] = []

        async def _sync(src):
            return _fail_result(src.name)

        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        monkeypatch.setattr(
            handlers, "remove_skill_source_clone", lambda n: removed.append(n) or True
        )
        c = await _client(client)
        try:
            resp = await c.post(
                "/api/skills/-/sources",
                json={"name": "team-skills", "repo": "https://github.com/org/nope.git"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "clone_failed"
            assert _config(env).get("skills", {}).get("sources", []) == []
            # And no half-cloned mirror is left behind.
            assert removed == ["team-skills"]
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_rejects_bad_name_without_syncing(self, client, monkeypatch):
        called = False

        async def _sync(src):
            nonlocal called
            called = True
            return _ok_result(src.name)

        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        c = await _client(client)
        try:
            for bad in ("../escape", "Team", "team_skills", ""):
                resp = await c.post(
                    "/api/skills/-/sources",
                    json={"name": bad, "repo": "https://github.com/o/r.git"},
                )
                assert resp.status == 400, bad
            assert called is False
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_requires_repo(self, client):
        c = await _client(client)
        try:
            resp = await c.post("/api/skills/-/sources", json={"name": "team"})
            assert resp.status == 400
            assert "repo" in (await resp.json())["error"]
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_duplicate_name_conflicts(self, client, env, monkeypatch):
        async def _sync(src):
            return _ok_result(src.name)

        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        c = await _client(client)
        try:
            body = {"name": "team-skills", "repo": "https://github.com/o/r.git"}
            assert (await c.post("/api/skills/-/sources", json=body)).status == 200
            resp = await c.post("/api/skills/-/sources", json=body)
            assert resp.status == 409
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_refreshes_the_live_loader(self, client, env, monkeypatch):
        """Otherwise the linked skills stay invisible until a gateway restart."""
        refreshed: list[object] = []

        async def _sync(src):
            return _ok_result(src.name)

        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        monkeypatch.setattr(
            handlers, "_get_skills", lambda _state: _FakeLoader(refreshed)
        )
        c = await _client(client)
        try:
            resp = await c.post(
                "/api/skills/-/sources",
                json={"name": "team-skills", "repo": "https://github.com/o/r.git"},
            )
            assert resp.status == 200
            assert len(refreshed) == 1
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_loader_refresh_failure_does_not_fail_the_add(self, client, env, monkeypatch):
        """The config write already succeeded; a refresh problem is not fatal."""

        async def _sync(src):
            return _ok_result(src.name)

        def _boom(_state):
            raise RuntimeError("no state")

        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        monkeypatch.setattr(handlers, "_get_skills", _boom)
        c = await _client(client)
        try:
            resp = await c.post(
                "/api/skills/-/sources",
                json={"name": "team-skills", "repo": "https://github.com/o/r.git"},
            )
            assert resp.status == 200
            assert [s["name"] for s in _config(env)["skills"]["sources"]] == ["team-skills"]
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_concurrent_adds_do_not_lose_a_source(self, client, env, monkeypatch):
        """config.json is rewritten wholesale, so load/check/save must be atomic.

        Without the shared config lock both requests read the pre-change
        snapshot and the second save silently discards the first entry.

        The interleaving is forced rather than hoped for: ``save`` is made slow,
        and because it runs in a worker thread that sleep yields the event loop.
        Unlocked, the second request loads during that window and then saves its
        stale snapshot. Locked, its load cannot start until the first save
        completes. A test that merely fires two requests concurrently passes
        either way — the scheduler happens not to interleave them.
        """
        real_cls = handlers.KiroCrewConfig

        class _SlowSaveLoader:
            @staticmethod
            def load():
                cfg = real_cls.load()
                original = cfg.save

                def _slow_save(*args, **kwargs):
                    time.sleep(0.15)
                    return original(*args, **kwargs)

                cfg.save = _slow_save  # type: ignore[method-assign]
                return cfg

        async def _sync(src):
            return _ok_result(src.name)

        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        monkeypatch.setattr(handlers, "KiroCrewConfig", _SlowSaveLoader)
        c = await _client(client)
        try:
            first = asyncio.ensure_future(
                c.post(
                    "/api/skills/-/sources",
                    json={"name": "one", "repo": "https://github.com/o/one.git"},
                )
            )
            await asyncio.sleep(0.05)  # let the first request reach its save
            second = asyncio.ensure_future(
                c.post(
                    "/api/skills/-/sources",
                    json={"name": "two", "repo": "https://github.com/o/two.git"},
                )
            )
            responses = await asyncio.gather(first, second)
            assert [r.status for r in responses] == [200, 200]
            saved = {s["name"] for s in _config(env)["skills"]["sources"]}
            assert saved == {"one", "two"}
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_concurrent_same_name_adds_do_not_destroy_the_winner(
        self, client, env, monkeypatch
    ):
        """Two adds for the same name share one mirror directory.

        Unserialized, both clear the duplicate check (neither is persisted yet),
        both sync into ``skill-sources/<name>``, and the loser's cleanup deletes
        the winner's clone — leaving a persisted source with no skills on disk.
        The per-name lock makes the second request see the first's persisted
        entry and 409 before it syncs or cleans anything.
        """
        synced: list[str] = []
        cleaned: list[str] = []

        async def _sync(src):
            synced.append(src.name)
            await asyncio.sleep(0.1)  # hold the transaction open
            return _ok_result(src.name)

        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        monkeypatch.setattr(
            handlers,
            "remove_skill_source_clone",
            lambda n: bool(cleaned.append(n)) or True,
        )
        c = await _client(client)
        try:
            body = {"name": "team-skills", "repo": "https://github.com/o/r.git"}
            responses = await asyncio.gather(
                c.post("/api/skills/-/sources", json=body),
                c.post("/api/skills/-/sources", json=body),
            )
            statuses = sorted(r.status for r in responses)
            assert statuses == [200, 409]
            # Exactly one sync ran, and no cleanup touched the winner's mirror.
            assert synced == ["team-skills"]
            assert cleaned == []
            assert [s["name"] for s in _config(env)["skills"]["sources"]] == ["team-skills"]
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_invalid_json_body(self, client):
        c = await _client(client)
        try:
            resp = await c.post(
                "/api/skills/-/sources",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
        finally:
            await c.close()


class TestSync:
    @pytest.mark.asyncio
    async def test_unknown_source_is_404(self, client):
        c = await _client(client)
        try:
            resp = await c.post("/api/skills/-/sources/nope/sync", json={})
            assert resp.status == 404
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_failed_sync_returns_502_not_200(self, client, env, monkeypatch):
        """A sync failure is an upstream failure, not a client error or a success."""
        results = [_ok_result(), _fail_result(error="fetch_failed")]

        async def _sync(src):
            return results.pop(0)

        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        c = await _client(client)
        try:
            await c.post(
                "/api/skills/-/sources",
                json={"name": "team-skills", "repo": "https://github.com/o/r.git"},
            )
            resp = await c.post("/api/skills/-/sources/team-skills/sync", json={})
            assert resp.status == 502
            body = await resp.json()
            assert body["ok"] is False
            assert body["result"]["error"] == "fetch_failed"
            # The human-readable reason is mirrored at the top level so the UI
            # renders it directly instead of dumping the response envelope.
            assert body["message"] == "git clone failed (exit 128)"
            assert body["error"] == "fetch_failed"
        finally:
            await c.close()


class TestDelete:
    @pytest.mark.asyncio
    async def test_removes_config_entry_and_mirror(self, client, env, monkeypatch):
        async def _sync(src):
            return _ok_result(src.name)

        removed: list[str] = []
        monkeypatch.setattr(handlers, "sync_skill_source", _sync)
        monkeypatch.setattr(
            handlers, "remove_skill_source_clone", lambda n: bool(removed.append(n)) or True
        )
        c = await _client(client)
        try:
            await c.post(
                "/api/skills/-/sources",
                json={"name": "team-skills", "repo": "https://github.com/o/r.git"},
            )
            resp = await c.delete("/api/skills/-/sources/team-skills")
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
            assert _config(env)["skills"]["sources"] == []
            assert removed == ["team-skills"]
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_unknown_source_is_404(self, client):
        c = await _client(client)
        try:
            resp = await c.delete("/api/skills/-/sources/nope")
            assert resp.status == 404
        finally:
            await c.close()
