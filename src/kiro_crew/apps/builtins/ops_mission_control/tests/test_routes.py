"""Tests for the HTTP surface.

Two properties matter most here.

**The enabled gate.** Builtin routes are registered at gateway startup and exist
even while the app is disabled, so every handler must refuse when disabled. A
missing gate on a default-disabled opt-in app means it is silently callable.

**Secrets are write-only.** No read endpoint may ever return a stored token, even
to an authenticated caller. The test asserts against the real handler rather than
inspecting the code, so a future refactor that starts echoing config wholesale is
caught.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import routes


class TestRouteRegistration(unittest.IsolatedAsyncioTestCase):
    async def test_all_routes_are_namespaced_under_the_app(self):
        """A builtin registering outside its own namespace would shadow core APIs."""
        app = web.Application()
        routes.register_routes(app)
        paths = [
            resource.canonical
            for resource in app.router.resources()
            if getattr(resource, "canonical", "")
        ]
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(
                path.startswith("/api/apps/ops-mission-control"),
                f"route escapes the app namespace: {path}",
            )

    async def test_expected_surface_is_present(self):
        app = web.Application()
        routes.register_routes(app)
        paths = {
            resource.canonical
            for resource in app.router.resources()
            if getattr(resource, "canonical", "")
        }
        base = "/api/apps/ops-mission-control"
        for suffix in (
            "/state",
            "/incidents",
            "/incident",
            "/incident/transition",
            "/incident/claim",
            "/incident/action",
            "/signals",
            "/providers",
            "/rotation",
            "/ledger",
            "/webhook",
        ):
            self.assertIn(base + suffix, paths)


class TestEnabledGate(unittest.IsolatedAsyncioTestCase):
    """Every handler must refuse while the app is disabled."""

    @staticmethod
    async def _invoke(handler: routes.Handler, *, enabled: bool) -> web.StreamResponse:
        """Drive a gated handler with the app enabled or disabled."""
        request = mock.MagicMock(spec=web.Request)
        with mock.patch.object(routes, "is_app_enabled", return_value=enabled):
            wrapped = routes._require_enabled(handler)
            return await wrapped(request)

    async def test_disabled_app_returns_403(self):
        async def _never_called(_request: web.Request) -> web.StreamResponse:
            raise AssertionError("handler ran while the app was disabled")

        response = await self._invoke(_never_called, enabled=False)
        self.assertEqual(response.status, 403)

    async def test_enabled_app_reaches_the_handler(self):
        async def _ok(_request: web.Request) -> web.StreamResponse:
            return web.json_response({"ok": True})

        response = await self._invoke(_ok, enabled=True)
        self.assertEqual(response.status, 200)

    async def test_every_registered_handler_is_gated(self):
        """Catches a new route added without the gate."""
        app = web.Application()
        routes.register_routes(app)
        ungated = []
        for resource in app.router.resources():
            for route in resource:
                handler = route.handler
                # ``_require_enabled`` uses functools.wraps, so a gated handler
                # carries __wrapped__ pointing at the real implementation.
                if not hasattr(handler, "__wrapped__"):
                    ungated.append(getattr(resource, "canonical", str(resource)))
        self.assertEqual(ungated, [], f"ungated routes: {ungated}")


class TestSecretsAreWriteOnly(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import os

        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        from kiro_crew.config import loader

        for name in ("config_dir", "_config_dir"):
            fn = getattr(loader, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        from kiro_crew.config import loader

        for name in ("config_dir", "_config_dir"):
            fn = getattr(loader, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_provider_listing_never_contains_a_token(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry, secrets

        secret_value = "u+ThisIsTheActualSecretValue"
        secrets.put_secret("pagerduty", "api_token", secret_value)
        registry.reset_registry()
        try:
            listing = [routes._provider_dict(p) for p in registry.get_registry().catalog()]
            # ``ensure_ascii=False`` so the placeholder's bullets are not escaped
            # to • — we are asserting on content, not on JSON encoding.
            payload = json.dumps(listing, ensure_ascii=False)
            self.assertNotIn(secret_value, payload)
            self.assertNotIn("ThisIsTheActualSecretValue", payload)

            # ...but the UI must still learn that the field IS set.
            pagerduty = next(p for p in listing if p["id"] == "pagerduty")
            self.assertEqual(pagerduty["secrets"]["api_token"], secrets.REDACTED_PLACEHOLDER)

            # An unset field reports empty, so the UI can distinguish the two.
            datadog = next(p for p in listing if p["id"] == "datadog")
            self.assertEqual(datadog["secrets"]["api_key"], "")
        finally:
            registry.reset_registry()


class TestIncidentsPayloadIsBounded(unittest.IsolatedAsyncioTestCase):
    """`/incidents` used to serialize the ENTIRE index on every dashboard poll.

    Fine at three incidents. Once a flapping alarm has minted hundreds — which became
    possible when resolved alarms were made re-claimable — it is an ever-growing payload
    on a polled endpoint.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _get_incidents(self):
        request = mock.MagicMock(spec=web.Request)
        request.query = {}
        response = await routes._handle_incidents(request)
        return json.loads(getattr(response, "text", "{}") or "{}")

    async def test_response_is_capped_and_says_when_it_truncated(self):
        """Silent truncation is how someone concludes an incident vanished."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        over = routes.MAX_INCIDENTS_RESPONSE + 5
        for n in range(over):
            inc = store.claim(
                models.Signal.create(
                    source="cloudwatch", native_id=f"alarm/{n}", title="t", resource="r"
                ),
                operating_mode=models.MODE_OBSERVE,
            )
            assert inc is not None

        payload = await self._get_incidents()
        self.assertEqual(len(payload["incidents"]), routes.MAX_INCIDENTS_RESPONSE)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["total"], over)

    async def test_a_small_board_is_not_marked_truncated(self):
        """The common case must carry no scary flag."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        store.claim(
            models.Signal.create(
                source="cloudwatch", native_id="alarm/one", title="t", resource="r"
            ),
            operating_mode=models.MODE_OBSERVE,
        )
        payload = await self._get_incidents()
        self.assertEqual(len(payload["incidents"]), 1)
        self.assertNotIn("truncated", payload)


