"""Fork (KlaudeCrew): claude-backend token/cost usage tracking (issue #6).

Wire shapes below are SOURCE-VERIFIED against the pinned claude-agent-acp
v0.66.0 package (``dist/acp-agent.js`` + the ACP SDK's ``types.gen.d.ts``),
the same version this fork's docs already cite for the model wire shape --
and then live-confirmed on a real gateway, including that the adapter's
cumulative cost is per PROCESS (restarts on session/load resume). See
docs/system-specs/modules/acp-client.md "Turn usage (claude backend)".
"""

from __future__ import annotations

import types

import pytest

from kiro_crew.acp._dispatch import parse_prompt_result_usage, parse_usage_update_cost
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    EVENT_COMPLETE,
    AcpPromptStats,
    TurnUsage,
    turn_usage_from_stats,
)


class TestParsePromptResultUsage:
    """ACP PromptResponse.usage (EXPERIMENTAL/optional) -- claude-agent-acp
    v0.66.0's prompt() handler resolves {stopReason, usage: sessionUsage(...)}
    for every stop reason, EXCEPT a queued-then-cancelled turn that never
    ran, which settles with no usage key at all."""

    def test_full_shape_maps_camel_to_snake(self):
        result = {
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 100,
                "outputTokens": 50,
                "cachedReadTokens": 10,
                "cachedWriteTokens": 5,
                "totalTokens": 165,
            },
        }
        parsed = parse_prompt_result_usage(result)
        assert parsed == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 10,
            "cache_creation_tokens": 5,
        }

    def test_missing_usage_key_returns_none(self):
        # The queued-then-cancelled-turn case: {stopReason: "cancelled"} only.
        assert parse_prompt_result_usage({"stopReason": "cancelled"}) is None

    def test_non_dict_result_returns_none(self):
        assert parse_prompt_result_usage(None) is None
        assert parse_prompt_result_usage("not a dict") is None

    def test_non_dict_usage_returns_none(self):
        assert parse_prompt_result_usage({"usage": "not a dict"}) is None

    def test_all_fields_absent_returns_none(self):
        assert parse_prompt_result_usage({"usage": {}}) is None

    def test_partial_fields_default_missing_to_zero(self):
        parsed = parse_prompt_result_usage({"usage": {"inputTokens": 42}})
        assert parsed == {
            "input_tokens": 42,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), "42", [], True, None])
    def test_malformed_field_values_degrade_not_raise(self, bad):
        parsed = parse_prompt_result_usage({"usage": {"inputTokens": bad, "outputTokens": 7}})
        assert parsed == {
            "input_tokens": 0,
            "output_tokens": 7,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }


class TestParseUsageUpdateCost:
    """usage_update's optional `cost` sub-object -- CUMULATIVE session cost
    (the ACP spec's own Cost.amount docstring), sourced from the Claude Code
    SDK's running total_cost_usd. Never a per-turn delta by itself."""

    def test_parses_amount(self):
        update = {"used": 1, "size": 2, "cost": {"amount": 0.1234, "currency": "USD"}}
        assert parse_usage_update_cost(update) == 0.1234

    def test_missing_cost_returns_none(self):
        assert parse_usage_update_cost({"used": 1, "size": 2}) is None

    def test_non_dict_update_returns_none(self):
        assert parse_usage_update_cost(None) is None

    def test_non_dict_cost_returns_none(self):
        assert parse_usage_update_cost({"cost": "not a dict"}) is None

    def test_non_usd_currency_returns_none(self):
        assert parse_usage_update_cost({"cost": {"amount": 1.0, "currency": "EUR"}}) is None

    def test_missing_currency_defaults_to_usd(self):
        assert parse_usage_update_cost({"cost": {"amount": 1.0}}) == 1.0

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), "1.0", [], True, None])
    def test_malformed_amount_returns_none(self, bad):
        assert parse_usage_update_cost({"cost": {"amount": bad, "currency": "USD"}}) is None


