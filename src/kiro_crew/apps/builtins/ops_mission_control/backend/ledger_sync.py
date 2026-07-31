"""Git-native ledger sync — shared memory for a team, with no server.

The workflow this app models kept its team knowledge in a git package
(`shared-lessons.jsonl`, ~2000 lines, append-only, one JSON object per line with an
author and a timestamp). That shape is not an accident and it is not internal: an
append-only JSONL file in a git repo *is* a distributed shared-memory primitive,
provided two writers who learn the same thing produce the same bytes.

This app's ledger already has that property — ids are content-addressed over
`(pattern, fix)`, so two people who independently learn one lesson write one id. What
was missing was the transport. This module is the transport, and deliberately nothing
more:

- **No new data model.** The synced artifact is `ledger.jsonl` exactly as written
  locally. A teammate's copy is readable by an instance that has never heard of sync.
- **No server.** Git is the coordination substrate; GitHub (or any remote) is the
  shared place. Identity and access control are the remote's problem, which is the
  point — a team that can already share a repo can already share memory.
- **Only the ledger.** NOT the dispatch index. The index is last-writer-wins on a
  shared key, so syncing it would silently let two instances believe they each own an
  incident. Cross-instance claim arbitration is a separate contract that has to be
  designed, not a file copy. See the module spec.

**Conflicts are expected and already handled.** Verified against a real `git merge` of
two divergent ledgers: git DOES conflict (both branches append to the same region), so
this is not the "content addressing means no conflicts" story. What content addressing
buys is that the conflicted file is *reconcilable* — `ledger.read_entries` skips the
`<<<<<<<` markers as malformed lines and merges duplicate ids. So the app stays correct
mid-merge, and `resolve_conflict` finishes the job by rewriting the file from the
already-reconciled entries.

**Pull before you match, push after you learn.** Ordering matters: pulling before a
ledger match is what makes a teammate's lesson available to this investigation, and
pushing after recording one is what makes yours available to theirs.

See ``docs/system-specs/modules/ops-mission-control.md`` § Ledger sync.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config
from kiro_crew.sandbox import resource_limit_preexec, sandboxed_spawn_argv
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_GIT_BINARY = "git"

#: Config keys. A remote URL is not a credential (auth is the remote's job — an SSH
#: key or a `gh` login the operator already has), so these live in plain app config.
_ENABLED_KEY = "ledger_sync_enabled"
_REMOTE_KEY = "ledger_sync_remote"
_BRANCH_KEY = "ledger_sync_branch"

DEFAULT_BRANCH = "main"

#: Wall-clock cap per git invocation. A hung fetch against an unreachable remote must
#: not stall the dispatch heartbeat, which is the caller.
GIT_TIMEOUT_SECS = 30.0

#: Marker prefixes git writes into a conflicted file. Listed here rather than inferred
#: so ``resolve_conflict`` and ``ledger.read_entries`` agree on what to ignore.
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


def configured() -> bool:
    """True when the operator enabled sync AND named a remote."""
    cfg = read_config()
    return bool(cfg.get(_ENABLED_KEY)) and bool(str(cfg.get(_REMOTE_KEY, "")).strip())


def remote() -> str:
    return str(read_config().get(_REMOTE_KEY, "")).strip()


def branch() -> str:
    return str(read_config().get(_BRANCH_KEY, "")).strip() or DEFAULT_BRANCH


def status() -> dict[str, Any]:
    """Why sync is or is not usable — surfaced in Settings.

    Distinguishes the failure modes because they need different fixes: off (flip the
    toggle), no remote (enter one), and no git repo yet (it gets created on first sync).
    """
    cfg = read_config()
    enabled = bool(cfg.get(_ENABLED_KEY))
    url = str(cfg.get(_REMOTE_KEY, "")).strip()
    initialized = (_repo_root() / ".git").is_dir()
    if not enabled:
        detail = "Off. Turn on to share the knowledge ledger with your team over git."
    elif not url:
        detail = "No remote set — enter a git URL (SSH or HTTPS) your team can push to."
    elif not initialized:
        detail = f"Ready. The repo is created on the first sync ({url})."
    else:
        detail = f"Syncing {url} on branch {branch()}."
    return {
        "enabled": enabled,
        "remote": url,
        "branch": branch(),
        "initialized": initialized,
        "ready": enabled and bool(url),
        "detail": detail,
    }


def set_settings(
    *,
    enabled: bool | None = None,
    remote_url: str | None = None,
    branch_name: str | None = None,
) -> None:
    """Persist the operator's choice. Non-secret, so plain app config."""
    from kiro_crew.apps.builtins.ops_mission_control.backend.providers import write_config

    cfg = read_config()
    if enabled is not None:
        cfg[_ENABLED_KEY] = bool(enabled)
    if remote_url is not None:
        cfg[_REMOTE_KEY] = remote_url.strip()
    if branch_name is not None:
        cfg[_BRANCH_KEY] = branch_name.strip()
    write_config(cfg)


