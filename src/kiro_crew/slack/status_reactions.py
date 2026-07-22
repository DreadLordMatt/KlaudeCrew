"""Split from slack/handler.py: status_reactions cluster."""

from __future__ import annotations

import asyncio
import logging
import re

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)


# Max chars of reasoning to surface inline in Slack before truncating. Keeps
# the 💭 Thinking block from becoming a wall of text (Mesh-1805); the full
# reasoning remains available in the dashboard Activity panel.
_THINKING_PREVIEW_LIMIT = 600


def _condense_thinking(mrkdwn: str, *, limit: int = _THINKING_PREVIEW_LIMIT) -> str:
    """Render reasoning as a subdued, truncated Slack blockquote.

    Keeps the reasoning visible but prevents a wall of text: truncates to
    ``limit`` chars on a whitespace boundary and renders each line as a
    blockquote so it appears indented/muted relative to the answer.

    Args:
        mrkdwn: Reasoning text, already converted to Slack mrkdwn and redacted.
        limit: Soft character cap before truncation.

    Returns:
        A Slack-mrkdwn string headed by ``💭 *Thinking*``.
    """
    text = mrkdwn.strip()
    truncated = False
    if len(text) > limit:
        # Break on the last whitespace (space, newline, tab) in the window so
        # reasoning whose only break is a newline still cuts cleanly instead of
        # falling through to the hard cut.
        boundaries = list(re.finditer(r"\s", text[:limit]))
        cut = (
            boundaries[-1].start() if boundaries and boundaries[-1].start() >= limit // 2 else limit
        )
        text = text[:cut].rstrip()
        truncated = True
    quoted = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in text.splitlines())
    suffix = "\n> _…full reasoning in dashboard Activity_" if truncated else ""
    return f"💭 *Thinking*\n{quoted}{suffix}"


# ── Phase-aware reaction constants ──────────────────────────────────────

_DEFAULT_PHASE_EMOJIS: dict[str, str] = {
    "queued": "eyes",
    "thinking": "thinking_face",
    "coding": "man_technologist",
    "browsing": "globe_with_meridians",
    "tool": "wrench",
    "done": "lobster",
    "error": "scream",
}


def _build_phase_emojis(
    overrides: dict[str, str | None] | None = None,
) -> tuple[dict[str, str | None], list[str]]:
    """Return ``(phase_emoji_dict, unknown_keys)`` with optional overrides applied.

    A phase value may be ``None`` to suppress that phase entirely (no emoji
    will be added or swapped in for it).  Stall emojis and transitions from
    other phases are unaffected.

    Unknown keys are collected and returned so callers can surface them
    to the user (e.g. startup warning) rather than silently dropping them.
    """
    result: dict[str, str | None] = dict(_DEFAULT_PHASE_EMOJIS)
    unknown: list[str] = []
    for key, value in (overrides or {}).items():
        if key in _DEFAULT_PHASE_EMOJIS:
            result[key] = value
        else:
            unknown.append(key)
    return result, unknown


try:
    _overrides = KiroCrewConfig.load().slack.reactions
except Exception:
    logger.warning("Failed to load reaction overrides from config; using defaults", exc_info=True)
    _overrides = {}
_PHASE_EMOJIS, _unknown_phases = _build_phase_emojis(_overrides)
del _overrides
if _unknown_phases:
    logger.warning(
        "Ignoring unknown slack.reactions keys: %s (valid: %s)",
        ", ".join(repr(k) for k in _unknown_phases),
        ", ".join(sorted(_DEFAULT_PHASE_EMOJIS)),
    )
del _unknown_phases


async def _add_phase_reaction(slack: SlackClientOps, channel: str, ts: str, phase: str) -> None:
    """Add the reaction for *phase* if the user hasn't suppressed it.

    Used by one-shot emoji-ack sites outside ``StatusReactionController``
    (e.g. ``!command`` handlers).  Honours ``slack.reactions`` ``null``
    suppression sentinels.
    """
    emoji = _PHASE_EMOJIS.get(phase)
    if emoji is None:
        return
    await slack.add_reaction(channel, ts, emoji)


