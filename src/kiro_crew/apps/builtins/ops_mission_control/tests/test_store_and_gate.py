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


class TestClosingAnIncidentLeavesAnArtifact(_HomeIsolated):
    """A closed incident must leave a Markdown record a non-KiroCrew reader can be handed.

    The renderer existed for the whole life of the app with exactly one reference — its own
    definition — so ``incidents/<id>.md`` was documented on-disk state that could not
    exist, and ``/incident``'s ``log`` was a structurally-empty field. These tests pin the
    call site rather than the renderer, because the renderer was never the broken half.
    """

    def test_resolving_an_incident_writes_its_postmortem(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(title="DLQ depth above 100"), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING, diagnosis="pool exhausted")
        store.transition(inc.incident_id, models.STATUS_RESOLVED, resolution="raised the cap")

        path = store.incident_log_path(inc.incident_id)
        self.assertTrue(path.is_file(), "resolving an incident wrote no postmortem")
        body = path.read_text(encoding="utf-8")
        self.assertIn("DLQ depth above 100", body)
        self.assertIn("pool exhausted", body)
        self.assertIn("raised the cap", body)
        # The route that serves it must find it too — a file nothing can read is no better
        # than no file.
        self.assertEqual(store.read_log(inc.incident_id), body)

    def test_escalating_also_writes_one(self):
        """Both terminal statuses, not just the happy one.

        An escalation is the case where a colleague is MOST likely to be handed the record,
        so writing the artifact only on ``resolved`` would miss the main use.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_ESCALATED, diagnosis="beyond me")
        self.assertTrue(store.incident_log_path(inc.incident_id).is_file())

    def test_an_open_incident_has_no_artifact_yet(self):
        """A postmortem describes a finished investigation; writing one early would lie."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING, diagnosis="looking")
        self.assertFalse(store.incident_log_path(inc.incident_id).is_file())
        self.assertEqual(store.read_log(inc.incident_id), "")

    def test_every_terminal_status_is_covered_by_the_writer(self):
        """Derived from the grammar, so a new terminal status cannot ship without an artifact.

        ``TERMINAL_STATUSES`` is computed from ``LEGAL_TRANSITIONS``, so a future status
        with no outgoing edge becomes terminal automatically — and would silently produce
        no record if this test named the two statuses by hand instead.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        for status in sorted(models.TERMINAL_STATUSES):
            inc = store.claim(self._signal(native_id=f"alarm/{status}"), operating_mode="observe")
            assert inc is not None
            store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
            store.transition(inc.incident_id, status)
            self.assertTrue(
                store.incident_log_path(inc.incident_id).is_file(),
                f"closing into {status!r} left no postmortem",
            )

    def test_a_later_field_update_cannot_blank_a_good_artifact(self):
        """The record is sourced from the incident, never from the caller's kwargs.

        ``update_fields`` re-enters ``transition`` with the SAME (terminal) status and no
        diagnosis, so a writer reading its arguments would re-render the artifact with
        empty sections — destroying the finished record on an unrelated write.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED, diagnosis="thread pool starved")
        store.update_fields(inc.incident_id, slack_thread_ts="1700000000.000100")

        body = store.read_log(inc.incident_id)
        self.assertIn("thread pool starved", body)
        self.assertNotIn("_pending_", body)

    def test_the_artifact_is_owner_only(self):
        """It describes production failures and is meant to be shared BY CHOICE.

        The directory has been 0o700 from the start; the file inside it inherited the
        umask, so on a machine with a permissive umask the filesystem decided who could
        read it before the operator did.
        """
        import os
        import stat

        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        if os.name == "nt":  # pragma: no cover - POSIX permission bits only
            self.skipTest("POSIX mode bits")
        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        mode = stat.S_IMODE(store.incident_log_path(inc.incident_id).stat().st_mode)
        self.assertEqual(mode & 0o077, 0, f"postmortem is readable beyond its owner: {mode:o}")

    def test_a_failed_write_does_not_fail_the_close(self):
        """A record of a state change must never be able to fail the state change.

        Same reasoning the Slack mirror already runs on: by the time the artifact is
        rendered the index write is durable, so an unwritable data directory must leave the
        incident resolved rather than raising out of ``transition``.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        with mock.patch.object(store, "write_log", side_effect=OSError("read-only fs")):
            closed = store.transition(inc.incident_id, models.STATUS_RESOLVED)
        self.assertEqual(closed.status, models.STATUS_RESOLVED)
        persisted = store.get_incident(inc.incident_id)
        assert persisted is not None
        self.assertEqual(persisted.status, models.STATUS_RESOLVED)

    def test_a_credential_in_provider_text_never_reaches_the_artifact(self):
        """The postmortem is the one file an operator is EXPECTED to hand to someone else.

        So it is the worst possible place for a leaked credential: the operator forwards it
        themselves, with their own confidence behind it. The renderer interpolated provider
        titles and a model-authored diagnosis verbatim and no ``redact`` call existed in
        this module at all — invisible only because no caller ever produced a file.

        Covers both scanners on purpose. Core alone misses a bare-hex Datadog key and a
        prefix-less bearer token; the app's token pass alone misses an AWS access key id.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        dd_key = "0123456789abcdef0123456789abcdef"
        signal = self._signal(title=f"probe failed: DD-API-KEY: {dd_key}", resource="svc/api")
        inc = store.claim(signal, operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(
            inc.incident_id,
            models.STATUS_RESOLVED,
            diagnosis="the reproducer used AKIAIOSFODNN7EXAMPLE",
            resolution="rotated it; curl used Bearer sk-abcdefghijklmnopqrst",
        )

        body = store.read_log(inc.incident_id)
        self.assertTrue(body, "no artifact to check")
        self.assertNotIn(dd_key, body)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", body)
        self.assertNotIn("sk-abcdefghijklmnopqrst", body)
        # Still a usable postmortem: only the secrets are gone, not the narrative.
        self.assertIn("probe failed", body)
        self.assertIn("the reproducer used", body)

    def test_the_narrative_survives_redaction(self):
        """Redaction must not be so eager that the artifact stops being worth sharing.

        A scanner that ate ordinary diagnostic prose would make the file useless and push
        people back to pasting the raw transcript, which is strictly worse.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        prose = "Connection pool exhausted at 14:02; tokenization-heavy requests queued."
        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED, diagnosis=prose)
        self.assertIn(prose, store.read_log(inc.incident_id))

    def test_pruning_the_index_keeps_the_written_record(self):
        """``prune_closed`` bounds the INDEX, and must not destroy history on disk.

        That was already the stated decision; it only became a testable claim once a file
        actually existed to delete.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        store.prune_closed(keep=0)
        self.assertIsNone(store.get_incident(inc.incident_id))
        self.assertTrue(store.incident_log_path(inc.incident_id).is_file())
        self.assertIn("## Diagnosis", store.read_log(inc.incident_id))


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

    def test_unanswered_needs_human_is_eventually_released(self):
        """An incident nobody answers must not pin its signal as claimed forever.

        ``LEGAL_TRANSITIONS`` has always legalised ``needs_human -> stale`` for exactly
        this reason, and the sweep never traversed it: ``needs_human`` was absent from
        ``_SWEEPABLE_STATUSES``, so the alarm was never re-claimed and nothing said so.
        The old guard asserted only that the transition was *legal*, which is why the
        gap survived — this exercises the sweep itself.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_NEEDS_HUMAN)
        # Past even the longer needs_human threshold (6× the working one).
        self._backdate(inc.incident_id, hours=99)
        released = store.sweep_stale(stale_after_secs=3600)
        self.assertIn(inc.incident_id, released)
        refreshed = store.get_incident(inc.incident_id)
        assert refreshed is not None
        self.assertEqual(refreshed.status, models.STATUS_STALE)

    def test_needs_human_gets_a_longer_grace_than_a_dead_investigation(self):
        """Waiting on a person is legitimately slower than an agent dying.

        At 5 hours idle an ``investigating`` incident is stale (its agent is gone) but a
        ``needs_human`` one is not — the operator may simply be asleep, and releasing it
        would discard the investigation's context to re-derive it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        waiting = store.claim(
            self._signal(native_id="alarm/waiting"), operating_mode=models.MODE_OBSERVE
        )
        working = store.claim(
            self._signal(native_id="alarm/working"), operating_mode=models.MODE_OBSERVE
        )
        assert waiting is not None and working is not None
        store.transition(waiting.incident_id, models.STATUS_NEEDS_HUMAN)
        store.transition(working.incident_id, models.STATUS_INVESTIGATING)
        self._backdate(waiting.incident_id, hours=5)
        self._backdate(working.incident_id, hours=5)

        released = store.sweep_stale(stale_after_secs=3600)
        self.assertIn(working.incident_id, released)
        self.assertNotIn(waiting.incident_id, released)

    def test_needs_human_threshold_is_independently_tunable(self):
        """An operator who wants them coupled differently can say so."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_NEEDS_HUMAN)
        self._backdate(inc.incident_id, hours=5)
        # Default multiplier would keep it (6h); an explicit 1h releases it.
        self.assertEqual(store.sweep_stale(stale_after_secs=3600), [])
        self.assertIn(
            inc.incident_id,
            store.sweep_stale(stale_after_secs=3600, needs_human_after_secs=3600),
        )


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

    def test_arming_is_server_side_not_agent_held(self):
        """The app must not hold `cron_pause`/`cron_resume`.

        Tier arming used to be the agent's job, and the only thing stopping it pausing
        `rotation-check` — the sole always-tier job, so the only one that can re-arm a
        gated instance — was a sentence of SOP prose. Prose is not enforcement, and
        `mcpTools` is declarative only (nothing calls `check_tool_permission` at runtime),
        so the manifest entry was never a gate either. Arming moved into
        `rotation.apply_tiers`; the capability must be gone from the manifest with it, or
        a future SOP edit can quietly hand the loaded gun back.
        """
        import json
        from pathlib import Path

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        manifest = json.loads(
            (Path(rotation.__file__).resolve().parents[1] / "app.json").read_text(encoding="utf-8")
        )
        tools = manifest["permissions"]["mcpTools"]
        self.assertNotIn("cron_pause", tools)
        self.assertNotIn("cron_resume", tools)

    def test_rotation_check_prompt_points_at_the_arm_route(self):
        """The cron message must POST /rotation/arm, not hand-pick crons to pause.

        Pins the manifest prompt to the enforced path. A revert to "pause the crons in
        tier_crons.on_shift" would re-open the hole even with the route in place.
        """
        import json
        from pathlib import Path

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        manifest = json.loads(
            (Path(rotation.__file__).resolve().parents[1] / "app.json").read_text(encoding="utf-8")
        )
        message = next(
            c["message"] for c in manifest["crons"] if c["name"] == "rotation-check"
        )
        self.assertIn("/rotation/arm", message)
        self.assertNotIn("cron_pause", message)
        self.assertNotIn("cron_resume", message)

    def test_protected_cron_names_is_derived_from_the_tier_table(self):
        """The protected set must BE the always tier, not a second hand-kept list.

        Restating the names would let a job move onto `always` while the guard kept
        protecting the old one — protected in one place, forgotten in the other.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self.assertEqual(
            rotation.protected_cron_names(),
            frozenset(rotation.crons_for_tier(rotation.TIER_ALWAYS)),
        )
        self.assertIn("ops-mission-control/rotation-check", rotation.protected_cron_names())

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

    def test_reconcile_unions_provider_keys_not_only_fingerprints(self):
        """A git merge must not drop one branch's provider identities.

        `upsert` unions `provider_keys` and `_reconcile` — the READ path that collapses the
        duplicate ids a real `git merge` leaves — did not, so a merge permanently wrote
        incomplete identity data. That is not cosmetic: `match()` treats a provider key as
        the EXACT-identity signal, so the very next recurrence on the dropped alert would
        have matched by shape hash alone, or not at all. Found in review.

        Written as two RAW lines with different keys, because that is the git-merge shape;
        the pre-existing `provider_keys` merge test went through `upsert`, which is exactly
        why it never caught this.
        """
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        mine = LedgerEntry.create(pattern="p", fix="f", provider_keys=["dd:monitor/111"])
        theirs = LedgerEntry.create(pattern="p", fix="f", provider_keys=["pd:incident/222"])
        self.assertEqual(mine.entry_id, theirs.entry_id, "content addressing must agree")

        path = ledger.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(mine.to_dict(), sort_keys=True)
            + "\n"
            + json.dumps(theirs.to_dict(), sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1, "one lesson must read as one entry")
        self.assertEqual(
            set(entries[0].provider_keys),
            {"dd:monitor/111", "pd:incident/222"},
            "both branches' provider identities must survive the merge",
        )
        # And the match path must actually honour the recovered key as an exact identity.
        self.assertTrue(ledger.is_exact_match(entries[0], "pd:incident/222"))

    def test_reconcile_caps_both_identity_lists(self):
        """Two already-capped lists unioned are up to 2x the cap.

        `upsert` bounds that and `_reconcile` did not, so a merge could grow a line past
        the limit and keep it there.
        """
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        cap = ledger.MAX_KEYS_PER_ENTRY
        mine = LedgerEntry.create(
            pattern="p",
            fix="f",
            fingerprints=[f"a{i}" for i in range(cap)],
            provider_keys=[f"ka{i}" for i in range(cap)],
        )
        theirs = LedgerEntry.create(
            pattern="p",
            fix="f",
            fingerprints=[f"b{i}" for i in range(cap)],
            provider_keys=[f"kb{i}" for i in range(cap)],
        )
        path = ledger.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(mine.to_dict(), sort_keys=True)
            + "\n"
            + json.dumps(theirs.to_dict(), sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        merged = ledger.read_entries()[0]
        self.assertLessEqual(len(merged.fingerprints), cap)
        self.assertLessEqual(len(merged.provider_keys), cap)
        # Newest kept: the recent identity is the one about to recur.
        self.assertIn(f"kb{cap - 1}", merged.provider_keys)

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

    def _proven(self, **overrides):
        """A verified/high entry that has already earned the fast path."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(
            pattern=overrides.pop("pattern", "p"),
            fix=overrides.pop("fix", "f"),
            trust=models.TRUST_VERIFIED,
            confidence=models.CONFIDENCE_HIGH,
        )
        entry.use_count = ledger.MIN_USES_FOR_FAST_PATH
        for key, value in overrides.items():
            setattr(entry, key, value)
        return entry

    def test_fast_path_requires_verified_and_high(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        weak = self._proven()
        weak.trust = models.TRUST_OBSERVED
        self.assertFalse(ledger.is_fast_path([weak]))
        self.assertTrue(ledger.is_fast_path([self._proven()]))

    def test_fast_path_also_requires_a_track_record(self):
        """Verified + high on an entry nobody has ever used is a claim, not a record.

        `POST /ledger` takes `confidence` and `trust` verbatim, so without this floor one
        hand-authored line unlocked "propose this fix directly" for a production failure
        on its very first match — and `record_use` then binds the provider key, so every
        later occurrence presents that same single piece of evidence as an EXACT match.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        fresh = self._proven(use_count=ledger.MIN_USES_FOR_FAST_PATH - 1)
        self.assertFalse(ledger.is_fast_path([fresh]))
        self.assertTrue(ledger.is_fast_path([self._proven()]))

    def test_one_recorded_failure_relocks_the_fast_path(self):
        """Unlocking needs corroboration; re-locking needs one counterexample.

        The asymmetry is deliberate. A fix observed not to hold must stop being the thing
        an agent proposes without checking — and it stays in the ledger with its full
        text, because a fix that works sometimes is worth more than nothing.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        missed = self._proven(miss_count=1)
        self.assertFalse(ledger.is_fast_path([missed]))
        self.assertTrue(ledger.is_demoted(missed))
        self.assertFalse(ledger.is_demoted(self._proven()))

    def test_record_miss_does_not_inflate_the_use_count(self):
        """A miss is not a use — that inversion is the original defect turned inside out."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f"))
        ledger.record_use(entry.entry_id)
        updated = ledger.record_miss(entry.entry_id)
        assert updated is not None
        self.assertEqual(updated.use_count, 1)
        self.assertEqual(updated.miss_count, 1)
        self.assertTrue(updated.last_miss)

    def test_record_miss_on_a_pruned_entry_is_not_an_error(self):
        """Hygiene prunes; a miss charged to a gone entry must be a no-op, not a raise."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        self.assertIsNone(ledger.record_miss("nope"))

    def test_reposting_an_entry_cannot_erase_its_recorded_failures(self):
        """The promotion route must not double as a way to launder counter-evidence.

        `ledger-hygiene.md` promotes observed → verified by re-POSTing the same
        pattern+fix, which merges by content-addressed id. If that merge took the incoming
        `miss_count` of 0, the nightly promotion step would clear every recorded failure
        on exactly the entries most likely to have them — with one curl.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f"))
        ledger.record_miss(entry.entry_id)
        merged = ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        self.assertEqual(merged.miss_count, 1)
        self.assertEqual(ledger.read_entries()[0].miss_count, 1)

    def test_a_git_merge_cannot_launder_a_teammates_recorded_failure(self):
        """Duplicate ids reconcile on READ, and miss_count must take the max there too.

        Confidence and trust take the STRONGEST of two records, so a merge that took the
        lower miss_count would make "pull the team ledger" a way to clear a demotion —
        the one direction a shared append-only knowledge base must never move by itself.
        """
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        mine = models.LedgerEntry.create(pattern="p", fix="f")
        theirs = models.LedgerEntry.create(pattern="p", fix="f")
        theirs.miss_count = 2
        theirs.last_miss = "2026-07-01T00:00:00Z"
        path = ledger.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Two lines, same content-addressed id — exactly what a real `git merge` leaves.
        path.write_text(
            json.dumps(mine.to_dict()) + "\n" + json.dumps(theirs.to_dict()) + "\n",
            encoding="utf-8",
        )
        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].miss_count, 2)

    def test_hygiene_demotes_an_entry_whose_fix_stopped_working(self):
        """The mechanical downward path §5.9 asked for, on the nightly pass not the hot one."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        ledger.record_use(entry.entry_id)
        ledger.record_use(entry.entry_id)
        ledger.record_miss(entry.entry_id)
        ledger.record_miss(entry.entry_id)
        summary = ledger.hygiene()
        self.assertEqual(summary["demoted"], 1)
        stored = ledger.read_entries()[0]
        self.assertEqual(stored.confidence, models.CONFIDENCE_MEDIUM)
        # Trust is NOT rewritten: "somebody saw this work" stays true even after it
        # failed elsewhere, and overwriting a human's own observation is editorialising.
        self.assertEqual(stored.trust, models.TRUST_VERIFIED)

    def test_one_failure_costs_exactly_one_confidence_step(self):
        """Hygiene runs nightly and its test is a ratio, which stays true once true.

        Without the spent-evidence guard a single miss would walk an entry
        high → medium → low across three nights on no new evidence at all, arriving at
        the bottom of the scale for one failure.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        ledger.record_miss(entry.entry_id)
        self.assertEqual(ledger.hygiene()["demoted"], 1)
        self.assertEqual(ledger.hygiene()["demoted"], 0)
        self.assertEqual(ledger.hygiene()["demoted"], 0)
        self.assertEqual(ledger.read_entries()[0].confidence, models.CONFIDENCE_MEDIUM)
        # A SECOND, genuinely new failure spends a second step.
        ledger.record_miss(entry.entry_id)
        self.assertEqual(ledger.hygiene()["demoted"], 1)
        self.assertEqual(ledger.read_entries()[0].confidence, models.CONFIDENCE_LOW)

    def test_a_well_used_entry_is_not_condemned_by_one_bad_night(self):
        """The ratio's whole purpose: more evidence it works, more it takes to overturn."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        for _ in range(8):
            ledger.record_use(entry.entry_id)
        ledger.record_miss(entry.entry_id)
        self.assertEqual(ledger.hygiene()["demoted"], 0)
        self.assertEqual(ledger.read_entries()[0].confidence, models.CONFIDENCE_HIGH)

    def test_stats_reports_proven_separately_from_its_two_halves(self):
        """`verified` and `high_confidence` are each HALF the bar, so neither is the bar.

        A board showing only those two overstated the ledger's authority: an entry can be
        counted in both while being something nobody has ever successfully applied.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        stats = ledger.stats()
        self.assertEqual(stats["verified"], 1)
        self.assertEqual(stats["high_confidence"], 1)
        self.assertEqual(stats["proven"], 0)
        for _ in range(ledger.MIN_USES_FOR_FAST_PATH):
            ledger.record_use(entry.entry_id)
        self.assertEqual(ledger.stats()["proven"], 1)
        ledger.record_miss(entry.entry_id)
        after = ledger.stats()
        self.assertEqual(after["proven"], 0)
        self.assertEqual(after["demoted"], 1)
        self.assertEqual(after["total_misses"], 1)

    def test_prune_order_stops_preferring_the_most_misleading_entries(self):
        """The prune sorted by `-use_count` alone, so a false match was pruned LAST.

        `use_count` incremented at claim time, before any outcome existed — so an entry
        that kept matching the wrong failure climbed the ranking on every mismatch and
        was the last thing the cap dropped. The ledger preferentially kept its worst rows.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        misleading = ledger.upsert(models.LedgerEntry.create(pattern="wrong", fix="a"))
        useful = ledger.upsert(models.LedgerEntry.create(pattern="right", fix="b"))
        for _ in range(4):
            ledger.record_use(misleading.entry_id)
            ledger.record_miss(misleading.entry_id)
        for _ in range(3):
            ledger.record_use(useful.entry_id)
        ledger.hygiene()
        # Hygiene writes in prune order, so position IS survival order at the cap.
        self.assertEqual([e.pattern for e in ledger.read_entries()], ["right", "wrong"])

    def test_record_use_binds_new_fingerprint(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["a"])
        ledger.upsert(entry)
        updated = ledger.record_use(entry.entry_id, fingerprint="b")
        assert updated is not None
        self.assertEqual(updated.use_count, 1)
        self.assertIn("b", updated.fingerprints)


class TestShapeHashOverMerges(_HomeIsolated):
    """The motivating defect for the exact-identity layer, asserted as a fact.

    ``compute_fingerprint`` strips every bare digit, so alarms that differ only in a
    number are indistinguishable to it. This is not a bug to fix in the hash — the
    stripping is deliberate, so that a DLQ at 500 and at 900 match — it is a reason the
    hash cannot be the ONLY key. Pinned so nobody later "fixes" the collision and
    quietly removes the reason the provider_key path exists.
    """

    def test_distinct_failures_collide_on_the_shape_hash(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            compute_fingerprint as fp,
        )

        self.assertEqual(
            fp("cloudwatch", "svc/api", "4xx error rate above 5"),
            fp("cloudwatch", "svc/api", "5xx error rate above 1"),
        )
        self.assertEqual(
            fp("cloudwatch", "svc/api", "p99 latency above 500ms"),
            fp("cloudwatch", "svc/api", "p50 latency above 100ms"),
        )

    def test_an_exact_provider_key_separates_them(self):
        """What the collision costs, and what the exact key buys."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        four = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/four",
            title="4xx error rate above 5",
            resource="svc/api",
            provider_key="us-east-1/four-xx",
        )
        five = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/five",
            title="5xx error rate above 1",
            resource="svc/api",
            provider_key="us-east-1/five-xx",
        )
        # Same shape hash, different provider identity — the premise of the fix.
        self.assertEqual(four.fingerprint, five.fingerprint)
        self.assertNotEqual(four.provider_key, five.provider_key)

        # A lesson learned about the 4xx alarm, bound to BOTH its keys.
        ledger.upsert(
            models.LedgerEntry.create(
                pattern="4xx spike from a bad deploy",
                fix="roll back the canary",
                fingerprints=[four.fingerprint],
                provider_keys=[four.provider_key],
                confidence=models.CONFIDENCE_HIGH,
                trust=models.TRUST_VERIFIED,
            )
        )
        # A different, weaker lesson about the 5xx alarm.
        ledger.upsert(
            models.LedgerEntry.create(
                pattern="5xx from upstream timeouts",
                fix="raise the pool size",
                fingerprints=[five.fingerprint],
                provider_keys=[five.provider_key],
            )
        )

        # Both still match by shape (they share a fingerprint) — but the 5xx signal's
        # OWN entry must rank first, ahead of the verified/high one that merely collides.
        ranked = ledger.match(five.fingerprint, provider_key=five.provider_key)
        self.assertEqual(ranked[0].pattern, "5xx from upstream timeouts")
        self.assertTrue(ledger.is_exact_match(ranked[0], five.provider_key))
        self.assertFalse(ledger.is_exact_match(ranked[1], five.provider_key))

    def test_without_a_provider_key_behaviour_is_unchanged(self):
        """Adapters that publish no stable identity must keep working exactly as before."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["fp"]))
        self.assertEqual(len(ledger.match("fp")), 1)
        self.assertEqual(len(ledger.match("fp", provider_key="")), 1)

    def test_a_provider_key_alone_matches_when_the_shape_drifted(self):
        """The point of the exact key: a reworded alarm still finds its own lesson."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(
            models.LedgerEntry.create(
                pattern="p", fix="f", fingerprints=["old-shape"], provider_keys=["cw:alarm-7"]
            )
        )
        found = ledger.match("a-totally-different-shape", provider_key="cw:alarm-7")
        self.assertEqual(len(found), 1)

    def test_record_use_binds_the_provider_key(self):
        """The first fuzzy match teaches the entry the provider's identity."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["fp"])
        ledger.upsert(entry)
        updated = ledger.record_use(entry.entry_id, fingerprint="fp", provider_key="cw:alarm-9")
        assert updated is not None
        self.assertIn("cw:alarm-9", updated.provider_keys)
        # And the next occurrence is now an EXACT match rather than a shape guess.
        self.assertTrue(
            ledger.is_exact_match(ledger.match("fp", provider_key="cw:alarm-9")[0], "cw:alarm-9")
        )

    def test_provider_keys_union_on_merge_like_fingerprints(self):
        """Preserves the conflict-free git dedupe property for the new field."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", provider_keys=["k1"]))
        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", provider_keys=["k2"]))
        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(set(entries[0].provider_keys), {"k1", "k2"})

    def test_keys_bound_to_one_entry_are_capped(self):
        """A per-occurrence identity (PagerDuty) must not grow a JSONL line forever."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(pattern="p", fix="f")
        ledger.upsert(entry)
        for i in range(ledger.MAX_KEYS_PER_ENTRY + 25):
            ledger.record_use(entry.entry_id, provider_key=f"pd:incident/{i}")
        stored = ledger.read_entries()[0]
        self.assertLessEqual(len(stored.provider_keys), ledger.MAX_KEYS_PER_ENTRY)
        # Newest kept, oldest dropped — the recent identity is the one about to recur.
        self.assertIn(f"pd:incident/{ledger.MAX_KEYS_PER_ENTRY + 24}", stored.provider_keys)
        self.assertNotIn("pd:incident/0", stored.provider_keys)

    def test_a_ledger_line_written_before_the_field_existed_still_loads(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        legacy = {
            "entry_id": "abc",
            "pattern": "p",
            "fix": "f",
            "fingerprints": ["fp"],
            "confidence": "medium",
            "trust": "observed",
            "use_count": 2,
        }
        path = ledger.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        loaded = ledger.read_entries()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].provider_keys, [])
        self.assertEqual(len(ledger.match("fp")), 1)

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


class TestThePostmortemSaysWhetherAnythingWasVerified(_HomeIsolated):
    """The artifact is what a colleague reads with no access to the board.

    "Actions taken: silenced the alarm" is the sentence most likely to be believed as an
    OUTCOME, and for the whole life of the renderer it would have said exactly that on the
    strength of a 2xx, with nothing in the file admitting no code had looked again.
    """

    def _closed(self, **fields):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/dlq", title="DLQ deep"),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None
        if fields:
            store.update_fields(inc.incident_id, **fields)
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED, resolution="silenced it")
        return store.read_log(inc.incident_id)

    def test_a_still_firing_action_is_named_as_such_in_the_artifact(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            ACTION_SILENCE,
            VERIFY_STILL_FIRING,
        )

        log = self._closed(
            last_action=ACTION_SILENCE,
            verification=VERIFY_STILL_FIRING,
            verification_detail="Still firing at cloudwatch after the silence.",
        )
        self.assertIn("STILL FIRING", log)

    def test_an_unverifiable_action_says_sent_not_confirmed(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            ACTION_ACK,
            VERIFY_NOT_CHECKABLE,
        )

        log = self._closed(last_action=ACTION_ACK, verification=VERIFY_NOT_CHECKABLE)
        self.assertIn("cannot observe", log)
        self.assertIn("not as confirmed", log)

    def test_an_incident_that_closed_before_its_recheck_ran_admits_it(self):
        """`resolved` is terminal, so this is the last word on it — it must not imply success."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            ACTION_SILENCE,
            VERIFY_PENDING,
        )

        log = self._closed(last_action=ACTION_SILENCE, verification=VERIFY_PENDING)
        self.assertIn("NOT CONFIRMED", log)

    def test_an_incident_with_no_action_gets_no_verification_line_at_all(self):
        """Most incidents. A "not applicable" line on every one buries the cases that matter."""
        log = self._closed()
        self.assertNotIn("Verification", log)

    def test_the_verification_detail_goes_through_the_redactor(self):
        """It quotes a provider's own poll-failure text, which can carry a credential."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            ACTION_SILENCE,
            VERIFY_UNKNOWN,
        )

        log = self._closed(
            last_action=ACTION_SILENCE,
            verification=VERIFY_UNKNOWN,
            verification_detail="401 from https://api.datadoghq.com?api_key=" + "a" * 32,
        )
        self.assertNotIn("a" * 32, log)


class TestTheSweepWindowsAreReadableBack(_HomeIsolated):
    """``PUT /settings`` accepted these three and no read path returned any of them.

    So an operator could change how long a dead investigation pins a signal and get no
    confirmation, no way to look the value up again, and — for the untouched case, which is
    every install — no way to discover the defaults they were already living under. The
    knob turned and the dial did not exist. A regression here silently restores that:
    nothing else in the app would fail, which is precisely why it survived so long.
    """

    def test_the_defaults_in_force_are_reported_without_any_config(self):
        """The commonest case. An install that never touched these still has values."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, rotation, store

        windows = rotation.sweep_windows()
        self.assertEqual(windows["max_claims_per_cycle"], dispatch.DEFAULT_MAX_CLAIMS_PER_CYCLE)
        self.assertEqual(windows["stale_after_secs"], dispatch.DEFAULT_STALE_AFTER_SECS)
        self.assertEqual(
            windows["needs_human_stale_after_secs"],
            dispatch.DEFAULT_STALE_AFTER_SECS * store.DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER,
        )

    def test_an_unset_needs_human_window_is_reported_resolved_not_as_zero(self):
        """Unset does NOT mean "never released" — ``sweep_stale`` derives it.

        Reporting the raw stored ``0`` would tell an operator an unanswered question holds
        its signal forever, which is the opposite of what the sweep does.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            set_top_level,
        )

        set_top_level("stale_after_secs", 600)
        windows = rotation.sweep_windows()
        self.assertEqual(
            windows["needs_human_stale_after_secs"],
            600 * store.DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER,
        )
        # And it says the number is ours, not the operator's — a derived window MOVES when
        # the working threshold changes, and the UI must be able to say which it is.
        self.assertTrue(windows["needs_human_derived"])

    def test_an_explicitly_set_needs_human_window_is_reported_as_pinned(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            set_top_level,
        )

        set_top_level("stale_after_secs", 600)
        set_top_level("needs_human_stale_after_secs", 9000)
        windows = rotation.sweep_windows()
        self.assertEqual(windows["needs_human_stale_after_secs"], 9000)
        self.assertFalse(windows["needs_human_derived"])

    def test_the_reported_windows_are_the_ones_the_heartbeat_actually_applies(self):
        """The whole value of the dial is that it points where the machine is going.

        ``rotation`` has to duplicate the config-key strings (importing them would close an
        import cycle with ``dispatch``), so a rename on either side would leave this panel
        confidently displaying a default while the heartbeat used the operator's value.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, rotation

        self.assertEqual(rotation._CONFIG_MAX_CLAIMS, dispatch._CONFIG_MAX_CLAIMS)
        self.assertEqual(rotation._CONFIG_STALE_AFTER, dispatch._CONFIG_STALE_AFTER)
        self.assertEqual(
            rotation._CONFIG_NEEDS_HUMAN_STALE_AFTER,
            dispatch._CONFIG_NEEDS_HUMAN_STALE_AFTER,
        )

    def test_a_corrupt_stored_window_falls_back_instead_of_reporting_nonsense(self):
        """``_config_int`` already guards this; the dial must inherit that, not bypass it."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            set_top_level,
        )

        set_top_level("stale_after_secs", "not a number")
        self.assertEqual(
            rotation.sweep_windows()["stale_after_secs"], dispatch.DEFAULT_STALE_AFTER_SECS
        )


class TestClaimProvenance(unittest.TestCase):
    """`claimed_at` recorded WHEN; nothing recorded BY WHAT.

    There are two paths into `store.claim` — the dispatch heartbeat and the board's
    manual claim — and after the fact they were indistinguishable. That is the question
    an operator actually asks of a surprising incident: did the agent decide to pick this
    up, or did I? The source workflow carries the same field and 126 of its 130 live
    entries populate it.
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

    def _signal(self, native_id="alarm/dlq"):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        return models.Signal.create(source="cloudwatch", native_id=native_id, title="DLQ deep")

    def test_the_heartbeat_is_the_default(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            models,
            store,
        )

        """A caller that says nothing is the cron, which is the overwhelming majority."""
        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        self.assertEqual(inc.claimed_by, models.CLAIMED_BY_HEARTBEAT)

    def test_an_operator_claim_is_recorded_as_such(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            models,
            store,
        )

        inc = store.claim(
            self._signal(),
            operating_mode=models.MODE_OBSERVE,
            claimed_by=models.CLAIMED_BY_OPERATOR,
        )
        assert inc is not None
        self.assertEqual(inc.claimed_by, models.CLAIMED_BY_OPERATOR)

    def test_an_unknown_claimant_is_coerced_not_stored(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            models,
            store,
        )

        """This value reaches the board and the digest, so it must not be free text.

        Storing whatever a caller passed would let a typo render as provenance — and a
        wrong provenance is worse than an absent one, because it reads as a fact.
        """
        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE, claimed_by="nonsense")
        assert inc is not None
        self.assertEqual(inc.claimed_by, models.CLAIMED_BY_HEARTBEAT)

    def test_it_round_trips_through_the_index(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            models,
            store,
        )

        """The field is useless if it does not survive a reload."""
        inc = store.claim(
            self._signal(),
            operating_mode=models.MODE_OBSERVE,
            claimed_by=models.CLAIMED_BY_OPERATOR,
        )
        assert inc is not None
        reloaded = store.get_incident(inc.incident_id)
        assert reloaded is not None
        self.assertEqual(reloaded.claimed_by, models.CLAIMED_BY_OPERATOR)

    def test_an_incident_written_before_the_field_existed_still_loads(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        """Every incident already on disk lacks this key; absent must mean unrecorded.

        A required field here would make the whole index unreadable on upgrade — the
        board would show nothing, which is a far worse failure than missing provenance.
        """
        legacy = {
            "incident_id": "INV-1",
            "signal": {"id": "cloudwatch:alarm/x", "source": "cloudwatch", "title": "t"},
            "status": models.STATUS_DISPATCHED,
        }
        inc = models.Incident.from_dict(legacy)
        self.assertEqual(inc.claimed_by, "")


class _FakeJob:
    """Minimal duck-type of a CronJob for the arming tests."""

    def __init__(self, name, enabled):
        self.id = f"id-{name}"
        self.name = name
        self.enabled = enabled


class _FakeCronService:
    """Records every enable_job_async call so a test can assert what was moved."""

    def __init__(self, jobs):
        self._jobs = jobs
        self.calls = []

    async def list_jobs_async(self, include_disabled=False):
        # Mirrors the real freshness-guaranteed reader `apply_tiers` uses.
        return list(self._jobs)

    async def enable_job_async(self, job_id, enabled=True):
        self.calls.append((job_id, enabled))
        for job in self._jobs:
            if job.id == job_id:
                job.enabled = enabled
        return True


class TestServerSideArming(unittest.IsolatedAsyncioTestCase):
    """`rotation.apply_tiers` — arming without the agent choosing what to pause.

    The property under test is the one the design review flagged: nothing may pause an
    always-tier cron, because that is the only job that can re-arm a gated instance and
    pausing it silently ends incident response until a human notices.
    """

    def _all_jobs(self, enabled=True):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        names = [name for names in rotation.TIER_CRONS.values() for name in names]
        return [_FakeJob(name, enabled) for name in names]

    async def test_off_shift_disarms_on_shift_but_never_the_always_tier(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        jobs = self._all_jobs(enabled=True)
        svc = _FakeCronService(jobs)
        result = await rotation.apply_tiers(ShiftStatus(on_shift=False), svc)

        self.assertTrue(result["ok"])
        by_name = {job.name: job.enabled for job in jobs}
        # The gated tier is off...
        self.assertFalse(by_name["ops-mission-control/dispatch"])
        self.assertFalse(by_name["ops-mission-control/reconcile"])
        # ...and the job that re-arms the instance is STILL RUNNING. This is the
        # invariant: an off-shift instance that cannot re-arm is permanently dead.
        self.assertTrue(by_name["ops-mission-control/rotation-check"])
        paused = {name for name, enabled in by_name.items() if not enabled}
        self.assertNotIn("ops-mission-control/rotation-check", paused)

    async def test_on_shift_arms_the_gated_tier(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        jobs = self._all_jobs(enabled=False)
        svc = _FakeCronService(jobs)
        await rotation.apply_tiers(ShiftStatus(on_shift=True), svc)

        by_name = {job.name: job.enabled for job in jobs}
        self.assertTrue(by_name["ops-mission-control/dispatch"])
        self.assertTrue(by_name["ops-mission-control/reconcile"])

    async def test_a_protected_cron_is_refused_even_if_the_tier_map_says_pause(self):
        """The guard is the invariant, not a consequence of how `tier_states` happens
        to be written today.

        `tier_states` hardcodes `always: True`, so this case is unreachable through the
        normal path — which is exactly why it is asserted directly. If a later edit makes
        the always tier conditional (the same class of bug as the `on_shift or unknown`
        regression already fixed in `tier_states`), this test is what fails.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        jobs = self._all_jobs(enabled=True)
        svc = _FakeCronService(jobs)
        hostile = {
            rotation.TIER_ALWAYS: False,
            rotation.TIER_ON_SHIFT: False,
            rotation.TIER_PRIMARY: False,
        }
        with mock.patch.object(rotation, "tier_states", return_value=hostile):
            await rotation.apply_tiers(ShiftStatus(on_shift=False), svc)

        by_name = {job.name: job.enabled for job in jobs}
        self.assertTrue(
            by_name["ops-mission-control/rotation-check"],
            "apply_tiers paused an always-tier cron; the instance can no longer re-arm",
        )
        self.assertNotIn(("id-ops-mission-control/rotation-check", False), svc.calls)

    async def test_no_change_reports_an_empty_changed_list(self):
        """The cron is `silent: true` and runs every 5 minutes; a no-op must be
        distinguishable from work so the SOP can exit without output."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        jobs = self._all_jobs(enabled=True)
        svc = _FakeCronService(jobs)
        # Settle first, then re-apply the identical shift.
        await rotation.apply_tiers(ShiftStatus(on_shift=True), svc)
        svc.calls.clear()
        again = await rotation.apply_tiers(ShiftStatus(on_shift=True), svc)

        self.assertEqual(again["changed"], [])
        self.assertEqual(svc.calls, [])

    async def test_jobs_this_app_does_not_own_are_left_alone(self):
        """Arming is scoped to the tier table. A user's unrelated cron that happens to
        be paused must not be resumed as a side effect of a shift starting."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        stranger = _FakeJob("my-personal-backup", False)
        svc = _FakeCronService(self._all_jobs(enabled=False) + [stranger])
        await rotation.apply_tiers(ShiftStatus(on_shift=True), svc)

        self.assertFalse(stranger.enabled)
        self.assertNotIn(("id-my-personal-backup", True), svc.calls)

    async def test_missing_cron_service_is_reported_not_crashed(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        result = await rotation.apply_tiers(ShiftStatus(on_shift=True), None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "cron_service_unavailable")

    async def test_the_primary_tier_cron_is_not_armed_from_here(self):
        """`ledger-hygiene` ships ENABLED and is gated in its ROUTE (409 `not_primary`),
        not by pausing.

        Widening arming to the primary tier while fixing the self-disarm hole would stop
        the nightly job on every non-primary instance — a behavior change, not a security
        fix. Worse, `is_primary()` shells out to `gh api user`, so a network blip answering
        False would silently skip a night's ledger maintenance. Scope is pinned here so a
        later "arm every tier for symmetry" edit has to argue with a test.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        jobs = self._all_jobs(enabled=True)
        svc = _FakeCronService(jobs)
        # Not the primary owner, and off shift: the widest possible disarm.
        with mock.patch.object(rotation, "is_primary", return_value=False):
            await rotation.apply_tiers(ShiftStatus(on_shift=False), svc)

        by_name = {job.name: job.enabled for job in jobs}
        self.assertTrue(
            by_name["ops-mission-control/ledger-hygiene"],
            "arming paused the primary-tier cron; its gate is the route, not the scheduler",
        )

    async def test_arming_reads_a_fresh_job_list_not_the_cached_snapshot(self):
        """`apply_tiers` must use `list_jobs_async`, not `list_jobs`.

        Both are safe on the loop — `CronService.list_jobs` is explicitly cache-only — so
        this is about FRESHNESS, not blocking. The cached snapshot refreshes only once per
        timer poll, so a pause the operator just made from the CLI or the dashboard can
        still read as active here, and arming would helpfully "resume" a job they had
        deliberately stopped seconds earlier. Pinned because `list_jobs` is the obvious
        simplification and reads as equivalent.
        """
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        source = inspect.getsource(rotation.apply_tiers)
        self.assertIn("list_jobs_async", source)
        # No bare `.list_jobs(` call — `list_jobs_async(` contains `list_jobs` as a
        # substring, so match on the call punctuation.
        self.assertNotIn(".list_jobs(", source)


class TestTheWriteGateConsultsEveryRotation(_HomeIsolated):
    """`_definitely_off_shift` must honour EVERY configured rotation, not just the file.

    It read `rotation.yaml` and returned False at the first line when absent — so a PagerDuty
    rotation reporting "someone else is on call" was invisible to the write gate, and
    `/incident/action` executed a production write against a provider this operator was not on
    call for. The rotation was consulted for TIER arming (via `registry.resolve_shift`) and
    ignored for AUTHORIZATION, which is the path where it matters most. Found in review.
    """

    def _install(self, *sources):
        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        reg = registry.OpsProviderRegistry()
        registry._registry = reg
        self.addCleanup(registry.reset_registry)
        for src in sources:
            reg.register_rotation_source(src)
        return reg

    @staticmethod
    def _source(source_id, *, on_shift, unknown=False, fallback=False, configured=True):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        class _Src:
            id = source_id
            is_fallback = fallback

            def configured(self):
                return configured

            def _on_shift_sync(self):
                return ShiftStatus(on_shift=on_shift, unknown=unknown)

        return _Src()

    def test_an_api_rotation_reporting_off_shift_blocks_the_write(self):
        """The reported bug: no schedule file on disk, PagerDuty says off shift."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._source("pagerduty", on_shift=False))
        self.assertTrue(rotation._definitely_off_shift())

    def test_on_shift_anywhere_allows_the_write(self):
        """A person on two rotations is on call — matches `resolve_shift`'s algebra."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(
            self._source("pagerduty", on_shift=False),
            self._source("schedule-file", on_shift=True),
        )
        self.assertFalse(rotation._definitely_off_shift())

    def test_the_always_on_fallback_cannot_veto_a_real_rotation(self):
        """`AlwaysOnRotationSource` is always configured and always on-shift, so counting it
        would make every real rotation unhearable — the exact bug already fixed once in
        `resolve_shift`."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(
            self._source("always-on", on_shift=True, fallback=True),
            self._source("pagerduty", on_shift=False),
        )
        self.assertTrue(rotation._definitely_off_shift())

    def test_unknown_is_not_an_off_shift_vote(self):
        """An unreachable API must not block an operator driving an action by hand."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._source("pagerduty", on_shift=False, unknown=True))
        self.assertFalse(rotation._definitely_off_shift())

    def test_no_configured_rotation_allows_the_write(self):
        """A solo install has no rotation to be off."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        self._install(self._source("pagerduty", on_shift=False, configured=False))
        self.assertFalse(rotation._definitely_off_shift())

    def test_one_broken_source_does_not_decide_the_vote(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        class _Exploding:
            id = "boom"
            is_fallback = False

            def configured(self):
                return True

            def _on_shift_sync(self):
                raise RuntimeError("API down")

        self._install(_Exploding(), self._source("schedule-file", on_shift=True))
        self.assertFalse(rotation._definitely_off_shift())


class TestProposalExpiryIsAtomic(_HomeIsolated):
    """`expire_stale_proposals` must hold the lock across read-check-write.

    It read the index, tested each proposal, and wrote through `update_fields` — separate
    accesses with no lock held across them, so the heartbeat could read an expired draft, a
    concurrent request revise or decide it, and this stale write then stamp `expired` over the
    newer state: silently reverting an operator's decision. Same class as the `decide_proposal`
    race fixed one function up. Found in review.
    """

    def test_the_whole_sweep_is_locked(self):
        """Structural, and deliberately so.

        A behavioural test needs two threads inside one file lock, and every harness tried
        against the `decide_proposal` race produced one winner because the old write path took
        the lock itself — the concurrency was unobservable from the outside. What is actually
        assertable is that the sweep takes the lock and does not call the relocking helper.
        """
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        source = inspect.getsource(store.expire_stale_proposals)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn("_IndexLock()", code)
        self.assertIn("_read_index_unlocked()", code)
        self.assertNotIn(
            "update_fields(",
            code,
            "the sweep re-enters the lock through update_fields instead of writing in place",
        )
        self.assertNotIn(
            "read_index()",
            code.replace("_read_index_unlocked()", ""),
            "the sweep reads through the unlocked snapshot",
        )

    def test_expiry_still_marks_a_past_ttl_proposal(self):
        """The lock must not change what the sweep does."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/x", title="x broke"),
            operating_mode=models.MODE_PROPOSE,
        )
        assert inc is not None
        store.update_fields(
            inc.incident_id,
            proposed_action={
                "state": models.PROPOSAL_PENDING,
                "action": "ack",
                "expires_at": "2000-01-01T00:00:00Z",
            },
        )
        touched = store.expire_stale_proposals()
        self.assertEqual(touched, [inc.incident_id])
        reloaded = store.get_incident(inc.incident_id)
        assert reloaded is not None
        assert reloaded.proposed_action is not None
        self.assertEqual(reloaded.proposed_action["state"], models.PROPOSAL_EXPIRED)

    def test_a_future_ttl_proposal_is_untouched(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/y", title="y broke"),
            operating_mode=models.MODE_PROPOSE,
        )
        assert inc is not None
        store.update_fields(
            inc.incident_id,
            proposed_action={
                "state": models.PROPOSAL_PENDING,
                "action": "ack",
                "expires_at": "2999-01-01T00:00:00Z",
            },
        )
        self.assertEqual(store.expire_stale_proposals(), [])
