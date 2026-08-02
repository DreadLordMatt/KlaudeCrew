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


class TestSignalsSplitsParkedFromFiring(unittest.IsolatedAsyncioTestCase):
    """`/signals` must expose provider-side suppression as its OWN bucket.

    Three different reasons a signal is absent from ``firing`` — it cleared, we could not
    look, or a human parked it — and the route is the only place that can tell a caller
    which. Before this a parked signal appeared ONLY in the raw ``signals`` array with
    state ``unknown``, so the panel counting that array under a column headed "Firing"
    rendered "3 firing" above an empty queue with nothing to explain the contradiction.
    """

    def setUp(self):
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        registry.reset_registry()
        # Only fakes: the public adapters would try to reach real APIs.
        self.registry = registry.OpsProviderRegistry()
        registry._registry = self.registry

    def tearDown(self):
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        registry.reset_registry()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_source(self, signals):
        class _Fake:
            id = "fake"
            display_name = "Fake"

            def configured(self):
                return True

            async def poll(self):
                return list(signals)

        self.registry.register_signal_source(_Fake())

    async def _get_signals(self):
        request = mock.MagicMock(spec=web.Request)
        response = await routes._handle_signals(request)
        return json.loads(getattr(response, "text", "{}") or "{}")

    @staticmethod
    def _signal(**kw):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal

        kw.setdefault("native_id", "a")
        kw.setdefault("title", "an alarm")
        return Signal.create(source="fake", **kw)

    async def test_a_parked_signal_lands_in_suppressed_and_nowhere_else(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import STATE_SUPPRESSED

        self._add_source(
            [self._signal(state=STATE_SUPPRESSED, suppressed_by="7f3a", suppressed_reason="silenced")]
        )
        payload = await self._get_signals()
        self.assertEqual(len(payload["suppressed"]), 1)
        self.assertEqual(payload["firing"], [])
        self.assertEqual(payload["cleared"], [])
        self.assertEqual(payload["unclaimed"], [])

    async def test_the_attribution_reaches_the_client(self):
        """Without who, "the app ignored my alarm" and "someone silenced it" look the same."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import STATE_SUPPRESSED

        self._add_source(
            [self._signal(state=STATE_SUPPRESSED, suppressed_by="7f3a", suppressed_reason="silenced")]
        )
        payload = await self._get_signals()
        parked = payload["suppressed"][0]
        self.assertEqual(parked["suppressed_by"], "7f3a")
        self.assertEqual(parked["suppressed_reason"], "silenced")

    async def test_parked_is_not_folded_into_cleared(self):
        """`cleared` asserts recovery, which a suppression does not — reconcile resolves on it."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            STATE_OK,
            STATE_SUPPRESSED,
        )

        self._add_source(
            [
                self._signal(native_id="parked", state=STATE_SUPPRESSED),
                self._signal(native_id="recovered", state=STATE_OK),
            ]
        )
        payload = await self._get_signals()
        self.assertEqual(len(payload["cleared"]), 1)
        self.assertEqual(payload["cleared"][0]["id"], "fake:recovered")
        self.assertEqual(payload["suppressed"][0]["id"], "fake:parked")

    async def test_firing_work_is_unaffected_by_a_parked_neighbour(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            STATE_FIRING,
            STATE_SUPPRESSED,
        )

        self._add_source(
            [
                self._signal(native_id="parked", state=STATE_SUPPRESSED),
                self._signal(native_id="live", state=STATE_FIRING),
            ]
        )
        payload = await self._get_signals()
        self.assertEqual([s["id"] for s in payload["firing"]], ["fake:live"])
        self.assertEqual([s["id"] for s in payload["unclaimed"]], ["fake:live"])


