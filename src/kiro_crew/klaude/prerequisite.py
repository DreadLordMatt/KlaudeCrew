"""Fork (KlaudeCrew): boot-time readiness check for the claude ACP backend.

kiro-cli's own first-run gate (``kiro_prerequisite.py`` /
``dashboard/handlers/kiro_prerequisite.py``) is a heavier latched-state
machine built around kiro-cli's install/sign-in flow (``kiro-cli --version``
/ ``whoami`` subprocess probes, device-code sign-in copy, an SPA blocking
gate). None of that applies to the claude backend, and it isn't bypassed
here -- ``slack/gateway.py`` skips constructing it at all (via
``assume_kiro_ready``) when ``agent.acp_backend == "claude"``, since probing
kiro-cli's install state is meaningless when kiro-cli isn't the backend.

``AcpClient._resolve_claude_acp_bin()`` / ``_resolve_claude_code_executable()``
are cheap, synchronous PATH/filesystem lookups (no subprocess spawn), so they
don't need the same latch-and-poll machinery kiro-cli's version probe does.
This module is the much smaller equivalent for the claude path: log one loud
line per missing piece at boot, with the exact remediation, so an
unresolvable adapter or ``claude`` binary fails LOUD before the first chat
turn rather than surfacing only as an opaque ``AcpError`` deep in a session.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

CLAUDE_AGENT_ACP_INSTALL_HINT = "npm i -g @agentclientprotocol/claude-agent-acp"
CLAUDE_CODE_INSTALL_HINT = (
    "install Claude Code (https://docs.claude.com/en/docs/claude-code) and run "
    "`claude login`, or set CLAUDE_CODE_EXECUTABLE to an existing binary's path"
)


def check_claude_backend_ready() -> bool:
    """Best-effort boot check. Returns True iff both binaries resolve.

    Never raises -- a resolver failure here must not prevent boot (kiro-cli
    stays a live fallback via ``agent.acp_backend: "kiro"``, and other
    subsystems -- Slack, cron, the dashboard itself -- work regardless of
    whether chat can start a session).
    """
    from kiro_crew.acp.client import _resolve_claude_acp_bin, _resolve_claude_code_executable

    ok = True
    try:
        if not _resolve_claude_acp_bin():
            logger.critical(
                "claude ACP backend selected (agent.acp_backend=claude) but "
                "claude-agent-acp was not found. Install it: %s",
                CLAUDE_AGENT_ACP_INSTALL_HINT,
            )
            ok = False
    except Exception:
        logger.warning("claude-agent-acp resolution check failed", exc_info=True)
        ok = False
    try:
        if not _resolve_claude_code_executable():
            logger.critical(
                "claude ACP backend selected (agent.acp_backend=claude) but no "
                "`claude` executable was found. %s",
                CLAUDE_CODE_INSTALL_HINT,
            )
            ok = False
    except Exception:
        logger.warning("claude executable resolution check failed", exc_info=True)
        ok = False
    return ok
