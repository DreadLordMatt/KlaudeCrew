"""Regressions in the attribution core, each one a correctness rule that is easy
to reintroduce.

Every test here builds real git history rather than stubbing git, because the
defects these lock out were all in how the engine *talks to* git -- the wrong
subcommand, the wrong coordinate, the wrong parent -- which a stub would happily
reproduce.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from kiro_crew.apps.builtins.pr_postmortem.engine import attribution, bundle, vcs
from kiro_crew.apps.builtins.pr_postmortem.engine.diffparse import parse_pre_image
from kiro_crew.apps.builtins.pr_postmortem.tests import support


class TestBlameKeying(unittest.TestCase):
    """Blame output must be keyed by the FINAL line number, not the original one.

    Keying by the original collides whenever two lines from different commits
    share a position in their own source file: one silently overwrites the other,
    and the per-commit line counts that drive the weighting come out short.
    """

    def test_two_commits_whose_original_line_numbers_collide_are_both_counted(self):
        # The two lines must have the SAME original line number in their own
        # commits and DIFFERENT final ones -- otherwise keying by either coordinate
        # gives the same answer and the test proves nothing. Prepending achieves
        # it: "one" was line 1 when it was written and is line 2 afterwards, while
        # "zero" is line 1 in the commit that added it.
        with tempfile.TemporaryDirectory() as repo:
            support.git(["init", "-q", "-b", "main"], repo)
            support.write(repo, "a.py", "one\n")
            first = support.commit(repo, "write one")
            support.write(repo, "a.py", "zero\none\n")
            second = support.commit(repo, "prepend zero")

            blamed = attribution._blame_range(repo, second, "a.py", 1, 2, False)

            self.assertEqual(
                {1: second, 2: first}, blamed,
                "keying by the ORIGINAL line number collapses both lines onto "
                f"key 1 and loses one commit: {blamed}",
            )


class TestMergeCommitDiff(unittest.TestCase):
    """`diff <fix>^ <fix>` -- never `show <fix>`.

    `git show` prints no diff at all for a merge commit, so a repo that merges
    rather than squashes would yield an empty pre-image and a
    `no_pre_image_signal` verdict for every fix: the tool finding nothing while
    looking like it worked.
    """

    def _repo_with_merge(self, repo: str) -> str:
        support.git(["init", "-q", "-b", "main"], repo)
        support.write(repo, "a.py", "base\n")
        support.commit(repo, "base")
        support.git(["checkout", "-q", "-b", "feature"], repo)
        support.write(repo, "a.py", "base\nfeature change\n")
        support.commit(repo, "feature work")
        support.git(["checkout", "-q", "main"], repo)
        support.git(["merge", "--no-ff", "-q", "-m", "Merge feature (#42)", "feature"],
                   repo)
        return support.git(["rev-parse", "HEAD"], repo).strip()

    def test_show_is_empty_but_diff_against_first_parent_is_not(self):
        with tempfile.TemporaryDirectory() as repo:
            merge = self._repo_with_merge(repo)

            shown = vcs.git(["show", "--unified=0", "--no-color", merge], repo)
            self.assertEqual(
                _diff_body(shown), "",
                "`git show` on a merge is expected to carry no diff -- if this "
                "ever changes, the reason for using `diff <c>^ <c>` is gone",
            )

            diffed = vcs.git(
                ["diff", "--unified=0", "--no-color", f"{merge}^", merge], repo
            )
            self.assertIn("feature change", diffed)
            self.assertTrue(parse_pre_image(diffed), "the merge diff must parse")

    def test_attribute_finds_pre_image_signal_on_a_merge_commit(self):
        """The end-to-end consequence: a merging repo must still yield a culprit.

        This is the test that fails if the engine goes back to `git show` -- the
        one above only characterises git's own behaviour.
        """
        with tempfile.TemporaryDirectory() as repo:
            support.git(["init", "-q", "-b", "main"], repo)
            support.write(repo, "a.py", "value = compute()\n")
            support.commit(repo, "introduce it (#7)")
            # A merge commit that CHANGES the line the culprit wrote.
            support.git(["checkout", "-q", "-b", "fix"], repo)
            support.write(repo, "a.py", "value = compute() or default()\n")
            support.commit(repo, "guard it")
            support.git(["checkout", "-q", "main"], repo)
            support.git(["merge", "--no-ff", "-q", "-m", "Merge fix (#8)", "fix"], repo)
            merge = support.git(["rev-parse", "HEAD"], repo).strip()

            with mock.patch.object(vcs, "gh_json", return_value=None), \
                    mock.patch.object(vcs, "merge_commit_for_pr", return_value=merge):
                att = attribution.attribute("o/r", repo, 8)

            self.assertNotIn(
                "no_pre_image_signal", str(att.verdict),
                "a merge commit must still produce pre-image signal",
            )
            self.assertEqual([7], [c.pr for c in att.candidates])


def _diff_body(text: str) -> str:
    """The diff hunks of git output, with the commit header stripped."""
    idx = text.find("diff --git")
    return text[idx:].strip() if idx >= 0 else ""


class TestEvidenceSpans(unittest.TestCase):
    """One evidence row per CONTIGUOUS run, not one row spanning min..max.

    With interleaved authorship a collapsed span claims lines the culprit never
    wrote. The evidence a human reads to judge a verdict IS this app's product, so
    an overstated span is a correctness bug even though the weighting is unchanged.
    """

    def test_interleaved_authorship_does_not_collapse_into_one_span(self):
        runs = attribution._contiguous_runs([10, 11, 20])
        self.assertEqual([[10, 11], [20]], [list(r) for r in runs])

    def test_a_single_line_is_its_own_run(self):
        self.assertEqual([[7]], [list(r) for r in attribution._contiguous_runs([7])])

    def test_no_lines_yields_no_runs(self):
        self.assertEqual([], [list(r) for r in attribution._contiguous_runs([])])

    def test_assembled_evidence_rows_never_span_another_commit_s_line(self):
        """The product-level property: the helper is only useful if the row
        assembly actually uses it, so this goes through `attribute()`.

        One commit owns lines 1, 2 and 4 while another owns line 3, so a collapsed
        span would read "1-4" and claim a line the culprit never wrote.
        """
        with tempfile.TemporaryDirectory() as repo:
            support.git(["init", "-q", "-b", "main"], repo)
            support.write(repo, "a.py", "a1\na2\nmid\na4\n")
            support.commit(repo, "write the block (#3)")
            support.write(repo, "a.py", "a1\na2\nb3\na4\n")
            support.commit(repo, "replace the middle line (#4)")
            support.write(repo, "a.py", "rewritten\n")
            support.commit(repo, "fix it (#5)")
            head = support.git(["rev-parse", "HEAD"], repo).strip()

            with mock.patch.object(vcs, "gh_json", return_value=None), \
                    mock.patch.object(vcs, "merge_commit_for_pr", return_value=head):
                att = attribution.attribute("o/r", repo, 5)

            # Selected by PR: a row stores an ABBREVIATED sha, so comparing it to a
            # full one silently matches nothing and the assertion would be vacuous.
            spans = [r.pre_image_lines for r in att.evidence if r.culprit_pr == 3]
            self.assertEqual(
                {"1-2", "4"}, set(spans),
                f"expected two runs for the culprit, got {spans} -- a span of "
                f"'1-4' would claim line 3, which another commit wrote",
            )
            self.assertEqual(
                ["3"], [r.pre_image_lines for r in att.evidence if r.culprit_pr == 4],
                "the interleaved line must be attributed to its own author",
            )


class TestUnmappedCommitsKeepSeparateIdentities(unittest.TestCase):
    """Commits with no PR mapping must NOT collapse into one candidate.

    Grouping on the PR number alone puts every unmapped commit under the same
    ``None`` key, so unrelated commits merge into a single candidate whose weight
    is the sum of theirs -- which can outrank the real culprit.
    """

    def test_two_unmapped_commits_produce_two_candidates(self):
        with tempfile.TemporaryDirectory() as repo:
            support.git(["init", "-q", "-b", "main"], repo)
            # Two culprit commits with NO "(#n)" subject, so neither maps to a PR.
            support.write(repo, "a.py", "alpha\n")
            support.commit(repo, "alpha with no pr reference")
            support.write(repo, "a.py", "alpha\nbeta\n")
            support.commit(repo, "beta with no pr reference")
            # The fix deletes both lines, so blame implicates both commits.
            support.write(repo, "a.py", "replaced\n")
            support.commit(repo, "fix it (#99)")

            with mock.patch.object(vcs, "gh_json", return_value=None), \
                    mock.patch.object(vcs, "merge_commit_for_pr",
                                      return_value=support.git(
                                          ["rev-parse", "HEAD"], repo).strip()):
                att = attribution.attribute("o/r", repo, 99)

            prs = [c.pr for c in att.candidates]
            self.assertEqual(
                2, len(att.candidates),
                f"each unmapped commit needs its own candidate, got {prs}",
            )
            self.assertEqual([None, None], prs)
            self.assertNotEqual(
                att.candidates[0].commits, att.candidates[1].commits,
                "two candidates must not describe the same commit",
            )


class TestGitHelperIsolatesTheHost(unittest.TestCase):
    """The shared runner must exclude the host's git configuration.

    Without this, a developer whose global config sets `core.hooksPath` or
    `init.templateDir` would have their own hooks executed by `git commit` here --
    arbitrary code, triggered merely by running the suite. The isolation is also
    what keeps assertions from depending on host settings such as a default branch
    name or a commit template.
    """

    def test_the_runner_neutralises_global_and_system_config(self):
        env = support.git_env()
        self.assertEqual(os.devnull, env["GIT_CONFIG_GLOBAL"])
        self.assertEqual(os.devnull, env["GIT_CONFIG_SYSTEM"])

    def test_a_value_in_a_global_config_file_is_not_visible_to_the_runner(self):
        # Prove the mechanism, not just the env var: point GIT_CONFIG_GLOBAL at a
        # real config file carrying a distinctive value, and confirm it does not
        # reach the runner. No executable file is involved, so this exercises the
        # same override that neutralises `core.hooksPath`.
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as repo:
            cfg = support.write(home, "gitconfig",
                                "[user]\n\tname = Host Config Leaked\n")
            support.git(["init", "-q", "-b", "main"], repo)
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": cfg},
                                 clear=False):
                leaked = support.git(["config", "user.name"], repo,
                                     check=False).strip()
                support.write(repo, "a.py", "x = 1\n")
                support.commit(repo, "commit under a hostile global config")
                author = support.git(["log", "-1", "--format=%an"], repo).strip()

        self.assertNotEqual("Host Config Leaked", leaked,
                            "the host's global config must not reach the runner")
        self.assertEqual("Test Author", author,
                         "the runner's own identity must be what lands in history")


class TestExecutablesNeverComeFromPath(unittest.TestCase):
    """`git` and `gh` resolve from trusted system directories, never from PATH.

    A gateway's PATH can legitimately lead with agent-writable directories, and this
    engine is driven by a scheduled scan -- the unattended context where a planted
    shim inheriting the gateway environment would go unnoticed.
    """

    def test_git_is_invoked_by_absolute_path(self):
        seen: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(subprocess, "run", fake_run):
            vcs.git(["status"], "/nonexistent")

        argv0 = str(seen["cmd"][0])  # type: ignore[index]
        self.assertTrue(os.path.isabs(argv0), f"argv[0] must be absolute: {argv0!r}")
        self.assertNotEqual("git", argv0)

    def test_an_unresolvable_git_fails_closed_rather_than_using_path(self):
        with mock.patch.object(vcs, "resolve_bin", return_value=None):
            with self.assertRaises(vcs.GitError) as caught:
                vcs.git(["status"], "/nonexistent")
        self.assertIn("refusing to resolve it through PATH", str(caught.exception))

    def test_an_unresolvable_gh_degrades_to_none(self):
        # gh is optional and every gh path has a local-git fallback, so an absent
        # or untrusted binary must read as "unavailable", not raise.
        with mock.patch.object(vcs, "resolve_bin", return_value=None):
            self.assertIsNone(vcs.gh_json(["pr", "view", "1"]))

    def test_a_writable_candidate_is_refused(self):
        # The whole point: an install under a directory this user can write is
        # exactly where a planted shim would go. A temp dir stands in for
        # ~/.local/bin, which is writable by construction.
        with tempfile.TemporaryDirectory() as bindir:
            planted = support.write(bindir, "git", "#!/bin/sh\nexit 0\n")
            os.chmod(planted, 0o700)
            with mock.patch.dict(vcs._CANDIDATES, {"git": (planted,)}):
                self.assertIsNone(
                    vcs.resolve_bin("git"),
                    "a binary in a user-writable directory must be refused",
                )

    def test_a_non_writable_candidate_is_accepted(self):
        # /usr/bin is the ordinary case on every CI platform this runs on.
        resolved = vcs.resolve_bin("git")
        self.assertIsNotNone(resolved, "git must resolve on a normal host")
        self.assertTrue(os.path.isabs(str(resolved)))

    def test_the_pinned_lists_hold_only_absolute_paths(self):
        for name, candidates in vcs._CANDIDATES.items():
            self.assertTrue(candidates, name)
            for candidate in candidates:
                windows = candidate[1:3] == ":\\"
                self.assertTrue(
                    candidate.startswith("/") or windows,
                    f"{name}: {candidate!r} is not an absolute path",
                )


class TestPrLookupIsNotDepthLimited(unittest.TestCase):
    """The targeted `(#n)` search must not carry a commit cap.

    A cap changes the answer by DEPTH rather than by relevance: a fix that merged
    beyond it is reported as `fix_commit_not_found`, and that empty result then
    overwrites a good report for the same pair.
    """

    def test_the_log_search_passes_no_max_count(self):
        calls: list[list[str]] = []

        def fake_git(args, repo_path, check=True):
            calls.append(list(args))
            return ""

        with mock.patch.object(vcs, "git", fake_git), \
                mock.patch.object(vcs, "gh_json", return_value=None):
            vcs.merge_commit_for_pr(1799, "o/r", "/nonexistent", "origin/main")

        self.assertTrue(calls, "the local search must run before falling back to gh")
        args = calls[0]
        self.assertIn("--grep", args)
        numeric_caps = [a for a in args if re.fullmatch(r"-\d+", a)]
        self.assertEqual([], numeric_caps,
                         f"no commit cap allowed, found {numeric_caps}")

    def test_a_fix_far_behind_the_tip_is_still_found(self):
        with tempfile.TemporaryDirectory() as repo:
            support.git(["init", "-q", "-b", "main"], repo)
            support.write(repo, "a.py", "seed\n")
            support.commit(repo, "fix: the target (#4242)")
            target = support.git(["rev-parse", "HEAD"], repo).strip()
            # Bury it well beyond any plausible cap.
            for i in range(60):
                support.write(repo, "a.py", f"line {i}\n")
                support.commit(repo, f"chore: filler {i}")

            with mock.patch.object(vcs, "gh_json", return_value=None):
                found = vcs.merge_commit_for_pr(4242, "o/r", repo, "main")

        self.assertEqual(target, found)


class TestDeletedTestsAreNotAddedTests(unittest.TestCase):
    """A fix that REMOVES a test file must not read as one that added coverage.

    `tests_added_by_fix` is the signal the "why tests missed it" judgement leans
    on, so counting a deletion inverts exactly the thing being judged.

    Two forms reach this differently, and both are covered because each is handled
    by a different branch: real `git diff` writes `+++ /dev/null` for a deletion,
    so the path classifies as non-test and is dropped before the delete check;
    a diff that keeps the `b/<path>` header instead reaches the delete check
    itself.
    """

    ADDITION = (
        "diff --git a/test/test_new.py b/test/test_new.py\n"
        "new file mode 100644\n"
        "index 0000000..2222222\n"
        "--- /dev/null\n"
        "+++ b/test/test_new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_new():\n"
        "+    assert True\n"
    )
    # `+++ b/<path>` retained: this is the form that reaches `is_deleted_file`.
    DELETION_WITH_PATH = (
        "diff --git a/test/test_thing.py b/test/test_thing.py\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "--- a/test/test_thing.py\n"
        "+++ b/test/test_thing.py\n"
        "@@ -1,2 +0,0 @@\n"
        "-def test_thing():\n"
        "-    assert True\n"
    )

    def test_a_deletion_that_still_names_the_test_path_is_not_reported(self):
        self.assertEqual([], bundle._test_changes(self.DELETION_WITH_PATH))

    def test_a_real_git_deletion_diff_is_not_reported(self):
        # Generated by git rather than hand-written, so the fixture cannot drift
        # from what the engine actually receives.
        with tempfile.TemporaryDirectory() as repo:
            support.git(["init", "-q", "-b", "main"], repo)
            support.write(repo, "test/test_thing.py", "def test_thing():\n    pass\n")
            support.commit(repo, "add a test")
            os.remove(os.path.join(repo, "test", "test_thing.py"))
            support.commit(repo, "remove that test")
            diff = support.git(["diff", "--unified=0", "HEAD^", "HEAD"], repo)

        self.assertIn("+++ /dev/null", diff, "this is the real deletion shape")
        self.assertEqual([], bundle._test_changes(diff))

    def test_an_added_test_file_still_is(self):
        added = bundle._test_changes(self.ADDITION)
        self.assertEqual(1, len(added))
        self.assertEqual("test/test_new.py", added[0].path)


class TestPrSubjectParsing(unittest.TestCase):
    """`(#n)` extraction must be digit-only and bounded.

    An unbounded integer conversion on text taken from a commit subject turns a
    crafted subject into an arbitrarily large int, and Python will happily build
    it -- a slow parse rather than an obvious rejection.
    """

    def test_a_normal_squash_subject_maps(self):
        self.assertEqual(1799, vcs.pr_from_subject("fix: do the thing (#1799)"))

    def test_absent_reference_is_none(self):
        self.assertIsNone(vcs.pr_from_subject("fix: do the thing"))

    def test_an_absurdly_long_number_is_refused(self):
        self.assertIsNone(vcs.pr_from_subject("fix: x (#" + "9" * 4000 + ")"))

    def test_non_digits_are_refused(self):
        self.assertIsNone(vcs.pr_from_subject("fix: x (#12ab)"))


class TestRedactionCoversTheWrittenBundle(unittest.TestCase):
    """A bundle is redacted BEFORE it reaches disk.

    A fix PR's diff is precisely where a credential that was committed and then
    removed still lives, and the bundle is handed to a model as evidence, so the
    write is the boundary that has to be covered -- not each reader of it.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def test_a_credential_in_untrusted_text_never_reaches_the_file(self):
        class _Fake:
            fix_pr = 7

            def to_dict(inner) -> dict:  # noqa: N805 - stub, not a method on self
                return {"fix_pr": 7,
                        "untrusted": {"title": f"key {TestRedactionCoversTheWrittenBundle.SECRET}"}}

        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(bundle, "build", return_value=_Fake()):
            paths = bundle.write_bundles("o/r", "/nonexistent", [{"fix_pr": 7}], out)
            self.assertEqual(1, len(paths))
            with open(paths[0], encoding="utf-8") as fh:
                written = fh.read()

        self.assertNotIn(self.SECRET, written)
        self.assertIn("[REDACTED", written)


