"""Ledger sync against a REAL git repo, not a mocked one.

Every bug these tests pin was invisible to mocked-git unit tests and fatal in practice.
A two-instance roundtrip against a bare remote found four, in the order they bit:

1. **The first push in a fresh process always failed.** The sandbox backend probe defers
   to a background thread on a cold cache and raises a self-described TRANSIENT error
   saying "retry"; ``push`` did not catch it, so the whole first sync errored for a
   condition that resolves in milliseconds.
2. **An instance with a local ledger could never pull.** ``git merge`` refuses when an
   untracked working-tree file would be overwritten, so any install that recorded even
   one lesson before its first pull was permanently unable to receive the team's.
3. **The second teammate to join could never merge.** Each instance runs its own
   ``git init``, so their histories are genuinely unrelated and git refuses outright.
   That is the ORDINARY multi-instance case.
4. **``rotation.yaml`` would never have been committed.** ``push`` ran
   ``git add ledger.jsonl`` only, so the on-call schedule — un-ignored specifically so it
   could sync — would have been committed nowhere and silently never reached anyone.

These tests are slower than the mocked ones on purpose. The whole feature is "git moves
the text", so the thing worth testing is git.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, timeout=60, check=False
    )
    return proc.stdout.decode("utf-8", "replace")


class _TwoInstances(unittest.IsolatedAsyncioTestCase):
    """A bare remote plus two independent data homes — two teammates, one repo."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.remote = self.root / "remote.git"
        self.remote.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main", str(self.remote)],
            capture_output=True,
            timeout=60,
            check=True,
        )
        self.home_a = self.root / "a"
        self.home_b = self.root / "b"
        self.home_a.mkdir()
        self.home_b.mkdir()
        self._prev = os.environ.get("KIROCREW_HOME")
        # Snapshot the module table. ``_use`` evicts this app's modules to simulate two
        # separate processes, and WITHOUT restoring them the eviction leaks into every
        # later test in the same process: a sibling that had already imported
        # ``routes``/``ledger_sync`` ends up patching a stale module object while the
        # handler under test resolves a fresh one, so its mock silently never applies.
        # Observed exactly that — four unrelated test_routes failures that passed when
        # that file ran alone. A test that breaks other tests is a bug in the test.
        self._modules = dict(sys.modules)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        # Restore the exact table we started with: put back what we evicted, and drop
        # the replacements we imported, so the next test sees the process as it was.
        for name in list(sys.modules):
            if name not in self._modules:
                del sys.modules[name]
        sys.modules.update(self._modules)
        shutil.rmtree(self.root, ignore_errors=True)

    def _use(self, home: Path):
        """Point the app at one instance's data home and reload its modules.

        Module reload is required: ``ledger_sync`` resolves the repo root from the data
        home through ``app_data_dir``, which caches. Re-importing is the honest way to
        simulate two separate processes inside one test. ``tearDown`` restores the table.
        """
        os.environ["KIROCREW_HOME"] = str(home)
        for name in list(sys.modules):
            if "ops_mission_control" in name or name.startswith("kiro_crew.apps.manager"):
                del sys.modules[name]
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, ledger_sync
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        ledger_sync.set_settings(remote_url=str(self.remote), branch_name="main", enabled=True)
        return ledger, ledger_sync, LedgerEntry


class TestRoundTrip(_TwoInstances):
    async def test_a_lesson_reaches_the_other_instance(self):
        """The whole point of the feature, end to end."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="DLQ fills on AccessDenied", fix="fix the policy"))
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        patterns = [e.pattern for e in ledger.read_entries()]
        self.assertIn("DLQ fills on AccessDenied", patterns)

    async def test_the_second_teammate_can_merge_unrelated_histories(self):
        """Bug 3: each instance runs its own `git init`, so roots are unrelated.

        Both write BEFORE either pulls — the ordinary case of two people installing
        independently. Git refuses this outright without --allow-unrelated-histories.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="A-only lesson here", fix="the A fix"))
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_b)
        ledger.upsert(entry.create(pattern="B-only lesson here", fix="the B fix"))
        detail = await sync.sync_safely(direction="pull")

        self.assertNotIn("unrelated histories", detail)
        patterns = sorted(e.pattern for e in ledger.read_entries())
        self.assertEqual(
            patterns,
            ["A-only lesson here", "B-only lesson here"],
            "the union must survive — neither side's work may be dropped",
        )

    async def test_a_local_ledger_does_not_block_the_first_pull(self):
        """Bug 2: 'Untracked working tree file would be overwritten by merge'."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="team lesson from A", fix="the shared fix"))
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_b)
        # B has its OWN untracked ledger before ever pulling.
        ledger.upsert(entry.create(pattern="local lesson on B", fix="the local fix"))
        detail = await sync.sync_safely(direction="pull")

        self.assertNotIn("would be overwritten", detail)
        self.assertNotIn("merge failed", detail)
        self.assertEqual(len(ledger.read_entries()), 2)

    async def test_concurrent_writers_converge_without_losing_an_entry(self):
        """The case a shared ledger exists for: both write, neither saw the other."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="shared baseline lesson", fix="common fix"))
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        ledger.upsert(entry.create(pattern="B-only throttling issue", fix="raise concurrency"))
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="A-only disk full issue", fix="rotate logs"))
        # A is stale: the push MUST be rejected rather than overwrite B's work.
        stale = await sync.sync_safely(direction="push")
        self.assertIn("push failed", stale, "a stale push must not clobber the remote")

        await sync.sync_safely(direction="pull")
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")
        a_final = sorted(e.pattern for e in ledger.read_entries())
        self.assertEqual(len(a_final), 3)

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        b_final = sorted(e.pattern for e in ledger.read_entries())
        self.assertEqual(a_final, b_final, "both instances must converge on the same ledger")


