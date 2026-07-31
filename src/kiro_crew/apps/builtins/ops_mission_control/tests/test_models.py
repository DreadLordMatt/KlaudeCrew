"""Tests for the Ops Mission Control data model.

The fingerprint tests are the important ones: fingerprint stability is what makes
the knowledge ledger work at all. If a fingerprint drifts per occurrence, a repeat
failure never matches its ancestor and the whole compounding-memory premise fails
silently — the app keeps working, it just stops learning.
"""

import unittest

from kiro_crew.apps.builtins.ops_mission_control.backend import models


class TestFingerprint(unittest.TestCase):
    def test_stable_across_timestamps_and_numbers(self):
        """The same failure tomorrow, with different numbers, is the same pattern."""
        a = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/rds",
            title="RDS connections above 800 at 2026-07-30T12:00:00Z",
            resource="AWS/RDS/DatabaseConnections",
        )
        b = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/rds",
            title="RDS connections above 950 at 2026-07-31T04:22:11Z",
            resource="AWS/RDS/DatabaseConnections",
        )
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_stable_across_instance_ids_and_uuids(self):
        a = models.Signal.create(
            source="datadog",
            native_id="monitor/1",
            title="ingest failed on i-0abc123def456789",
            resource="ingest",
        )
        b = models.Signal.create(
            source="datadog",
            native_id="monitor/1",
            title="ingest failed on i-0999888777666555",
            resource="ingest",
        )
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_distinct_failures_differ(self):
        a = models.Signal.create(
            source="cloudwatch", native_id="alarm/rds", title="RDS connections high"
        )
        b = models.Signal.create(
            source="cloudwatch", native_id="alarm/dlq", title="DLQ depth exceeded"
        )
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_same_title_different_source_differs(self):
        """Provider is part of identity — the same words mean different things."""
        a = models.Signal.create(source="cloudwatch", native_id="x", title="latency high")
        b = models.Signal.create(source="datadog", native_id="x", title="latency high")
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_signal_id_is_provider_scoped(self):
        s = models.Signal.create(source="pagerduty", native_id="incident/PABC", title="t")
        self.assertEqual(s.id, "pagerduty:incident/PABC")


class TestNormalization(unittest.TestCase):
    def test_severity_vocabularies(self):
        self.assertEqual(models.normalize_severity("P1"), models.SEVERITY_CRITICAL)
        self.assertEqual(models.normalize_severity("sev-2"), models.SEVERITY_WARNING)
        self.assertEqual(models.normalize_severity("low"), models.SEVERITY_INFO)
        self.assertEqual(models.normalize_severity("critical"), models.SEVERITY_CRITICAL)

    def test_unknown_severity_is_warning_not_critical(self):
        """An unparseable provider must not be able to manufacture top priority."""
        self.assertEqual(models.normalize_severity("banana"), models.SEVERITY_WARNING)
        self.assertEqual(models.normalize_severity(""), models.SEVERITY_WARNING)

    def test_unknown_state_is_unknown_not_firing(self):
        """An unparseable state must not create phantom work on the board."""
        self.assertEqual(models.normalize_state("banana"), models.STATE_UNKNOWN)
        self.assertEqual(models.normalize_state("triggered"), models.STATE_FIRING)
        self.assertEqual(models.normalize_state("resolved"), models.STATE_OK)


class TestEffectiveMode(unittest.TestCase):
    def test_rule_cannot_escalate_above_app_ceiling(self):
        """An operator pinned to observe cannot be overridden by a rule."""
        self.assertEqual(
            models.effective_mode(models.MODE_OBSERVE, models.MODE_ACT),
            models.MODE_OBSERVE,
        )

    def test_rule_narrows(self):
        self.assertEqual(
            models.effective_mode(models.MODE_ACT, models.MODE_PROPOSE),
            models.MODE_PROPOSE,
        )

    def test_no_rule_uses_app_default(self):
        self.assertEqual(models.effective_mode(models.MODE_ACT, None), models.MODE_ACT)

    def test_unknown_mode_falls_to_observe(self):
        self.assertEqual(models.effective_mode("nonsense", None), models.MODE_OBSERVE)


