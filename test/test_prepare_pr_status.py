"""Regression tests for the prepare-pr aggregate readiness policy."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr" / "scripts" / "pr_status.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepare_pr_status", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pr_payload(checks: list[dict[str, str]], **overrides: object) -> str:
    payload: dict[str, object] = {
        "number": 42,
        "title": "fix: keep the change focused",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "REVIEW_REQUIRED",
        "url": "https://github.com/example/repo/pull/42",
        "headRefName": "fix/focused",
        "statusCheckRollup": checks,
        # The issue-link gate is a CLEAN precondition, so the baseline payload
        # satisfies it. Tests that exercise the gate override these two.
        "body": "Fixes #7",
        "closingIssuesReferences": [{"number": 7}],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _install_fake_gh(module: ModuleType, payload: str) -> None:
    def fake_run(args: list[str]) -> tuple[int, str, str]:
        if args[:3] == ["gh", "auth", "status"]:
            return 0, "", ""
        if args[:3] == ["gh", "pr", "view"]:
            return 0, payload, ""
        raise AssertionError("unexpected command: {}".format(args))

    module.run = fake_run
    module.unresolved_thread_count = lambda _number: 3


def test_passed_aggregate_overrides_old_failures_and_advisory_threads() -> None:
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "old duplicate check", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"context": "PR Readiness", "state": "SUCCESS"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 0


def test_passed_aggregate_overrides_an_old_pending_check() -> None:
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "old duplicate check", "status": "IN_PROGRESS", "conclusion": ""},
            {"context": "PR Readiness", "state": "SUCCESS"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 0


def test_legacy_pull_request_without_aggregate_still_fails_closed() -> None:
    module = _load_script()
    payload = _pr_payload(
        [{"name": "Backend Tests", "status": "COMPLETED", "conclusion": "FAILURE"}]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_check_run_named_pr_readiness_cannot_mask_a_failure() -> None:
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "PR Readiness", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "Backend Tests", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_merged_pull_request_is_terminal_not_running() -> None:
    """A non-open PR must exit 20, not wait on mergeability GitHub never computes."""
    module = _load_script()
    payload = _pr_payload([], state="MERGED", mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_closed_pull_request_is_terminal_not_running() -> None:
    module = _load_script()
    payload = _pr_payload([], state="CLOSED", mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_open_pull_request_with_unknown_mergeability_still_waits() -> None:
    """The terminal-state check must not swallow the legitimate async wait."""
    module = _load_script()
    payload = _pr_payload(
        [{"name": "Backend Tests", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        mergeable="UNKNOWN",
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 10


def test_superseded_cancelled_run_does_not_count_as_a_failure() -> None:
    """A re-run leaves the CANCELLED attempt in the rollup; newest run wins."""
    module = _load_script()
    payload = _pr_payload(
        [
            {
                "name": "GPT Review",
                "workflowName": "review.yml",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "startedAt": "2026-08-06T01:00:00Z",
            },
            {
                "name": "GPT Review",
                "workflowName": "review.yml",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-06T02:00:00Z",
            },
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 0


def test_superseded_success_does_not_mask_a_newer_failure() -> None:
    """Newest-wins must work in both directions: a fresh failure stays red."""
    module = _load_script()
    payload = _pr_payload(
        [
            {
                "name": "Backend Tests",
                "workflowName": "ci.yml",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-06T01:00:00Z",
            },
            {
                "name": "Backend Tests",
                "workflowName": "ci.yml",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-08-06T02:00:00Z",
            },
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_same_check_name_in_different_workflows_stays_distinct() -> None:
    """Identity is workflow-qualified: two workflows may share a job name."""
    module = _load_script()
    payload = _pr_payload(
        [
            {
                "name": "build",
                "workflowName": "linux.yml",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-06T02:00:00Z",
            },
            {
                "name": "build",
                "workflowName": "windows.yml",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-08-06T01:00:00Z",
            },
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_unordered_duplicates_are_all_kept_fail_closed() -> None:
    """Without startedAt on both entries there is no ordering evidence, so
    neither may silently supersede the other -- the failure must survive."""
    module = _load_script()
    payload = _pr_payload(
        [
            {
                "name": "Backend Tests",
                "workflowName": "ci.yml",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            },
            {
                "name": "Backend Tests",
                "workflowName": "ci.yml",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-06T02:00:00Z",
            },
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_status_contexts_collapse_by_context_name() -> None:
    """StatusContexts share the identity axis via their context string."""
    module = _load_script()
    payload = _pr_payload(
        [
            {"context": "PR Readiness", "state": "FAILURE", "startedAt": "2026-08-06T01:00:00Z"},
            {"context": "PR Readiness", "state": "SUCCESS", "startedAt": "2026-08-06T02:00:00Z"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 0


# --- issue-link contract (closing keyword) ------------------------------------
#
# The gate exists because finished work merged with only "Related: #n" left the
# issue open forever, with nothing downstream to reconcile it. The host's own
# closingIssuesReferences resolution is the truth; the body regexes only
# classify WHY it resolved to nothing, so the operator is told which of the
# three mistakes they made.


def test_resolved_closing_reference_satisfies_the_gate() -> None:
    module = _load_script()
    assert module.closing_link_reason("Fixes #7", [{"number": 7}]) is None


def test_bare_reference_without_a_verb_is_blocked() -> None:
    """The exact shape that merged in #2433/#2439 and closed nothing."""
    module = _load_script()
    reason = module.closing_link_reason("Related: #2368, #2375 for context", [])
    assert reason is not None
    assert "no closing keyword" in reason


