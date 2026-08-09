"""Session control: letting one chat session drive another.

Three operations — send a message, stop the turn, read the transcript — plus the
authorization that decides whether a caller may address a target at all. The
operations are deliberately thin: they reuse the same delivery, stop and history
paths the dashboard itself uses, so a controlled session behaves exactly like
one a human is typing into.

Two properties shape the whole module.

**Delivery is a user turn, not an injection.** A message arrives on the target's
normal input path: steered into the running turn when there is one, queued when
steering is unavailable, or starting a fresh turn when the target is idle. There
is no side channel that bypasses the turn lifecycle, so stop, queue, context
accounting and the transcript all keep working as they do for a human.

**Provenance is never hidden.** The delivered text carries a one-line header
naming the sending session. A session that could type into another session
*as the human* would be an impersonation primitive; the receiving model has to
be able to tell the difference, and so does the person reading the transcript.

Authorization is deny-by-default and checked in one place
(:func:`authorize_target`) for all three operations, so a guard cannot be
present on one verb and missing on another.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_delivery import (
    STEER_DISCARDED,
    STEER_REQUEUED,
    STEER_STEERED,
    queue_for_next_turn,
    steer_into_running_turn,
)
from kiro_crew.dashboard.chat_persistence import _TRANSIENT_ROLES as _PERSISTENCE_TRANSIENT_ROLES
from kiro_crew.dashboard.chat_utils import (
    SESSION_CONTROL_MAX_HOPS,
    SESSION_CONTROL_PAYLOAD,
    slot_history_key,
)
from kiro_crew.dashboard.state import SESSION_CONTROL_END, SESSION_CONTROL_PREFIX
from kiro_crew.history import transcript_stem
from kiro_crew.security import (
    redact_and_truncate,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

# Longest message one session may deliver to another. Generous enough for a
# handoff briefing, small enough that a runaway loop cannot flood a peer's
# context window before the cooldown and depth caps below notice.
MAX_MESSAGE_CHARS = 8000

# Messages a single target may be holding from session control before further
# sends are refused. The queue is the pressure gauge: if the target is not
# draining, more text is not the answer.
MAX_QUEUE_DEPTH = 5

# Minimum gap between two sends to the SAME target. A tight-loop caller (an
# agent retrying, or two sessions talking to each other) hits this instead of
# filling the target's queue. Per-target rather than per-caller: the resource
# being protected is the target's attention.
SEND_COOLDOWN_SECS = 3.0

# How far a chain of session-control sends may travel. A -> B -> A is bounded by
# the per-target cooldown in RATE but not in TOTAL: each send to an idle peer
# starts a turn, whose agent may send back, and two sessions can trade messages
# until something else stops them. The hop count rides in the delivered message's
# meta and increments on each relay, so a loop terminates on its own instead of
# burning tokens unattended. A human typing into either session resets it, since
# their message carries no hop meta. Defined in ``chat_utils`` because the turn
# runner enforces the same bound when it requeues a relay it cannot measure.
MAX_HOPS = SESSION_CONTROL_MAX_HOPS

# Reads are cheap but not free — each one walks the target's in-memory window.
MAX_READ_MESSAGES = 100
DEFAULT_READ_MESSAGES = 20

# Per-message content cap for reads, so pulling a transcript tail cannot return
# a multi-megabyte tool payload verbatim.
MAX_READ_CONTENT_CHARS = 4000

# Slot-key prefixes for sessions no human is watching: a cron run's own slot
# (``cron-<job_id>``) and a background workflow's result slot
# (``workflow-<run_id>``, created by ``workflow_inject`` only when the
# originating tab is gone). They are refused as BOTH source and target. As a
# target, a message would start a fresh agent turn in a display-only slot nobody
# reads; as a source, a scheduled job would be able to type into the user's live
# conversations unattended. Notifications are the supported path for those
# (``send_message``), not session control.
UNATTENDED_SLOT_PREFIXES = ("cron-", "workflow-")

# Roles a read must not count, taken from the persistence layer's own list rather
# than restated here: those are exactly the rows rehydration DROPS, so any cursor
# that counted them would name a different position after a restart than before
# it. ``chunk`` runs are deleted when a segment flushes and ``done`` markers never
# persist at all, so counting either inflates ``total``, the list shrinks back
# under it, and the next ``since=next_since`` read skips the finished reply for good.
TRANSIENT_ROLES = _PERSISTENCE_TRANSIENT_ROLES

# target -> monotonic timestamp of the last accepted send. Pruned on every
# check (see :func:`_cooldown_remaining`) so a long-lived gateway that has
# addressed thousands of sessions does not retain a row per target forever.
_last_send: dict[str, float] = {}


def _cooldown_remaining(slot_key: str, now: float) -> float:
    """Seconds still owed before *slot_key* may receive another send.

    Prunes expired entries while it walks, which is what keeps the map bounded:
    an entry is only meaningful for ``SEND_COOLDOWN_SECS`` and is dead weight
    after that.
    """
    for key, ts in list(_last_send.items()):
        if now - ts >= SEND_COOLDOWN_SECS:
            _last_send.pop(key, None)
    last = _last_send.get(slot_key)
    if last is None:
        return 0.0
    return max(0.0, SEND_COOLDOWN_SECS - (now - last))


def _scrub(text: str) -> str:
    """Run both redaction scanners over *text* before it leaves this module.

    Everything this module emits is conversation content read off a live slot —
    another session's transcript tail, its title, a sender's title stamped onto a
    delivered message — so any of it can carry a credential a tool printed or a
    URL that would exfiltrate on render. Both scanners run here rather than at the
    caller, because this module is the boundary.
    """
    scrubbed, _ = redact_exfiltration_urls(text)
    scrubbed, _ = redact_credentials(scrubbed)
    return scrubbed


class SessionControlError(Exception):
    """A refusal carrying the HTTP status AND the machine-readable reason.

    ``code`` is the contract the dashboard and the MCP tools match on; ``message``
    is advisory English prose (RFC 9457 3.1.3). Prose alone would be
    untranslatable by construction, since callers render it verbatim.
    """

    def __init__(self, message: str, status: int = 400, code: str = "session_control_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def session_control_enabled() -> bool:
    """Whether the session-control surface is switched on in config.

    A config read that RAISES resolves to disabled, not to the field's default.
    ``load()`` can fail on a malformed section that has nothing to do with this
    feature, and treating that as "enabled" would let unrelated corruption
    silently undo an explicit ``session_control: false`` — the one switch
    standing between two of the user's sessions. Failing closed costs a
    refusal the user can diagnose from the log line; failing open costs the
    opt-out.
    """
    try:
        return bool(KiroCrewConfig.load().agent.session_control)
    except Exception:
        logger.warning(
            "session_control: config read failed — refusing until config loads", exc_info=True
        )
        return False


def caller_slot_key(state: "DashboardState", session_key: str) -> str:
    """Map a caller's session key to its slot key, or ``""`` when unknown.

    The MCP process authenticates as a session key (the history key), while
    every operation here is slot-keyed. Resolution walks the live slots and
    matches on the key each slot actually writes, which is the same identity
    ``list_sessions`` reports — so "who am I" cannot disagree between the two.

    An unresolvable caller is not fatal: it only means the self-send guard has
    nothing to compare against, which :func:`authorize_target` treats as a
    refusal rather than a pass.
    """
    if not session_key:
        return ""
    for slot in list(state._slots.values()):
        try:
            history_key = slot_history_key(slot)
            if session_key in (history_key, slot.key, transcript_stem(history_key)):
                return slot.key
        except Exception:
            continue
    return ""


def _resolve_slot(state: "DashboardState", target: str) -> "_ChatSlot | None":
    """Find the live slot *target* names: by slot key, transcript stem, or title.

    All three forms are things a caller actually holds. ``list_sessions`` reports
    FILENAME STEMS (``dashboard_chat-7``), not slot keys (``chat-7``), and the
    tool description tells callers to pass what it returned — so matching only
    ``slot.key`` refused the documented happy path with ``target_not_found``.
    Title matching covers what the caller sees on screen; it is exact and
    case-insensitive, and an ambiguous title is refused rather than guessed,
    because guessing here means delivering a message to the wrong conversation.
    """
    slot = state.get_slot(target)
    if slot is not None:
        return slot
    for candidate in list(state._slots.values()):
        try:
            if transcript_stem(slot_history_key(candidate)) == target:
                return candidate
        except Exception:
            continue
    wanted = target.strip().casefold()
    if not wanted:
        return None
    matches = [
        s
        for s in list(state._slots.values())
        if (s.display_title or "").strip().casefold() == wanted
    ]
    if len(matches) > 1:
        raise SessionControlError(
            f"{len(matches)} sessions share the title {target!r} — address it by its "
            "session key instead",
            status=409,
            code="ambiguous_target",
        )
    return matches[0] if matches else None


def authorize_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    operation: str,
) -> "_ChatSlot":
    """Resolve *target* and decide whether *caller* may act on it.

    Deny-by-default: every refusal raises :class:`SessionControlError` and is
    recorded in the SEL, so an attempt to reach a session that is out of bounds
    is visible after the fact even though nothing happened.
    """

    def deny(reason: str, code: str, status: int = 403) -> SessionControlError:
        sel().log_api_access(
            caller=f"session:{caller_session_key or 'unknown'}",
            operation=f"session_control.{operation}",
            outcome="denied",
            source="mcp",
            resources=f"target={target}:{code}",
            error=reason,
        )
        return SessionControlError(reason, status=status, code=code)

    if not session_control_enabled():
        raise deny(
            "session control is disabled in config (agent.session_control)",
            "session_control_disabled",
        )

    caller_key = caller_slot_key(state, caller_session_key)
    if not caller_key:
        # Without a resolved caller the self-send guard is blind, and a session
        # that can reach every peer while being unidentifiable is exactly the
        # shape this surface must not have.
        raise deny("caller session could not be identified", "caller_unidentified")
    if caller_key.startswith(UNATTENDED_SLOT_PREFIXES):
        raise deny(
            "unattended sessions (scheduled runs) cannot control other sessions",
            "unattended_caller",
        )

    try:
        slot = _resolve_slot(state, target)
    except SessionControlError as exc:
        raise deny(exc.message, exc.code, status=exc.status) from exc
    if slot is None:
        # 404 rather than 403: naming a session that is not open is a mistake,
        # not an authorization failure. Only sessions the dashboard currently
        # holds are addressable — a closed tab is out of scope, because waking
        # one would resurrect a conversation the user put away.
        raise deny(f"no open session matches {target!r}", "target_not_found", status=404)

    if slot.key == caller_key:
        raise deny("a session cannot control itself", "self_target")
    if slot.key.startswith(UNATTENDED_SLOT_PREFIXES):
        raise deny(
            "unattended sessions (scheduled runs) cannot be controlled", "unattended_target"
        )
    if getattr(slot, "memory_mode", "persistent") != "persistent":
        raise deny("incognito and temporary sessions are not addressable", "ephemeral_target")
    if getattr(slot, "_app", ""):
        raise deny("app-scoped sessions are not addressable", "app_scoped_target")
    if getattr(slot, "linked_session_key", ""):
        # A channel-linked session's conversation is mirrored to Slack/Telegram,
        # so reaching it crosses a surface boundary in both directions: a message
        # would surface to whoever reads that thread, and a read would pull the
        # channel's content back. That alone is reason enough to keep it out.
        #
        # It is also the one target whose STOP cannot be honoured: the stop path
        # addresses the session as ``dashboard:<slot>`` while a linked slot's turns
        # actually run under its ``linked_session_key``, so the cancel would miss
        # and the target would keep executing after a reported success. Refusing
        # is the honest answer until the stop path resolves the effective key.
        raise deny(
            "channel-linked sessions are not addressable", "linked_session_target"
        )

    # The caller's own isolation gates it too, and for the same reasons the
    # target's does: an incognito or temporary session is one the user asked to
    # leave no trace, and an app-scoped session belongs to its app. Either one
    # reaching a persistent peer would launder content across the boundary it
    # was created to have — in the direction the target-side checks cannot see.
    caller_slot = state.get_slot(caller_key)
    if caller_slot is None:
        raise deny("caller session is no longer open", "caller_gone")
    if getattr(caller_slot, "_app", ""):
        raise deny("app-scoped sessions cannot control other sessions", "app_scoped_caller")
    if getattr(caller_slot, "memory_mode", "persistent") != "persistent":
        raise deny(
            "incognito and temporary sessions cannot control other sessions",
            "ephemeral_caller",
        )
    if getattr(caller_slot, "linked_session_key", ""):
        # The exfiltration direction, and the reason this is not merely the
        # mirror of the target-side check: a linked caller's own conversation is
        # a channel thread, so anything it reads lands in front of whoever is in
        # that channel. `session_read_message` would hand a private dashboard
        # transcript to Slack/Discord readers who were never party to it.
        #
        # `CHANNEL_AGENT_BLOCKED_TOOLS` already blocks these tools for channel
        # AGENTS, but that guard keys on the agent identity; a linked SLOT is a
        # second route to the same surface and has to be closed on its own.
        raise deny(
            "channel-linked sessions cannot control other sessions",
            "linked_session_caller",
        )

    if getattr(slot, "workspace", "default") != getattr(caller_slot, "workspace", "default"):
        # Workspaces are the memory boundary; reaching across one would let a
        # session act on work it cannot see.
        raise deny("target session belongs to a different workspace", "workspace_mismatch")

    return slot


def _audit(
    *,
    caller_session_key: str,
    operation: str,
    slot_key: str,
    outcome: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record one completed session-control operation in the SEL.

    Logged as a tool invocation rather than an API access because that is what
    it is from the caller's side, and because it carries the per-call detail
    (how the message landed, whether a stop escalated) that makes the audit
    line answer "what actually happened to the other session".
    """
    sel().log_tool_invocation(
        session_key=caller_session_key,
        agent="",
        source="mcp",
        tool_name=f"session_{operation}",
        tool_kind="command",
        outcome=outcome,
        resources=f"target={slot_key}",
        metadata=dict(detail or {}, target=slot_key),
    )


