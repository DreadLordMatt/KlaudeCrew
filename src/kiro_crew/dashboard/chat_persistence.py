"""Session persistence — save, restore, history prefix."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew import model_registry
from kiro_crew.agent import KIRO_AGENTS_DIR
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.dashboard.chat_utils import (
    _history_key_for,
    _normalize_model,
    _sync_dashboard_slots,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, _normalize_slot_key
from kiro_crew.effort import EFFORT_LEVELS, EFFORT_VALUES
from kiro_crew.history import _archive_lines, update_metadata_off_loop
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import ARTIFACT_SLUG_RE

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterator

    from kiro_crew.history import ConversationLog


def _redact_value(v):  # type: ignore[no-untyped-def]
    """Recursively redact any value (str, dict, list, or passthrough)."""
    if isinstance(v, str):
        v, _ = redact_exfiltration_urls(v)
        v, _ = redact_credentials(v)
        return v
    if isinstance(v, dict):
        return _redact_meta(v)
    if isinstance(v, list):
        return [_redact_value(i) for i in v]
    return v


def _redact_meta(meta: dict) -> dict:
    """Recursively redact string values in meta dict."""
    return {k: _redact_value(v) for k, v in meta.items()}


def _redact_meta_for_role(role: str, meta: dict) -> dict:
    """Redact meta, but preserve role-specific user-actionable external URLs (e.g. mcp_oauth)."""
    if role == "mcp_oauth":
        out: dict = {}
        for k, v in meta.items():
            if k == "oauth_url" and isinstance(v, str):
                # Two gates on rehydrate:
                #   1. http(s)-only — a tampered history line can't smuggle a
                #      javascript:/data: URL into <a href>.
                #   2. URL must not embed a credential or exfil-eligible host —
                #      a legit OAuth consent URL never carries credential
                #      patterns; presence of one means it's tampered/bogus.
                lower = v.lower()
                safe_scheme = lower.startswith("https://") or lower.startswith("http://")
                _, hit_cred = redact_credentials(v)
                _, hit_exfil = redact_exfiltration_urls(v)
                out[k] = v if (safe_scheme and not hit_cred and not hit_exfil) else ""
            else:
                out[k] = _redact_value(v)
        return out
    return _redact_meta(meta)


_MAX_HISTORY_CHARS = 8000

# Bounded retries for taking a consistent (window, _disk_older_count) snapshot
# when _save_slot_to_history runs in the flush executor thread concurrently with
# event-loop mutations (#4). A handful suffices — the only racing mutation is the
# rare >10000-message trim; retries just re-read until the two reads agree.
_FLUSH_SNAPSHOT_RETRIES = 4

# Fallback effort levels — used when no ACP session has reported its config
# yet (cold start). Sourced from the shared ``effort.py`` vocabulary so every
# provider agrees on the levels (incl. "xhigh") and there is a single source of
# truth; ACP overrides these at runtime via update_reasoning_effort_values().
# Order matches natural escalation (low→max) for display purposes.
_REASONING_EFFORT_FALLBACK_ORDER: list[str] = list(EFFORT_LEVELS)
_REASONING_EFFORT_FALLBACK = EFFORT_VALUES

# Runtime state: validation set + ordered list (ACP order preserved).
# Persisted JSON is untrusted input — values flow into a subprocess CLI arg
# (the removed standalone provider's --effort flag) and the ACP /effort slash
# command, so BSC1
# set-membership validation applies on the read path too, not just the API.
_reasoning_effort_values: set[str] = set(_REASONING_EFFORT_FALLBACK)
_reasoning_effort_ordered: list[str] = list(_REASONING_EFFORT_FALLBACK_ORDER)

# Re-exported (back-compat) for any caller importing the static allowlist.
_REASONING_EFFORT_VALUES = EFFORT_VALUES


def get_reasoning_effort_values() -> frozenset[str]:
    """Return currently valid effort levels (ACP-dynamic + fallback)."""
    return frozenset(_reasoning_effort_values)


def get_reasoning_effort_ordered() -> list[str]:
    """Return effort levels in ACP-reported order (excludes empty/default)."""
    return list(_reasoning_effort_ordered)


# Anchored with ``\Z`` (not ``$``) so a value with a trailing newline such as
# "low\n" is rejected — ``$`` would match before the newline and let it through
# to the persistence/subprocess boundary.
_SAFE_EFFORT_RE = re.compile(r"[a-z][a-z0-9_-]{0,20}\Z")


def update_reasoning_effort_values(acp_levels: list[str]) -> None:
    """Update valid effort levels from ACP session config.

    Preserves ACP order for display. The validation set grows monotonically —
    it UNIONS the new levels onto the existing set (and the fallback) and never
    shrinks, so a level that a prior session reported (and that a slot may have
    persisted) stays valid even after another session reports a narrower config.

    Sanitizes input: only lowercase alphanumeric strings pass through
    (defense-in-depth for subprocess boundary).

    Note: ``_reasoning_effort_ordered`` is a process-global *fallback* display
    list only. The dropdown resolves levels per-slot from the slot's live ACP
    provider (see ``api_effort_levels``); this global is served only when no
    live provider is available.
    """
    global _reasoning_effort_values, _reasoning_effort_ordered
    safe_levels = [
        level for level in acp_levels if isinstance(level, str) and _SAFE_EFFORT_RE.match(level)
    ]
    level_set = set(safe_levels)
    # Union-only: never drop a previously-valid level (BSC1 persistence safety).
    merged = _reasoning_effort_values | set(_REASONING_EFFORT_FALLBACK) | level_set | {""}
    ordered = [level for level in safe_levels if level]
    if merged != _reasoning_effort_values or ordered != _reasoning_effort_ordered:
        logger.info("Effort levels updated from ACP: %s", ordered)
        _reasoning_effort_values = merged
        _reasoning_effort_ordered = ordered


def _validate_reasoning_effort(raw: object) -> str:
    """Return *raw* if it's a valid reasoning_effort string, else "".

    Used by the persistence restore paths so a tampered/corrupted
    metadata file cannot smuggle an arbitrary string into the CC
    ``--effort`` subprocess argument.
    """
    if isinstance(raw, str) and raw in _reasoning_effort_values:
        return raw
    if raw:
        logger.warning("Discarding invalid persisted reasoning_effort: %r", raw)
    return ""


def save_all_slots_to_history(state: DashboardState) -> None:
    """Save all active slots to history. Called on gateway shutdown."""
    for slot in list(state._slots.values()):
        try:
            _save_slot_to_history(state, slot, force=True)
        except Exception:
            logger.error("Shutdown: failed to save slot %s", slot.key, exc_info=True)
    # Snapshot the open-tab set so the next startup restores them. This is
    # belt-and-braces vs the periodic flush snapshot — it ensures graceful
    # shutdown captures the very latest state, including tabs whose
    # _dirty was False but were still visually present in the sidebar.
    try:
        state._persist_open_slots()
    except Exception:
        logger.debug("Shutdown: open_slots snapshot failed", exc_info=True)


def _kiro_model_map() -> dict[str, str]:
    """Map kiro agent name/stem -> configured model, for restored slots."""
    kiro_model_map: dict[str, str] = {}
    try:
        for f in KIRO_AGENTS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if data.get("name"):
                    kiro_model_map[data["name"]] = model
                kiro_model_map[f.stem] = model
            except (json.JSONDecodeError, OSError):
                continue
    except Exception:
        logger.debug("Failed to build kiro model map", exc_info=True)
    return kiro_model_map


def _load_restore_cfg() -> "KiroCrewConfig | None":
    try:
        return KiroCrewConfig.load()
    except Exception:
        return None


def _slot_name_from_history_key(key: str) -> str | None:
    """Slot name for a ``list_sessions()`` key, or None if it is not a chat slot.

    ``list_sessions()`` reports the FILENAME-folded form (``dashboard_x``) while
    ``_history_key_for`` produces the canonical colon form (``dashboard:x``), so
    anything keyed off one and looked up by the other silently misses. Both
    prefixes are stripped here and every lookup goes through the slot name.

    The result is folded through ``_normalize_slot_key`` — which also unwraps
    STACKED ``dashboard_dashboard_x`` prefixes — so it equals the key
    ``get_or_create_slot`` will actually register. Returning the unfolded name
    would let a legacy stacked file miss the ``slot_name in state._slots`` dedup
    guard and then replay its whole window onto the canonical slot that guard
    was meant to protect, duplicating that session's messages on the next save.
    """
    if key.startswith("dashboard:"):
        stripped = key.removeprefix("dashboard:")
    elif key.startswith("dashboard_"):
        stripped = key.removeprefix("dashboard_")
    else:
        return None
    return _normalize_slot_key(stripped) or None


@dataclass
class RestorePlan:
    """Everything a restore needs, read from disk with no state touched.

    Produced by :func:`_collect_restore_plan` — deliberately a plain data
    object built from a ``ConversationLog`` rather than a ``DashboardState``, so
    the collect phase *cannot* mutate slots even by accident. That is what makes
    it safe to run in a worker thread while the event loop keeps serving.
    """

    open_keys: list[str]
    """Keys read from open_slots.json, validated and folded. Also the floor."""
    listing: dict[str, dict]
    """slot name -> list_sessions() row, so no applier re-walks the directory."""
    sessions: list[tuple[str, dict, dict]]
    """(slot_name, listing row, metadata) for eligible non-open-tab sessions."""
    kiro_model_map: dict[str, str]
    cfg: "KiroCrewConfig | None"

    @property
    def total(self) -> int:
        return len(self.open_keys) + len(self.sessions)


def _read_open_slots_keys() -> list[str]:
    """Validated, folded keys from open_slots.json. Pure file read, no log needed.

    Split out from :func:`_collect_open_keys` so the startup path can install the
    snapshot floor from these keys BEFORE it yields to the loop. The file holds
    one short string per open tab, so this read is bounded by the tab count
    rather than by accumulated history.
    """
    keys: list[str] = []
    path = config_dir() / "open_slots.json"
    if not path.exists():
        return keys
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("open_slots.json unreadable; skipping", exc_info=True)
        return keys
    raw_keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(raw_keys, list):
        return keys
    for raw in raw_keys:
        if not isinstance(raw, str) or not raw:
            continue
        # Defense-in-depth: slot keys flow into _history_key_for() ->
        # filesystem path construction. open_slots.json is 0o600 so the threat
        # is small, but a key smuggled in (symlink attack at write time or a
        # separate vuln) could escape the sessions directory (e.g.
        # "../../etc/passwd"). Live-gateway slot keys never contain path
        # separators; reject any that do, warn so an attempted breakout is
        # visible, and keep restoring the rest.
        if "/" in raw or "\\" in raw:
            logger.warning("restore_open_slots: rejecting key with path separators: %r", raw)
            continue
        # Fold to the canonical (filename-charset) key. Snapshots written
        # before slot-key normalization landed may carry a raw display-style
        # key (e.g. "Artifact: My Doc") alongside its sanitized twin — after
        # folding, the second form is deduped here instead of restoring a
        # duplicate sidebar session backed by the same transcript.
        folded = _normalize_slot_key(raw)
        if folded and folded not in keys:
            keys.append(folded)
    return keys


def _collect_open_keys(
    log: "ConversationLog", *, keys: list[str] | None = None
) -> tuple[list[str], dict[str, dict]]:
    """Open-tab keys plus the session listing, both read from disk.

    The listing is ALWAYS built — a missing or malformed snapshot yields no open
    keys but still returns the full listing, because the recent-session half of a
    plan reads it too. Returning an empty listing here would silently reduce a
    fresh home's restore to nothing. Pass *keys* to reuse an earlier read.
    """
    listing: dict[str, dict] = {}
    for s in log.list_sessions():
        name = _slot_name_from_history_key(s.get("key", ""))
        if name:
            listing[name] = s
    return (keys if keys is not None else _read_open_slots_keys()), listing


def _collect_recent_sessions(
    log: "ConversationLog",
    window_minutes: int,
    *,
    folders_only: bool,
    listing: dict[str, dict] | None = None,
    skip: set[str] | None = None,
) -> list[tuple[str, dict, dict]]:
    """Filter the session listing down to the sessions a restore should replay.

    Pure I/O plus filtering: mtime cutoff, ``folders_only``, pinned/foldered
    exemption and ``closed``. *skip* holds slot names already covered by the
    open-tab list, so the two halves of a plan never collect the same session
    twice.
    """
    cutoff = time.time() - (window_minutes * 60) if window_minutes > 0 else None
    if listing is None:
        rows = list(log.list_sessions())
    else:
        rows = list(listing.values())
    out: list[tuple[str, dict, dict]] = []
    for s in rows:
        key = s.get("key", "")
        slot_name = _slot_name_from_history_key(key)
        if not slot_name:
            continue
        if skip and slot_name in skip:
            continue
        meta = log.get_metadata(key)
        has_folder = bool(meta.get("folder_id"))
        has_pin = bool(meta.get("pinned"))
        if folders_only and not has_folder and not has_pin:
            continue
        if meta.get("closed"):
            continue
        if not has_folder and not has_pin:
            if cutoff is not None and s.get("modified", 0) < cutoff:
                continue
        out.append((slot_name, s, meta))
    return out


def _collect_restore_plan(
    log: "ConversationLog",
    window_minutes: int,
    *,
    folders_only: bool,
    open_keys: list[str] | None = None,
) -> RestorePlan:
    """Build the whole startup restore plan off the event loop.

    One ``list_sessions()`` walk feeds both halves; the recent-session half
    skips anything the open-tab half already covers.

    *open_keys* reuses a read the caller already did: the startup path reads the
    snapshot first so it can install the floor before it yields to the loop.
    """
    resolved_keys, listing = _collect_open_keys(log, keys=open_keys)
    # No skip= against open_keys. Deduplication happens at APPLY time, against
    # state._slots, and it has to: an open-tab key whose only file on disk is a
    # legacy stacked "dashboard_dashboard_x.jsonl" cannot be resolved by the
    # canonical rehydrate (it looks for dashboard_x.jsonl and finds nothing), so
    # skipping it here as "already covered" would drop that tab from BOTH paths.
    # Collecting it twice costs one extra metadata read off the loop; the
    # open-tab half runs first, so the second pass hits the _slots guard.
    sessions = _collect_recent_sessions(
        log,
        window_minutes,
        folders_only=folders_only,
        listing=listing,
    )
    return RestorePlan(
        open_keys=resolved_keys,
        listing=listing,
        sessions=sessions,
        kiro_model_map=_kiro_model_map(),
        cfg=_load_restore_cfg(),
    )


def restore_open_slots(state: DashboardState) -> int:
    """Restore the tabs the user had open at the previous shutdown.

    Reads ``<config_dir>/open_slots.json`` (written by
    ``DashboardState._persist_open_slots`` on every flush) and rehydrates
    each listed key from on-disk session metadata so it shows up in the
    Sessions sidebar exactly as it did before the restart — independent of
    the ``restore_window_minutes`` mtime cutoff used by
    ``restore_recent_sessions``.

    Path resolves through ``config_dir()`` (honors ``KIROCREW_HOME``) so
    dev/test instances with non-default homes don't read the production
    ``~/.kirocrew`` snapshot.

    Returns the number of slots restored. Missing / malformed file is a
    no-op (returns 0). Sessions that have been explicitly closed
    (``meta.closed``) are skipped via _rehydrate_slot_from_history's own
    guard, so closing a tab and then restarting still loses the tab.
    """
    if not state.conversation_log:
        return 0
    keys, listing = _collect_open_keys(state.conversation_log)
    kiro_model_map = _kiro_model_map()
    restore_cfg = _load_restore_cfg()
    restored = 0
    for raw in keys:
        if raw in state._slots:
            continue
        try:
            slot = _rehydrate_slot_from_history(
                state,
                raw,
                session_info=listing.get(raw, {}),
                kiro_model_map=kiro_model_map,
                restore_cfg=restore_cfg,
            )
        except Exception:
            logger.debug("restore_open_slots: rehydrate failed for %s", raw, exc_info=True)
            _rollback_partial_slot(state, raw)
            continue
        if slot is not None:
            restored += 1
    if restored:
        logger.info("Restored %d open tab(s) from open_slots.json", restored)
    return restored


def _rollback_partial_slot(state: DashboardState, slot_name: str) -> None:
    """Undo a slot registration leaked by a failed rehydrate.

    ``_rehydrate_slot_from_history`` calls ``state.get_or_create_slot()`` BEFORE
    its fallible work (read_messages, redaction, slot.append), so a failure
    there leaves an empty slot registered in ``state._slots``. Without this pop,
    the recent-session pass runs next, hits its ``if slot_name in state._slots``
    dedup guard and skips the proper restore — the user would see a tab with the
    right title/agent but empty or wrong message history.

    It also adds ``dashboard:{slot_name}`` to ``_restricted_keys`` before that
    fallible work for any non-persistent memory_mode. Roll that back too, else a
    later ``get_or_create_slot(slot_name)`` (default ``memory_mode='persistent'``)
    silently inherits restricted status, blocking consolidation/lessons for what
    should be a normal session.
    """
    state._slots.pop(slot_name, None)
    state._restricted_keys.discard(f"dashboard:{slot_name}")


def _attach_variants(slot: _ChatSlot, m: dict) -> None:
    """Copy variant history from a persisted message onto the slot's last message, with redaction."""
    if m.get("variants"):
        slot.messages[-1]["variants"] = [  # type: ignore[assignment]
            {
                **v,
                "content": redact_credentials(redact_exfiltration_urls(v.get("content", ""))[0])[0],
            }
            for v in m["variants"]
            if isinstance(v, dict)
        ]
        slot.messages[-1]["variant_idx"] = m.get("variant_idx", 0)


