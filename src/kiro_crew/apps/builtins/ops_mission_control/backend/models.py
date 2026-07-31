"""Ops Mission Control — data model.

Three types carry the whole app:

``Signal``
    A normalized work item. Every provider maps its native object (a CloudWatch
    alarm, a PagerDuty incident, a Datadog monitor, a webhook body) onto this one
    shape — which is what lets the board, the dispatch heartbeat, and the
    knowledge ledger stay provider-agnostic.

``Incident``
    A claimed ``Signal`` being worked, with its status, the chat slot backing the
    investigation, and the ledger entries it matched.

``LedgerEntry``
    One learned pattern: what broke, what fixed it, how much we trust that. The
    compounding-knowledge mechanism — the reason the second occurrence of a
    failure is cheaper than the first.

See ``docs/task-specs/2026/07/ops-mission-control/spec.md`` §3.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Constants — no hardcoded strings/values in business logic (AGENTS.md)
# ---------------------------------------------------------------------------

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
VALID_SEVERITIES: frozenset[str] = frozenset({SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_INFO})

STATE_FIRING = "firing"
STATE_OK = "ok"
STATE_UNKNOWN = "unknown"
VALID_STATES: frozenset[str] = frozenset({STATE_FIRING, STATE_OK, STATE_UNKNOWN})

STATUS_UNCLAIMED = "unclaimed"
STATUS_DISPATCHED = "dispatched"
STATUS_INVESTIGATING = "investigating"
STATUS_NEEDS_HUMAN = "needs_human"
STATUS_RESOLVED = "resolved"
STATUS_ESCALATED = "escalated"
STATUS_STALE = "stale"

Status = Literal[
    "unclaimed",
    "dispatched",
    "investigating",
    "needs_human",
    "resolved",
    "escalated",
    "stale",
]

#: Legal status transitions. Enforced by ``store.transition`` — an incident can
#: never jump straight from ``unclaimed`` to ``resolved``, which would leave the
#: board asserting work was done that no investigation ever ran.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_UNCLAIMED: frozenset({STATUS_DISPATCHED}),
    # ``dispatched -> needs_human`` is reachable because an investigating agent can
    # block on a tool approval BEFORE it finishes its first turn — observed live:
    # the agent's opening move was a read-only AWS probe, which parked on a
    # ``permission`` message while the incident was still ``dispatched``. Without
    # this edge the board reports a blocked incident as progressing, which is the
    # one thing an ops board must never do.
    # ``dispatched -> resolved`` is reachable because a signal can clear in the gap
    # between being claimed and the investigating agent's first turn — a flapping
    # alarm, or a GitHub issue someone closes a minute later. The reconcile SOP's
    # whole job is to resolve incidents whose signal stopped firing, and without
    # this edge it has NO legal move for that case: the incident sticks at
    # ``dispatched`` until the stale sweep hours later, so the board claims work is
    # in progress on a problem that no longer exists. Found by exercising the
    # reconcile SOP against a real cleared GitHub signal.
    STATUS_DISPATCHED: frozenset(
        {STATUS_INVESTIGATING, STATUS_NEEDS_HUMAN, STATUS_RESOLVED, STATUS_STALE}
    ),
    STATUS_INVESTIGATING: frozenset(
        {STATUS_NEEDS_HUMAN, STATUS_RESOLVED, STATUS_ESCALATED, STATUS_STALE}
    ),
    # ``needs_human -> stale`` too: an incident nobody ever answers must not pin a
    # signal as claimed forever, or the alarm silently stops being worked.
    STATUS_NEEDS_HUMAN: frozenset(
        {STATUS_INVESTIGATING, STATUS_RESOLVED, STATUS_ESCALATED, STATUS_STALE}
    ),
    # ``stale -> resolved`` for the same reason: a released incident whose signal has
    # since cleared must be closable. With only ``-> dispatched`` available,
    # reconcile's only move would be to hand a dead signal back to an agent, which
    # spends a whole investigation to conclude nothing is wrong.
    STATUS_STALE: frozenset({STATUS_DISPATCHED, STATUS_RESOLVED}),
    # Terminal states. Re-opening is a new signal, not a transition — a resolved
    # incident that "comes back" is a fresh firing with its own timeline.
    STATUS_RESOLVED: frozenset(),
    STATUS_ESCALATED: frozenset(),
}

#: Closed for good — no legal transition leads out. DERIVED from the grammar above
#: rather than hand-listed, so a future status with no outgoing edges is terminal
#: automatically and one that gains an edge stops being terminal, with no second list
#: to forget to update.
#:
#: ``store.claim`` uses this to let a CLOSED incident's signal be claimed AGAIN, as a
#: new incident. ``signal.id`` is stable for the alarm's lifetime, so without this the
#: app permanently stopped responding to any failure it had already handled once — and
#: the compounding-memory fast path, which can only pay off on a second occurrence,
#: was unreachable in production.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    status for status, onward in LEGAL_TRANSITIONS.items() if not onward
)

#: Why an incident is currently waiting on a person. Surfaced so the board can say
#: "waiting for you to approve a command" rather than the ambiguous "needs human",
#: which reads the same whether the agent wants a decision or has given up.
BLOCKED_ON_APPROVAL = "awaiting_approval"
BLOCKED_ON_INPUT = "awaiting_input"
BLOCKED_ON_DIAGNOSIS = "awaiting_diagnosis"

#: Statuses that count as open work for board counts and the stale sweep.
OPEN_STATUSES: frozenset[str] = frozenset(
    {STATUS_UNCLAIMED, STATUS_DISPATCHED, STATUS_INVESTIGATING, STATUS_NEEDS_HUMAN}
)

MODE_OBSERVE = "observe"
MODE_PROPOSE = "propose"
MODE_ACT = "act"

#: Autonomy ordering. ``observe`` < ``propose`` < ``act``; the effective mode for
#: an incident is the MINIMUM of the app default and any matching rule
#: (tightest-wins), mirroring the governance ``effective = POLICY ∩ PROFILE``
#: algebra. Default is ``observe``: a stranger's first install must not be able to
#: write to their production tracker.
MODE_ORDER: dict[str, int] = {MODE_OBSERVE: 0, MODE_PROPOSE: 1, MODE_ACT: 2}

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
#: Confidence decays along this chain when an entry goes unused (ledger hygiene).
CONFIDENCE_DECAY: dict[str, str] = {
    CONFIDENCE_HIGH: CONFIDENCE_MEDIUM,
    CONFIDENCE_MEDIUM: CONFIDENCE_LOW,
    CONFIDENCE_LOW: CONFIDENCE_LOW,
}

TRUST_VERIFIED = "verified"
TRUST_OBSERVED = "observed"

ACTION_ACK = "ack"
ACTION_RESOLVE = "resolve"
ACTION_COMMENT = "comment"
VALID_ACTIONS: frozenset[str] = frozenset({ACTION_ACK, ACTION_RESOLVE, ACTION_COMMENT})

#: Length of the hex digest kept for fingerprints and ledger entry ids. 16 hex
#: chars = 64 bits, ample against accidental collision in a per-user ledger while
#: staying short enough to read in a log line.
_DIGEST_LEN = 16

#: Substrings replaced when building a fingerprint, so a recurrence of the same
#: failure on a different host/instance/date matches its ancestor.
_VOLATILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ISO-8601-ish timestamps
    re.compile(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(:\d{2})?(\.\d+)?z?", re.IGNORECASE),
    # bare dates and clock times
    re.compile(r"\d{4}-\d{2}-\d{2}"),
    re.compile(r"\b\d{2}:\d{2}(:\d{2})?\b"),
    # uuids
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
    # long hex runs (request ids, digests) and i-/vol- style resource suffixes
    re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(i|vol|eni|snap|ami)-[0-9a-f]{8,}\b", re.IGNORECASE),
    # bare numbers (counts, thresholds, ports) — a DLQ at 500 and at 900 is the
    # same pattern
    re.compile(r"\d+"),
)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_severity(raw: str) -> str:
    """Map a provider's severity vocabulary onto ours.

    Unknown values fall back to ``warning`` rather than ``critical``: a provider
    we do not understand should not be able to manufacture top-priority work, and
    should not be silently demoted to ``info`` either.
    """
    v = (raw or "").strip().lower()
    if v in VALID_SEVERITIES:
        return v
    if v in {"p1", "sev1", "sev-1", "high", "error", "alarm", "urgent", "fatal"}:
        return SEVERITY_CRITICAL
    if v in {"p2", "p3", "sev2", "sev-2", "warn", "medium", "degraded"}:
        return SEVERITY_WARNING
    if v in {"p4", "p5", "sev3", "sev-3", "low", "ok", "nominal", "debug"}:
        return SEVERITY_INFO
    return SEVERITY_WARNING


def normalize_state(raw: str) -> str:
    """Map a provider's state vocabulary onto ``firing`` / ``ok`` / ``unknown``.

    Unknown values become ``unknown``, NOT ``firing`` — an unparseable state must
    not create phantom work on the board.
    """
    v = (raw or "").strip().lower()
    if v in VALID_STATES:
        return v
    if v in {"alarm", "alert", "triggered", "open", "firing", "acknowledged", "warn"}:
        return STATE_FIRING
    if v in {"ok", "resolved", "closed", "cleared", "nominal"}:
        return STATE_OK
    return STATE_UNKNOWN


def compute_fingerprint(source: str, resource: str, title: str) -> str:
    """Stable identity for *the kind of failure this is*.

    Deliberately excludes timestamps, uuids, instance ids, and bare numbers (see
    ``_VOLATILE_PATTERNS``) so the same failure recurring tomorrow on a different
    host produces the SAME fingerprint and therefore matches its ledger ancestor.
    That matching is the entire compounding-knowledge mechanism; a fingerprint
    that drifts per occurrence would make the ledger useless.
    """
    shape = f"{title or ''} {resource or ''}".strip().lower()
    for pattern in _VOLATILE_PATTERNS:
        shape = pattern.sub("#", shape)
    shape = re.sub(r"[^a-z0-9#]+", " ", shape).strip()
    basis = f"{(source or '').strip().lower()}|{shape}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_DIGEST_LEN]


@dataclass(frozen=True)
class Signal:
    """A normalized work item from any provider."""

    id: str
    source: str
    title: str
    severity: str = SEVERITY_WARNING
    state: str = STATE_FIRING
    fired_at: str = ""
    resource: str = ""
    url: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""

    @classmethod
    def create(
        cls,
        *,
        source: str,
        native_id: str,
        title: str,
        severity: str = SEVERITY_WARNING,
        state: str = STATE_FIRING,
        fired_at: str = "",
        resource: str = "",
        url: str = "",
        labels: dict[str, str] | None = None,
    ) -> Signal:
        """Build a Signal with normalization and fingerprinting applied.

        Adapters should always go through this rather than the raw constructor,
        so severity/state vocabularies and fingerprints stay consistent across
        providers — including companion-contributed ones.
        """
        return cls(
            id=f"{source}:{native_id}",
            source=source,
            title=title,
            severity=normalize_severity(severity),
            state=normalize_state(state),
            fired_at=fired_at or utc_now_iso(),
            resource=resource,
            url=url,
            labels=dict(labels or {}),
            fingerprint=compute_fingerprint(source, resource, title),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Signal:
        labels = data.get("labels")
        return cls(
            id=str(data.get("id", "")),
            source=str(data.get("source", "")),
            title=str(data.get("title", "")),
            severity=normalize_severity(str(data.get("severity", ""))),
            state=normalize_state(str(data.get("state", ""))),
            fired_at=str(data.get("fired_at", "")),
            resource=str(data.get("resource", "")),
            url=str(data.get("url", "")),
            labels={str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {},
            fingerprint=str(data.get("fingerprint", "")),
        )


@dataclass
class Incident:
    """A claimed Signal being worked."""

    incident_id: str
    signal: Signal
    status: str = STATUS_UNCLAIMED
    operating_mode: str = MODE_OBSERVE
    claimed_at: str = ""
    updated_at: str = ""
    slot_key: str = ""
    slack_thread_ts: str = ""
    ledger_matches: list[str] = field(default_factory=list)
    diagnosis: str = ""
    proposed_action: dict[str, Any] | None = None
    resolution: str = ""
    #: Why this incident is waiting on a person (one of the ``BLOCKED_ON_*``
    #: constants), or "" when it is not blocked. Derived from the investigation
    #: slot rather than stored as intent, so it cannot go stale against reality.
    blocked_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["signal"] = self.signal.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Incident:
        raw_signal = data.get("signal")
        matches = data.get("ledger_matches")
        proposed = data.get("proposed_action")
        return cls(
            incident_id=str(data.get("incident_id", "")),
            signal=Signal.from_dict(raw_signal if isinstance(raw_signal, dict) else {}),
            status=str(data.get("status", STATUS_UNCLAIMED)),
            operating_mode=str(data.get("operating_mode", MODE_OBSERVE)),
            claimed_at=str(data.get("claimed_at", "")),
            updated_at=str(data.get("updated_at", "")),
            slot_key=str(data.get("slot_key", "")),
            slack_thread_ts=str(data.get("slack_thread_ts", "")),
            ledger_matches=[str(m) for m in matches] if isinstance(matches, list) else [],
            diagnosis=str(data.get("diagnosis", "")),
            proposed_action=proposed if isinstance(proposed, dict) else None,
            resolution=str(data.get("resolution", "")),
            blocked_reason=str(data.get("blocked_reason", "")),
        )

    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


@dataclass
class LedgerEntry:
    """One learned failure pattern and its fix."""

    entry_id: str
    pattern: str
    fix: str
    fingerprints: list[str] = field(default_factory=list)
    confidence: str = CONFIDENCE_MEDIUM
    trust: str = TRUST_OBSERVED
    use_count: int = 0
    first_seen: str = ""
    last_used: str = ""
    source: str = "agent"

    @staticmethod
    def compute_id(pattern: str, fix: str) -> str:
        """Content-addressed id over (pattern, fix).

        Content addressing is what makes the append-only JSONL ledger mergeable
        across git-synced team members without conflict resolution: two people
        who learn the same lesson independently produce the same id, so the merge
        is a dedupe rather than a fight (spec §3.3).
        """
        basis = f"{(pattern or '').strip().lower()}|{(fix or '').strip().lower()}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_DIGEST_LEN]

    @classmethod
    def create(
        cls,
        *,
        pattern: str,
        fix: str,
        fingerprints: list[str] | None = None,
        confidence: str = CONFIDENCE_MEDIUM,
        trust: str = TRUST_OBSERVED,
        source: str = "agent",
    ) -> LedgerEntry:
        now = utc_now_iso()
        return cls(
            entry_id=cls.compute_id(pattern, fix),
            pattern=pattern,
            fix=fix,
            fingerprints=list(fingerprints or []),
            confidence=confidence if confidence in CONFIDENCE_DECAY else CONFIDENCE_MEDIUM,
            trust=trust if trust in {TRUST_VERIFIED, TRUST_OBSERVED} else TRUST_OBSERVED,
            use_count=0,
            first_seen=now,
            last_used=now,
            source=source,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        fps = data.get("fingerprints")
        try:
            use_count = int(data.get("use_count", 0))
        except (TypeError, ValueError):
            use_count = 0
        return cls(
            entry_id=str(data.get("entry_id", "")),
            pattern=str(data.get("pattern", "")),
            fix=str(data.get("fix", "")),
            fingerprints=[str(f) for f in fps] if isinstance(fps, list) else [],
            confidence=str(data.get("confidence", CONFIDENCE_MEDIUM)),
            trust=str(data.get("trust", TRUST_OBSERVED)),
            use_count=use_count,
            first_seen=str(data.get("first_seen", "")),
            last_used=str(data.get("last_used", "")),
            source=str(data.get("source", "agent")),
        )


def effective_mode(app_default: str, rule_mode: str | None) -> str:
    """Resolve the operating mode for one incident — tightest-wins.

    ``effective = min(app_default, rule_mode)`` over ``observe < propose < act``.
    A rule can only ever NARROW what the app default already allows, so a
    user-authored rule cannot escalate an instance the operator has pinned to
    ``observe``. With no matching rule the app default applies (spec §5.3).
    """
    base = MODE_ORDER.get(app_default, 0)
    if rule_mode is None:
        level = base
    else:
        level = min(base, MODE_ORDER.get(rule_mode, 0))
    for name, value in MODE_ORDER.items():
        if value == level:
            return name
    return MODE_OBSERVE