class TestTurnUsageFromStats:
    def test_copies_claude_fields(self):
        stats = AcpPromptStats(
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=3,
            cache_creation_tokens=4,
            cost_usd=0.5,
        )
        usage = turn_usage_from_stats(stats)
        assert usage == TurnUsage(
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=3,
            cache_creation_tokens=4,
            cost_usd=0.5,
            credits=0.0,
        )

    def test_copies_kiro_credits(self):
        stats = AcpPromptStats(credits=3.5)
        usage = turn_usage_from_stats(stats)
        assert usage.credits == 3.5
        assert usage.input_tokens == 0 and usage.cost_usd == 0.0

    def test_num_turns_and_duration_always_zero(self):
        # Neither field is ever populated from the ACP wire on either backend
        # (see test_turn_duration_*.py for the pinned "duration_ms stays 0"
        # contract this must not disturb).
        stats = AcpPromptStats(input_tokens=1, credits=1.0)
        usage = turn_usage_from_stats(stats)
        assert usage.num_turns == 0
        assert usage.duration_ms == 0


class TestAcpPromptStatsCarryOverResetsBillingFields:
    def test_new_fields_reset_to_zero(self):
        stats = AcpPromptStats(
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=3,
            cache_creation_tokens=4,
            cost_usd=0.5,
            credits=9.0,
        )
        fresh = stats.carry_over()
        assert fresh.input_tokens == 0
        assert fresh.output_tokens == 0
        assert fresh.cache_read_tokens == 0
        assert fresh.cache_creation_tokens == 0
        assert fresh.cost_usd == 0.0
        assert fresh.credits == 0.0

    def test_context_fields_survive(self):
        stats = AcpPromptStats(context_pct=42.0, context_used_tokens=100, context_window_tokens=200)
        fresh = stats.carry_over()
        assert fresh.context_pct == 42.0
        assert fresh.context_used_tokens == 100
        assert fresh.context_window_tokens == 200


def _usage_update_msg(used=1, size=2, cost_amount=None):
    from kiro_crew.acp.types import JsonRpcMessage

    update = {"sessionUpdate": "usage_update", "used": used, "size": size}
    if cost_amount is not None:
        update["cost"] = {"amount": cost_amount, "currency": "USD"}
    return JsonRpcMessage(method="session/update", params={"update": update})


