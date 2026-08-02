"""Ops Mission Control — incident store and dispatch index.

The dispatch index (``incidents/index.json``) is the claim ledger: it is what
makes a firing signal *owned* rather than merely visible. Two properties matter
and are enforced here rather than by convention:

**Claims are atomic.** Two heartbeats (two KiroCrew instances, or an overlapping
tick) must never both claim the same signal and spawn two investigations of one
incident. ``claim`` takes an exclusive file lock and does a compare-and-set, so
exactly one caller wins and the loser skips.

**Transitions are legal or refused.** An incident cannot jump from ``unclaimed``
straight to ``resolved`` — that would let the board assert work was done that no
investigation ever ran. ``models.LEGAL_TRANSITIONS`` is the whole grammar and
``transition`` is the only door.

File locking goes through ``platform_compat`` (never raw ``fcntl``) because this
app must work on Windows, where that module does not exist.

See ``docs/task-specs/2026/07/ops-mission-control/spec.md`` §3, §4.5.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    CLAIMED_BY_HEARTBEAT,
    LEGAL_TRANSITIONS,
    OPEN_STATUSES,
    OPEN_VERIFICATIONS,
    STATUS_DISPATCHED,
    STATUS_INVESTIGATING,
    STATUS_NEEDS_HUMAN,
    STATUS_STALE,
    TERMINAL_STATUSES,
    VALID_CLAIMANTS,
    VERIFY_NOT_CHECKABLE,
    VERIFY_STILL_FIRING,
    Incident,
    Signal,
    utc_now_iso,
)
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

APP_NAME = "ops-mission-control"

_INCIDENTS_DIRNAME = "incidents"
_INDEX_FILENAME = "index.json"
_LOCK_FILENAME = ".index.lock"

#: Incident id prefix. ``INV`` (investigation) rather than ``INC`` so it reads
#: distinctly from provider-native incident numbers in a Slack thread title.
_ID_PREFIX = "INV"

#: Directory mode for the incident tree. Not secret (no tokens live here — see
#: ``secrets.py``), but incident logs describe a user's production failures, so
#: owner-only is the right default.
_DIR_MODE = 0o700

#: Statuses the stale sweep can release. An incident that has been claimed but is
#: not progressing holds its signal unworked, and the failure is silent — so every
#: pre-terminal state is sweepable, including ``needs_human``.
#:
#: ``needs_human`` was previously excluded, which made the app's quietest failure:
#: ``LEGAL_TRANSITIONS`` legalises ``needs_human -> stale`` *specifically* so "an
#: incident nobody ever answers must not pin a signal as claimed forever"
#: (``models.py``), and ``dispatch.py`` counts every non-stale non-terminal incident
#: as owning its signal — so an unanswered question meant the alarm was never
#: re-claimed, on machinery that looked deliberate. The edge existed, was unit-tested
#: as a legal move, and was never traversed.
_SWEEPABLE_STATUSES: frozenset[str] = frozenset(
    {STATUS_DISPATCHED, STATUS_INVESTIGATING, STATUS_NEEDS_HUMAN}
)

#: How much longer than a working incident a ``needs_human`` one may sit before the
#: sweep releases it. 6× the working threshold (so 12h at the 2h default) — an
#: unanswered question should outlive one sleep cycle, because releasing it re-claims
#: the alarm and discards the investigation's context.
DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER = 6

#: File mode for a rendered postmortem. ``incidents_dir()`` is already 0o700 for the
#: reason stated on ``_DIR_MODE``, but the file inside it inherited the umask, which on a
#: shared machine can mean group- or world-readable. The whole point of the artifact is
#: that a human decides who receives it, so the filesystem must not decide first.
_LOG_FILE_MODE = 0o600


def _redacted(text: str) -> str:
    """Run ``text`` through both redaction passes before it lands in an artifact.

    BOTH are required, and which one is not obvious — so it is stated here rather than
    left to be rediscovered. Core ``security.redact`` catches recognizable vendor
    credentials and exfiltration URLs but leaves a bare-hex Datadog key and a
    prefix-less ``Bearer`` token untouched; the app's ``secrets.redact_tokens`` covers
    exactly those provider shapes. ``registry.gather_evidence`` composes them the same
    way, and that is deliberate symmetry: the two places provider text leaves this app
    must not sanitize to different standards.

    Imported inside the function, matching ``registry.gather_evidence``: ``store`` is on
    the hot claim path and imported by every route, while ``security`` is a large module
    and the postmortem renderer runs at most once per incident close.
    """
    if not text:
        return text
    from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import redact_tokens
    from kiro_crew.security import redact as core_redact

    return redact_tokens(core_redact(text))


def incidents_dir() -> Path:
    d = app_data_dir(APP_NAME) / _INCIDENTS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    platform_compat.chmod_safe(d, _DIR_MODE)
    return d


def index_path() -> Path:
    return incidents_dir() / _INDEX_FILENAME


def incident_log_path(incident_id: str) -> Path:
    """Markdown investigation log for one incident.

    ``incident_id`` is generated by this module (``INV-<n>``), never
    caller-supplied, but it lands in a filesystem path — so it is validated
    anyway rather than trusted, keeping the guarantee local to this function.
    """
    safe = "".join(c for c in incident_id if c.isalnum() or c in "-_")
    if not safe or safe != incident_id:
        raise ValueError(f"unsafe incident id: {incident_id!r}")
    return incidents_dir() / f"{safe}.md"


# ---------------------------------------------------------------------------
# Index I/O
# ---------------------------------------------------------------------------


def _read_index_unlocked() -> dict[str, Incident]:
    try:
        raw = json.loads(index_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Incident] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            try:
                out[str(key)] = Incident.from_dict(value)
            except (TypeError, ValueError):
                logger.warning("ops-mission-control: skipping malformed index entry %r", key)
    return out


def _write_index_unlocked(index: dict[str, Incident]) -> None:
    payload = {key: inc.to_dict() for key, inc in index.items()}
    atomic_write(index_path(), json.dumps(payload, indent=2, sort_keys=True))


class _IndexLock:
    """Exclusive lock around a read-modify-write of the dispatch index.

    Routed through ``platform_compat.file_lock`` so the same code works on
    Windows, where ``fcntl`` does not exist.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def __enter__(self) -> _IndexLock:
        lock_file = incidents_dir() / _LOCK_FILENAME
        self._fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        platform_compat.acquire_lock(self._fd, exclusive=True)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                platform_compat.release_lock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


