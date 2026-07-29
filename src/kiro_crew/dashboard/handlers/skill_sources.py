"""API handlers for linked skill repositories (``skills.sources``).

Routes live under the ``/api/skills/-/`` escape prefix because
``/api/skills/{name:.+}`` is a catch-all for skill detail — a bare
``/api/skills/sources`` would be swallowed as a skill named "sources".

Mutations are owner-only in effect: the routes require an authenticated
dashboard user and every add/remove/sync is SEL-audited, matching the
portability and workspace-create handlers. The repo URL a caller supplies is
the one thing that reaches ``git clone``, so it is validated by
``is_clone_host_trusted`` inside ``sync_skill_source`` before any spawn.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig, SkillSourceConfig
from kiro_crew.dashboard.handlers._shared import _get_skills
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.sel import sel as _sel_fn  # circular import — sel imports lazily
from kiro_crew.skill_sources import (
    count_skills,
    is_valid_source_name,
    read_sync_state,
    record_sync_state,
    remove_skill_source_clone,
    source_lock,
    source_skill_root,
    sync_skill_source,
)

logger = logging.getLogger(__name__)


def _sel():
    return _sel_fn()


async def _refresh_skills_loader(
    request: web.Request, cfg: KiroCrewConfig | None = None
) -> None:
    """Make a source change visible to the live SkillsLoader immediately.

    The loader is one long-lived instance per gateway and resolves its extra
    roots at construction, so without this a freshly linked repo's skills would
    not appear (in the skills list, in trigger matching, or in session context)
    until a restart. Best-effort: a refresh failure must not fail the mutation
    that already succeeded on disk — the next gateway start picks it up.

    Offloaded: ``reload_extra_paths`` re-scans every linked mirror, which must
    not run on the event loop.
    """
    try:
        state = request.app["state"]
        loader = _get_skills(state)
        await asyncio.to_thread(loader.reload_extra_paths, cfg)
    except Exception:
        logger.warning("skill-sources: could not refresh the live skills loader", exc_info=True)


def _require_user(request: web.Request) -> str | None:
    if "user" not in request or not request["user"]:
        return None
    return str(request["user"])


def _source_row(src: SkillSourceConfig, state: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build the API view of one configured source.

    ``skill_count`` prefers a live count of the mounted tree over the ledger's
    stored value so a mirror that was changed on disk (or a subdir that was
    re-pointed in config without a re-sync) reports what is actually loadable.

    BLOCKING — walks the mirror (``count_skills`` is an ``os.walk`` bounded at
    20k entries) and stats the mount root. Never call this directly from a
    handler; go through :func:`_build_source_rows`, which runs it in a worker
    thread. A source with an empty ``subdir`` mounts the whole checkout, so on a
    large linked repo an on-loop call would stall every chat turn and the
    liveness heartbeat.
    """
    entry = state.get(src.name, {})
    root = source_skill_root(src.name, src.subdir) if src.enabled else None
    live_count: int | None = None
    if root is not None:
        try:
            live_count = count_skills(root)
        except OSError:
            live_count = None
    return {
        "name": src.name,
        "repo": src.repo,
        "branch": src.branch,
        "subdir": src.subdir,
        "enabled": src.enabled,
        "cloned": root is not None,
        "head": entry.get("head", ""),
        "skill_count": live_count if live_count is not None else entry.get("skill_count", 0),
        "synced_at": entry.get("synced_at", 0.0),
        "last_success_at": entry.get("last_success_at", 0.0),
        "last_ok": bool(entry.get("ok", False)) if entry else None,
        "last_error": entry.get("error", ""),
    }


def _source_rows_blocking(sources: list[SkillSourceConfig]) -> list[dict[str, Any]]:
    """Read the sync ledger and build every row. BLOCKING — call via to_thread."""
    state = read_sync_state()
    return [_source_row(s, state) for s in sources]


async def _build_source_rows(sources: list[SkillSourceConfig]) -> list[dict[str, Any]]:
    """Build the API view of *sources* in a worker thread.

    One hop for the whole list rather than one per source: the ledger read is
    shared, and N sources would otherwise mean N thread round-trips.
    """
    return await asyncio.to_thread(_source_rows_blocking, list(sources))


async def _one_source_row(src: SkillSourceConfig) -> dict[str, Any]:
    rows = await _build_source_rows([src])
    return rows[0]


async def api_skill_sources(request: web.Request) -> web.Response:
    """GET /api/skills/-/sources — list linked repos with sync status."""
    if _require_user(request) is None:
        return web.json_response({"error": "authentication required"}, status=401)
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    return web.json_response({"sources": await _build_source_rows(cfg.skills.sources)})


