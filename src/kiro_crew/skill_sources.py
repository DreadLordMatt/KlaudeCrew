"""Linked skill repositories — pull shared skills from a git remote.

A *skill source* is an owner-designated git repo (typically a team-owned
GitHub/GitLab repo) whose ``SKILL.md`` trees are mirrored into
``$KIROCREW_HOME/skill-sources/<name>/`` and mounted as an additional
read-only skills root. Teammates get the same skills by configuring the same
repo; nobody copies skill files by hand.

Design decisions worth stating, because each rules out an alternative that
looks reasonable:

**Mounted, not copied.** The clone is added to ``SkillsLoader``'s extra roots
rather than copied into ``$KIROCREW_HOME/skills/``. Three consequences we want:
a sync can never overwrite a hand-authored local skill (the local root keeps
precedence on name collisions), an upstream deletion disappears on the next
sync instead of leaving an orphan behind, and there is exactly one writer for
the mirror (this module) so no reconciliation is needed.

**Mirror semantics, not merge semantics.** Updates are ``fetch`` +
``reset --hard`` + ``clean -fd``, not ``pull --ff-only``. The mirror holds no
local work — it is downstream-only — so there is nothing to preserve and
nothing to conflict. This deliberately differs from
``apps/registry._git_clone_or_pull``, which fast-forwards and *swallows* the
failure ("building with existing code"): applied here that would let a dirty
mirror pin stale team skills forever with no signal. A failed sync here is
reported, and the previously-synced tree stays mounted until it succeeds
(stale beats missing, but stale is never silent).

**Credential posture: owner-designated.** The repo URL is typed by the owner
into their own config, so the clone keeps the gateway's ambient git identity
via ``minimal_env`` and the URL-derived sandbox mode — the same posture
``apps/registry`` uses for bundled/owner-designated installs, not the
credential-free ``anonymous_git_env`` posture reserved for URLs that came from
untrusted index content. Every URL still passes the ``is_clone_host_trusted``
SSRF gate first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.registry import (
    _contained_join,
    _context_clone_sandbox_mode,
    _is_safe_registry_subdir,
    _kill_process_group,
    is_clone_host_trusted,
    minimal_env,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.hooks import is_sensitive_path
from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv

logger = logging.getLogger(__name__)

SKILL_SOURCES_DIR_NAME = "skill-sources"

# Kebab-case, same shape the app registry enforces on names that reach a path
# join or an rmtree. Anchored and bounded so a config edit cannot smuggle a
# traversal segment or an unbounded name into the clone root.
_SOURCE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SOURCE_NAME_LEN = 64

_CLONE_TIMEOUT = 60
_FETCH_TIMEOUT = 60
_GIT_TIMEOUT = 15

# Ceiling on the mirror walk used for the skill count. A linked repo is
# expected to hold tens of skills; the cap stops a pathological repo from
# turning a status read into an unbounded stat storm.
_MAX_WALK_ENTRIES = 20_000

_STATE_FILENAME = ".state.json"


@dataclass
class SkillSourceSyncResult:
    """Outcome of one sync attempt against one linked repo."""

    name: str
    ok: bool
    action: str = ""  # "cloned" | "updated" | "unchanged" | "failed"
    head: str = ""
    skill_count: int = 0
    error: str = ""
    message: str = ""
    log: list[str] = field(default_factory=list)
    synced_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "action": self.action,
            "head": self.head,
            "skill_count": self.skill_count,
            "error": self.error,
            "message": self.message,
            "log": list(self.log),
            "synced_at": self.synced_at,
        }


def skill_sources_dir() -> Path:
    """Return ``$KIROCREW_HOME/skill-sources/`` — the mirror root."""
    return config_dir() / SKILL_SOURCES_DIR_NAME


def is_valid_source_name(name: Any) -> bool:
    """True when *name* is safe to use as a clone directory name.

    Re-validated on every read, not just on write: the name reaches both a
    path join and (on removal) ``shutil.rmtree``, and ``config.json`` is an
    editable file, so trusting a once-validated value would make a hand-edited
    config a delete primitive.
    """
    if not isinstance(name, str) or not name:
        return False
    if len(name) > _MAX_SOURCE_NAME_LEN:
        return False
    return bool(_SOURCE_NAME_RE.match(name))


def skill_source_dir(name: str) -> Path | None:
    """Return the clone directory for *name*, or ``None`` if the name is unsafe."""
    if not is_valid_source_name(name):
        logger.warning("skill-sources: rejecting unsafe source name %r", name)
        return None
    return skill_sources_dir() / name


def source_skill_root(name: str, subdir: str = "") -> Path | None:
    """Return the directory to mount as a skills root for source *name*.

    ``subdir`` lets a repo keep its skills under e.g. ``skills/`` alongside
    other content. Returns ``None`` when the name or subdir is unsafe, when the
    clone is absent, or when the resolved path escapes the clone (symlink
    containment) or is sensitive.
    """
    dest = skill_source_dir(name)
    if dest is None:
        return None
    if not _is_safe_registry_subdir(subdir):
        logger.warning("skill-sources: rejecting unsafe subdir %r for %s", subdir, name)
        return None
    root = _contained_join(dest, subdir or "")
    if root is None or not root.is_dir():
        return None
    if is_sensitive_path(str(root)):
        logger.warning("skill-sources: refusing sensitive skill root %s", root)
        return None
    return root


def skill_source_roots(sources: list[Any]) -> list[Path]:
    """Resolve configured *sources* to existing, mountable skills roots.

    Order follows the configured order so a duplicate skill name resolves
    deterministically (``SkillsLoader`` keeps the first occurrence). Unsafe or
    not-yet-cloned sources are skipped rather than raising — a bad entry must
    not stop the other skills from loading.
    """
    roots: list[Path] = []
    for src in sources or []:
        name = getattr(src, "name", "") or ""
        if not getattr(src, "enabled", True):
            continue
        root = source_skill_root(name, getattr(src, "subdir", "") or "")
        if root is not None and root not in roots:
            roots.append(root)
    return roots


# ---------------------------------------------------------------------------
# Sync state ledger
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    # A source name is kebab-case, so a dotfile can never collide with one.
    return skill_sources_dir() / _STATE_FILENAME


# Serializes the ledger's read-modify-write. A threading.Lock (not asyncio) is
# correct here because every caller runs these functions in a worker thread via
# ``asyncio.to_thread``. Without it, two syncs for DIFFERENT sources both read
# the pre-change ledger and the second atomic replace drops the first's entry —
# the per-source locks cannot help, since the contention is on one shared file.
_state_lock = threading.Lock()

# Per-source mutation locks. Lives here rather than in the dashboard handlers
# because the startup sync (in the gateway) and the HTTP handlers both need to
# take the SAME lock for a given source; two registries would not exclude each
# other. Keyed by source name and rebound when the running loop changes.
_source_locks: dict[str, asyncio.Lock] = {}
_source_locks_loop: asyncio.AbstractEventLoop | None = None


def source_lock(name: str) -> asyncio.Lock:
    """Return the mutation lock for source *name*, bound to the current loop.

    Scoped per name rather than globally because the guarded region includes a
    ``git clone`` with a 60s ceiling; a global lock would stall unrelated work
    for that long. Two requests touching the same mirror directory must still
    exclude each other — see the callers for the specific races.
    """
    global _source_locks_loop
    loop = asyncio.get_running_loop()
    if _source_locks_loop is not loop:
        _source_locks.clear()
        _source_locks_loop = loop
    lock = _source_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _source_locks[name] = lock
    return lock


def read_sync_state() -> dict[str, dict[str, Any]]:
    """Read the per-source sync ledger. Best-effort: returns {} on any error."""
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}


def _write_sync_state(state: dict[str, dict[str, Any]]) -> None:
    try:
        skill_sources_dir().mkdir(parents=True, exist_ok=True)
        atomic_write(_state_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")
    except OSError:
        # The ledger is display metadata; losing it must not fail a sync that
        # already wrote the files the loader reads.
        logger.warning("skill-sources: could not persist sync state", exc_info=True)


def record_sync_state(result: SkillSourceSyncResult) -> None:
    """Persist one source's outcome without clobbering the other sources'.

    The whole read-modify-write is held under ``_state_lock``; the ledger is a
    single file rewritten in full, so an unsynchronized update loses concurrent
    entries for unrelated sources.
    """
    with _state_lock:
        _record_sync_state_locked(result)


def _record_sync_state_locked(result: SkillSourceSyncResult) -> None:
    state = read_sync_state()
    prior = state.get(result.name, {})
    entry: dict[str, Any] = {
        "ok": result.ok,
        "action": result.action,
        "synced_at": result.synced_at,
        "error": result.error,
    }
    if result.ok:
        entry["head"] = result.head
        entry["skill_count"] = result.skill_count
        entry["last_success_at"] = result.synced_at
    else:
        # Keep the last known-good head/count so the UI can say "showing skills
        # from <sha>, last sync failed" instead of blanking the row.
        entry["head"] = prior.get("head", "")
        entry["skill_count"] = prior.get("skill_count", 0)
        entry["last_success_at"] = prior.get("last_success_at", 0.0)
    state[result.name] = entry
    _write_sync_state(state)


def forget_sync_state(name: str) -> None:
    """Drop one source's ledger entry. Serialized for the same reason as
    :func:`record_sync_state` — this also rewrites the whole file."""
    with _state_lock:
        state = read_sync_state()
        if state.pop(name, None) is not None:
            _write_sync_state(state)


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------


async def _run_git(
    args: list[str],
    *,
    cwd: Path | None,
    sandbox_mode: str,
    env: dict[str, str],
    timeout: int,
    repo: Path | None = None,
) -> tuple[int, str]:
    """Run ``git *args`` sandboxed, returning ``(returncode, combined_output)``.

    Routed through ``wrap_argv`` (OS isolation) then ``cgroup_scope_argv``
    (resource ceiling, outermost) with an RLIMIT ``preexec_fn``, matching the
    app-registry clone path. ``wrap_argv`` raises when no sandbox backend is
    available and unsandboxed exec is not permitted — that propagates, so a
    sync fails closed rather than spawning git unisolated.

    ``repo`` pins the operation to that checkout with explicit ``--git-dir`` and
    ``--work-tree``, and neutralizes the repo-local settings that can redirect
    or execute work. This is required, not cosmetic: a mirror is a git repo
    whose ``.git/config`` is mutable, and ``core.worktree`` there redirects the
    destructive half of a sync. Measured directly — with a poisoned
    ``core.worktree``, a bare ``reset --hard`` writes into the pointed-at
    directory, and passing ``--work-tree`` explicitly keeps every write inside
    the mirror, because command-line options outrank any config file. ``clean
    -fd`` under the same poisoning would delete untracked files there, so this
    covers the overwrite and the delete.

    ``-c`` overrides also outrank an ``include.path``-pulled file, which is why
    the dangerous keys are set on the command line rather than by editing config.
    """
    prefix: list[str] = []
    if repo is not None:
        prefix = [
            # Explicit paths beat core.worktree / GIT_WORK_TREE.
            f"--git-dir={repo / '.git'}",
            f"--work-tree={repo}",
            # Repo-local hooks must never run: a synced mirror is third-party
            # content, and a checkout hook would execute on every sync.
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
        ]
    argv, _cleanup = wrap_argv(["git", *prefix, *args], mode=sandbox_mode)
    argv = cgroup_scope_argv(argv)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        env=env,
        preexec_fn=resource_limit_preexec(),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _kill_process_group(proc)
        return 124, f"git {args[0]} timed out after {timeout}s"
    except asyncio.CancelledError:
        # Gateway shutdown (or any task cancellation) must not leave git running
        # detached: a clone or `reset --hard` that keeps going after we stop
        # watching it leaves the mirror mid-rewrite, and the next gateway start
        # would mount a partial tree. Kill the group, then re-raise so
        # cancellation still propagates.
        await _kill_process_group(proc)
        raise
    return proc.returncode or 0, stdout.decode(errors="replace").strip()


def _is_cloned(dest: Path) -> bool:
    """True when *dest* already holds a git checkout.

    ``os.path.exists`` rather than ``.is_dir()`` on ``.git``: in a git worktree
    ``.git`` is a *file*, and an ``is_dir`` check would misreport such a
    checkout as un-cloned and then try to clone over a non-empty directory.
    """
    return dest.is_dir() and os.path.exists(dest / ".git")


def is_within_root(path: str, root_real: str) -> bool:
    """True when *path* resolves inside *root_real* (already a realpath).

    Keeps a linked repo's symlinks from escaping its mirror. Compares realpaths
    with ``os.path.commonpath`` rather than a string prefix so a sibling
    directory sharing a name prefix (``/x/mirror-evil`` vs ``/x/mirror``) is not
    treated as contained.

    Lives here rather than in ``skills.py`` because three separate call sites
    need it — skill discovery, ``load_skill``, and :func:`count_skills` — and
    ``skills`` already imports this module, so the reverse direction would be a
    cycle. Having one definition is the point: the escape this guards against
    was originally closed in discovery only, and ``load_skill`` reached the same
    files by a different path.
    """
    if not root_real:
        return True
    try:
        target = os.path.realpath(path)
        return os.path.commonpath([target, root_real]) == root_real
    except (OSError, ValueError):
        # ValueError: paths on different drives (Windows) have no common path.
        return False


def count_skills(root: Path) -> int:
    """Count loadable ``SKILL.md`` files under *root*, bounded by the walk cap.

    Only counts entries the loader would actually expose: a ``SKILL.md`` that is
    a symlink escaping the mirror is skipped here for the same reason discovery
    skips it. Counting those made link-time validation disagree with the loader —
    a repo whose only "skills" were escaping symlinks would report a positive
    count and link successfully while contributing nothing.
    """
    count = 0
    seen = 0
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip VCS metadata and the dot-dirs the skills loader also prunes.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        seen += len(dirnames) + len(filenames)
        if "SKILL.md" in filenames:
            candidate = os.path.join(dirpath, "SKILL.md")
            if is_within_root(candidate, root_real) and os.path.isfile(candidate):
                count += 1
        if seen > _MAX_WALK_ENTRIES:
            logger.warning("skill-sources: walk cap hit under %s; count is partial", root)
            break
    return count


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


async def sync_skill_source(source: Any) -> SkillSourceSyncResult:
    """Clone or refresh one linked skill repo.

    Never raises for an expected failure (bad name, untrusted host, git error,
    sandbox unavailable) — those come back as ``ok=False`` with an ``error``
    code so a caller syncing several sources reports per-source outcomes
    instead of aborting the batch.
    """
    name = getattr(source, "name", "") or ""
    repo = (getattr(source, "repo", "") or "").strip()
    branch = (getattr(source, "branch", "") or "main").strip() or "main"
    subdir = getattr(source, "subdir", "") or ""
    log: list[str] = []
    now = time.time()

    def fail(error: str, message: str) -> SkillSourceSyncResult:
        log.append(message)
        return SkillSourceSyncResult(
            name=name,
            ok=False,
            action="failed",
            error=error,
            message=message,
            log=log,
            synced_at=now,
        )

    dest = skill_source_dir(name)
    if dest is None:
        return fail("invalid_name", f"Invalid source name {name!r} (must be lowercase kebab-case).")
    if not repo:
        return fail("missing_repo", f"Source {name!r} has no repo URL.")
    if not _is_safe_registry_subdir(subdir):
        return fail("invalid_subdir", f"Unsafe subdirectory {subdir!r} for source {name!r}.")
    if not _is_valid_branch(branch):
        return fail("invalid_branch", f"Unsafe branch name {branch!r} for source {name!r}.")
    # SSRF gate before any spawn. Loads config from disk, so keep it off the loop.
    if not await asyncio.to_thread(is_clone_host_trusted, repo):
        return fail(
            "untrusted_host",
            f"Refusing to clone {repo!r}: host is not a public forge or a configured registry host.",
        )

    env = minimal_env()
    sandbox_mode = await asyncio.to_thread(_context_clone_sandbox_mode, repo)
    # Commit the mirror was serving before this sync touched it. Used to roll the
    # checkout back if a failure happens AFTER the reset: without this, an
    # upstream change that invalidates the mount (e.g. the configured subdir was
    # deleted) would leave the mirror wiped and the team with no skills at all,
    # even though the sync is reported as failed. Empty on a fresh clone (nothing
    # to restore) and best-effort — a restore that itself fails is logged, not
    # escalated, because the sync is already failing.
    prev_head = ""
    reset_done = False

    async def _restore_previous_head() -> None:
        if not (reset_done and prev_head):
            return
        log.append(f"Restoring the previously synced commit {prev_head[:7]}...")
        try:
            code, out = await _run_git(
                ["reset", "--hard", prev_head],
                cwd=dest,
                repo=dest,
                sandbox_mode=sandbox_mode,
                env=env,
                timeout=_GIT_TIMEOUT,
            )
        except (RuntimeError, OSError) as exc:
            log.append(f"Could not restore {prev_head[:7]}: {exc}")
            return
        if code != 0:
            log.append(f"Could not restore {prev_head[:7]} (exit {code}): {out}")
        else:
            log.append(f"Restored {prev_head[:7]}; the mirror still serves the previous commit.")

    async def fail_after_reset(error: str, message: str) -> SkillSourceSyncResult:
        """Fail a sync that already rewrote the checkout, rolling it back first."""
        await _restore_previous_head()
        return fail(error, message)

    try:
        if _is_cloned(dest):
            action = "updated"
            code, out = await _run_git(
                ["rev-parse", "HEAD"],
                cwd=dest,
                repo=dest,
                sandbox_mode=sandbox_mode,
                env=env,
                timeout=_GIT_TIMEOUT,
            )
            if code == 0:
                prev_head = out.split("\n")[0].strip()[:40]
            log.append(f"Fetching {repo} (branch: {branch})...")
            # Fetch the CONFIGURED url, not the remote named "origin". Two
            # reasons: editing a source's repo field would otherwise keep
            # refreshing from the URL captured at clone time and go on serving
            # the wrong repository's skills; and ``is_clone_host_trusted``
            # validates ``repo``, so fetching anything else would mean the SSRF
            # gate checked a value the spawn does not use.
            code, out = await _run_git(
                ["fetch", "--depth", "1", repo, branch],
                cwd=dest,
                repo=dest,
                sandbox_mode=sandbox_mode,
                env=env,
                timeout=_FETCH_TIMEOUT,
            )
            log.append(out)
            if code != 0:
                return fail("fetch_failed", f"git fetch failed (exit {code}) for {name!r}.")
            # Mirror semantics: the local tree is discardable, so hard-reset onto
            # the fetched tip and clean untracked leftovers. This is what makes an
            # upstream skill *deletion* propagate.
            code, out = await _run_git(
                ["reset", "--hard", "FETCH_HEAD"],
                cwd=dest,
                repo=dest,
                sandbox_mode=sandbox_mode,
                env=env,
                timeout=_GIT_TIMEOUT,
            )
            log.append(out)
            reset_done = True
            if code != 0:
                return await fail_after_reset(
                    "reset_failed", f"git reset failed (exit {code}) for {name!r}."
                )
            code, out = await _run_git(
                ["clean", "-fd"],
                cwd=dest,
                repo=dest,
                sandbox_mode=sandbox_mode,
                env=env,
                timeout=_GIT_TIMEOUT,
            )
            if code != 0:
                # Untracked residue is cosmetic for a read-only mount; note it
                # rather than failing an otherwise-successful sync.
                log.append(f"git clean reported exit {code} (continuing)")
            else:
                log.append(out)
        else:
            action = "cloned"
            if dest.exists():
                # A non-git directory occupying the clone path (interrupted
                # earlier clone, or manual meddling). Clear it so the clone can
                # proceed; it holds no user data by construction.
                log.append(f"Clearing non-git directory at {dest}")
                await asyncio.to_thread(shutil.rmtree, dest, True)
            await asyncio.to_thread(lambda: dest.parent.mkdir(parents=True, exist_ok=True))
            log.append(f"Cloning {repo} (branch: {branch})...")
            code, out = await _run_git(
                [
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    branch,
                    "--single-branch",
                    repo,
                    str(dest),
                ],
                cwd=None,
                sandbox_mode=sandbox_mode,
                env=env,
                timeout=_CLONE_TIMEOUT,
            )
            log.append(out)
            if code != 0:
                await asyncio.to_thread(shutil.rmtree, dest, True)
                return fail("clone_failed", f"git clone failed (exit {code}) for {name!r}.")
    except RuntimeError as exc:
        # wrap_argv fails closed when no sandbox backend is usable.
        return fail("sandbox_unavailable", f"Sandbox unavailable, refusing to run git: {exc}")
    except OSError as exc:
        return fail("io_error", f"Filesystem error syncing {name!r}: {exc}")

    head = ""
    code, out = await _run_git(
        ["rev-parse", "HEAD"],
        cwd=dest,
        repo=dest,
        sandbox_mode=sandbox_mode,
        env=env,
        timeout=_GIT_TIMEOUT,
    )
    if code == 0:
        head = out.split("\n")[0].strip()[:40]

    root = source_skill_root(name, subdir)
    if root is None:
        return await fail_after_reset(
            "missing_skill_root",
            (
                f"Synced {name!r} but {subdir or '<repo root>'} is not a readable "
                "directory in the repo."
            ),
        )
    skill_count = await asyncio.to_thread(count_skills, root)
    if skill_count == 0:
        # A source contributing nothing is almost always a wrong subdir or branch,
        # and link-time validation is documented to catch exactly that. Refusing
        # here (rather than persisting a successful-looking empty source) also
        # means an update that would empty the mirror rolls back instead of
        # silently removing every shared skill.
        return await fail_after_reset(
            "no_skills",
            (
                f"No SKILL.md files found in {name!r} under "
                f"{subdir or '<repo root>'} — check the branch and subdirectory."
            ),
        )
    message = f"{action} {name!r}: {skill_count} skill(s) at {head[:7] or 'unknown'}"
    log.append(message)
    result = SkillSourceSyncResult(
        name=name,
        ok=True,
        action=action,
        head=head,
        skill_count=skill_count,
        message=message,
        log=log,
        synced_at=now,
    )
    return result


def _is_valid_branch(branch: str) -> bool:
    """Reject branch names that could be read as git options or path escapes.

    argv (never a shell string) is used throughout, so quoting is not the
    concern; a leading ``-`` being parsed as a flag is.
    """
    if not branch or len(branch) > 255:
        return False
    if branch.startswith("-") or branch.startswith("/") or branch.endswith("/"):
        return False
    if ".." in branch or "\x00" in branch or "~" in branch or "^" in branch or ":" in branch:
        return False
    return not any(c.isspace() for c in branch)


async def sync_skill_sources(sources: list[Any]) -> list[SkillSourceSyncResult]:
    """Sync every enabled source sequentially, recording each outcome.

    Sequential rather than concurrent: syncs are network-bound but each spawns
    a sandboxed git process, and a handful of linked repos is the expected
    scale. Serial keeps the log readable and the process count bounded.

    Each source is synced under its own :func:`source_lock`, and its config
    entry is RE-READ inside that lock rather than trusted from *sources*. A
    batch sync is slow (one clone per source, up to 60s each), so by the time a
    later entry is reached the user may have unlinked it or edited its repo —
    acting on the opening snapshot would re-clone and re-mount a source that no
    longer exists, or pull from a URL that is no longer configured. Entries that
    disappeared or were disabled are skipped; entries that changed are synced in
    their current form.
    """
    results: list[SkillSourceSyncResult] = []
    for src in sources or []:
        name = getattr(src, "name", "") or ""
        if not name:
            continue
        async with source_lock(name):
            current = await asyncio.to_thread(_current_source, name)
            if current is None:
                logger.info("skill-sources: %r was removed mid-sync; skipping", name)
                continue
            if not getattr(current, "enabled", True):
                continue
            result = await sync_skill_source(current)
            await asyncio.to_thread(record_sync_state, result)
        results.append(result)
    return results


def _current_source(name: str) -> Any | None:
    """Re-read *name*'s entry from config on disk. BLOCKING — call via to_thread."""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        logger.warning("skill-sources: could not re-read config for %r", name, exc_info=True)
        return None
    return next((s for s in cfg.skills.sources if s.name == name), None)


def remove_skill_source_clone(name: str) -> bool:
    """Delete the mirror for *name*. Returns True when something was removed.

    The name is re-validated here (not just at add time) because this reaches
    ``shutil.rmtree`` and the source list comes from an editable config file.
    """
    dest = skill_source_dir(name)
    if dest is None:
        return False
    forget_sync_state(name)
    if not dest.exists():
        return False
    shutil.rmtree(dest, ignore_errors=True)
    return True