def _rehydrate_slot_from_history(
    state: DashboardState,
    slot_name: str,
    *,
    messages: list[dict] | None = None,
    session_info: dict | None = None,
    meta: dict | None = None,
    kiro_model_map: dict[str, str] | None = None,
    restore_cfg: "KiroCrewConfig | None" = None,
) -> _ChatSlot | None:
    """Rehydrate a single dashboard slot from persisted history.

    Unlike ``state.get_or_create_slot`` (which creates a fresh, empty slot with
    default ``memory_mode='persistent'``), this helper reads the session's
    metadata and messages from ``conversation_log`` so the restored slot has
    the original title/agent/model/memory_mode and its message history
    populated. Returns ``None`` if the session does not exist on disk (so
    callers can fall through to other delivery paths without creating a
    phantom empty tab).

    Intended for targeted resume paths (e.g. cron→origin injection after
    gateway restart). Bulk startup restore still uses ``restore_recent_sessions``.

    *messages* and *session_info* let a caller supply the two large disk reads
    this would otherwise do inline — the chained message window and this
    session's ``list_sessions()`` row. *kiro_model_map* and *restore_cfg* supply
    the two small but repeated ones: the config load and the agent-directory
    glob, which are per-process facts, not per-session, and would otherwise be
    redone for every key in a bulk restore. The async restore pass passes all
    four — the windows read in a worker thread, the rest once per plan — keeping
    the I/O off the event loop while the slot mutation (loop-affine:
    ``slot.append`` sets an ``asyncio.Event`` and ``get_or_create_slot``
    broadcasts through the SSE queues) still happens on it. Every one defaults
    to the inline read, so standalone callers are unchanged.
    """
    if not state.conversation_log:
        return None
    # Canonicalize to the filename-charset key (idempotent) so callers holding
    # a stale raw display-style key (e.g. a cron's caller_session recorded
    # before slot-key normalization) resolve to the same slot the restore
    # paths create — get_or_create_slot() below applies the same fold.
    slot_name = _normalize_slot_key(slot_name)
    if slot_name in state._slots:
        return state._slots[slot_name]
    history_key = _history_key_for(slot_name)
    if meta is None:
        # get_metadata reads the WHOLE file to take its first line, and the
        # mtime cache is cold on the first read after boot — so on a large
        # session this is a multi-megabyte synchronous read. A bulk restore
        # passes it in, read off the loop alongside the message window.
        meta = state.conversation_log.get_metadata(history_key)
    # No metadata → session was never persisted, or it was deleted while a
    # caller was reading its window. Don't create a phantom slot, and don't let
    # the tab_id backfill below recreate a file the user just deleted.
    if not meta:
        return None
    if meta.get("closed"):
        return None
    _restore_cfg = restore_cfg if restore_cfg is not None else _load_restore_cfg()
    # The same kiro-agent model map restore_recent_sessions builds, so legacy
    # sessions without a persisted `model` still resolve correctly. Both this and
    # the config load above are per-process facts: a bulk restore passes them in
    # once rather than reloading the config and re-globbing the agent directory
    # on the event loop for every single key.
    if kiro_model_map is None:
        kiro_model_map = _kiro_model_map()
    slot = state.get_or_create_slot(slot_name, app=meta.get("app", ""))
    # Pull display fields from session listing for title parity with bulk restore.
    if session_info is None:
        session_info = next(
            (
                s
                for s in state.conversation_log.list_sessions()
                if s.get("key") == history_key
            ),
            {},
        )
    # Titles may have been auto-generated by an LLM (_generate_title_via_kiro)
    # and are surfaced on the dashboard, so apply the same redaction passes
    # used on assistant content before setting. Defence-in-depth — the title
    # author is trusted-ish (our own kiro process), but the generation input
    # is user content, so a prompt injection could craft a title with an
    # exfiltration URL or leaked credential.
    raw_title = session_info.get("title") or meta.get("title") or slot_name
    raw_title, _ = redact_exfiltration_urls(raw_title)
    raw_title, _ = redact_credentials(raw_title)
    slot.title = raw_title
    slot._titled = bool(session_info.get("title") or meta.get("title"))
    if meta.get("created_at"):
        slot.created_at = meta["created_at"]
    if meta.get("agent"):
        slot.agent = meta["agent"]
    if meta.get("model"):
        # _normalize_model handles deprecation renames. For claude_code sessions,
        # also map a pre-migration raw provider id back to the canonical key so it
        # matches the canonical-keyed dropdown (no-op for other providers). Reuse
        # the already-loaded _restore_cfg provider — no second config load.
        _prov = _restore_cfg.agent.provider if _restore_cfg else ""
        slot.model = model_registry.canonicalize_for_provider(
            _normalize_model(meta["model"]), _prov
        )
    elif slot.agent:
        try:
            mc = _restore_cfg.agents.get(slot.agent) if _restore_cfg else None
            kiro_name = mc.kiro_agent if mc and mc.kiro_agent else slot.agent
            slot.model = kiro_model_map.get(kiro_name, "")
        except Exception:
            logger.debug("Failed to resolve model for rehydrated slot %s", slot_name, exc_info=True)
    if meta.get("reasoning_effort"):
        slot.reasoning_effort = _validate_reasoning_effort(meta["reasoning_effort"])
    if meta.get("workspace"):
        slot.workspace = meta["workspace"]
    if meta.get("project"):
        slot.project = meta["project"]
    if meta.get("mode"):
        slot.mode = meta["mode"]
    if meta.get("folder_id"):
        slot.folder_id = meta["folder_id"]
    if meta.get("app"):
        slot._app = meta["app"]
    # Re-validate the companion binding against the slug grammar on restore
    # (same gate as slot create) — history JSONL is a file an attacker with
    # disk access could tamper, and this value flows into to_dict()/WS
    # broadcasts to every connected dashboard client.
    _artifact_meta = meta.get("artifact")
    if isinstance(_artifact_meta, str) and ARTIFACT_SLUG_RE.match(_artifact_meta):
        slot._artifact = _artifact_meta
    if meta.get("pinned"):
        slot.pinned = True
    if meta.get("color_index") is not None:
        slot.color_index = meta["color_index"]
    raw_tags = meta.get("tags")
    if isinstance(raw_tags, list):
        slot.tags = [str(t) for t in raw_tags if isinstance(t, str) and t]
    mm = meta.get("memory_mode", "persistent")
    slot.memory_mode = mm
    if mm != "persistent":
        state._restricted_keys.add(f"dashboard:{slot_name}")
    if meta.get("forked_from") is not None:
        slot.forked_from = meta["forked_from"]
    # Restore the persisted tab_id so cross-restart fork chaining survives.
    # get_or_create_slot (called by our caller) assigns a fresh random uuid to
    # slot._tab_id; if we don't overwrite it here, the next _flush_dirty_slots
    # persists that uuid back into meta, severing the tab_id ancestry that
    # read_messages_chained walks across forks — one restart + one flush
    # permanently loses forked-session history. Mirrors restore_recent_sessions.
    tab_id = meta.get("tab_id")
    if not tab_id:
        tab_id = uuid.uuid4().hex[:12]
        # _rehydrate_slot_from_history runs on the event-loop thread (cold-slot
        # resolution in api_send_message). update_metadata enters _locked
        # (flock + os.close), a blocking-on-loop-prohibited op, so backfill the
        # tab_id off the loop rather than on it.
        update_metadata_off_loop(
            state.conversation_log, history_key, {"tab_id": tab_id}
        )
    slot._tab_id = tab_id
    # Use read_messages_chained (not read_messages) so the loaded window walks
    # the tab_id ancestry across forks, matching restore_recent_sessions.
    # read_messages alone caps visible history at 200 lines from THIS file and
    # drops the ancestor chain — long-running forked sessions would lose 200+
    # messages of context on every gateway restart.
    messages = (
        state.conversation_log.read_messages_chained(history_key)
        if messages is None
        else messages
    )
    # Only the recent window is loaded into memory; older on-disk lines become
    # the FROZEN PREFIX that saves never rewrite. _disk_older_count must
    # therefore count those older lines so the save model preserves them.
    slot._disk_older_count = max(0, len(messages) - 500)
    for m in messages[-500:]:
        role = m.get("role", "assistant")
        cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = m.get("content", "")
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            meta=(
                _redact_meta_for_role(role, m["meta"]) if isinstance(m.get("meta"), dict) else None
            ),
        )
        _attach_variants(slot, m)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    # The whole in-memory window is already on disk → it is the on-disk window
    # region. Saves re-serialize the window in place; the frozen prefix (older
    # turns counted above) is never rewritten.
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    logger.info("Rehydrated session %s (%s) from history", slot_name, slot.title)
    return slot