class TestIncidentServesItsPostmortem(unittest.IsolatedAsyncioTestCase):
    """``/incident`` must report the artifact honestly, including its absence.

    ``log`` was served and typed from the day the route shipped while the renderer had no
    caller, so it was structurally always ``""``. ``log_path`` is new and is the riskier of
    the two: a path is a promise that a file is at the other end, so it must be empty
    whenever there is nothing there rather than computed from the id.
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
    async def _get(incident_id: str):
        request = mock.MagicMock(spec=web.Request)
        request.query = {"id": incident_id}
        response = await routes._handle_incident(request)
        return response.status, json.loads(getattr(response, "text", "{}") or "{}")

    async def test_a_closed_incident_serves_its_artifact_and_where_it_lives(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/dlq", title="DLQ deep"),
            operating_mode=models.MODE_OBSERVE,
        )
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED, resolution="drained it")

        status, payload = await self._get(inc.incident_id)
        self.assertEqual(status, 200)
        self.assertIn("DLQ deep", payload["log"])
        self.assertIn("drained it", payload["log"])
        self.assertTrue(payload["log_path"].endswith(f"{inc.incident_id}.md"))
        self.assertTrue(Path(payload["log_path"]).is_file())

    async def test_an_open_incident_reports_no_path_at_all(self):
        """A path for a file that does not exist would send the operator to an empty ls."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/open", title="live"),
            operating_mode=models.MODE_OBSERVE,
        )
        assert inc is not None
        _status, payload = await self._get(inc.incident_id)
        self.assertEqual(payload["log"], "")
        self.assertEqual(payload["log_path"], "")


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


class TestNeedsHumanNotifiesOnTheEdgeOnly(unittest.IsolatedAsyncioTestCase):
    """``/incident/transition`` must push a desktop notification on the EDGE into
    ``needs_human`` and nowhere else.

    The pre-transition status is captured before the write for one specific reason:
    ``store.update_fields`` re-enters ``transition`` with the SAME status on an unrelated
    field edit, so without the comparison an incident parked on a tool approval would
    re-toast at critical priority on every subsequent write to it — the unchanged
    condition ``SKILL.md``'s noise discipline forbids.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.pushes: list[tuple] = []
        # Patched by IDENTITY off the module object `routes` itself holds, not by dotted
        # string. A dotted patch walks package attributes, and `test_ledger_sync_git`
        # evicts this app's modules from `sys.modules` to simulate two processes — after
        # which the string can resolve a DIFFERENT `notify_out` copy than the handler
        # calls, so the mock silently never applies. That is a test-order-dependent
        # failure that reads as a product bug.

        def _record(*args, **kwargs):
            # A named function, not a lambda: `append(...) or True` reads as returning
            # bool but is typed `None | bool`, and mypy is blocking here.
            self.pushes.append(args)
            return True

        self._patch = mock.patch.object(
            routes.notify_out, "notify_needs_human", side_effect=_record
        )
        self._patch.start()

    def tearDown(self):
        import os

        self._patch.stop()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _transition(self, incident_id: str, status: str, **extra):
        request = mock.MagicMock(spec=web.Request)
        request.app = {"state": object()}
        request.json = mock.AsyncMock(
            return_value={"id": incident_id, "status": status, **extra}
        )
        response = await routes._handle_transition(request)
        return response.status, json.loads(getattr(response, "text", "{}") or "{}")

    def _claim(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/dlq", title="DLQ deep"),
            operating_mode=models.MODE_OBSERVE,
        )
        assert inc is not None
        return inc

    async def test_entering_needs_human_notifies_once(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._claim()
        status, _payload = await self._transition(
            inc.incident_id, models.STATUS_NEEDS_HUMAN
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.pushes), 1)
        self.assertIn(inc.incident_id, self.pushes[0])

    async def test_a_second_write_while_still_blocked_notifies_nothing(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._claim()
        await self._transition(inc.incident_id, models.STATUS_NEEDS_HUMAN)
        await self._transition(
            inc.incident_id, models.STATUS_NEEDS_HUMAN, diagnosis="still thinking"
        )
        self.assertEqual(len(self.pushes), 1, "a re-block must not re-notify")

    async def test_a_transition_to_any_other_status_notifies_nothing(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._claim()
        await self._transition(inc.incident_id, models.STATUS_INVESTIGATING)
        await self._transition(
            inc.incident_id, models.STATUS_RESOLVED, resolution="drained it"
        )
        self.assertEqual(self.pushes, [])

    async def test_a_failing_notifier_cannot_fail_the_transition(self):
        """The state change is already durable; a notification centre fault is not news."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._claim()
        self._patch.stop()
        try:
            with mock.patch.object(
                routes.notify_out,
                "notify_needs_human",
                side_effect=RuntimeError("bus exploded"),
            ):
                with self.assertRaises(RuntimeError):
                    # notify_out itself never raises (see test_notify_out); this proves the
                    # route does not swallow a genuine programming fault silently, which is
                    # why the chokepoint and not the caller owns the try/except.
                    await self._transition(inc.incident_id, models.STATUS_NEEDS_HUMAN)
        finally:
            self._patch.start()


