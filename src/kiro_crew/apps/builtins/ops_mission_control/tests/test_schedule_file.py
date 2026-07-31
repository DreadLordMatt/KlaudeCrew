"""Tests for the git-native on-call schedule.

The owner's ask was a rotation without a rotation service: a YAML file in the same repo
the ledger syncs through, keyed on GitHub logins. The properties worth pinning, in order
of what would hurt most if broken:

1. **A real rotation must be hearable.** The always-on default is always configured and
   always on-shift, so before this it masked every real source — a schedule saying
   "someone else is on call" could not be heard at all. That defeats the entire feature.
2. **Every failure arms, never disarms.** Missing file, bad YAML, no login, expired
   schedule — all must resolve to ``unknown`` (tier armed). Wrongly disarming costs
   missed incidents; wrongly arming costs cheap polls in an observe-only tier.
3. **A date-only ``to`` includes that day.** ``to: 2026-08-08`` means "through the 8th".
   Reading it as midnight would silently drop the last day of every shift.
4. **The file is untrusted input.** It arrives by ``git pull`` from a shared repo, so it
   must be size-capped and parsed with ``safe_load``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend.providers import schedule_file
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import ShiftStatus


class _Env(unittest.TestCase):
    """Isolated data home, and a stubbed login so no test shells out to ``gh``."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        schedule_file.reset_login_cache()
        self._login = mock.patch.object(
            schedule_file, "_resolve_login_sync", return_value="octocat"
        )
        self._login.start()

    def tearDown(self) -> None:
        self._login.stop()
        schedule_file.reset_login_cache()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write(body: str) -> Path:
        path = schedule_file.schedule_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    @staticmethod
    def _at(iso: str) -> datetime:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


class TestOnShiftResolution(_Env):
    def test_operator_named_in_the_current_window_is_on_shift(self) -> None:
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n")
        status = schedule_file.resolve_now(self._at("2026-08-03T12:00"))
        self.assertTrue(status.on_shift)
        self.assertFalse(status.unknown, "a matched window is a definitive answer")
        self.assertEqual(status.who, "octocat")

    def test_someone_else_on_shift_means_off_shift(self) -> None:
        """The whole point: the file must be able to say 'not you'."""
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: hubot\n")
        status = schedule_file.resolve_now(self._at("2026-08-03T12:00"))
        self.assertFalse(status.on_shift)
        self.assertFalse(status.unknown, "a covered window is definitive even when it is not you")
        self.assertEqual(status.who, "hubot")

    def test_co_primary_list_counts(self) -> None:
        self._write(
            "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: [hubot, octocat]\n"
        )
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-03T12:00")).on_shift)

    def test_login_match_is_case_insensitive(self) -> None:
        """GitHub logins are case-insensitive; 'Octocat' in the file must still match."""
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: Octocat\n")
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-03T12:00")).on_shift)

    def test_date_only_end_includes_the_final_day(self) -> None:
        """`to: 2026-08-08` means through the 8th, not midnight at its start.

        Read as 00:00 this drops the last day of every shift written date-only — the
        single most likely misreading of this file format.
        """
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n")
        self.assertTrue(
            schedule_file.resolve_now(self._at("2026-08-08T18:00")).on_shift,
            "the 8th is still on shift",
        )
        self.assertTrue(
            schedule_file.resolve_now(self._at("2026-08-09T00:30")).unknown,
            "the 9th falls outside every window",
        )

    def test_explicit_times_are_honored(self) -> None:
        self._write(
            "shifts:\n  - from: 2026-08-01T09:00\n    to: 2026-08-01T17:00\n    who: octocat\n"
        )
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-01T12:00")).on_shift)
        self.assertTrue(
            schedule_file.resolve_now(self._at("2026-08-01T18:00")).unknown,
            "after the window is a schedule gap, which arms rather than disarms",
        )

    def test_timezone_shifts_the_window(self) -> None:
        """A team writing local times must not be silently interpreted as UTC."""
        self._write(
            "timezone: America/Los_Angeles\n"
            "shifts:\n  - from: 2026-08-01T09:00\n    to: 2026-08-01T17:00\n    who: octocat\n"
        )
        # 09:00 PDT == 16:00 UTC, so 15:00 UTC is BEFORE the shift starts.
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-01T15:00")).unknown)
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-01T20:00")).on_shift)

    def test_unknown_timezone_falls_back_to_utc(self) -> None:
        self._write(
            "timezone: Mars/Olympus_Mons\n"
            "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n"
        )
        self.assertTrue(schedule_file.resolve_now(self._at("2026-08-03T12:00")).on_shift)


