"""Tests for the dispatch index, the autonomy gate, and the ledger.

The claim tests cover the property that prevents duplicate investigations, and the
autonomy tests cover the property that prevents the agent writing to a stranger's
production tooling without an explicit, scoped grant. Both are the kind of thing
that fails silently in production if it regresses, which is why they are asserted
directly rather than through the HTTP layer.

``KIROCREW_HOME`` is redirected to a temp dir so nothing here touches the real
data home.
"""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class _HomeIsolated(unittest.TestCase):
    """Redirects the data home so store/ledger writes land in a temp dir."""

    def setUp(self):
        import os

        self.tmp = Path(tempfile.mkdtemp())
        self._prev_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        # config_dir() caches; clear the caches these modules read through.
        from kiro_crew.config import loader

        for candidate in ("config_dir", "_config_dir"):
            fn = getattr(loader, candidate, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()

    def tearDown(self):
        import os

        if self._prev_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev_home
        from kiro_crew.config import loader

        for candidate in ("config_dir", "_config_dir"):
            fn = getattr(loader, candidate, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _signal(native_id="alarm/x", title="something broke", source="cloudwatch", **kw):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        return models.Signal.create(source=source, native_id=native_id, title=title, **kw)


class TestClaim(_HomeIsolated):
    def test_claim_creates_incident(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        self.assertIsNotNone(inc)
        assert inc is not None
        self.assertEqual(inc.status, models.STATUS_DISPATCHED)
        self.assertTrue(inc.incident_id.startswith("INV-"))

    def test_second_claim_of_same_signal_loses(self):
        """Exactly one caller may own a signal — this is what stops duplicate
        investigations when two heartbeats overlap."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        sig = self._signal()
        first = store.claim(sig, operating_mode=models.MODE_OBSERVE)
        second = store.claim(sig, operating_mode=models.MODE_OBSERVE)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_stale_incident_is_reclaimable_in_place(self):
        """Re-pickup must reuse the timeline, not accumulate a new incident."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        sig = self._signal()
        first = store.claim(sig, operating_mode=models.MODE_OBSERVE)
        assert first is not None
        store.transition(first.incident_id, models.STATUS_STALE)
        again = store.claim(sig, operating_mode=models.MODE_OBSERVE)
        self.assertIsNotNone(again)
        assert again is not None
        self.assertEqual(again.incident_id, first.incident_id)
        self.assertEqual(again.status, models.STATUS_DISPATCHED)
        self.assertEqual(len(store.read_index()), 1)

    def test_a_resolved_alarm_that_refires_is_claimed_again(self):
        """The bug that made the app's whole premise unreachable.

        ``signal.id`` is stable for the alarm's lifetime
        (``cloudwatch:alarm/DlqDepth`` forever), and ``claim`` treated ANY existing
        incident — including a closed one — as "accounted for". So once an alarm was
        resolved, the app **permanently stopped responding to it**: proven live, a
        resolved alarm re-firing on days 2, 3 and 30 all returned ``None``.

        Worse, it made the compounding-memory fast path unreachable in production. That
        payoff can only happen on a SECOND occurrence, and a second occurrence could
        never be claimed — so the feature the app is built around could never fire.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        sig = self._signal()
        first = store.claim(sig, operating_mode=models.MODE_OBSERVE)
        assert first is not None
        store.transition(first.incident_id, models.STATUS_INVESTIGATING)
        store.transition(first.incident_id, models.STATUS_RESOLVED)

        again = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        self.assertIsNotNone(again, "a resolved alarm that fires again must be claimable")
        assert again is not None
        # A recurrence is a NEW incident, not a reopening: the first one owns its
        # diagnosis, resolution, and Slack thread, and overwriting those would destroy
        # the record that makes the ledger trustworthy.
        self.assertNotEqual(again.incident_id, first.incident_id)
        self.assertEqual(again.status, models.STATUS_DISPATCHED)
        closed = store.get_incident(first.incident_id)
        assert closed is not None
        self.assertEqual(closed.status, models.STATUS_RESOLVED, "history must survive")

    def test_an_escalated_alarm_that_refires_is_claimed_again(self):
        """Escalated is the other terminal state and must behave identically."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        first = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert first is not None
        store.transition(first.incident_id, models.STATUS_INVESTIGATING)
        store.transition(first.incident_id, models.STATUS_ESCALATED)
        self.assertIsNotNone(store.claim(self._signal(), operating_mode=models.MODE_OBSERVE))

    def test_an_open_incident_still_blocks_a_duplicate_claim(self):
        """The recurrence fix must NOT weaken the dedupe it sits next to.

        needs_human is the trap: it is waiting on a person, not closed, so a heartbeat
        must not open a second incident for the same still-firing alarm.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        for status in (
            models.STATUS_DISPATCHED,
            models.STATUS_INVESTIGATING,
            models.STATUS_NEEDS_HUMAN,
        ):
            with self.subTest(status=status):
                # A distinct alarm per subtest, so each case is independent without
                # needing to tear down and rebuild the data home.
                native = f"alarm/open-{status}"
                first = store.claim(self._signal(native), operating_mode=models.MODE_OBSERVE)
                assert first is not None
                if status != models.STATUS_DISPATCHED:
                    store.transition(first.incident_id, models.STATUS_INVESTIGATING)
                if status == models.STATUS_NEEDS_HUMAN:
                    store.transition(first.incident_id, models.STATUS_NEEDS_HUMAN)
                self.assertIsNone(
                    store.claim(self._signal(native), operating_mode=models.MODE_OBSERVE),
                    f"an incident in {status} must still own its signal",
                )

    def test_closed_incidents_are_pruned_but_open_work_never_is(self):
        """Bounds the index that making resolved alarms re-claimable just unbounded.

        A flapping alarm on the 2-minute dispatch cadence now mints one incident per
        flap, and every claim re-reads and re-writes the whole index — measured
        superlinear (50 entries → 6ms/claim, 450 → 53ms). A month of one flapping alarm
        projects to ~21,600 incidents. Open work is exempt at any age: live work
        disappearing because history is long would be far worse than a large index.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        # 12 closed, then 2 still open.
        for n in range(12):
            inc = store.claim(self._signal(f"alarm/closed-{n}"), operating_mode=models.MODE_OBSERVE)
            assert inc is not None
            store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
            store.transition(inc.incident_id, models.STATUS_RESOLVED)
        open_ids = []
        for n in range(2):
            inc = store.claim(self._signal(f"alarm/open-{n}"), operating_mode=models.MODE_OBSERVE)
            assert inc is not None
            open_ids.append(inc.incident_id)

        removed = store.prune_closed(keep=5)
        self.assertEqual(removed, 7)
        index = store.read_index()
        self.assertEqual(len(index), 7, "5 kept closed + 2 open")
        for incident_id in open_ids:
            self.assertIn(incident_id, index, "open work must survive pruning")

    def test_pruning_keeps_the_most_recently_closed(self):
        """Ordered by when it CLOSED, so a long-running incident that just finished is
        treated as recent rather than aged out by its claim time."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        last = None
        for n in range(6):
            inc = store.claim(self._signal(f"alarm/c-{n}"), operating_mode=models.MODE_OBSERVE)
            assert inc is not None
            store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
            last = store.transition(inc.incident_id, models.STATUS_RESOLVED)
        assert last is not None
        store.prune_closed(keep=2)
        self.assertIn(last.incident_id, store.read_index(), "newest close must be kept")

    def test_pruning_under_the_cap_changes_nothing(self):
        """The daily pass must stay silent when there is nothing to retire."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        self.assertEqual(store.prune_closed(keep=500), 0)
        self.assertEqual(len(store.read_index()), 1)

    def test_a_flapping_alarm_index_stays_bounded(self):
        """The scenario end to end: repeated flap, then one maintenance pass."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        for _ in range(25):
            inc = store.claim(self._signal("alarm/Flappy"), operating_mode=models.MODE_OBSERVE)
            assert inc is not None, "each flap opens a fresh incident"
            store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
            store.transition(inc.incident_id, models.STATUS_RESOLVED)
        self.assertEqual(len(store.read_index()), 25, "unbounded before maintenance")
        store.prune_closed(keep=10)
        self.assertEqual(len(store.read_index()), 10)

    def test_terminal_statuses_are_derived_from_the_grammar(self):
        """Derived, not hand-listed, so the two can never disagree.

        A status with no outgoing transition IS terminal by definition; keeping a second
        literal list would be one more thing to forget when a status is added.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self.assertEqual(
            models.TERMINAL_STATUSES,
            frozenset({models.STATUS_RESOLVED, models.STATUS_ESCALATED}),
        )
        for status in models.TERMINAL_STATUSES:
            self.assertEqual(models.LEGAL_TRANSITIONS[status], frozenset())
        # And nothing open may be misfiled as terminal.
        self.assertFalse(models.TERMINAL_STATUSES & models.OPEN_STATUSES)

    def test_ids_increment(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        a = store.claim(self._signal("alarm/a"), operating_mode=models.MODE_OBSERVE)
        b = store.claim(self._signal("alarm/b"), operating_mode=models.MODE_OBSERVE)
        assert a is not None and b is not None
        self.assertNotEqual(a.incident_id, b.incident_id)


class TestTransition(_HomeIsolated):
    def test_illegal_transition_refused(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        # Back to unclaimed is not legal: an incident that exists has been claimed by
        # definition, and "release it" is `stale`, not a rewind.
        #
        # This used to assert dispatched -> resolved, which is now a REQUIRED edge —
        # reconcile must close an incident whose signal cleared before the agent's
        # first turn. Note same-status is deliberately a no-op update (see
        # store.transition), so it is not a usable example of illegality either.
        with self.assertRaises(ValueError):
            store.transition(inc.incident_id, models.STATUS_UNCLAIMED)

    def test_terminal_state_cannot_transition(self):
        """A resolved incident is final; a signal that returns is a NEW incident."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        for target in (
            models.STATUS_INVESTIGATING,
            models.STATUS_DISPATCHED,
            models.STATUS_NEEDS_HUMAN,
        ):
            with self.assertRaises(ValueError):
                store.transition(inc.incident_id, target)

    def test_legal_path_works(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        final = store.transition(inc.incident_id, models.STATUS_RESOLVED, resolution="cleared")
        self.assertEqual(final.status, models.STATUS_RESOLVED)
        self.assertEqual(final.resolution, "cleared")

    def test_unknown_incident_raises_keyerror(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        with self.assertRaises(KeyError):
            store.transition("INV-999", models.STATUS_INVESTIGATING)

    def test_terminal_state_is_final(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        with self.assertRaises(ValueError):
            store.transition(inc.incident_id, models.STATUS_INVESTIGATING)

    def test_same_status_is_a_field_update_not_a_transition(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        updated = store.update_fields(inc.incident_id, diagnosis="pool exhaustion")
        self.assertEqual(updated.diagnosis, "pool exhaustion")
        self.assertEqual(updated.status, models.STATUS_DISPATCHED)


class TestStaleSweep(_HomeIsolated):
    @staticmethod
    def _backdate(incident_id: str, hours: int) -> None:
        """Age an incident by rewriting the index directly.

        ``transition``/``update_fields`` deliberately re-stamp ``updated_at`` —
        any transition IS activity — so a caller cannot backdate an incident.
        The sweep is therefore exercised by editing the persisted index.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        path = store.index_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data[incident_id]["updated_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_idle_incident_is_released(self):
        """A dead investigation must not hold a signal claimed forever."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        self._backdate(inc.incident_id, hours=5)
        released = store.sweep_stale(stale_after_secs=3600)
        self.assertIn(inc.incident_id, released)
        refreshed = store.get_incident(inc.incident_id)
        assert refreshed is not None
        self.assertEqual(refreshed.status, models.STATUS_STALE)

    def test_fresh_incident_survives(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        self.assertEqual(store.sweep_stale(stale_after_secs=3600), [])

    def test_investigating_incident_is_also_sweepable(self):
        """Both pre-terminal working states can strand a signal."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        self._backdate(inc.incident_id, hours=5)
        self.assertIn(inc.incident_id, store.sweep_stale(stale_after_secs=3600))

    def test_terminal_incident_is_never_swept(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        self._backdate(inc.incident_id, hours=99)
        self.assertEqual(store.sweep_stale(stale_after_secs=3600), [])


class TestAutonomyGate(_HomeIsolated):
    def _write_config(self, payload: dict) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import store as store_mod
        from kiro_crew.apps.manager import app_data_dir

        data_dir = app_data_dir(store_mod.APP_NAME)
        (data_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_default_is_observe(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self.assertEqual(rotation.app_mode(), models.MODE_OBSERVE)
        allowed, reason = rotation.authorize_action(self._signal(), models.ACTION_RESOLVE)
        self.assertFalse(allowed)
        self.assertIn("observe", reason)

    def test_act_mode_alone_is_not_enough(self):
        """Both an app-level ceiling AND a matching rule are required."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config({"mode": models.MODE_ACT})
        allowed, reason = rotation.authorize_action(self._signal(), models.ACTION_RESOLVE)
        self.assertFalse(allowed)
        self.assertIn("no matching act-rule", reason)

    def test_blanket_rule_is_refused(self):
        """'Act on everything from this provider' must not be expressible."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [{"source": "cloudwatch", "mode": models.MODE_ACT}],
            }
        )
        self.assertEqual(rotation.load_rules(), [])
        allowed, _ = rotation.authorize_action(self._signal(), models.ACTION_RESOLVE)
        self.assertFalse(allowed)

    def test_scoped_rule_grants(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "resource_glob": "AWS/SQS/*",
                    }
                ],
            }
        )
        matching = self._signal(resource="AWS/SQS/Messages")
        allowed, _ = rotation.authorize_action(matching, models.ACTION_RESOLVE)
        self.assertTrue(allowed)

    def test_scoped_rule_does_not_leak_to_other_resources(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "resource_glob": "AWS/SQS/*",
                    }
                ],
            }
        )
        other = self._signal(resource="AWS/RDS/DatabaseConnections")
        allowed, _ = rotation.authorize_action(other, models.ACTION_RESOLVE)
        self.assertFalse(allowed)

    def test_app_ceiling_clamps_a_permissive_rule(self):
        """A rule cannot escalate an instance the operator pinned to observe."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_OBSERVE,
                "autonomy_rules": [
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "resource_glob": "*",
                    }
                ],
            }
        )
        allowed, _ = rotation.authorize_action(
            self._signal(resource="anything"), models.ACTION_RESOLVE
        )
        self.assertFalse(allowed)

    def test_rule_can_narrow_which_actions(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "resource_glob": "AWS/SQS/*",
                        "actions": [models.ACTION_COMMENT],
                    }
                ],
            }
        )
        sig = self._signal(resource="AWS/SQS/Messages")
        self.assertTrue(rotation.authorize_action(sig, models.ACTION_COMMENT)[0])
        self.assertFalse(rotation.authorize_action(sig, models.ACTION_RESOLVE)[0])

    def test_unknown_action_refused(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        allowed, reason = rotation.authorize_action(self._signal(), "delete_everything")
        self.assertFalse(allowed)
        self.assertIn("unknown action", reason)

    def test_label_match_rule(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "label_match": {"env": "staging"},
                    }
                ],
            }
        )
        staging = self._signal(labels={"env": "staging"})
        prod = self._signal(labels={"env": "prod"})
        self.assertTrue(rotation.authorize_action(staging, models.ACTION_RESOLVE)[0])
        self.assertFalse(rotation.authorize_action(prod, models.ACTION_RESOLVE)[0])


class TestTierArming(_HomeIsolated):
    def test_a_failing_rotation_api_still_arms_the_tier(self):
        """Fail-open is preserved — but the SOURCE decides it, not the gate.

        This test used to construct ``on_shift=False, unknown=True`` and assert the tier
        armed anyway, which encoded ``on_shift or unknown`` into the gate. That silently
        defeated strict gating: a committed schedule that cannot say whether this operator
        is on call returns exactly that shape, and the ``or`` re-armed every teammate —
        verified before the fix (``on_shift=False`` yet ``dispatch armed=True``).

        The intent was always "an unreachable API must not disable response", and that is
        still true: ``pagerduty.on_shift`` returns ``on_shift=True, unknown=True`` on any
        failure, which is the shape asserted here. Two sources, two policies for "cannot
        tell", one gate that just reads ``on_shift``.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        # Exactly what pagerduty.py returns when its API cannot answer.
        states = rotation.tier_states(ShiftStatus(on_shift=True, unknown=True))
        self.assertTrue(states[rotation.TIER_ON_SHIFT])

    def test_an_indeterminate_schedule_disarms_the_tier(self):
        """The other half: a strict source's "cannot tell" must reach the crons.

        `resolve_now` returning ``on_shift=False`` is only meaningful if the tier map
        honours it. This is the assertion whose absence let the ``or`` survive.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        states = rotation.tier_states(ShiftStatus(on_shift=False, unknown=True))
        self.assertFalse(states[rotation.TIER_ON_SHIFT])
        self.assertTrue(states[rotation.TIER_ALWAYS], "rotation-check must survive to re-arm")

    def test_reconcile_is_on_shift_gated_not_always(self):
        """It mutates SHARED state, so exactly one instance may run it.

        `reconcile` POSTs `incident/transition` and rewrites the incident's Slack message.
        On the `always` tier every teammate raced to resolve the same incidents and edit
        the same thread — the shared-state mutation the single-owner model prevents, on the
        reconciliation path instead of the claim path. The source workflow gates its
        equivalent (`dispatch-snapshot`) the same way.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        on_shift = rotation.crons_for_tier(rotation.TIER_ON_SHIFT)
        self.assertIn("ops-mission-control/reconcile", on_shift)
        self.assertNotIn(
            "ops-mission-control/reconcile",
            rotation.crons_for_tier(rotation.TIER_ALWAYS),
        )
        # rotation-check must stay always-on, or nothing can re-arm the gated tier.
        self.assertEqual(
            rotation.crons_for_tier(rotation.TIER_ALWAYS), ("ops-mission-control/rotation-check",)
        )

    def test_off_shift_disarms(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        states = rotation.tier_states(ShiftStatus(on_shift=False))
        self.assertFalse(states[rotation.TIER_ON_SHIFT])
        # The always tier must stay armed or the instance cannot re-arm itself.
        self.assertTrue(states[rotation.TIER_ALWAYS])

    def test_rotation_check_is_on_the_always_tier(self):
        """If rotation-check were on-shift-gated, an off-shift instance could
        never turn itself back on."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self.assertIn(
            "ops-mission-control/rotation-check",
            rotation.crons_for_tier(rotation.TIER_ALWAYS),
        )

    def test_tier_cron_names_match_the_manifest(self):
        """Every tier cron must be a job the scheduler actually has.

        Derived from `app.json` rather than hardcoded, so adding or renaming a
        manifest cron fails here instead of silently producing a tier that
        pauses/resumes nothing. This shipped broken: `TIER_CRONS` carried bare
        `omc-*` names while registration namespaces them as
        `ops-mission-control/<name>`, so every pause/resume the rotation tier emitted
        targeted a job that did not exist and the whole tier mechanism was inert.
        """
        import json
        from pathlib import Path

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        manifest = json.loads(
            (Path(rotation.__file__).resolve().parents[1] / "app.json").read_text(encoding="utf-8")
        )
        app_name = manifest["name"]
        registered = {f"{app_name}/{c['name']}" for c in manifest.get("crons", [])}
        self.assertTrue(registered, "manifest must declare crons")

        tiered = {name for names in rotation.TIER_CRONS.values() for name in names}
        self.assertEqual(
            tiered,
            registered,
            "TIER_CRONS must name exactly the crons app registration creates",
        )

    def test_sop_frontmatter_cron_names_match_the_manifest(self):
        """Each SOP's `cron:` must name the job that actually runs it.

        The crons bind to SOPs by FILE PATH, so a stale name here breaks nothing at
        runtime — which is precisely why it is dangerous: it was the stale `omc-*`
        frontmatter that misled `TIER_CRONS` into naming crons that were never
        registered, silently disabling tier arming. Documentation that lies in the
        same direction as a real bug is worth a test.
        """
        import json
        from pathlib import Path

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        app_dir = Path(rotation.__file__).resolve().parents[1]
        manifest = json.loads((app_dir / "app.json").read_text(encoding="utf-8"))
        registered = {f"{manifest['name']}/{c['name']}" for c in manifest.get("crons", [])}

        sops = app_dir.parents[2] / "builtin_skills" / "ops-mission-control" / "sops"
        if not sops.is_dir():  # pragma: no cover - python-only checkout
            self.skipTest("builtin_skills not present in this checkout")

        declared = {}
        for sop in sorted(sops.glob("*.md")):
            for line in sop.read_text(encoding="utf-8").splitlines()[:8]:
                if line.startswith("cron:"):
                    value = line.split(":", 1)[1].strip()
                    if value and value != "null":
                        declared[sop.name] = value
                    break

        self.assertTrue(declared, "at least one SOP must declare a cron")
        for filename, cron_name in declared.items():
            self.assertIn(
                cron_name,
                registered,
                f"{filename} declares cron {cron_name!r}, which no manifest cron creates",
            )

    def test_describe_exposes_per_tier_crons(self):
        """The rotation-check SOP pauses `tier_crons.on_shift` and nothing else.

        Without this field the only cron list on the response is `armed_crons` — the
        flat union across armed tiers — and an agent told to pause "the armed crons"
        would pause `omc-rotation-check` itself (see the next test).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        described = rotation.describe(ShiftStatus(on_shift=True))
        self.assertIn("tier_crons", described)
        self.assertEqual(
            described["tier_crons"][rotation.TIER_ON_SHIFT],
            list(rotation.crons_for_tier(rotation.TIER_ON_SHIFT)),
        )
        # The on-shift list must NOT contain this job, or arming becomes one-way.
        self.assertNotIn(
            "ops-mission-control/rotation-check",
            described["tier_crons"][rotation.TIER_ON_SHIFT],
        )

    def test_armed_crons_off_shift_still_contains_rotation_check(self):
        """Pins WHY `tier_crons` exists, so the trap cannot silently return.

        `armed_crons` is the union of every armed tier, so off shift it legitimately
        still lists `omc-rotation-check` (an `always` job). That is correct for "what
        is running now" and catastrophic as a pause list: pausing it strands the
        instance with no way to re-arm, silently ending incident response. If this
        assertion ever fails, re-read the SOP before changing it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        described = rotation.describe(ShiftStatus(on_shift=False))
        self.assertIn("ops-mission-control/rotation-check", described["armed_crons"])
        self.assertNotIn("ops-mission-control/dispatch", described["armed_crons"])


class TestLedgerGitMerge(_HomeIsolated):
    """A git merge of two ledgers must be a dedupe, not a doubling.

    The design argument for content-addressed ids on append-only JSONL is that two
    people who learn the same lesson write the same id, so merging is safe. But git
    resolves that as BOTH lines present — and `read_entries` appended every line, so a
    shared lesson counted twice: inflated `stats()`, `match()` returning the same entry
    twice, and the handover digest listing one pattern as two.
    """

    @staticmethod
    def _duplicate_every_line() -> None:
        """What a git merge of two branches that each appended the entry leaves."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        path = ledger.ledger_path()
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        path.write_text("\n".join(lines + lines) + "\n", encoding="utf-8")

    def test_duplicate_lines_collapse_to_one_entry(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        ledger.upsert(LedgerEntry.create(pattern="shared lesson", fix="the fix"))
        self._duplicate_every_line()

        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1, "one lesson must read as one entry")
        self.assertEqual(ledger.stats()["total"], 1)

    def test_merge_takes_the_stronger_trust_and_confidence(self):
        """Learning a lesson again must never weaken what is known."""
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        weak = LedgerEntry.create(pattern="p", fix="f", confidence="low", trust="observed")
        ledger.upsert(weak)
        strong = LedgerEntry.create(pattern="p", fix="f", confidence="high", trust="verified")
        strong.use_count = 7
        # Same id, stronger record — exactly what the other branch appended.
        path = ledger.ledger_path()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(strong.to_dict()) + "\n")

        merged = ledger.read_entries()
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].confidence, "high")
        self.assertEqual(merged[0].trust, "verified")
        self.assertEqual(merged[0].use_count, 7, "use_count takes the max, not the last seen")

    def test_fingerprints_union_across_branches(self):
        """Two people hit the same failure on different resources.

        Losing one branch's fingerprint means that recurrence stops matching — the
        ledger keeps working while silently no longer recognizing half its own history.
        """
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        mine = LedgerEntry.create(pattern="p", fix="f", fingerprints=["fp-a"])
        ledger.upsert(mine)
        theirs = LedgerEntry.create(pattern="p", fix="f", fingerprints=["fp-b"])
        with ledger.ledger_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(theirs.to_dict()) + "\n")

        merged = ledger.read_entries()
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0].fingerprints), {"fp-a", "fp-b"})
        # And BOTH fingerprints still match the merged entry.
        self.assertTrue(ledger.match("fp-a"))
        self.assertTrue(ledger.match("fp-b"))

    def test_match_does_not_return_the_same_entry_twice(self):
        """A duplicated entry would be presented to the agent as two hypotheses."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        ledger.upsert(LedgerEntry.create(pattern="p", fix="f", fingerprints=["fp-x"]))
        self._duplicate_every_line()

        hits = ledger.match("fp-x")
        self.assertEqual(len(hits), 1)
        self.assertEqual(len({h.entry_id for h in hits}), 1)

    def test_distinct_lessons_are_not_collapsed(self):
        """Only identical pattern+fix shares an id; a different fix must survive."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        ledger.upsert(LedgerEntry.create(pattern="p", fix="fix one"))
        ledger.upsert(LedgerEntry.create(pattern="p", fix="fix two"))
        self.assertEqual(len(ledger.read_entries()), 2)

    def test_survives_an_unresolved_git_conflict(self):
        """A real divergent merge produces CONFLICT MARKERS, not a clean dedupe.

        The spec claimed content-addressed ids make a merge "a dedupe rather than a
        conflict". Verified against an actual `git merge` of two divergent ledgers: git
        emits `<<<<<<< HEAD` / `=======` / `>>>>>>>` because both branches appended to
        the same region. The ids ARE identical, so the *entries* dedupe correctly — but
        only if reading tolerates the markers and reconciles the duplicate.

        This is the state a user's working tree is actually in mid-merge, so the app
        must stay usable: markers skipped as malformed lines, every real entry kept,
        the shared lesson collapsed with its fingerprints unioned.
        """
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        shared_a = LedgerEntry.create(pattern="shared", fix="f", fingerprints=["fp-a"])
        shared_b = LedgerEntry.create(pattern="shared", fix="f", fingerprints=["fp-b"])
        self.assertEqual(shared_a.entry_id, shared_b.entry_id, "same lesson, same id")
        mine = LedgerEntry.create(pattern="only-mine", fix="f")
        theirs = LedgerEntry.create(pattern="only-theirs", fix="f")

        conflicted = "\n".join(
            [
                "<<<<<<< HEAD",
                json.dumps(shared_a.to_dict()),
                json.dumps(mine.to_dict()),
                "=======",
                json.dumps(shared_b.to_dict()),
                json.dumps(theirs.to_dict()),
                ">>>>>>> their-branch",
            ]
        )
        ledger.ledger_path().parent.mkdir(parents=True, exist_ok=True)
        ledger.ledger_path().write_text(conflicted + "\n", encoding="utf-8")

        entries = ledger.read_entries()
        self.assertEqual(len(entries), 3, "shared lesson collapses; both locals survive")
        patterns = {e.pattern for e in entries}
        self.assertEqual(patterns, {"shared", "only-mine", "only-theirs"})
        merged = [e for e in entries if e.pattern == "shared"][0]
        self.assertEqual(set(merged.fingerprints), {"fp-a", "fp-b"})

    def test_first_occurrence_keeps_its_position(self):
        """Callers rank by read order; a merge must not reshuffle the ledger."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        for n in range(3):
            ledger.upsert(LedgerEntry.create(pattern=f"p{n}", fix="f"))
        before = [e.pattern for e in ledger.read_entries()]
        self._duplicate_every_line()
        self.assertEqual([e.pattern for e in ledger.read_entries()], before)


class TestLedger(_HomeIsolated):
    def test_upsert_then_match(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(
            pattern="DLQ fills", fix="clear and redrive", fingerprints=["fp1"]
        )
        ledger.upsert(entry)
        matches = ledger.match("fp1")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].pattern, "DLQ fills")

    def test_upsert_same_content_dedupes(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["a"]))
        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["b"]))
        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(set(entries[0].fingerprints), {"a", "b"})

    def test_relearning_does_not_weaken_trust(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_OBSERVED,
                confidence=models.CONFIDENCE_LOW,
            )
        )
        entry = ledger.read_entries()[0]
        self.assertEqual(entry.trust, models.TRUST_VERIFIED)
        self.assertEqual(entry.confidence, models.CONFIDENCE_HIGH)

    def test_fast_path_requires_verified_and_high(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        weak = [
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_OBSERVED,
                confidence=models.CONFIDENCE_HIGH,
            )
        ]
        strong = [
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        ]
        self.assertFalse(ledger.is_fast_path(weak))
        self.assertTrue(ledger.is_fast_path(strong))

    def test_record_use_binds_new_fingerprint(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["a"])
        ledger.upsert(entry)
        updated = ledger.record_use(entry.entry_id, fingerprint="b")
        assert updated is not None
        self.assertEqual(updated.use_count, 1)
        self.assertIn("b", updated.fingerprints)

    def test_hygiene_decays_unused_confidence(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(pattern="p", fix="f", confidence=models.CONFIDENCE_HIGH)
        entry.last_used = (
            datetime.now(timezone.utc) - timedelta(days=ledger.DECAY_AFTER_DAYS + 10)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        ledger._write_all([entry])
        summary = ledger.hygiene()
        self.assertEqual(summary["decayed"], 1)
        self.assertEqual(ledger.read_entries()[0].confidence, models.CONFIDENCE_MEDIUM)

    def test_hygiene_is_a_noop_when_clean(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f"))
        summary = ledger.hygiene()
        self.assertEqual(summary["deduped"], 0)
        self.assertEqual(summary["decayed"], 0)
        self.assertEqual(summary["pruned"], 0)

    def test_malformed_line_is_skipped(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f"))
        with ledger.ledger_path().open("a", encoding="utf-8") as handle:
            handle.write("{ not json\n")
        self.assertEqual(len(ledger.read_entries()), 1)

    def test_match_on_empty_fingerprint_returns_nothing(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", fingerprints=[""]))
        self.assertEqual(ledger.match(""), [])


if __name__ == "__main__":
    unittest.main()


class TestContradictionDetection(_HomeIsolated):
    """The source workflow's consolidation SOP asks a leader to "resolve contradictions".

    Ours asked the same of the hygiene agent but gave it no tooling — finding them meant an
    O(n²) eye-scan across the whole ledger, which is both a token cost and the kind of
    search a model skims once the ledger is longer than a screenful.

    Detection is deterministic and belongs in Python. RESOLUTION deliberately does not:
    splitting a pattern requires knowing what the two causes actually are.
    """

    @staticmethod
    def _entry(pattern, fix, fingerprints, uses=0):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        entry = LedgerEntry.create(pattern=pattern, fix=fix, fingerprints=fingerprints)
        entry.use_count = uses
        return entry

    def test_two_fixes_for_one_fingerprint_is_a_contradiction(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("DLQ high", "reattach sqs:DeleteMessage", ["fp1"]))
        ledger.upsert(self._entry("DLQ high", "raise the visibility timeout", ["fp1"]))
        found = ledger.find_contradictions()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["fingerprint"], "fp1")

    def test_the_same_fix_twice_is_not_a_contradiction(self):
        """That is the duplicate case, which dedupe already merges by content id.

        Conflating the two would make every deduplicated pair look like a conflict and
        bury the real ones.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("failure A", "the one true fix", ["fp1"]))
        ledger.upsert(self._entry("failure A described differently", "the one true fix", ["fp1"]))
        self.assertEqual(ledger.find_contradictions(), [])

    def test_different_fingerprints_are_not_compared(self):
        """Two unrelated failures with different fixes are just two lessons."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("failure A", "fix A", ["fp1"]))
        ledger.upsert(self._entry("failure B", "fix B", ["fp2"]))
        self.assertEqual(ledger.find_contradictions(), [])

    def test_most_used_pairs_come_first(self):
        """A responder reviewing a long list must see what is actively misleading people
        before the speculative conflicts."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("rare failure", "rare fix one", ["rare"], uses=0))
        ledger.upsert(self._entry("rare failure", "rare fix two", ["rare"], uses=1))
        ledger.upsert(self._entry("hot failure", "hot fix one", ["hot"], uses=9))
        ledger.upsert(self._entry("hot failure", "hot fix two", ["hot"], uses=8))
        found = ledger.find_contradictions()
        self.assertEqual([r["fingerprint"] for r in found], ["hot", "rare"])

    def test_a_pair_sharing_two_fingerprints_is_reported_once(self):
        """Entries can share several fingerprints; the pair is still one conflict."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("failure", "fix one", ["fpA", "fpB"]))
        ledger.upsert(self._entry("failure", "fix two", ["fpA", "fpB"]))
        self.assertEqual(len(ledger.find_contradictions()), 1)

    def test_hygiene_reports_the_count_without_resolving_them(self):
        """Detected, never auto-resolved — deleting a fix that worked for somebody would
        make the next responder rediscover it."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("DLQ high", "fix one", ["fp1"]))
        ledger.upsert(self._entry("DLQ high", "fix two", ["fp1"]))
        summary = ledger.hygiene()
        self.assertEqual(summary["contradictions"], 1)
        self.assertEqual(len(ledger.read_entries()), 2, "both entries must survive")

    def test_an_empty_ledger_has_no_contradictions(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        self.assertEqual(ledger.find_contradictions(), [])