def read_index() -> dict[str, Incident]:
    """Snapshot of the dispatch index. No lock — readers tolerate staleness."""
    return _read_index_unlocked()


def get_incident(incident_id: str) -> Incident | None:
    return read_index().get(incident_id)


def find_by_signal(signal_id: str) -> Incident | None:
    """Locate an incident by its originating signal id.

    The heartbeat uses this to decide "is this already claimed?", so it matches on
    ``Signal.id`` (provider-scoped and stable across polls) rather than on the
    incident id.
    """
    for inc in read_index().values():
        if inc.signal.id == signal_id:
            return inc
    return None


def _next_incident_id(index: dict[str, Incident]) -> str:
    highest = 0
    for key in index:
        if key.startswith(f"{_ID_PREFIX}-"):
            try:
                highest = max(highest, int(key.split("-", 1)[1]))
            except (IndexError, ValueError):
                continue
    return f"{_ID_PREFIX}-{highest + 1}"


# ---------------------------------------------------------------------------
# Claim + transition
# ---------------------------------------------------------------------------


def claim(
    signal: Signal, *, operating_mode: str, claimed_by: str = CLAIMED_BY_HEARTBEAT
) -> Incident | None:
    """Atomically claim ``signal``, returning the new incident or ``None``.

    ``None`` means someone else already owns this signal — the caller must skip
    it, NOT retry. This is the compare-and-set that prevents two heartbeats from
    spawning duplicate investigations of one alarm.

    A signal whose previous incident went ``stale`` is re-claimable: the stale
    incident is re-dispatched in place, keeping one timeline per signal rather
    than accumulating a new incident per re-pickup.

    **A signal whose previous incident reached a TERMINAL status is also
    re-claimable — as a NEW incident.** ``signal.id`` is stable for the life of the
    underlying alarm (``cloudwatch:alarm/DlqDepth`` forever), so treating "terminal"
    as "accounted for" meant the app permanently stopped responding to any failure it
    had already handled once. Proven: resolve an alarm on day 1, and its re-firing on
    days 2, 3, and 30 all returned ``None``. Worse, it made the app's whole premise
    unreachable — the compounding-memory fast path can only pay off on a SECOND
    occurrence, and a second occurrence could never be claimed.

    A recurrence is a new incident, not a reopening: the first one has its own
    diagnosis, resolution, and Slack thread, and overwriting those would destroy the
    record that makes the ledger trustworthy. Re-firing after a real fix is exactly
    the signal a responder most needs to see ("we fixed this and it came back").
    """
    with _IndexLock():
        index = _read_index_unlocked()

        for inc in index.values():
            if inc.signal.id != signal.id:
                continue
            if inc.status == STATUS_STALE:
                inc.status = STATUS_DISPATCHED
                inc.operating_mode = operating_mode
                inc.updated_at = utc_now_iso()
                # Refresh the signal snapshot: severity or resource may have
                # moved since the original claim.
                inc.signal = signal
                _write_index_unlocked(index)
                return inc
            if inc.status in TERMINAL_STATUSES:
                # Closed. This firing is a recurrence — fall through and open a new
                # incident rather than blocking forever on a finished one.
                continue
            # An OPEN incident owns this signal (dispatched / investigating /
            # needs_human). Losing that race is normal, not an error.
            return None

        incident_id = _next_incident_id(index)
        now = utc_now_iso()
        incident = Incident(
            incident_id=incident_id,
            signal=signal,
            status=STATUS_DISPATCHED,
            operating_mode=operating_mode,
            claimed_at=now,
            # Coerced against the known set rather than trusted: this reaches the board
            # and the digest, and an unrecognized value would render as provenance.
            claimed_by=(claimed_by if claimed_by in VALID_CLAIMANTS else CLAIMED_BY_HEARTBEAT),
            updated_at=now,
        )
        index[incident_id] = incident
        _write_index_unlocked(index)
        return incident