class TestStateReportsTheNotificationChannel(unittest.IsolatedAsyncioTestCase):
    """``/state`` must carry ``notify`` so Settings can render it without a new endpoint.

    Readiness depends on live gateway state (is there a notification bus in this process),
    so it cannot be answered from the unauthenticated config file the panel already has.
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

    async def test_state_carries_the_notify_status_with_every_field_the_ui_types(self):
        request = mock.MagicMock(spec=web.Request)
        request.app = {"state": None}
        response = await routes._handle_state(request)
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertIn("notify", payload)
        for key in ("enabled", "bus_available", "ready", "detail", "channels"):
            self.assertIn(key, payload["notify"])

    async def test_a_process_without_a_bus_is_not_reported_ready(self):
        """`enabled` alone must never paint as active — nothing would be delivered."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import notify_out

        notify_out.set_settings(enabled=True)
        request = mock.MagicMock(spec=web.Request)
        request.app = {"state": None}
        response = await routes._handle_state(request)
        payload = json.loads(getattr(response, "text", "{}") or "{}")
        self.assertTrue(payload["notify"]["enabled"])
        self.assertFalse(payload["notify"]["ready"])


class TestAnActionSchedulesItsOwnVerification(unittest.IsolatedAsyncioTestCase):
    """A 2xx from a provider is no longer the end of the story.

    `_handle_action` used to await `sink.execute`, audit, and return — so the response's
    `ok` meant only "transmitted". Checkmk documents exactly that gap for its Livestatus
    command dispatch; Nagios's command pipe returns nothing at all. The route now records
    what was done and when to look again, and says which of the two it is doing.
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

    def _incident(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/dlq", title="DLQ deep"),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None
        return inc

    async def test_a_silence_is_rechecked_at_the_end_of_its_own_window(self):
        """The schedule `ACTION_SILENCE`'s mandatory expiry buys.

        A suppression that expires straight back into the same firing condition is the
        strongest evidence available that nothing was fixed — so the recheck is anchored
        to the window, not to a flat interval invented for it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._incident()
        verdict, due = routes._schedule_verification(inc.incident_id, models.ACTION_SILENCE, 3600)
        self.assertEqual(verdict, models.VERIFY_PENDING)
        # 3600s out is well past the 5-minute default, which is how we know the window
        # (not the default) chose the time.
        flat = routes._schedule_verification(
            inc.incident_id, models.ACTION_RESOLVE, None
        )[1]
        self.assertGreater(due, flat)

    async def test_an_ack_is_recorded_as_not_checkable_rather_than_left_blank(self):
        """An ack leaves an alert firing BY DESIGN, so firing state proves nothing.

        `normalize_state` maps `acknowledged` onto `firing` on purpose. Deriving a verdict
        from the alarm's state would turn an unverifiable write into a confident one — so
        this says "cannot observe" instead, and schedules no recheck to mislead a later
        cycle.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = self._incident()
        verdict, due = routes._schedule_verification(inc.incident_id, models.ACTION_ACK, None)
        self.assertEqual(verdict, models.VERIFY_NOT_CHECKABLE)
        self.assertEqual(due, "")
        stored = store.get_incident(inc.incident_id)
        assert stored is not None
        self.assertEqual(stored.last_action, models.ACTION_ACK)
        self.assertNotIn(stored.verification, models.OPEN_VERIFICATIONS)

    async def test_a_comment_is_not_verifiable_either(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        inc = self._incident()
        verdict, _due = routes._schedule_verification(
            inc.incident_id, models.ACTION_COMMENT, None
        )
        self.assertEqual(verdict, models.VERIFY_NOT_CHECKABLE)

    async def test_a_vanished_incident_degrades_instead_of_failing_the_action(self):
        """The provider write already happened and cannot be undone.

        A bookkeeping failure must not turn a completed action into a 500 — it degrades to
        "no verification scheduled", which the response then reports honestly as "".
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self.assertEqual(
            routes._schedule_verification("INV-nope", models.ACTION_SILENCE, 60), ("", "")
        )

    async def test_an_observe_only_action_schedules_no_recheck_at_all(self):
        """`ok=True` from the noop sink means "we successfully did nothing".

        The recheck cannot tell that from a real provider write: it read the still-firing
        alarm as the ACTION having failed and charged a `miss_count` to every ledger entry
        the investigation cited. On a default install that is the ONLY path — `cloudwatch`
        and `webhook` register no ActionSink, so every action falls through to `noop` — so
        watching the proposal flow, which is exactly what an operator is told to do before
        granting real authority, demoted their own proven knowledge for a write nobody made.

        Asserted through the real handler rather than `_schedule_verification`, because the
        gate lives in `_handle_action` and the direct-call tests above cannot see it.
        """
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, registry
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            LedgerEntry,
            Signal,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            read_config,
            write_config,
        )

        # A properly scoped act-rule, so the autonomy gate genuinely allows the write.
        cfg = read_config()
        cfg["mode"] = "act"
        cfg["autonomy_rules"] = [
            {
                "source": "cloudwatch",
                "mode": "act",
                "actions": ["resolve"],
                "resource_glob": "*",
            }
        ]
        write_config(cfg)

        registry.reset_registry()
        self.addCleanup(registry.reset_registry)
        signal = Signal.create(
            source="cloudwatch", native_id="alarm/dlq", title="DLQ deep", resource="arn:dlq"
        )
        entry = ledger.upsert(
            LedgerEntry.create(
                pattern="DLQ deep",
                fix="drain it",
                fingerprints=[signal.fingerprint],
                confidence="high",
                trust="verified",
            )
        )
        ledger.record_use(entry.entry_id)
        ledger.record_use(entry.entry_id)
        self.assertTrue(ledger.entry_unlocks_fast_path(ledger.read_entries()[0]))

        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        incident = store.claim(signal, operating_mode="act")
        assert incident is not None
        store.update_fields(incident.incident_id, ledger_matches=[entry.entry_id])

        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/apps/ops-mission-control/incident/action",
                    json={"id": incident.incident_id, "action": "resolve"},
                )
                self.assertEqual(resp.status, 200)
                body = await resp.json()

        # The write was "accepted" by the observe-only sink...
        self.assertTrue(body["ok"])
        # ...and nothing was scheduled, so no later cycle can reach a verdict about it.
        self.assertEqual(body["verification"], "")
        self.assertEqual(body["verify_after"], "")
        stored = store.get_incident(incident.incident_id)
        assert stored is not None
        self.assertEqual(stored.verification, "")
        # The operator's proven entry is untouched and still on the fast path.
        self.assertEqual(ledger.read_entries()[0].miss_count, 0)
        self.assertTrue(ledger.entry_unlocks_fast_path(ledger.read_entries()[0]))