class TestTrackUsageUpdateCostBaseline:
    """AcpClient._track_usage_update_cost: diffs successive CUMULATIVE cost
    reads into a per-turn delta, since TurnUsage.cost_usd is per-turn but the
    wire value is cumulative session cost."""

    def test_first_observation_is_the_first_turns_spend(self, tmp_path):
        """The adapter's accumulator is per process (live-verified: a
        session/load resume restarts near zero), so the very first cumulative
        value IS this turn's own cost — it must be credited, not swallowed
        as a baseline seed."""
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._track_usage_update(_usage_update_msg(cost_amount=1.50))
        assert client._cost_usd_baseline == 1.50
        assert client.last_prompt_stats.cost_usd == pytest.approx(1.50)

    def test_second_higher_observation_adds_only_the_delta(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._track_usage_update(_usage_update_msg(cost_amount=1.50))
        client.last_prompt_stats = client.last_prompt_stats.carry_over()  # turn boundary
        client._track_usage_update(_usage_update_msg(cost_amount=1.75))
        assert client.last_prompt_stats.cost_usd == pytest.approx(0.25)
        assert client._cost_usd_baseline == 1.75

    def test_multiple_deltas_within_one_turn_accumulate(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._track_usage_update(_usage_update_msg(cost_amount=1.00))
        client._track_usage_update(_usage_update_msg(cost_amount=1.10))
        client._track_usage_update(_usage_update_msg(cost_amount=1.35))
        assert client.last_prompt_stats.cost_usd == pytest.approx(1.35)

    def test_negative_delta_resyncs_baseline_without_subtracting(self, tmp_path):
        """A backend-side resync (e.g. a fresh SDK query object) must not
        make cost_usd go negative -- it just re-anchors the baseline."""
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._track_usage_update(_usage_update_msg(cost_amount=2.00))
        client.last_prompt_stats = client.last_prompt_stats.carry_over()  # turn boundary
        client._track_usage_update(_usage_update_msg(cost_amount=1.75))  # backend reset
        client._track_usage_update(_usage_update_msg(cost_amount=1.90))  # normal delta after
        assert client.last_prompt_stats.cost_usd == pytest.approx(0.15)  # 1.90 - 1.75
        assert client._cost_usd_baseline == 1.90

    def test_carry_over_preserves_baseline_across_turns(self, tmp_path):
        """The baseline lives on the client (not AcpPromptStats), so a new
        turn's carry_over() must not reset it -- otherwise every turn would
        re-credit the whole cumulative total instead of just its delta."""
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._track_usage_update(_usage_update_msg(cost_amount=1.00))
        client.last_prompt_stats = client.last_prompt_stats.carry_over()  # turn boundary
        client._track_usage_update(_usage_update_msg(cost_amount=1.20))
        assert client.last_prompt_stats.cost_usd == pytest.approx(0.20)

    def test_no_cost_field_is_a_noop(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._track_usage_update(_usage_update_msg())  # no cost key
        assert client._cost_usd_baseline == 0.0
        assert client.last_prompt_stats.cost_usd == 0.0

    def test_kiro_backend_never_tracks_cost(self, tmp_path):
        """_is_claude gates the whole cost path -- a kiro client must ignore
        a cost field even if one somehow arrived on its usage_update."""
        client = AcpClient(work_dir=tmp_path)  # default backend: kiro
        client._track_usage_update(_usage_update_msg(cost_amount=5.0))
        assert client._cost_usd_baseline == 0.0
        assert client.last_prompt_stats.cost_usd == 0.0


class TestDispatchEventsPopulatesClaudeUsage:
    """End-to-end through AcpClient._dispatch_events' "complete" arm -- the
    only site with a real JSON-RPC result to read PromptResponse.usage from."""

    @pytest.mark.asyncio
    async def test_complete_result_usage_populates_event_and_stats(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        complete_msg = types.SimpleNamespace(
            method="session/prompt",
            id=1,
            result={
                "stopReason": "end_turn",
                "usage": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "cachedReadTokens": 10,
                    "cachedWriteTokens": 5,
                    "totalTokens": 165,
                },
            },
        )

        async def _fake_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = _fake_loop
        events = [e async for e in client._dispatch_events(1, 5.0)]

        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EVENT_COMPLETE
        assert ev.usage.input_tokens == 100
        assert ev.usage.output_tokens == 50
        assert ev.usage.cache_read_tokens == 10
        assert ev.usage.cache_creation_tokens == 5
        assert ev.usage.credits == 0.0
        # last_prompt_stats itself is populated too (provider_last_turn_usage
        # and any later EVENT_COMPLETE construction read from it directly).
        assert client.last_prompt_stats.input_tokens == 100

    @pytest.mark.asyncio
    async def test_complete_with_no_usage_key_leaves_tokens_zero(self, tmp_path):
        """The queued-then-cancelled-turn shape: {stopReason} only. Must not
        raise, and must not fabricate nonzero counts."""
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        complete_msg = types.SimpleNamespace(
            method="session/prompt", id=1, result={"stopReason": "cancelled"}
        )

        async def _fake_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = _fake_loop
        events = [e async for e in client._dispatch_events(1, 5.0)]

        assert events[0].usage.input_tokens == 0
        assert events[0].usage.output_tokens == 0

    @pytest.mark.asyncio
    async def test_kiro_backend_never_reads_result_usage(self, tmp_path):
        """_is_claude gates the parse -- a kiro client must ignore a usage
        key even if one somehow arrived on its result (defensive; kiro-cli
        never actually sends this)."""
        client = AcpClient(work_dir=tmp_path)  # default backend: kiro
        complete_msg = types.SimpleNamespace(
            method="session/prompt",
            id=1,
            result={"stopReason": "end_turn", "usage": {"inputTokens": 999}},
        )

        async def _fake_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = _fake_loop
        events = [e async for e in client._dispatch_events(1, 5.0)]

        assert events[0].usage.input_tokens == 0

    @pytest.mark.asyncio
    async def test_usage_update_cost_and_result_tokens_combine_in_one_turn(self, tmp_path):
        """The realistic shape: a usage_update (cost) arrives mid-turn, then
        the complete result carries the token counts -- both must land in
        the SAME EVENT_COMPLETE's TurnUsage."""
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._cost_usd_baseline = 1.00  # a prior turn already seeded this
        update_msg = _usage_update_msg(used=500, size=200000, cost_amount=1.30)
        complete_msg = types.SimpleNamespace(
            method="session/prompt",
            id=1,
            result={
                "stopReason": "end_turn",
                "usage": {"inputTokens": 20, "outputTokens": 10},
            },
        )

        async def _fake_loop(req_id, timeout):
            yield "update", update_msg
            yield "complete", complete_msg

        client._prompt_loop = _fake_loop
        events = [e async for e in client._dispatch_events(1, 5.0)]

        complete = [e for e in events if e.kind == EVENT_COMPLETE][0]
        assert complete.usage.input_tokens == 20
        assert complete.usage.output_tokens == 10
        assert complete.usage.cost_usd == pytest.approx(0.30)