def transition(incident_id: str, new_status: str, **updates: Any) -> Incident:
    """Move an incident to ``new_status``, refusing illegal transitions.

    Raises ``KeyError`` for an unknown incident and ``ValueError`` for a
    transition the grammar does not allow. Extra keyword arguments update
    matching ``Incident`` fields in the same locked write, so a caller never has
    to do a second read-modify-write to attach a diagnosis or a slot key.
    """
    with _IndexLock():
        index = _read_index_unlocked()
        incident = index.get(incident_id)
        if incident is None:
            raise KeyError(incident_id)

        if new_status != incident.status:
            allowed = LEGAL_TRANSITIONS.get(incident.status, frozenset())
            if new_status not in allowed:
                raise ValueError(
                    f"illegal transition {incident.status!r} -> {new_status!r} "
                    f"for {incident_id}"
                )
            incident.status = new_status

        for key, value in updates.items():
            if hasattr(incident, key):
                setattr(incident, key, value)
            else:
                logger.warning("ops-mission-control: ignoring unknown field %r", key)

        incident.updated_at = utc_now_iso()
        index[incident_id] = incident
        _write_index_unlocked(index)

    # The postmortem is rendered when an incident CLOSES, and this is the only door to a
    # terminal status (``sweep_stale`` writes only ``stale``, which is not terminal, and
    # ``slot_watch.reconcile``'s ``derive_status`` never returns one) — so one call here
    # covers every close there is.
    #
    # OUTSIDE the lock, deliberately. ``_IndexLock`` is the compare-and-set that every
    # ``claim`` contends on and claim cost is already superlinear in index size (see
    # MAX_CLOSED_INCIDENTS), so rendering and writing a file must not lengthen that
    # critical section. The index write above is already durable, which is also why a
    # failure here cannot be allowed to fail the transition.
    if incident.status in TERMINAL_STATUSES:
        _write_closing_postmortem(incident)
    return incident


