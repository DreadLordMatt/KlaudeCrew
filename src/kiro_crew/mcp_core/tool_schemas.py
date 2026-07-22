"""MCP tool-schema declarations for the kirocrew-core server.

``_list_tools`` was extracted verbatim from ``mcp_core`` — it is a pure
schema builder (the single largest function in the original module) and
declares the JSON-RPC ``tools/list`` payload consumed by kiro-cli."""

from __future__ import annotations

from typing import Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.subagent import resolve_max_subagents
from kiro_crew.validation import LEARN_ADD_SCHEMA, MAX_SHORT_STRING


def _list_tools() -> list[dict[str, Any]]:
    # Derive the learn_add rule/negative char limit from the schema field the
    # validator actually enforces (single source of truth) so the tool hint
    # tracks the enforced limit — including a future config-driven value —
    # instead of a parallel constant that can silently drift.
    _rule_max = next(
        (f.max_len for f in LEARN_ADD_SCHEMA.fields if f.name == "rule"),
        MAX_SHORT_STRING,
    )
    _neg_max = next(
        (f.max_len for f in LEARN_ADD_SCHEMA.fields if f.name == "negative"),
        MAX_SHORT_STRING,
    )
    # Advertise the live concurrent sub-agent cap so the model fans out with
    # confidence instead of self-limiting. resolve_max_subagents is the single
    # source of truth (auto-sizes from host mem/CPU + learned cost, or the
    # explicit agent.max_subagents). A snapshot at tool-list time is fine: this
    # is advisory guidance, not an enforced limit, and SubagentManager
    # auto-queues any overflow regardless.
    try:
        _max_sub = resolve_max_subagents(KiroCrewConfig.load())
    except Exception:
        _max_sub = 0
    _cap_hint = (
        f" You can run up to {_max_sub} sub-agents concurrently; if a task has "
        "more independent parts than that, still pass ALL of them in one call — "
        "any beyond the cap are queued and drained automatically as slots free, "
        "so you never need to split the work into multiple manual rounds."
        if _max_sub > 0
        else ""
    )
    return [
        {
            "name": "spawn_run",
            "description": (
                "Spawn subagent(s) to run tasks in the background. "
                "Returns immediately — results arrive as [Subagent completion event] "
                "messages in your conversation. For parallel work, use 'tasks' array. "
                "Tasks are automatically batched if they exceed the concurrency limit."
                + _cap_hint
                + " WAIT for all completion events before responding to the user."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Single task description",
                    },
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Multiple tasks to run in parallel",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Agent name for the subagent. Use spawn_list to see available agents.",
                    },
                    "agents": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Agent names corresponding to each task in 'tasks' array",
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Override tool-call budget for this spawn (default: config or 100)",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Optional absolute path to launch the subagent subprocess in, "
                            "instead of the default sandbox. Enables cwd-relative resource globs "
                            "(.kiro/steering, AGENTS.md, CLAUDE.md) to resolve against this directory. "
                            "Must be under a configured subagent_cwd_allowed_roots entry "
                            "(default: [~/workspace, ~/workplace]). Applies to all tasks in a batch spawn."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional model override for the subagent (e.g. 'deepseek-3.2', "
                            "'claude-haiku-4.5'). When set, the subagent runs on this model "
                            "instead of the gateway default. To discover available models, "
                            "run: kiro-cli chat --list-models --format json"
                        ),
                    },
                },
            },
        },
        {
            "name": "spawn_list",
            "description": "List all running and completed subagents (read-only, no commands executed)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "skill_search",
            "description": (
                "Search available skills by keyword (grep over skill names, "
                "descriptions, and — on a metadata miss — bodies). Only the most-"
                "used skills are pre-listed in the injected '## Available Skills' "
                "block; use this tool to discover the long tail that is NOT shown "
                "there. Returns matching skills with file paths — `cat` a path to "
                "load the full skill, or use the $<name> inline token."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords to search for across skills.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20, max 50).",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "spawn_status",
            "description": (
                "Retrieve a completed subagent's full transcript by agent ID (from a "
                "completion event). The completion event gives a summary plus this "
                "transcript on disk — use this tool (or the read/grep tools on the path) "
                "to read the rest instead of re-running the subagent. For large "
                "transcripts, page with offset/limit (line-based, like reading code) or "
                "filter with grep (regex) rather than pulling the whole thing into context."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Subagent ID from completion event",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "0-based start line for a paged read (default 0)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max lines to return (1-2000). Omit for the full transcript; "
                            "use with offset to page through a large result."
                        ),
                    },
                    "grep": {
                        "type": "string",
                        "description": (
                            "Case-insensitive regex; return only transcript lines that "
                            "match (offset/limit then apply to the matches)."
                        ),
                    },
                },
                "required": ["agent_id"],
            },
        },
        {
            "name": "spawn_sub_agents",
            "description": (
                "Spawn one or more sub-agents to run tasks in parallel. Each sub-agent "
                "gets its own session with full tool access. BLOCKS until all sub-agents "
                "complete, then returns their collected results. Use for delegating "
                "independent subtasks to specialist agents. Preferred over spawn_run when "
                "you need results before continuing." + _cap_hint
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_or_mode": {
                                    "type": "string",
                                    "description": "Agent name for the sub-agent",
                                },
                                "prompt": {
                                    "type": "string",
                                    "description": "Task/prompt for the sub-agent",
                                },
                            },
                            "required": ["prompt"],
                        },
                        "description": "Array of sub-agents to spawn in parallel",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Optional absolute path to launch sub-agents in. "
                            "Must be under a configured subagent_cwd_allowed_roots entry."
                        ),
                    },
                },
                "required": ["agents"],
            },
        },
        {
            "name": "learn_add",
            "description": (
                "Save a learned correction or preference that persists across all "
                "future sessions. MUST be called when the user corrects you, says "
                "'always do X', 'never do Y', or 'remember that'. Include both "
                "the rule (what to do) and negative (what not to do)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "rule": {
                        "type": "string",
                        "maxLength": _rule_max,
                        "description": (
                            f"The lesson to remember. HARD LIMIT {_rule_max} "
                            "characters — longer rules are REJECTED (not truncated), "
                            "so keep it concise. Put 'what not to do' in the separate "
                            "'negative' field rather than inlining a long '-- NOT: ...' "
                            "clause here, and split unrelated corrections into multiple "
                            "learn_add calls instead of one oversized rule."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": ["tool", "preference", "knowledge"],
                        "description": "Category: tool, preference, or knowledge",
                    },
                    "negative": {
                        "type": "string",
                        "maxLength": _neg_max,
                        "description": (
                            f"What NOT to do (optional). HARD LIMIT {_neg_max} "
                            "characters — rejected if exceeded."
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["global", "workspace"],
                        "description": "Where to save: 'global' (default, all workspaces) or 'workspace' (active workspace only)",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name (required when scope='workspace'). Use the workspace name from your session context.",
                    },
                },
                "required": ["rule", "category"],
            },
        },
        {
            "name": "learn_list",
            "description": "List all saved lessons and corrections",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "learn_remove",
            "description": "Remove lessons whose rule contains the given substring",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring to match"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "task_run",
            "description": (
                "Start the autonomous task runner from a spec file or inline content. "
                "Use when the user provides a task spec or says 'run this task', "
                "'start a task', or 'run a task'. "
                "For inline specs, prefix content with __inline__:"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "string",
                        "description": "Path to spec file, or inline content prefixed with __inline__:",
                    },
                    "name": {
                        "type": "string",
                        "description": "Human-readable task name (auto-derived from spec if omitted)",
                    },
                },
                "required": ["spec"],
            },
        },
        {
            "name": "wait",
            "description": (
                "Pause execution for a specified duration while preserving full session "
                "context. Use when waiting for external systems (AutoSDE review, CI "
                "pipeline, deployment). Max 1800s (30 min)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": "Duration to wait in seconds (60-1800)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why we are waiting (shown to user)",
                    },
                },
                "required": ["seconds", "reason"],
            },
        },
        {
            "name": "register_hook",
            "description": (
                "Register a webhook listener so an external system can inject a message "
                "into a dedicated agent session later. Returns the webhook URL and session "
                "key. Use this when you need to hand off to an external process (e.g. "
                "submit a code review, then wait for AutoSDE to call back with results). "
                "The external system POSTs to the returned URL with the results."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "hook_id": {
                        "type": "string",
                        "description": "Unique identifier for this hook (e.g. 'autosde:pr-123')",
                    },
                    "context_summary": {
                        "type": "string",
                        "description": "Summary of current work context for session resume",
                    },
                },
                "required": ["hook_id", "context_summary"],
            },
        },
        {
            "name": "send_message",
            "description": (
                "Send a message to the user. By default delivers a dashboard "
                'notification only. Set session="slack" to also send a Slack DM. '
                "Set 'channel' to target a tracked channel, or 'user' to DM an "
                "allowed user — specify at most one, not both. "
                "Use this whenever you decide someone should be notified — most "
                "commonly in silent cron jobs, but applicable any time proactive "
                "notification is needed."
                "\n\nsession param (optional):"
                "\n  omitted  — dashboard notification only (default)."
                '\n  "slack"  — Slack DM + dashboard notification.'
                '\n  "origin" — inject into the dashboard session that spawned'
                " this cron. Falls through to notification-only if origin is"
                " unreachable (tab closed, history deleted, or cron has no origin)."
                "\n\nExplicit channel=... or user=... always sends to Slack."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Message text. Also used as fallback when blocks are provided.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional title for the notification",
                    },
                    "blocks": {
                        "type": "array",
                        "description": "Optional Slack Block Kit blocks array. When provided, the message is sent as a rich Block Kit message with text as fallback.",
                        "items": {"type": "object"},
                        "maxItems": 50,
                    },
                    "channel": {
                        "type": "string",
                        "description": "Target channel ID (e.g. C0123ABC456). Must be a tracked channel. Omit to send to owner DM.",
                    },
                    "user": {
                        "type": "string",
                        "description": "Target user ID (e.g. U0123ABC456) to DM. Must be an allowed user. Omit to send to owner DM.",
                    },
                    "unfurl_links": {
                        "type": "boolean",
                        "description": "Whether to unfurl URL link previews. Defaults to true.",
                    },
                    "unfurl_media": {
                        "type": "boolean",
                        "description": "Whether to unfurl media (images/video) previews. Defaults to true.",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": (
                            "Optional Slack thread timestamp (e.g. '1712793600.123456'). "
                            "When provided, the message is posted as a threaded reply under "
                            "that parent message. Works with 'channel' (thread in channel) "
                            "or 'user' (thread in DM)."
                        ),
                    },
                    "reply_broadcast": {
                        "type": "boolean",
                        "description": (
                            "When true and 'thread_ts' is set, also broadcast the threaded reply "
                            "to the channel's main message list. Requires 'thread_ts' — passing "
                            "reply_broadcast=true without thread_ts returns 400. Defaults to false."
                        ),
                    },
                    "session": {
                        "type": "string",
                        "enum": ["origin", "slack"],
                        "description": (
                            "Delivery routing. Omit for notification bell only (default). "
                            '"slack" adds Slack DM delivery. '
                            '"origin" injects into the dashboard session that spawned '
                            "this cron (falls back to notification if unreachable)."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "delete_message",
            "description": (
                "Delete a message previously sent by this bot. Only works on "
                "messages authored by the KiroCrew bot itself (Slack API constraint). "
                "Use to clean up transient notifications after the user acknowledges them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel ID where the message was posted.",
                    },
                    "ts": {
                        "type": "string",
                        "description": "Timestamp of the message to delete (from send_message response).",
                    },
                },
                "required": ["channel", "ts"],
            },
        },
        {
            "name": "read_slack_profile",
            "description": (
                "Read a Slack user's profile. Returns display name, title, "
                "status, timezone, and other profile fields. Rate limited to "
                "5 lookups per minute."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user": {
                        "type": "string",
                        "description": "Slack user ID (e.g. U0123ABC456).",
                    },
                },
                "required": ["user"],
            },
        },
        {
            "name": "file_send",
            "description": (
                "Send a file to the user. Copies the file to the outbox and "
                "notifies the dashboard/Slack with a download link. Use when "
                "you've generated a report, export, artifact, or any file the "
                "user should receive."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file to send"},
                    "description": {
                        "type": "string",
                        "description": "Brief description of what the file is",
                    },
                    "channel": {
                        "type": "string",
                        "description": (
                            "Optional Slack channel ID (e.g. C0123ABC456) to upload "
                            "the file to. Must be a tracked channel the bot is a "
                            "member of. Omit to send to the owner's DM."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "artifact_save",
            "description": (
                "Save a chat-rendered artifact (typically the HTML body of an "
                "<mcwidget>) so the user can find, view, and iterate on it later. "
                "Returns the slug — a stable handle the user (and you) can "
                "reference in future sessions ('iterate on artifact <slug>'). "
                "Use this when the user asks to save a widget, when you create "
                "something worth keeping, or before iterating (use artifact_update "
                "for the iteration step itself)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Human-readable name (e.g. 'CR Queue Dashboard'). Used to derive the slug if omitted.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Artifact content. For widgets, the inner HTML of the <mcwidget> tag (NOT the surrounding tag itself).",
                    },
                    "slug": {
                        "type": "string",
                        "description": "Optional explicit slug (lowercase, digits, hyphens). Auto-derived from name when omitted.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["widget", "html", "markdown", "svg", "json", "text", "webapp"],
                        "description": (
                            "Artifact kind. Optional — inferred from the content "
                            "when omitted (HTML-ish body -> widget, markdown text "
                            "-> markdown). Pass explicitly to override; markdown "
                            "documents should set kind='markdown'."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "enum": ["chat", "cron", "subagent", "manual", "import"],
                        "description": "Provenance marker. Default: chat.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short description of what the artifact shows or does.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for filtering in the library (max 16).",
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Optional folder to file the artifact in — a folder id "
                            "OR a '/'-separated human path (e.g. 'Reports/Q3'). "
                            "Missing path segments are auto-created (mkdir -p). "
                            "Omit or pass 'root' to leave it unfiled."
                        ),
                    },
                    "webapp_metadata": {
                        "type": "object",
                        "description": (
                            "For kind='webapp' only — metadata for the app-artifact "
                            "control card. Shape: {slug, origin_session, "
                            "deploy_target:{provider,account,region,public_url}, "
                            "architecture, lifecycle, cost, teardown}. "
                            "For draft apps: set lifecycle.status='draft'"
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["name", "content"],
            },
        },
        {
            "name": "artifact_get",
            "description": (
                "Load an artifact by slug. Returns the metadata and content. "
                "Use this before artifact_update to read the current HTML when "
                "the user asks to iterate on an existing artifact."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug (lowercase, digits, hyphens).",
                    },
                    "version": {
                        "type": "integer",
                        "description": "Specific version to read. Omit for current.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_update",
            "description": (
                "Update an artifact's live state. Each agent edit "
                "automatically creates a new version (like a git commit) — "
                "the user can revert to any prior agent iteration via "
                "artifact_revert. Use after artifact_get when iterating "
                "on an existing artifact at the user's request."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to update.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "New content. Each call records a new version "
                            "automatically when invoked via MCP."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "New name (optional rename).",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replacement tag list (optional).",
                    },
                    "webapp_metadata": {
                        "type": "object",
                        "description": (
                            "Webapp deployment metadata (optional). Used to "
                            "transition an artifact between draft and live "
                            "deployment states."
                        ),
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_revert",
            "description": (
                "Revert an artifact's live state to a prior version. Reads "
                "version N's content and writes it as the new live state, "
                "creating a fresh snapshot tagged 'reverted' so the activity "
                "timeline shows the rollback. Use this instead of "
                "artifact_update when the user asks to undo recent changes "
                "or restore an earlier state — it avoids the agent having "
                "to manually fetch the old content first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to revert.",
                    },
                    "target_version": {
                        "type": "integer",
                        "description": (
                            "Version number to restore. Use artifact_versions "
                            "first to list available versions."
                        ),
                        "minimum": 1,
                    },
                },
                "required": ["slug", "target_version"],
            },
        },
        {
            "name": "artifact_list",
            "description": (
                "List saved artifacts. Optionally filter by tag, kind, or "
                "name substring. Use this to discover what artifacts exist "
                "before iterating, or when the user asks 'what have we saved?'"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Filter by tag."},
                    "kind": {
                        "type": "string",
                        "enum": ["widget", "html", "markdown", "svg", "json", "text", "webapp"],
                        "description": "Filter by kind.",
                    },
                    "q": {
                        "type": "string",
                        "description": "Case-insensitive substring filter on artifact name.",
                    },
                },
            },
        },
        {
            "name": "artifact_versions",
            "description": (
                "List the version numbers stored for an artifact. Use this "
                "before artifact_get with an explicit version to figure out "
                "what's available."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_delete",
            "description": (
                "Permanently delete an artifact and all its versions. Use only "
                "when the user explicitly asks to remove an artifact."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to delete.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_get_comments",
            "description": (
                "Get all comments on an artifact (local + provider-synced). "
                "Use to read feedback, review comments, or discussion threads "
                "on an artifact before addressing them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to get comments for.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_post_comment",
            "description": (
                "Post a comment on an artifact. Agent comments are flagged "
                "(is_agent) and SEL-audited. Use scope='shared' to sync to the "
                "provider."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Comment body text.",
                    },
                    "scope": {
                        "type": "string",
                        "description": "private (local only) or shared (syncs to provider).",
                    },
                },
                "required": ["slug", "text"],
            },
        },
        {
            "name": "artifact_reply_comment",
            "description": (
                "Reply to an existing comment thread on an artifact. "
                "If the parent is provider-origin, the reply posts back."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "parent_id": {
                        "type": "string",
                        "description": "ID of the comment to reply to.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Reply body text.",
                    },
                },
                "required": ["slug", "parent_id", "text"],
            },
        },
        {
            "name": "artifact_mark_review",
            "description": (
                "Advance a comment thread to REVIEW status, signaling "
                "the issue is addressed and awaiting human verification. "
                "Agent can mark_review but NEVER resolve."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "comment_id": {
                        "type": "string",
                        "description": "ID of the root comment to advance.",
                    },
                },
                "required": ["slug", "comment_id"],
            },
        },
        {
            "name": "artifact_delete_comment",
            "description": (
                "Delete a comment thread you have demonstrably applied — an "
                "unambiguous directive ('delete this', 'fix typo') that was "
                "fully executed. Root deletes cascade to replies. For "
                "judgment calls the human may want to verify, use "
                "artifact_mark_review instead. Provider-synced comments "
                "cannot be deleted by agents (the tool refuses) — mark those "
                "REVIEW. Deletion is SEL-audited and recorded in the "
                "artifact's activity feed with your reason."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "comment_id": {
                        "type": "string",
                        "description": "ID of the comment to delete (root deletes its replies too).",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "One-line justification recorded in the audit log and "
                            "activity feed, e.g. 'applied in v12: deleted the "
                            "flagged paragraph'."
                        ),
                    },
                },
                "required": ["slug", "comment_id", "reason"],
            },
        },
        {
            "name": "artifact_folder_list",
            "description": (
                "List the artifact-library folder tree. Returns each folder's id, "
                "name, parent_id, human path, and direct item_count. Use to "
                "discover folder ids/paths before moving or organizing artifacts."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "artifact_folder_create",
            "description": (
                "Create an artifact-library folder. ``parent`` accepts a folder id "
                "OR a '/'-separated human path; missing segments are auto-created "
                "(mkdir -p). Omit ``parent`` (or pass 'root') to create at the top "
                "level. Returns the new folder id and canonical path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Folder name (max 100 chars)."},
                    "parent": {
                        "type": "string",
                        "description": "Parent folder id or human path. Omit / 'root' for top level.",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "artifact_folder_rename",
            "description": "Rename an artifact-library folder. ``folder`` = folder id or human path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder id or human path."},
                    "name": {"type": "string", "description": "New name (max 100 chars)."},
                },
                "required": ["folder", "name"],
            },
        },
        {
            "name": "artifact_folder_move",
            "description": (
                "Reparent an artifact-library folder (nest it under another, or move "
                "to the top level). Cycle-guarded — a folder cannot become its own "
                "descendant. ``folder`` and ``new_parent`` are each a folder id or "
                "human path; omit ``new_parent`` (or pass 'root') to move to top level."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder to move (id or path)."},
                    "new_parent": {
                        "type": "string",
                        "description": "Destination parent folder (id or path). Omit / 'root' for top level.",
                    },
                },
                "required": ["folder"],
            },
        },
        {
            "name": "artifact_folder_delete",
            "description": (
                "Delete an artifact-library folder. By default (delete_contents=false) "
                "this is SAFE: the folder's direct child folders and artifacts are "
                "re-parented up to the folder's parent, and only the folder itself is "
                "removed. Pass delete_contents=true to permanently delete the entire "
                "subtree, INCLUDING every descendant artifact — echo the affected "
                "count to the user before doing so. ``folder`` = folder id or human path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder id or human path."},
                    "delete_contents": {
                        "type": "boolean",
                        "description": (
                            "false (default) = keep artifacts, re-parent to the folder's "
                            "parent. true = permanently delete the whole subtree."
                        ),
                    },
                },
                "required": ["folder"],
            },
        },
        {
            "name": "artifact_move",
            "description": (
                "Move an existing artifact into a folder (or unfile it). ``folder`` = "
                "a folder id, a '/'-separated human path (missing segments auto-created), "
                "or ''/'root' to unfile. Metadata-only — does not change the artifact's "
                "content or version."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Artifact slug to move."},
                    "folder": {
                        "type": "string",
                        "description": "Destination folder id or human path; ''/'root' to unfile.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "deploy_artifact",
            "description": (
                "Preview a deploy of a webapp artifact or local directory to a "
                "public URL on the user's AWS account. This tool is PREVIEW-ONLY: "
                "it returns scan status and deploy details but never executes. "
                "Final confirmation happens in the dashboard Artifact Deploy page. "
                "Restricted-session guard and SEL audit apply identically to the "
                "HTTP endpoint."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "site_id": {
                        "type": "string",
                        "description": "Deploy slot name (e.g. 'my-app').",
                    },
                    "artifact_slug": {
                        "type": "string",
                        "description": (
                            "Slug of a static artifact (widget/html/markdown) "
                            "to deploy — its content is rendered as a page. "
                            "kind=webapp artifacts are rejected (their content "
                            "is an app summary, not deployable HTML — deploy "
                            "the app's built directory via local_dir instead). "
                            "Mutually exclusive with local_dir."
                        ),
                    },
                    "local_dir": {
                        "type": "string",
                        "description": (
                            "Validated absolute path to a static directory "
                            "(e.g. fullstack app's public/ root). Mutually "
                            "exclusive with artifact_slug."
                        ),
                    },
                    "profile": {
                        "type": "string",
                        "description": "AWS profile override (default: registry default).",
                    },
                    "ttl_hours": {
                        "type": "integer",
                        "description": "Hours until auto-cleanup, 0-8760 (default: 72; 0 = persistent).",
                    },
                },
                "required": ["site_id"],
            },
        },
        {
            "name": "autonudge_stop",
            "description": (
                "Stop the auto-nudge loop driving your current session. Call this "
                "when you determine the loop should halt (e.g. goal complete, "
                "blocked on user input, or a STOP sentinel file indicates shutdown). "
                "Removes the loop from the AutoNudgeService so no further nudges "
                "fire into this session. Safe to call even if no loop is active — "
                "returns a no-op message."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the loop is being stopped (logged for audit)",
                    },
                },
            },
        },
        {
            "name": "local_knowledge_search",
            "description": (
                "Search the user's knowledge library. Call ONLY when the user's "
                "message contains one of these explicit signals:\n"
                "- Asks 'what do we know about X' or 'check knowledge for X'\n"
                "- References a specific document, wiki, or stored content by name\n"
                "- Says 'in my docs', 'in my notes', 'according to our knowledge'\n"
                "- Asks a factual question AND mentions a topic you know is in "
                "their knowledge base\n\n"
                "Do NOT call for: general coding questions, file operations, "
                "debugging, or any task you can answer from context alone."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to find relevant knowledge chunks",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 3, max 5)",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "knowledge_dedup",
            "description": (
                "Find and collapse cross-source duplicate documents in the Knowledge "
                "Base (e.g. the same file uploaded directly AND synced via a folder). "
                "Defaults to a DRY-RUN preview that lists which duplicate would be "
                "deleted and which copy is kept, changing nothing. Pass apply=true to "
                "perform the hard deletes. Use when the user asks to de-duplicate, "
                "clean up, or preview duplicates in their knowledge base."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "apply": {
                        "type": "boolean",
                        "description": (
                            "false (default) = dry-run preview, no changes. "
                            "true = perform the hard deletes."
                        ),
                        "default": False,
                    },
                },
            },
        },
        {
            "name": "search_chat_history",
            "description": (
                "Search your own past conversation transcripts (chat history) by "
                "keyword and get back ranked, snippet-level hits. Use this to "
                "recover context that is NOT in your injected memory — e.g. 'what "
                "did we decide about X three weeks ago', 'the error message from "
                "that debugging session', a name/number/path mentioned earlier. "
                "Search like a human: try a query, read the snippets, then re-search "
                "with different keywords if the first hit isn't right. Returns "
                "metadata + a short snippet per session (NOT full transcripts) — "
                "call get_chat_session with a returned session_key to read the full "
                "thread once a hit looks promising. Scoped to your current workspace "
                "by default. This is a READ — it never modifies memory or history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword(s) to search for in past conversations.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10, max 50).",
                        "default": 10,
                    },
                    "before": {
                        "type": "string",
                        "description": "Optional ISO date (YYYY-MM-DD); only sessions modified before this day.",
                    },
                    "after": {
                        "type": "string",
                        "description": "Optional ISO date (YYYY-MM-DD); only sessions modified on/after this day.",
                    },
                    "all_workspaces": {
                        "type": "boolean",
                        "description": "Search across all workspaces instead of just the current one (default false).",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_chat_session",
            "description": (
                "Read the full message transcript of one past conversation, "
                "identified by a session_key returned from search_chat_history. "
                "Returns the messages as role/content pairs, tail-capped at "
                "max_messages. Use after search_chat_history when a snippet hit "
                "looks like the thread you need. Refuses incognito/temporary "
                "sessions. This is a READ — it never modifies memory or history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_key": {
                        "type": "string",
                        "description": "The session_key from a search_chat_history result.",
                    },
                    "max_messages": {
                        "type": "integer",
                        "description": "Max (most recent) messages to return (default 50, max 200).",
                        "default": 50,
                    },
                    "all_workspaces": {
                        "type": "boolean",
                        "description": "Allow reading a session from a different workspace than the caller's (default false — deny cross-workspace).",
                        "default": False,
                    },
                },
                "required": ["session_key"],
            },
        },
        {
            "name": "browse_outline",
            "description": (
                "Compress a browser snapshot into a compact outline with element refs. "
                "Use AFTER calling browser_snapshot to reduce a large accessibility tree "
                "(50-100K tokens) into a navigable outline (~2-5K tokens). "
                "Returns interactive elements with refs for clicking, plus page structure."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot": {
                        "type": "string",
                        "description": "The raw browser_snapshot output text to compress",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Max output lines (default 100)",
                        "default": 100,
                    },
                },
                "required": ["snapshot"],
            },
        },
        {
            "name": "browse_search",
            "description": (
                "Search a browser snapshot for specific text or patterns. "
                "Returns matching lines with element refs. Use instead of reading "
                "the full snapshot when looking for specific content on a page."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot": {
                        "type": "string",
                        "description": "The raw browser_snapshot output text to search",
                    },
                    "query": {
                        "type": "string",
                        "description": "Text or regex pattern to search for",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max matching lines to return (default 50)",
                        "default": 50,
                    },
                },
                "required": ["snapshot", "query"],
            },
        },
        {
            "name": "set_project",
            "description": (
                "Set the calling chat slot's project directory. The directory scopes "
                "file search, @-mention auto-complete, the [PROJECT] context line, "
                "and project-level .kiro/steering/**/*.md. "
                "\n\n"
                "Use after a skill scaffolds a new working tree (e.g. brazil-workspace) "
                "so the agent retargets to the new source instead of the old one. "
                'To clear the project, pass path="" with clear=true. '
                "\n\n"
                "Restrictions: only works in dashboard sessions with explicit identity "
                "(injected KIROCREW_SESSION_KEY or per-call caller context). Subagents, "
                "Slack, and cron contexts are rejected — those resolve via PID-walk and "
                "would silently mutate the wrong slot. Sensitive paths (~/.aws, ~/.ssh, "
                "etc.) are blocked by the underlying endpoint. "
                "\n\n"
                "The session is reset on the NEXT turn boundary (not inline) so this "
                "tool returns cleanly without killing its own caller."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute path to the new project directory. "
                            "Must be non-empty unless clear=true."
                        ),
                    },
                    "clear": {
                        "type": "boolean",
                        "description": "Set true to clear the project scope (path must be empty).",
                    },
                },
                "required": ["path"],
            },
        },
        # --- Dynamic workflows (M6): author + run + monitor from chat ---
        {
            "name": "workflow_author",
            "description": (
                "Turn a natural-language goal into a runnable DYNAMIC WORKFLOW "
                "Python script (orchestrates agents via a sandboxed `ctx` DSL). "
                "Returns the validated script source — then call workflow_run to "
                "execute it. (Usually you can skip this and pass `intent` straight to "
                "workflow_run, which authors+runs in one step.)"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "The goal in plain language, e.g. 'deep research on the origin of pizza'",
                    },
                },
                "required": ["intent"],
            },
        },
        {
            "name": "workflow_run",
            "description": (
                "★ THE tool for 'use a dynamic workflow to …' / 'run a workflow' / any "
                "multi-phase, monitorable, restartable agent orchestration. PREFER THIS "
                "over spawn_sub_agents for such requests. Just pass `intent` (the user's "
                "goal in plain words) and it authors + launches the workflow in one step "
                "— do NOT hand-roll the orchestration with spawn tools. Returns a run_id "
                "immediately; the run streams to the Workflows dashboard tab and its "
                "result is injected back into this chat on completion. Monitor with "
                "workflow_status / workflow_result; restart parts with "
                "workflow_rerun_subtree."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Workflow script source (Python)"},
                    "intent": {
                        "type": "string",
                        "description": "If no source: a NL goal to author then run",
                    },
                    "name": {"type": "string", "description": "Optional run name"},
                    "args": {
                        "type": "object",
                        "description": "Optional args passed to the workflow",
                    },
                    "budget_total": {
                        "type": "integer",
                        "description": "Optional token budget ceiling for the run",
                    },
                },
            },
        },
        {
            "name": "workflow_status",
            "description": (
                "Get the live status of a background workflow run by run_id "
                "(running/finished/failed/cancelled + agent/event counts). Use to "
                "monitor a run you started; for the full result use workflow_result."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "workflow_result",
            "description": (
                "Get a workflow run's full result + event stream by run_id "
                "(phases, per-agent outcomes, logs, final result)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "workflow_list",
            "description": "List recent background workflow runs (newest first) with their status.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "workflow_cancel",
            "description": "Cancel a running background workflow by run_id.",
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "workflow_rerun_subtree",
            "description": (
                "Re-run a prior workflow, REPLAYING the unchanged prefix and "
                "re-executing from a chosen step ('restart parts' at runtime). "
                "Agent calls before `from_index` reuse the prior run's cached "
                "results; calls at/after re-call the model. from_index=0 re-runs "
                "everything fresh. Returns a new run_id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "The prior run to restart from"},
                    "from_index": {
                        "type": "integer",
                        "description": "Agent call_index to restart at (0 = full re-run)",
                        "default": 0,
                    },
                },
                "required": ["run_id"],
            },
        },
    ]