def _apply_recent_session(
    state: DashboardState,
    s: dict,
    slot_name: str,
    meta: dict,
    kiro_model_map: dict[str, str],
    _restore_cfg: "KiroCrewConfig | None",
    *,
    messages: list[dict] | None = None,
) -> _ChatSlot:
    """Materialize one listed session as a chat slot (metadata + message window).

    The expensive half of a recent-session restore, split out so the async pass
    replays the *same* code path as the sync wrapper rather than a parallel copy.
    Callers own filtering, the restored counter and the log line.

    *messages* lets the caller supply the chained window instead of reading it
    here, so the async pass can do that read in a worker thread while the slot
    mutation (loop-affine) stays on the event loop.
    """
    key = s.get("key", "")
    log = state.conversation_log
    if log is None:
        # Unreachable via either caller (both guard on conversation_log), but the
        # attribute is Optional and this function reads through it twice.
        raise RuntimeError("restore requires a conversation log")
    slot = state.get_or_create_slot(slot_name, app=meta.get("app", ""))
    # Titles can be LLM-generated (auto-title) and are surfaced on the
    # dashboard — apply the same redaction as assistant content. Matches
    # the treatment in _rehydrate_slot_from_history above.
    raw_title = s.get("title", slot_name)
    raw_title, _ = redact_exfiltration_urls(raw_title)
    raw_title, _ = redact_credentials(raw_title)
    slot.title = raw_title
    slot._titled = bool(s.get("title"))
    if meta.get("created_at"):
        slot.created_at = meta["created_at"]
    if meta.get("agent"):
        slot.agent = meta["agent"]
    if meta.get("model"):
        # Canonicalize a pre-migration claude_code provider id to the
        # canonical dropdown key (no-op for other providers); reuse the
        # already-loaded _restore_cfg provider.
        _prov = _restore_cfg.agent.provider if _restore_cfg else ""
        slot.model = model_registry.canonicalize_for_provider(
            _normalize_model(meta["model"]), _prov
        )
    elif slot.agent:
        try:
            mc = _restore_cfg.agents.get(slot.agent) if _restore_cfg else None
            kiro_name = mc.kiro_agent if mc and mc.kiro_agent else slot.agent
            slot.model = kiro_model_map.get(kiro_name, "")
        except Exception:
            logger.debug(
                "Failed to resolve model for restored slot %s", slot_name, exc_info=True
            )
    if meta.get("reasoning_effort"):
        slot.reasoning_effort = _validate_reasoning_effort(meta["reasoning_effort"])
    if meta.get("workspace"):
        slot.workspace = meta["workspace"]
    if meta.get("project"):
        slot.project = meta["project"]
    if meta.get("mode"):
        slot.mode = meta["mode"]
    if meta.get("folder_id"):
        slot.folder_id = meta["folder_id"]
    if meta.get("app"):
        slot._app = meta["app"]
    # Same tamper gate as _rehydrate_slot_from_history: re-validate the
    # companion binding against the slug grammar before it reaches
    # to_dict()/WS broadcasts.
    _artifact_meta = meta.get("artifact")
    if isinstance(_artifact_meta, str) and ARTIFACT_SLUG_RE.match(_artifact_meta):
        slot._artifact = _artifact_meta
    if meta.get("pinned"):
        slot.pinned = True
    if meta.get("color_index") is not None:
        slot.color_index = meta["color_index"]
    if meta.get("color_theme"):
        slot.color_theme = meta["color_theme"]
    raw_tags = meta.get("tags")
    if isinstance(raw_tags, list):
        slot.tags = [str(t) for t in raw_tags if isinstance(t, str) and t]
    mm = meta.get("memory_mode", "persistent")
    slot.memory_mode = mm
    if mm != "persistent":
        state._restricted_keys.add(f"dashboard:{slot_name}")
    if meta.get("forked_from") is not None:
        slot.forked_from = meta["forked_from"]
    tab_id = meta.get("tab_id")
    if not tab_id:
        tab_id = uuid.uuid4().hex[:12]
        # This runs with the event loop live, and update_metadata enters
        # _locked (flock + os.close) — a blocking-on-loop-prohibited op, so
        # backfill the tab_id off the loop rather than on it.
        update_metadata_off_loop(log, key, {"tab_id": tab_id})
    slot._tab_id = tab_id
    if messages is None:
        messages = log.read_messages_chained(key)
    slot._disk_older_count = max(0, len(messages) - 500)
    for m in messages[-500:]:
        role = m.get("role", "assistant")
        cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = m.get("content", "")
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            meta=(
                _redact_meta_for_role(role, m["meta"])
                if isinstance(m.get("meta"), dict)
                else None
            ),
        )
        _attach_variants(slot, m)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    # Loaded window is the on-disk window region; older lines (counted in
    # _disk_older_count above) are the frozen prefix saves never rewrite.
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    return slot