def test_verb_present_but_host_resolved_nothing_is_blocked_distinctly() -> None:
    module = _load_script()
    reason = module.closing_link_reason("Fixes #999999", [])
    assert reason is not None
    assert "resolved no issue" in reason
    # Must NOT be reported as the missing-verb case; the operator needs to know
    # the verb is fine and the NUMBER is the problem.
    assert "no closing keyword" not in reason


def test_no_reference_at_all_is_blocked_with_the_opt_out_named() -> None:
    module = _load_script()
    reason = module.closing_link_reason("A pure refactor with no tracked issue.", [])
    assert reason is not None
    assert "no issue closed" in reason


def test_explicit_opt_out_satisfies_the_gate() -> None:
    module = _load_script()
    body = "A pure refactor.\n\nno issue closed: no ticket exists for this cleanup."
    assert module.closing_link_reason(body, []) is None


def test_opt_out_must_be_a_trailer_not_a_mention() -> None:
    """Prose that merely discusses the gate must NOT satisfy it.

    An unanchored substring match let any body containing the phrase pass —
    including a body that only explains what the phrase is for.
    """
    module = _load_script()
    prose = "The gate accepts a `no issue closed: <why>` line as an opt-out."
    assert module.closing_link_reason(prose, []) is not None
    indented = "  no issue closed: buried in an instruction block"
    assert module.closing_link_reason(indented, []) is not None
    assert module.closing_link_reason("no issue closed but I forgot the colon", []) is not None


def test_shipped_body_template_cannot_satisfy_the_gate_unfilled() -> None:
    """An author who copies the template and skips the Issue link section must
    be BLOCKED, not passed.

    This runs the real regexes against the real shipped asset, so the template
    and the gate cannot drift back into agreeing. The template deliberately
    contains no column-0 opt-out declaration and no resolvable `#<digits>`.
    """
    module = _load_script()
    template = (
        SCRIPT.parent.parent / "assets" / "pr-body-template.md"
    ).read_text(encoding="utf-8")
    reason = module.closing_link_reason(template, [])
    assert reason is not None, "unfilled template silently passes the issue-link gate"
    assert "no issue link" in reason


def test_markdown_headings_are_not_mistaken_for_issue_references() -> None:
    """`# Problem` must not read as a bare `#n` ref, or every PR reports the
    wrong reason."""
    module = _load_script()
    reason = module.closing_link_reason("# Problem\n\n## Why it matters\n", [])
    assert reason is not None
    assert "no issue link" in reason


def test_missing_body_is_treated_as_no_link_not_a_crash() -> None:
    module = _load_script()
    assert module.closing_link_reason(None, []) is not None


def test_gh_query_requests_the_issue_link_fields() -> None:
    """The fake gh injects a payload directly, so no other test would notice the
    real ``--json`` field list dropping these two names -- the gate would then
    always see an absent body and mis-report on every live PR."""
    module = _load_script()
    seen: list[str] = []

    def capture(args: list[str]) -> tuple[int, str, str]:
        if args[:3] == ["gh", "auth", "status"]:
            return 0, "", ""
        if args[:3] == ["gh", "pr", "view"]:
            seen.append(args[args.index("--json") + 1])
            return 1, "", "stop here"
        raise AssertionError("unexpected command: {}".format(args))

    module.run = capture
    module.main(["pr_status.py", "42"])
    assert seen, "gh pr view was never called"
    assert "body" in seen[0].split(","), seen[0]
    assert "closingIssuesReferences" in seen[0].split(","), seen[0]


def test_clean_status_is_withheld_when_the_issue_link_is_missing(capsys) -> None:
    """End-to-end: the gate must actually move the verdict, not just exist.

    Asserts the REASON, not only the exit code. An earlier version of this test
    used a check dict with no ``status`` field, which ``classify_check`` counts
    as failed (fail-closed on unknown shape) -- so it returned 20 for a bogus
    "1 check(s) failed" and still passed with the gate deleted.
    """
    module = _load_script()
    checks = [
        {"name": "PR Readiness", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    _install_fake_gh(module, _pr_payload(checks, body="Related: #7", closingIssuesReferences=[]))
    assert module.main(["pr_status.py", "42"]) == 20
    out = capsys.readouterr().out
    assert "no closing keyword" in out, out
    assert "check(s) failed" not in out, out


def test_clean_status_is_reached_when_the_issue_link_resolves(capsys) -> None:
    """The mirror case: the same fixture with a resolved trailer must be CLEAN.

    Without this, a gate that blocked unconditionally would look correct.
    """
    module = _load_script()
    checks = [
        {"name": "PR Readiness", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    _install_fake_gh(
        module,
        _pr_payload(checks, body="Fixes #7", closingIssuesReferences=[{"number": 7}]),
    )
    assert module.main(["pr_status.py", "42"]) == 0
    assert "STATUS: CLEAN" in capsys.readouterr().out
