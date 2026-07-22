"""Split from slack/handler.py: keyword_commands cluster."""

from __future__ import annotations

import logging
import time

from kiro_crew.cron import CronService, compute_next_run_ts, format_schedule, get_local_tz
from kiro_crew.history import ConversationLog
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import SessionManager
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.format import SLACK_MSG_LIMIT, TRUNCATION_NOTICE, split_message
from kiro_crew.slack.sessions_view import (
    _SESSIONS_DEFAULT_LIMIT,
    _build_sessions_blocks,
    _collect_recent_sessions,
)
from kiro_crew.subagent import SubagentManager
from kiro_crew.taskrunner import TaskRunner

logger = logging.getLogger(__name__)


def _remove_all_jobs(cron_service: CronService) -> str:
    """Remove all cron jobs and return a summary."""
    jobs = cron_service.list_jobs(include_disabled=True)
    if not jobs:
        return "No cron jobs to remove."
    lines = []
    for j in jobs:
        # j.name is free-form user/LLM-supplied text reaching a Slack reply and
        # the persisted conversation log — redact it like the `cron list` branch
        # does for j.message (j.id is a generated UUID, left as-is).
        safe_name, _ = redact_credentials(redact_exfiltration_urls(j.name)[0])
        lines.append(f"- `{j.id}` — {safe_name}")
    for j in jobs:
        cron_service.remove_job(j.id)
    return f"✅ Removed {len(lines)} cron job(s):\n" + "\n".join(lines)


def _handle_spawn_command(text: str, manager: SubagentManager, session_key: str = "") -> str | None:
    """Intercept spawn/bg keyword commands. Returns reply or None."""
    t = text.strip()
    low = t.lower()

    for prefix in ("spawn ", "bg "):
        if low.startswith(prefix):
            return _do_spawn(t[len(prefix) :].strip(), manager, session_key)
    return None


def _do_spawn(task: str, manager: SubagentManager, session_key: str = "") -> str | None:
    """Execute a spawn command. Returns reply string."""
    if not task:
        return None

    # "spawn list" / "spawn status"
    if task.lower() in ("list", "status"):
        running = manager.running
        if not running:
            return "No subagents running."
        lines = ["*Running subagents:*"]
        for a in running:
            elapsed = int(time.time() - a.started)
            lines.append(f"🔹 `{a.id}` | {elapsed}s | {a.task[:60]}")
        return "\n".join(lines)

    info = manager.spawn(task, parent_session_key=session_key)
    if not info:
        return f"⚠️ Subagent capacity reached ({manager.max_concurrent}). Try again later."
    return f"🚀 Spawned subagent `{info.id}`\n_{task[:100]}_"


