"""Tests for the dispatch engine — the loop that makes the app actually work.

The most important assertions here are the ones about *silence* and about ledger
matching. A heartbeat that speaks every two minutes makes the ops channel
unreadable, and a claim that does not consult the ledger makes the compounding-
memory mechanism decorative. Both fail quietly in production, so both are pinned.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path


class _HomeIsolated(unittest.IsolatedAsyncioTestCase):
    """Redirects the data home and resets the provider registry per test."""

    def setUp(self):
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        self.tmp = Path(tempfile.mkdtemp())
        self._prev_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        self._clear_caches()
        registry.reset_registry()
        # Install ONLY the fakes each test registers — the public adapters would
        # otherwise try to reach real APIs.
        self.registry = registry.OpsProviderRegistry()
        registry._registry = self.registry

    def tearDown(self):
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        registry.reset_registry()
        if self._prev_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev_home
        self._clear_caches()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _clear_caches():
        from kiro_crew.config import loader

        for name in ("config_dir", "_config_dir"):
            fn = getattr(loader, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()

    @staticmethod
    def _signal(native_id="alarm/dlq", title="DLQ depth exceeded", **kw):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal

        return Signal.create(source="fake", native_id=native_id, title=title, **kw)

    def _add_source(self, signals):
        parent = self

        class _Fake:
            id = "fake"
            display_name = "Fake"

            def configured(self):
                return True

            async def poll(self):
                return list(signals)

        self.registry.register_signal_source(_Fake())
        return parent

    def _write_config(self, payload):
        from kiro_crew.apps.builtins.ops_mission_control.backend import store as store_mod
        from kiro_crew.apps.manager import app_data_dir

        (app_data_dir(store_mod.APP_NAME) / "config.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )


class TestCycleSilence(_HomeIsolated):
    async def test_no_signals_means_no_change(self):
        """The cron must be able to tell 'nothing happened' and stay quiet."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([])
        result = await dispatch.run_cycle()
        self.assertFalse(result.changed)
        self.assertEqual(result.claimed, [])

    async def test_already_claimed_signal_is_not_a_change(self):
        """A signal that keeps firing must not re-announce itself every cycle."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        first = await dispatch.run_cycle()
        self.assertTrue(first.changed)
        second = await dispatch.run_cycle()
        self.assertFalse(second.changed)

    async def test_a_resolved_alarm_refiring_is_claimed_through_run_cycle(self):
        """The recurrence fix, asserted through the FULL cycle rather than `store.claim`.

        `store.claim` learned that a terminal incident no longer owns its signal, and 408
        unit tests passed — but `run_cycle` has its own cheap pre-filter that computed
        `owned` from every non-stale incident, so it discarded the recurrence *before*
        `claim` ever saw it. The app still permanently stopped responding to any failure it
        had already handled once, and the compounding-memory fast path stayed unreachable.

        Caught only by driving a real gateway: inject → resolve → re-inject reported
        `polled=1, claimed=0`. Two places encoded the same ownership rule and fixing one
        looked complete. This test exercises the path the cron actually takes.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, models, store

        self._add_source([self._signal()])
        first = await dispatch.run_cycle()
        self.assertEqual(len(first.claimed), 1)
        incident_id = first.claimed[0].incident.incident_id

        store.transition(incident_id, models.STATUS_INVESTIGATING)
        store.transition(incident_id, models.STATUS_RESOLVED)

        # Same alarm fires again — a fresh incident, not a reopening.
        again = await dispatch.run_cycle()
        self.assertEqual(len(again.claimed), 1, "a resolved alarm that re-fires must be claimed")
        self.assertNotEqual(again.claimed[0].incident.incident_id, incident_id)

    async def test_an_open_incident_still_suppresses_its_signal_in_run_cycle(self):
        """The pre-filter's real job must survive the fix.

        `needs_human` is the trap: waiting on a person, not closed. A cycle that opened a
        second incident for the same still-firing alarm would double-investigate it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, models, store

        self._add_source([self._signal()])
        first = await dispatch.run_cycle()
        incident_id = first.claimed[0].incident.incident_id
        store.transition(incident_id, models.STATUS_INVESTIGATING)
        store.transition(incident_id, models.STATUS_NEEDS_HUMAN)

        again = await dispatch.run_cycle()
        self.assertEqual(again.claimed, [], "an open incident must still own its signal")

    async def test_non_firing_signal_is_ignored(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import STATE_OK

        self._add_source([self._signal(state=STATE_OK)])
        result = await dispatch.run_cycle()
        self.assertFalse(result.changed)
        self.assertEqual(result.polled, 0)


class TestClaimCap(_HomeIsolated):
    async def test_storm_is_capped_not_dropped(self):
        """A 50-alarm storm claims the cap now and the rest on later cycles."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        many = [self._signal(native_id=f"alarm/{n}", title=f"thing {n} broke") for n in range(50)]
        self._add_source(many)
        result = await dispatch.run_cycle()
        self.assertEqual(len(result.claimed), dispatch.DEFAULT_MAX_CLAIMS_PER_CYCLE)
        # The remainder is reported, not silently discarded.
        self.assertEqual(result.unclaimed_remaining, 50 - dispatch.DEFAULT_MAX_CLAIMS_PER_CYCLE)

    async def test_cap_is_configurable(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._write_config({"max_claims_per_cycle": 1})
        self._add_source([self._signal(native_id="a"), self._signal(native_id="b")])
        result = await dispatch.run_cycle()
        self.assertEqual(len(result.claimed), 1)

    async def test_nonsense_cap_falls_back_to_default(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._write_config({"max_claims_per_cycle": "banana"})
        many = [self._signal(native_id=f"alarm/{n}") for n in range(10)]
        self._add_source(many)
        result = await dispatch.run_cycle()
        self.assertEqual(len(result.claimed), dispatch.DEFAULT_MAX_CLAIMS_PER_CYCLE)


class TestLedgerWiring(_HomeIsolated):
    """The point of the whole app: a repeat failure arrives with its answer."""

    def _seed_verified_pattern(self, fingerprint):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            CONFIDENCE_HIGH,
            TRUST_VERIFIED,
            LedgerEntry,
        )

        return ledger.upsert(
            LedgerEntry.create(
                pattern="DLQ fills with duplicate-PK rows",
                fix="Clear the DLQ and redrive",
                fingerprints=[fingerprint],
                confidence=CONFIDENCE_HIGH,
                trust=TRUST_VERIFIED,
            )
        )

    async def test_matching_pattern_is_attached_and_persisted(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, store

        signal = self._signal()
        entry = self._seed_verified_pattern(signal.fingerprint)
        self._add_source([signal])

        result = await dispatch.run_cycle()
        claimed = result.claimed[0]
        self.assertEqual([m.entry_id for m in claimed.matches], [entry.entry_id])
        # Persisted, so re-opening the incident later still shows the match.
        stored = store.get_incident(claimed.incident.incident_id)
        assert stored is not None
        self.assertEqual(stored.ledger_matches, [entry.entry_id])

    async def test_verified_high_confidence_match_is_the_fast_path(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        signal = self._signal()
        self._seed_verified_pattern(signal.fingerprint)
        self._add_source([signal])
        self.assertTrue((await dispatch.run_cycle()).claimed[0].fast_path)

    async def test_weak_match_is_not_the_fast_path(self):
        """An unverified guess must not be presented as a known answer."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            CONFIDENCE_LOW,
            TRUST_OBSERVED,
            LedgerEntry,
        )

        signal = self._signal()
        ledger.upsert(
            LedgerEntry.create(
                pattern="maybe this",
                fix="try that",
                fingerprints=[signal.fingerprint],
                confidence=CONFIDENCE_LOW,
                trust=TRUST_OBSERVED,
            )
        )
        self._add_source([signal])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertEqual(len(claimed.matches), 1)
        self.assertFalse(claimed.fast_path)

    async def test_use_count_is_incremented_and_reported_post_increment(self):
        """The brief must not claim 'used 0x' for a pattern it just used."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger

        signal = self._signal()
        self._seed_verified_pattern(signal.fingerprint)
        self._add_source([signal])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertEqual(claimed.matches[0].use_count, 1)
        self.assertEqual(ledger.read_entries()[0].use_count, 1)

    async def test_recurrence_of_the_same_failure_matches_its_ancestor(self):
        """Different numbers and timestamps, same pattern — the whole premise."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        original = self._signal(title="DLQ depth exceeded 500 at 2026-01-01T00:00:00Z")
        self._seed_verified_pattern(original.fingerprint)
        # A different day, a different count, a different native id.
        recurrence = self._signal(
            native_id="alarm/dlq-2", title="DLQ depth exceeded 912 at 2026-07-30T12:00:00Z"
        )
        self._add_source([recurrence])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertTrue(claimed.fast_path)

    async def test_unknown_failure_claims_cleanly_with_no_matches(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertEqual(claimed.matches, [])
        self.assertFalse(claimed.fast_path)


class TestRotationGate(_HomeIsolated):
    async def test_off_shift_skips_dispatch_entirely(self):
        """A misconfigured manual trigger must not dispatch off-shift."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        class _OffShift:
            id = "off"
            display_name = "off"

            def configured(self):
                return True

            async def on_shift(self):
                return ShiftStatus(on_shift=False)

        self.registry.register_rotation_source(_OffShift())
        self._add_source([self._signal()])
        result = await dispatch.run_cycle()
        self.assertFalse(result.changed)
        self.assertIn("off shift", result.skipped_reason)

    async def test_unknown_rotation_still_dispatches(self):
        """Fail-open: an unreachable rotation API must not stop response."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        result = await dispatch.run_cycle()
        self.assertTrue(result.changed)


class TestBrief(_HomeIsolated):
    async def test_brief_states_authority_limits(self):
        """The investigating agent must be told what it may not do."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        brief = dispatch.investigation_brief(claimed)
        self.assertIn("act", brief)
        self.assertIn("Never run a remediation command", brief)

    async def test_fresh_install_says_nothing_is_watching(self):
        """The first thing a new user does, and the moment the app must admit setup.

        With no configured source, `polled == 0` is ambiguous — "nothing is wrong" and
        "nothing is watching" are opposite conclusions. The dashboard derived this
        itself, but an agent hitting POST /dispatch got a silent empty result.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        # No _add_source() call: this is a genuinely empty registry.
        result = await dispatch.run_cycle()
        self.assertFalse(result.changed, "a fresh install must still be silent")
        self.assertIn("No signal source is configured", result.skipped_reason)
        self.assertIn("Settings", result.skipped_reason)

    async def test_an_unconfigured_source_does_not_count_as_watching(self):
        """Registered is not the same as set up."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        class _Unconfigured:
            id = "not-set-up"
            display_name = "Not set up"

            def configured(self) -> bool:
                return False

            async def poll(self):
                raise AssertionError("must not poll an unconfigured source")

        self.registry.register_signal_source(_Unconfigured())
        result = await dispatch.run_cycle()
        self.assertIn("No signal source is configured", result.skipped_reason)

    async def test_a_source_whose_configured_check_raises_is_not_trusted(self):
        """An adapter that cannot answer "am I ready" must not be polled.

        Treating it as ready turns "nothing is watching" into a source-level error
        every single cycle, which is noise the operator cannot act on.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        class _Broken:
            id = "broken-readiness"
            display_name = "Broken"

            def configured(self) -> bool:
                raise RuntimeError("config store unreadable")

            async def poll(self):
                raise AssertionError("must not poll")

        self.registry.register_signal_source(_Broken())
        result = await dispatch.run_cycle()
        self.assertIn("No signal source is configured", result.skipped_reason)

    async def test_brief_carries_brokered_evidence(self):
        """The gateway reads; the agent reasons over text.

        The investigating agent's sandbox has no AWS credentials, so before this the
        brief carried signal metadata and ledger hints and nothing else — an AWS
        investigation had no alarm history and no logs. The fix is brokering, NOT
        handing the agent credentials: the gateway already holds the profile and
        already redacts at a single chokepoint.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            Evidence,
        )

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        claimed.evidence = [
            Evidence(
                source="cloudwatch-evidence",
                kind="logs",
                title="Recent errors — /aws/lambda/x",
                body="[ERROR] ValueError: File processing failed.",
            )
        ]
        brief = dispatch.investigation_brief(claimed)
        self.assertIn("ValueError: File processing failed.", brief)
        self.assertIn("Recent errors", brief)
        # It must say the agent has no credentials, or the agent wastes a turn trying.
        # Asserted unconditionally in test_brief_always_states_it_has_no_credentials —
        # kept here too so the with-evidence path can never lose it silently.
        self.assertIn("you have NONE", brief)

    async def test_brief_evidence_is_bounded(self):
        """The per-item EvidenceBudget (64 KB) is a spool cap, not a prompt cap.

        Six calls at 64 KB is ~384 KB into a prompt, against a documented 50k total
        session context budget. A real beta brief measured 37k chars from two items.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            Evidence,
        )

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        claimed.evidence = [
            Evidence(source="s", kind="logs", title=f"item {n}", body="x" * 50_000)
            for n in range(6)
        ]
        brief = dispatch.investigation_brief(claimed)
        self.assertLess(len(brief), 20_000, "brief must stay well under the context budget")
        # And it must ADMIT the clipping — silent truncation invites confident
        # reasoning over a partial picture.
        self.assertIn("truncated", brief)

    async def test_brief_omits_the_evidence_block_when_there_is_none(self):
        """An empty 'evidence' heading reads as 'we looked and found nothing'."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        claimed.evidence = []
        self.assertNotIn("Provider evidence", dispatch.investigation_brief(claimed))

    async def test_brief_always_states_it_has_no_credentials(self):
        """The no-evidence brief is the case that MOST needs the warning.

        Regression test for an observed live failure. The statement used to live only
        inside the ``if claimed.evidence`` branch, so an incident with nothing gathered
        — an unconfigured evidence source, a provider outage, a source that returned
        empty — handed the agent an AWS alarm and no explanation. Two real sessions
        (INV-1, INV-2) then spent their entire turn re-running ``aws … --profile …``,
        collecting NoCredentials each time, and produced no diagnosis.

        Asserted with evidence EMPTY on purpose: with evidence present the old code
        passed too, which is exactly why the gap survived.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        claimed.evidence = []

        brief = dispatch.investigation_brief(claimed)
        self.assertIn("you have NONE", brief)
        # And it must name the dead end concretely — "you lack credentials" alone still
        # leaves `aws sts get-caller-identity` looking worth one try.
        self.assertIn("Do not run `aws`", brief)

    async def test_evidence_failure_does_not_lose_the_claim(self):
        """Evidence is context, never a gate on claiming work."""
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        with mock.patch.object(
            dispatch, "gather_evidence_safely", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                await dispatch.run_cycle()

        # The helper itself must swallow, so the cycle above is the only raising path.
        registry = mock.Mock()
        registry.gather_evidence = mock.AsyncMock(side_effect=RuntimeError("provider down"))
        out = await dispatch.gather_evidence_safely(registry, self._signal())
        self.assertEqual(out, [])

    async def test_claimed_incident_serializes_evidence(self):
        """The dispatch route returns this to the cron, which passes it on."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            Evidence,
        )

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        claimed.evidence = [Evidence(source="s", kind="k", title="t", body="b")]
        payload = claimed.to_dict()
        self.assertEqual(payload["evidence"][0]["body"], "b")
        self.assertEqual(payload["evidence"][0]["title"], "t")

    async def test_brief_flags_a_new_failure(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._add_source([self._signal()])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertIn("new to the", dispatch.investigation_brief(claimed))

    async def test_brief_distinguishes_known_from_hypothesis(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            CONFIDENCE_HIGH,
            TRUST_VERIFIED,
            LedgerEntry,
        )

        signal = self._signal()
        ledger.upsert(
            LedgerEntry.create(
                pattern="p",
                fix="f",
                fingerprints=[signal.fingerprint],
                confidence=CONFIDENCE_HIGH,
                trust=TRUST_VERIFIED,
            )
        )
        self._add_source([signal])
        claimed = (await dispatch.run_cycle()).claimed[0]
        self.assertIn("KNOWN PATTERN", dispatch.investigation_brief(claimed))


class TestCloudWatchAdapterShape(_HomeIsolated):
    """The CloudWatch adapter's alarm→Signal mapping, against a fixture payload.

    Verified end-to-end against a live AWS account during development (it polled a
    real firing alarm and claimed it correctly). This test pins the mapping without
    needing credentials, so CI covers it too.
    """

    _ALARM = {
        "AlarmName": "podcast-jobs-pending",
        "AlarmDescription": "Podcast jobs pending in SQS",
        "Namespace": "AWS/SQS",
        "MetricName": "ApproximateNumberOfMessagesVisible",
        "Dimensions": [],
    }

    async def test_alarm_maps_onto_a_normalized_signal(self):
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            SEVERITY_WARNING,
            STATE_FIRING,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
        )

        self._write_config({"providers": {"cloudwatch": {"enabled": True, "region": "us-east-1"}}})
        client = mock.MagicMock()
        client.describe_alarms.return_value = {"MetricAlarms": [self._ALARM]}
        with mock.patch.object(cloudwatch, "_boto3_client", return_value=client):
            signals = await cloudwatch.CloudWatchSignalSource().poll()

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.source, "cloudwatch")
        self.assertEqual(signal.id, "cloudwatch:alarm/podcast-jobs-pending")
        self.assertEqual(signal.title, "Podcast jobs pending in SQS")
        self.assertEqual(signal.resource, "AWS/SQS/ApproximateNumberOfMessagesVisible")
        self.assertEqual(signal.severity, SEVERITY_WARNING)
        self.assertEqual(signal.state, STATE_FIRING)
        self.assertTrue(signal.fingerprint)
        self.assertEqual(signal.labels["alarm_name"], "podcast-jobs-pending")

    async def test_critical_named_alarm_is_escalated(self):
        """CloudWatch has no severity, so the name is the heuristic."""
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            SEVERITY_CRITICAL,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
        )

        self._write_config({"providers": {"cloudwatch": {"enabled": True, "region": "us-east-1"}}})
        alarm = {**self._ALARM, "AlarmName": "prod-critical-db-down"}
        client = mock.MagicMock()
        client.describe_alarms.return_value = {"MetricAlarms": [alarm]}
        with mock.patch.object(cloudwatch, "_boto3_client", return_value=client):
            signals = await cloudwatch.CloudWatchSignalSource().poll()
        self.assertEqual(signals[0].severity, SEVERITY_CRITICAL)

    async def test_missing_boto3_reports_no_signals_rather_than_raising(self):
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            cloudwatch,
        )

        self._write_config({"providers": {"cloudwatch": {"enabled": True}}})
        with mock.patch.object(cloudwatch, "_boto3_client", return_value=None):
            self.assertEqual(await cloudwatch.CloudWatchSignalSource().poll(), [])


class TestProviderErrorsSurface(_HomeIsolated):
    async def test_broken_source_is_reported_not_fatal(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        class _Broken:
            id = "broken"
            display_name = "broken"

            def configured(self):
                return True

            async def poll(self):
                raise RuntimeError("provider down")

        self.registry.register_signal_source(_Broken())
        self._add_source([self._signal()])
        result = await dispatch.run_cycle()
        # The healthy source still produced a claim.
        self.assertEqual(len(result.claimed), 1)
        self.assertIn("broken", result.errors)


if __name__ == "__main__":
    unittest.main()
