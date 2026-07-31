"""Autonomy gate and rotation-driven tier arming.

Two safety decisions live here.

**The autonomy gate** (``resolve_mode`` / ``authorize_action``) decides whether the
agent may write to a user's production tooling. The default is ``observe`` —
nothing is written anywhere. ``act`` requires BOTH an app-level mode of ``act`` AND
a user-authored rule whose predicate matches this specific signal. There is no
wildcard: a rule must name a source and either a resource glob or a label match, so
"act on everything" is not expressible. Autonomy is earned per-pattern by the
operator after they have watched the agent's proposals be correct.

This is a deliberate divergence from the system this is modeled on, which
auto-resolved two known machine-generated intakes by default. That team could
reason about which intakes were safe because they had built them; a stranger's
first install has no such basis, and auto-resolving a human's production page on
day one is a much worse failure than being slightly slow.

**Tier arming** (``tier_states``) maps the on-shift answer onto which SOP tiers
run. Note the fail-open: an unreachable rotation API arms the on-shift tier rather
than disarming it. Wrongly arming costs a few API polls; wrongly disarming means
nobody notices an outage.

See ``docs/task-specs/2026/07/ops-mission-control/spec.md`` §4.4, §5.3.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    MODE_ACT,
    MODE_OBSERVE,
    MODE_ORDER,
    MODE_PROPOSE,
    VALID_ACTIONS,
    Signal,
    effective_mode,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import ShiftStatus
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

TIER_ALWAYS = "always"
TIER_ON_SHIFT = "on_shift"
TIER_PRIMARY = "primary"

#: Cron names per tier, as the SCHEDULER knows them.
#:
#: These MUST match what app registration actually creates. A manifest cron named
#: ``dispatch`` is registered namespaced as ``ops-mission-control/dispatch`` — so the
#: bare ``omc-*`` names this table used to carry matched no job at all, and every
#: pause/resume the rotation tier emitted silently targeted nothing. The whole tier
#: mechanism was inert. Found by exercising the rotation-check SOP against the real
#: scheduler; pinned by ``test_tier_cron_names_match_the_manifest``.
_CRON_PREFIX = "ops-mission-control"

TIER_CRONS: dict[str, tuple[str, ...]] = {
    TIER_ALWAYS: (f"{_CRON_PREFIX}/rotation-check", f"{_CRON_PREFIX}/reconcile"),
    TIER_ON_SHIFT: (f"{_CRON_PREFIX}/dispatch",),
    TIER_PRIMARY: (f"{_CRON_PREFIX}/ledger-hygiene",),
}

#: Default app-level autonomy. ``observe`` — see the module docstring.
DEFAULT_APP_MODE = MODE_OBSERVE

#: Config key holding the user's autonomy rules.
_RULES_KEY = "autonomy_rules"
_APP_MODE_KEY = "mode"
_PRIMARY_KEY = "primary_instance"


@dataclass(frozen=True)
class AutonomyRule:
    """One user-authored grant.

    A rule must name a ``source`` AND at least one of ``resource_glob`` /
    ``label_match``. A source-only rule is refused (see ``from_dict``): "act on
    everything CloudWatch reports" is exactly the blanket grant this design
    exists to prevent.
    """

    source: str
    mode: str
    resource_glob: str = ""
    label_match: dict[str, str] | None = None
    actions: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutonomyRule | None:
        source = str(data.get("source", "")).strip()
        mode = str(data.get("mode", "")).strip()
        if not source or mode not in MODE_ORDER:
            return None
        resource_glob = str(data.get("resource_glob", "")).strip()
        raw_labels = data.get("label_match")
        label_match = (
            {str(k): str(v) for k, v in raw_labels.items()}
            if isinstance(raw_labels, dict) and raw_labels
            else None
        )
        # No wildcard grants: a rule that names only a source would authorize
        # every signal from that provider forever.
        if mode == MODE_ACT and not resource_glob and not label_match:
            logger.warning(
                "ops-mission-control: refusing act-rule for %r with no resource_glob "
                "or label_match (blanket grants are not permitted)",
                source,
            )
            return None
        raw_actions = data.get("actions")
        actions = (
            frozenset(str(a) for a in raw_actions if str(a) in VALID_ACTIONS)
            if isinstance(raw_actions, list)
            else frozenset()
        )
        return cls(
            source=source,
            mode=mode,
            resource_glob=resource_glob,
            label_match=label_match,
            actions=actions,
        )

    def matches(self, signal: Signal) -> bool:
        if self.source != signal.source:
            return False
        if self.resource_glob and not fnmatch.fnmatch(signal.resource, self.resource_glob):
            return False
        if self.label_match:
            for key, expected in self.label_match.items():
                if signal.labels.get(key) != expected:
                    return False
        return True


def app_mode() -> str:
    """Operator-set ceiling on autonomy, defaulting to ``observe``."""
    mode = str(read_config().get(_APP_MODE_KEY, DEFAULT_APP_MODE))
    return mode if mode in MODE_ORDER else DEFAULT_APP_MODE


def load_rules() -> list[AutonomyRule]:
    raw = read_config().get(_RULES_KEY)
    if not isinstance(raw, list):
        return []
    rules: list[AutonomyRule] = []
    for item in raw:
        if isinstance(item, dict):
            rule = AutonomyRule.from_dict(item)
            if rule is not None:
                rules.append(rule)
    return rules


def resolve_mode(signal: Signal) -> str:
    """Effective operating mode for one signal — tightest-wins.

    With no matching rule the app mode applies. A matching rule can only NARROW
    it, so a rule cannot escalate an instance the operator pinned to ``observe``.
    """
    base = app_mode()
    matching = [r for r in load_rules() if r.matches(signal)]
    if not matching:
        return base
    # Most permissive matching rule, still clamped by the app ceiling.
    best = max(matching, key=lambda r: MODE_ORDER.get(r.mode, 0))
    return effective_mode(base, best.mode)


def authorize_action(signal: Signal, action: str) -> tuple[bool, str]:
    """Decide whether ``action`` may actually execute against ``signal``.

    Returns ``(allowed, reason)``. Every decision — allow and deny — is
    SEL-audited, because this is the boundary where the agent gains the ability
    to change something in the user's production tooling.
    """
    if action not in VALID_ACTIONS:
        return _audited(signal, action, False, f"unknown action {action!r}")

    mode = resolve_mode(signal)
    if MODE_ORDER.get(mode, 0) < MODE_ORDER[MODE_ACT]:
        return _audited(
            signal,
            action,
            False,
            f"mode is {mode!r} — execution requires 'act'",
        )

    matching = [r for r in load_rules() if r.matches(signal) and r.mode == MODE_ACT]
    if not matching:
        return _audited(signal, action, False, "no matching act-rule for this signal")

    # A rule may narrow which actions it grants. An empty set means "any action
    # this sink supports", which is the common case for a tightly-scoped rule.
    for rule in matching:
        if not rule.actions or action in rule.actions:
            return _audited(signal, action, True, f"granted by rule on {rule.source}")
    return _audited(signal, action, False, f"matching rule does not grant {action!r}")


def _audited(signal: Signal, action: str, allowed: bool, reason: str) -> tuple[bool, str]:
    sel().log_api_access(
        caller="core:ops-mission-control",
        operation="action_authorize",
        outcome="success" if allowed else "rejected",
        resources=f"signal={signal.id} action={action}",
        error="" if allowed else reason,
    )
    return allowed, reason


def is_primary() -> bool:
    """Whether this instance runs the ``primary`` tier.

    Defaults to ``True`` so a single-instance install runs ledger hygiene. A team
    sets exactly one instance primary so the pass does not run N times over a
    shared ledger.
    """
    return bool(read_config().get(_PRIMARY_KEY, True))


def tier_states(shift: ShiftStatus) -> dict[str, bool]:
    """Which tiers should be armed, given the current shift status.

    ``shift.unknown`` arms the on-shift tier: failing to reach a rotation API must
    not silently switch off incident response.
    """
    on_shift_armed = bool(shift.on_shift or shift.unknown)
    return {
        TIER_ALWAYS: True,
        TIER_ON_SHIFT: on_shift_armed,
        TIER_PRIMARY: is_primary(),
    }


def crons_for_tier(tier: str) -> tuple[str, ...]:
    return TIER_CRONS.get(tier, ())


def describe(shift: ShiftStatus) -> dict[str, Any]:
    """Rotation + autonomy summary for the dashboard."""
    states = tier_states(shift)
    return {
        "on_shift": shift.on_shift,
        "who": shift.who,
        "until": shift.until,
        "unknown": shift.unknown,
        "tiers": states,
        # Flat union across all ARMED tiers — what is running right now.
        "armed_crons": sorted(
            name for tier, armed in states.items() if armed for name in crons_for_tier(tier)
        ),
        # Per-tier breakdown, which is what the rotation-check SOP actually needs.
        # Without it the only cron list on this response is the flat union above,
        # which OFF shift still contains ``ops-mission-control/rotation-check`` (an
        # ``always``-tier job). An agent told to "pause the armed crons" would then
        # pause the very cron that re-arms the instance, permanently disabling
        # incident response. The SOP must pause exactly ``tier_crons.on_shift``.
        "tier_crons": {tier: list(crons_for_tier(tier)) for tier in states},
        "mode": app_mode(),
        "rules": len(load_rules()),
        "primary": is_primary(),
        "modes_available": [MODE_OBSERVE, MODE_PROPOSE, MODE_ACT],
    }