def _inbound_hops(slot: "_ChatSlot | None") -> int:
    """How many session-control relays the CALLER's own turn is already deep.

    Read off the NEWEST user turn, not the newest marker anywhere in the
    transcript: the depth belongs to the message the caller is currently acting
    on. Stopping at the first user row is what makes a human typing reset the
    chain — their message carries no marker, so a real conversation after a relay
    starts counting again at zero instead of inheriting the old chain's depth.
    """
    if slot is None:
        return 0
    for msg in reversed(list(getattr(slot, "messages", []) or [])):
        if msg.get("role") != "user":
            continue
        meta = msg.get("meta") or {}
        marker = meta.get("session_control") if isinstance(meta, dict) else None
        if not isinstance(marker, dict):
            return 0
        try:
            return max(0, int(marker.get("hops", 1)))
        except (TypeError, ValueError):
            return 1
    return 0


def _neutralize_envelope_markers(text: str) -> tuple[str, int]:
    """Escape any envelope marker inside *text*; return the text and a hit count.

    The envelope is textual, so text that CONTAINS the terminator would close it
    early and everything after would read as unattributed user-role instruction —
    a sending session could forge input that looks like it came from the human.
    Escaping the opening bracket breaks the exact match while staying readable and
    losing nothing, which a reject-outright rule would not: a relay quoting an
    earlier relay is a normal case, and hop counting exists precisely because
    chains happen.
    """
    hits = 0
    for marker in (SESSION_CONTROL_END, SESSION_CONTROL_PREFIX):
        found = text.count(marker)
        if found:
            hits += found
            text = text.replace(marker, "\\" + marker)
    return text, hits


