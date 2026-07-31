"""GitHub Issues adapter — label-filtered issues as work items.

Authenticates through the ``gh`` CLI rather than asking for a token, following the
precedent already set by the Issue Radar builtin: ``gh`` is likely already
authenticated on a developer's machine, and reusing it means this app stores one
fewer credential. A machine without ``gh`` reports unconfigured.

The intended use is an ops label (``incident``, ``oncall``, ``sev2``) on a repo
that the team files operational work into — the generic equivalent of the internal
system's ticket queue, without assuming any particular tracker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    ACTION_COMMENT,
    ACTION_RESOLVE,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    STATE_FIRING,
    Signal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    config_list,
    config_value,
    provider_enabled,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    DEFAULT_POLL_LIMIT,
    ActionResult,
)
from kiro_crew.sandbox import resource_limit_preexec, sandboxed_spawn_argv

logger = logging.getLogger(__name__)

PROVIDER_ID = "github-issues"

_GH_BINARY = "gh"

#: Wall-clock cap for a ``gh`` invocation. Kept under the per-source poll timeout
#: so a hung CLI surfaces as a source error rather than stalling the heartbeat.
_GH_TIMEOUT_SECS = 12.0

#: Labels that mark an issue as critical rather than routine.
_CRITICAL_LABELS: frozenset[str] = frozenset(
    {"sev1", "sev-1", "p1", "critical", "outage", "incident"}
)


def _gh_available() -> bool:
    return shutil.which(_GH_BINARY) is not None


async def _run_gh(args: list[str]) -> tuple[int, str, str]:
    """Run ``gh`` with a timeout, returning ``(rc, stdout, stderr)``.

    Routed through ``sandboxed_spawn_argv`` (OS filesystem isolation +
    credential-scrubbed environment) and given a kernel resource ceiling via
    ``resource_limit_preexec``, because the repo, label set, and comment body all
    come from config an agent can influence — and ``gh`` reads the repo's own
    config on the way. This is the chokepoint ``test/test_spawn_audit.py``
    requires every agent-influenced spawn to use.

    Never raises for a non-zero exit — the caller decides whether that is a
    source-level error or an expected condition (e.g. a repo without issues).
    """
    argv, env, cleanup = sandboxed_spawn_argv([_GH_BINARY, *args])
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            preexec_fn=resource_limit_preexec(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GH_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 1, "", f"gh timed out after {_GH_TIMEOUT_SECS:.0f}s"
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)


class GitHubIssuesAdapter:
    """SignalSource + ActionSink over ``gh issue``."""

    id = PROVIDER_ID
    display_name = "GitHub Issues"
    detail = "Open issues carrying an ops label. Uses your authenticated gh CLI."
    config_fields: tuple[str, ...] = ("enabled", "repo", "labels")
    secret_fields: tuple[str, ...] = ()

    def configured(self) -> bool:
        return (
            provider_enabled(PROVIDER_ID)
            and bool(config_value(PROVIDER_ID, "repo"))
            and _gh_available()
        )

    # -- SignalSource ------------------------------------------------------

    async def poll(self) -> list[Signal]:
        if not self.configured():
            return []
        repo = config_value(PROVIDER_ID, "repo")
        labels = config_list(PROVIDER_ID, "labels")

        args = [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(DEFAULT_POLL_LIMIT),
            "--json",
            "number,title,labels,createdAt,url,assignees",
        ]
        for label in labels:
            args += ["--label", label]

        rc, stdout, stderr = await _run_gh(args)
        if rc != 0:
            raise RuntimeError(f"gh issue list failed: {stderr.strip()[:200]}")
        try:
            issues = json.loads(stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh returned malformed JSON: {exc}") from None
        if not isinstance(issues, list):
            return []

        signals: list[Signal] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            number = issue.get("number")
            if number is None:
                continue
            # An issue with a human assignee is already owned — claiming it would
            # duplicate their work. Same filter the internal dispatch applied.
            if issue.get("assignees"):
                continue
            label_names = [
                str(item.get("name", ""))
                for item in (issue.get("labels") or [])
                if isinstance(item, dict)
            ]
            severity = (
                SEVERITY_CRITICAL
                if any(name.lower() in _CRITICAL_LABELS for name in label_names)
                else SEVERITY_WARNING
            )
            signals.append(
                Signal.create(
                    source=PROVIDER_ID,
                    native_id=f"{repo}#{number}",
                    title=str(issue.get("title", "") or f"issue #{number}"),
                    severity=severity,
                    state=STATE_FIRING,
                    fired_at=str(issue.get("createdAt", "")),
                    resource=repo,
                    url=str(issue.get("url", "")),
                    labels={
                        "repo": repo,
                        "issue_number": str(number),
                        "gh_labels": ",".join(label_names),
                    },
                )
            )
        return signals

    # -- ActionSink --------------------------------------------------------

    def supported_actions(self) -> frozenset[str]:
        return frozenset({ACTION_RESOLVE, ACTION_COMMENT})

    async def execute(self, signal: Signal, action: str, payload: dict[str, Any]) -> ActionResult:
        if not self.configured():
            return ActionResult(ok=False, action=action, error="github-issues is not configured")
        repo = signal.labels.get("repo", "")
        number = signal.labels.get("issue_number", "")
        if not repo or not number:
            return ActionResult(ok=False, action=action, error="signal carries no issue reference")

        note = str(payload.get("note", ""))[:4000]
        if action == ACTION_COMMENT:
            args = ["issue", "comment", number, "--repo", repo, "--body", note]
        else:
            args = ["issue", "close", number, "--repo", repo]
            if note:
                args += ["--comment", note]

        rc, _stdout, stderr = await _run_gh(args)
        if rc != 0:
            return ActionResult(ok=False, action=action, error=stderr.strip()[:200])
        return ActionResult(ok=True, action=action, detail=f"github {action} {repo}#{number}")