class TestTransitionGrammar(unittest.TestCase):
    def test_cannot_resolve_without_being_claimed(self):
        """Resolving requires a claim first, so every resolution has a timeline.

        Note this is narrower than "an investigation happened": a CLAIMED incident
        may resolve without one when its signal simply stopped firing (see
        ``test_dispatched_can_resolve_when_the_signal_clears``). What stays
        forbidden is resolving something that was never claimed at all, which would
        produce a resolution with no incident record behind it.
        """
        self.assertNotIn(
            models.STATUS_RESOLVED,
            models.LEGAL_TRANSITIONS[models.STATUS_UNCLAIMED],
        )

    def test_dispatched_can_resolve_when_the_signal_clears(self):
        """Reconcile's core case, and it had no legal move before.

        A signal can clear between the claim and the agent's first turn (a flapping
        alarm; a GitHub issue closed a minute later). Without this edge the
        incident sticks at ``dispatched`` until the stale sweep hours later, so the
        board asserts work is in progress on a problem that no longer exists.
        Found by exercising the reconcile SOP against a real cleared signal.
        """
        self.assertIn(
            models.STATUS_RESOLVED,
            models.LEGAL_TRANSITIONS[models.STATUS_DISPATCHED],
        )

    def test_stale_can_resolve_when_the_signal_clears(self):
        """Otherwise reconcile's only move is to re-dispatch a dead signal, spending
        a whole investigation to conclude nothing is wrong."""
        self.assertIn(
            models.STATUS_RESOLVED,
            models.LEGAL_TRANSITIONS[models.STATUS_STALE],
        )

    def test_terminal_states_have_no_exits(self):
        self.assertEqual(models.LEGAL_TRANSITIONS[models.STATUS_RESOLVED], frozenset())
        self.assertEqual(models.LEGAL_TRANSITIONS[models.STATUS_ESCALATED], frozenset())

    def test_stale_can_be_reclaimed(self):
        self.assertIn(models.STATUS_DISPATCHED, models.LEGAL_TRANSITIONS[models.STATUS_STALE])

    def test_every_status_has_a_rule(self):
        """A status with no entry would raise a KeyError in the store at runtime."""
        for status in (
            models.STATUS_UNCLAIMED,
            models.STATUS_DISPATCHED,
            models.STATUS_INVESTIGATING,
            models.STATUS_NEEDS_HUMAN,
            models.STATUS_RESOLVED,
            models.STATUS_ESCALATED,
            models.STATUS_STALE,
        ):
            self.assertIn(status, models.LEGAL_TRANSITIONS)


class TestRoundTrip(unittest.TestCase):
    def test_signal_round_trip(self):
        s = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/x",
            title="t",
            resource="r",
            labels={"k": "v"},
        )
        again = models.Signal.from_dict(s.to_dict())
        self.assertEqual(s, again)

    def test_incident_round_trip(self):
        s = models.Signal.create(source="cloudwatch", native_id="alarm/x", title="t")
        inc = models.Incident(incident_id="INV-1", signal=s, ledger_matches=["abc"])
        again = models.Incident.from_dict(inc.to_dict())
        self.assertEqual(again.incident_id, "INV-1")
        self.assertEqual(again.signal, s)
        self.assertEqual(again.ledger_matches, ["abc"])

    def test_malformed_incident_dict_does_not_raise(self):
        """A corrupt index entry must degrade, not crash the board."""
        inc = models.Incident.from_dict({"incident_id": "INV-9", "ledger_matches": "oops"})
        self.assertEqual(inc.incident_id, "INV-9")
        self.assertEqual(inc.ledger_matches, [])


class TestLedgerEntryIdentity(unittest.TestCase):
    def test_content_addressed_id_is_deterministic(self):
        """Two people learning the same lesson must produce the same id — that is
        what makes a git-merged ledger a dedupe rather than a conflict."""
        a = models.LedgerEntry.create(pattern="DLQ fills up", fix="clear and redrive")
        b = models.LedgerEntry.create(pattern="dlq fills up", fix="Clear and redrive")
        self.assertEqual(a.entry_id, b.entry_id)

    def test_different_fix_is_a_different_entry(self):
        a = models.LedgerEntry.create(pattern="DLQ fills up", fix="clear and redrive")
        b = models.LedgerEntry.create(pattern="DLQ fills up", fix="raise concurrency")
        self.assertNotEqual(a.entry_id, b.entry_id)


if __name__ == "__main__":
    unittest.main()
