"""Fork (KlaudeCrew): resolve_claude_wire_id -- matches a preferred model
value (a canonical registry key, a registry alias, or an already-bare wire
value) against a claude session's live-advertised model ids.

Live-verified wire shape (real gateway + real claude + claude-agent-acp
v0.66.0): simple values like "sonnet", "opus[1m]", "haiku", "default",
"claude-fable-5[1m]" -- not Bedrock ids, not dated snapshot ids. This
matches ONLY against that live-advertised set, never the static (Bedrock-
only) model_registry.json "claude_code" column.
"""

from __future__ import annotations

from kiro_crew.model_registry import resolve_claude_wire_id

# The real advertised set observed live, used throughout as a realistic fixture.
_ADVERTISED = ["default", "opus[1m]", "claude-fable-5[1m]", "sonnet", "haiku"]


class TestResolveClaudeWireId:
    def test_exact_wire_value_matches_verbatim(self) -> None:
        assert resolve_claude_wire_id("opus[1m]", _ADVERTISED) == "opus[1m]"

    def test_canonical_key_resolves_via_alias(self) -> None:
        # "sonnet" is a registered alias of the sonnet-4.6-1m canonical key.
        assert resolve_claude_wire_id("sonnet-4.6-1m", _ADVERTISED) == "sonnet"

    def test_registry_alias_resolves_to_same_wire_value(self) -> None:
        # The alias itself (not the canonical key) must also match.
        assert resolve_claude_wire_id("sonnet", _ADVERTISED) == "sonnet"

    def test_unknown_bare_value_passes_through_and_matches_verbatim(self) -> None:
        # "haiku" isn't in ANY registry alias list (only "claude-haiku-4.5" is) --
        # canonicalization is a no-op on both sides, so raw equality still matches.
        assert resolve_claude_wire_id("haiku", _ADVERTISED) == "haiku"

    def test_no_match_returns_none(self) -> None:
        assert resolve_claude_wire_id("gpt-4", _ADVERTISED) is None

    def test_canonical_key_not_advertised_returns_none(self) -> None:
        # opus-4.7-1m is a real registry entry but not in this session's list.
        assert resolve_claude_wire_id("opus-4.7-1m", _ADVERTISED) is None

    def test_empty_preferred_returns_none(self) -> None:
        assert resolve_claude_wire_id("", _ADVERTISED) is None

    def test_empty_advertised_returns_none(self) -> None:
        assert resolve_claude_wire_id("sonnet", []) is None

    def test_advertised_entries_with_falsy_values_skipped(self) -> None:
        assert resolve_claude_wire_id("sonnet", ["", "sonnet"]) == "sonnet"

    def test_bedrock_id_as_preferred_still_resolves(self) -> None:
        # A caller that (incorrectly) passes a Bedrock id as "preferred" still
        # gets a correct answer: canonicalize_for_provider folds it to the
        # canonical key first, same as any other claude_code-shaped input.
        assert (
            resolve_claude_wire_id("global.anthropic.claude-sonnet-4-6[1m]", _ADVERTISED)
            == "sonnet"
        )

    def test_never_returns_a_bedrock_or_fabricated_id(self) -> None:
        # Regression guard for the bug this function replaces: no code path
        # here can ever produce a "global.anthropic...." string as output --
        # the return value is always either a member of `advertised` or None.
        for preferred in ("opus-4.8-1m", "sonnet-4.6-1m", "haiku-4.5", "auto", "unknown-model"):
            result = resolve_claude_wire_id(preferred, _ADVERTISED)
            assert result is None or result in _ADVERTISED