def _envelope_label(sender_title: str) -> str:
    """A single-line label that cannot terminate the quoted field it sits in.

    `_scrub` is redaction, not delimiter escaping, so a session TITLE is a second
    escape vector: `"]` plus a newline ends the header line and the rest of the
    title becomes top-level instruction. Quotes become typographic ones and every
    newline collapses, so the header stays one line whatever the title contains.
    """
    label = _scrub((sender_title or "another session").strip()) or "another session"
    label = label.replace('"', "\u201d").replace("]", ")")
    label = " ".join(label.split())
    return label[:120] or "another session"


def attributed_message(sender_title: str, message: str) -> str:
    """Wrap *message* in the registered session-control envelope.

    Provenance is part of the delivered CONTENT rather than display-only
    metadata: the receiving model reads content, and it is the party that most
    needs to know this turn came from a peer session instead of the human. The
    shape follows the injected-message convention (quoted label, explicit
    terminator) so the frontend classifies it from the shared prefix list and a
    reader can see where the peer's text ends.

    Both interpolated fields are neutralized first — the body cannot close the
    envelope and the label cannot close its own quoted field.
    """
    body, _ = _neutralize_envelope_markers(message)
    return f'{SESSION_CONTROL_PREFIX}{_envelope_label(sender_title)}"]\n{body}\n{SESSION_CONTROL_END}'