def _handle_cron_command(
    text: str, cron_service: CronService, channel: str, thread_ts: str
) -> str | None:
    """Handle cron keyword commands. Returns reply or None."""
    t = text.strip().lower()
    parts = t.split()

    if len(parts) < 2 or parts[0] != "cron":
        return None

    action = parts[1]

    if action == "list":
        jobs = cron_service.list_jobs(include_disabled=True)
        if not jobs:
            return "No cron jobs scheduled."
        lines = ["*Your cron jobs:*"]
        now = time.time()
        tz_name, _ = get_local_tz()
        for j in jobs:
            status = "✅" if j.enabled else "⏸️"
            sched = format_schedule(j.schedule, tz_name=j.timezone or tz_name)
            sched, _ = redact_credentials(redact_exfiltration_urls(sched)[0])
            last = ""
            if j.last_status == "ok":
                last = " ✓"
            elif j.last_status == "error":
                last = " ❌"
            nxt = compute_next_run_ts(j, now=now)
            next_part = ""
            if nxt is not None:
                delta = nxt - now
                if delta >= 86400:
                    d = int(delta // 86400)
                    h = int((delta % 86400) // 3600)
                    rel = f"in {d}d {h}h"
                elif delta >= 3600:
                    h = int(delta // 3600)
                    m = int((delta % 3600) // 60)
                    rel = f"in {h}h {m}m"
                elif delta > 0:
                    m = int(delta // 60)
                    rel = f"in {m}m" if m >= 1 else "in <1m"
                else:
                    rel = "now"
                next_part = f" | ⏭ {rel}"
            safe_msg, _ = redact_credentials(redact_exfiltration_urls(j.message)[0])
            safe_msg = safe_msg[:50]
            lines.append(f"{status} `{j.id}` | `{sched}` | {safe_msg}{last}{next_part}")
        return "\n".join(lines)

    if len(parts) < 3:
        return None

    job_id = parts[2]

    if action == "remove":
        if job_id == "all":
            return _remove_all_jobs(cron_service)
        if cron_service.remove_job(job_id):
            return f"✅ Removed cron job `{job_id}`"
        return f"❌ Job `{job_id}` not found"

    if action == "pause":
        if cron_service.enable_job(job_id, enabled=False):
            return f"⏸️ Paused cron job `{job_id}`"
        return f"❌ Job `{job_id}` not found"

    if action == "resume":
        if cron_service.enable_job(job_id, enabled=True):
            return f"▶️ Resumed cron job `{job_id}`"
        return f"❌ Job `{job_id}` not found"

    return None


def _handle_run_command(
    text: str,
    runner: TaskRunner,
    slack: SlackClientOps,
    channel: str,
    thread_ts: str,
) -> str | None:
    """Intercept 'run <path>' keyword commands. Returns reply or None."""
    t = text.strip()
    low = t.lower()

    if low.startswith("project run "):
        t = "task run " + t[12:]
        low = t.lower()

    if not low.startswith("task run "):
        return None

    arg = t[9:].strip()
    if not arg:
        return None

    # "run status"
    if arg.lower() == "status":
        status = runner.status()
        if not status.get("running") and not status.get("status"):
            return "No task running."
        return (
            f"*Task Runner*\n"
            f"Status: {status.get('status', 'idle')}\n"
            f"Steps: {status.get('completed', 0)}/{status.get('steps', 0)}\n"
            f"Current: step {status.get('current_step', 0)}"
        )

    # "run cancel"
    if arg.lower() == "cancel":
        if not runner.running:
            return "No task running."
        runner.cancel()
        return "🛑 Task cancelled."

    # "task run <spec-path>" — start a task
    if runner.running:
        return "⚠️ Task runner is already running. Use `task run cancel` first."

    from pathlib import Path

    spec_path = Path(arg).expanduser()
    if not spec_path.exists():
        return f"❌ Spec file not found: `{spec_path}`"

    try:
        runner.start_background(spec_path, source="chat")
    except Exception as exc:
        return f"❌ Failed to start: {exc}"
    return f"🚀 Task started: `{spec_path.name}`\nUse `task run status` to check progress."


async def _handle_sessions_command(
    cmd_text: str,
    slack: SlackClientOps,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    conversation_log: ConversationLog | None,
    *,
    sessions: SessionManager | None = None,
) -> None:
    """Handle the ``sessions`` keyword in DMs.

    Delegates to :func:`kiro_crew.slack.sessions_view._collect_recent_sessions`
    and :func:`kiro_crew.slack.sessions_view._build_sessions_blocks` so the
    keyword, the ``/<command> sessions`` slash command, and the App Home Tab
    all render the same Block Kit content with the same Resume button wiring.
    """
    # Wrap the collector so a transient OSError still produces a SEL audit
    # entry. Without this, an IO failure would skip the audit entirely and
    # the access attempt would be invisible to the security pipeline.
    # Mirrors the slash and Home Tab error-path patterns.
    try:
        rows = _collect_recent_sessions(sessions, limit=_SESSIONS_DEFAULT_LIMIT)
    except Exception as exc:
        # Redact-then-truncate: redact() first so credential / exfil
        # patterns aren't split mid-string by the truncation step.
        redacted_exc, _ = redact_exfiltration_urls(str(exc))
        redacted_exc, _ = redact_credentials(redacted_exc)
        sel().log_api_access(
            caller=session_key,
            operation="slack.sessions_data_access",
            outcome="error",
            source="slack",
            resources="0 sessions read (collector failed)",
            error=redacted_exc[:200],
        )
        logger.exception("sessions keyword: collector failed for session_key %s", session_key)
        await slack.post_message(channel, "_Sessions unavailable._", reply_ts)
        return

    sel().log_api_access(
        caller=session_key,
        operation="slack.sessions_data_access",
        outcome="allowed",
        source="slack",
        resources=f"{len(rows)} sessions read",
    )

    if not rows:
        await slack.post_message(channel, "_No recent sessions._", reply_ts)
        return

    blocks = _build_sessions_blocks(rows)
    await slack.post_blocks(channel, blocks, "Recent sessions:", reply_ts)


async def _safe_update(slack: SlackClientOps, channel: str, ts: str, text: str) -> None:
    """Update a Slack message, truncating if too long.

    Used for progressive streaming edits — truncation is fine here since
    the final message uses _safe_final_update which splits instead.
    """
    text, _ = redact_exfiltration_urls(text)
    if len(text) > SLACK_MSG_LIMIT:
        text = text[:SLACK_MSG_LIMIT] + TRUNCATION_NOTICE
    try:
        await slack.update_message(channel, ts, text)
    except Exception:
        logger.debug("Failed to update message %s", ts, exc_info=True)


async def _safe_final_update(
    slack: SlackClientOps, channel: str, ts: str, text: str, thread_ts: str | None = None
) -> None:
    """Final message update — splits into multiple messages if too long."""
    text, _ = redact_exfiltration_urls(text)
    parts = split_message(text)
    # First part updates the existing streaming message
    try:
        await slack.update_message(channel, ts, parts[0])
    except Exception:
        logger.debug("Failed to update message %s", ts, exc_info=True)
    # Overflow parts posted as follow-up messages in the same thread
    for part in parts[1:]:
        try:
            await slack.post_message(channel, part, thread_ts)
        except Exception:
            logger.debug("Failed to post continuation message", exc_info=True)