def _repo_root() -> Path:
    """The ledger's own directory. The ledger file IS the repo's content.

    Syncing the directory that already holds ``ledger.jsonl`` avoids a copy step and
    the drift a copy invites. Note this directory also holds other app data, so
    ``_ensure_repo`` writes a ``.gitignore`` that tracks the ledger and nothing else —
    the dispatch index must never be committed (see the module docstring).
    """
    return ledger.ledger_path().parent


async def _git(*args: str) -> tuple[int, str, str]:
    """Run one git command in the ledger directory, sandboxed.

    Routed through ``sandboxed_spawn_argv`` + ``resource_limit_preexec`` because the
    remote URL and branch come from config an agent can influence, and git reads its
    own config files on the way. This is the chokepoint ``test/test_spawn_audit.py``
    requires. Never raises on a non-zero exit — the caller decides what that means.
    """
    argv, env, cleanup = sandboxed_spawn_argv([_GIT_BINARY, *args])
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(_repo_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            preexec_fn=resource_limit_preexec(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", f"git {args[0]} timed out after {GIT_TIMEOUT_SECS}s"
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except OSError as exc:
        return 127, "", f"could not run git: {exc}"
    finally:
        # The third value is a temp-profile PATH, not a callable — same handling as
        # the github_issues adapter. Assuming it was a cleanup function is what mypy
        # caught ("str" not callable).
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)


async def _ensure_repo() -> tuple[bool, str]:
    """Initialize the repo and its remote if needed. Idempotent."""
    root = _repo_root()
    root.mkdir(parents=True, exist_ok=True)

    # Track ONLY the shared-knowledge files: the ledger and the on-call schedule. The
    # dispatch index, provider config, and incident logs live in this same directory and
    # must never be pushed — the index because it is not merge-safe, the rest because it
    # is local state (and config could name a log group an operator considers private).
    #
    # ``rotation.yaml`` is here because a schedule only works if every teammate reads the
    # same one; a locally-written file that never syncs is worse than no schedule, since
    # it looks configured while disagreeing with everyone else. It is small, human-edited,
    # and text — the same merge profile as the ledger.
    gitignore = root / ".gitignore"
    wanted = "*\n!.gitignore\n!ledger.jsonl\n!rotation.yaml\n"
    if not gitignore.exists() or gitignore.read_text(encoding="utf-8") != wanted:
        gitignore.write_text(wanted, encoding="utf-8")

    if not (root / ".git").is_dir():
        rc, _, err = await _git("init", "-q")
        if rc != 0:
            return False, f"git init failed: {err.strip()[:200]}"

    rc, out, _ = await _git("remote", "get-url", "origin")
    url = remote()
    if rc != 0:
        rc, _, err = await _git("remote", "add", "origin", url)
        if rc != 0:
            return False, f"git remote add failed: {err.strip()[:200]}"
    elif out.strip() != url:
        # The operator changed the remote in Settings; follow it rather than silently
        # continuing to sync the old one.
        rc, _, err = await _git("remote", "set-url", "origin", url)
        if rc != 0:
            return False, f"git remote set-url failed: {err.strip()[:200]}"
    return True, ""


def has_conflict() -> bool:
    """True when the ledger file currently holds git conflict markers."""
    path = ledger.ledger_path()
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.startswith(CONFLICT_MARKERS) for line in text.splitlines())


