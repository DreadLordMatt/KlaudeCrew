"""Ops Mission Control — provider extension points.

Four narrow Protocols, each answering exactly one question:

===================  ==============================================
``SignalSource``     What is firing right now?
``RotationSource``   Who is on shift?
``ActionSink``       How do I acknowledge / resolve / comment?
``EvidenceSource``   What context surrounds this signal?
===================  ==============================================

This mirrors the Composed Platform Providers (CPP) pattern in
``kiro_crew/platform/interfaces.py``: the core defines Protocols, ships a default
adapter for each, and never branches on which edition is running. The public
adapters (CloudWatch, PagerDuty, Datadog, GitHub Issues, webhook, no-op) live
beside this file; an Amazon-internal companion contributes its own through the
ADD-only registry and is never imported here.

Splitting the seam four ways rather than defining one fat ``OpsProvider`` is
deliberate: real providers cover different subsets. CloudWatch has alarms and
metrics but no rotation and nothing to resolve; a static YAML rota answers only
"who is on shift". A fat interface would force every adapter to stub three
quarters of itself.

See ``docs/task-specs/2026/07/ops-mission-control/spec.md`` §4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal

#: Default caps for one investigation's evidence gathering. These exist because
#: evidence sources are paid, rate-limited third-party APIs: an investigation
#: that fans out without a ceiling can burn a user's Datadog quota or stall the
#: dispatch heartbeat behind a slow Logs Insights query.
DEFAULT_EVIDENCE_TIMEOUT_SECS = 20.0
DEFAULT_EVIDENCE_MAX_CALLS = 6
DEFAULT_EVIDENCE_MAX_BYTES = 64 * 1024

#: Per-source cap for one poll cycle. A provider returning thousands of stale
#: alarms must not be able to flood the board or the claim loop.
DEFAULT_POLL_LIMIT = 100

#: Wall-clock budget for a single source's poll. The dispatch heartbeat polls all
#: sources concurrently and must finish well inside its 2-minute interval, so one
#: unreachable provider cannot stall the others.
DEFAULT_POLL_TIMEOUT_SECS = 15.0


@dataclass(frozen=True)
class EvidenceBudget:
    """Caps for one investigation's evidence gathering.

    One budget served every adapter, which does not fit how they actually behave: a
    CloudWatch Logs Insights query is a submit-then-poll round trip that legitimately
    wants ~25s, while a Datadog REST call either answers in a couple of seconds or is
    broken. CloudWatch had already noticed — it declared ``_LOG_MAX_WAIT_SECS = 25.0``
    and then applied ``min(25.0, budget.timeout_secs)``, so with the global default of
    20s its own ceiling was unreachable dead code.

    ``for_source`` resolves a per-adapter budget from an adapter's declared
    ``evidence_budget_hint``, clamped so an adapter can never exceed the operator's
    configured ceiling. The hint expresses "this is what I need"; the operator's value
    stays the authority.
    """

    timeout_secs: float = DEFAULT_EVIDENCE_TIMEOUT_SECS
    max_calls: int = DEFAULT_EVIDENCE_MAX_CALLS
    max_bytes: int = DEFAULT_EVIDENCE_MAX_BYTES

    def for_source(self, source: Any) -> "EvidenceBudget":
        """This budget narrowed (never widened) by ``source``'s declared hint.

        An adapter with no hint gets this budget unchanged, so adding the attribute is
        opt-in and no existing adapter changes behavior. Every field is clamped with
        ``min``: a hint asking for MORE than the operator allowed is ignored, because a
        provider adapter must not be able to raise its own spend ceiling — the same
        reason the autonomy gate is resolved outside the adapter.
        """
        # Mapping, not dict: an adapter is encouraged to expose the hint as an
        # immutable MappingProxyType (a mutable class attribute shared across
        # instances invites an adapter rewriting its own ceiling at runtime), and a
        # mappingproxy is NOT a dict. Checking for dict silently ignored every
        # correctly-written hint — caught only because the test asserted the
        # clamped VALUE rather than that the call returned something.
        hint = getattr(source, "evidence_budget_hint", None)
        if not isinstance(hint, Mapping) or not hint:
            return self

        def _clamp(key: str, current: float) -> float:
            raw = hint.get(key)
            if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0:
                return current
            return min(float(raw), current)

        return EvidenceBudget(
            timeout_secs=_clamp("timeout_secs", self.timeout_secs),
            max_calls=int(_clamp("max_calls", self.max_calls)),
            max_bytes=int(_clamp("max_bytes", self.max_bytes)),
        )


@dataclass(frozen=True)
class Evidence:
    """One piece of context gathered about a signal.

    ``body`` is caller-facing text destined for a model prompt and/or a Slack
    thread. It MUST be passed through ``security.redact`` before it leaves the
    adapter — provider payloads routinely embed credentials, presigned URLs, and
    account identifiers. The gather helpers in this package do that centrally so
    an adapter cannot forget.
    """

    source: str
    kind: str
    title: str
    body: str
    url: str = ""


@dataclass(frozen=True)
class ShiftStatus:
    """Who is on shift, per a ``RotationSource``."""

    on_shift: bool
    who: str = ""
    until: str = ""
    #: True when the source cannot answer (unconfigured, API down). The tier gate
    #: treats this as ARMED rather than disarmed: failing to reach a rotation API
    #: must not silently switch off a team's incident response. Fail-open is
    #: correct here precisely because the on_shift tier only *observes* by
    #: default — arming it costs API polls, while wrongly disarming it costs
    #: missed incidents.
    unknown: bool = False


@dataclass(frozen=True)
class ActionResult:
    """Outcome of an ``ActionSink.execute`` call."""

    ok: bool
    action: str
    detail: str = ""
    error: str = ""


@dataclass(frozen=True)
class ProviderInfo:
    """Catalog metadata for the settings UI.

    ``config_fields`` are non-secret and land in the app's ``data/config.json``.
    ``secret_fields`` are write-only and land in the keystone-protected secret
    store — they are NEVER returned by any read endpoint (spec §5.1).
    """

    id: str
    display_name: str
    roles: tuple[str, ...]
    configured: bool
    config_fields: tuple[str, ...] = ()
    secret_fields: tuple[str, ...] = ()
    detail: str = ""
    labels: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class SignalSource(Protocol):
    """A source of work items."""

    @property
    def id(self) -> str:
        """Stable adapter id, e.g. ``"cloudwatch"``. Used as the registry key."""
        ...

    @property
    def display_name(self) -> str: ...

    def configured(self) -> bool:
        """True when this adapter has everything it needs to poll.

        An unconfigured source is skipped by the heartbeat and shown as
        unconfigured in the UI — it never raises and never blocks a poll cycle.
        """
        ...

    async def poll(self) -> list[Signal]:
        """Return currently-firing signals, normalized via ``Signal.create``."""
        ...


@runtime_checkable
class RotationSource(Protocol):
    """Answers whether this operator is currently on shift."""

    @property
    def id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    def configured(self) -> bool: ...

    async def on_shift(self) -> ShiftStatus: ...


@runtime_checkable
class ActionSink(Protocol):
    """Performs a write against a provider (ack / resolve / comment)."""

    @property
    def id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    def configured(self) -> bool: ...

    def supported_actions(self) -> frozenset[str]:
        """Subset of ``models.VALID_ACTIONS`` this sink can perform."""
        ...

    async def execute(self, signal: Signal, action: str, payload: dict[str, Any]) -> ActionResult:
        """Perform ``action`` for ``signal``.

        Callers MUST have resolved the autonomy gate first — a sink does not
        police its own authority (spec §5.3). The no-op sink is the default, so an
        unconfigured install cannot write anywhere.
        """
        ...


@runtime_checkable
class EvidenceSource(Protocol):
    """Gathers read-only context about a signal."""

    @property
    def id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    def configured(self) -> bool: ...

    async def gather(self, signal: Signal, budget: EvidenceBudget) -> list[Evidence]: ...