async def send_message(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    message: str,
    mode: str = "steer",
) -> dict[str, Any]:
    """Deliver *message* to *target* as a user turn. Returns how it landed.

    ``mode="steer"`` (default) interrupts a running turn so the target reacts
    now; ``mode="queue"`` always waits for the current turn to finish. Either
    way an idle target starts a turn immediately — the message is never left
    sitting in a queue nothing will drain.
    """
    if mode not in ("steer", "queue"):
        raise SessionControlError(
            f"mode must be 'steer' or 'queue', got {mode!r}", code="invalid_mode"
        )
    raw = (message or "").strip()
    if not raw:
        raise SessionControlError("message is required", code="message_required")
    if len(raw) > MAX_MESSAGE_CHARS:
        raise SessionControlError(
            f"message is {len(raw)} chars, over the {MAX_MESSAGE_CHARS} limit",
            code="message_too_long",
        )
    # Scrub HERE, not at the persist sites. The delivered text goes to the
    # target's PROVIDER — ``client.steer(text)`` and ``_run_chat(..., text)`` —
    # and a sender composing a handoff can quote a tool output that printed a
    # credential. Redacting only the copy written to the transcript would send
    # the raw secret to the model and keep the clean one on disk.
    body = _scrub(raw)

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="send",
    )

    now = time.monotonic()
    owed = _cooldown_remaining(slot.key, now)
    if owed > 0:
        raise SessionControlError(
            f"a message was sent to this session moments ago — wait {owed:.1f}s more "
            f"(minimum {SEND_COOLDOWN_SECS:.0f}s between sends to one session)",
            status=429,
            code="send_cooldown",
        )

    if len(slot._queue) >= MAX_QUEUE_DEPTH:
        raise SessionControlError(
            f"target already has {len(slot._queue)} messages waiting — let it catch up",
            status=429,
            code="target_backlogged",
        )

    caller_key = caller_slot_key(state, caller_session_key)
    caller_slot = state.get_slot(caller_key)
    sender_title = getattr(caller_slot, "display_title", "") if caller_slot else ""
    hops = _inbound_hops(caller_slot) + 1
    if hops > MAX_HOPS:
        raise SessionControlError(
            f"this handoff is already {hops - 1} sessions deep — the chain stops at "
            f"{MAX_HOPS} so two sessions cannot trade messages indefinitely. Report back "
            "to the person instead.",
            status=429,
            code="hop_budget_exhausted",
        )
    text = attributed_message(sender_title, body)
    attribution = {
        "session_control": {
            "from_slot": caller_key,
            "from_title": sender_title,
            "hops": hops,
        }
    }

    # RESERVE the cooldown here: past every refusal that can still reject this send,
    # and BEFORE the first await. Delivery suspends, so two sends that both passed
    # the check before either suspended would both steer — the target taking a burst
    # inside the very window the cooldown exists to prevent. Claiming the slot up
    # front closes that gap; the failure paths below release it again so a refused or
    # discarded send does not lock the target out for the rest of the window.
    _last_send[slot.key] = now
    if slot.running or slot._in_stage_execution:
        if mode == "steer":
            outcome = await steer_into_running_turn(state, slot, text, meta_extra=attribution)
            if outcome == STEER_DISCARDED:
                # A hard stop landed while the steer RPC was in flight and threw
                # the message away with the turn. Reporting that as "queued"
                # would tell the caller its handoff is pending when nothing
                # holds it, so it is a refusal the caller can retry.
                _audit(
                    caller_session_key=caller_session_key,
                    operation="send",
                    slot_key=slot.key,
                    outcome="discarded",
                )
                # Release the claim: nothing reached the target, so holding the
                # window would block the retry this error is telling them to make.
                _last_send.pop(slot.key, None)
                raise SessionControlError(
                    "the target's turn was force-stopped while the message was being "
                    "delivered, so it was discarded — resend it",
                    status=409,
                    code="delivery_discarded",
                )
            if outcome in (STEER_STEERED, STEER_REQUEUED):
                if state.get_slot(slot.key) is not slot:
                    # The target was closed or replaced while `steer()` was in
                    # flight, so the delivery landed on a slot no longer wired
                    # into state: the row we appended is on a detached object and
                    # the reply has nowhere to surface. Nothing is recoverable
                    # here, so report it as discarded rather than claim a handoff
                    # the user will never see. Identity, not `is None` — a
                    # same-key slot recreated during the await is a DIFFERENT
                    # conversation, and delivering into it would be worse than
                    # losing the message.
                    _audit(
                        caller_session_key=caller_session_key,
                        operation="send",
                        slot_key=slot.key,
                        outcome="discarded",
                        detail={"reason": "target_detached"},
                    )
                    # Release the claim — the slot it was keyed to is gone.
                    _last_send.pop(slot.key, None)
                    raise SessionControlError(
                        "the target session was closed while the message was being "
                        "delivered, so it was discarded — resend it",
                        status=409,
                        code="delivery_discarded",
                    )
                delivered = "steered" if outcome == STEER_STEERED else "queued"
                _audit(
                    caller_session_key=caller_session_key,
                    operation="send",
                    slot_key=slot.key,
                    outcome="allowed",
                    detail={"delivered": delivered},
                )
                return {"ok": True, "target": slot.key, "delivered": delivered}
        if state.get_slot(slot.key) is not slot:
            # Reached when a refused steer fell through to the queue and the target
            # was closed during that await: appending would put the message on a
            # queue nothing will ever drain. The steer-success branch above makes
            # the same check — this is the fallback path, which awaits just as much
            # and so needs it just as badly.
            _audit(
                caller_session_key=caller_session_key,
                operation="send",
                slot_key=slot.key,
                outcome="discarded",
                detail={"reason": "target_detached"},
            )
            _last_send.pop(slot.key, None)
            raise SessionControlError(
                "the target session was closed while the message was being "
                "delivered, so it was discarded — resend it",
                status=409,
                code="delivery_discarded",
            )
        queue_for_next_turn(
            state, slot, text, payload=SESSION_CONTROL_PAYLOAD, meta=attribution
        )
        _audit(
            caller_session_key=caller_session_key,
            operation="send",
            slot_key=slot.key,
            outcome="allowed",
            detail={"delivered": "queued"},
        )
        return {"ok": True, "target": slot.key, "delivered": "queued"}

    # Idle target: start a turn the same way an auto-nudge does. Imported here
    # because ``dashboard.chat`` reaches back into the gateway at import time.
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn

    slot.append("user", _scrub(text), "msg msg-u", meta=attribution)
    # ``_synthetic_payload`` is what keeps a relay off the target's linked
    # Slack/Telegram thread: the text is a peer agent's, and mirroring it as the
    # target user's speech would put words in their mouth on a surface other
    # people read. The queue path carries the same origin via its payload tag.
    task = spawn_guarded_turn(
        state, slot, _run_chat(state, slot, text, _synthetic_payload=True)
    )
    # Mirror the /api/chat send path so ``slot.running`` flips immediately and
    # the sidebar shows the turn as active.
    slot.task = task
    state.push_slots_update()
    _audit(
        caller_session_key=caller_session_key,
        operation="send",
        slot_key=slot.key,
        outcome="allowed",
        detail={"delivered": "started"},
    )
    return {"ok": True, "target": slot.key, "delivered": "started"}


