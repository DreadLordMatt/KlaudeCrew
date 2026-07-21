"""One-time import of a legacy ``~/.meshclaw`` data dir into the current
KiroCrew home (``~/.kirocrew``), followed by a reversible rename of the
legacy dir to ``~/.meshclaw.bak``.

Why this exists
---------------
KiroCrew is the renamed successor to MeshClaw. When a MeshClaw user first
launches KiroCrew, their old data lives in ``~/.meshclaw`` while the new
app reads/writes ``~/.kirocrew`` (the running app sets ``KIROCREW_HOME`` to
its own home, so :func:`config_dir` resolves to the *destination*). This
module offers a one-time, consent-gated import that mirrors the manual
``meshclaw-to-kirocrew-migrate.sh`` selective-merge script exactly.

Design (two-phase, boot-safe)
-----------------------------
The import replaces ``memory.db`` and ``workspace/knowledge/knowledge.db``
in place and finally *renames* the legacy dir, so it MUST NOT run while any
of those SQLite stores are open. It therefore runs in two phases:

* Phase 1 (onboarding, UI-driven): the dashboard ``.../start`` endpoint
  writes an *intent* marker and restarts the gateway.
* Phase 2 (early boot): :func:`run_pending_meshclaw_import` runs from
  ``cli_server._gateway()`` **before** ``run_gateway()`` opens any store —
  if the intent marker is present it performs the import, renames the
  legacy dir, writes the *done* marker, and clears the intent.

Most copy steps are additive, but the old-wins/merge steps (``memory.db``,
``preferences.md``/``projects.md``, ``knowledge.db``, the session-map /
folders / crons merges) overwrite destination files in place, so the copy
phase is ALL-OR-NOTHING: a fail-closed, write-once backup of exactly those
targets (dbs together with their ``-wal``/``-shm`` sidecars) is taken under
``.migrations/meshclaw_import_dest_backup/`` BEFORE anything is overwritten
(each backup file is written atomically; a backup failure aborts before any
overwrite, so nothing needs undoing). During the copy every additive path
the run creates in the destination is journaled, and on ANY failure —
including an unexpected exception — the run rolls back completely: the
overwrite targets are restored from the backup, the journaled additive
files/dirs are deleted, and the backup dir plus ALL run markers (including
the intent) are cleared. The destination is back to its fresh pre-import
state, boot proceeds normally, and the consent-gated import offer simply
becomes available again. There is NO retained retry state.

The on-disk backup dir doubles as a durable crash record: a later boot that
finds a dest backup with no *copied* marker (the prior run was killed
mid-copy — SIGKILL/power loss — before it could either commit or roll back)
restores the overwrite targets from it, consumes the backup, clears the
intent, and does NOT import (re-running the copy needs fresh consent).
Additive residue from the crashed run cannot be enumerated (the journal is
memory-only); if it keeps the destination looking non-fresh the import
offer simply stays withdrawn. A backup missing its completion marker (the
crash hit the backup phase itself, before any overwrite) is discarded
untrusted rather than restored from.

Freshness is re-validated at execution time with no bypass: if no fully
successful copy is recorded and the destination no longer looks fresh, the
run bails (the user accrued real data since consenting). The intent marker
has a simple max-age TTL — a stale intent is dropped, not honored.

A *copied* marker records a fully successful copy phase, after which only
the final rename remains: a later boot re-attempts just the rename and
never re-copies (which could clobber post-import changes in the
destination). The rename retry is bounded by an attempt counter in the
copied marker; after the cap the run gives up — the completed import stays
in place (the destination is whole), the intent is cleared, and a prominent
log tells the user to move ``~/.meshclaw`` aside manually. The rename
itself is ``os.rename`` only and reversible (``~/.meshclaw.bak`` is never
deleted). Do not run the import while a separate legacy MeshClaw app is
actively writing ``~/.meshclaw`` — the WAL sidecars are snapshotted
best-effort and the ``.bak`` rename preserves the originals.

Manifest parity
---------------
The copy manifest is a faithful Python port of
``meshclaw-to-kirocrew-migrate.sh``:

* sessions/ additive; session_map.json + folders.json union-merge (new wins)
* memory.db old-wins together with its ``-wal``/``-shm`` sidecars when the
  source has them (consistent snapshot; stale dest sidecars dropped
  otherwise); ``memory_index.db`` always dropped so the embedding index
  rebuilds on launch
* workspace docs additive; workspace ``memory/`` (preferences.md/projects.md
  old-wins, ``history/`` additive); workspace ``knowledge/knowledge.db``
  old-wins with its ``-wal``/``-shm`` snapshot (stale dest sidecars dropped
  otherwise). Overwritten dest files are first backed up under
  ``.migrations/meshclaw_import_dest_backup/``
* apps: 10 meshclaw-only apps copied in full; 7 shared apps get ``data/``
  only (additive); the ~2900 stub app dirs (no ``installed.json``) are
  never touched because only these explicit lists are copied
* skills: copy each dir not already present and not a built-in ``meshclaw-*``
* artifacts/ + uploads/ additive
* crons.json MERGED into any existing dest file (dest jobs kept, dest wins
  on id collision) with every imported legacy job ``enabled=false`` AND
  ``user_paused=true``; legacy dict/list forms both accepted; crons/ +
  cron-history/ additive
* mcp.json copied only if absent (else parked at ``mcp.json.from-meshclaw``);
  mcp-servers/ additive
* .env copied only if absent (in-product self-migration of the app's own
  data dir)
* misc user state (lessons/tags/secretary/mochi files absent-only; plan_memory,
  tasks, live_views, review-requests, workspace-oncall, writing_review,
  agent-metadata dirs additive)
* config.json is NEVER copied
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

# ── Names ────────────────────────────────────────────────────────────────
LEGACY_DIR_NAME = ".meshclaw"
BACKUP_DIR_NAME = ".meshclaw.bak"
MARKER_DIR_NAME = ".migrations"
DONE_MARKER = "meshclaw_import_done"
INTENT_MARKER = "meshclaw_import_requested"
COPIED_MARKER = "meshclaw_import_copied"
DEST_BACKUP_DIR = "meshclaw_import_dest_backup"
# Written inside the dest-backup dir once EVERY target has been copied. Its
# absence marks a backup the copy phase never started from: a crash during
# the backup phase leaves the destination untouched, so such a partial
# backup must be discarded, never restored from (it may be missing targets
# that DID exist pre-import, and a restore would delete them).
_BACKUP_COMPLETE_MARKER = ".backup-complete"
# Written inside the dest-backup dir when a restore attempt could not put
# every target back; the backup dir is then RETAINED for the next attempt
# (and for manual recovery) instead of being deleted.
_RESTORE_INCOMPLETE_MARKER = ".restore-incomplete"

# Dest files that the old-wins/merge steps overwrite; backed up (when they
# exist) into ``.migrations/meshclaw_import_dest_backup/`` before the copy
# and restored from there if the copy phase fails. The SQLite dbs are
# backed up together with their ``-wal``/``-shm`` sidecars so the backup is
# WAL-consistent; session_map.json / folders.json are included because the
# union merges rewrite them in place.
_DEST_BACKUP_TARGETS = (
    "memory.db",
    "memory.db-wal",
    "memory.db-shm",
    "session_map.json",
    "folders.json",
    "workspace/memory/preferences.md",
    "workspace/memory/projects.md",
    "workspace/knowledge/knowledge.db",
    "workspace/knowledge/knowledge.db-wal",
    "workspace/knowledge/knowledge.db-shm",
    "crons.json",
)

# SQLite dbs restored as a family SET in a safe order: the destination's
# ``-wal``/``-shm`` sidecars are strictly dropped BEFORE the db file is
# swapped in (a foreign sidecar beside a replaced db would corrupt it on
# open), then the backed-up sidecars (if any) are restored after it.
_SQLITE_FAMILY_DBS = ("memory.db", "workspace/knowledge/knowledge.db")

# The final rename is retried across boots at most this many times (bounds
# e.g. an EXDEV loop where os.rename can never succeed). After the cap the
# run gives up: the completed import stays in place and the intent is
# cleared (the destination is whole, so this is safe).
_MAX_RENAME_ATTEMPTS = 3
# An intent marker older than this with no committed copy is stale consent
# (e.g. the restart never happened) and is dropped, not honored.
_INTENT_MAX_AGE_SECONDS = 15 * 60

# A fresh-init destination memory.db is tiny; anything larger means the
# user has real data in the new home and the import offer is withdrawn.
_FRESH_MEMORY_DB_MAX_BYTES = 1024 * 1024

# Apps copied in FULL (meshclaw-only apps, not present in a fresh KiroCrew).
APPS_FULL = (
    "auto-improvement", "board", "code-reviewer", "deploy-web", "mimir",
    "oncall-radar", "secretary", "taskkeeper", "team-manager", "writing-review",
)
# Shared apps: only their ``data/`` subdir is imported (additive).
APPS_SHARED = (
    "agent-worlds", "auto-research", "channels", "code-review-sage",
    "file-explorer", "projects", "workflows",
)

# workspace/ top-level names handled explicitly (excluded from the additive
# docs copy).
_WORKSPACE_EXCLUDE = frozenset({"memory", "knowledge", ".kiro", "HEARTBEAT.md"})

# Absent-only single files under the data-dir root.
_MISC_FILES = (
    "lessons.jsonl", "lessons.md", "tags.json", "tag_boards.json",
    "secretary.json", "secretary_state.json", "notifications.jsonl",
    "mochi-queue.json", "mochi-activity.json", "mochi-watchlist.json",
    "mochi-chat-history.json", "mochi-prompt.md", "mochi-prompt-bg.md",
)
# Additive directories under the data-dir root.
_MISC_DIRS = (
    "plan_memory", "tasks", "live_views", "review-requests",
    "workspace-oncall", "writing_review", "agent-metadata",
)

# Top-level source dirs excluded from the size estimate (huge / ephemeral and
# not part of the migrated footprint), mirroring the backup exclude in the
# reference script.
_SIZE_SKIP_TOP = frozenset({"models", "run"})
# Individual files never part of the migrated footprint.
_SIZE_SKIP_FILES = frozenset({"security_events.jsonl", "config.json"})
# Memoized size estimates keyed by root path so /status polls don't re-walk.
_size_estimate_cache: dict[str, int] = {}


# ── Path seams (monkeypatched in tests) ────────────────────────────────────
def _home() -> Path:
    return Path.home()


def _legacy_source() -> Path:
    return _home() / LEGACY_DIR_NAME


def _legacy_backup() -> Path:
    return _home() / BACKUP_DIR_NAME


def _marker_dir() -> Path:
    return config_dir() / MARKER_DIR_NAME


def _done_marker_path() -> Path:
    return _marker_dir() / DONE_MARKER


def _intent_marker_path() -> Path:
    return _marker_dir() / INTENT_MARKER


def _copied_marker_path() -> Path:
    return _marker_dir() / COPIED_MARKER


def _dest_backup_dir() -> Path:
    return _marker_dir() / DEST_BACKUP_DIR


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _nested_paths(a: Path, b: Path) -> bool:
    """True iff one path is nested under the other (after resolving).

    A nested source/destination would make the copy recurse into itself and
    the final rename destroy live data, so both :func:`detect...` and
    :func:`run_meshclaw_import` bail when this holds.
    """
    try:
        ra, rb = a.resolve(), b.resolve()
    except OSError:
        return False
    if ra == rb:
        return False
    return ra.is_relative_to(rb) or rb.is_relative_to(ra)


# ── Run journal (all-or-nothing copy phase) ─────────────────────────────────
class _RunJournal:
    """Records every path the running copy phase newly creates in the
    destination, so a failed copy can delete its additive residue and leave
    the destination exactly as it was pre-import."""

    def __init__(self) -> None:
        self.files: list[Path] = []
        self.dirs: list[Path] = []


# Active journal for the in-flight copy phase (None outside of it). The copy
# runs single-threaded at early boot, so a module global is safe and spares
# threading a context object through every helper.
_journal = None  # type: _RunJournal | None


def _record_new_file(p: Path) -> None:
    """Journal a destination file/symlink this run is about to create."""
    if _journal is not None:
        _journal.files.append(p)


def _mkdirs(p: Path) -> None:
    """``mkdir -p`` that journals every directory it actually creates."""
    if p.exists():
        return
    missing: list[Path] = []
    q = p
    while not q.exists():
        missing.append(q)
        q = q.parent
    p.mkdir(parents=True, exist_ok=True)
    if _journal is not None:
        _journal.dirs.extend(missing)


# ── Low-level copy helpers (pure-Python; no rsync dependency) ───────────────
def _unlink(p: Path) -> None:
    """Best-effort unlink (marker cleanup etc.); never raises."""
    try:
        if p.is_symlink() or p.exists():
            p.unlink()
    except OSError:
        logger.debug("could not unlink %s", p, exc_info=True)


def _unlink_strict(p: Path) -> None:
    """Unlink that RAISES on failure.

    Used on overwrite paths: silently keeping a surviving destination entry
    (e.g. a symlink) and then copying *through* it would write outside the
    destination tree, so a failed unlink must fail the step.
    """
    if p.is_symlink() or p.exists():
        p.unlink()


def _fsync_path(p: Path) -> None:
    """fsync a file by path (durability before an ``os.replace``)."""
    fd = os.open(p, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(p: Path) -> None:
    """Best-effort fsync of a directory (durability of a rename/replace)."""
    try:
        fd = os.open(p, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _raise_walk_error(exc: OSError) -> None:
    """``os.walk(onerror=...)`` hook: an unreadable subtree fails the step
    instead of being silently skipped (which would look like success)."""
    raise exc


def _copy_entry(s: Path, d: Path) -> None:
    """Copy a single file or symlink ``s`` -> ``d`` (parent created).

    Symlinks are preserved as symlinks (matching ``rsync -a`` / ``cp -a``);
    regular files are copied with metadata via ``copy2``. Callers are
    responsible for journaling ``d`` when it is newly created.
    """
    _mkdirs(d.parent)
    if s.is_symlink():
        try:
            os.symlink(os.readlink(s), d)
        except OSError:
            logger.debug("could not recreate symlink %s", d, exc_info=True)
        return
    shutil.copy2(s, d)


def _atomic_write_text(p: Path, text: str) -> None:
    """Atomically write ``text`` to ``p`` (temp file + fsync + ``os.replace``)."""
    _mkdirs(p.parent)
    new = not (p.is_symlink() or p.exists())
    tmp = p.with_name(p.name + ".tmp-import")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        _fsync_dir(p.parent)
    finally:
        _unlink(tmp)
    if new:
        _record_new_file(p)


def _split_symlink_dirs(root_p: Path, dirs: list) -> tuple[list, list]:
    """Partition ``dirs`` into (regular, symlinked) directory names."""
    regular: list = []
    linked: list = []
    for dn in dirs:
        (linked if (root_p / dn).is_symlink() else regular).append(dn)
    return regular, linked


def _copytree_additive(src: Path, dst: Path, *, exclude: frozenset[str] = frozenset()) -> None:
    """Additive recursive copy (``rsync -a --ignore-existing``).

    Never overwrites an existing destination entry. ``exclude`` names are
    pruned at the *top level* of ``src`` only. Symlinked directories are
    recreated as symlinks (never followed/expanded).
    """
    if not src.is_dir():
        return
    for root, dirs, files in os.walk(src, onerror=_raise_walk_error):
        root_p = Path(root)
        rel = root_p.relative_to(src)
        if rel == Path("."):
            dirs[:] = [x for x in dirs if x not in exclude]
            files = [x for x in files if x not in exclude]
        target_dir = dst / rel
        _mkdirs(target_dir)
        # Recreate symlinked dirs as symlinks; prune them from recursion.
        regular, linked = _split_symlink_dirs(root_p, dirs)
        dirs[:] = regular
        for dn in linked:
            d = target_dir / dn
            if d.is_symlink() or d.exists():
                continue
            try:
                os.symlink(os.readlink(root_p / dn), d)
                _record_new_file(d)
            except OSError:
                logger.debug("could not recreate dir symlink %s", d, exc_info=True)
        for fn in files:
            s = root_p / fn
            d = target_dir / fn
            if d.is_symlink() or d.exists():
                continue
            # Journal BEFORE copying so a partially-written file is still
            # cleaned up by the rollback.
            _record_new_file(d)
            _copy_entry(s, d)


def _copytree_overwrite(src: Path, dst: Path) -> None:
    """Recursive copy (``rsync -a``): overwrites existing destination files.

    Symlinked directories in the *source* are recreated as symlinks (never
    followed). A destination DIRECTORY position occupied by a symlink is
    strictly unlinked (raise on failure) and replaced with a real directory —
    never written *through*, which would land files outside the destination
    tree. An existing *real* directory at the destination is left in place.
    """
    if not src.is_dir():
        return
    for root, dirs, files in os.walk(src, onerror=_raise_walk_error):
        root_p = Path(root)
        rel = root_p.relative_to(src)
        target_dir = dst / rel
        if target_dir.is_symlink():
            # Never write through a dest dir symlink into an outside tree.
            _unlink_strict(target_dir)
        _mkdirs(target_dir)
        # Recreate symlinked dirs as symlinks; prune them from recursion.
        regular, linked = _split_symlink_dirs(root_p, dirs)
        dirs[:] = regular
        for dn in linked:
            d = target_dir / dn
            new = not (d.is_symlink() or d.exists())
            if d.is_symlink() or d.is_file():
                _unlink_strict(d)
            if d.exists():  # a real dir: don't destroy it to plant a link
                continue
            try:
                os.symlink(os.readlink(root_p / dn), d)
                if new:
                    _record_new_file(d)
            except OSError:
                logger.debug("could not recreate dir symlink %s", d, exc_info=True)
        for fn in files:
            s = root_p / fn
            d = target_dir / fn
            new = not (d.is_symlink() or d.exists())
            if not new:
                _unlink_strict(d)
            else:
                # Journal BEFORE copying so a partially-written file is
                # still cleaned up by the rollback.
                _record_new_file(d)
            _copy_entry(s, d)


def _copy_file_overwrite(s: Path, d: Path) -> None:
    """``cp -a`` a single file, overwriting the destination (old-wins).

    Regular files are copied to a temp file in the destination directory,
    fsynced, and swapped in with ``os.replace`` (atomic — a crash mid-copy
    can never leave a torn destination file, and a symlink at the
    destination is replaced, never followed). Symlink sources are strictly
    unlinked-then-recreated.
    """
    if not s.is_file():
        return
    _mkdirs(d.parent)
    new = not (d.is_symlink() or d.exists())
    if s.is_symlink():
        _unlink_strict(d)
        if new:
            _record_new_file(d)
        _copy_entry(s, d)
        return
    tmp = d.with_name(d.name + ".tmp-import")
    try:
        shutil.copy2(s, tmp)
        _fsync_path(tmp)
        os.replace(tmp, d)
        _fsync_dir(d.parent)
    finally:
        _unlink(tmp)
    if new:
        _record_new_file(d)


def _copy_file_if_absent(s: Path, d: Path) -> None:
    """Copy a single file only if the destination does not already exist."""
    if not s.is_file():
        return
    if d.is_symlink() or d.exists():
        return
    _record_new_file(d)
    _copy_entry(s, d)


# ── JSON merges ─────────────────────────────────────────────────────────────
def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _merge_session_map(src: Path, dst: Path) -> None:
    """Union-merge session_map.json (new/dest wins on key collisions)."""
    sp = src / "session_map.json"
    dp = dst / "session_map.json"
    if not sp.is_file() and not dp.is_file():
        return  # nothing to merge — don't materialize an empty {}
    old = _load_json(sp, {})
    new = _load_json(dp, {})
    if not isinstance(old, dict) or not isinstance(new, dict):
        return
    merged = dict(old)
    merged.update(new)  # new wins
    _atomic_write_text(dp, json.dumps(merged, indent=2))


def _merge_folders(src: Path, dst: Path) -> None:
    """Union-merge folders.json (new/dest wins), dedup by ``id`` (or value)."""
    sp = src / "folders.json"
    dp = dst / "folders.json"
    if not sp.is_file() and not dp.is_file():
        return  # nothing to merge — don't materialize an empty []
    old = _load_json(sp, [])
    new = _load_json(dp, [])
    if not isinstance(old, list) or not isinstance(new, list):
        return

    def key(x):
        if isinstance(x, dict) and x.get("id") is not None:
            return x.get("id")
        return json.dumps(x, sort_keys=True)

    seen: set = set()
    out: list = []
    for x in list(new) + list(old):  # new first -> new wins
        k = key(x)
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    _atomic_write_text(dst / "folders.json", json.dumps(out, indent=2))


def _normalize_legacy_cron_jobs(data) -> list:
    """Normalize a legacy crons.json payload to a list of job dicts.

    Accepts both the dict form (``{"jobs": [...]}``) and the older
    top-level-list form.
    """
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
    elif isinstance(data, list):
        jobs = data
    else:
        return []
    return [j for j in jobs if isinstance(j, dict)]


def _import_crons(src: Path, dst: Path) -> None:
    """MERGE legacy crons.json into any existing dest crons.json.

    Every legacy job is imported ``enabled=false`` + ``user_paused=true``.
    If the destination file exists, all destination jobs are kept and legacy
    jobs are appended only when their ``id`` is absent (dest wins on
    collision). If the destination is absent, the paused legacy set is
    written as ``{"jobs": [...]}``. The write is atomic.
    """
    p = src / "crons.json"
    if not p.is_file():
        return
    legacy = _normalize_legacy_cron_jobs(_load_json(p, None))
    if not legacy:
        return
    for j in legacy:
        j["enabled"] = False
        j["user_paused"] = True

    dest_p = dst / "crons.json"
    if dest_p.is_file():
        dest_data = _load_json(dest_p, None)
        if isinstance(dest_data, dict):
            container: dict | None = dict(dest_data)
            dest_jobs = [j for j in dest_data.get("jobs", []) if isinstance(j, dict)]
        elif isinstance(dest_data, list):
            container = None
            dest_jobs = [j for j in dest_data if isinstance(j, dict)]
        else:
            container = None
            dest_jobs = []
        dest_ids = {j.get("id") for j in dest_jobs if j.get("id") is not None}
        merged = list(dest_jobs) + [j for j in legacy if j.get("id") not in dest_ids]
        if container is not None:
            container["jobs"] = merged
            out = container
        else:
            out = {"jobs": merged}
    else:
        out = {"jobs": legacy}
    _atomic_write_text(dest_p, json.dumps(out, indent=2))


# ── Metadata (status endpoint) ──────────────────────────────────────────────
def _estimate_size_bytes(root: Path) -> int:
    """Best-effort byte size of the *migrated* footprint (memoized).

    Counts only what the manifest would actually copy: skips ``models/`` and
    ``run/``, ``*.log`` / ``*.log.*`` files, ``security_events.jsonl``,
    ``config.json``, and app dirs without an ``installed.json``. The result
    is cached per root so repeated /status polls don't re-walk the tree.
    """
    key = str(root)
    cached = _size_estimate_cache.get(key)
    if cached is not None:
        return cached
    total = 0
    apps_dir = root / "apps"
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dp = Path(dirpath)
            if dp == root:
                dirnames[:] = [d for d in dirnames if d not in _SIZE_SKIP_TOP]
            elif dp == apps_dir:
                dirnames[:] = [
                    d for d in dirnames if (apps_dir / d / "installed.json").is_file()
                ]
            for fn in filenames:
                if fn in _SIZE_SKIP_FILES or fn.endswith(".log") or ".log." in fn:
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    if not os.path.islink(fp):
                        total += os.path.getsize(fp)
                except OSError:
                    continue
    except OSError:
        pass
    _size_estimate_cache[key] = total
    return total


def _count_sessions(root: Path) -> int:
    """Count session transcripts in ``sessions/`` (layout-agnostic)."""
    sdir = root / "sessions"
    if not sdir.is_dir():
        return 0
    try:
        jsonl = list(sdir.glob("*.jsonl"))
        if jsonl:
            return len(jsonl)
        return sum(1 for _ in sdir.iterdir())
    except OSError:
        return 0


# ── Markers / intent ────────────────────────────────────────────────────────
def _ensure_marker_dir() -> Path:
    d = _marker_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_done_marker(reason: str = "") -> None:
    _ensure_marker_dir()
    payload = {
        "done_at": _now_iso(),
        "reason": reason,
        "source": str(_legacy_source()),
        "backup": str(_legacy_backup()),
    }
    _done_marker_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_copied_marker() -> None:
    """Record that the copy phase completed with ZERO step failures.

    Present + no done marker means only the final rename is outstanding; a
    retry must skip the copy phase entirely (re-copying could clobber
    post-import changes in the destination). Also carries the
    ``rename_attempts`` counter that bounds the cross-boot rename retry.
    """
    _ensure_marker_dir()
    _atomic_write_text(
        _copied_marker_path(),
        json.dumps({"copied_at": _now_iso(), "rename_attempts": 0}, indent=2),
    )


def _bump_rename_attempts() -> int:
    """Increment and persist the rename attempt counter in the copied marker."""
    p = _copied_marker_path()
    data = _load_json(p, {})
    if not isinstance(data, dict):
        data = {}
    attempts = int(data.get("rename_attempts", 0) or 0) + 1
    data.setdefault("copied_at", _now_iso())
    data["rename_attempts"] = attempts
    _ensure_marker_dir()
    _atomic_write_text(p, json.dumps(data, indent=2))
    return attempts


def write_import_intent() -> Path:
    """Record intent (Phase 1) so the next boot performs the import."""
    _ensure_marker_dir()
    # NOTE: no rename_attempts reset here. A re-request while a copied
    # marker is pending is effectively unreachable through the product
    # path: after a successful copy the destination holds the imported
    # data, so the /start endpoint's availability check (freshness) refuses
    # to re-request. In the corner where a tiny import leaves the dest
    # still looking fresh, a re-request after ``rename_gave_up`` simply
    # hits the exhausted rename cap and gives up again — acceptable, since
    # the destination is whole either way.
    p = _intent_marker_path()
    _atomic_write_text(p, json.dumps({"requested_at": _now_iso()}, indent=2))
    return p


def clear_import_intent() -> None:
    """Remove the Phase-1 intent marker (public seam for the /start handler).

    Used when the scheduled restart task completes WITHOUT exec'ing: the
    consent it recorded must not linger for an unrelated later restart to
    consume. A genuine re-request simply rewrites the marker.
    """
    _clear_intent()


def _intent_is_stale() -> bool:
    """True iff the intent marker is older than ``_INTENT_MAX_AGE_SECONDS``.

    Phase 1 restarts the gateway immediately after writing the intent, so a
    boot seeing an old intent is acting on stale consent — e.g. the restart
    never happened and the user kept using the app — and must not import.
    """
    p = _intent_marker_path()
    data = _load_json(p, {})
    ts = data.get("requested_at") if isinstance(data, dict) else None
    if isinstance(ts, str):
        try:
            req = datetime.fromisoformat(ts)
            if req.tzinfo is None:
                req = req.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - req).total_seconds()
            return age > _INTENT_MAX_AGE_SECONDS
        except ValueError:
            pass
    try:
        age = datetime.now(timezone.utc).timestamp() - p.stat().st_mtime
        return age > _INTENT_MAX_AGE_SECONDS
    except OSError:
        return True


def _clear_intent() -> None:
    _unlink(_intent_marker_path())


# ── Public API ────────────────────────────────────────────────────────────
def _dest_is_fresh(dest: Path) -> bool:
    """True iff the destination home looks like a fresh install.

    Not fresh (import offer withdrawn) when the dest ``memory.db`` exceeds a
    fresh-init size threshold, there are any ``sessions/*.jsonl``
    transcripts, or any of the meshclaw-only ``APPS_FULL`` app dirs already
    exists non-empty — any of these means the user already has real data in
    the new home that an old-wins/full-overwrite import could damage.
    """
    try:
        mdb = dest / "memory.db"
        if mdb.is_file() and mdb.stat().st_size > _FRESH_MEMORY_DB_MAX_BYTES:
            return False
        sdir = dest / "sessions"
        if sdir.is_dir() and any(sdir.glob("*.jsonl")):
            return False
        apps = dest / "apps"
        for a in APPS_FULL:
            d = apps / a
            if d.is_dir() and any(d.iterdir()):
                return False
    except OSError:
        return False
    return True


def detect_meshclaw_import_available() -> dict:
    """Return whether a legacy import can be offered, plus metadata.

    Available only if ``~/.meshclaw`` exists, is a *different* dir than the
    current KiroCrew home (so running as legacy MeshClaw is a no-op), neither
    dir is nested inside the other, ``~/.meshclaw.bak`` is absent, the done
    marker is absent, and the destination still looks like a fresh install
    (see :func:`_dest_is_fresh`).
    """
    src = _legacy_source()
    dest = config_dir()
    try:
        available = (
            src.is_dir()
            and not _same_path(src, dest)
            and not _nested_paths(src, dest)
            and not _legacy_backup().exists()
            and not _done_marker_path().exists()
            and _dest_is_fresh(dest)
        )
    except OSError:
        available = False

    size = _estimate_size_bytes(src) if available else 0
    sessions = _count_sessions(src) if available else 0
    return {
        "available": bool(available),
        "sourcePath": "~/.meshclaw",
        "sizeEstimateBytes": int(size),
        "sessionCount": int(sessions),
    }


def _run_guarded(steps: list, name: str, fn) -> None:
    """Run one manifest step, recording ok/fail; never raises."""
    try:
        fn()
        steps.append({"step": name, "ok": True})
    except Exception as exc:  # noqa: BLE001 — a single step must not abort the import
        logger.warning("meshclaw import step %r failed: %s", name, exc, exc_info=True)
        steps.append({"step": name, "ok": False, "error": str(exc)})


def _backup_dest_targets(dest: Path) -> None:
    """Back up the exact dest files the overwrite steps will replace.

    Copies each existing ``_DEST_BACKUP_TARGETS`` entry (preserving its
    relative path) into ``.migrations/meshclaw_import_dest_backup/``.
    WRITE-ONCE: an entry already present in the backup is never overwritten.
    Each backup file is written via a tmp sibling + fsync + ``os.replace``
    so a crash mid-backup can never leave a truncated file at the final
    backup path (which the restore would then trust). The backup dir is
    ALWAYS created (even when zero targets exist) and a completion marker
    is stamped inside it LAST: the dir is the durable on-disk record a
    later boot uses to detect a run that crashed mid-copy, and the marker
    distinguishes a complete, restorable backup from one the crash
    interrupted mid-backup (which must be discarded, never restored from).
    This runs as a fail-closed precondition — a failure here aborts the
    copy phase before anything is overwritten.
    """
    bdir = _dest_backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    for rel in _DEST_BACKUP_TARGETS:
        p = dest / rel
        if p.is_file():
            b = bdir / rel
            if b.exists():
                continue  # write-once: keep the pristine first backup
            b.parent.mkdir(parents=True, exist_ok=True)
            tmp = b.with_name(b.name + ".tmp-import")
            try:
                shutil.copy2(p, tmp)
                _fsync_path(tmp)
                os.replace(tmp, b)
            finally:
                _unlink(tmp)
    marker = bdir / _BACKUP_COMPLETE_MARKER
    mtmp = marker.with_name(marker.name + ".tmp-import")
    try:
        mtmp.write_text(json.dumps({"completed_at": _now_iso()}), encoding="utf-8")
        _fsync_path(mtmp)
        os.replace(mtmp, marker)
    finally:
        _unlink(mtmp)
    _fsync_dir(bdir)


def _restore_dest_targets(dest: Path) -> list[str]:
    """Roll the overwrite targets back to their pre-import state.

    For each ``_DEST_BACKUP_TARGETS`` entry: restore it from the write-once
    backup when a backup copy exists, otherwise remove it (no backup means
    the file did not exist pre-import, so anything there was written by the
    failed run). SQLite dbs are restored as a family SET in a safe order:
    the destination's ``-wal``/``-shm`` sidecars are strictly removed
    FIRST (a foreign sidecar beside a replaced db would corrupt it on
    open), then the db file is swapped in atomically, then the backed-up
    sidecars (if any) are restored. If a family's sidecar cannot be
    removed, the db swap is skipped for that family — fail-closed beats a
    db/WAL mismatch.

    Best-effort per target — a single failure must not stop the remaining
    targets from being rolled back — but NEVER silent: every target that
    could not be restored is returned so the caller can refuse to report a
    clean rollback (and keep the backup dir).
    """
    bdir = _dest_backup_dir()
    failures: list[str] = []

    def _restore_one(rel: str) -> bool:
        d = dest / rel
        b = bdir / rel
        try:
            if b.is_file():
                _copy_file_overwrite(b, d)
            else:
                _unlink_strict(d)
            return True
        except OSError:
            failures.append(rel)
            logger.warning(
                "meshclaw import rollback: could not restore %s", rel, exc_info=True
            )
            return False

    handled: set[str] = set()
    for db_rel in _SQLITE_FAMILY_DBS:
        wal_rel, shm_rel = db_rel + "-wal", db_rel + "-shm"
        handled.update({db_rel, wal_rel, shm_rel})
        # (1) strictly drop the dest sidecars before touching the db
        sidecar_failed = False
        for rel in (wal_rel, shm_rel):
            try:
                _unlink_strict(dest / rel)
            except OSError:
                sidecar_failed = True
                failures.append(rel)
                logger.warning(
                    "meshclaw import rollback: could not remove %s", rel,
                    exc_info=True,
                )
        if sidecar_failed:
            # Swapping the db in under a sidecar we could not remove would
            # hand SQLite a mismatched WAL — leave the family untouched.
            failures.append(db_rel)
            continue
        # (2) restore the db itself (tmp sibling + os.replace, atomic)
        if not _restore_one(db_rel):
            continue  # don't lay backed-up sidecars beside a failed db
        # (3) restore the backed-up sidecars, if the pre-import dest had any
        for rel in (wal_rel, shm_rel):
            if (bdir / rel).is_file():
                _restore_one(rel)
    for rel in _DEST_BACKUP_TARGETS:
        if rel in handled:
            continue
        _restore_one(rel)
    return failures


def _remove_backup_dir() -> None:
    """Best-effort removal of the dest-backup dir (rollback / abort paths)."""
    try:
        shutil.rmtree(_dest_backup_dir())
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning(
            "meshclaw import: could not remove dest backup dir", exc_info=True
        )


def _write_restore_incomplete_marker(failures: list[str]) -> None:
    """Note inside the (retained) backup dir that a restore attempt failed.

    Purely informational for the next attempt / manual recovery — detection
    of an unconsumed backup keys off the dir itself, not this marker.
    """
    try:
        (_dest_backup_dir() / _RESTORE_INCOMPLETE_MARKER).write_text(
            json.dumps(
                {"at": _now_iso(), "failed_targets": failures}, indent=2
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.warning(
            "meshclaw import: could not write restore-incomplete marker",
            exc_info=True,
        )


def _rollback_copy_phase(dest: Path, journal: _RunJournal) -> bool:
    """Undo a failed copy phase completely: the destination goes back to its
    exact pre-import state and NO retry state is retained.

    Order matters: (1) delete every additive file this run created (from the
    journal), pruning now-empty directories it created; (2) restore the
    overwrite targets from the write-once backup (recreating any target the
    journal deletion removed); (3) remove the backup dir ONLY if every
    target was restored — on any restore failure the backup dir is KEPT
    (with a restore-incomplete note) so the data needed to finish the
    rollback is never destroyed; (4) clear the intent and all run markers.
    Each item is best-effort — one failure must not stop the rest of the
    rollback. Returns True iff every overwrite target was restored (the
    caller must NOT report a clean rollback otherwise).
    """
    for f in reversed(journal.files):
        try:
            if f.is_symlink() or f.exists():
                f.unlink()
        except OSError:
            logger.warning(
                "meshclaw import rollback: could not delete %s", f, exc_info=True
            )
    for d in sorted(journal.dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()  # only succeeds on (our own, now-empty) dirs
        except OSError:
            pass
    failures = _restore_dest_targets(dest)
    if failures:
        _write_restore_incomplete_marker(failures)
        logger.error(
            "meshclaw import rollback INCOMPLETE: could not restore %d "
            "target(s) (%s). The pre-import backup is KEPT at %s — restore "
            "these files from it manually or let the next boot retry.",
            len(failures), ", ".join(failures), _dest_backup_dir(),
        )
    else:
        _remove_backup_dir()
    _unlink(_copied_marker_path())
    _clear_intent()
    return not failures


def _recover_crashed_run(dest: Path) -> dict:
    """Recover from a prior run that was killed mid-copy (X1).

    Detection (by the callers): the on-disk dest backup dir exists but the
    *copied* marker does not — the run died after starting its copy phase
    but before it could either commit (copied marker) or roll back (which
    consumes the backup dir). The in-memory journal died with the process,
    so the backup dir is the only durable record: restore the overwrite
    targets from it, consume it, clear the intent, and do NOT import —
    re-running the copy needs fresh consent via the normal offer.

    Additive residue (sessions/apps files the crashed run copied) cannot be
    enumerated without the journal; that is acceptable — the backup targets
    are restored, and if the residue keeps the destination looking
    non-fresh the import offer simply stays withdrawn.

    A backup missing its completion marker means the crash hit the backup
    phase itself, BEFORE any overwrite: the destination is untouched and
    the partial backup must not be trusted for a restore (it may be missing
    targets that did exist pre-import, and restoring would delete them) —
    it is simply discarded.

    Fail-closed like the rollback path: the backup dir is deleted only when
    every target was restored; otherwise it is kept (with a
    restore-incomplete note) and the distinct ``recovery_incomplete``
    status is returned — never a clean report.
    """
    bdir = _dest_backup_dir()
    if not (bdir / _BACKUP_COMPLETE_MARKER).is_file():
        logger.warning(
            "meshclaw import: discarding a partial dest backup left by a "
            "run that crashed during its backup phase (the destination was "
            "never modified, so there is nothing to restore)"
        )
        _remove_backup_dir()
        _clear_intent()
        return {"status": "recovered_crashed_run", "renamed": False, "steps": []}
    logger.warning(
        "meshclaw import: found a dest backup from a prior run that crashed "
        "mid-copy — restoring the overwrite targets from it. Additive files "
        "the crashed run copied (e.g. sessions/apps) cannot be enumerated "
        "and are left in place; if they keep the destination looking "
        "non-fresh, the import offer stays withdrawn."
    )
    failures = _restore_dest_targets(dest)
    if failures:
        _write_restore_incomplete_marker(failures)
        logger.error(
            "meshclaw import crash recovery INCOMPLETE: could not restore "
            "%d target(s) (%s). The pre-import backup is KEPT at %s — "
            "restore these files from it manually or let the next boot "
            "retry.",
            len(failures), ", ".join(failures), bdir,
        )
        _clear_intent()
        return {
            "status": "recovery_incomplete",
            "renamed": False,
            "steps": [],
            "restore_failures": failures,
        }
    _remove_backup_dir()
    _clear_intent()
    return {"status": "recovered_crashed_run", "renamed": False, "steps": []}


def _snapshot_sqlite_sidecars(src_db: Path, dst_db: Path) -> None:
    """Copy ``-wal``/``-shm`` sidecars from source when present (consistent
    snapshot); otherwise drop any stale destination sidecar. A failed drop
    raises — a stale WAL left beside a replaced db would corrupt it."""
    for suf in ("-wal", "-shm"):
        s = src_db.with_name(src_db.name + suf)
        d = dst_db.with_name(dst_db.name + suf)
        if s.is_file():
            _copy_file_overwrite(s, d)
        else:
            _unlink_strict(d)


def _copy_manifest(src: Path, dest: Path, steps: list) -> None:
    """Perform the full copy manifest (everything except the final rename).

    The fail-closed dest backup (:func:`_backup_dest_targets`) MUST have
    already succeeded before this is called — every guarded step below may
    overwrite a backup target and relies on
    :func:`_rollback_copy_phase` for recovery.
    """
    dest.mkdir(parents=True, exist_ok=True)

    # 1. sessions/ (additive)
    _run_guarded(steps, "sessions", lambda: _copytree_additive(src / "sessions", dest / "sessions"))
    # 2. session_map.json + folders.json (union merge, new wins)
    _run_guarded(steps, "session_map", lambda: _merge_session_map(src, dest))
    _run_guarded(steps, "folders", lambda: _merge_folders(src, dest))

    # 3. memory.db (old wins) + WAL snapshot; drop index so it re-embeds
    def _memory_db() -> None:
        _copy_file_overwrite(src / "memory.db", dest / "memory.db")
        if (src / "memory.db").is_file():
            _snapshot_sqlite_sidecars(src / "memory.db", dest / "memory.db")
            _unlink_strict(dest / "memory_index.db")
    _run_guarded(steps, "memory.db", _memory_db)

    # 4. workspace/: docs additive; memory/knowledge old-wins
    def _workspace() -> None:
        _copytree_additive(src / "workspace", dest / "workspace", exclude=_WORKSPACE_EXCLUDE)
        _copy_file_overwrite(
            src / "workspace" / "memory" / "preferences.md",
            dest / "workspace" / "memory" / "preferences.md",
        )
        _copy_file_overwrite(
            src / "workspace" / "memory" / "projects.md",
            dest / "workspace" / "memory" / "projects.md",
        )
        _copytree_additive(
            src / "workspace" / "memory" / "history",
            dest / "workspace" / "memory" / "history",
        )
        kdb_src = src / "workspace" / "knowledge" / "knowledge.db"
        if kdb_src.is_file():
            kdb_dst = dest / "workspace" / "knowledge" / "knowledge.db"
            _copy_file_overwrite(kdb_src, kdb_dst)
            _snapshot_sqlite_sidecars(kdb_src, kdb_dst)
        _copytree_additive(src / "workspace" / ".kiro", dest / "workspace" / ".kiro")
    _run_guarded(steps, "workspace", _workspace)

    # 5. apps: 10 full; 7 shared -> data/ only
    def _apps() -> None:
        for a in APPS_FULL:
            _copytree_overwrite(src / "apps" / a, dest / "apps" / a)
        for a in APPS_SHARED:
            _copytree_additive(src / "apps" / a / "data", dest / "apps" / a / "data")
    _run_guarded(steps, "apps", _apps)

    # 6. skills: custom only (skip meshclaw-* and already-present)
    def _skills() -> None:
        sk = src / "skills"
        if not sk.is_dir():
            return
        for d in sorted(sk.iterdir()):
            if not d.is_dir():
                continue
            if d.name.startswith("meshclaw-"):
                continue
            if (dest / "skills" / d.name).exists():
                continue
            _copytree_overwrite(d, dest / "skills" / d.name)
    _run_guarded(steps, "skills", _skills)

    # 7. artifacts/ + uploads/ (additive)
    _run_guarded(steps, "artifacts", lambda: _copytree_additive(src / "artifacts", dest / "artifacts"))
    _run_guarded(steps, "uploads", lambda: _copytree_additive(src / "uploads", dest / "uploads"))

    # 8. crons.json (paused) + crons/ + cron-history/
    _run_guarded(steps, "crons.json", lambda: _import_crons(src, dest))
    _run_guarded(steps, "crons", lambda: _copytree_additive(src / "crons", dest / "crons"))
    _run_guarded(steps, "cron-history", lambda: _copytree_additive(src / "cron-history", dest / "cron-history"))

    # 9. mcp.json (absent-only, else parked) + mcp-servers/
    def _mcp() -> None:
        mj_src = src / "mcp.json"
        mj_dst = dest / "mcp.json"
        if mj_src.is_file():
            if mj_dst.is_file():
                _copy_file_overwrite(mj_src, mj_dst.with_name("mcp.json.from-meshclaw"))
            else:
                _record_new_file(mj_dst)
                _copy_entry(mj_src, mj_dst)
        _copytree_additive(src / "mcp-servers", dest / "mcp-servers")
    _run_guarded(steps, "mcp", _mcp)

    # .env (absent-only; in-product self-migration of the app's own data dir)
    _run_guarded(steps, ".env", lambda: _copy_file_if_absent(src / ".env", dest / ".env"))

    # 10. misc user state
    def _misc() -> None:
        for fn in _MISC_FILES:
            _copy_file_if_absent(src / fn, dest / fn)
        for dn in _MISC_DIRS:
            _copytree_additive(src / dn, dest / dn)
    _run_guarded(steps, "misc", _misc)

    # config.json is NEVER copied — the fresh install's config is authoritative.


def run_meshclaw_import() -> dict:
    """Copy-then-rename import: all-or-nothing copy phase, bounded rename retry.

    Guards / outcomes:

    * no-op statuses: ``already_done`` / ``already_done_backup`` /
      ``no_source`` / ``source_is_dest`` / ``nested_paths``;
    * ``recovered_crashed_run`` / ``recovery_incomplete``: an on-disk dest
      backup with no copied marker means a prior run was killed mid-copy
      before it could commit or roll back. The overwrite targets are
      restored from the durable backup (a backup missing its completion
      marker is discarded untrusted — the crash hit the backup phase, so
      the destination was never modified), the backup is consumed, the
      intent is cleared, and NOTHING is imported — a retry needs fresh
      consent. If any target cannot be restored the backup is KEPT and the
      distinct incomplete status is returned;
    * ``dest_not_fresh``: unless a prior copy already fully succeeded
      (copied marker), a destination that no longer looks fresh means the
      user accrued real data since consenting — the intent is cleared and
      nothing is copied. No bypass;
    * ``backup_failed``: the fail-closed write-once dest backup is a hard
      precondition. If it fails, nothing has been touched — the partial
      backup dir and the intent are cleared and the run bails terminally;
    * ``rolled_back``: the copy phase is all-or-nothing. Every additive
      path the run creates is journaled; on ANY failure — a failed manifest
      step or an unexpected exception (e.g. a marker-write OSError) — the
      overwrite targets are restored from the backup, the journaled
      additive files/dirs are deleted, and the backup dir plus all markers
      (including the intent) are cleared. The destination is back to its
      pre-import state and the consent-gated import offer simply becomes
      available again. ``rollback_incomplete`` is the fail-closed variant:
      if any overwrite target could not be restored, the backup dir is KEPT
      (with a restore-incomplete note) and the run never reports a clean
      rollback. ``KeyboardInterrupt`` / ``SystemExit`` also trigger the
      full rollback and are re-raised once it completes;
    * ``ok`` / ``copied_no_rename`` / ``rename_gave_up``: after a fully
      successful copy the *copied* marker commits the import; only the
      ``os.rename`` of ``~/.meshclaw`` -> ``~/.meshclaw.bak`` remains
      (``os.rename`` only — no move fallback, so a partial ``.bak`` can
      never be created) and is retried across boots without ever
      re-copying, bounded by an attempt counter in the copied marker. After
      the cap the run gives up: the completed import stays in place (the
      destination is whole), the intent is cleared, and a prominent log
      tells the user to move ``~/.meshclaw`` aside manually;
    * the done marker is written only after a successful rename; the copied
      marker is cleaned up at that point.
    """
    global _journal
    dest = config_dir()
    src = _legacy_source()
    backup = _legacy_backup()

    if _done_marker_path().exists():
        return {"status": "already_done", "renamed": False, "steps": []}
    # Crash recovery (X1): an on-disk dest backup with no copied marker
    # means a prior consented run was killed mid-copy (SIGKILL/power loss)
    # before it could commit or roll back. Recover from the durable backup
    # and bail WITHOUT importing — re-running the copy needs fresh consent.
    # This also guarantees a fresh consented run never sees a leftover
    # backup: the write-once backup below only ever trusts THIS run's files.
    if _dest_backup_dir().is_dir() and not _copied_marker_path().exists():
        return _recover_crashed_run(dest)
    if backup.exists():
        _write_done_marker(reason="backup_present")
        return {"status": "already_done_backup", "renamed": False, "steps": []}
    if not src.is_dir():
        return {"status": "no_source", "renamed": False, "steps": []}
    if _same_path(src, dest):
        # Running as legacy MeshClaw itself (source == destination) — nothing
        # to import and renaming would destroy the live home.
        return {"status": "source_is_dest", "renamed": False, "steps": []}
    if _nested_paths(src, dest):
        # One dir inside the other: the copy would recurse into itself and
        # the rename would destroy live data.
        return {"status": "nested_paths", "renamed": False, "steps": []}

    steps: list = []
    if not _copied_marker_path().exists():
        # Execution-time freshness re-check: consent was given against a
        # fresh destination. If it is no longer fresh, the user accrued real
        # data — do NOT import. No bypass.
        if not _dest_is_fresh(dest):
            _clear_intent()
            return {"status": "dest_not_fresh", "renamed": False, "steps": []}

        # Fail-closed, write-once backup BEFORE any overwrite step.
        try:
            _backup_dest_targets(dest)
        except Exception as exc:  # noqa: BLE001 — must abort the copy phase, not boot
            logger.warning("meshclaw import: dest backup failed: %s", exc, exc_info=True)
            steps.append({"step": "dest_backup", "ok": False, "error": str(exc)})
            # Nothing was overwritten: discard the partial backup and the
            # intent — a future attempt is just the normal consent-gated
            # offer again.
            _remove_backup_dir()
            _clear_intent()
            return {"status": "backup_failed", "renamed": False, "steps": steps}
        steps.append({"step": "dest_backup", "ok": True})

        # All-or-nothing copy: journal every additive path created, and on
        # ANY failure (step failure or unexpected exception) roll back
        # completely and clear all state.
        journal = _RunJournal()
        _journal = journal
        failed = False
        reraise: BaseException | None = None
        try:
            _copy_manifest(src, dest, steps)
            failed = any(not s.get("ok") for s in steps)
            if not failed:
                _write_copied_marker()
        except BaseException as exc:  # noqa: BLE001 — uncaught-exception seam (S7/X2)
            # BaseException, not Exception: KeyboardInterrupt / SystemExit
            # mid-copy must also trigger the full rollback (a half-imported
            # dest is never acceptable) — but they are RE-RAISED after the
            # rollback completes rather than swallowed.
            logger.error(
                "meshclaw import: unexpected copy-phase failure: %s", exc, exc_info=True
            )
            steps.append({"step": "unexpected", "ok": False, "error": str(exc)})
            failed = True
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                reraise = exc
        finally:
            _journal = None
        if failed:
            clean = _rollback_copy_phase(dest, journal)
            if reraise is not None:
                raise reraise
            return {
                "status": "rolled_back" if clean else "rollback_incomplete",
                "renamed": False,
                "steps": steps,
            }
    # else: prior copy phase already succeeded — only the rename remains.

    # Final (reversible) step: rename legacy dir. os.rename only — never a
    # copy-based move, which could leave a partial ``.bak`` on failure. The
    # cross-boot retry is bounded so an os.rename that can never succeed
    # (e.g. EXDEV) does not loop forever.
    try:
        rename_attempts = _bump_rename_attempts()
    except OSError:
        # Can't persist the counter; still attempt the rename this boot —
        # it may well succeed and finish the import.
        logger.warning(
            "meshclaw import: could not persist rename attempt count", exc_info=True
        )
        rename_attempts = 1
    if rename_attempts > _MAX_RENAME_ATTEMPTS:
        logger.error(
            "meshclaw import: giving up on renaming %s -> %s after %d failed "
            "attempts. The import itself is COMPLETE — your data is in %s. "
            "Move or delete %s manually to silence this.",
            src, backup, rename_attempts - 1, dest, src,
        )
        _clear_intent()
        return {
            "status": "rename_gave_up",
            "renamed": False,
            "steps": steps,
            "rename_attempts": rename_attempts,
        }
    renamed = False
    try:
        os.rename(src, backup)
        renamed = True
    except OSError:
        logger.warning("meshclaw import: could not rename %s -> %s", src, backup, exc_info=True)

    if renamed:
        try:
            _write_done_marker(reason="import_complete")
        except OSError:
            # Self-healing: with ``.bak`` present, the next run takes the
            # already_done_backup path and re-writes the done marker.
            logger.warning("meshclaw import: could not write done marker", exc_info=True)
        _unlink(_copied_marker_path())

    return {
        "status": "ok" if renamed else "copied_no_rename",
        "renamed": renamed,
        "steps": steps,
    }


# The single retryable status: a successful copy whose final rename failed
# (under its bounded attempt cap). Every other status is terminal for the
# intent — a failed copy is fully rolled back and must be re-requested.
_RETRYABLE_STATUSES = frozenset({"copied_no_rename"})


def run_pending_meshclaw_import() -> dict:
    """Phase-2 early-boot runner: import iff an intent marker is pending.

    MUST be called from ``cli_server._gateway()`` before ``run_gateway()``
    opens ``memory.db`` / ``knowledge.db``. Cheap no-op on every boot where
    no import was requested or the import already completed.
    """
    if _done_marker_path().exists():
        if _intent_marker_path().exists():
            _clear_intent()
        return {"ran": False, "reason": "already_done"}
    # Crash recovery (X1) runs BEFORE the intent gates: after a SIGKILL /
    # power loss mid-copy the next boot may be arbitrarily late (stale
    # intent) or the intent may be gone entirely — the on-disk backup dir
    # alone is the durable record and must be honored regardless.
    if _dest_backup_dir().is_dir() and not _copied_marker_path().exists():
        return {"ran": True, **_recover_crashed_run(config_dir())}
    if not _intent_marker_path().exists():
        return {"ran": False, "reason": "no_intent"}

    # Stale consent: Phase 1 restarts the gateway immediately after writing
    # the intent, so an old intent means the restart never happened (and the
    # user kept using the app). Drop it. The TTL guards *consent for the
    # destructive copy* only — once the copied marker shows the copy is
    # committed, the only remaining work is the consent-insensitive rename,
    # which must still complete on boots arbitrarily far in the future.
    if _intent_is_stale() and not _copied_marker_path().exists():
        _clear_intent()
        return {"ran": False, "reason": "stale_intent"}

    result = run_meshclaw_import()
    # Retain the intent only for the single retryable outcome (successful
    # copy awaiting its rename). Every other status is terminal: success /
    # already-done write the done marker, a failed copy is fully rolled back
    # (offer becomes available again), and the remaining no-op outcomes
    # would repeat identically on every boot.
    if result.get("status") not in _RETRYABLE_STATUSES:
        _clear_intent()
    return {"ran": True, **result}