class TestWhatGetsCommitted(_TwoInstances):
    async def test_the_rotation_schedule_syncs(self):
        """Bug 4: push staged only ledger.jsonl, so the schedule reached nobody."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (  # noqa: E501
            schedule_file,
        )

        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        path = schedule_file.schedule_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n",
            encoding="utf-8",
        )
        await sync.sync_safely(direction="push")

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        self.assertTrue(
            schedule_file.schedule_path().exists(),
            "rotation.yaml must reach teammates or the schedule is local-only",
        )
        self.assertIn("octocat", schedule_file.schedule_path().read_text(encoding="utf-8"))

    async def test_the_dispatch_index_is_never_pushed(self):
        """It is not merge-safe, and it is local state. Pushing it would corrupt peers."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        index = ledger.ledger_path().parent / "index.json"
        index.write_text('{"local": "state"}', encoding="utf-8")
        await sync.sync_safely(direction="push")

        tracked = _git(ledger.ledger_path().parent, "ls-files")
        self.assertIn("ledger.jsonl", tracked)
        self.assertNotIn("index.json", tracked)

    async def test_provider_config_is_never_pushed(self):
        """Config can name a log group an operator considers private."""
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        cfg = ledger.ledger_path().parent / "config.json"
        cfg.write_text('{"cloudwatch": {"log_groups": ["/private/thing"]}}', encoding="utf-8")
        await sync.sync_safely(direction="push")

        tracked = _git(ledger.ledger_path().parent, "ls-files")
        self.assertNotIn("config.json", tracked)


class TestFaultTolerance(_TwoInstances):
    async def test_an_unreachable_remote_is_survived(self):
        """Sync is a convenience; it must never raise into a caller."""
        ledger, sync, entry = self._use(self.home_a)
        sync.set_settings(remote_url=str(self.root / "does-not-exist.git"))
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        detail = await sync.sync_safely(direction="push")
        self.assertIsInstance(detail, str, "must return a string, never raise")

    async def test_unconfigured_sync_is_a_quiet_noop(self):
        ledger, sync, entry = self._use(self.home_a)
        sync.set_settings(enabled=False)
        self.assertEqual(await sync.sync_safely(direction="pull"), "")

    async def test_pushing_twice_is_idempotent(self):
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")
        again = await sync.sync_safely(direction="push")
        self.assertEqual(again, "nothing to push")

    async def test_a_locally_committed_but_unpushed_entry_is_not_stranded(self):
        """A clean tree is not proof everything is shared.

        If a previous run committed and then failed to reach the remote, an early
        'nothing to push' on a clean tree would strand that commit forever.
        """
        ledger, sync, entry = self._use(self.home_a)
        ledger.upsert(entry.create(pattern="a lesson to carry", fix="a fix"))
        # Commit locally, but point at a dead remote so the push cannot land.
        sync.set_settings(remote_url=str(self.root / "dead.git"))
        await sync.sync_safely(direction="push")
        # Now the real remote comes back: the tree is clean but HEAD is unpushed.
        sync.set_settings(remote_url=str(self.remote))
        self.assertEqual(await sync.sync_safely(direction="push"), "pushed")

        ledger, sync, entry = self._use(self.home_b)
        await sync.sync_safely(direction="pull")
        self.assertEqual(len(ledger.read_entries()), 1, "the stranded entry arrived")


if __name__ == "__main__":
    unittest.main()