#: The on-call schedule. Named here rather than imported from ``schedule_file`` to keep
#: this module free of a dependency on a provider (the transport must not know which
#: rotation source exists); ``TRACKED_FILES`` already carries the same literal.
_SCHEDULE_FILENAME = "rotation.yaml"


def schedule_has_conflict() -> bool:
    """True when ``rotation.yaml`` currently holds git conflict markers.

    Separate from ``has_conflict`` (which is ledger-only) because a conflicted schedule
    is far more dangerous than a conflicted ledger: markers make the YAML unparseable,
    and an unparseable schedule means no instance can tell whether it is on call.
    """
    path = _repo_root() / _SCHEDULE_FILENAME
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.startswith(CONFLICT_MARKERS) for line in text.splitlines())


async def _resolve_schedule_conflict() -> bool:
    """Take the REMOTE's schedule when it conflicts. Returns whether it did.

    "Theirs" rather than a merge attempt: a shift is a single-owner fact, so there is no
    union to compute — one of the two edits has to lose, and the remote is the version the
    rest of the team is already acting on. Converging on it keeps every instance's view of
    who is on call identical, which is the property that makes the file usable as a lock.
    The local edit is not destroyed; it stays in the reflog and the operator can re-apply
    and push it.
    """
    if not schedule_has_conflict():
        return False
    rc, _, err = await _git("checkout", "--theirs", "--", _SCHEDULE_FILENAME)
    if rc != 0:
        logger.warning(
            "ops-mission-control: could not take the remote schedule: %s", err.strip()[:200]
        )
        return False
    await _git("add", "--", _SCHEDULE_FILENAME)
    sel().log_api_access(
        caller="core:ops-mission-control",
        operation="ledger_sync_schedule_conflict",
        outcome="success",
        resources="resolution=theirs",
    )
    logger.warning(
        "ops-mission-control: %s conflicted on pull; took the remote version. A local "
        "edit to the on-call schedule was NOT merged — re-apply and push it if it is "
        "still wanted.",
        _SCHEDULE_FILENAME,
    )
    return True


def resolve_conflict() -> int:
    """Rewrite the ledger from its own reconciled entries, dropping markers.

    ``read_entries`` already tolerates conflict markers and merges duplicate ids, so
    the reconciled view is available *before* this runs — which is why the app stays
    correct mid-merge. This makes that view durable so git sees a clean file.

    Returns the number of entries kept. Safe to call when there is no conflict: it
    rewrites the same content.
    """
    entries = ledger.read_entries()
    ledger._write_all(entries)  # same writer upsert/hygiene use, so format cannot drift
    sel().log_api_access(
        caller="core:ops-mission-control",
        operation="ledger_sync_resolve",
        outcome="success",
        resources=f"entries={len(entries)}",
    )
    return len(entries)