async def stop_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    force: bool = False,
) -> dict[str, Any]:
    """Stop *target*'s in-flight turn, via the same path as the Stop button.

    A first call cancels cooperatively; calling again while that is pending
    escalates to a hard kill, so ``force`` is a request rather than a guarantee
    — the target's own stop state decides.
    """
    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="stop",
    )
    # Deferred: ``chat_handlers`` imports ``dashboard.chat`` transitively, which
    # reaches back into the gateway at import time — a module-scope import here
    # closes that cycle through ``handlers.session_control`` -> ``server``.
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    result = await stop_slot_turn(state, slot, force=force, source="session_control")
    _audit(
        caller_session_key=caller_session_key,
        operation="stop",
        slot_key=slot.key,
        outcome="allowed",
        detail={"force": force, "result": result.get("info", "stopping")},
    )
    return {"ok": True, "target": slot.key, **result}


def read_messages(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    limit: int = DEFAULT_READ_MESSAGES,
    since: int | None = None,
) -> dict[str, Any]:
    """Read *target*'s transcript tail plus enough state to poll it.

    ``next_since`` is the cursor to poll with; passing it back as ``since`` on the
    next call returns only what arrived in between, which is the whole
    send → wait → read loop. ``running`` says whether the target is still
    working, so a caller knows the difference between "nothing new yet" and
    "finished and idle".
    """
    if limit < 1 or limit > MAX_READ_MESSAGES:
        raise SessionControlError(
            f"limit must be between 1 and {MAX_READ_MESSAGES}", code="invalid_limit"
        )
    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="read",
    )

    # Indexes are ABSOLUTE positions in the session, not offsets into the live
    # window. A slot keeps only the most recent ``_MAX_SLOT_MESSAGES`` in memory
    # and credits each trimmed row to ``_disk_older_count``, so window length
    # stops growing once trimming starts. A cursor derived from that length
    # would freeze at the cap and never see another reply; adding the
    # frozen-prefix count makes it monotonic for the session's whole life.
    raw_window = list(slot.messages)
    base = int(getattr(slot, "_disk_older_count", 0) or 0)
    # Stop the cursor before the streaming tail (see ``TRANSIENT_ROLES``): those
    # rows are deleted when the segment flushes, so a cursor past them would sit
    # beyond the list that replaces them and never return the finished reply.
    messages = [m for m in raw_window if m.get("role") not in TRANSIENT_ROLES]
    durable_end = len(messages)
    total = base + durable_end
    if since is not None:
        if since < 0:
            raise SessionControlError("since must be >= 0", code="invalid_since")
        if base:
            # ``_disk_older_count`` counts every trimmed row, transient ones
            # included (persistence writes them and only skips them when reading
            # back), while the positions above are built over DURABLE rows only.
            # The two agree until a transient row is trimmed into the frozen
            # prefix — then `base` advances with no durable row behind it, every
            # position shifts, and a `since` read serves a durable message the
            # caller already had.
            #
            # An exact cursor needs a durable-only prefix count, which does not
            # exist yet and cannot be added from here: ``_disk_older_count`` has a
            # contract with the save model (it is the frozen prefix saves must not
            # rewrite) and is read by backfill, rewind and channel slots. So this
            # refuses loudly instead of quietly duplicating. Tail reads (no
            # ``since``) still work, and the window is 10,000 rows, so only a very
            # long-lived session reaches this at all. Tracked for the real fix.
            raise SessionControlError(
                "this session is long enough that older messages have been "
                "trimmed, and cursor positions are no longer exact — read without "
                "`since` to get the latest messages",
                status=409,
                code="cursor_unavailable",
            )
        # `base` is 0 from here on — the guard above refused every trimmed
        # session — so the absolute position and the window offset coincide and
        # the old "your cursor aged out" gap report cannot arise any more.
        start = min(since, total)
        offset = start
    else:
        # A tail read is still served on a trimmed session (only `since` reads are
        # refused), so the two spaces come apart here: slice the in-memory window
        # by OFFSET, but report the index in ABSOLUTE terms so the number still
        # means "position in the session". Conflating them returned an empty
        # window, because `total` counts the frozen prefix the list does not hold.
        offset = max(0, durable_end - limit)
        start = base + offset
    window = messages[offset:][:limit]

    out: list[dict[str, Any]] = []
    for offset, msg in enumerate(window):
        content = str(msg.get("content", "") or "")
        # ``redact_and_truncate`` scans the COMPLETE text before slicing. Cutting
        # first would split a credential straddling the boundary into a prefix
        # that no longer matches the scanner, so the fragment would ship.
        emitted = redact_and_truncate(content, MAX_READ_CONTENT_CHARS)
        row: dict[str, Any] = {
            "index": start + offset,
            "role": str(msg.get("role", "") or ""),
            "content": emitted,
            "ts": str(msg.get("ts", "") or ""),
        }
        if len(content) > MAX_READ_CONTENT_CHARS:
            row["truncated"] = True
        out.append(row)

    _audit(
        caller_session_key=caller_session_key,
        operation="read",
        slot_key=slot.key,
        outcome="allowed",
        detail={"returned": len(out)},
    )
    return {
        "ok": True,
        "target": slot.key,
        "title": _scrub(slot.display_title),
        # Busy means "more output is coming", which is exactly what a poller needs
        # to decide whether to wait. `slot.running` alone is not that: during a
        # multi-stage plan each stage's `_run_chat` closes its own turn, so it
        # briefly reads False BETWEEN stages and a poller would conclude the work
        # had finished and stop before the later stages produced anything. The
        # send path at the top of this module already treats the orchestrating
        # flag as busy for the same reason; reporting had to agree with it.
        "running": bool(slot.running or getattr(slot, "_in_stage_execution", False)),
        # True when the target is mid-reply: rows exist that the cursor
        # deliberately does not cover yet, so "nothing new" here does not mean
        # "nothing happening".
        **({"streaming": True} if durable_end < len(raw_window) else {}),
        "queue_depth": len(slot._queue),
        "total": total,
        # The cursor to poll with next. This is NOT `total`: when more than
        # `limit` rows are new, the window stops short of the end, and a caller
        # that polled `since=total` would jump the gap and never see the rows in
        # between. `next_since` is the absolute position just past the last row
        # actually returned, so consecutive polls cover every row exactly once.
        # `total` stays in the response as the backlog depth — the difference
        # from `next_since` is how far behind the caller still is.
        #
        # Omitted once rows have been trimmed, because positions stop being exact
        # there (see the `cursor_unavailable` refusal above). Handing back a
        # cursor that the next call would reject is worse than saying it is gone,
        # so callers get `cursor_exact: false` and fall back to tail reads.
        **(
            {"next_since": start + len(out)}
            if not base
            else {"cursor_exact": False}
        ),
        "messages": out,
    }
