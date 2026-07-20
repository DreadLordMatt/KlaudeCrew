"""Regression guard for the prompt.md <-> _CRITICAL_RULES dedupe.

The diff-block output rule must be stated exactly once in what a dashboard
session receives: authoritatively in the base system prompt (config/prompt.md),
and NOT re-stated in the per-turn injected _CRITICAL_RULES block. The dashboard
session gets both prompt.md (system prompt) and _CRITICAL_RULES (injected), so
duplicating the rule wastes ~7 lines on every turn. The absolute-path and
[OPTIONS:] rules are dashboard-only and must remain in _CRITICAL_RULES.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.agent import _BUNDLED_CFG_DIR
from kiro_crew.context import _CRITICAL_RULES

# Distinctive fragment of the diff-block mandate.
_DIFF_RULE_FRAGMENT = "diff code block"


def _prompt_md_text() -> str:
    return (Path(_BUNDLED_CFG_DIR) / "prompt.md").read_text(encoding="utf-8")


def test_diff_rule_is_authoritative_in_prompt_md():
    """The diff-block mandate must live in the base system prompt."""
    assert _DIFF_RULE_FRAGMENT in _prompt_md_text()


def test_diff_rule_not_duplicated_in_critical_rules():
    """The diff-block mandate must NOT be restated in _CRITICAL_RULES (dedupe)."""
    assert _DIFF_RULE_FRAGMENT not in _CRITICAL_RULES


def test_critical_rules_retains_dashboard_only_rules():
    """Dedupe must not drop the rules that are unique to _CRITICAL_RULES."""
    # Absolute-path rule (drives the UI file viewer) and the [OPTIONS:] rule
    # are not in prompt.md, so they must survive in _CRITICAL_RULES.
    assert "absolute path" in _CRITICAL_RULES
    assert "[OPTIONS:" in _CRITICAL_RULES
    assert "[CRITICAL RULES" in _CRITICAL_RULES
