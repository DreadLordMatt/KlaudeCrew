"""Session cleanup watchdog — a minimal dispatcher for periodic cleanup hooks.

The session cleanup loop historically inlined several maintenance behaviours
(idle expiry, orphaned-MCP sweep, orphaned-PID sweep, deniedCommands
re-enforcement). This module introduces a thin container that the loop body
delegates to, so new cleanup behaviours (e.g. RSS-based recycling) can be
added as siblings rather than further inlining.

Design (CR 1 — wrap-only):
  * Each hook is a self-contained :class:`CleanupHook` — the *execution* half
    of a Command. Each hook carries its own try/except internally, identical
    to the inline behaviour it was lifted from, so :meth:`SessionWatchdog.tick`
    performs no error handling of its own beyond a debug-level backstop. This
    is deliberate: a blanket ``logger.exception`` here would *promote* the
    severity of failures that the original inline blocks swallowed silently,
    which would be an observable behaviour change rather than a faithful move.
  * The container is stateless and config-free.

This intentionally does NOT model the *decision* half (``evaluate() -> Verdict``).
The existing cleanup functions fuse find-and-act (e.g. idle expiry both selects
idle sessions and resets them in place), so splitting decision from execution
is a genuine refactor — deferred to CR 2, where each :class:`CleanupHook` grows
an ``evaluate()`` method and ``name`` is promoted from a logging identity into a
metrics dimension and dedupe key, evolving Command into Strategy.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupHook:
    """The execution half of one cleanup command (Command pattern).

    Attributes:
        name: Stable identity used for diagnostics today; CR 2 promotes this to
            a metrics dimension and verdict dedupe key.
        run: The async callable performing the cleanup. It MUST encapsulate its
            own error handling so the dispatcher can stay dumb (see module docs).
    """

    name: str
    run: Callable[[], Awaitable[None]]


class SessionWatchdog:
    """Sequentially dispatches a fixed list of :class:`CleanupHook` per tick.

    Stateless and config-free. ``tick`` runs hooks in registration order; a hook
    raising past its own guard is logged at debug level and does not prevent the
    remaining hooks from running.
    """

    def __init__(self, hooks: list[CleanupHook]) -> None:
        self._hooks = hooks

    async def tick(self) -> None:
        """Run every registered hook once, in order, isolating failures."""
        for hook in self._hooks:
            try:
                await hook.run()
            except Exception:
                # Backstop only. Each hook owns its real error handling; we log
                # at debug (never exception) so we don't promote the severity of
                # failures the lifted inline blocks intentionally swallowed.
                logger.debug(
                    "watchdog hook %s raised past its own guard",
                    hook.name,
                    exc_info=True,
                )