async def pull() -> tuple[bool, str]:
    """Fetch and merge the team's ledger. Call BEFORE matching.

    A teammate's lesson is only useful to this investigation if it arrives before the
    fingerprint lookup. Returns ``(ok, detail)`` and never raises: sync is a
    convenience, and a dispatch cycle must not fail because a remote was unreachable.
    """
    if not configured():
        return False, "ledger sync is not configured"
    ok, err = await _ensure_repo()
    if not ok:
        return False, err

    rc, _, err = await _git("fetch", "--quiet", "origin", branch())
    if rc != 0:
        # An empty remote has no branch yet — normal on first use, not an error.
        detail = err.strip()[:200]
        if "couldn't find remote ref" in detail or "not found" in detail.lower():
            return True, "remote has no ledger branch yet (first sync will create it)"
        return False, f"fetch failed: {detail}"

    # Stage-and-commit any local tracked file BEFORE merging. Without this, git refuses
    # the merge outright — "Untracked working tree file 'ledger.jsonl' would be
    # overwritten by merge" — so an instance that recorded even one lesson before its
    # first pull could NEVER pull, permanently. Found by a real two-instance roundtrip
    # against a bare remote; every unit test passed because they mock git.
    #
    # Committing first is also the correct semantic: this instance's entries are real
    # work, and the merge is meant to UNION them with the team's, which is exactly what
    # the conflict reconciler below does.
    await _stage_and_commit("local ops ledger before merge")

    # ``--allow-unrelated-histories`` is REQUIRED here, not a convenience. Every instance
    # runs its own ``git init`` against a shared remote, so two installs that each
    # recorded a lesson before their first pull have genuinely unrelated root commits and
    # git refuses outright ("fatal: refusing to merge unrelated histories") — the second
    # teammate to join could never pull, which is the ordinary multi-instance case, not an
    # edge case. Also found by the real roundtrip.
    #
    # The flag is safe precisely because of how this repo is shaped: the tracked content
    # is a content-addressed union (ledger ids are sha256 over pattern+fix) and the
    # conflict path below reconciles duplicates rather than picking a side, so joining two
    # histories cannot lose an entry. On a normal source repo this flag would be reckless.
    rc, _, err = await _git(
        "merge", "--no-edit", "--allow-unrelated-histories", f"origin/{branch()}"
    )
    if rc != 0:
        # The SCHEDULE is checked first and handled differently from the ledger, because
        # the two have opposite merge semantics.
        #
        # A ledger conflict is reconcilable: entries are content-addressed, so the union
        # is unambiguously correct. A rotation.yaml conflict is a genuine disagreement —
        # two people edited the same shift — and there is no safe automatic answer.
        #
        # Left alone, the markers made the YAML unparseable, which under fail-open
        # RE-ARMED EVERY INSTANCE: observed in a three-teammate run through a real repo,
        # where a conflicted schedule reported `team=[]` and all three instances armed —
        # the exact double-claim the shared schedule exists to prevent. Taking THEIRS is
        # the safe resolution: the remote is what the rest of the team is already acting
        # on, so converging on it keeps every instance's view identical, and the local
        # edit is recoverable from the reflog rather than silently merged into nonsense.
        schedule_conflicted = await _resolve_schedule_conflict()

        if has_conflict():
            kept = resolve_conflict()
            await _stage_and_commit("merge team ledger", allow_empty_message_only=True)
            detail = f"merged with conflict, reconciled to {kept} entries"
            if schedule_conflicted:
                detail += "; schedule conflict resolved to the remote's version"
            return True, detail
        if schedule_conflicted:
            await _stage_and_commit("take remote schedule", allow_empty_message_only=True)
            return True, "schedule conflict resolved to the remote's version"
        return False, f"merge failed: {err.strip()[:200]}"
    return True, "pulled"


#: Files the sync tracks. The ledger is the shared knowledge; ``rotation.yaml`` is the
#: on-call schedule (see ``providers/schedule_file.py``). Both are small, human-edited
#: text that merges. Everything else in this directory is local state — the dispatch
#: index is not merge-safe, and provider config could name a private log group.
TRACKED_FILES: tuple[str, ...] = ("ledger.jsonl", "rotation.yaml", ".gitignore")


async def _stage_and_commit(message: str, *, allow_empty_message_only: bool = False) -> bool:
    """Stage every tracked file and commit if anything changed.

    Returns True when a commit was made. Staging the whole tracked SET rather than just
    the ledger is load-bearing: ``rotation.yaml`` is un-ignored so it can sync, but a
    push that only ever ran ``git add ledger.jsonl`` would leave the schedule committed
    nowhere and silently never reach teammates.
    """
    for name in TRACKED_FILES:
        if (_repo_root() / name).exists():
            await _git("add", "--", name)
    rc, out, _ = await _git("status", "--porcelain")
    if rc == 0 and not out.strip() and not allow_empty_message_only:
        return False
    rc, _, err = await _git("commit", "--no-edit", "-q", "-m", message)
    if rc != 0 and "nothing to commit" not in err.lower():
        logger.debug("ops-mission-control: ledger commit skipped: %s", err.strip()[:200])
        return False
    return rc == 0