def restore_recent_sessions(
    state: DashboardState, window_minutes: int = 30, *, folders_only: bool = False
) -> int:
    """Restore sessions as chat slots (synchronous, restores everything).

    Startup uses :func:`restore_sessions_at_startup` instead, which does the
    same work with the reads off the event loop. This wrapper stays for targeted
    and test callers that want the whole thing done by the time it returns.
    """
    if not state.conversation_log:
        return 0
    kiro_model_map = _kiro_model_map()
    _restore_cfg = _load_restore_cfg()
    restored = 0
    for slot_name, s, meta in _collect_recent_sessions(
        state.conversation_log, window_minutes, folders_only=folders_only
    ):
        if slot_name in state._slots:
            continue
        slot = _apply_recent_session(state, s, slot_name, meta, kiro_model_map, _restore_cfg)
        restored += 1
        logger.info("Restored session %s (%s)", slot_name, slot.title)
    _sync_dashboard_slots(state)
    return restored


async def restore_sessions_at_startup(
    state: DashboardState, window_minutes: int, *, folders_only: bool = False
) -> int:
    """Restore the user's tabs and recent sessions without stalling the loop.

    Startup rehydration used to run unbounded and synchronously while the
    loop-stall watchdog was already armed: the heartbeat could not be petted, so
    ``LoopStallWatchdog`` ``_exit(1)``'d the gateway mid-boot on any home with
    enough history, and the failure got more certain as history accumulated.

    Two changes close that outright rather than moving its threshold:

    * The whole eligibility scan (``list_sessions()`` + per-session metadata +
      filtering) runs in a worker thread via :func:`_collect_restore_plan`,
      which takes a ``ConversationLog`` rather than the state and so cannot
      mutate slots.
    * The replay then runs as a background task that yields to the loop between
      sessions and reads each message window off-loop, leaving only bounded CPU
      (redact + append of at most 500 in-memory messages) on the loop per
      session. No session shape can stretch that to the 25s watchdog budget.

    The collect is awaited rather than backgrounded so the slot counter is
    reseeded past every known key BEFORE this returns — a new chat minted while
    the replay is still running must not take an index a returning tab is about
    to claim.

    Returns the number of sessions the plan will restore (the replay itself
    finishes in the background).
    """
    log = state.conversation_log
    if not log:
        return 0
    # Install the snapshot floor BEFORE the first await, not after the scan.
    # The scan yields the loop, and the 5s flush fires during it with _slots
    # still empty — so installing the floor afterwards would let that flush
    # write an empty open_slots.json over the very file the floor exists to
    # protect, permanently dropping every old tab. Reading the snapshot here is
    # bounded by the tab count (one short string per tab), not by history, so it
    # is safe to do inline; the expensive walk stays off the loop below.
    open_keys = _read_open_slots_keys()
    state._restore_floor = tuple(open_keys)
    # Reserve the open-tab keys here too, for the same reason and in the same
    # breath: the scan below yields, and a returning browser tab holds exactly
    # these keys in its local state, so it is the most likely thing to arrive
    # during it. Without the reservation that request registers an EMPTY slot,
    # the replay skips it, and a later flush writes the empty window over the
    # transcript. The recent-session keys are not knowable until the scan
    # returns, and are added below.
    state._pending_restore_keys.update(open_keys)
    plan = await asyncio.to_thread(
        _collect_restore_plan,
        log,
        window_minutes,
        folders_only=folders_only,
        open_keys=open_keys,
    )
    state.reseed_slot_counter([*plan.open_keys, *(name for name, _, _ in plan.sessions)])
    # Reserve the remaining planned keys before the replay is scheduled. Until
    # the replay reaches a key, any request handler that creates from a
    # caller-supplied key would otherwise register an EMPTY slot under it, which
    # a later flush writes over the real transcript. With the reservation in
    # place, such a handler awaits ensure_pending_slot_restored() and gets the
    # real session instead. The open-tab keys were reserved above, pre-scan.
    state._pending_restore_keys.update(plan.open_keys)
    state._pending_restore_keys.update(name for name, _, _ in plan.sessions)
    # Publish the listing the scan already paid for, so an on-demand replay gets
    # its title-parity row for free instead of re-walking the session directory.
    # The config and model map ride along for the same reason: both are
    # per-process, and re-deriving them per request would glob the agent
    # directory on the loop.
    state._restore_shared = (plan.listing, plan.kiro_model_map, plan.cfg)
    if plan.total:
        task = asyncio.create_task(_run_restore_plan(state, plan))
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    else:
        state._restore_floor = ()
    return plan.total


async def _run_restore_plan(state: DashboardState, plan: RestorePlan) -> int:
    """Replay a :class:`RestorePlan`, one session per loop iteration.

    Every step is best-effort: a session that fails to rehydrate is logged and
    skipped, and slots that appeared in the meantime (a client resumed the tab,
    or the other half of the plan got there first) are left alone.
    """
    log = state.conversation_log
    if not log:
        return 0
    restored = 0
    failed: set[str] = set()
    # Slot names the RECENT half will replay. A tab backed only by a legacy
    # stacked file appears in both halves: its folded name is in open_keys, but
    # the canonical history key has no file, so only the listing row in
    # plan.sessions can actually restore it. The open-tab half must therefore not
    # release such a key when it finishes empty-handed.
    deferred_to_recent = {name for name, _, _ in plan.sessions}
    logger.info("Restoring %d session(s) in the background", plan.total)

    for raw in plan.open_keys:
        await asyncio.sleep(0)
        if raw in state._slots:
            state._pending_restore_keys.discard(raw)
            continue
        history_key = _history_key_for(raw)
        try:
            window = await asyncio.to_thread(log.read_messages_chained, history_key)
            # Read metadata off-loop too, and AFTER the window: get_metadata
            # reads the whole file to take its first line, and reading it last
            # means a session deleted during the window read comes back empty
            # here, so the rehydrate returns None instead of resurrecting it.
            meta = await asyncio.to_thread(log.get_metadata, history_key)
            # Re-check after the awaits: a client can resume this tab while the
            # reads are in flight, and _rehydrate_slot_from_history would then
            # hand back the live slot — counted here as a restore that never
            # happened.
            if raw in state._slots:
                continue
            with _replaying(state, raw):
                slot = _rehydrate_slot_from_history(
                    state,
                    raw,
                    messages=window,
                    meta=meta,
                    session_info=plan.listing.get(raw, {}),
                    kiro_model_map=plan.kiro_model_map,
                    restore_cfg=plan.cfg,
                )
            if slot is not None:
                restored += 1
        except Exception:
            logger.debug("restore: rehydrate failed for %s", raw, exc_info=True)
            failed.add(raw)
            _rollback_partial_slot(state, raw)
        finally:
            # Release the reservation only once this key is SETTLED. Holding it
            # across the off-loop reads is the point: a request arriving mid-read
            # rehydrates on demand instead of registering an empty slot, and the
            # post-await _slots re-check above then skips it here.
            #
            # Two states are not settled, and both have to be RE-ASSERTED rather
            # than merely left alone, because _replaying claims the key on entry:
            #
            # * the recent half still owns it — a stacked-only tab restores
            #   nothing here, so only its listing row can, and
            # * this key FAILED — the transcript is intact on disk, so letting a
            #   request register an empty slot over it is the data loss the
            #   reservation exists to prevent. A retained key means requests for
            #   it get a retryable 503 until a retry succeeds, which is the right
            #   trade against destroying the session.
            #
            # There is no await between the _replaying claim and this line, so no
            # request can observe the intermediate state.
            if raw not in state._slots and (raw in failed or raw in deferred_to_recent):
                state._pending_restore_keys.add(raw)
            else:
                state._pending_restore_keys.discard(raw)

    for slot_name, s, _collected_meta in plan.sessions:
        await asyncio.sleep(0)
        if slot_name in state._slots:
            state._pending_restore_keys.discard(slot_name)
            continue
        key = s.get("key", "")
        try:
            window = await asyncio.to_thread(log.read_messages_chained, key)
            # Metadata is re-read AFTER the window, not reused from the plan, so
            # a session deleted or closed while the read was in flight is caught
            # here: empty metadata means the JSONL is gone, and applying stale
            # metadata would let the tab_id backfill recreate the file the user
            # just deleted. No await between this check and the apply.
            meta = await asyncio.to_thread(log.get_metadata, key)
            if not meta or meta.get("closed"):
                continue
            if slot_name in state._slots:
                continue
            with _replaying(state, slot_name):
                _apply_recent_session(
                    state, s, slot_name, meta, plan.kiro_model_map, plan.cfg, messages=window
                )
            restored += 1
        except Exception:
            logger.debug("restore: session %s failed", slot_name, exc_info=True)
            # Retain the floor entry, same rule as the open-tab half: since the
            # open-key skip was dropped, a tab backed only by a legacy stacked
            # file arrives here, and its folded slot name can be a floor key.
            # Releasing it on a transient read error would let the next snapshot
            # write an open_slots.json without it and lose the tab for good.
            failed.add(slot_name)
            _rollback_partial_slot(state, slot_name)
        finally:
            # Same hold-across-the-reads rule as the open-tab half above, and the
            # same re-assert on failure: a key whose restore raised still has its
            # transcript on disk, so releasing it would let the next request
            # register an empty slot over that session.
            if slot_name in failed and slot_name not in state._slots:
                state._pending_restore_keys.add(slot_name)
            else:
                state._pending_restore_keys.discard(slot_name)

    # Release the floor for everything this pass SETTLED — restored (now in
    # _slots and snapshotted on its own merit), already live, or absent from
    # disk (a rehydrate that returns None cleans a dead entry out of the file).
    # A key whose restore RAISED stays in the floor: the failure may be transient
    # (a read error under load), and releasing it would let the next flush write
    # an open_slots.json without it and lose the tab for good. Worst case a
    # permanently broken key stays in the file for this gateway's lifetime — one
    # dead entry, which the collect phase already tolerates, and no lost data.
    #
    # Deliberately NOT in a finally: if this task is cancelled (gateway shutdown
    # mid-replay) or raises, the WHOLE floor must stay so the shutdown snapshot
    # still records every tab that never got its turn.
    state._restore_floor = tuple(k for k in state._restore_floor if k in failed)
    # No key is pending now, so nothing can consult these again. An on-demand
    # replay still in flight falls back to its own off-loop reads.
    state._restore_shared = None
    _sync_dashboard_slots(state)
    logger.info("Restore complete: %d/%d session(s)", restored, plan.total)
    return restored