_STALL_EMOJI_SOFT = "yawning_face"
_STALL_EMOJI_HARD = "fearful"

_STALL_SOFT_SECS = 15.0
_STALL_HARD_SECS = 45.0
_PHASE_DEBOUNCE_SECS = 0.7

_TERMINAL_PHASES = frozenset({"done", "error"})
_IMMEDIATE_PHASES = frozenset({"queued"})

_CODING_TOOLS: frozenset[str] = frozenset(
    {"Bash", "Write", "Edit", "Read", "Glob", "Grep", "NotebookEdit"}
)
_WEB_TOOLS: frozenset[str] = frozenset({"WebFetch", "WebSearch", "Browser"})

_CODING_KINDS: frozenset[str] = frozenset(t.lower() for t in _CODING_TOOLS)
_WEB_KINDS: frozenset[str] = frozenset(t.lower() for t in _WEB_TOOLS)


def _tool_to_phase(tool_name: str, tool_kind: str = "") -> str:
    """Map a tool name/kind to a reaction phase."""
    kind_lower = tool_kind.lower()
    if kind_lower:
        if kind_lower in _CODING_KINDS:
            return "coding"
        if kind_lower in _WEB_KINDS:
            return "browsing"
    # Extract base tool name for MCP tools (mcp__builder-mcp__Bash → Bash)
    base = tool_name.split("__")[-1] if "__" in tool_name else tool_name
    if base in _CODING_TOOLS:
        return "coding"
    if base in _WEB_TOOLS:
        return "browsing"
    return "tool"


