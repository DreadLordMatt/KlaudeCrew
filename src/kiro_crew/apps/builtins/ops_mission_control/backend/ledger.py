"""Ops Mission Control — knowledge ledger.

The compounding-memory mechanism. When an investigation figures out why something
broke and what fixed it, that becomes a ``LedgerEntry`` keyed by the signal's
fingerprint. The next time a signal with the same fingerprint fires, the
investigation starts from the answer instead of rediscovering it.

This is the part the source team singled out as the reason to templatize their
workflow: "institutional memory that actually compounds… the repo already has fix
patterns that would've taken a new oncall engineer hours to rediscover."

Three design choices carry the weight:

**Append-only JSONL.** Never rewritten in place except by the hygiene pass. A
crashed writer can truncate at most the last line, and history is auditable.

**Content-addressed ids.** ``entry_id = sha256(pattern + fix)``. Two engineers who
independently learn the same lesson produce the same id, so a git merge of two
ledgers is a dedupe rather than a conflict — which is what makes optional
team sync viable without a server.

**Confidence decay.** An entry that stops being useful loses confidence rather
than lingering forever at "high". A ledger that only ever accumulates becomes
noise, and noise is what killed the tribal-knowledge approach this replaces.

See ``docs/task-specs/2026/07/ops-mission-control/spec.md`` §3.3, §7.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    CONFIDENCE_DECAY,
    CONFIDENCE_HIGH,
    TRUST_VERIFIED,
    LedgerEntry,
    utc_now_iso,
)
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

APP_NAME = "ops-mission-control"

_LEDGER_FILENAME = "ledger.jsonl"

#: Cap on ledger size. Beyond this the hygiene pass prunes lowest-value entries
#: (least used, weakest confidence, oldest). Bounded because the ledger is read
#: into a model prompt on every investigation — an unbounded ledger silently
#: turns into an unbounded context cost.
MAX_LEDGER_ENTRIES = 500

#: Matches returned to one investigation. Small on purpose: the point is the
#: two or three patterns most likely to be the answer, not a reading list.
MAX_MATCHES_PER_SIGNAL = 3

#: Days without a use before confidence decays one step.
DECAY_AFTER_DAYS = 90

#: A verified, high-confidence match is the "known-pattern fast path" — the
#: investigation can propose its fix directly instead of re-deriving it.
FAST_PATH_CONFIDENCE = CONFIDENCE_HIGH
FAST_PATH_TRUST = TRUST_VERIFIED


def ledger_path() -> Path:
    return app_data_dir(APP_NAME) / _LEDGER_FILENAME


def read_entries() -> list[LedgerEntry]:
    """All ledger entries, reconciled by id. A malformed line is skipped, never fatal.

    **Duplicate ids are merged on read, because a git merge produces them.** The whole
    argument for content-addressed ids on an append-only JSONL file is that two people
    who learn the same lesson write the same id, so merging two ledgers is a dedupe
    rather than a conflict — but git resolves that as *both lines present*. Appending
    every line meant one shared lesson counted twice: ``stats()`` inflated, ``match()``
    returned the same entry twice, and the handover digest listed one pattern as two.

    Reconciled the same way ``upsert`` merges (fingerprints union, strongest confidence
    and trust, highest use count), so a read after a merge agrees with what a local
    upsert of the same two entries would have produced. First occurrence keeps its
    position, so ordering stays stable for callers that rank by it.
    """
    path = ledger_path()
    if not path.exists():
        return []
    ordered: list[str] = []
    by_id: dict[str, LedgerEntry] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = LedgerEntry.from_dict(json.loads(line))
                except (json.JSONDecodeError, TypeError, ValueError):
                    logger.warning(
                        "ops-mission-control: skipping malformed ledger line %d", line_no
                    )
                    continue
                prior = by_id.get(entry.entry_id)
                if prior is None:
                    ordered.append(entry.entry_id)
                    by_id[entry.entry_id] = entry
                else:
                    by_id[entry.entry_id] = _reconcile(prior, entry)
    except OSError:
        logger.exception("ops-mission-control: failed to read ledger")
        return []
    return [by_id[eid] for eid in ordered]


def _reconcile(prior: LedgerEntry, other: LedgerEntry) -> LedgerEntry:
    """Merge two records of the same content-addressed entry.

    Mirrors ``upsert``'s algebra deliberately: learning a lesson again must never
    *weaken* what is known, so confidence and trust take the strongest of the two and
    ``use_count`` the highest. Keeping ``max`` on use_count rather than summing is the
    conservative choice — two branches that each recorded the same 3 uses did not
    between them see 6 occurrences.
    """
    order = list(CONFIDENCE_DECAY.keys())  # high, medium, low

    def _rank(value: str) -> int:
        return order.index(value) if value in order else len(order)

    prior.fingerprints = list(dict.fromkeys([*prior.fingerprints, *other.fingerprints]))
    prior.confidence = min((prior.confidence, other.confidence), key=_rank)
    prior.trust = TRUST_VERIFIED if TRUST_VERIFIED in {prior.trust, other.trust} else prior.trust
    prior.use_count = max(prior.use_count, other.use_count)
    prior.last_used = max(prior.last_used, other.last_used)
    prior.first_seen = min(
        (x for x in (prior.first_seen, other.first_seen) if x), default=prior.first_seen
    )
    return prior


def _write_all(entries: list[LedgerEntry]) -> None:
    """Rewrite the whole ledger. Only the hygiene pass should call this."""
    payload = "".join(json.dumps(entry.to_dict(), sort_keys=True) + "\n" for entry in entries)
    atomic_write(ledger_path(), payload)


def _append(entry: LedgerEntry) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")


def upsert(entry: LedgerEntry) -> LedgerEntry:
    """Add ``entry``, or merge it into the existing entry with the same id.

    Because ids are content-addressed, "the same lesson learned twice" merges:
    fingerprints union, use_count carries forward, and the stronger confidence and
    trust win. Learning a lesson again should never *weaken* what we know.
    """
    existing = {e.entry_id: e for e in read_entries()}
    prior = existing.get(entry.entry_id)
    if prior is None:
        _append(entry)
        return entry

    merged_fps = list(dict.fromkeys([*prior.fingerprints, *entry.fingerprints]))
    order = list(CONFIDENCE_DECAY.keys())  # high, medium, low
    best_confidence = min(
        (prior.confidence, entry.confidence),
        key=lambda c: order.index(c) if c in order else len(order),
    )
    prior.fingerprints = merged_fps
    prior.confidence = best_confidence
    prior.trust = TRUST_VERIFIED if TRUST_VERIFIED in {prior.trust, entry.trust} else prior.trust
    prior.last_used = utc_now_iso()
    existing[entry.entry_id] = prior
    _write_all(list(existing.values()))
    return prior


def match(fingerprint: str, *, limit: int = MAX_MATCHES_PER_SIGNAL) -> list[LedgerEntry]:
    """Entries that have previously matched this fingerprint.

    Ranked by trust, then confidence, then use count — a verified pattern used
    six times outranks an observed one seen once, which is the ordering an
    investigation wants when deciding whether to trust the fast path.
    """
    if not fingerprint:
        return []
    order = list(CONFIDENCE_DECAY.keys())
    candidates = [e for e in read_entries() if fingerprint in e.fingerprints]
    candidates.sort(
        key=lambda e: (
            0 if e.trust == TRUST_VERIFIED else 1,
            order.index(e.confidence) if e.confidence in order else len(order),
            -e.use_count,
        )
    )
    return candidates[:limit]


def is_fast_path(entries: list[LedgerEntry]) -> bool:
    """True when a match is trustworthy enough to propose its fix directly.

    Requires BOTH verified trust and high confidence. Proposing a remembered fix
    for a production failure on weaker evidence than that is how a knowledge base
    starts doing harm.
    """
    return any(e.trust == FAST_PATH_TRUST and e.confidence == FAST_PATH_CONFIDENCE for e in entries)


def record_use(entry_id: str, fingerprint: str = "") -> LedgerEntry | None:
    """Mark an entry as used, optionally binding a new fingerprint to it.

    Binding lets a pattern generalize: the same root cause surfacing through a
    differently-worded alarm gets attached to the entry that already knows the
    fix.
    """
    entries = read_entries()
    changed = False
    hit: LedgerEntry | None = None
    for entry in entries:
        if entry.entry_id != entry_id:
            continue
        entry.use_count += 1
        entry.last_used = utc_now_iso()
        if fingerprint and fingerprint not in entry.fingerprints:
            entry.fingerprints.append(fingerprint)
        hit = entry
        changed = True
        break
    if changed:
        _write_all(entries)
    return hit


def remove(entry_id: str) -> bool:
    entries = read_entries()
    remaining = [e for e in entries if e.entry_id != entry_id]
    if len(remaining) == len(entries):
        return False
    _write_all(remaining)
    return True


def find_contradictions(entries: list[LedgerEntry] | None = None) -> list[dict[str, Any]]:
    """Entry pairs that claim DIFFERENT fixes for the SAME failure fingerprint.

    The source workflow's consolidation SOP asks a leader to "resolve contradictions", and
    ours asks the same of the hygiene agent. But finding them was left entirely to the
    model's eye across the whole ledger — an O(n²) scan over text, which is exactly the
    mechanical work that should not cost model turns and is exactly the kind a model skims
    once the ledger is more than a screenful.

    So this DETECTS and does not decide. Two entries sharing a fingerprint with different
    fixes usually means the failure has more than one cause, and the right answer is to
    split the pattern descriptions so each is distinguishable — a judgement call about what
    the two causes actually are, which needs the model. Deleting one would silently discard
    a real, working fix.

    Ordered most-proven-first (by combined use count) so a responder reviewing a long list
    sees the pairs that are actively misleading people before the speculative ones.
    """
    rows = entries if entries is not None else read_entries()
    by_fingerprint: dict[str, list[LedgerEntry]] = {}
    for entry in rows:
        for fingerprint in entry.fingerprints:
            by_fingerprint.setdefault(fingerprint, []).append(entry)

    seen_pairs: set[tuple[str, str]] = set()
    found: list[dict[str, Any]] = []
    for fingerprint, group in by_fingerprint.items():
        if len(group) < 2:
            continue
        for i, left in enumerate(group):
            for right in group[i + 1 :]:
                # Same fix reached by two entries is not a contradiction — that is the
                # duplicate case dedupe already merges by content-addressed id.
                if left.fix.strip() == right.fix.strip():
                    continue
                # Explicit 2-tuple: `tuple(sorted(...))` widens to tuple[str, ...] and
                # loses the arity the set's type declares.
                first, second = sorted((left.entry_id, right.entry_id))
                key = (first, second)
                if key in seen_pairs:
                    # Two entries can share more than one fingerprint; report the pair
                    # once rather than once per shared fingerprint.
                    continue
                seen_pairs.add(key)
                found.append(
                    {
                        "fingerprint": fingerprint,
                        "entries": [left.to_dict(), right.to_dict()],
                        "uses": left.use_count + right.use_count,
                    }
                )
    found.sort(key=lambda row: (-int(row["uses"]), str(row["fingerprint"])))
    return found


def hygiene(*, now: datetime | None = None) -> dict[str, int]:
    """Dedupe, decay unused confidence, and prune. Runs on the ``primary`` tier.

    Returns a summary of what changed so the SOP can stay silent when the answer
    is "nothing" — silence-by-default applies to maintenance jobs too.
    """
    current = now or datetime.now(timezone.utc)
    entries = read_entries()
    before = len(entries)

    # Dedupe by content-addressed id, merging fingerprints and keeping the
    # highest use_count. Duplicates arrive via git-synced ledgers.
    merged: dict[str, LedgerEntry] = {}
    for entry in entries:
        seen = merged.get(entry.entry_id)
        if seen is None:
            merged[entry.entry_id] = entry
            continue
        seen.fingerprints = list(dict.fromkeys([*seen.fingerprints, *entry.fingerprints]))
        seen.use_count = max(seen.use_count, entry.use_count)
        if entry.trust == TRUST_VERIFIED:
            seen.trust = TRUST_VERIFIED
        if entry.last_used > seen.last_used:
            seen.last_used = entry.last_used
    deduped = list(merged.values())
    dupes_removed = before - len(deduped)

    # Decay confidence for entries unused past the window.
    cutoff = current - timedelta(days=DECAY_AFTER_DAYS)
    decayed = 0
    for entry in deduped:
        try:
            last = datetime.strptime(entry.last_used, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (TypeError, ValueError):
            continue
        if last >= cutoff:
            continue
        weaker = CONFIDENCE_DECAY.get(entry.confidence, entry.confidence)
        if weaker != entry.confidence:
            entry.confidence = weaker
            decayed += 1

    # Prune to the cap, dropping least-valuable first.
    order = list(CONFIDENCE_DECAY.keys())
    deduped.sort(
        key=lambda e: (
            -e.use_count,
            0 if e.trust == TRUST_VERIFIED else 1,
            order.index(e.confidence) if e.confidence in order else len(order),
            e.last_used,
        )
    )
    pruned = max(0, len(deduped) - MAX_LEDGER_ENTRIES)
    kept = deduped[:MAX_LEDGER_ENTRIES]

    if dupes_removed or decayed or pruned:
        _write_all(kept)

    return {
        "before": before,
        "after": len(kept),
        "deduped": dupes_removed,
        "decayed": decayed,
        "pruned": pruned,
        # Detected, never auto-resolved: splitting a pattern needs to know what the two
        # causes ARE. Counted here so the hygiene SOP can jump straight to the pairs
        # instead of re-scanning the ledger by eye, and so a rising count is visible.
        "contradictions": len(find_contradictions(kept)),
    }


def stats() -> dict[str, int]:
    entries = read_entries()
    return {
        "total": len(entries),
        "verified": sum(1 for e in entries if e.trust == TRUST_VERIFIED),
        "high_confidence": sum(1 for e in entries if e.confidence == CONFIDENCE_HIGH),
        "total_uses": sum(e.use_count for e in entries),
    }
