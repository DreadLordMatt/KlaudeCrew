"""Fork (KlaudeCrew): claude backend session transcript lifecycle.

The Claude Agent SDK persists each session's own transcript under
``~/.claude/projects/<munged-cwd>/<session-id>.jsonl`` (or under
``CLAUDE_CONFIG_DIR`` when the fork's isolation env is set -- see
``config/loader.py``'s ``_acp()`` closure and ``KIROCREW_CC_ISOLATE``). This
module is the claude-side counterpart to two things upstream leaves as
kiro-only: ``providers/acp.py``'s ``cleanup_session()`` (deletes
``~/.kiro/sessions/cli/<sid>.{json,jsonl}`` on the kiro path today) and the
``session/load`` resume guard in ``acp/client.py`` (checks the kiro
transcript exists before attempting a resume, so a stale persisted session
id can't replay a dead transcript on top of a fresh system prompt).

CAVEAT: the exact project-directory munging rule is the Claude Code CLI's
own on-disk convention, not part of the public ACP protocol, and this fork
tree has no live adapter install to verify it against (see the fork plan's
Risks section). The transform implemented here -- replace each path
separator in the resolved cwd with ``-`` -- matches the publicly documented
convention. If it's ever wrong on some SDK version, the failure mode is
deliberately the SAFE direction everywhere this is consulted: a
not-found/unresolvable transcript is treated as "no prior transcript",
which is exactly what a genuinely fresh session already does (see
:func:`claude_transcript_exists`) -- never a false positive that would
either skip a real check or claim a transcript exists when it doesn't.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _claude_projects_root() -> Path:
    """Base directory the SDK stores session transcripts under.

    Honors ``CLAUDE_CONFIG_DIR`` -- the same isolation env the provider
    factory sets on the claude path (``config/loader.py``'s ``_acp()``) --
    so cleanup targets the same tree the running session actually wrote to.
    Falls back to the SDK's own default, ``~/.claude``, when unset (e.g.
    ``KIROCREW_CC_ISOLATE=0``, or a caller outside the factory).
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(override) if override else Path.home() / ".claude"
    return base / "projects"


def _munge_cwd(work_dir: Path | str) -> str:
    """Best-effort reproduction of the SDK's project-directory naming.

    See module docstring's CAVEAT.
    """
    resolved = str(Path(work_dir).resolve())
    return resolved.replace(os.sep, "-")


def claude_transcript_path(work_dir: Path | str, session_id: str) -> Path:
    return _claude_projects_root() / _munge_cwd(work_dir) / f"{session_id}.jsonl"


def claude_transcript_exists(work_dir: Path | str, session_id: str) -> bool:
    """Best-effort existence check for the ``session/load`` resume guard.

    Fails toward ``False`` ("no transcript") on any resolution error -- see
    module docstring CAVEAT: that's always the safe direction here, since it
    degrades to the same "fresh session/new" path a missing transcript
    already takes on the kiro side.
    """
    if not session_id:
        return False
    try:
        return claude_transcript_path(work_dir, session_id).exists()
    except OSError:
        return False


def delete_claude_transcript(work_dir: Path | str, session_id: str) -> None:
    """Delete a claude session's transcript file.

    Mirrors ``providers/acp.py``'s kiro-only ``cleanup_session()`` for the
    claude path -- same path-traversal guard, same fail-soft-on-OSError
    behavior.
    """
    if not session_id:
        return
    from kiro_crew.providers.cleanup import _is_safe_path

    root = _claude_projects_root()
    target = claude_transcript_path(work_dir, session_id)
    if not _is_safe_path(target, root):
        logger.error("claude transcript cleanup: path traversal blocked for %s", target)
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:
        logger.warning("claude transcript cleanup: failed to delete %s", target, exc_info=True)