class StatusReactionController:
    """Phase-aware Slack reaction controller with debounce and stall detection.

    Provides richer emoji feedback than the old binary eyes/lobster pair.
    Intermediate phases are debounced so rapid tool transitions don't spam
    the Slack API.  A stall watchdog adds yawning/fearful reactions when
    the agent appears stuck.
    """

    def __init__(
        self, slack: SlackClientOps, channel: str, ts: str, *, enabled: bool = True
    ) -> None:
        self._enabled = enabled
        self._slack = slack
        self._channel = channel
        self._ts = ts
        self._loop = asyncio.get_running_loop()

        self._current_emoji: str | None = None
        self._pending_phase: str | None = None
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._stall_soft_handle: asyncio.TimerHandle | None = None
        self._stall_hard_handle: asyncio.TimerHandle | None = None
        self._stall_emoji: str | None = None
        self._stall_paused = False
        self._finalized = False

    # ── public API ──────────────────────────────────────────────────

    def set_phase(self, phase: str) -> None:
        """Request a phase transition (may be debounced)."""
        if self._finalized or not self._enabled:
            return

        if phase in _TERMINAL_PHASES:
            self.finalize(error=(phase == "error"))
            return

        if phase in _IMMEDIATE_PHASES:
            self._cancel_debounce()
            emoji = _PHASE_EMOJIS.get(phase, phase)
            asyncio.ensure_future(self._swap_emoji(emoji))
            self._reset_stall_watchdog()
            return

        # Intermediate phase — debounce
        self._pending_phase = phase
        self._cancel_debounce()
        self._debounce_handle = self._loop.call_later(_PHASE_DEBOUNCE_SECS, self._fire_debounce)

    def on_progress(self) -> None:
        """Reset stall watchdog — call on any LLM/tool activity."""
        if not self._finalized and not self._stall_paused and self._enabled:
            self._reset_stall_watchdog()

    def pause_stall_watchdog(self) -> None:
        """Pause stall detection (e.g. waiting for user approval)."""
        self._stall_paused = True
        self._cancel_stall_timers()

    def resume_stall_watchdog(self) -> None:
        """Resume stall detection after a pause."""
        self._stall_paused = False
        if not self._finalized and self._enabled:
            self._reset_stall_watchdog()

    def finalize(self, error: bool = False) -> None:
        """Swap to terminal emoji. Idempotent."""
        if self._finalized or not self._enabled:
            return
        self._finalized = True
        self._cancel_debounce()
        self._cancel_stall_timers()
        # Clean up stall emoji before setting terminal
        asyncio.ensure_future(self._do_finalize(error))

    # ── internal ────────────────────────────────────────────────────

    async def _do_finalize(self, error: bool) -> None:
        if self._stall_emoji:
            try:
                await self._slack.remove_reaction(self._channel, self._ts, self._stall_emoji)
            except Exception:
                pass
            self._stall_emoji = None
        terminal = _PHASE_EMOJIS["error" if error else "done"]
        await self._swap_emoji(terminal)

    def _fire_debounce(self) -> None:
        """Timer callback — bridge to async."""
        asyncio.ensure_future(self._apply_pending())

    async def _apply_pending(self) -> None:
        if self._finalized or self._pending_phase is None:
            return
        emoji = _PHASE_EMOJIS.get(self._pending_phase, self._pending_phase)
        self._pending_phase = None
        await self._swap_emoji(emoji)
        self._reset_stall_watchdog()

    async def _swap_emoji(self, new_emoji: str | None) -> None:
        """Remove old reaction and add new one (skip if same).

        ``new_emoji=None`` means the phase is suppressed by config: remove
        any previously-applied reaction but do not add a replacement.
        """
        if new_emoji == self._current_emoji:
            return
        old = self._current_emoji
        self._current_emoji = new_emoji
        if old:
            try:
                await self._slack.remove_reaction(self._channel, self._ts, old)
            except Exception:
                pass
        if new_emoji is None:
            return
        try:
            await self._slack.add_reaction(self._channel, self._ts, new_emoji)
        except Exception:
            pass

    def _cancel_debounce(self) -> None:
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None

    def _cancel_stall_timers(self) -> None:
        if self._stall_soft_handle is not None:
            self._stall_soft_handle.cancel()
            self._stall_soft_handle = None
        if self._stall_hard_handle is not None:
            self._stall_hard_handle.cancel()
            self._stall_hard_handle = None

    def _reset_stall_watchdog(self) -> None:
        if not self._enabled:
            return
        self._cancel_stall_timers()
        # Remove existing stall emoji
        if self._stall_emoji:
            emoji_to_remove = self._stall_emoji
            self._stall_emoji = None
            asyncio.ensure_future(self._remove_stall_emoji(emoji_to_remove))
        if self._stall_paused or self._finalized:
            return
        self._stall_soft_handle = self._loop.call_later(_STALL_SOFT_SECS, self._on_stall_soft)
        self._stall_hard_handle = self._loop.call_later(_STALL_HARD_SECS, self._on_stall_hard)

    async def _remove_stall_emoji(self, emoji: str) -> None:
        try:
            await self._slack.remove_reaction(self._channel, self._ts, emoji)
        except Exception:
            pass

    def _on_stall_soft(self) -> None:
        asyncio.ensure_future(self._add_stall_emoji(_STALL_EMOJI_SOFT))

    def _on_stall_hard(self) -> None:
        asyncio.ensure_future(self._add_stall_emoji(_STALL_EMOJI_HARD))

    async def _add_stall_emoji(self, emoji: str) -> None:
        if self._finalized:
            return
        # Remove previous stall emoji if upgrading
        if self._stall_emoji and self._stall_emoji != emoji:
            try:
                await self._slack.remove_reaction(self._channel, self._ts, self._stall_emoji)
            except Exception:
                pass
        self._stall_emoji = emoji
        try:
            await self._slack.add_reaction(self._channel, self._ts, emoji)
        except Exception:
            pass
