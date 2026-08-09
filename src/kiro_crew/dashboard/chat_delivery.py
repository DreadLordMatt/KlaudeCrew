"""Delivery of a user message into a chat slot, independent of the caller.

``POST /api/chat`` (a browser typing) and the session-control surface (one chat
session driving another) must put a message into a slot the SAME way, because
the interesting cases are all in the bookkeeping: a mid-turn steer has to be
registered before the RPC suspends, the transcript segment has to be cut at the
steer boundary, and a steer the live client refuses must fall through to the
queue rather than vanish. Two copies of that would drift, and the drift would
be invisible until a message was silently dropped.

The helpers here own that bookkeeping and nothing else — no HTTP, no request
parsing, no response shaping — so both call sites can layer their own
authorization and response format on top.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_utils import _redact_for_display
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

# Outcomes of :func:`steer_into_running_turn`.
#
# The two middle values split the case where the optimistic registration
# vanished during the steer RPC. Both mean "do NOT queue it again", but they are
# opposite answers to "did the message survive": the turn's teardown REQUEUED it
# (it is in the slot's queue and will run), while a hard stop DISCARDED it along
# with the turn. Collapsing them would report a thrown-away message as pending.
STEER_STEERED = "steered"
STEER_REQUEUED = "requeued"
STEER_DISCARDED = "discarded"
STEER_UNAVAILABLE = "unavailable"


def sanitize_outbound(text: str) -> str:
    """Return *text* with credentials and exfiltration URLs stripped.

    The single sanitization chain every delivery path uses before a message is
    persisted or broadcast: raw content must never reach an external surface.
    """
    sanitized, _ = redact_exfiltration_urls(text)
    sanitized, _ = redact_credentials(sanitized)
    return sanitized


def _row_has_delivery_id(slot: Any, delivery_id: str) -> bool:
    """Whether a durable row already carries *delivery_id* in its meta.

    The drain unions every consumed queue entry's meta onto the row it appends, so
    this is true exactly when the requeue-then-drain path already persisted this
    delivery — including when the row merged several queued messages together, where
    no content comparison would match.
    """
    for m in reversed(slot.messages):
        meta = m.get("meta")
        if not isinstance(meta, dict):
            continue
        if meta.get("steer_delivery_id") == delivery_id:
            return True
        # A merged row names every delivery it stands for, so membership — not
        # equality — is the question once the drain has folded messages together.
        many = meta.get("steer_delivery_ids")
        if isinstance(many, list) and delivery_id in many:
            return True
    return False


def _queue_matches(slot: Any, message: str) -> int:
    """How many queue entries currently carry exactly *message*."""
    return sum(1 for item in slot._queue if item.get("content") == message)


def _log_stop_race(slot: Any, stop_gen: int, *, preserved: bool) -> None:
    """Record a steer that raced a stop, and which way it resolved."""
    logger.info(
        "steer for slot %s raced a stop (generation %d -> %d); message %s",
        slot.key,
        stop_gen,
        int(getattr(slot, "_stop_generation", 0) or 0),
        "preserved" if preserved else "discarded",
    )


async def steer_into_running_turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
    *,
    meta_extra: dict[str, Any] | None = None,
) -> str:
    """Inject *message* into the slot's RUNNING turn; return a ``STEER_*`` outcome.

    Requires a live, steer-capable inner ACP client that the turn published on
    the slot. Fire-and-forget by design: the inline steer card materializes when
    kiro-cli echoes ``steering_consumed``.

    ``meta_extra`` is merged into the persisted message's ``meta`` so a caller
    can attribute the steer (for example, which session sent it) without this
    helper knowing about provenance.
    """
    client = getattr(slot, "_acp_client", None)
    if client is None or not getattr(client, "supports_steer", False):
        return STEER_UNAVAILABLE

    # Register as pending BEFORE the await: ``steer()`` suspends on
    # ``stdin.drain()``, and if the turn's finally runs during that suspension
    # it must already see this steer to requeue it (an append after the await
    # would land on an idle slot and orphan the message). The force-stop
    # ``clear()`` races correctly for the same reason: a hard kill during the
    # await discards the entry, so a late write cannot resurrect it.
    # Captured BEFORE the await. ``_stop_generation`` counts stop INITIATIONS and
    # is never reset by turn teardown, so it detects a Stop that fired AND
    # resolved while ``steer()`` was suspended — re-reading ``_stop_state`` after
    # the await would miss exactly that window.
    stop_gen = int(getattr(slot, "_stop_generation", 0) or 0)

    # AT MOST ONE pending steer per distinct text, enforced here at entry.
    #
    # `_pending_steers` holds plain strings and every consumer of it matches by
    # CONTENT — the turn teardown requeues by content, the queue comparison below
    # matches by content. So with two identical entries in flight, no amount of
    # counting downstream can say WHOSE entry survived: if another caller's copy is
    # consumed while ours is refused, the count falls back exactly as it would if
    # ours had gone, and we would persist a refused message as delivered and then
    # let the teardown requeue it — the same text twice.
    #
    # Rather than try to resolve an ambiguous signal, remove the ambiguity: refuse
    # the second identical steer. Nothing is lost, because `STEER_UNAVAILABLE`
    # sends the caller down the queue path. And since a concurrent caller hits this
    # same guard, once our entry is appended no further identical entry can appear,
    # which is what makes every check after the await unambiguously about ours.
    if slot._pending_steers.count(message):
        logger.info(
            "identical steer already pending for slot %s; queueing instead", slot.key
        )
        return STEER_UNAVAILABLE

    # The QUEUE still needs counting, because an unrelated earlier entry can carry
    # the same words and this guard says nothing about it: reading mere presence
    # there as "our requeue" would drop the transcript row for a message the turn
    # actually consumed.
    queued_before = _queue_matches(slot, message)
    # A real identity, not a content match. Every earlier attempt here compared
    # text, and text cannot survive the transitions: consumed, requeued, drained,
    # or merged into a larger row all look alike afterwards. The id is keyed by the
    # message only because the one-per-text guard above makes that key unique, and
    # it is handed to the requeue, which puts it on the queue entry; the drain then
    # unions entry meta onto the row it appends, so the id reaches the row even
    # through a merge.
    delivery_id = uuid.uuid4().hex
    slot._steer_delivery_ids[message] = delivery_id
    slot._pending_steers.append(message)
    try:
        steered = await client.steer(message)
    except Exception as exc:  # best-effort — the caller falls back to the queue
        logger.warning("steer failed for slot %s: %s", slot.key, exc)
        steered = False

    # ONE reconciliation for every path. The outcome turns on WHERE the text is
    # now, not on `steered`: the RPC returning True only means the client
    # accepted the write, and the turn it was written into may already have ended
    # during the await. A natural teardown is the case a `steered`-gated check
    # misses entirely — it requeues the pending steer without touching
    # `_stop_generation`, so reporting STEERED would let the caller persist a row
    # that the queue drain then appends a second time.
    # Our entry is the only possible match (see the one-per-text guard), so a
    # surviving match is unambiguously ours.
    if _row_has_delivery_id(slot, delivery_id):
        # The whole requeue-then-drain sequence completed while we were suspended,
        # so the row is already written and the only thing left to get wrong is
        # writing a second one. Checked first: it is the one signal that survives
        # every intermediate transition, including a merged row.
        slot._steer_delivery_ids.pop(message, None)
        logger.info(
            "steer for slot %s was requeued and drained during the RPC; row already "
            "persisted",
            slot.key,
        )
        return STEER_REQUEUED

    still_registered = bool(slot._pending_steers.count(message))
    queued = _queue_matches(slot, message) > queued_before
    stopped = int(getattr(slot, "_stop_generation", 0) or 0) != stop_gen

    if still_registered:
        if not steered:
            # Unwind the optimistic registration so a queue fallback cannot
            # double-deliver. Unambiguous by construction: the one-per-text guard
            # above means this is the only matching entry, which is why this is a
            # plain remove and not an index dance over possible duplicates.
            slot._pending_steers.remove(message)
            slot._steer_delivery_ids.pop(message, None)
            return STEER_UNAVAILABLE
        if stopped:
            # Still registered means the teardown has not run yet and will
            # requeue it, so the text still runs — the caller must NOT resend.
            _log_stop_race(slot, stop_gen, preserved=True)
            return STEER_REQUEUED
        # Delivered and live: fall through to cut the segment and persist the row.

    # Ours vanished during the await, so some consumer took it. Which one decides
    # whether the message still runs, and only the queue can tell them apart.
    if queued:
        # The turn's teardown moved it — a natural end or a soft stop. Either
        # way it gets its own queue card and the drain appends it, so persisting
        # a row here would duplicate it.
        if stopped:
            _log_stop_race(slot, stop_gen, preserved=True)
        return STEER_REQUEUED
    if stopped:
        # A hard kill clears `_pending_steers` alongside the queue: gone for good.
        _log_stop_race(slot, stop_gen, preserved=False)
        return STEER_DISCARDED
    if not steered:
        # NOT discarded. The entry is gone, nothing queued it, and no stop ran —
        # and the only thing that removes a registration in that state is the
        # running turn CONSUMING it. `steer()` writing successfully and then
        # raising on `stdin.drain()` lands exactly here, so trusting the exception
        # over the evidence would answer 409 for a message the target already has:
        # the caller resends and the target runs it twice.
        #
        # The asymmetry is deliberate. `STEER_DISCARDED` is the one outcome that
        # tells a caller to RESEND, so it must fire only when the text is provably
        # gone — which now means exactly one thing: absent from both lists with a
        # stop in flight (the hard-kill clear). Everywhere else the text is
        # accounted for by somebody, and a duplicate is worse than a stale error.
        logger.info(
            "steer RPC for slot %s failed but its registration was consumed; "
            "treating as delivered",
            slot.key,
        )
    # The entry is gone because the RUNNING turn consumed it — the
    # `steering_consumed` settle path removes it exactly as a requeue would, which
    # is why absence alone can never be read as loss. A real delivery, so it takes
    # the same persisting tail as the live case.

    ts = datetime.now(timezone.utc).isoformat()
    # Cut the in-flight text segment at the steer boundary BEFORE persisting the
    # user message, so the transcript reads [assistant(pre-steer), user(steer),
    # …] — the order the client rendered live. Without this the whole segment
    # lands BELOW the steer bubble at end-of-turn and the refresh visibly
    # reorders the reply. Best-effort: a cut failure must never lose the steer.
    cut = getattr(slot, "_steer_segment_cut", None)
    if cut is not None:
        try:
            cut()
        except Exception:
            logger.warning("steer segment cut failed for slot %s", slot.key, exc_info=True)

    sanitized = sanitize_outbound(message)
    meta: dict[str, Any] = {"steer": True}
    if meta_extra:
        meta.update(meta_extra)
    # Store the sanitized form — raw content must never reach an external
    # surface — so the steer survives a page reload via the dirty-flush cycle.
    slot.append("user", sanitized, "msg msg-u", ts=ts, meta=meta)
    state.broadcast_ws(
        "steer_push",
        {
            "slot": slot.key,
            "content": _redact_for_display(sanitized),
            "ts": ts,
        },
    )
    return STEER_STEERED


def queue_for_next_turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
    *,
    payload: str = "",
    meta: dict | None = None,
) -> str:
    """Append *message* to the slot's queue and announce it; return the queue id.

    The running turn's teardown drains the queue, so this is how a message
    reaches a busy slot when steering is unavailable or not asked for.

    ``payload`` marks the text's origin for the drain: a caller relaying machine
    speech must tag it, or the turn it eventually starts is mirrored to the
    target's linked channel as if the user had typed it. ``meta`` is applied to
    the transcript row the drain appends, so provenance a caller attaches at
    delivery time survives the wait.
    """
    qid = slot.queue_append(message, payload=payload, meta=meta)
    state.broadcast_ws(
        "queue_push",
        {
            "slot": slot.key,
            "content": _redact_for_display(sanitize_outbound(message)),
            "ts": datetime.now(timezone.utc).isoformat(),
            "queue_id": qid,
        },
    )
    return qid