@contextmanager
def _replaying(state: DashboardState, slot_name: str) -> "Iterator[None]":
    """Hide a slot from the persistence sweeps while its window is replayed.

    ``get_or_create_slot`` registers the slot before the replay fills it, and
    ``slot.append`` marks it ``_dirty`` — so a 5s flush or a shutdown save
    landing inside the replay would persist a partial window whose
    ``_disk_older_count`` / ``_disk_window_len`` still describe the pre-replay
    slot, and the next flush would duplicate those lines. Skipping the save is
    lossless: the window being replayed was read from that same file.

    Entering also CLAIMS the key out of ``_pending_restore_keys``, marking it
    settled: the bulk pass and an on-demand replay can both be holding this key
    across their reads, and the first one to reach this point owns the apply.
    There is no ``await`` between either caller's post-read ``_slots`` re-check
    and this claim, so the loser's re-check sees the registered slot and
    discards its own copy of the window rather than appending it twice.
    """
    state._pending_restore_keys.discard(slot_name)
    state._restoring_keys.add(slot_name)
    try:
        yield
    finally:
        state._restoring_keys.discard(slot_name)


def _listing_row(state: DashboardState, log: "ConversationLog", slot_name: str) -> dict:
    """This session's ``list_sessions()`` row, for title parity with the bulk pass.

    Prefers the row the startup scan already collected. The fallback walk is
    only reached when the pass has finished and cleared the listing, and it is
    the caller's job to run this off the loop.
    """
    shared = getattr(state, "_restore_shared", None)
    if shared is not None:
        row = shared[0].get(slot_name)
        if row is not None:
            return row
    history_key = _history_key_for(slot_name)
    return next((s for s in log.list_sessions() if s.get("key") == history_key), {})


async def ensure_pending_slot_restored(state: DashboardState, name: str | None) -> None:
    """Replay a key the startup plan owns but has not reached yet, off the loop.

    Await this from an async handler BEFORE ``get_or_create_slot`` whenever the
    key is caller-supplied and the handler does not load the window itself.
    Registering an empty slot under a planned key destroys data: the slot has
    ``_disk_older_count=0`` and ``_disk_window_len=0``, ``_restoring_keys`` does
    not cover it, and the next 5s flush writes that empty window over the
    session's transcript.

    ``get_or_create_slot`` deliberately does not do this itself. It cannot know
    whether its caller is about to rehydrate: the resume handler does, and
    handing it a populated slot made it append the same window a second time and
    flush a doubled transcript. Putting the decision at the call site makes the
    two cases distinguishable, and it also lets the reads run in a worker thread
    while only the apply — which touches ``asyncio.Event`` and the SSE queues —
    stays on the loop.

    Returns nothing: the caller's own ``get_or_create_slot`` then finds the
    restored slot, or creates the fresh one it asked for if the session is
    genuinely gone from disk.

    Raises ``web.HTTPServiceUnavailable`` if the restore FAILED (a transient read
    error, say). Failing the request is the only safe outcome: returning would
    let the caller create an empty slot over an intact transcript, which the next
    flush destroys. The reservation is retained so the bulk pass still replays
    the key, and the status is retryable because the next attempt usually wins.
    """
    if not name:
        return
    slot_name = _normalize_slot_key(name)
    if not slot_name or slot_name in state._slots:
        return
    if slot_name not in getattr(state, "_pending_restore_keys", ()):
        return
    tasks = getattr(state, "_pending_rehydrate_tasks", None)
    if tasks is None:
        tasks = state._pending_rehydrate_tasks = {}
    task = tasks.get(slot_name)
    if task is None:
        # A task rather than a bare await, for two reasons: a client that
        # disconnects mid-read must not abandon a half-applied replay, and the
        # next request for this key joins this same one instead of starting a
        # second read of the same window. 67 returning tabs are 67 keys, not 67
        # reads of one key.
        task = asyncio.ensure_future(_replay_one_key(state, slot_name))
        tasks[slot_name] = task
    # shield: cancelling THIS request (client went away) must not cancel a
    # replay that other requests are waiting on, or that is mid-apply.
    try:
        await asyncio.shield(task)
    except Exception as exc:
        raise web.HTTPServiceUnavailable(
            reason=f"session {slot_name} is still being restored"
        ) from exc


async def _replay_one_key(state: DashboardState, slot_name: str) -> None:
    """One-key mirror of the open-tab step in :func:`_run_restore_plan`."""
    log = state.conversation_log
    if log is None:
        return
    info = await asyncio.to_thread(_listing_row, state, log, slot_name)
    # Read from the key the LISTING reports, not the canonical fold. A tab backed
    # only by a legacy stacked file (``dashboard_dashboard_x``) has no canonical
    # file, so reading the folded key would come back empty and the caller would
    # then create an empty slot over a session that does exist on disk.
    history_key = info.get("key") or _history_key_for(slot_name)
    # Per-process facts, taken from the plan when the replay published them. The
    # fallbacks run in a worker thread because _kiro_model_map globs the agent
    # directory and _load_restore_cfg reads config from disk — deriving either on
    # the loop is the read class this PR exists to remove.
    shared = getattr(state, "_restore_shared", None)
    if shared is not None:
        _, model_map, cfg = shared
    else:
        model_map = await asyncio.to_thread(_kiro_model_map)
        cfg = await asyncio.to_thread(_load_restore_cfg)
    try:
        # Same read order as the bulk pass: window first, metadata second, so a
        # session deleted while the window was in flight comes back with empty
        # metadata and the rehydrate declines instead of resurrecting the file.
        window = await asyncio.to_thread(log.read_messages_chained, history_key)
        meta = await asyncio.to_thread(log.get_metadata, history_key)
        # The reservation is held across those reads rather than claimed up
        # front, exactly as the bulk pass holds it, so the bulk pass may be
        # replaying this same key concurrently. Re-check _slots and keep the
        # apply await-free: whoever registers the slot first owns it and the
        # loser drops its window here. The cost of losing is one wasted read;
        # the cost of not checking would be a doubled transcript.
        if slot_name in state._slots:
            state._pending_restore_keys.discard(slot_name)
            return
        with _replaying(state, slot_name):
            _rehydrate_slot_from_history(
                state,
                slot_name,
                messages=window,
                meta=meta,
                session_info=info,
                kiro_model_map=model_map,
                restore_cfg=cfg,
            )
        state._pending_restore_keys.discard(slot_name)
    except Exception:
        logger.warning("on-demand restore failed for %s", slot_name, exc_info=True)
        _rollback_partial_slot(state, slot_name)
        # RETAIN the reservation and re-raise, rather than settling the key.
        # Swallowing here let the caller's get_or_create_slot register an empty
        # slot under a key whose transcript is intact on disk, and the next
        # flush wrote that empty window over it — the exact data loss this
        # function exists to prevent, reached through a transient read error.
        # "The bulk pass will retry it" only holds while no slot exists: once
        # one does, the bulk pass skips the key. Re-added explicitly because
        # _replaying already claimed it on entry.
        state._pending_restore_keys.add(slot_name)
        raise
    finally:
        # Always clear the in-flight entry, so a later request can retry a key
        # whose first attempt failed.
        tasks = getattr(state, "_pending_rehydrate_tasks", None)
        if tasks is not None:
            tasks.pop(slot_name, None)


def ensure_restored_before_inject(
    state: DashboardState, name: str | None, retry: "Callable[[], object]"
) -> bool:
    """Gate a non-async, loop-resident caller on a pending key's restore.

    The cron and workflow injectors run on the event loop but are plain
    functions, called from the Slack gateway and the workflow watcher, so they
    cannot await. They still must not register an empty slot under a key the
    startup replay owns, because the next flush writes that empty window over
    the session's transcript.

    Reading inline would put an unbounded history-scaled read back on the loop —
    the very thing this PR removes. So when the key is still pending this
    schedules the off-loop restore and re-runs *retry* (the caller itself) once
    it lands. The second entry finds the key settled and proceeds normally, so
    there is no recursion beyond one hop.

    Returns True when the work was deferred, meaning the caller must return
    immediately and do nothing else.

    With no running loop there is no loop to protect and no watchdog armed, so
    the restore happens inline and this returns False.
    """
    if not name:
        return False
    slot_name = _normalize_slot_key(name)
    if not slot_name or slot_name in state._slots:
        return False
    if slot_name not in getattr(state, "_pending_restore_keys", ()):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _restore_pending_slot_inline(state, slot_name)
        return False

    async def _restore_then_retry() -> None:
        try:
            await ensure_pending_slot_restored(state, slot_name)
        except Exception:
            # Deliberately do NOT fall through to the injection: that would put
            # an empty slot over the transcript. The result is still on the job
            # record and the notification path is unaffected.
            logger.warning(
                "deferred restore failed for %s; skipping injection", slot_name, exc_info=True
            )
            return
        retry()

    task = loop.create_task(_restore_then_retry())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return True


def _restore_pending_slot_inline(state: DashboardState, slot_name: str) -> None:
    """Inline restore for the no-running-loop case only. See the caller."""
    # Reuse whatever the replay published, so this does not re-derive per-process
    # facts either. Each kwarg defaults to an inline read when absent.
    shared = getattr(state, "_restore_shared", None)
    try:
        with _replaying(state, slot_name):
            _rehydrate_slot_from_history(
                state,
                slot_name,
                kiro_model_map=shared[1] if shared is not None else None,
                restore_cfg=shared[2] if shared is not None else None,
            )
    except Exception:
        logger.warning("inline restore failed for %s", slot_name, exc_info=True)
        _rollback_partial_slot(state, slot_name)
        # Same rule as the async path: retain the reservation on failure rather
        # than settling a key whose transcript is still intact on disk.
        state._pending_restore_keys.add(slot_name)
    else:
        state._pending_restore_keys.discard(slot_name)


def _diff_dropped_message_lines(old_lines: list[str], new_lines: list[str]) -> list[str]:
    """Return existing message lines that *new_lines* would drop.

    Both inputs are full file-line lists (metadata line at index 0, which is
    skipped on both sides). Compares by normalized JSON (``sort_keys``, so a
    key-order change is not a spurious drop). Corrupted/unparseable old lines
    are treated as dropped (archived). This is the same drop-detection rule
    ``ConversationLog.rewrite_session`` applies; it is factored out here so the
    dashboard rewrite path and ``rewrite_session`` share one definition.
    """
    if old_lines and '"_type"' in old_lines[0]:
        old_lines = old_lines[1:]
    kept_serialized: set[str] = set()
    for ln in new_lines[1:]:
        if not ln.strip():
            continue
        try:
            kept_serialized.add(json.dumps(json.loads(ln), sort_keys=True))
        except (json.JSONDecodeError, ValueError):
            continue
    dropped: list[str] = []
    for ln in old_lines:
        if not ln.strip():
            continue
        try:
            normalized = json.dumps(json.loads(ln), sort_keys=True)
        except (json.JSONDecodeError, ValueError):
            dropped.append(ln)  # corrupted line → archive it
            continue
        if normalized not in kept_serialized:
            dropped.append(ln)
    return dropped


