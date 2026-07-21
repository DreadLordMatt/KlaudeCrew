"""Auto-research shared constants, status enum, and pure helpers.

Leaf module (stdlib + config_dir only) — the base of the auto_research
dependency DAG. Every other submodule imports names from here.
"""

from __future__ import annotations

import re
from enum import Enum

from kiro_crew.config.paths import config_dir

# Resolve under the active KiroCrew home (honors KIROCREW_HOME for isolated dev
# gateways) — NOT a hardcoded ~/.kirocrew, which would make dev instances collide
# with prod Research Lab state and contend with the prod gateway's watchdog.
RESEARCH_DIR = config_dir() / "workspace" / "research"
DB_PATH = config_dir() / "apps" / "auto-research" / "campaigns.db"
MAX_CYCLES_HARD_CAP = 100
# Execution mode + recursive-exploration budget defaults (RL v2). The SQLite
# column DEFAULTs in _get_db() mirror these — keep them in sync.
VALID_EXECUTION_MODES = ("agent", "workflow")
DEFAULT_EXECUTION_MODE = "agent"
DEFAULT_MAX_SUBQUESTIONS_PER_ROUND = 3
DEFAULT_DEPTH_DECAY = 0.5
DEFAULT_RESERVE_FRACTION = 0.15
POLL_INTERVAL = 5
_MAX_PARALLEL_WORKERS = 5  # hard cap on parallel sub-agents per cycle
# Default seconds between cycles (until the next nudge fires). The watchdog's
# inactivity timeout is idle_secs * 2; the first cycle gets a longer startup
# grace (it can't produce anything until the first nudge + a full work turn).
DEFAULT_IDLE_SECS = 120
_FIRST_CYCLE_GRACE_SECS = 600
# Worker auto-approve is capped at 24h; past this the watchdog pauses the
# campaign to NEEDS_INPUT and it must be resumed (re-authorized) to continue.
_TRUST_TTL_SECS = 24 * 3600
_CAMPAIGN_ID_RE = re.compile(r"^[a-f0-9]{8}$")


def _unresponsive_deadline(idle_secs: int) -> int:
    """Idle seconds (no slot activity AND no new finding) before unresponsive.

    Generous floor: a deep research cycle can take minutes (web fetches +
    synthesis), so a tight idle_secs*2 window falsely fails healthy slow cycles.
    The watchdog also resets this timer whenever the worker slot is actively
    running a turn, so this only bounds genuine no-activity stalls.
    """
    return max(idle_secs * 2, _FIRST_CYCLE_GRACE_SECS)


class CampaignStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STAGNANT = "stagnant"
    NEEDS_INPUT = "needs_input"
    COMPLETE = "complete"
    FAILED = "failed"
    STOPPED = "stopped"


# Terminal statuses cannot transition to any other status.
_TERMINAL_STATUSES = (CampaignStatus.COMPLETE, CampaignStatus.STOPPED)

# Per-cycle trigger injected by the autonudge loop. The full methodology lives in
# the kirocrew-research agent's system prompt, so this only needs to name the cycle.
_RESEARCH_AGENT = "kirocrew-research"
_RESEARCH_NUDGE = (
    "Run the next research cycle for campaign {cid} "
    "(dir {dir}). Follow your per-cycle research "
    "protocol and end the turn when done."
)