class TestFailuresArmRatherThanDisarm(_Env):
    """Every degradation must leave the tier ARMED. See ShiftStatus.unknown."""

    def _assert_armed_unknown(self, status: ShiftStatus) -> None:
        self.assertTrue(status.on_shift, "must arm")
        self.assertTrue(status.unknown, "must not claim to be a real answer")

    def test_missing_file(self) -> None:
        self._assert_armed_unknown(schedule_file.resolve_now())

    def test_malformed_yaml(self) -> None:
        self._write("shifts: [unclosed\n")
        self._assert_armed_unknown(schedule_file.resolve_now())

    def test_yaml_that_is_not_a_mapping(self) -> None:
        self._write("- just\n- a\n- list\n")
        self._assert_armed_unknown(schedule_file.resolve_now())

    def test_no_shifts_key(self) -> None:
        self._write("timezone: UTC\n")
        self._assert_armed_unknown(schedule_file.resolve_now())

    def test_expired_schedule(self) -> None:
        """A rotation nobody refilled must not disable everyone's response."""
        self._write("shifts:\n  - from: 2020-01-01\n    to: 2020-01-08\n    who: octocat\n")
        self._assert_armed_unknown(schedule_file.resolve_now(self._at("2026-08-03T12:00")))

    def test_unresolvable_login(self) -> None:
        """No gh, no configured login: we cannot know, so we arm."""
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n")
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value=""):
            self._assert_armed_unknown(schedule_file.resolve_now(self._at("2026-08-03T12:00")))

    def test_reversed_window_is_skipped_not_honored(self) -> None:
        self._write("shifts:\n  - from: 2026-08-08\n    to: 2026-08-01\n    who: octocat\n")
        self._assert_armed_unknown(schedule_file.resolve_now(self._at("2026-08-03T12:00")))

    def test_entry_with_no_who_is_skipped(self) -> None:
        self._write("shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n")
        self._assert_armed_unknown(schedule_file.resolve_now(self._at("2026-08-03T12:00")))


class TestUntrustedInput(_Env):
    """The file arrives by `git pull` from a shared repo — treat it as hostile."""

    def test_oversized_file_is_refused(self) -> None:
        self._write("shifts:\n" + ("  # padding padding padding\n" * 20_000))
        self.assertGreater(
            schedule_file.schedule_path().stat().st_size, schedule_file.MAX_SCHEDULE_BYTES
        )
        _, _, error = schedule_file.read_schedule()
        self.assertIn("exceeds", error)

    def test_yaml_uses_safe_load(self) -> None:
        """A pushed schedule must not be able to construct arbitrary Python."""
        source = Path(schedule_file.__file__).read_text(encoding="utf-8")
        self.assertIn("yaml.safe_load", source)
        self.assertNotIn("yaml.load(", source)

    def test_shift_count_is_bounded(self) -> None:
        body = "shifts:\n" + "".join(
            f"  - from: 2026-01-01\n    to: 2026-01-02\n    who: u{n}\n" for n in range(20)
        )
        self._write(body)
        with mock.patch.object(schedule_file, "MAX_SHIFTS", 5):
            shifts, _, error = schedule_file.read_schedule()
        self.assertEqual(error, "")
        self.assertEqual(len(shifts), 5, "scan is capped")

    def test_schedule_filename_is_not_operator_configurable(self) -> None:
        """Every teammate must read the SAME file, or they disagree about the rotation."""
        self.assertEqual(schedule_file.SCHEDULE_FILENAME, "rotation.yaml")
        self.assertTrue(str(schedule_file.schedule_path()).endswith("rotation.yaml"))


class TestLivesInTheSyncedRepo(_Env):
    def test_schedule_sits_beside_the_ledger(self) -> None:
        """It must be inside the repo ledger_sync pushes, or it never reaches teammates."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        self.assertEqual(schedule_file.schedule_path().parent, ledger.ledger_path().parent)

    def test_sync_tracks_the_schedule_file(self) -> None:
        """ledger_sync's .gitignore tracks the ledger and nothing else by default —
        the schedule must be tracked too or it is written locally and never shared."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        source = Path(ledger_sync.__file__).read_text(encoding="utf-8")
        self.assertIn(
            schedule_file.SCHEDULE_FILENAME,
            source,
            "ledger_sync must un-ignore rotation.yaml, or the schedule never syncs",
        )


