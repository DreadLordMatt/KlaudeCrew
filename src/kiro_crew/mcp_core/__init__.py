"""kirocrew-core MCP server — thin tool dispatcher (see submodules).

Runs as ``kirocrew mcp-core`` — kiro-cli spawns it as a child process and
calls tools via JSON-RPC over stdio (MCP protocol).

This module was a single 4590-line file dominated by ``_list_tools`` (the
tool-schema builder) and ``_call_tool_inner`` (a flat ``if name == ...``
dispatch over ~49 tools). Both were split into focused siblings:

* ``tool_schemas``  — ``_list_tools`` (schema declarations)
* ``transport``     — gateway HTTP helpers + ``_API``
* ``governance``    — session-key resolution + governance vetting
* ``identity``      — local secret / user token / PID walk / workspace bucket
* ``browser_snapshot`` — browse_* snapshot compression
* ``handlers.*``    — per-family tool dispatchers (subagent, messaging,
  artifacts, knowledge_chat, workflow, misc)

``_call_tool_inner`` now composes the per-family dispatchers, each of which
returns ``handlers._UNHANDLED`` when it does not own the tool name, so the
original flat routing + early-return semantics are preserved. Every name
previously importable from ``kiro_crew.mcp_core`` is re-exported below.
"""

from __future__ import annotations

# ── Re-exports of names the original single-file ``mcp_core`` imported ──
# Preserved so ``from kiro_crew.mcp_core import X`` (and existing monkeypatch
# targets) keep resolving. Mutation-bearing state (token/knowledge caches)
# lives single-owner in its submodule; the names below are value/name
# re-exports only and must not be mutated through this shim.
import contextlib  # noqa: F401
import hashlib  # noqa: F401
import json  # noqa: F401
import mimetypes  # noqa: F401
import os  # noqa: F401
import platform  # noqa: F401
import re as _re  # noqa: F401
import subprocess  # noqa: F401
import tempfile  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import unicodedata  # noqa: F401
import urllib.error  # noqa: F401
import urllib.request  # noqa: F401
import uuid  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any
from urllib.parse import urlencode, urlparse  # noqa: F401

from kiro_crew import platform_compat  # noqa: F401
from kiro_crew.aim_agents import list_agents  # noqa: F401
from kiro_crew.artifacts import _infer_kind  # noqa: F401
from kiro_crew.config.loader import (  # noqa: F401
    KiroCrewConfig,
    config_dir,
    outbox_dir,
)
from kiro_crew.context_management import (  # noqa: F401
    COMPLETION_KEEP_DEFAULT_CHARS,
    summarize_result,
)
from kiro_crew.dashboard.origin import parse_dashboard_url  # noqa: F401
from kiro_crew.history import _SEARCH_SCAN_WINDOW as SEARCH_SCAN_WINDOW  # noqa: F401
from kiro_crew.history import ConversationLog  # noqa: F401
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes  # noqa: F401
from kiro_crew.knowledge.dedup import dedup_sweep  # noqa: F401
from kiro_crew.knowledge.embedder import create_embedder_from_config  # noqa: F401
from kiro_crew.knowledge.retrieval import HybridRetriever  # noqa: F401
from kiro_crew.knowledge.store import KnowledgeStore  # noqa: F401
from kiro_crew.mcp_core.browser_snapshot import (  # noqa: F401
    _compress_snapshot_to_outline,
    _search_snapshot,
)
from kiro_crew.mcp_core.governance import (  # noqa: F401
    _audit_governance_deny,
    _governance_app,
    _resolve_session_key,
    _resolve_session_key_strict,
    _vet_channel_governance,
    _vet_memory_writes_governance,
    _vet_messaging_governance,
)
from kiro_crew.mcp_core.handlers import _UNHANDLED  # noqa: F401
from kiro_crew.mcp_core.handlers import artifacts as _artifacts
from kiro_crew.mcp_core.handlers import knowledge_chat as _knowledge_chat
from kiro_crew.mcp_core.handlers import messaging as _messaging
from kiro_crew.mcp_core.handlers import misc as _misc
from kiro_crew.mcp_core.handlers import subagent as _subagent
from kiro_crew.mcp_core.handlers import workflow as _workflow
from kiro_crew.mcp_core.handlers.artifacts import (  # noqa: F401
    _artifact_reemit_hint,
    _artifact_ref_link,
    _format_anchor,
    _resolve_artifact_folder_id,
)
from kiro_crew.mcp_core.handlers.knowledge_chat import (  # noqa: F401
    _HISTORY_INCOGNITO_MODES,
    _HISTORY_SNIPPET_ROLES,
    _KNOWLEDGE_CACHE,
    _KNOWLEDGE_CACHE_LOCK,
    _SEARCH_HISTORY_SCAN,
    _SNIPPET_MAX_LEN,
    _SNIPPET_RADIUS,
    _casefold_match_span,
    _extract_history_snippet,
    _get_knowledge_search,
    _history_is_incognito,
    _knowledge_db_signature,
    _parse_iso_date_epoch,
    _redact_history_output,
)
from kiro_crew.mcp_core.handlers.messaging import (  # noqa: F401
    _current_session_thread_ts,
)
from kiro_crew.mcp_core.handlers.workflow import (  # noqa: F401
    _redact_obj,
    _wf_return,
)
from kiro_crew.mcp_core.identity import (  # noqa: F401
    _USER_TOKEN_CACHE,
    _caller_workspace,
    _get_ppid,
    _internal_secret,
    _local_user_token,
    _ppid_via_libproc,
    _ws_bucket,
)
from kiro_crew.mcp_core.tool_schemas import _list_tools