class TestSelfAttributionIsDropped(unittest.TestCase):
    """Blame landing on the fix's own PR is not a culprit."""

    def test_the_fix_pr_is_never_its_own_culprit(self):
        with tempfile.TemporaryDirectory() as repo:
            support.git(["init", "-q", "-b", "main"], repo)
            support.write(repo, "a.py", "one\n")
            support.commit(repo, "add it (#5)")
            support.write(repo, "a.py", "two\n")
            support.commit(repo, "change it (#5)")

            head = support.git(["rev-parse", "HEAD"], repo).strip()
            with mock.patch.object(vcs, "gh_json", return_value=None), \
                    mock.patch.object(vcs, "merge_commit_for_pr", return_value=head):
                att = attribution.attribute("o/r", repo, 5)

            self.assertNotIn(5, [c.pr for c in att.candidates])


class TestShippedProseMatchesReality(unittest.TestCase):
    """The manifest and the skill must not promise surfaces the app lacks."""

    APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_manifest_declares_no_backend_or_ui(self):
        import json

        with open(os.path.join(self.APP, "app.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertNotIn("backend", manifest,
                         "this app serves no HTTP routes")
        self.assertNotIn("ui", manifest, "this app has no dashboard page")
        self.assertNotIn("api", manifest.get("permissions") or {})

    def test_skill_does_not_reference_a_data_home_it_cannot_resolve(self):
        path = os.path.join(self.APP, "skills", "pr-postmortem-scan", "SKILL.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("PRPM_DATA_DIR", text,
                      "the skill must tell the reader how the data dir resolves")
        self.assertNotIn("~/.kiro/crew/apps/pr-postmortem", text,
                         "a builtin is not installed under the apps dir")


if __name__ == "__main__":
    unittest.main()