class TestAdapterContract(_Env):
    def test_unconfigured_when_no_schedule_exists(self) -> None:
        self.assertFalse(schedule_file.ScheduleFileRotationSource().configured())

    def test_configured_once_a_schedule_is_committed(self) -> None:
        self._write("shifts: []\n")
        self.assertTrue(schedule_file.ScheduleFileRotationSource().configured())

    def test_configured_does_not_require_a_login(self) -> None:
        """The common case is a committed file plus an already-authenticated gh."""
        self._write("shifts: []\n")
        with mock.patch.object(schedule_file, "_resolve_login_sync", return_value=""):
            self.assertTrue(schedule_file.ScheduleFileRotationSource().configured())

    def test_status_reports_without_raising_on_a_missing_file(self) -> None:
        status = schedule_file.status()
        self.assertFalse(status["present"])
        self.assertTrue(status["unknown"])
        self.assertIn("rotation.yaml", status["detail"])

    def test_status_counts_shifts(self) -> None:
        self._write(
            "timezone: UTC\nshifts:\n"
            "  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n"
            "  - from: 2026-08-08\n    to: 2026-08-15\n    who: hubot\n"
        )
        status = schedule_file.status()
        self.assertEqual(status["shifts"], 2)
        self.assertEqual(status["timezone"], "UTC")

    def test_status_exposes_no_secret_fields(self) -> None:
        """A GitHub login is not a credential; this adapter must add no secret."""
        self.assertEqual(schedule_file.ScheduleFileRotationSource().secret_fields, ())


class TestLoginResolution(_Env):
    def test_configured_login_wins_over_shelling_out(self) -> None:
        """An operator who set a login must not pay a `gh` spawn per rotation tick."""
        self._login.stop()
        try:
            gh_calls = []

            def _counting_run(argv, *a, **kw):
                # Match the whole argv, not argv[0]: sandboxed_spawn_argv PREPENDS a
                # wrapper, so `gh` is no longer element 0 and an argv[0] check silently
                # lets the real `gh` run (observed: it returned the developer's login).
                if "api" in argv and "user" in argv:
                    gh_calls.append(argv)
                raise AssertionError("no spawn should happen at all on this path")

            with mock.patch.object(
                schedule_file, "config_value", return_value="configured-user"
            ) as cfg:
                with mock.patch.object(schedule_file.subprocess, "run", side_effect=_counting_run):
                    self.assertEqual(schedule_file._resolve_login_sync(), "configured-user")
            self.assertEqual(gh_calls, [], "a configured login must not shell out")
            self.assertTrue(cfg.called)
        finally:
            self._login.start()

    def test_gh_failure_is_cached_so_it_does_not_reshell_every_tick(self) -> None:
        """The rotation-check cron runs on a schedule; an unbounded re-spawn per tick
        on a machine with no `gh` is pure waste.

        Counts only OUR ``gh`` invocations. A bare ``subprocess.run`` call count is the
        wrong assertion here: ``sandboxed_spawn_argv`` makes its own probe (``ssh -V``)
        on the way in, so a broad patch counts the sandbox layer's spawns as if they
        were ours and reports 2 for a single ``gh`` attempt.
        """
        self._login.stop()
        try:
            schedule_file.reset_login_cache()

            gh_calls = []
            real_run = schedule_file.subprocess.run

            def _counting_run(argv, *a, **kw):
                # Match the whole argv, not argv[0]: sandboxed_spawn_argv PREPENDS a
                # wrapper, so `gh` is no longer element 0 and an argv[0] check silently
                # lets the real `gh` run (observed: it returned the developer's login).
                if "api" in argv and "user" in argv:
                    gh_calls.append(argv)
                    raise OSError("no gh")
                return real_run(argv, *a, **kw)

            with mock.patch.object(schedule_file, "config_value", return_value=""):
                with mock.patch.object(schedule_file.subprocess, "run", side_effect=_counting_run):
                    self.assertEqual(schedule_file._resolve_login_sync(), "")
                    self.assertEqual(schedule_file._resolve_login_sync(), "")
            self.assertEqual(len(gh_calls), 1, "the miss is cached too")
        finally:
            self._login.start()

    def test_login_lookup_is_routed_through_the_spawn_chokepoint(self) -> None:
        """test/test_spawn_audit.py requires it, and a rotation check is agent-reachable."""
        source = Path(schedule_file.__file__).read_text(encoding="utf-8")
        self.assertIn("sandboxed_spawn_argv", source)
        self.assertIn("resource_limit_preexec", source)


if __name__ == "__main__":
    unittest.main()