def _write_closing_postmortem(incident: Incident) -> None:
    """Render the shareable artifact for a just-closed incident. Never fatal.

    Sourced from the PERSISTED record rather than from the transition's kwargs. A caller
    that closes an incident without re-sending its diagnosis (``update_fields``, a
    reconcile) would otherwise blank a good artifact with empty arguments — the record is
    the truth, the kwargs are only this request's delta.

    ``next_steps`` renders as ``_none_`` because no ``Incident`` field carries one:
    ``proposed_action`` exists but is never assigned by anything (see the gap report's
    §5.7). Leaving the section honestly empty rather than filling it with a paraphrase of
    the diagnosis — a postmortem that invents its own follow-ups is worse than one that
    admits it has none.

    An ``OSError`` here is logged and swallowed for the same reason ``_handle_transition``
    tolerates a failed Slack mirror: the state change is already durable, and a record of
    the change must never be able to fail the change itself.
    """
    try:
        write_log(
            incident,
            diagnosis=incident.diagnosis,
            actions=incident.resolution,
            next_steps="",
        )
    except (OSError, ValueError):
        logger.warning(
            "ops-mission-control: could not write the postmortem for %s",
            incident.incident_id,
            exc_info=True,
        )


def update_fields(incident_id: str, **updates: Any) -> Incident:
    """Update incident fields without changing status."""
    current = get_incident(incident_id)
    if current is None:
        raise KeyError(incident_id)
    return transition(incident_id, current.status, **updates)