# ── Re-exports: preserve every historically importable ``mcp_core`` symbol ──
from kiro_crew.mcp_core.transport import (  # noqa: F401
    _API,
    _delete,
    _delete_user,
    _get,
    _get_user,
    _http_error_body,
    _patch,
    _post,
    _resolve_api_base,
    _session_key_header_error,
    _with_token,
)
from kiro_crew.mcp_shared import (  # noqa: F401
    ToolCancelled,
    call_tool_with_logging,
    is_tool_cancelled,
    run_mcp_stdio_loop,
)
from kiro_crew.platform import redact_via_context as redact  # noqa: F401
from kiro_crew.security import (  # noqa: F401
    BINARY_MIME_ALLOWLIST,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel  # noqa: F401
from kiro_crew.skills import SkillsLoader  # noqa: F401
from kiro_crew.subagent import resolve_max_subagents  # noqa: F401
from kiro_crew.subagent_persistence import _agent_dir  # noqa: F401
from kiro_crew.validation import (  # noqa: F401
    _SLACK_TS_RE,
    ARTIFACT_AGENT_MARKER,
    ARTIFACT_DELETE_COMMENT_SCHEMA,
    ARTIFACT_DELETE_SCHEMA,
    ARTIFACT_FOLDER_CREATE_SCHEMA,
    ARTIFACT_FOLDER_DELETE_SCHEMA,
    ARTIFACT_FOLDER_LIST_SCHEMA,
    ARTIFACT_FOLDER_MOVE_SCHEMA,
    ARTIFACT_FOLDER_RENAME_SCHEMA,
    ARTIFACT_GET_COMMENTS_SCHEMA,
    ARTIFACT_GET_SCHEMA,
    ARTIFACT_LIST_SCHEMA,
    ARTIFACT_MARK_REVIEW_SCHEMA,
    ARTIFACT_MOVE_SCHEMA,
    ARTIFACT_POST_COMMENT_SCHEMA,
    ARTIFACT_REPLY_COMMENT_SCHEMA,
    ARTIFACT_REVERT_SCHEMA,
    ARTIFACT_SAVE_SCHEMA,
    ARTIFACT_UPDATE_SCHEMA,
    ARTIFACT_VERSIONS_SCHEMA,
    AUTONUDGE_STOP_SCHEMA,
    CHANNEL_ID_RE,
    GET_CHAT_SESSION_SCHEMA,
    KNOWLEDGE_DEDUP_SCHEMA,
    LEARN_ADD_SCHEMA,
    LOCAL_KNOWLEDGE_SEARCH_SCHEMA,
    MAX_MEDIUM_STRING,
    MAX_SHORT_STRING,
    MCP_CORE_SCHEMAS,
    REGISTER_HOOK_SCHEMA,
    SEARCH_CHAT_HISTORY_SCHEMA,
    SET_PROJECT_SCHEMA,
    SKILL_SEARCH_SCHEMA,
    SPAWN_RUN_SCHEMA,
    SPAWN_SUB_AGENTS_SCHEMA,
    TASK_RUN_SCHEMA,
    WAIT_SCHEMA,
    WORKFLOW_AUTHOR_SCHEMA,
    WORKFLOW_RERUN_SCHEMA,
    WORKFLOW_RUN_ID_SCHEMA,
    WORKFLOW_RUN_SCHEMA,
    validate_tool_args,
)


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    schema = MCP_CORE_SCHEMAS.get(name)
    if schema:
        return validate_tool_args(args, schema)
    return args  # tools without schemas (learn_list) pass through


# Order is immaterial for correctness (tool names are unique across families);
# kept roughly in the original declaration order for readability.
_DISPATCHERS = (
    _subagent.handle,
    _misc.handle,
    _messaging.handle,
    _artifacts.handle,
    _knowledge_chat.handle,
    _workflow.handle,
)


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Route *name* to the owning family dispatcher (flat-if fall-through).

    Each family returns ``_UNHANDLED`` for tool names it does not own; we try
    them in order and return the first real result, mirroring the original
    single ``if name == ...: return`` chain."""
    for _dispatch in _DISPATCHERS:
        result = _dispatch(name, args)
        if result is not _UNHANDLED:
            return result
    return f"Unknown tool: {name}"


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key="mcp_core",
        downstream_service="kirocrew-core",
    )


def run_mcp_core_server() -> None:
    """Run MCP stdio server for core agent tools."""
    run_mcp_stdio_loop("kirocrew-core", "1.0.0", _list_tools, _call_tool)