def _archive_dropped_lines(
    state: DashboardState, history_key: str, old_lines: list[str], new_lines: list[str]
) -> None:
    """Archive on-disk message lines that *new_lines* (full file) would drop.

    Used only by the rewrite path (rewind/regenerate/fork), which intentionally
    truncates the in-memory window. The frozen prefix is present unchanged in
    both *old_lines* and *new_lines*, so it is never archived — only the dropped
    window tail is. No-op in the steady-state superset case.
    """
    dropped = _diff_dropped_message_lines(old_lines, new_lines)
    if not dropped:
        return
    base = state.conversation_log._dir if state.conversation_log else None
    _archive_lines(history_key, dropped, reason="compact", base=base)


def _build_message_entry(m: dict) -> dict | None:
    """Build one persisted JSONL message dict from an in-memory slot message.

    Returns None for transient roles that are never persisted. Applies the
    same redaction the overwrite path used so append and rewrite produce
    byte-identical lines for the same message.
    """
    role = m.get("role", "assistant")
    if role in ("chunk", "done", "streaming", "queued", "permission"):
        return None
    content = m.get("content", "")
    if role not in ("user", "system"):
        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)
    entry: dict = {
        "role": role,
        "content": content,
        "ts": m.get("ts", ""),
        "source_thread": "dashboard",
        "source_user": "dashboard",
    }
    if m.get("variants"):
        redacted_variants: list[dict] = []
        for v in m["variants"]:
            if not isinstance(v, dict):
                continue
            vc = v.get("content", "")
            vc, _ = redact_exfiltration_urls(vc)
            vc, _ = redact_credentials(vc)
            redacted_variants.append({**v, "content": vc})
        entry["variants"] = redacted_variants
        entry["variant_idx"] = m.get("variant_idx", 0)
    cls_val = m.get("cls", "")
    if role == "system" and cls_val:
        entry["cls"] = cls_val
    if isinstance(m.get("meta"), dict):
        entry["meta"] = _redact_meta_for_role(role, m["meta"])
    return entry


# Transient/streaming roles that are never persisted (mirrors
# ``_build_message_entry``). A window-region disk line carrying one of these is
# not a real message and is never treated as a cross-process append to preserve.
_TRANSIENT_ROLES = frozenset({"chunk", "done", "streaming", "queued", "permission"})


def _frozen_prefix_and_foreign_appends(
    slot: _ChatSlot,
    path,
    disk_older: int,
    window_entries: list[dict],
    *,
    collect_foreign: bool = True,
) -> tuple[str, list[str], list[str]]:
    """Return ``(frozen_prefix, foreign_lines, dedup_dropped)`` for a save.

    ``frozen_prefix`` is the verbatim bytes of the first *disk_older* on-disk
    message lines — the turns OLDER than the in-memory window. They are never
    rewritten, so older history survives a restart that only loaded a recent
    window. The bytes are cached on the slot keyed by ``(mtime, size,
    disk_older)`` so a steady 5s flush is O(window) rather than O(file size)
    (#5).

    ``foreign_lines`` are on-disk message lines in the WINDOW region (the bytes
    after the frozen prefix) that this slot's in-memory *window_entries* do NOT
    represent — i.e. acknowledged appends made by ANOTHER process (subagent /
    cron / CLI) that this slot never saw. ``_save_slot_to_history`` captures its
    ``window`` snapshot BEFORE taking ``_locked``, so a cross-process writer can
    fully append + release between the snapshot and this save acquiring the lock;
    a bare ``meta + frozen + window`` replace would then silently delete that
    acknowledged message. Carrying these lines into the payload makes the save
    non-destructive against cross-process appends (the data-loss finding). A
    disk line is treated as ours (dropped; the window re-serializes it) when its
    ``ts`` matches a window entry (covers in-place edits, which keep ``ts`` but
    change content) OR — as a COUNT-BOUNDED tiebreak — its ``(role, content)``
    matches an as-yet-unconsumed window entry (covers a same-process
    ``append_if_absent`` copy persisted with a FRESH ``ts`` distinct from the
    window entry's in-memory ``ts``). The tiebreak is bounded so each window
    entry absorbs AT MOST ONE disk copy: if the on-disk window region holds two
    lines with identical ``(role, content)`` but distinct timestamps — the
    window's own persisted copy PLUS a genuinely distinct event from another
    process (e.g. a repeated identical cron / workflow result) — only the first
    is folded into the window and the second is preserved as a foreign append.
    A plain ``(role, content)`` set collapsed those two real events into one
    (the GPT 5.6 HIGH data-loss finding); the bounded, timestamp-first identity
    fixes it. Timestamp is the closest thing to a stable per-message id today;
    the intended successor is a creation-time per-message uuid that demotes this
    heuristic to a legacy fallback for un-stamped lines — tracked as a committed
    follow-up in https://github.com/kirodotdev/KiroCrew/issues/381 (see also
    ``docs/system-specs/modules/history.md``). ``dedup_dropped`` returns any
    fresh-``ts`` content-tiebreak drops so the caller can route them through the
    archive — even the residual ambiguous case (a distinct message
    indistinguishable from an ``append_if_absent`` copy without a stable id)
    then loses no data permanently.

    Fast path (#5): when BOTH the on-disk mtime AND size match the frozen-prefix
    cache, THIS slot was the last writer and nothing has landed since, so the
    prefix is served from cache and the foreign lines preserved by the previous
    save are re-emitted verbatim from cache — the O(file) read/scan runs ONLY
    when the file changed on disk since our last write. Size is part of the key
    because an append always grows the file even inside a single coarse mtime
    tick, so mtime alone is not a safe change signal for a data-loss guard.
    Re-emitting the cached foreign lines (rather than assuming there are none)
    is what makes the fast path non-destructive: a previous save may have
    preserved a cross-process append INTO the on-disk window region, and since
    ``disk_older`` is unchanged those preserved lines would otherwise be dropped
    by a bare frozen-prefix + in-memory-window rebuild on the very next save.

    Returns ``("", [])`` when the file is missing/unreadable/has no metadata line.
    """
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return ("", [], [])
    cache = slot._frozen_prefix_cache
    if (
        cache is not None
        and cache[0] == mtime
        and cache[1] == size
        and cache[2] == disk_older
    ):
        # File is byte-identical to our last write → prefix AND the foreign
        # lines that write preserved are both served from cache. Returning the
        # cached foreign lines (a copy, so the caller cannot mutate the cache)
        # keeps the fast path non-destructive: the previously-preserved
        # cross-process append is re-emitted instead of silently dropped. No
        # scan runs, so there are no fresh dedup drops to archive.
        return (cache[3], list(cache[4]), [])
    try:
        existing = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return ("", [], [])
    if not existing or '"_type"' not in existing[0]:
        return ("", [], [])
    body = existing[1:]  # message lines only (metadata excluded)
    prefix = "".join(body[:disk_older]) if disk_older > 0 else ""
    if not collect_foreign:
        # Rewrite (rewind / regenerate / fork) INTENTIONALLY truncates the
        # window, so a disk window-region line absent from the (truncated) window
        # is ambiguous between a rewound tail (must drop) and a cross-process
        # append (must keep). Those edits are same-session/same-process (not the
        # cross-process loss this scan guards), so skip the scan and let the
        # rewrite's archive-diff handle the dropped tail. Cache with no foreign
        # lines so a subsequent fast path re-emits nothing extra.
        slot._frozen_prefix_cache = (mtime, size, disk_older, prefix, [])
        return (prefix, [], [])
    # Scan the on-disk window region for lines the in-memory window does not
    # carry — those are cross-process appends we must preserve. Identity is
    # timestamp-first (the closest thing to a stable per-message id today), with
    # (role, content) used only as a COUNT-BOUNDED tiebreak so each window entry
    # absorbs at most ONE disk copy (see the module docstring / history.md).
    #
    # Build COUNT-BOUNDED consumption budgets over the window entries so each
    # on-disk window-region line is matched to AT MOST ONE window entry and each
    # window entry absorbs AT MOST ONE disk line. Identity is checked in three
    # tiers of decreasing confidence:
    #   (a) exact (ts, role, content) — an unchanged re-serialization (the common
    #       steady-save case), resolved first across ALL disk lines so a greedy
    #       edit/tiebreak match can never steal an entry a later exact line needs;
    #   (b) ts only — an in-place edit (same ``ts``, changed content: window wins);
    #   (c) (role, content) only — a same-content copy persisted with a FRESH
    #       ``ts`` (the ``append_if_absent`` case), routed to the archive.
    # Keying every tier by COUNT (deques of entry indices guarded by a shared
    # ``consumed`` flag) — rather than a ``ts -> entry`` dict plus a per-``ts``
    # ``set`` — is what makes this correct when several messages share one ``ts``.
    # Coarse system clocks (notably Windows' ~15ms tick) can stamp a burst of
    # rapid appends with an IDENTICAL ``datetime.now().isoformat()``; the old
    # dict/set collapsed those colliding-``ts`` entries to a single slot, so a
    # genuine window line was mis-classified as a foreign append and DUPLICATED on
    # disk. The bounded multiset below matches them one-for-one regardless of
    # ``ts`` collisions.
    exact_idx: dict[tuple[object, object, object], "deque[int]"] = {}
    ts_idx: dict[object, "deque[int]"] = {}
    rc_idx: dict[tuple[object, object], "deque[int]"] = {}
    for _i, e in enumerate(window_entries):
        _ets = e.get("ts")
        _erole = e.get("role")
        _econtent = e.get("content", "")
        if _ets:
            exact_idx.setdefault((_ets, _erole, _econtent), deque()).append(_i)
            ts_idx.setdefault(_ets, deque()).append(_i)
        rc_idx.setdefault((_erole, _econtent), deque()).append(_i)
    consumed = [False] * len(window_entries)

    def _take(dq: "deque[int] | None") -> bool:
        """Consume the first not-yet-consumed entry index in ``dq`` (if any)."""
        if not dq:
            return False
        while dq:
            _idx = dq.popleft()
            if not consumed[_idx]:
                consumed[_idx] = True
                return True
        return False

    # Parse the on-disk window-region lines once (skipping blank/corrupt/transient
    # lines exactly as before), so the two matching passes share one parse.
    disk_msgs: list[tuple[str, object, object, object]] = []  # (norm, ts, role, content)
    for ln in body[disk_older:]:
        if not ln.strip():
            continue
        try:
            entry = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue  # corrupt window-region line — not a preservable message
        if not isinstance(entry, dict) or entry.get("_type") == "metadata":
            continue
        role = entry.get("role")
        if role is None or role in _TRANSIENT_ROLES:
            continue
        norm = ln if ln.endswith("\n") else ln + "\n"
        disk_msgs.append((norm, entry.get("ts"), role, entry.get("content", "")))

    # Pass 1 — exact (ts, role, content): unambiguously our own unchanged
    # re-serialization. Resolving these first makes the result independent of the
    # disk-line order (an earlier edit/tiebreak match can no longer consume an
    # entry that a later exact line requires).
    handled = [False] * len(disk_msgs)
    for _j, (_norm, ts, role, content) in enumerate(disk_msgs):
        if ts and _take(exact_idx.get((ts, role, content))):
            handled[_j] = True

    foreign: list[str] = []
    dedup_dropped: list[str] = []
    # After the exact pass, an in-place EDIT (same ``ts``, changed content) is the
    # only legitimate reason to drop a still-unmatched disk line by ``ts`` alone.
    # But under COLLIDING timestamps a ts-only match is AMBIGUOUS: a foreign
    # cross-process append that happens to share the ``ts`` is indistinguishable
    # from an edited window entry, and greedily consuming the ts budget would
    # silently DROP that acknowledged foreign line (data loss) — the exact guard
    # this scan exists to uphold. So restrict ts-only matching to the UNAMBIGUOUS
    # singleton case: a ``ts`` carried by EXACTLY ONE still-unmatched window entry
    # AND EXACTLY ONE still-unmatched disk line. Any ts group with more than one
    # unmatched line on either side is ambiguous, so its disk lines fall through
    # to the content tiebreak / foreign preservation below (favouring a rare
    # duplicate over irreversible data loss). Counts are taken from the
    # post-exact-pass state and are static for pass 2 (the ``consumed`` guard in
    # ``_take`` still prevents any double-consumption).
    w_unmatched_ts: dict[object, int] = {}
    for _i, e in enumerate(window_entries):
        _wt = e.get("ts")
        if _wt and not consumed[_i]:
            w_unmatched_ts[_wt] = w_unmatched_ts.get(_wt, 0) + 1
    d_unmatched_ts: dict[object, int] = {}
    for _j, (_norm, ts, _role, _content) in enumerate(disk_msgs):
        if ts and not handled[_j]:
            d_unmatched_ts[ts] = d_unmatched_ts.get(ts, 0) + 1

    # Pass 2 — for still-unmatched disk lines: ts-only (UNAMBIGUOUS in-place edit)
    # then the bounded (role, content) tiebreak, else genuinely foreign.
    for _j, (norm, ts, role, content) in enumerate(disk_msgs):
        if handled[_j]:
            continue
        # ts-match: an in-place edit keeps the ``ts`` but changes content, so the
        # window's version wins and the disk line is dropped silently — but ONLY
        # when the ``ts`` group is an unambiguous 1:1 (else a colliding foreign
        # append could be mistaken for the edit and lost).
        if (
            ts
            and w_unmatched_ts.get(ts, 0) == 1
            and d_unmatched_ts.get(ts, 0) == 1
            and _take(ts_idx.get(ts))
        ):
            continue
        # content tiebreak (bounded): a window entry with this exact
        # (role, content) that no match already consumed absorbs this disk copy —
        # the ``append_if_absent`` fresh-``ts`` case. A drop carrying a DISTINCT
        # non-empty ``ts`` is the genuinely ambiguous case (it could be a distinct
        # message we cannot tell apart without a stable id), so route it through
        # the archive; a ts-less / matching re-serialization is a plain window
        # copy and is dropped silently to avoid archive spam.
        if _take(rc_idx.get((role, content))):
            if ts:
                dedup_dropped.append(norm)
            continue
        # genuinely foreign → preserve verbatim.
        foreign.append(norm)
    # Cache the frozen prefix AND the foreign lines together, keyed on the
    # as-read (mtime, size). If this save's atomic_write later fails, the file
    # on disk is unchanged, so a subsequent save that re-reads the same
    # (mtime, size) must re-emit these same preserved foreign lines rather than
    # drop them — hence they are cached here, not just at the post-write site.
    slot._frozen_prefix_cache = (mtime, size, disk_older, prefix, foreign)
    return (prefix, foreign, dedup_dropped)