def sweep_stale(stale_after_secs: int, needs_human_after_secs: int | None = None) -> list[str]:
    """Release incidents idle beyond their threshold for re-pickup.

    Without this, an investigation whose agent died leaves a signal permanently
    claimed and therefore permanently unworked — the failure mode is silent, which
    is why the sweep runs on every heartbeat rather than on demand.

    ``needs_human`` gets its OWN, longer threshold (defaulting to
    ``DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER`` × the working one). Waiting on a person is
    legitimately slower than an agent dying mid-investigation, so reusing the working
    threshold would yank a question out from under an operator who is simply asleep —
    and the point of sweeping it is to stop an *abandoned* question pinning a signal,
    not to punish a slow answer. Passing ``None`` keeps the two coupled by the
    multiplier; an explicit value lets the operator tune them independently.
    """
    from datetime import datetime, timezone

    if needs_human_after_secs is None:
        needs_human_after_secs = stale_after_secs * DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER

    released: list[str] = []
    now = datetime.now(timezone.utc)
    with _IndexLock():
        index = _read_index_unlocked()
        changed = False
        for incident_id, inc in index.items():
            if inc.status not in _SWEEPABLE_STATUSES:
                continue
            threshold = (
                needs_human_after_secs if inc.status == STATUS_NEEDS_HUMAN else stale_after_secs
            )
            try:
                seen = datetime.strptime(inc.updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except (TypeError, ValueError):
                continue
            if (now - seen).total_seconds() < threshold:
                continue
            if STATUS_STALE not in LEGAL_TRANSITIONS.get(inc.status, frozenset()):
                continue
            inc.status = STATUS_STALE
            inc.updated_at = utc_now_iso()
            released.append(incident_id)
            changed = True
        if changed:
            _write_index_unlocked(index)
    return released


def open_incidents() -> list[Incident]:
    """Incidents that represent live work, newest claim first."""
    return sorted(
        (inc for inc in read_index().values() if inc.status in OPEN_STATUSES),
        key=lambda i: i.claimed_at,
        reverse=True,
    )


def counts_by_status() -> dict[str, int]:
    counts: dict[str, int] = {}
    for inc in read_index().values():
        counts[inc.status] = counts.get(inc.status, 0) + 1
    return counts


#: Closed incidents kept in the dispatch index. Open ones are NEVER pruned regardless
#: of this cap — live work must not vanish because history is long.
#:
#: This exists because making a resolved alarm re-claimable (see ``claim``) removed the
#: accidental ceiling that "one incident per alarm, forever" used to provide. A genuinely
#: flapping alarm on the 2-minute dispatch cadence now mints a new incident per flap, and
#: every claim re-reads and re-writes the WHOLE index — measured superlinear: 50 entries
#: → 6ms/claim, 450 → 53ms. Left unbounded, a month of one flapping alarm projects to
#: ~21,600 incidents, and `/incidents` returns every one of them to the dashboard.
#:
#: 500 is chosen to be far above any real review window (a responder looks at this week,
#: not last quarter) while keeping claim cost flat and the board payload bounded. The
#: investigation LOGS are separate files and are deliberately left alone — pruning an
#: index row does not destroy the written record of what happened.
#:
#: That last sentence was written when no log could exist, and is now a real (small,
#: bounded, owner-only) disk cost: every close renders ``incidents/<id>.md``, so 25 flaps
#: of one alarm leave 25 files that pruning never removes. The decision stands anyway, and
#: is worth restating rather than quietly reversing — the artifact is the app's only output
#: a non-KiroCrew reader can be handed, so deleting it because the *index* got long would
#: destroy the record precisely when history is what someone is looking for. A postmortem
#: is a few kB; a year of a pathologically flapping alarm is single-digit MB.
MAX_CLOSED_INCIDENTS = 500


def prune_closed(*, keep: int = MAX_CLOSED_INCIDENTS) -> int:
    """Drop the oldest closed incidents beyond ``keep``. Returns how many were removed.

    Runs from the daily hygiene pass, not from ``claim``: pruning is maintenance, and
    doing it on the hot path would make an ordinary claim occasionally pay for a large
    rewrite. Ordered by ``updated_at`` (when it CLOSED) rather than ``claimed_at``, so a
    long-running incident that just finished is treated as recent.
    """
    with _IndexLock():
        index = _read_index_unlocked()
        closed = [inc for inc in index.values() if inc.status in TERMINAL_STATUSES]
        if len(closed) <= keep:
            return 0
        closed.sort(key=lambda i: (i.updated_at, i.incident_id), reverse=True)
        for inc in closed[keep:]:
            del index[inc.incident_id]
        removed = len(closed) - keep
        _write_index_unlocked(index)
    logger.info("ops-mission-control: pruned %d closed incident(s) from the index", removed)
    return removed


# ---------------------------------------------------------------------------
# Investigation log
# ---------------------------------------------------------------------------


def _verification_line(incident: Incident) -> str:
    """One sentence saying whether anything CHECKED that the action landed.

    In the postmortem because this is the artifact a colleague reads without access to
    the board, and "Actions taken: silenced the alarm" is the sentence most likely to be
    believed as an outcome. For the whole life of the renderer it would have said exactly
    that on the strength of a 2xx, with nothing in the file admitting that no code had
    looked again.

    Renders nothing at all when no action was taken — the overwhelming majority of
    incidents, and adding a "not applicable" line to every one of them would bury the
    cases where it matters.
    """
    if not incident.verification:
        return ""
    detail = _redacted(incident.verification_detail)
    if incident.verification == VERIFY_NOT_CHECKABLE:
        return (
            f"> **Verification:** the `{incident.last_action}` was accepted by the "
            f"provider, but this app cannot observe whether it took effect — an "
            f"acknowledgement leaves an alert firing by design, so the alarm's state "
            f"says nothing either way. Treat it as sent, not as confirmed."
        )
    if incident.verification in OPEN_VERIFICATIONS:
        return (
            f"> **Verification:** NOT CONFIRMED. The `{incident.last_action}` was "
            f"accepted by the provider and this incident closed before a re-read could "
            f"say whether the condition actually changed." + (f" {detail}" if detail else "")
        )
    if incident.verification == VERIFY_STILL_FIRING:
        return (
            f"> **Verification: the signal was STILL FIRING after this action.** "
            f"{detail or 'The provider accepted the request; the condition did not change.'}"
        )
    return f"> **Verification:** confirmed. {detail or 'The signal is no longer firing.'}"


def write_log(incident: Incident, *, diagnosis: str, actions: str, next_steps: str) -> Path:
    """Write the human-readable investigation log.

    Markdown on disk rather than only JSON because these logs are the artifact a
    human reads at 2am, and the only artifact a reader who does not run KiroCrew can
    be handed — attachable to a ticket, pasteable into a review. The structure mirrors
    the internal system's per-ticket investigation files.

    **Every field carrying provider or model text is redacted here.** For the whole
    life of this renderer it interpolated ``signal.title``, ``signal.resource``, the
    caller's ``diagnosis`` and its ``actions`` verbatim — provider alarm payloads and a
    model-authored narrative, neither of which had been through the chokepoint that
    every other egress path in this app uses. It went unnoticed because the function
    had no caller, so no file was ever produced to inspect. The cost is not cosmetic
    and not hypothetical: this is the one file an operator is *expected* to hand to
    somebody else, so a leaked credential in it is worse than having no postmortem at
    all — no artifact means an inconvenience, a poisoned artifact means a credential
    forwarded by hand, with the operator's own confidence behind it.

    Deliberately NOT redacted: ``incident_id`` (ours), ``severity`` /``status``/
    ``operating_mode`` (closed enums), ``fired_at`` (a timestamp) and ``fingerprint``
    (a 16-hex hash we compute). Passing those through the scanners is a verified no-op,
    and naming them here stops a later reader assuming they were forgotten.
    ``verification`` and ``last_action`` join that list for the same reason — both are
    closed enums we assign — while ``verification_detail`` DOES go through the redactor,
    because it quotes a provider's own poll-failure text.
    """
    path = incident_log_path(incident.incident_id)
    matched = "\n".join(f"- `{_redacted(m)}`" for m in incident.ledger_matches)
    verified = _verification_line(incident)
    body = f"""# {incident.incident_id} — {_redacted(incident.signal.title)}

| | |
|---|---|
| Signal | `{_redacted(incident.signal.id)}` |
| Source | {_redacted(incident.signal.source)} |
| Severity | {incident.signal.severity} |
| Resource | {_redacted(incident.signal.resource) or "—"} |
| Fired | {incident.signal.fired_at} |
| Status | {incident.status} |
| Mode | {incident.operating_mode} |
| Fingerprint | `{incident.signal.fingerprint}` |

## Diagnosis

{_redacted(diagnosis) or "_pending_"}

## Actions taken

{_redacted(actions) or "_none_"}

{verified}

## Next steps

{_redacted(next_steps) or "_none_"}

## Matched knowledge

{matched or "_no prior pattern matched_"}
"""
    # Owner-only, explicitly. The containing directory is already 0o700; without this
    # the file itself inherited the process umask.
    atomic_write(path, body, mode=_LOG_FILE_MODE)
    return path


def read_log(incident_id: str) -> str:
    try:
        return incident_log_path(incident_id).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, ValueError):
        return ""
