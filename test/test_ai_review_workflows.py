"""Regression tests for human-readable and human-overridable AI reviews."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


class TestHumanOverrideHandler:
    def test_handler_runs_from_trusted_issue_comment_context(self) -> None:
        workflow = _workflow("ai-review-human-override.yml")

        assert "issue_comment:" in workflow
        assert "pull_request_target:" not in workflow
        assert "actions/checkout@" not in workflow
        assert "/ai-review override <fable|gpt|arbiter|all> <current-sha>: <reason>" in workflow

    def test_handler_requires_write_permission_fresh_sha_and_reason(self) -> None:
        workflow = _workflow("ai-review-human-override.yml")

        assert 'if [ "$ACTOR" = "$author" ]; then' not in workflow
        assert "collaborators/$ACTOR/permission" in workflow
        assert "admin|maintain|write) allowed=true" in workflow
        assert 'if [[ "$head" != "$requested_sha"* ]]; then' in workflow
        assert 'if [ -z "$reason" ]; then' in workflow
        assert 'if [ "${#reason}" -gt 500 ]; then' in workflow
        assert "only a repository writer" in workflow

    def test_handler_records_a_bot_marker_before_changing_checks(self) -> None:
        workflow = _workflow("ai-review-human-override.yml")
        marker = (
            "<!-- ai-review-human-override target=$target head=$head "
            "actor=$ACTOR source=$COMMENT_ID -->"
        )

        assert marker in workflow
        assert workflow.index(marker) < workflow.index("actions/runs/$run_id/rerun")
        assert "select(.head_sha == $head" in workflow
        assert 'name="Arbiter — judge from comments"' in workflow
        assert "-f status=completed -f conclusion=success" in workflow


class TestLineReviewHumanOverrides:
    def test_fable_consumes_only_a_bot_authored_sha_scoped_record(self) -> None:
        workflow = _workflow("claude-review.yml")

        assert "target=fable head=$HEAD" in workflow
        assert '.user.login == "github-actions[bot]"' in workflow
        assert "steps.human_override.outputs.active != 'true'" in workflow
        assert "✅ human override accepted" in workflow
        assert "Human judgment by $OVERRIDE_ACTOR overrides Fable 5" in workflow
        assert "/ai-review override fable $HEAD:" in workflow

    def test_gpt_has_clear_verdict_banner_and_human_override(self) -> None:
        workflow = _workflow("codex-review.yml")

        assert "target=gpt head=$HEAD" in workflow
        assert '.user.login == "github-actions[bot]"' in workflow
        assert "steps.human_override.outputs.active != 'true'" in workflow
        assert 'verdict="✅ no blocking findings"' in workflow
        assert (
            "GPT 5.6 completed its review of \\`$HEAD\\` and found no blocking issues." in workflow
        )
        assert "✅ human override accepted" in workflow
        assert "Human judgment by $OVERRIDE_ACTOR overrides GPT 5.6" in workflow
        assert "/ai-review override gpt $HEAD:" in workflow


class TestArbiterPresentation:
    def test_arbiter_replaces_stale_results_while_waiting(self) -> None:
        workflow = _workflow("longterm-arbiter.yml")

        assert 'TITLE="⏳ review pending"' in workflow
        assert "this replaces any stale verdict from the previous commit" in workflow
        assert "Always refresh the human-facing comment, including while waiting" in workflow

    def test_arbiter_has_clear_verdict_and_override_paths(self) -> None:
        workflow = _workflow("longterm-arbiter.yml")

        assert "target=arbiter head=$SHA" in workflow
        assert 'STATE="human_override"' in workflow
        assert 'TITLE="✅ no blocking findings"' in workflow
        assert (
            "Arbiter found no unresolved long-term items that require action before "
            "merging \\`$SHA\\`." in workflow
        )
        assert 'TITLE="✅ human override accepted"' in workflow
        assert "/ai-review override arbiter $SHA:" in workflow
        assert "defer-longterm" in workflow


class TestSimplerAlternativeSignal:
    """The advisory 'a simpler solution exists' signal: surfaced prominently by
    the design reviewer, emphasized by the Arbiter, and NEVER a blocker."""

    def test_design_review_emits_and_renders_the_signal(self) -> None:
        workflow = _workflow("design-review.yml")

        # Machine header contract + dedicated, prominently-rendered section.
        assert "Design-Simpler-Alternative: <yes | no>" in workflow
        assert "### 💡 Simpler alternative" in workflow
        # The post step parses the header, matches decorated values (yes*), and
        # composes the badge INTO the posted comment header (assert the wiring,
        # not just that the strings exist somewhere).
        assert "^Design-Simpler-Alternative:" in workflow
        assert "yes*) simpler_badge=" in workflow
        assert "blast radius: $blast$simpler_badge" in workflow

    def test_design_review_never_blocks_on_mere_over_engineering(self) -> None:
        workflow = _workflow("design-review.yml")

        # Gate #4 says the finding is advisory ...
        assert "a simpler-alternative finding NEVER raises the" in workflow
        # ... and the BLOCK verdict no longer lists "a better alternative was
        # ignored" as a ground — over-engineering is owned by the advisory signal.
        assert "owned entirely by the ADVISORY" in workflow
        assert "a clearly better alternative\n              was ignored" not in workflow

    def test_arbiter_emphasizes_but_never_blocks_on_the_signal(self) -> None:
        workflow = _workflow("longterm-arbiter.yml")

        assert "ALSO NON-BLOCKING: OVER-ENGINEERING / a SIMPLER-ALTERNATIVE signal" in workflow
        # A co-stated concrete harm is still judged against the normal bar.
        assert "only the SIMPLICITY / over-engineering aspect is" in workflow
        # Emphasized at the top of the non-blocking follow-ups, and kept above
        # the collapsed fold on a PASS so the author still notices it (assert the
        # extraction is wired in, not merely that the prose exists).
        assert '"💡 Simpler alternative:"' in workflow
        assert "notices the leaner option (advisory, never blocking)" in workflow
        assert "grep -m1 -F '💡 Simpler alternative:'" in workflow
