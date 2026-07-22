"""Split from slack/handler.py: agent_resolution cluster."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import safe_read_file_bytes, validate_file_path
from kiro_crew.security import is_sensitive_path
from kiro_crew.slack.handler_state import _hydrated_sessions, _thread_agents, _thread_projects

logger = logging.getLogger(__name__)


_cached_default_agent: str | None = None  # None = not yet loaded from disk


# Review mode: stores draft text keyed by "channel|thread_ts|uuid" for button/modal
# handlers. Each entry includes the *requester* user_id so handlers can authorize the
# requester (in addition to bot owner) to act on their own drafts.
# Bounded with TTL to prevent memory leaks from abandoned drafts.
_REVIEW_PLACEHOLDER_TS = "review_placeholder"
_REVIEW_DRAFT_TTL = 3600  # 1 hour
_REVIEW_DRAFT_MAX = 1024
# key → (draft, requester_user_id, timestamp)
_review_drafts: dict[str, tuple[str, str, float]] = {}


def _review_drafts_get(key: str) -> tuple[str, str]:
    """Get (draft, requester_user_id), returning ("","") if missing or expired."""
    entry = _review_drafts.get(key)
    if entry is None:
        return "", ""
    draft, requester, ts = entry
    if time.monotonic() - ts > _REVIEW_DRAFT_TTL:
        _review_drafts.pop(key, None)
        return "", ""
    return draft, requester


def _review_drafts_set(key: str, draft: str, requester_user_id: str) -> None:
    """Store a draft with TTL + requester id, evicting oldest if at capacity."""
    now = time.monotonic()
    # Evict expired entries
    expired = [k for k, (_, _, ts) in _review_drafts.items() if now - ts > _REVIEW_DRAFT_TTL]
    for k in expired:
        _review_drafts.pop(k, None)
    # Evict oldest if still at capacity
    if len(_review_drafts) >= _REVIEW_DRAFT_MAX:
        oldest_key = min(_review_drafts, key=lambda k: _review_drafts[k][2])
        _review_drafts.pop(oldest_key, None)
    _review_drafts[key] = (draft, requester_user_id, now)


def _review_drafts_pop(key: str) -> tuple[str, str]:
    """Pop (draft, requester_user_id), returning ("","") if missing or expired."""
    entry = _review_drafts.pop(key, None)
    if entry is None:
        return "", ""
    draft, requester, ts = entry
    if time.monotonic() - ts > _REVIEW_DRAFT_TTL:
        return "", ""
    return draft, requester


def _get_default_agent() -> str:
    """Read persisted default agent, cached to avoid disk I/O on every message."""
    global _cached_default_agent
    if _cached_default_agent is None:
        _cached_default_agent = KiroCrewConfig.load().agent.default_agent
    return _cached_default_agent


def _hydrate_thread_overrides(session_key: str, conversation_log: ConversationLog | None) -> None:
    """Populate in-memory caches from conversation log metadata if not already set."""
    if session_key in _hydrated_sessions:
        return
    _hydrated_sessions.add(session_key)
    if not conversation_log:
        return
    try:
        meta = conversation_log.get_metadata(session_key)
    except Exception:
        logger.debug("Failed to hydrate thread overrides for %s", session_key, exc_info=True)
        return
    if meta.get("agent"):
        _thread_agents[session_key] = meta["agent"]
    if meta.get("project"):
        # Defense-in-depth: re-validate the persisted path at this input
        # boundary. Conversation-log metadata is normally written through the
        # guarded !project handler, but if it is ever corrupted or tampered
        # with, a sensitive credential path (~/.aws, ~/.ssh, …) must never be
        # loaded into the in-memory cache.
        if not is_sensitive_path(meta["project"]):
            _thread_projects[session_key] = meta["project"]
        else:
            logger.warning(
                "Ignoring sensitive project path from thread metadata for %s",
                session_key,
            )


def _get_agent_for_session(session_key: str) -> str:
    """Return agent for a session: thread override first, then global default."""
    return _thread_agents.get(session_key) or _get_default_agent()


def _discover_project_agents(project_dir: str | None) -> list[Path]:
    """Return agent JSON files from <project_dir>/.kiro/ and .kiro/agents/."""
    if not project_dir:
        return []
    if is_sensitive_path(project_dir):
        return []
    kiro_dir = Path(project_dir) / ".kiro"
    if not kiro_dir.is_dir():
        return []
    specs = list(kiro_dir.glob("*.agent-spec.json"))
    agents_dir = kiro_dir / "agents"
    if agents_dir.is_dir():
        specs.extend(agents_dir.glob("*.json"))
    return sorted(specs, key=lambda f: f.stem)


def _resolve_agent_name(name: str, project_dir: str | None = None) -> str | None:
    """Resolve an agent name to its internal name via suffix matching.

    Searches project-local .kiro/ first (if project_dir set), then ~/.kiro/agents/.
    Returns the resolved name, or None if not found.
    """
    # Project-local agents take priority
    for spec in _discover_project_agents(project_dir):
        if spec.stem == name or spec.stem.replace(".agent-spec", "") == name:
            # Fallback must strip the ".agent-spec" suffix: the match arm
            # accepts both "<name>" and "<name>.agent-spec", so returning the
            # raw stem would yield "<name>.agent-spec" — a name that won't
            # resolve downstream. Use the cleaned stem in every fallback branch.
            fallback = spec.stem.removesuffix(".agent-spec")
            raw = safe_read_file_bytes(str(spec))
            if raw is None:
                return fallback
            try:
                return json.loads(raw.decode("utf-8")).get("name", fallback)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return fallback

    agents_dir = Path.home() / ".kiro" / "agents"
    jsons = (
        sorted(agents_dir.glob("*.json"), key=lambda f: (len(f.stem), f.stem))
        if agents_dir.is_dir()
        else []
    )
    match = next(
        (f for f in jsons if f.stem == name or f.stem.endswith(f"-{name}")),
        None,
    )
    if not match:
        # Fallback: search Claude Code cc-plugins agents
        cc_match = _resolve_cc_agent_name(name)
        return cc_match
    safe = validate_file_path(str(match))
    if not safe:
        return None
    try:
        return json.loads(Path(safe).read_text(encoding="utf-8")).get("name", match.stem)
    except (json.JSONDecodeError, OSError):
        return match.stem


# Frontmatter ``name:`` matcher for cc-plugins agent specs. Pre-compiled at
# module level rather than per-iteration inside the agent-file walk below.
_CC_AGENT_NAME_RE = re.compile(r'^name:\s*["\']?([^"\'\n]+)', re.MULTILINE)


def _iter_cc_agent_names(cc_plugins_dir: Path | None = None) -> Iterator[str]:
    """Yield the ``name:`` from each ``~/.aim/cc-plugins/*/agents/*.md`` agent.

    Single source of truth for walking the cc-plugins agent set: reads each
    Markdown file, parses its YAML ``---`` frontmatter, and yields the declared
    agent name (quotes/whitespace stripped). Files that are unreadable, lack
    frontmatter, or omit ``name:`` are skipped. Iterated in sorted path order
    for deterministic output.
    """
    cc_dir = cc_plugins_dir or (Path.home() / ".aim" / "cc-plugins")
    if not cc_dir.is_dir():
        return
    for md_file in sorted(cc_dir.glob("*/agents/*.md")):
        try:
            raw = safe_read_file_bytes(str(md_file))
            if raw is None:
                continue
            content = raw.decode("utf-8")
            if not content.startswith("---"):
                continue
            frontmatter = content[3 : content.index("---", 3)]
            name_match = _CC_AGENT_NAME_RE.search(frontmatter)
            if not name_match:
                continue
            agent_name = name_match.group(1).strip().strip("\"'")
            if agent_name:
                yield agent_name
        except Exception:
            continue


def _resolve_cc_agent_name(name: str, cc_plugins_dir: Path | None = None) -> str | None:
    """Return *name* if a cc-plugins agent declares it, else None."""
    for agent_name in _iter_cc_agent_names(cc_plugins_dir):
        if agent_name == name:
            return agent_name
    return None


def _list_all_agent_names(cc_plugins_dir: Path | None = None) -> str:
    """Return a comma-separated list of all available agent names.

    Merges ``~/.kiro/agents/*.json`` (by stem) with the cc-plugins agents from
    :func:`_iter_cc_agent_names`. The internal ``kirocrew-lite`` variant is
    hidden. Returns ``"(none found)"`` when empty.

    Note: this listing is unioned across both agent sources, but *activation*
    is not. cc-plugins (Claude Code) agents only actually load when
    ``agent.provider=claude_code``; under the kiro-cli provider a ``!ta`` to a
    cc-plugins name resolves and is recorded, but the next kiro session looks
    for ``~/.kiro/agents/<name>.json`` and falls back if it is absent. Switch
    the provider to ``claude_code`` to run cc-plugins agents.
    """
    names: list[str] = []
    agents_dir = Path.home() / ".kiro" / "agents"
    if agents_dir.is_dir():
        # Hide the internal kirocrew-lite variant from BOTH sources — a
        # ~/.kiro/agents/kirocrew-lite.json would otherwise leak into the list.
        names.extend(f.stem for f in sorted(agents_dir.glob("*.json")) if f.stem != "kirocrew-lite")
    seen = set(names)
    for agent_name in _iter_cc_agent_names(cc_plugins_dir):
        if agent_name not in seen and agent_name != "kirocrew-lite":
            names.append(agent_name)
            seen.add(agent_name)
    return ", ".join(names) if names else "(none found)"


def _set_default_agent(name: str) -> None:
    """Persist default agent to config (shared with dashboard)."""
    global _cached_default_agent
    path = config_path()
    if is_sensitive_path(str(path)):
        raise ValueError(f"Refusing to write to sensitive path: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    data.setdefault("agent", {})["default_agent"] = name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(data, indent=2) + "\n")
    except OSError as e:
        raise ValueError(f"Failed to write config: {e}") from e
    _cached_default_agent = name


def _persist_channel_config(
    channel_id: str,
    activation: str | None = None,
    agent: str | None = None,
) -> None:
    """Update a single channel's config in config.json (merge, not overwrite)."""
    path = config_path()
    if is_sensitive_path(str(path)):
        raise ValueError(f"Refusing to write to sensitive path: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    slack_data = data.setdefault("slack", {})
    channels = slack_data.setdefault("channels", {})
    ch = channels.setdefault(channel_id, {})
    if activation is not None:
        ch["activation"] = activation
    if agent is not None:
        ch["agent"] = agent
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(data, indent=2) + "\n")
    except OSError as e:
        raise ValueError(f"Failed to write config: {e}") from e
