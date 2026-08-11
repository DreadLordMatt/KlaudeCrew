"""Fork (KlaudeCrew): the per-session ``settings.local.json`` permission seed.

claude-agent-acp resolves its own permission policy from the Claude Code
settings stack unless told otherwise. Seeding
``<work_dir>/.claude/settings.local.json`` with ``permissions.defaultMode:
"default"`` is what makes it ask KiroCrew instead of deciding locally --
every tool call then round-trips through ``session/request_permission``,
which is how a claude-backed session participates in the same approve /
trust_reads / trust / yolo protocol as kiro-cli (dashboard approval UI,
subagent trust levels, cron auto-approval, and
``HooksConfig.auto_deny_tools`` in ``hooks.py`` all depend on that round
trip happening at all).

Bound onto ``AcpClient`` as ``_write_claude_local_settings`` by
``klaude/registry.py``. Called zero-arg, self-bound, on the primary spawn
path and again on the model-substitution retry path (``acp/client.py``
around lines 2306 and 2810) -- both call sites already exist upstream,
guarded by ``getattr`` so the public core stays unaffected when this
package isn't registered.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.atomic_write import atomic_write

_SETTINGS_RELATIVE_PATH = Path(".claude") / "settings.local.json"


def _build_settings() -> dict:
    # NOTE: some claude-agent-acp builds reportedly need an additional
    # `availableModels` allowlist entry to unlock the 1M-token context
    # window on models that support it. The code that would have known its
    # exact key/shape was deleted upstream along with the standalone
    # provider (see docs/system-specs/features/claude-code-provider.md) and
    # isn't otherwise documented in this tree, so it can't be reproduced
    # here without verifying it against a live adapter. Ship permission
    # routing only until confirmed -- this stays the one place to extend if
    # the finding lands, i.e. `{**base, "availableModels": [...]}`.
    return {"permissions": {"defaultMode": "default"}}


def write_claude_local_settings(self) -> None:
    """Write ``<work_dir>/.claude/settings.local.json``. Bound onto AcpClient.

    Lets OSError propagate -- both call sites in ``acp/client.py`` already
    catch ``(OSError, ValueError, TypeError)`` around this call and log a
    warning, so duplicating that here would just double the noise.
    """
    work_dir = Path(self._work_dir)
    target = work_dir / _SETTINGS_RELATIVE_PATH
    atomic_write(target, json.dumps(_build_settings(), indent=2))