async def push(*, message: str = "update ops ledger") -> tuple[bool, str]:
    """Commit and push the local ledger. Call AFTER recording a lesson."""
    if not configured():
        return False, "ledger sync is not configured"
    ok, err = await _ensure_repo()
    if not ok:
        return False, err

    if has_conflict():
        # Never push a conflicted file to teammates.
        resolve_conflict()

    # A conflicted SCHEDULE must never reach the remote, and unlike the ledger it cannot
    # be auto-reconciled — so REFUSE rather than guess. This is not defensive
    # hypothesising: an earlier three-teammate run pushed a schedule containing conflict
    # markers, and from then on every teammate's pull faithfully received a file that
    # cannot be parsed. An unparseable schedule means no instance can tell whether it is
    # on call, so one bad push disarms (or, under fail-open, wrongly arms) the entire
    # team, and no amount of downstream conflict handling can recover it — "theirs" is
    # already corrupt.
    #
    # Refusing costs one operator a push they must fix by hand; publishing costs the whole
    # team its on-call gating.
    if schedule_has_conflict():
        logger.error(
            "ops-mission-control: refusing to push — %s holds conflict markers. Resolve "
            "the on-call schedule by hand; pushing it would leave every teammate unable "
            "to parse who is on call.",
            _SCHEDULE_FILENAME,
        )
        sel().log_api_access(
            caller="core:ops-mission-control",
            operation="ledger_sync_push",
            outcome="refused",
            resources=f"reason=conflicted_{_SCHEDULE_FILENAME}",
        )
        return False, f"refused: {_SCHEDULE_FILENAME} holds conflict markers — resolve it first"

    committed = await _stage_and_commit(message)
    # A clean tree is not automatically "nothing to push": a previous run may have
    # committed locally and then failed to reach the remote, and returning early there
    # would strand that commit forever. Only skip when there is also nothing unpushed.
    if not committed and not await _has_unpushed():
        return True, "nothing to push"

    rc, _, err = await _git("push", "--quiet", "origin", f"HEAD:{branch()}")
    if rc != 0:
        return False, f"push failed: {err.strip()[:200]}"
    sel().log_api_access(
        caller="core:ops-mission-control",
        operation="ledger_sync_push",
        outcome="success",
        resources=f"remote={remote()} branch={branch()}",
    )
    return True, "pushed"


async def _has_unpushed() -> bool:
    """True when HEAD holds commits the remote branch does not.

    Distinguishes "clean tree, all shared" from "clean tree, but a previous push never
    landed". Treats an unknown answer as True: attempting a redundant push is cheap,
    while skipping a needed one strands a lesson locally forever.
    """
    rc, out, _ = await _git("rev-list", "--count", f"origin/{branch()}..HEAD")
    if rc != 0:
        return True
    try:
        return int(out.strip() or "0") > 0
    except ValueError:
        return True


async def sync_safely(*, direction: str = "pull") -> str:
    """Run a sync step, swallowing every fault. Returns a short outcome string.

    The dispatch cycle and the daily hygiene pass call this. Shared memory improving an
    investigation is worth having; it is never worth losing a claim over, so an
    unreachable remote degrades to "this instance works from what it already knows".

    One retry on the FIRST attempt, because the sandbox backend probe is deliberately
    deferred off the event loop on a cold cache and raises a self-described TRANSIENT
    error telling the caller to retry ("cache warms in ms"). A real roundtrip hit this
    on every first push in a fresh process — the whole first sync failed for a condition
    that resolves in milliseconds. The retry is bounded at one and only re-runs an
    idempotent git step, so it cannot mask a genuine fault.
    """
    if not configured():
        return ""
    last = ""
    for attempt in (1, 2):
        try:
            ok, detail = await (pull() if direction == "pull" else push())
        except Exception as exc:  # noqa: BLE001 — sync must never break a cycle
            last = f"{direction} errored"
            transient = "retry" in str(exc).lower() or "transient" in str(exc).lower()
            if attempt == 1 and transient:
                logger.debug(
                    "ops-mission-control: ledger %s hit a transient spawn fault; retrying",
                    direction,
                )
                await asyncio.sleep(0.25)
                continue
            logger.exception("ops-mission-control: ledger %s failed", direction)
            return last
        if not ok:
            logger.warning("ops-mission-control: ledger %s: %s", direction, detail)
        return detail
    return last