async def api_skill_sources_add(request: web.Request) -> web.Response:
    """POST /api/skills/-/sources — link a repo, then sync it immediately.

    The sync runs inline rather than in the background so the caller gets the
    real outcome (bad URL, wrong branch, no SKILL.md files) instead of an
    optimistic 200 followed by a silently empty skills list. On sync failure
    the source is NOT persisted — a configured-but-broken entry would otherwise
    keep failing on every startup sync with nobody having asked for it.
    """
    caller = _require_user(request)
    if caller is None:
        return web.json_response({"error": "authentication required"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON body"}, status=400)

    name = str(body.get("name", "") or "").strip()
    repo = str(body.get("repo", "") or "").strip()
    branch = str(body.get("branch", "") or "main").strip() or "main"
    subdir = str(body.get("subdir", "") or "").strip().strip("/")

    def denied(message: str, status: int = 400) -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="skills.source.add",
            outcome="denied",
            source="dashboard",
            resources=name or repo,
            error=message,
        )
        return web.json_response({"error": message}, status=status)

    if not repo:
        return denied("repo is required")
    if not name:
        return denied("name is required")
    if not is_valid_source_name(name):
        return denied("name must be lowercase kebab-case (a-z, 0-9, hyphens), max 64 chars")

    # The whole transaction — duplicate check, sync into skill-sources/<name>,
    # failure cleanup, and persist — is serialized per NAME. Two concurrent
    # same-name adds would otherwise both clear the duplicate check (neither is
    # persisted yet), clone into the same directory, and the loser's cleanup
    # would delete the winner's mirror, leaving a persisted source with no
    # skills. See _source_lock for why this is not the shared config lock.
    async with source_lock(name):
        async with _get_config_lock():
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if any(s.name == name for s in cfg.skills.sources):
            return denied(f"a skill source named {name!r} already exists", status=409)

        candidate = SkillSourceConfig(
            name=name, repo=repo, branch=branch, subdir=subdir, enabled=True
        )
        result = await sync_skill_source(candidate)
        await asyncio.to_thread(record_sync_state, result)
        if not result.ok:
            # Leave no mirror behind for a source we are not persisting. rmtree
            # of a git clone is unbounded, so it never runs on the loop. Safe to
            # delete unconditionally here: the name lock guarantees no other
            # request owns this mirror.
            await asyncio.to_thread(remove_skill_source_clone, name)
            _sel().log_api_access(
                caller=caller,
                operation="skills.source.add",
                outcome="error",
                source="dashboard",
                resources=f"{name} {repo}",
                error=result.error,
            )
            return web.json_response(
                {
                    "error": result.message or "sync failed",
                    "code": result.error,
                    "log": result.log,
                },
                status=400,
            )

        # Re-load before mutating: the inline sync awaited network I/O, so the
        # on-disk config may have changed underneath us. The load/check/append/
        # save is held under the shared config lock (the same one the agents
        # handlers use) because config.json is rewritten wholesale — two
        # concurrent mutations of DIFFERENT sources would otherwise both read
        # the pre-change snapshot and the second save would drop the first
        # one's entry.
        async with _get_config_lock():
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            if any(s.name == name for s in cfg.skills.sources):
                return denied(f"a skill source named {name!r} already exists", status=409)
            cfg.skills.sources.append(candidate)
            await asyncio.to_thread(cfg.save)
    await _refresh_skills_loader(request, cfg)
    _sel().log_api_access(
        caller=caller,
        operation="skills.source.add",
        outcome="success",
        source="dashboard",
        resources=f"{name} {repo}@{branch}",
    )
    return web.json_response({"ok": True, "source": await _one_source_row(candidate)})


async def api_skill_sources_sync(request: web.Request) -> web.Response:
    """POST /api/skills/-/sources/{name}/sync — refresh one linked repo."""
    caller = _require_user(request)
    if caller is None:
        return web.json_response({"error": "authentication required"}, status=401)
    name = request.match_info.get("name", "")
    # Take the name lock BEFORE loading and resolving the source. Resolving first
    # and locking after left a window where a concurrent unlink could remove the
    # source and delete its mirror, and this handler would then re-clone and
    # remount a source that no longer exists in config. Holding the lock across
    # the load means the source we act on is the source that is still configured.
    async with source_lock(name):
        async with _get_config_lock():
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
        src = next((s for s in cfg.skills.sources if s.name == name), None)
        if src is None:
            return web.json_response({"error": f"no skill source named {name!r}"}, status=404)
        result = await sync_skill_source(src)
        await asyncio.to_thread(record_sync_state, result)
    if result.ok:
        # A sync can add or remove skills in the mirror, so the loader's cached
        # file list is now stale even though the mounted root is unchanged.
        await _refresh_skills_loader(request, cfg)
    _sel().log_api_access(
        caller=caller,
        operation="skills.source.sync",
        outcome="success" if result.ok else "error",
        source="dashboard",
        resources=f"{name}@{result.head[:7]}" if result.head else name,
        error=result.error,
    )
    # ``message`` and ``error`` are mirrored at the top level so a client can
    # render the human-readable reason without reaching into ``result`` — the
    # failure body is what the UI shows verbatim.
    return web.json_response(
        {
            "ok": result.ok,
            "message": result.message,
            "error": result.error,
            "result": result.to_dict(),
            "source": await _one_source_row(src),
        },
        status=200 if result.ok else 502,
    )


async def api_skill_sources_delete(request: web.Request) -> web.Response:
    """DELETE /api/skills/-/sources/{name} — unlink a repo and drop its mirror."""
    caller = _require_user(request)
    if caller is None:
        return web.json_response({"error": "authentication required"}, status=401)
    name = request.match_info.get("name", "")
    # Name lock first so an unlink cannot delete a mirror an in-flight add of the
    # same name is still cloning into; config lock for the wholesale rewrite.
    async with source_lock(name):
        async with _get_config_lock():
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            remaining = [s for s in cfg.skills.sources if s.name != name]
            if len(remaining) == len(cfg.skills.sources):
                return web.json_response({"error": f"no skill source named {name!r}"}, status=404)
            cfg.skills.sources = remaining
            await asyncio.to_thread(cfg.save)
        removed = await asyncio.to_thread(remove_skill_source_clone, name)
    await _refresh_skills_loader(request, cfg)
    _sel().log_api_access(
        caller=caller,
        operation="skills.source.remove",
        outcome="success",
        source="dashboard",
        resources=f"{name} mirror_removed={removed}",
    )
    return web.json_response({"ok": True, "mirror_removed": removed})
