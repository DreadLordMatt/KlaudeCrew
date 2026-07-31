"""Ops Mission Control — the dispatch engine.

This is the module that turns the parts into a working first responder. Without
it the registry polls into the void: signals are normalized but nothing claims
them, and ``Incident.ledger_matches`` stays empty forever — which would leave the
compounding-memory mechanism structurally present but functionally dead.

One cycle:

1. Poll every configured ``SignalSource`` concurrently.
2. Diff against the dispatch index and claim what is unowned (atomically — see
   ``store.claim``; a losing claimant skips rather than duplicating work).
3. **Match each claim's fingerprint against the knowledge ledger** and attach the
   hits, so the investigation opens already knowing what this failure was last
   time. This step is the whole point.
4. Release investigations that have gone idle, so a dead agent cannot hold a
   signal claimed and therefore unworked.

The cycle is deliberately *not* an agent turn. It is deterministic Python the cron
calls once, which keeps the expensive part (an actual investigation) to signals
that genuinely need one and keeps the heartbeat's cost flat. The internal workflow
this models kept its heartbeat silent and cheap for exactly that reason.

See ``docs/task-specs/2026/07/ops-mission-control/spec.md`` §4.5.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import (
    ledger,
    rotation,
    slack_out,
    store,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    STATE_FIRING,
    STATUS_STALE,
    TERMINAL_STATUSES,
    Incident,
    LedgerEntry,
    Signal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    Evidence,
    EvidenceBudget,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.registry import get_registry
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Claims per cycle. A provider that fans out 200 alarms at once must not be able
#: to spawn 200 investigation sessions — the cap turns a storm into a queue that
#: drains over successive heartbeats instead of a thundering herd.
DEFAULT_MAX_CLAIMS_PER_CYCLE = 3

#: Seconds of inactivity before a claimed incident is released for re-pickup.
#: Two hours: long enough that a genuinely slow investigation is not yanked away,
#: short enough that a crashed one is noticed the same shift.
DEFAULT_STALE_AFTER_SECS = 2 * 60 * 60

_CONFIG_MAX_CLAIMS = "max_claims_per_cycle"
_CONFIG_STALE_AFTER = "stale_after_secs"

#: Total characters of provider evidence rendered into one investigation brief.
#:
#: The per-item ``EvidenceBudget`` (64 KB) bounds what an ADAPTER may return, which is
#: the right cap for a spool but far too large for a prompt: six calls at 64 KB is
#: ~384 KB, and a real beta-account brief measured 37k chars from just two items
#: against a documented 50k TOTAL session context budget (see ``context.py``). Evidence
#: only started reaching the prompt when brokering landed, so this cap is new work, not
#: a regression. 8k is roughly the conversation budget — enough for an alarm history
#: plus a screenful of log lines, which is what a first diagnosis actually reads.
MAX_BRIEF_EVIDENCE_CHARS = 8000

#: Characters of any single evidence item, so one huge log dump cannot crowd out the
#: alarm history that would have explained it.
MAX_BRIEF_EVIDENCE_ITEM_CHARS = 4000


@dataclass
class ClaimedIncident:
    """One newly-claimed incident plus the context an investigation needs."""

    incident: Incident
    matches: list[LedgerEntry] = field(default_factory=list)
    fast_path: bool = False
    #: Redacted provider context, gathered by the GATEWAY and handed to the agent.
    #: See ``investigation_brief`` for why the agent is not given credentials.
    evidence: list[Evidence] = field(default_factory=list)
    #: Ledger entries that are semantically SIMILAR to this signal but whose fingerprint
    #: does NOT match. Kept separate from ``matches`` deliberately: a fingerprint match is
    #: evidence this exact failure recurred, while a similar one is a lead. Merging them
    #: would let a near-miss inherit "used 4x, verified" authority it has not earned, and
    #: would make ``record_use`` inflate the use count of an entry this incident never
    #: actually used — corrupting the one number that tells a responder how proven a fix is.
    similar: list[LedgerEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident": self.incident.to_dict(),
            "matches": [m.to_dict() for m in self.matches],
            "similar": [m.to_dict() for m in self.similar],
            "evidence": [
                {"source": e.source, "kind": e.kind, "title": e.title, "body": e.body}
                for e in self.evidence
            ],
            # True when a verified, high-confidence pattern matched: the
            # investigation can propose that fix directly instead of re-deriving
            # it. This is the "known-pattern fast path".
            "fast_path": self.fast_path,
        }


@dataclass
class CycleResult:
    """Everything one dispatch cycle did. Empty means the cron must stay silent."""

    claimed: list[ClaimedIncident] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    polled: int = 0
    unclaimed_remaining: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    skipped_reason: str = ""

    @property
    def changed(self) -> bool:
        """Whether anything happened worth reporting.

        The dispatch cron checks this and emits NOTHING when it is false.
        Silence-by-default is a hard requirement, not an optimization: the
        workflow this models stayed usable at ~200 messages/week precisely
        because its heartbeat never spoke unless there was news.
        """
        return bool(self.claimed or self.released)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimed": [c.to_dict() for c in self.claimed],
            "released": self.released,
            "polled": self.polled,
            "unclaimed_remaining": self.unclaimed_remaining,
            "errors": self.errors,
            "changed": self.changed,
            "skipped_reason": self.skipped_reason,
        }


def _config_int(key: str, default: int) -> int:
    raw = read_config().get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _attach_similar_safely(claimed: ClaimedIncident) -> None:
    """Resolve the shared vector store and attach similar lessons. Never raises.

    The store is resolved HERE rather than threaded through ``run_cycle`` because it is
    an optional enhancement: an instance with no vector store (model still downloading,
    or a deliberately minimal install) must dispatch exactly as before. Resolving lazily
    also keeps ``dispatch`` importable without pulling in SQLite/FAISS.

    Runs on a worker thread — the caller wraps it in ``asyncio.to_thread`` — because both
    the import and the search touch synchronous SQLite.
    """
    store_obj = None
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.vector_memory import VectorMemoryStore

        # Constructed per call, matching the convention in cli_commands/onboarding_import:
        # there is no shared singleton, and holding one open across cycles would keep a
        # SQLite handle alive for a feature that may never be used on this install.
        store_obj = VectorMemoryStore(embedding_dim=KiroCrewConfig.load().memory.embedding_dim)
        store_obj.init()
        attach_similar_lessons(claimed, store_obj)
    except Exception:  # noqa: BLE001 — no store, or a broken one, is a supported state
        logger.debug(
            "ops-mission-control: semantic recall unavailable; fingerprint matches stand",
            exc_info=True,
        )
    finally:
        if store_obj is not None:
            try:
                store_obj.close()
            except Exception:  # noqa: BLE001 — a close fault must not surface here
                logger.debug("ops-mission-control: vector store close failed")


def attach_similar_lessons(
    claimed: ClaimedIncident, store: Any, *, limit: int = 3
) -> ClaimedIncident:
    """Attach semantically similar ledger entries that the fingerprint missed.

    This is the payoff of indexing the ledger: a fingerprint match only fires when the
    SAME failure shape recurs, so a teammate's lesson about an equivalent failure on a
    different resource is invisible to it. Semantic recall surfaces that lead.

    Deliberately does NOT call ``record_use``. A similar hit is not a use — inflating the
    count would corrupt the signal that decides ``is_fast_path``, which is the one thing
    standing between a remembered fix and a confidently-wrong one.

    Fingerprint matches are excluded from the result, so the brief never lists the same
    entry twice under two different confidence framings.

    ``store`` is injected (never imported here) so dispatch has no hard dependency on the
    vector store: a caller without one passes ``None`` and this is a no-op.
    """
    if store is None:
        return claimed
    query = f"{claimed.incident.signal.title} {claimed.incident.signal.resource}".strip()
    if not query:
        return claimed
    try:
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_index

        rows = ledger_index.search_similar(store, query, limit=limit + len(claimed.matches))
    except Exception:  # noqa: BLE001 — semantic recall is additive, never required
        logger.exception("ops-mission-control: similar-lesson lookup failed")
        return claimed

    if not rows:
        return claimed

    # Map the indexed text back to real ledger entries. The index stores
    # "<pattern> — fix: <fix>", so match on the pattern prefix rather than trying to
    # reverse the format — a text round-trip would break the moment entry_text changes.
    matched_ids = {m.entry_id for m in claimed.matches}
    by_text: dict[str, LedgerEntry] = {}
    for entry in ledger.read_entries():
        if entry.entry_id in matched_ids:
            continue
        by_text[ledger_index.entry_text(entry)] = entry

    found: list[LedgerEntry] = []
    for row in rows:
        # Distinct name: `entry` is already bound as a LedgerEntry by the loop above,
        # and reusing it for an Optional is what mypy flagged.
        hit = by_text.get(str(row.get("text", "")))
        if hit is not None and hit not in found:
            found.append(hit)
        if len(found) >= limit:
            break
    claimed.similar = found
    return claimed


def attach_ledger_matches(incident: Incident) -> ClaimedIncident:
    """Bind what we already know about this failure to a fresh incident.

    The fingerprint lookup is what makes the second occurrence of a failure
    cheaper than the first. Matching also *records the use*, so an entry that
    keeps proving useful climbs the ranking and an entry nobody needs decays out
    during hygiene — the ledger stays a working index rather than an archive.
    """
    matches = ledger.match(incident.signal.fingerprint)
    # Record the use and keep the UPDATED entry: rendering the pre-increment copy
    # would show "used 0×" for a pattern this very incident just used, which
    # misreports the one number that tells a responder how proven a fix is.
    recorded: list[LedgerEntry] = []
    for entry in matches:
        updated = ledger.record_use(entry.entry_id, incident.signal.fingerprint)
        recorded.append(updated or entry)
    matches = recorded

    fast_path = ledger.is_fast_path(matches)
    if matches:
        store.update_fields(incident.incident_id, ledger_matches=[m.entry_id for m in matches])
        incident.ledger_matches = [m.entry_id for m in matches]
    return ClaimedIncident(incident=incident, matches=matches, fast_path=fast_path)


async def run_cycle(
    *, max_claims: int | None = None, slack_client: Any | None = None
) -> CycleResult:
    """Run one dispatch cycle.

    Safe to call concurrently with itself: claims are atomic, so a second caller
    simply finds nothing left to claim.

    ``slack_client`` is the gateway's live Slack client, passed in by the caller
    (KiroCrew has no global state accessor). None simply means the pin board is
    not mirrored this cycle.
    """
    registry = get_registry()

    # Respect the rotation gate here as well as in the cron tier. The tier is the
    # cheap outer gate (paused crons cost nothing); this is the correctness one,
    # since a manual or misconfigured trigger must not dispatch off-shift.
    shift = await registry.resolve_shift()
    tiers = rotation.tier_states(shift)
    if not tiers.get(rotation.TIER_ON_SHIFT, True):
        return CycleResult(skipped_reason="off shift — on_shift tier is disarmed")

    # Say WHY nothing happened when no source is configured. Every caller otherwise
    # has to infer it from `polled == 0`, and the two conclusions are opposites:
    # "nothing is wrong" versus "nothing is watching". The dashboard derived this
    # itself, but an agent hitting POST /dispatch on a fresh install got a silent
    # empty result — the first thing a new user does, and the one moment the app most
    # needs to admit it is not set up yet.
    if not registry.configured_signal_sources():
        return CycleResult(
            skipped_reason=(
                "No signal source is configured, so nothing is being watched. "
                "Connect one in Settings → Providers."
            )
        )

    signals, errors = await registry.poll_all()
    firing = [s for s in signals if s.state == STATE_FIRING]

    index = store.read_index()
    # A signal is "owned" only by an OPEN incident. A closed one (resolved/escalated) must
    # not suppress a fresh firing — `signal.id` is stable for the alarm's lifetime, so
    # treating terminal as owned means the app permanently stops responding to any failure
    # it has already handled once, and the compounding-memory fast path (which can only pay
    # off on a SECOND occurrence) becomes unreachable.
    #
    # This is a CHEAP PRE-FILTER in front of `store.claim`, and fixing `claim` alone was not
    # enough: this line discarded the recurrence before `claim` ever saw it. Caught only by
    # driving a real gateway — 408 unit tests passed because they call `claim` directly and
    # never go through `run_cycle`'s filter. Two places encoded the same rule; both needed
    # it. `stale` stays claimable for its own reason (re-pickup in place).
    owned = {
        inc.signal.id
        for inc in index.values()
        if inc.status != STATUS_STALE and inc.status not in TERMINAL_STATUSES
    }
    candidates = [s for s in firing if s.id not in owned]

    limit = (
        max_claims
        if max_claims is not None
        else _config_int(_CONFIG_MAX_CLAIMS, DEFAULT_MAX_CLAIMS_PER_CYCLE)
    )

    claimed: list[ClaimedIncident] = []
    for signal in candidates[:limit]:
        result = await asyncio.to_thread(_claim_one, signal)
        if result is not None:
            # Gather evidence HERE, on the credentialed gateway, rather than letting
            # the investigating agent fetch it — the agent has no AWS credentials and
            # deliberately gets none (see ``investigation_brief``). Bounded by the
            # budget and redacted at the registry chokepoint. Failure is non-fatal: an
            # investigation with no evidence is worse than one with, but far better
            # than a claim we drop because a provider was slow.
            result.evidence = await gather_evidence_safely(registry, signal)
            # Semantic recall from the (git-synced) ledger index. Off the event loop
            # because it touches SQLite/FAISS, and best-effort: a missing or broken
            # index must leave the fingerprint matches untouched.
            await asyncio.to_thread(_attach_similar_safely, result)
            claimed.append(result)

    released = await asyncio.to_thread(
        store.sweep_stale, _config_int(_CONFIG_STALE_AFTER, DEFAULT_STALE_AFTER_SECS)
    )

    # Mirror newly-claimed incidents onto the Slack pin board. After the claim, so
    # a Slack outage can never cost us a claim; each send is already failure-
    # tolerant internally.
    if claimed:
        await slack_out.publish_all([c.incident for c in claimed], slack_client)

    if claimed or released:
        sel().log_api_access(
            caller="core:ops-mission-control",
            operation="dispatch_cycle",
            outcome="success",
            resources=(
                f"claimed={[c.incident.incident_id for c in claimed]} " f"released={released}"
            ),
        )

    return CycleResult(
        claimed=claimed,
        released=released,
        polled=len(firing),
        unclaimed_remaining=max(0, len(candidates) - len(claimed)),
        errors=errors,
    )


async def gather_evidence_safely(registry: Any, signal: Signal) -> list[Evidence]:
    """Gather provider evidence, treating any fault as "no evidence".

    ``gather_evidence`` already isolates per-adapter failures and timeouts, so this
    only catches a fault in the fan-out itself. Kept separate so the claim loop reads
    as one line and the non-fatal intent is explicit.
    """
    try:
        return await registry.gather_evidence(signal, EvidenceBudget())
    except Exception:  # noqa: BLE001 — evidence is context, never a gate
        logger.exception("ops-mission-control: evidence gathering failed for %s", signal.id)
        return []


def _claim_one(signal: Signal) -> ClaimedIncident | None:
    """Claim one signal and attach its ledger context (runs off the event loop)."""
    mode = rotation.resolve_mode(signal)
    incident = store.claim(signal, operating_mode=mode)
    if incident is None:
        # Lost the race — normal, not an error. Another heartbeat owns it.
        return None
    try:
        return attach_ledger_matches(incident)
    except Exception:  # noqa: BLE001 — a ledger fault must not lose the claim
        logger.exception("ops-mission-control: ledger match failed for %s", incident.incident_id)
        return ClaimedIncident(incident=incident)


def investigation_brief(claimed: ClaimedIncident) -> str:
    """Render the context an investigating agent should start from.

    Kept here rather than in the SOP prompt so the *facts* are assembled
    deterministically and only the reasoning is left to the model — a prompt that
    asks an agent to go fetch its own context spends a turn on work Python
    already did.

    **Evidence is brokered, not delegated.** The investigating agent's sandbox has no
    AWS credentials, so it cannot read alarm history or logs itself — and the answer
    is NOT to give it any. The gateway already holds the operator's profile and
    already redacts every gathered body at a single chokepoint
    (``registry.gather_evidence``); handing the agent scoped, redacted *text* gives it
    what it needs to diagnose while keeping credentials in one place, which is what
    least-privilege guidance asks for. An agent with its own AWS profile would be a
    second credential holder whose reads nothing redacts and whose scope nothing
    bounds.

    So the flow is: gateway gathers (credentialed, bounded, redacted) → brief carries
    the text → agent reasons. Before this the brief carried no evidence at all, so an
    AWS investigation had signal metadata and ledger hints and nothing else.
    """
    inc = claimed.incident
    sig = inc.signal
    lines = [
        f"Incident {inc.incident_id} — {sig.title}",
        "",
        f"Source:      {sig.source}",
        f"Severity:    {sig.severity}",
        f"Resource:    {sig.resource or '—'}",
        f"Fired at:    {sig.fired_at}",
        f"Fingerprint: {sig.fingerprint}",
        f"Mode:        {inc.operating_mode}",
    ]
    if sig.url:
        lines.append(f"Provider:    {sig.url}")

    lines.append("")
    if not claimed.matches:
        lines.append(
            "No prior pattern matched this fingerprint — this failure is new to the "
            "ledger. If you work out a reusable fix, record it so the next "
            "occurrence is cheap."
        )
    else:
        if claimed.fast_path:
            lines.append(
                "KNOWN PATTERN (verified, high confidence) — confirm it still "
                "applies, then propose this fix rather than re-deriving it:"
            )
        else:
            lines.append(
                "Possible prior patterns — treat these as hypotheses to test, not "
                "answers (none is both verified and high-confidence):"
            )
        for entry in claimed.matches:
            lines.append(
                f"  • [{entry.confidence}/{entry.trust}, used {entry.use_count}×] "
                f"{entry.pattern}"
            )
            lines.append(f"      fix: {entry.fix}")

    if claimed.similar:
        # Framed as leads, NOT patterns. These reached the brief by semantic similarity,
        # so their fingerprints do NOT match this signal — the wording has to stop the
        # agent applying one as though this failure had recurred, which is exactly the
        # mistake a ranked list invites.
        lines.append("")
        lines.append(
            "Related lessons from elsewhere in the ledger (semantic match — the "
            "fingerprints do NOT match this signal, so treat each as a lead worth "
            "checking, never as a fix to apply):"
        )
        for entry in claimed.similar:
            lines.append(f"  • [{entry.confidence}/{entry.trust}] {entry.pattern}")
            lines.append(f"      fix: {entry.fix}")

    # The no-credentials statement is UNCONDITIONAL, and deliberately so. It used to
    # live only inside the ``if claimed.evidence`` branch below, which meant the one
    # case that most needs it — no evidence gathered — was the one case that never got
    # it. An agent handed an AWS incident and no explanation reasonably assumes it
    # should go look itself, and then spends its whole turn re-running
    # ``aws … --profile …`` against a credential chain it cannot reach (observed on
    # INV-1/INV-2: repeated NoCredentials, no diagnosis). Saying it once, always, costs
    # two lines and removes a guaranteed dead end.
    lines.append("")
    lines.append(
        "Credentials: you have NONE for the systems in this incident, by design. The "
        "gateway holds the operator's profile and brokers reads to you already "
        "redacted, so it stays the single credential holder. Do not run `aws`, "
        "`datadog`, or any provider CLI — it will fail, and a failure loop is not a "
        "diagnosis. If a read you need is missing, say which one and why; an operator "
        "configures it as an evidence source rather than handing you a profile."
    )

    if claimed.evidence:
        lines.append("")
        lines.append("Provider evidence, already gathered for you (redacted):")
        # Bounded, and SAY SO when truncating: an agent that silently receives half a
        # log dump will reason confidently about a partial picture, which is worse
        # than knowing the view is clipped.
        spent = 0
        for item in claimed.evidence:
            if spent >= MAX_BRIEF_EVIDENCE_CHARS:
                lines.append("")
                lines.append(
                    f"  (evidence truncated at {MAX_BRIEF_EVIDENCE_CHARS} chars — "
                    "further items omitted; narrow the configured log groups if you "
                    "need to see them)"
                )
                break
            body = item.body[:MAX_BRIEF_EVIDENCE_ITEM_CHARS]
            clipped = len(item.body) > len(body)
            lines.append("")
            lines.append(f"  --- {item.title} ({item.source}/{item.kind}) ---")
            for line in body.splitlines():
                if line.strip():
                    lines.append(f"      {line}")
            if clipped:
                lines.append("      (item truncated)")
            spent += len(body)

    lines.append("")
    lines.append(
        "Authority reminder: only 'act' mode may execute a provider write, and "
        "only where a user rule grants it. Never run a remediation command "
        "against infrastructure — diagnose and propose; the human applies."
    )
    return "\n".join(lines)
