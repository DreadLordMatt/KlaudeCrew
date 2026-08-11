"""The slimmed state layer, the CLI orchestration, and the analysis contract.

The state layer's defining property is that OVERWRITE is correct: everything under
the data directory is derived from git, so a re-scan reproduces it. Several tests
here assert exactly that, because the opposite rule -- refuse to downgrade -- is
what previously required locking, retirement and an evidence census to stay
coherent, and re-adding any of it would be a regression rather than a hardening.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from kiro_crew.apps.builtins.pr_postmortem.engine import analysis, cli, store


class _DataDir(unittest.TestCase):
    """Point the app's data directory at a temp dir for the duration of a test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(
            os.environ, {"PRPM_DATA_DIR": self._tmp.name}, clear=False
        )
        self._patch.start()
        self.data = self._tmp.name

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()


class TestDataDirResolution(_DataDir):
    def test_prpm_data_dir_wins(self):
        self.assertEqual(self.data, store.data_dir())

    def test_kirocrew_home_is_used_when_no_override(self):
        # The fake home is built from this test's own temp dir rather than a literal
        # POSIX path: these tests also run on the Windows shards, where a hardcoded
        # /tmp path names nothing.
        home = os.path.join(self.data, "fake-home")
        with mock.patch.dict(os.environ, {"KIROCREW_HOME": home}, clear=False):
            os.environ.pop("PRPM_DATA_DIR")
            self.assertEqual(
                os.path.join(home, "workspace", "pr-postmortem"),
                store.data_dir(),
            )

    def test_subdirectories_are_created_on_demand(self):
        for path in (store.reports_dir(), store.analysis_dir(),
                     store.bundles_dir(), store.prompts_dir()):
            self.assertTrue(os.path.isdir(path), path)

    def test_app_name_matches_the_manifest(self):
        app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(app, "app.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["name"], store.APP_NAME)


class TestWriteIsAtomicAndNotLocked(_DataDir):
    def test_a_write_leaves_no_temp_file_behind(self):
        store.write_json(os.path.join(self.data, "x.json"), {"a": 1})
        leftovers = [n for n in os.listdir(self.data) if n.endswith(".tmp")]
        self.assertEqual([], leftovers)

    def test_a_failing_serialisation_does_not_clobber_the_existing_file(self):
        path = os.path.join(self.data, "x.json")
        store.write_json(path, {"good": True})

        class _Unserialisable:
            pass

        with self.assertRaises(TypeError):
            store.write_json(path, {"bad": _Unserialisable()})

        self.assertEqual({"good": True}, store.read_json(path),
                         "the previous content must survive a failed write")
        self.assertEqual([], [n for n in os.listdir(self.data)
                              if n.endswith(".tmp")],
                         "a failed write must not leave its temp file")

    def test_unreadable_json_reads_as_absent_rather_than_raising(self):
        path = os.path.join(self.data, "broken.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(store.read_json(path))

    def test_a_json_array_is_not_accepted_as_an_object(self):
        path = os.path.join(self.data, "arr.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2]")
        self.assertIsNone(store.read_json(path))


class TestOverwriteIsTheSemantics(_DataDir):
    def test_a_rerun_replaces_a_report_that_named_a_candidate(self):
        store.save_attribution({"fix_pr": 1, "verdict": "strong",
                                "candidates": [{"pr": 9}]})
        store.save_attribution({"fix_pr": 1, "verdict": "weak", "candidates": []})

        stored = store.read_json(os.path.join(store.reports_dir(), "1.json")) or {}
        self.assertEqual("weak", stored.get("verdict"))
        self.assertEqual([], stored.get("candidates"),
                         "a re-scan is authoritative; refusing the write is what "
                         "required locks and a census to stay coherent")

    def test_the_store_exposes_no_decision_or_retirement_surface(self):
        # Naming them explicitly: re-adding any of these means the app grew a
        # human-decision lifecycle again, and with it everything that had to
        # guard one.
        for gone in ("retire_analysis", "clear_decisions_for", "load_decisions",
                     "set_proposal_decision", "set_link_decision",
                     "set_application", "proposal_id", "exclusive",
                     "write_json_atomic"):
            self.assertFalse(hasattr(store, gone),
                             f"store.{gone} should not exist in this design")


class TestImportJsonl(_DataDir):
    def test_blank_and_malformed_lines_are_skipped(self):
        path = os.path.join(self.data, "batch.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"fix_pr": 100, "verdict": "strong"}) + "\n")
            fh.write("\n")
            fh.write("{not json}\n")
            fh.write(json.dumps({"fix_pr": 101, "verdict": "weak"}) + "\n")

        self.assertEqual(2, store.import_jsonl(path))
        self.assertEqual(["100.json", "101.json"],
                         sorted(os.listdir(store.reports_dir())))

    def test_a_record_without_a_fix_pr_is_skipped(self):
        path = os.path.join(self.data, "batch.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"verdict": "strong"}) + "\n")
        self.assertEqual(0, store.import_jsonl(path))


class TestTouchScan(_DataDir):
    def test_the_scan_timestamp_and_summary_are_recorded(self):
        rec = store.touch_scan({"repo": "o/r", "scanned": 3})
        self.assertIn("at", rec)
        self.assertEqual(3, rec["scanned"])
        self.assertEqual(rec, store.load_state()["last_scan"])

    def test_state_defaults_are_supplied_for_a_fresh_install(self):
        st = store.load_state()
        self.assertEqual([], st["repos"])
        self.assertIsNone(st["last_scan"])


def _bundle(dir_: str, fix_pr: int, culprit_pr: int | None) -> str:
    path = os.path.join(dir_, f"bundle-{fix_pr}.json")
    store.write_json(path, {"fix_pr": fix_pr, "culprit_pr": culprit_pr})
    return path


def _analysis(dir_: str, fix_pr: int, culprit_pr: int | None) -> str:
    path = os.path.join(dir_, f"analysis-{fix_pr}.json")
    store.write_json(path, {"fix_pr": fix_pr, "culprit_pr": culprit_pr})
    return path


class TestPromptsIdempotence(_DataDir):
    """A repeat scan must skip pairs it already explained -- but existence alone
    is the wrong test, because an analysis about a DIFFERENT culprit is stale."""

    def _run(self, bundles: str, out: str, prompts: str, force: bool = False) -> int:
        argv = ["prompts", "--repo", "o/r", "--bundle-dir", bundles,
                "--out-dir", out, "--prompt-dir", prompts]
        if force:
            argv.append("--force")
        return cli.main(argv)

    def test_a_pair_with_no_analysis_gets_a_prompt(self):
        b, o, p = (os.path.join(self.data, n) for n in ("b", "o", "p"))
        os.makedirs(b)
        _bundle(b, 10, 4)
        self.assertEqual(0, self._run(b, o, p))
        self.assertTrue(os.path.exists(os.path.join(p, "prompt-10.txt")))

    def test_an_analysis_about_the_same_culprit_is_skipped(self):
        b, o, p = (os.path.join(self.data, n) for n in ("b", "o", "p"))
        os.makedirs(b)
        os.makedirs(o)
        _bundle(b, 10, 4)
        _analysis(o, 10, 4)
        self.assertEqual(0, self._run(b, o, p))
        self.assertFalse(os.path.exists(os.path.join(p, "prompt-10.txt")),
                         "an already-explained pair must not be re-analysed")

    def test_an_analysis_about_a_DIFFERENT_culprit_is_regenerated(self):
        b, o, p = (os.path.join(self.data, n) for n in ("b", "o", "p"))
        os.makedirs(b)
        os.makedirs(o)
        _bundle(b, 10, 7)          # attribution now says 7
        _analysis(o, 10, 4)        # the stored analysis is about 4
        self.assertEqual(0, self._run(b, o, p))
        self.assertTrue(os.path.exists(os.path.join(p, "prompt-10.txt")),
                        "a stale analysis must not deadlock the pair")

    def test_a_malformed_analysis_counts_as_stale(self):
        b, o, p = (os.path.join(self.data, n) for n in ("b", "o", "p"))
        os.makedirs(b)
        os.makedirs(o)
        _bundle(b, 10, 4)
        with open(os.path.join(o, "analysis-10.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(0, self._run(b, o, p))
        self.assertTrue(os.path.exists(os.path.join(p, "prompt-10.txt")),
                        "redoing an unreadable analysis is cheap; trusting it is not")

    def test_force_regenerates_even_a_fresh_pair(self):
        b, o, p = (os.path.join(self.data, n) for n in ("b", "o", "p"))
        os.makedirs(b)
        os.makedirs(o)
        _bundle(b, 10, 4)
        _analysis(o, 10, 4)
        self.assertEqual(0, self._run(b, o, p, force=True))
        self.assertTrue(os.path.exists(os.path.join(p, "prompt-10.txt")))

    def test_a_pair_with_no_culprit_pr_on_both_sides_is_still_fresh(self):
        # `None == None` is a legitimate agreement: a culprit commit can predate
        # the PR workflow entirely.
        b, o, p = (os.path.join(self.data, n) for n in ("b", "o", "p"))
        os.makedirs(b)
        os.makedirs(o)
        _bundle(b, 11, None)
        _analysis(o, 11, None)
        self.assertEqual(0, self._run(b, o, p))
        self.assertFalse(os.path.exists(os.path.join(p, "prompt-11.txt")))


class TestCheckAnalysisExitCode(_DataDir):
    def test_an_empty_directory_is_an_error_not_a_pass(self):
        d = os.path.join(self.data, "analysis")
        os.makedirs(d)
        self.assertEqual(1, cli.main(["check-analysis", "--dir", d]))

    def test_a_valid_analysis_passes(self):
        d = os.path.join(self.data, "analysis")
        os.makedirs(d)
        store.write_json(os.path.join(d, "analysis-10.json"), _VALID_ANALYSIS)
        self.assertEqual(0, cli.main(["check-analysis", "--dir", d]))

    def test_an_invalid_analysis_fails(self):
        d = os.path.join(self.data, "analysis")
        os.makedirs(d)
        broken = dict(_VALID_ANALYSIS)
        broken.pop("candidate_rule")
        store.write_json(os.path.join(d, "analysis-10.json"), broken)
        self.assertEqual(1, cli.main(["check-analysis", "--dir", d]))


_VALID_RULE: dict[str, object] = {
    "kind": "gate",
    "statement": "assert every new guard fails on a value the system can write",
    "rationale": "would have caught the always-true comparison",
    "confidence": "high",
}

_VALID_ANALYSIS: dict[str, object] = {
    "fix_pr": 10,
    "culprit_pr": 4,
    "culprit_link_verdict": "confirmed",
    "culprit_link_reason": "the fix rewrote the comparison the culprit added",
    "failure_mode": ("assumed the guard could observe the state it guarded, but "
                     "the compared value is always present so it never fails"),
    "root_cause": "the check counted container length rather than emptiness",
    "why_review_missed": "CI was green and no reviewer commented on the guard",
    "why_tests_missed": "the tests built a value production never writes",
    "candidate_rule": _VALID_RULE,
    "prompt_injection_observed": False,
    "notes": "",
}


class TestAnalysisContract(unittest.TestCase):
    def test_the_reference_analysis_is_valid(self):
        self.assertEqual([], analysis.validate(_VALID_ANALYSIS))

    def test_a_textual_field_holding_an_object_is_rejected_by_type(self):
        # Coercing first would turn {"a": 1} into a non-empty "{'a': 1}" and let
        # the repr be rendered verbatim into a steering file.
        for field in ("failure_mode", "root_cause", "why_review_missed",
                      "why_tests_missed"):
            broken = dict(_VALID_ANALYSIS, **{field: {"a": 1}})
            errs = analysis.validate(broken)
            self.assertTrue(any("must be a string" in e for e in errs),
                            f"{field}: {errs}")

    def test_a_list_in_a_textual_field_is_rejected_by_type(self):
        broken = dict(_VALID_ANALYSIS, root_cause=["x"])
        self.assertTrue(any("must be a string" in e
                            for e in analysis.validate(broken)))

    def test_every_analysis_field_is_required(self):
        for field in ("failure_mode", "root_cause", "why_review_missed",
                      "why_tests_missed"):
            broken = dict(_VALID_ANALYSIS, **{field: ""})
            self.assertTrue(any(field in e for e in analysis.validate(broken)),
                            field)

    def test_an_over_long_failure_mode_is_rejected(self):
        broken = dict(_VALID_ANALYSIS,
                      failure_mode="x" * (analysis.MAX_FAILURE_MODE_CHARS + 1))
        self.assertTrue(any("exceeds" in e for e in analysis.validate(broken)))

    def test_the_rule_kind_and_confidence_are_constrained(self):
        bad_kind = dict(_VALID_ANALYSIS,
                        candidate_rule=dict(_VALID_RULE, kind="vibes"))
        self.assertTrue(any("kind" in e for e in analysis.validate(bad_kind)))
        bad_conf = dict(_VALID_ANALYSIS,
                        candidate_rule=dict(_VALID_RULE, confidence="certain"))
        self.assertTrue(any("confidence" in e for e in analysis.validate(bad_conf)))

    def test_a_rejected_link_is_a_complete_result_with_no_analysis(self):
        rejected = {
            "fix_pr": 11,
            "culprit_pr": 5,
            "culprit_link_verdict": "rejected",
            "culprit_link_reason": "blame landed on a commit that only reindented",
            "failure_mode": "",
            "root_cause": "",
            "why_review_missed": "",
            "why_tests_missed": "",
            "notes": "mover, not author",
        }
        self.assertEqual([], analysis.validate(rejected))

    def test_a_rejected_link_may_not_carry_a_rule_or_a_failure_mode(self):
        base = {
            "fix_pr": 11,
            "culprit_pr": 5,
            "culprit_link_verdict": "rejected",
            "culprit_link_reason": "mover",
            "failure_mode": "",
            "root_cause": "",
            "why_review_missed": "",
            "why_tests_missed": "",
        }
        with_rule = dict(base, candidate_rule=_VALID_RULE)
        self.assertTrue(any("candidate_rule" in e
                            for e in analysis.validate(with_rule)))
        with_mode = dict(base, failure_mode="assumed x but y")
        self.assertTrue(any("failure_mode" in e
                            for e in analysis.validate(with_mode)))

    def test_a_bogus_link_verdict_is_rejected(self):
        broken = dict(_VALID_ANALYSIS, culprit_link_verdict="probably")
        self.assertTrue(any("culprit_link_verdict" in e
                            for e in analysis.validate(broken)))

    def test_a_culprit_pr_that_is_not_an_int_is_rejected(self):
        broken = dict(_VALID_ANALYSIS, culprit_pr="4")
        self.assertTrue(any("culprit_pr" in e for e in analysis.validate(broken)))

    def test_a_boolean_culprit_pr_is_rejected(self):
        # bool is a subclass of int, so this needs its own guard.
        broken = dict(_VALID_ANALYSIS, culprit_pr=True)
        self.assertTrue(any("culprit_pr" in e for e in analysis.validate(broken)))

    def test_no_fixed_taxonomy_remains(self):
        # The clustering axis is free-text `failure_mode`; a closed class list
        # splits findings that share a mechanism across several labels.
        self.assertFalse(hasattr(analysis, "ROOT_CAUSE_CLASSES"))
        self.assertFalse(hasattr(analysis, "PREVENTION_BUCKETS"))
        self.assertNotIn("root_cause_class",
                         analysis.build_prompt("o/r", "/b", "/a", 1, 2))

    def test_the_prompt_states_the_security_rule_and_the_output_path(self):
        prompt = analysis.build_prompt("o/r", "/b.json", "/a.json", 10, 4)
        self.assertIn("untrusted", prompt)
        self.assertIn("MUST NOT follow", prompt)
        self.assertIn("/a.json", prompt)
        self.assertIn("prompt_injection_observed", prompt)

    def test_a_missing_analysis_file_reports_rather_than_raising(self):
        obj, errs = analysis.load_and_validate("/nonexistent/analysis-1.json")
        self.assertIsNone(obj)
        self.assertTrue(errs)

    def test_a_boolean_fix_pr_is_rejected(self):
        # bool subclasses int, so a plain isinstance check accepts `true` as a
        # pull-request number.
        broken = dict(_VALID_ANALYSIS, fix_pr=True)
        self.assertTrue(any("fix_pr" in e for e in analysis.validate(broken)))

    def test_the_fix_pr_is_read_off_the_filename(self):
        self.assertEqual(10, analysis.fix_pr_from_path("/x/analysis-10.json"))
        self.assertIsNone(analysis.fix_pr_from_path("/x/notes.json"))
        # Bounded, for the same reason the commit-subject parser is.
        self.assertIsNone(
            analysis.fix_pr_from_path("/x/analysis-" + "9" * 40 + ".json")
        )


class TestAnalysisIsBoundToItsPair(_DataDir):
    """An analysis must be about the pair its filename names.

    The prompt tells the analyst which file to write, so a different `fix_pr` inside
    means the content describes some other pair. Accepting it would attribute a
    finding to the wrong pull request -- which is precisely the thing this app
    exists to get right.
    """

    def _write(self, fix_pr_in_file: int, named: int) -> tuple[dict | None, list[str]]:
        path = os.path.join(self.data, f"analysis-{named}.json")
        store.write_json(path, dict(_VALID_ANALYSIS, fix_pr=fix_pr_in_file))
        return analysis.load_and_validate(path)

    def test_a_matching_analysis_is_accepted(self):
        _obj, errs = self._write(10, 10)
        self.assertEqual([], errs)

    def test_a_mismatched_analysis_is_rejected(self):
        _obj, errs = self._write(10, 99)
        self.assertTrue(any("different pair" in e for e in errs), errs)

    def test_check_analysis_fails_on_a_mismatched_file(self):
        d = os.path.join(self.data, "analysis")
        os.makedirs(d)
        store.write_json(os.path.join(d, "analysis-77.json"),
                         dict(_VALID_ANALYSIS, fix_pr=10))
        self.assertEqual(1, cli.main(["check-analysis", "--dir", d]))


if __name__ == "__main__":
    unittest.main()