class TestLedgerHygieneWiring(unittest.IsolatedAsyncioTestCase):
    """The daily pass is where the git-native memory loop gets its only caller.

    ``ledger_sync`` and ``ledger_index.import_pending`` were both built and tested and
    wired to NOTHING: sync had no caller anywhere, and ``dispatch``'s semantic recall
    queried an index nothing ever populated — so on a real install recall returned zero
    hits forever while every unit test passed. These tests exist so that cannot recur.

    The other property pinned here is ORDER. pull → hygiene → index → push is not
    cosmetic: deduping before the merge leaves freshly-arrived duplicates for tomorrow,
    indexing before hygiene embeds rows about to be pruned, and pushing before hygiene
    makes every instance re-derive the same dedupe locally so the repo never converges.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    async def _run(*, calls=None, sync_result="", index_result=None, hygiene=None):
        """Drive the handler with sync and indexing stubbed, recording the call order."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, ledger_sync

        order = calls if calls is not None else []

        async def _sync(*, direction="pull"):
            order.append(f"sync:{direction}")
            return sync_result

        def _hygiene():
            order.append("hygiene")
            return hygiene if hygiene is not None else {"deduped": 0, "decayed": 0, "pruned": 0}

        def _index():
            order.append("index")
            return index_result or {"scanned": 0, "written": 0, "skipped": 0, "embedded": 0}

        request = mock.MagicMock(spec=web.Request)
        with mock.patch.object(ledger_sync, "sync_safely", _sync):
            with mock.patch.object(ledger, "hygiene", _hygiene):
                with mock.patch.object(routes, "_index_ledger_safely", _index):
                    response = await routes._handle_ledger_hygiene(request)
        return response, order

    async def test_the_pass_runs_sync_hygiene_and_index(self):
        """Regression: all three were unreachable on a real install."""
        response, order = await self._run()
        self.assertEqual(response.status, 200)
        self.assertIn("sync:pull", order)
        self.assertIn("hygiene", order)
        self.assertIn("index", order)
        self.assertIn("sync:push", order)

    async def test_stage_order_is_pull_hygiene_index_push(self):
        _, order = await self._run()
        self.assertEqual(order, ["sync:pull", "hygiene", "index", "sync:push"])

    async def test_a_pull_that_brought_news_marks_the_pass_changed(self):
        """``changed`` decides whether the cron speaks. A teammate's lesson arriving
        changes what the agent knows tomorrow, so it is worth saying."""
        response, _ = await self._run(sync_result="pulled")
        self.assertTrue(json.loads(getattr(response, "text", "{}") or "{}")["changed"])

    async def test_newly_indexed_rows_mark_the_pass_changed(self):
        response, _ = await self._run(
            index_result={"scanned": 5, "written": 5, "skipped": 0, "embedded": 5}
        )
        self.assertTrue(json.loads(getattr(response, "text", "{}") or "{}")["changed"])

    async def test_a_quiet_pass_stays_silent(self):
        """Silence-by-default is a hard requirement — the cron must not speak daily
        just because it ran."""
        response, _ = await self._run()
        self.assertFalse(json.loads(getattr(response, "text", "{}") or "{}")["changed"])

    async def test_hygiene_alone_still_marks_changed(self):
        response, _ = await self._run(hygiene={"deduped": 2, "decayed": 0, "pruned": 1})
        self.assertTrue(json.loads(getattr(response, "text", "{}") or "{}")["changed"])

    async def test_the_response_reports_each_stage_separately(self):
        """An operator debugging "why is recall empty" needs to see WHICH stage did
        nothing — a single boolean cannot distinguish "no remote" from "no model"."""
        response, _ = await self._run(
            sync_result="pulled",
            index_result={"scanned": 3, "written": 3, "skipped": 0, "embedded": 3},
        )
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertIn("sync", payload)
        self.assertIn("pull", payload["sync"])
        self.assertIn("push", payload["sync"])
        self.assertEqual(payload["index"]["written"], 3)
        self.assertIn("summary", payload)

    async def test_indexing_failure_does_not_lose_the_hygiene_pass(self):
        """Local dedupe is the part that always works and always matters; a missing
        embedding model must not cost it."""
        # Import ledger_index explicitly and patch the MODULE OBJECT. A dotted-path
        # patch target fails in a fresh process ("module ... has no attribute
        # 'ledger_index'"): the package attribute only exists once something has
        # imported the submodule, and routes imports it lazily inside the handler.
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            ledger,
            ledger_index,
            ledger_sync,
        )

        async def _sync(*, direction="pull"):
            return ""

        def _boom(*_a, **_kw):
            raise RuntimeError("no embedding model")

        request = mock.MagicMock(spec=web.Request)
        with mock.patch.object(ledger_sync, "sync_safely", _sync):
            with mock.patch.object(ledger, "hygiene", return_value={"deduped": 1}):
                # Not stubbing _index_ledger_safely: this exercises its real
                # swallow-everything contract rather than asserting it exists.
                with mock.patch.object(ledger_index, "import_pending", _boom):
                    response = await routes._handle_ledger_hygiene(request)
        self.assertEqual(response.status, 200)
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertEqual(payload["summary"]["deduped"], 1, "hygiene still ran")
        self.assertEqual(payload["index"]["written"], 0)

    async def test_unconfigured_sync_is_not_an_error(self):
        """The single-user case: no remote, and nothing scary in the response."""
        response, _ = await self._run(sync_result="")
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["sync"]["pull"], "")

    async def test_index_helper_survives_having_no_vector_store(self):
        """Exercises the real helper with no store available."""
        with mock.patch(
            "kiro_crew.vector_memory.VectorMemoryStore",
            side_effect=RuntimeError("faiss unavailable"),
        ):
            result = routes._index_ledger_safely()
        self.assertEqual(result, {"scanned": 0, "written": 0, "skipped": 0, "embedded": 0})


if __name__ == "__main__":
    unittest.main()