def _save_slot_to_history(
    state: DashboardState,
    slot: _ChatSlot,
    messages: list[dict] | None = None,
    *,
    closed: bool = False,
    force: bool = False,
    rewrite: bool = False,
) -> None:
    """Persist slot messages to JSONL history (append-safe).

    The session file is modeled as **frozen prefix + live window**:

    - The **frozen prefix** is the first ``slot._disk_older_count`` on-disk
      message lines — the turns OLDER than the in-memory window (set at
      restore/resume). These bytes are read verbatim and NEVER rewritten, so a
      restart that loaded only a recent window can no longer destroy older
      history.
    - The **live window** is ``slot.messages`` (small, ~500 messages). It is
      re-serialized in full on every save. Re-serializing the whole window means
      in-place edits to already-shown messages (stop-event resolution, file-change
      chips, mcp_oauth banner completion) and any reordering done by
      ``_flush_segment`` all persist correctly — there is no position counter to
      get out of sync.

    The default save writes ``meta + frozen_prefix + serialize(window)``.

    Pass ``rewrite=True`` (or an explicit *messages* snapshot, which implies it)
    for operations that INTENTIONALLY truncate the window (rewind/regenerate/
    fork): the file is rebuilt as ``meta + frozen_prefix + serialize(snapshot)``
    and the dropped window tail is archived first via ``_archive_dropped_lines``.

    Concurrency (#4): ``_flush_dirty_slots`` runs this in an executor thread
    while ``_run_chat`` mutates ``slot.messages`` on the event loop. We snapshot
    ``list(slot.messages)`` (a single GIL-atomic attribute read) and the matching
    ``slot._disk_older_count`` up front, then operate only on that snapshot, so a
    concurrent ``_flush_segment`` reassigning ``slot.messages`` cannot interleave
    with the read-serialize-write and skip/duplicate a message.

    Operates ONLY on this slot's own single session file (``_path(history_key)``);
    tab_id chaining is 1:1 (a slot's tab_id maps to exactly one file — fork makes
    a fresh slot with its own file), so this never reads/writes a sibling and
    legacy no-tab_id sessions stay isolated.
    """
    if not state.conversation_log:
        return
    # A slot whose window is still being replayed is not in a saveable state: it
    # is registered and already _dirty, but its _disk_older_count /
    # _disk_window_len still describe the pre-replay slot, so the frozen-prefix
    # arithmetic below would write a partial window that the next flush then
    # duplicates. Skipping loses nothing — that window was read from this very
    # file. This is the choke point, so it covers the shutdown sweep
    # (save_all_slots_to_history, force=True) and any future caller;
    # _flush_dirty_slots skips earlier so it does not clear _dirty either.
    if slot.key in state._restoring_keys:
        return
    # An explicit message snapshot always means "this is the full authoritative
    # window state" → rewrite. Edit paths (rewind/regenerate/fork) pass a snapshot.
    # A slot left in _pending_rewrite by a failed inline rewrite (#3) also takes
    # the archive-safe rewrite path until it succeeds.
    if messages is not None or slot._pending_rewrite:
        rewrite = True
    # Snapshot the window and its disk-older count CONSISTENTLY (#4). The save
    # may run in the flush executor thread while _flush_segment (reassigns
    # slot.messages) or append (trims the front AND bumps _disk_older_count)
    # run on the event loop. A trim is the only mutation that changes the
    # window/_disk_older_count relationship, so we read _disk_older_count,
    # snapshot the window, then confirm _disk_older_count is unchanged; a small
    # bounded retry closes the race without locks (slot._lock is an asyncio.Lock
    # and so cannot be acquired from this thread). An explicit snapshot is
    # already consistent by construction.
    if messages is not None:
        window = list(messages)
        disk_older = slot._disk_older_count
    else:
        for _ in range(_FLUSH_SNAPSHOT_RETRIES):
            disk_older = slot._disk_older_count
            window = list(slot.messages)
            if slot._disk_older_count == disk_older:
                break
        else:
            disk_older = slot._disk_older_count
            window = list(slot.messages)
    if not window:
        return
    # Skip a pure no-op: a freshly resumed slot with no new AND no edited
    # messages. ``slot._dirty`` is set by both append and in-place edits
    # (update_message / _resolve_stop_event / file-change + mcp_oauth patches),
    # so a dirty slot whose length merely equals the resumed count still falls
    # through and re-serializes the window — otherwise an in-place edit after
    # resume would never reach disk (#2). closed/force/rewrite always proceed.
    if (
        slot._resumed_count > 0
        and len(window) <= slot._resumed_count
        and not slot._dirty
        and not closed
        and not force
        and not rewrite
    ):
        return
    history_key = _history_key_for(slot.key)
    try:
        # Hold the SAME per-session cross-process lock that ``append`` /
        # ``append_off_loop`` / rotate / rewrite / metadata mutations take, across
        # the whole read-modify-atomic_write below (metadata read, frozen-prefix
        # read, archive-diff read, and the file-replacing ``atomic_write``).
        # Without it, a concurrent ``append_off_loop`` (e.g. a workflow/cron
        # result appended to the originating dashboard session) can land between
        # this save's snapshot of the file and its ``atomic_write`` — the save
        # then replaces the file with meta+frozen+window and silently deletes the
        # acknowledged append. ``_locked`` serializes the two so neither is lost.
        # On the event loop ``_locked`` makes ONE non-blocking acquire and raises
        # ``HistoryLockTimeout`` under contention (never blocking the loop); the
        # ``save_slot_off_loop`` helper routes on-loop callers to a worker thread
        # so they take the patient acquire path instead of dropping the save.
        with state.conversation_log._locked(history_key):
            existing_meta = state.conversation_log.get_metadata(history_key)

            path = state.conversation_log._path(history_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            meta_line: dict = {
                "_type": "metadata",
                "created_at": existing_meta.get("created_at") or slot.created_at,
                "last_consolidated": existing_meta.get("last_consolidated", 0),
            }
            # Preserve history-layer-owned metadata this dashboard save does NOT
            # manage. ``rotation_generation`` (bumped by ``_maybe_rotate``, with
            # its ``rotated_at`` stamp) lets a concurrent consolidation detect a
            # rotation and skip applying a stale offset. Reconstructing the
            # metadata subset here would drop it, resetting the generation to 0
            # and re-opening the exact consolidation race the rotation-generation
            # fix closed. Carry these forward verbatim (absent field == no-op).
            for _meta_key in ("rotation_generation", "rotated_at", "compacted_at"):
                if _meta_key in existing_meta:
                    meta_line[_meta_key] = existing_meta[_meta_key]
            if closed:
                meta_line["closed"] = True
            meta_line["memory_mode"] = slot.memory_mode
            if slot.title and slot.title != slot.key:
                meta_line["title"] = slot.title
            if slot.agent:
                meta_line["agent"] = slot.agent
            meta_line["model"] = slot.model
            if slot.reasoning_effort:
                meta_line["reasoning_effort"] = slot.reasoning_effort
            if slot.mode:
                meta_line["mode"] = slot.mode
            if slot.workspace and slot.workspace != "default":
                meta_line["workspace"] = slot.workspace
            if slot.project:
                meta_line["project"] = slot.project
            if slot.folder_id:
                meta_line["folder_id"] = slot.folder_id
            if slot._app:
                meta_line["app"] = slot._app
            # Artifact companion binding — persisted so a bound
            # session restored after a gateway restart (or resumed from the
            # History page) comes back as the artifact's active bound session.
            if slot._artifact:
                meta_line["artifact"] = slot._artifact
            if slot.pinned:
                meta_line["pinned"] = True
            if slot.color_index is not None:
                meta_line["color_index"] = slot.color_index
            if slot.color_theme:
                meta_line["color_theme"] = slot.color_theme
            if slot.tags:
                meta_line["tags"] = list(slot.tags)
            if slot.forked_from is not None:
                meta_line["forked_from"] = slot.forked_from
            tab_id = getattr(slot, "_tab_id", None) or existing_meta.get("tab_id")
            if tab_id:
                meta_line["tab_id"] = tab_id
            meta_str = json.dumps(meta_line) + "\n"

            # ── Frozen prefix (never rewritten) + freshly serialized window ──
            # Read the verbatim bytes of the on-disk lines OLDER than the
            # in-memory window (cached, O(window) on a steady flush — #5), AND
            # detect any cross-process appends that landed in the on-disk window
            # region since our last write. Then re-serialize the ENTIRE window
            # snapshot so in-place edits and reordering persist, and append the
            # foreign lines so a concurrent cross-process append (landed between
            # this save's pre-lock ``window`` snapshot and the lock) is preserved
            # rather than clobbered by the meta+frozen+window replace.
            window_entries = [
                e for m in window if (e := _build_message_entry(m)) is not None
            ]
            window_lines = [json.dumps(e) + "\n" for e in window_entries]
            frozen_prefix, foreign_lines, dedup_dropped = (
                _frozen_prefix_and_foreign_appends(
                    slot, path, disk_older, window_entries, collect_foreign=not rewrite
                )
            )
            # A fresh-``ts`` disk copy folded into the window by the bounded
            # (role, content) tiebreak is redundant with a window entry, so the
            # payload does not carry it. It is nonetheless the genuinely ambiguous
            # case (indistinguishable from a distinct same-content message without
            # a stable per-message id), so archive it before the atomic replace so
            # the trade-off loses no data permanently (arbiter long-term item 2b).
            if dedup_dropped:
                try:
                    base = (
                        state.conversation_log._dir
                        if state.conversation_log
                        else None
                    )
                    _archive_lines(
                        history_key, dedup_dropped, reason="foreign-dedup", base=base
                    )
                except Exception:
                    logger.warning(
                        "Failed to archive foreign-dedup drops for %s",
                        history_key,
                        exc_info=True,
                    )
            payload = (
                meta_str + frozen_prefix + "".join(window_lines) + "".join(foreign_lines)
            )

            # Rewrite paths (rewind/regenerate/fork) intentionally TRUNCATE the
            # window, so the dropped tail must be archived first to stay
            # recoverable. The default save is a superset of what's on disk
            # (frozen prefix unchanged + same-or-grown window), so it drops
            # nothing — and we skip the O(file) archive-diff read there to keep a
            # steady flush O(window) (#5). Both sides are passed as proper
            # per-line lists so the normalized-JSON diff matches the
            # frozen-prefix lines (never archived).
            if rewrite and path.exists():
                try:
                    old_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                    new_lines = payload.splitlines(keepends=True)
                    _archive_dropped_lines(state, history_key, old_lines, new_lines)
                except Exception:
                    logger.warning(
                        "Failed to archive dropped lines for %s", history_key, exc_info=True
                    )

            atomic_write(path, payload, fsync=True)
            # A rewrite (archive-safe) save succeeded → clear the pending-rewrite
            # flag so later saves return to the cheap default path (#3).
            if rewrite:
                slot._pending_rewrite = False
            # Record how many window messages are now on disk so memory trimming
            # can safely fold leading window messages into the frozen prefix (#8).
            slot._disk_window_len = len(window)
            # Record the post-write mtime in the frozen-prefix cache (even when
            # there is no frozen prefix, ``disk_older == 0``). The cache doubles
            # as the "did another process touch this file since we last wrote
            # it?" signal: a matching mtime on the next save proves THIS slot was
            # the last writer, so the frozen prefix is reusable and no NEW
            # cross-process append can have landed — letting the foreign-append
            # scan take the O(window) fast path (#5) instead of re-reading the
            # whole file. The foreign lines this save just preserved are cached
            # alongside so the fast path re-emits them verbatim: they now live in
            # the on-disk window region (after the frozen prefix), and because
            # ``disk_older`` is unchanged a bare frozen+window rebuild on the next
            # save would otherwise silently delete them (data-loss finding).
            try:
                _st = path.stat()
                slot._frozen_prefix_cache = (
                    _st.st_mtime,
                    _st.st_size,
                    disk_older,
                    frozen_prefix,
                    foreign_lines,
                )
            except OSError:
                slot._frozen_prefix_cache = None
            state.conversation_log._invalidate_cache(history_key)
            state.conversation_log.invalidate_tab_id_cache()
    except Exception:
        logger.error("Failed to save slot %s to history", slot.key, exc_info=True)
        raise


async def save_slot_off_loop(
    state: DashboardState,
    slot: _ChatSlot,
    messages: list[dict] | None = None,
    *,
    closed: bool = False,
    force: bool = False,
    rewrite: bool = False,
    best_effort: bool = True,
) -> None:
    """Persist a slot from the event loop without blocking or dropping the save.

    :func:`_save_slot_to_history` now holds the per-session cross-process
    ``_locked`` across its read-modify-``atomic_write`` (see the finding it
    fixes). That lock, invoked on the gateway event loop, makes a single
    non-blocking acquire and raises :class:`~kiro_crew.history.HistoryLockTimeout`
    under any concurrent holder (e.g. a workflow/cron result appending via
    :func:`~kiro_crew.history.append_off_loop`) — so calling the save inline on
    the loop would both risk a disk write on the loop and drop the save under
    benign contention, or surface the timeout into the aiohttp handler.

    This helper mirrors :func:`~kiro_crew.history.append_off_loop`: on a running
    loop it dispatches the save to a worker thread so it takes the *patient*
    off-loop acquire path; off the loop it saves inline.

    ``best_effort`` (default ``True``): a lock timeout / I/O error is logged and
    the slot is marked ``_dirty`` so the periodic flush retries the write — the
    in-memory slot is the source of truth. This retry re-arm matters for the
    metadata mutation endpoints (pin / folder / tag / mode), which call this with
    ``force=True`` but do not otherwise mark the slot dirty: without it a
    swallowed failure would drop an acknowledged edit with no retry, losing it
    after a restart. Pass ``best_effort=False`` for archival paths (session
    close/cleanup) that must CONFIRM the durable write succeeded before removing
    the session: the save still runs off-loop (patient acquire), but any
    exception propagates so the caller can roll back and keep the slot.
    """

    def _do() -> None:
        _save_slot_to_history(
            state, slot, messages, closed=closed, force=force, rewrite=rewrite
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        if best_effort:
            try:
                _do()
            except Exception:  # noqa: BLE001 - best-effort durable copy
                # A swallowed failure must NOT be silently final: mark the slot
                # dirty so the periodic flush retries the write. Metadata-only
                # mutations (pin / folder / tag / mode) call this with
                # ``force=True`` but do not otherwise set ``_dirty``; without this
                # a lock timeout / I/O error would drop the change and the flush
                # would never retry it, losing an acknowledged edit after restart.
                slot._dirty = True
                logger.warning(
                    "save_slot_off_loop: inline save failed slot=%s", slot.key, exc_info=True
                )
            return
        _do()
        return
    if best_effort:
        try:
            await loop.run_in_executor(None, _do)
        except Exception:  # noqa: BLE001 - best-effort durable copy
            # See the inline branch above: re-arm the periodic flush so a
            # swallowed metadata/message save is retried rather than lost.
            slot._dirty = True
            logger.warning(
                "save_slot_off_loop: offloaded save failed slot=%s", slot.key, exc_info=True
            )
        return
    # Non-best-effort: propagate so the caller can roll back (do NOT remove the
    # session until the durable write is confirmed).
    await loop.run_in_executor(None, _do)


def _build_history_prefix(slot: _ChatSlot) -> str:
    """Build a condensed history prefix from slot messages for session re-injection."""
    lines: list[str] = []
    total = 0
    for m in slot.messages:
        role = m.get("role", "")
        if role in ("chunk", "done", "streaming", "queued", "permission", "error", "tool"):
            continue
        label = "User" if role == "user" else "Assistant"
        text = m.get("content", "")[:500]
        line = f"{label}: {text}"
        if total + len(line) > _MAX_HISTORY_CHARS:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return (
        "[Previous chat history for this tab — session was reset after stop]\n"
        + "\n".join(lines)
        + "\n[End of history]\n\n"
    )
