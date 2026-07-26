"""Hardened git working-tree read handlers: /api/file-diff and /api/git-changes.

These endpoints render VISUAL-ONLY information about a git repository the
dashboard user points their chat at. The repository contents are treated as
HOSTILE: a checkout can carry a ``.git/config`` that binds arbitrary commands
to content reads (clean/smudge/process filters, ``textconv``, ``fsmonitor``,
external diff drivers), an ``info/attributes`` that re-binds them after any
pre-check (TOCTOU), hooks, and object-store redirections (``alternates``).
Because these endpoints are POLLED and run unsandboxed as the gateway user,
a single misstep is arbitrary code execution with access to protected
credentials.

The security model, in one paragraph: no content-reading git invocation ever
consults repository-authored EXECUTABLE configuration. Content reads run
against a PRIVATE synthetic ``GIT_DIR`` (:func:`_isolate_repo_git_metadata`)
containing only minimal, non-executable config and a detached HEAD, with the
real object database attached read-only as an alternate and the real index as
an explicit input — so the repo's own config, hooks, and ``info/attributes``
are absent by construction, not by (raceable) inspection. Attribute lookups
are redirected to the empty tree (``GIT_ATTR_SOURCE``), the process env is
allowlisted (:func:`_hardened_git_env`), the git executable is pinned at
gateway startup (:data:`_GIT_EXE`), every subprocess read is byte-capped
(:func:`_run_git_capped_bytes`) and deadline-bounded, protected home paths are
excluded via pathspecs before git ever reads them, and worktree content read
from Python refuses hard links (``safe_read_file_bytes_nolink``) so a link
planted at ``~/.ssh/id_rsa`` cannot be served as an innocent project diff.
Everything filesystem-touching runs inside ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from kiro_crew.config.loader import config_dir
from kiro_crew.platform_compat import IS_POSIX, chmod_safe
from kiro_crew.security import _CREW_SECRET_LEAVES, _SENSITIVE_HOME_DIRS, is_sensitive_path

logger = logging.getLogger(__name__)


def _sel():
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


# ── Git executable pinning ────────────────────────────────────────────────
# Resolved ONCE at import time (gateway startup), while PATH is still the
# operator's own environment. These endpoints are polled and spawn git
# unsandboxed as the gateway user; resolving per-call would let anything that
# can prepend a user-writable PATH entry AFTER startup (an agent bash session
# writing ~/bin/git, a repo shipping ./git plus a relative PATH element)
# redirect the spawn to its own binary. realpath so the repo-containment check
# in _assert_git_outside compares canonical locations.
_GIT_EXE: str | None = shutil.which("git")
if _GIT_EXE:
    _GIT_EXE = os.path.realpath(_GIT_EXE)


def _assert_git_not_writable(exe: str) -> None:
    """Refuse a git binary the gateway user can REPLACE.

    Pinning a path is not pinning code: if the resolved executable (or the
    directory holding it) is writable by this uid, a same-uid agent can swap
    the file in place and the next poll executes attacker code unsandboxed. A
    root-owned system git passes; a user-owned shim directory does not.

    POSIX-only. ``os.access(W_OK)`` on Windows reflects only the read-only
    attribute and reports directories as writable unconditionally, so applying
    it there refuses EVERY repository (a false positive on a stock
    ``Program Files`` git) while proving nothing about the real ACL. A genuine
    Windows check needs the security-descriptor APIs; until then this control
    is documented as POSIX-scoped rather than silently mis-enforced. The other
    controls (containment, synthetic GIT_DIR, allowlisted env) are unaffected.
    """
    if not IS_POSIX:
        return
    for target in (exe, os.path.dirname(exe)):
        try:
            writable = os.access(target, os.W_OK)
        except OSError as exc:
            raise PermissionError(f"git executable could not be validated: {exe}") from exc
        if writable:
            raise PermissionError(
                "the resolved git executable is writable by this user, so it cannot "
                f"be trusted for unsandboxed execution: {target}"
            )


def _git_exe() -> str:
    """The pinned absolute git executable (lazy re-resolve if absent at import).

    Re-validated non-writable on every call: the check is a cheap ``access(2)``,
    and re-running it means a permission change made AFTER startup is caught on
    the next poll instead of trusting the import-time verdict forever.
    """
    global _GIT_EXE
    if _GIT_EXE is None:
        found = shutil.which("git")
        if not found:
            raise FileNotFoundError("git executable not found on PATH")
        _GIT_EXE = os.path.realpath(found)
    _assert_git_not_writable(_GIT_EXE)
    return _GIT_EXE


def _hardened_git() -> list[str]:
    """Base argv for every git spawn on this module's endpoints.

    The ``-c`` overrides neutralize USER-level execution vectors (textconv,
    the user attributes file, fsmonitor). Repository-level config is
    neutralized structurally by :func:`_isolate_repo_git_metadata`; these
    flags remain as defense in depth for the metadata probes that run before
    isolation is built.
    """
    return [
        _git_exe(),
        "-c",
        "diff.textconv=",
        "-c",
        f"core.attributesFile={os.devnull}",
        "-c",
        "core.fsmonitor=",
    ]


def _assert_git_outside(root: str) -> None:
    """Refuse to scan a repository that CONTAINS the pinned git executable.

    If the gateway was started with a PATH whose git resolves INSIDE the
    directory now being scanned, the repository effectively supplied its own
    interpreter — every "hardened" invocation would be attacker code. This is
    the residual case import-time pinning cannot cover.
    """
    exe = _git_exe()
    root_real = os.path.realpath(root)
    try:
        if os.path.commonpath([exe, root_real]) == root_real:
            raise PermissionError("git executable resolves inside the scanned repository")
    except ValueError:
        # Different drives / mixed absolute-ness (Windows) — cannot be inside.
        pass


# Git needs only process/locale/temp variables for these local read-only
# calls. Never inherit GIT_CONFIG_*, GIT_DIR, object-store overrides, or
# unrelated gateway credentials into a repository-adjacent process.
_GIT_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    }
)


def _hardened_git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in _GIT_SAFE_ENV_KEYS}
    env.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _run_git_capped_bytes(
    args: list[str],
    cwd: str,
    env: dict,
    timeout: float,
    max_bytes: int,
) -> tuple[int, bytes, bool]:
    """Run a git command reading at most ``max_bytes`` of stdout (raw bytes).

    ``subprocess.run`` buffers the COMPLETE output before any caller-side
    limit can apply — on a huge file or dirty tree that is an unbounded
    allocation on a polled endpoint. Returns ``(returncode, data, truncated)``;
    when truncated the process is killed and returncode is reported as 0
    (partial data is valid). A reader thread + join keeps the timeout portable
    (``select()`` cannot watch pipes on Windows).
    """
    proc = subprocess.Popen(
        args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    chunks: list[bytes] = []
    seen = 0
    truncated = False

    def _reader() -> None:
        nonlocal seen, truncated
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                return
            take = chunk[: max(0, max_bytes - seen)]
            if take:
                chunks.append(take)
            seen += len(chunk)
            if seen > max_bytes:
                truncated = True
                proc.kill()
                return

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        proc.kill()
        reader.join(5)
        raise subprocess.TimeoutExpired(args, timeout)
    rc = proc.wait(timeout=5)
    return (0 if truncated else rc), b"".join(chunks), truncated


def _run_git_capped(
    args: list[str],
    cwd: str,
    env: dict,
    timeout: float,
    max_bytes: int,
) -> tuple[int, str, bool]:
    """Text-mode wrapper over ``_run_git_capped_bytes`` (tolerant decode)."""
    rc, data, truncated = _run_git_capped_bytes(
        args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes
    )
    return rc, data.decode("utf-8", errors="replace"), truncated


# GIT_ATTR_SOURCE redirects ALL attribute lookups to a tree object. Pointed at
# the empty tree, git sees zero attributes: no path binds to a clean/process
# filter or diff driver, so nothing a hostile .gitattributes names can affect
# our diff/show/numstat reads. The -c core.attributesFile=/dev/null hardening
# only replaces the USER-level attributes file — the repo's own worktree
# .gitattributes is still honored without this. 2.41 (not 2.40, where the
# --attr-source plumbing first appeared): the ENVIRONMENT variable is only
# honored reliably from 2.41, and an ignored variable fails silently open.
_GIT_ATTR_SOURCE_MIN_VERSION = (2, 41)
_git_version_cache: tuple[int, int] | None = None


def _git_supports_attr_source(timeout: float = 5.0) -> bool:
    """Cached GIT_ATTR_SOURCE support probe, bounded by the caller budget."""
    global _git_version_cache
    if _git_version_cache is None:
        try:
            r = subprocess.run(
                [_git_exe(), "--version"],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_hardened_git_env(),
            )
            m = re.match(r"git version (\d+)\.(\d+)", r.stdout.strip())
            if not m:
                return False
            _git_version_cache = (int(m.group(1)), int(m.group(2)))
        except (subprocess.SubprocessError, OSError):
            return False
    return _git_version_cache >= _GIT_ATTR_SOURCE_MIN_VERSION


def _empty_tree_hash(cwd: str, env: dict, timeout: float = 5.0) -> str:
    """The repo's empty-tree object id, or ``""`` on failure.

    Computed per-repo (``hash-object -t tree`` on empty stdin — portable, no
    ``/dev/null`` path) rather than hard-coded: the well-known SHA-1 constant
    is wrong for SHA-256 repos. Every failure mode degrades to ``""`` —
    callers treat that as "no empty tree available" rather than an error,
    because this runs while BUILDING the environment, outside the handlers'
    try blocks.
    """
    try:
        h = subprocess.run(
            [*_hardened_git(), "hash-object", "-t", "tree", "--stdin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            input="",
        )
        if h.returncode == 0:
            return h.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


def _repo_git_env(cwd: str, timeout: float = 5.0) -> dict:
    """Hardened env for the metadata probes run in the repo at ``cwd``.

    Adds ``GIT_ATTR_SOURCE=<empty tree>`` so the repo's own .gitattributes is
    never consulted. On git older than 2.41 the variable would be silently
    ignored, so it is simply not set — the -c/env hardening still applies, and
    :func:`_repo_attrs_unsafe` then decides whether reading is safe at all.
    ``timeout`` lets a caller with an aggregate budget bound this probe too.
    """
    env = _hardened_git_env()
    if _git_supports_attr_source(timeout):
        tree = _empty_tree_hash(cwd, env, timeout)
        if tree:
            env["GIT_ATTR_SOURCE"] = tree
    return env


# GIT_ATTR_SOURCE does NOT cover every attribute source: git always consults
# $GIT_DIR/info/attributes, and that file takes PRECEDENCE over the
# attr-source tree. A repo-local info/attributes ("*.txt filter=x") plus a
# matching filter.x.clean in the repo's own config therefore still binds an
# arbitrary command to content reads — on every git version. Neither file
# travels through clone/fetch/push, so this requires a .git directory the user
# did not author; but these endpoints are POLLED and run unsandboxed as the
# gateway user, so the reads fail CLOSED rather than trusting the repo.
# (The isolation step removes both files from later reads structurally; the
# pre-check exists so the REFUSAL reason is accurate and cheap.)
_ATTRS_UNSAFE_BOUND_FILTER = (
    "this file has a content filter or textconv bound to it by the "
    "repository, which git older than 2.41 cannot be prevented from running"
)
_ATTRS_UNSAFE_INFO_ATTRS = (
    "this repository has a local info/attributes entry, which overrides "
    "attribute isolation and can bind a content filter to diff reads"
)
_ATTRS_UNSAFE_SENSITIVE_GITDIR = (
    "this repository's git directory resolves into a protected location, so "
    "its committed content is not served"
)


def _git_dirs(cwd: str, env: dict, timeout: float = 5.0) -> list[str]:
    """Absolute git-dir and common-dir, failing closed if both probes fail."""
    args = [
        *_hardened_git(),
        "rev-parse",
        "--path-format=absolute",
        "--git-dir",
        "--git-common-dir",
    ]
    r = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if r.returncode != 0:
        # --path-format=absolute is newer than rev-parse's git-dir options.
        # Retry compatibly and resolve relative results against the cwd.
        args = [*_hardened_git(), "rev-parse", "--git-dir", "--git-common-dir"]
        r = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
        )
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, args)
    paths = []
    for line in r.stdout.splitlines():
        # rstrip("\n") not strip(): a git-dir path may legally end in a
        # space, and trimming it would retarget the probe.
        value = line.rstrip("\n")
        if value:
            absolute = value if os.path.isabs(value) else os.path.join(cwd, value)
            paths.append(os.path.realpath(absolute))
    if not paths:
        raise subprocess.CalledProcessError(1, args)
    return paths


def _path_has_bound_filter(cwd: str, path: str, env: dict, timeout: float = 5.0) -> bool:
    """Whether the repo binds a ``filter`` or ``diff`` attribute to ``path``.

    The per-path escape hatch for git < 2.41, where GIT_ATTR_SOURCE is
    unavailable. ``check-attr`` RESOLVES attributes without running any
    driver, so it is safe to ask, and it answers for exactly one path instead
    of requiring a whole-tree audit. ``-z`` output is NUL-separated
    (path, attr, value) triples — parsed positionally so a path containing
    ": " cannot confuse it. Fails CLOSED (True) if the probe cannot be
    understood.
    """
    # Byte-capped: the attribute VALUES are repository-controlled (an attribute
    # can name an arbitrarily long driver), and subprocess.run would buffer all
    # of it on a polled endpoint. Truncation fails CLOSED — a clipped answer
    # cannot prove the path is filter-free.
    rc, out, truncated = _run_git_capped(
        [*_hardened_git(), "check-attr", "-z", "filter", "diff", "--", path],
        cwd=cwd,
        env=env,
        timeout=timeout,
        max_bytes=_CHECK_ATTR_MAX_OUTPUT_BYTES,
    )
    if rc != 0 or truncated:
        return True
    fields = out.split("\0")
    values = fields[2::3]
    if not values:
        return True
    # "unspecified" (no attribute) and "unset" (explicitly disabled) are the
    # only values that cannot name a driver.
    return any(v not in ("unspecified", "unset") for v in values)


def _repo_attrs_unsafe(
    cwd: str,
    env: dict,
    *,
    timeout: float = 5.0,
    path: str | None = None,
) -> str:
    """Why this repo's content cannot be read safely, or ``""`` when it can.

    Fail-closed conditions, in order of how much they cost to check:

    1. A git dir (or common dir) that resolves into a sensitive location. The
       blob store lives there, so ``git show <base>:<rel>`` would serve its
       committed content even though the WORKTREE path passed the caller's
       validation — a ``.git`` pointer file is enough to arrange that.
    2. ANY existing ``info/attributes`` entry that is not an empty REGULAR
       file. ``lstat`` rather than ``getsize``: a FIFO reports size 0 while
       still streaming attacker-chosen attributes into git on read, and a
       symlink or unreadable entry cannot be cleared either.

    ── Why a MISSING GIT_ATTR_SOURCE is no longer a refusal ──
    Executing a driver needs BOTH halves: an ATTRIBUTE binding a path to a
    driver name (worktree ``.gitattributes``) AND a CONFIG entry defining what
    that name runs (``filter.<name>.clean``, ``diff.<name>.textconv``). An
    attribute whose driver is undefined resolves to nothing and git carries on.

    ``GIT_ATTR_SOURCE`` removes the ATTRIBUTE half, and only works on git
    >= 2.41. :func:`_isolate_repo_git_metadata` removes the CONFIG half on
    EVERY git version — content reads run against a private GIT_DIR with
    minimal config, ``GIT_CONFIG_GLOBAL=devnull`` and
    ``GIT_CONFIG_NOSYSTEM=1``, so the repository's own config is never read and
    no driver can be defined at all. Refusing whole-repo scans merely because
    the (redundant) attribute half is unavailable made the Local tab
    permanently unusable on stock git 2.34 (Ubuntu 22.04), 2.39 (Debian 12),
    and Apple git 2.39 — a dead feature for most users in exchange for a
    second layer over an already-closed hole. Verified by
    ``test_repo_filter_never_executes_without_attr_source``: with the variable
    absent and a repo carrying both halves, the canary never fires.

    What remains WITHOUT attr-source is output DISTORTION, not execution: a
    worktree ``.gitattributes`` can still set e.g. ``-diff`` to suppress a
    patch body. That is a display-fidelity concern for a read-only panel (a
    repository can misrepresent its contents many other ways), not a path to
    running code, so it does not gate the scan. The variable is still SET
    whenever git supports it, which removes even that.
    """
    for gitdir in dict.fromkeys(_git_dirs(cwd, env, timeout)):
        if is_sensitive_path(os.path.realpath(gitdir)):
            return _ATTRS_UNSAFE_SENSITIVE_GITDIR
        attrs = os.path.join(gitdir, "info", "attributes")
        try:
            st = os.lstat(attrs)
        except FileNotFoundError:
            continue
        except OSError:
            return _ATTRS_UNSAFE_INFO_ATTRS
        if not stat.S_ISREG(st.st_mode) or st.st_size > 0:
            return _ATTRS_UNSAFE_INFO_ATTRS
    # Single-file diffs keep the per-path probe: it is cheap, and a repo that
    # binds a filter to THIS file is worth refusing outright rather than
    # relying solely on config isolation.
    if not env.get("GIT_ATTR_SOURCE") and path is not None:
        if _path_has_bound_filter(cwd, path, env, timeout):
            return _ATTRS_UNSAFE_BOUND_FILTER
    return ""


def _git_path_abs(cwd: str, env: dict, name: str, timeout: float) -> str:
    """Resolve ``git rev-parse --git-path`` with an old-Git fallback."""
    args = [*_hardened_git(), "rev-parse", "--path-format=absolute", "--git-path", name]
    r = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if r.returncode != 0:
        args = [*_hardened_git(), "rev-parse", "--git-path", name]
        r = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
        )
    if r.returncode != 0 or not r.stdout.strip():
        raise subprocess.CalledProcessError(r.returncode or 1, args)
    # Newline-only trim — the resolved path may end in whitespace.
    value = r.stdout.rstrip("\n")
    absolute = value if os.path.isabs(value) else os.path.join(cwd, value)
    return os.path.realpath(absolute)


def _read_repo_metadata_file(path: str, max_bytes: int) -> bytes | None:
    """Read a REPOSITORY-CONTROLLED metadata file safely, or ``None`` if absent.

    Repository metadata is attacker-shaped, so a plain ``open()`` is unsafe on
    three counts: the entry may be a SYMLINK aimed at a protected file (the
    gateway would read it before any sensitive-path check), a FIFO (the open
    itself blocks forever, pinning an executor worker on a polled endpoint), or
    a device. The descriptor is therefore opened ``O_NOFOLLOW | O_NONBLOCK``
    and its ``fstat`` — the inode actually opened, so no check-to-use window —
    must be a regular file. Anything else raises ``PermissionError`` so the
    caller fails closed.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    # O_NOFOLLOW does not exist on Windows, where `getattr(..., 0)` silently
    # degrades the flag to a no-op — the open would then FOLLOW a
    # repository-controlled link. Pre-check the entry's own type with lstat and
    # refuse anything that is not a regular file, so the no-follow guarantee
    # holds on every platform (the fstat below still pins the inode actually
    # opened, so this pre-check only closes the platform gap, it is not the
    # sole defense).
    if not hasattr(os, "O_NOFOLLOW"):
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                raise PermissionError(f"repository metadata is not a regular file: {path}")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PermissionError(f"repository metadata could not be read safely: {path}") from exc
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # ELOOP (a symlink, refused by O_NOFOLLOW) is a refusal, not an
        # absence; anything else unreadable is treated the same way.
        raise PermissionError(f"repository metadata could not be read safely: {path}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError(f"repository metadata is not a regular file: {path}")
        return os.read(fd, max_bytes)
    except OSError as exc:
        raise PermissionError(f"repository metadata could not be read safely: {path}") from exc
    finally:
        os.close(fd)


def _index_is_split(index_path: str) -> bool:
    """Whether the index depends on a ``sharedindex.*`` sibling (split index).

    Detected from the filesystem rather than the index bytes or config: the
    ``link`` extension can sit AFTER the entry table (so a bounded prefix scan
    would miss it on a large index), and `core.splitIndex` may be set globally
    or left stale. The operative fact is whether a `sharedindex.*` file exists
    beside the index — that is the data our synthetic GIT_DIR cannot reach.
    A stale sibling yields a conservative refusal, which is the safe direction.
    """
    try:
        parent = os.path.dirname(index_path)
        with os.scandir(parent) as it:
            for entry in it:
                if entry.name.startswith("sharedindex."):
                    return True
    except OSError:
        return False
    return False


def _quote_alternate_dir(path: str) -> str:
    """Encode ONE path for ``GIT_ALTERNATE_OBJECT_DIRECTORIES``.

    That variable is a LIST separated by ``:`` on POSIX (``;`` on Windows), so
    a repository whose path contains the separator would be misparsed into two
    bogus entries and its objects would go missing (wrong diffs, files reported
    clean). Git accepts a C-style quoted entry to carry such a path literally.
    Only quote when necessary so ordinary paths stay readable in logs.
    """
    sep = ";" if sys.platform == "win32" else ":"
    if sep not in path and '"' not in path and "\\" not in path:
        return path
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _alternate_object_dirs(object_dir: str) -> list[str]:
    """``object_dir`` plus every store reachable through its alternates chain.

    Git follows ``objects/info/alternates`` RECURSIVELY, so attaching a
    repository's object dir also attaches whatever that file points at. A
    hostile repo can aim the chain at a protected object store and have
    ``git show`` serve its committed blobs. Enumerating the chain lets the
    caller reject it BEFORE the store is attached. Bounded depth, entry count,
    and a visited set keep a cyclic or fan-out chain from spinning; an
    unreadable-in-full list raises so the caller fails closed rather than
    attaching a store it could not verify.
    """
    roots: list[str] = []
    stack = [(object_dir, 0)]
    visited: set[str] = set()
    while stack:
        current, level = stack.pop()
        real = os.path.realpath(current)
        if real in visited:
            continue
        if level > _ALTERNATES_MAX_DEPTH:
            # FAIL CLOSED: skipping the rest of the chain would leave stores
            # ATTACHED but unverified — exactly the bypass this walk exists to
            # prevent. A chain deeper than the bound is refused outright.
            raise PermissionError("this repository's object alternates chain is too deep to verify")
        visited.add(real)
        roots.append(real)
        if len(visited) > _ALTERNATES_MAX_ENTRIES:
            raise PermissionError(
                "this repository's object alternates chain is too large to verify"
            )
        try:
            raw = _read_repo_metadata_file(
                os.path.join(real, "info", "alternates"), _ALTERNATES_MAX_BYTES + 1
            )
        except PermissionError:
            # Non-regular (symlink / FIFO / device) or otherwise untrustworthy
            # metadata: fail closed rather than attaching the store.
            raise
        if raw is None:
            continue
        if len(raw) > _ALTERNATES_MAX_BYTES:
            raise PermissionError("this repository's object alternates list is too large to verify")
        for line in raw.decode("utf-8", errors="replace").splitlines():
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            # Relative entries resolve against the store holding the list.
            path = entry if os.path.isabs(entry) else os.path.join(real, entry)
            stack.append((path, level + 1))
    return roots


class _IsolatedGitMetadata:
    """Private Git metadata and environment for one endpoint invocation."""

    def __init__(self, env: dict, tempdir: tempfile.TemporaryDirectory):
        self.env = env
        self._tempdir = tempdir

    def close(self) -> None:
        self._tempdir.cleanup()


def _isolate_repo_git_metadata(
    cwd: str,
    base_env: dict,
    timeout: Callable[[], float],
) -> _IsolatedGitMetadata:
    """Build a private GIT_DIR that repository contents cannot mutate.

    The synthetic directory contains only minimal non-executable config and a
    detached HEAD. The real index and object database remain explicit
    read-only inputs (``GIT_INDEX_FILE`` / an object ALTERNATE — never the
    primary object dir, so git will not follow the real store's own
    ``objects/info/alternates`` into protected locations for WRITES, and our
    private store is where the re-written empty tree lands). Repository and
    global config and the real ``info/attributes`` path are absent for every
    later content-reading Git invocation — closing the config/attributes
    TOCTOU permanently rather than by pre-check.
    """

    def _remaining() -> float:
        value = timeout()
        if value <= 0:
            raise subprocess.TimeoutExpired("git metadata isolation", value)
        return value

    gitdirs = _git_dirs(cwd, base_env, _remaining())
    for gitdir in gitdirs:
        if is_sensitive_path(gitdir):
            raise PermissionError(_ATTRS_UNSAFE_SENSITIVE_GITDIR)

    root_r = subprocess.run(
        [*_hardened_git(), "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=_remaining(),
        env=base_env,
    )
    if root_r.returncode != 0 or not root_r.stdout.strip():
        raise subprocess.CalledProcessError(root_r.returncode or 1, root_r.args)
    root = os.path.realpath(root_r.stdout.rstrip("\n"))
    if is_sensitive_path(root):
        raise PermissionError("repository worktree resolves into a protected location")
    _assert_git_outside(root)

    object_dir = _git_path_abs(cwd, base_env, "objects", _remaining())
    index_path = _git_path_abs(cwd, base_env, "index", _remaining())
    # `.git/index` is a repository-controlled PATH: a repo can make it a
    # symlink at a protected file, and _git_path_abs resolves that target — git
    # would then open it on every status/diff with no sensitive-path check.
    # Validate the resolved target before it is handed over: sensitive targets
    # are refused, and a non-regular entry (FIFO/device) is refused too since
    # git would block or misread it. A MISSING index is fine (a fresh repo has
    # none); git creates it inside our private metadata, not the real store.
    if os.path.lexists(index_path):
        if is_sensitive_path(index_path):
            raise PermissionError("this repository's index resolves into a protected location")
        try:
            if not stat.S_ISREG(os.stat(index_path).st_mode):
                raise PermissionError("this repository's index is not a regular file")
        except OSError as exc:
            raise PermissionError("this repository's index could not be validated") from exc
        # A SPLIT index (core.splitIndex=true) keeps most entries in
        # `sharedindex.<oid>` files inside the REAL git dir. Paired with our
        # synthetic GIT_DIR git cannot find them, so status/diff either fails or
        # reports changed files as CLEAN — a silent wrong answer. Refuse instead,
        # which the caller surfaces as an honest refusal.
        if _index_is_split(index_path):
            raise PermissionError(
                "this repository uses a split index (core.splitIndex), which cannot be "
                "read through isolated Git metadata"
            )
    for store in _alternate_object_dirs(object_dir):
        if is_sensitive_path(store):
            raise PermissionError(_ATTRS_UNSAFE_SENSITIVE_GITDIR)

    format_r = subprocess.run(
        [*_hardened_git(), "rev-parse", "--show-object-format"],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=_remaining(),
        env=base_env,
    )
    object_format = format_r.stdout.strip() if format_r.returncode == 0 else "sha1"
    if object_format not in ("sha1", "sha256"):
        raise ValueError("unsupported Git object format")

    head_r = subprocess.run(
        [*_hardened_git(), "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=_remaining(),
        env=base_env,
    )
    head_oid = head_r.stdout.strip() if head_r.returncode == 0 else ""

    tempdir = tempfile.TemporaryDirectory(prefix="kirocrew-git-")
    try:
        gitdir = tempdir.name
        chmod_safe(gitdir, 0o700)
        os.makedirs(os.path.join(gitdir, "objects"), mode=0o700)
        os.makedirs(os.path.join(gitdir, "refs", "heads"), mode=0o700)
        os.makedirs(os.path.join(gitdir, "refs", "tags"), mode=0o700)
        repo_version = "1" if object_format == "sha256" else "0"
        config = (
            "[core]\n"
            f"\trepositoryformatversion = {repo_version}\n"
            "\tbare = false\n"
            f"\tfilemode = {_repo_bool(cwd, base_env, 'core.fileMode', _remaining())}\n"
            f"\tsymlinks = {_repo_bool(cwd, base_env, 'core.symlinks', _remaining())}\n"
            # autocrlf governs line-ending NORMALIZATION. Omitting it while the
            # real repo sets it makes a CRLF worktree compare against LF blobs,
            # so every line reads as modified once stat info is invalidated.
            f"\tautocrlf = {_repo_bool(cwd, base_env, 'core.autocrlf', _remaining(), default='false', allowed=('true', 'false', 'input'))}\n"
        )
        if object_format == "sha256":
            config += "[extensions]\n\tobjectformat = sha256\n"
        Path(gitdir, "config").write_text(config, encoding="utf-8")
        Path(gitdir, "HEAD").write_text(
            f"{head_oid}\n" if head_oid else "ref: refs/heads/kirocrew-unborn\n",
            encoding="ascii",
        )
        env = dict(base_env)
        env.update(
            {
                "GIT_DIR": gitdir,
                "GIT_WORK_TREE": root,
                "GIT_INDEX_FILE": index_path,
                "GIT_OBJECT_DIRECTORY": os.path.join(gitdir, "objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": _quote_alternate_dir(object_dir),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        empty_tree = _empty_tree_hash(root, env, _remaining())
        if empty_tree:
            # Write the empty tree into the PRIVATE object dir so
            # GIT_ATTR_SOURCE can resolve it even if the real store lacks it.
            write_tree = subprocess.run(
                [*_hardened_git(), "hash-object", "-w", "-t", "tree", "--stdin"],
                cwd=root,
                input=b"",
                capture_output=True,
                timeout=_remaining(),
                env=env,
            )
            written_oid = write_tree.stdout.decode("ascii", errors="replace").strip()
            if write_tree.returncode != 0 or written_oid != empty_tree:
                raise subprocess.CalledProcessError(write_tree.returncode or 1, write_tree.args)
            env["GIT_ATTR_SOURCE"] = empty_tree
        return _IsolatedGitMetadata(env, tempdir)
    except Exception:
        tempdir.cleanup()
        raise


def _repo_bool(
    cwd: str,
    env: dict,
    key: str,
    timeout: float,
    *,
    default: str = "",
    allowed: tuple[str, ...] = ("true", "false"),
) -> str:
    """A repository config value, sanitized to one of ``allowed``.

    Hardcoding ``core.fileMode``/``core.symlinks``/``core.autocrlf`` in the
    synthetic config overrides the repository's own semantics: a checkout with
    ``core.fileMode=false`` (a common network-share / container setting)
    reports a phantom modification for every file whose mode bits differ, and a
    CRLF worktree whose ``autocrlf`` is dropped compares against LF blobs and
    reads as wholly modified. Only a value from the ``allowed`` allowlist is
    accepted — never a raw config string — so a hostile value cannot inject
    config syntax into the file we write. ``default`` is git's own default for
    the key (``true`` for fileMode/symlinks, ``false`` for autocrlf), used when
    the key is unset or unreadable.

    ``--type=bool`` is used only for the boolean keys; ``autocrlf`` also
    accepts ``input``, so it is read untyped and allowlisted instead.
    """
    fallback = default or ("false" if sys.platform == "win32" else "true")
    typed = ["--type=bool"] if allowed == ("true", "false") else []
    try:
        # Byte-capped: the VALUE is repository-controlled, so an oversized
        # `core.autocrlf` would otherwise be buffered in full on every poll —
        # a memory-exhaustion lever for a hostile repo. A truncated read cannot
        # be trusted to be the whole token, so it falls back to git's default.
        rc, out, truncated = _run_git_capped(
            [*_hardened_git(), "config", *typed, "--get", key],
            cwd=cwd,
            env=env,
            timeout=timeout,
            max_bytes=_REPO_CONFIG_MAX_OUTPUT_BYTES,
        )
    except (subprocess.SubprocessError, OSError, PermissionError):
        return fallback
    if rc != 0 or truncated:
        return fallback
    value = out.strip()
    return value if value in allowed else fallback


def _sensitive_git_pathspecs(root: str) -> list[str]:
    """Top-anchored exclusions for every protected path inside ``root``.

    Covers the ``$HOME``-relative protected set (``_SENSITIVE_HOME_DIRS``)
    AND the active data home (``config_dir()``) — the latter is what a custom
    ``KIROCREW_HOME`` inside a scanned repository resolves to; without it the
    security-policy/profile/token keystone files would receive no exclusion
    and ``git diff --numstat`` would read them before row filtering.
    """
    root_real = os.path.realpath(root)
    home = os.path.realpath(os.path.expanduser("~"))
    targets = [os.path.realpath(os.path.join(home, p)) for p in _SENSITIVE_HOME_DIRS]
    data_home = ""
    try:
        data_home = os.path.realpath(str(config_dir()))
        targets.append(data_home)
    except OSError:
        pass
    excludes: set[str] = set()
    for target in targets:
        try:
            if os.path.commonpath([root_real, target]) != root_real:
                continue
        except (OSError, ValueError):
            continue
        rel = os.path.relpath(target, root_real).replace(os.sep, "/")
        if rel in ("", ".", "..") or rel.startswith("../"):
            # The protected location IS the scanned root (e.g. the data home
            # was pointed at a repository root). A "." exclusion would exclude
            # everything and was previously DISCARDED, leaving the keystone
            # files fully readable by status/numstat. Exclude each secret LEAF
            # relative to the root instead, so the repo stays scannable while
            # its protected files still never reach git.
            if target == data_home and rel in ("", "."):
                for leaf in _CREW_SECRET_LEAVES:
                    leaf_rel = leaf.replace(os.sep, "/").strip("/")
                    if leaf_rel:
                        excludes.add(f":(exclude,top,literal,icase){leaf_rel}")
                        excludes.add(f":(exclude,top,glob,icase){leaf_rel}/**")
            continue
        excludes.add(f":(exclude,top,literal,icase){rel}")
        excludes.add(f":(exclude,top,glob,icase){rel}/**")
    return [".", *sorted(excludes)] if excludes else []


def _git_diff_base(cwd: str, env: dict, timeout: float = 5.0) -> str:
    """``HEAD`` when it exists, else the repo's empty-tree hash.

    On an unborn branch (fresh ``git init``, nothing committed) ``HEAD`` does
    not resolve, so ``git diff HEAD``/``git show HEAD:...`` fail outright and
    staged-but-never-committed files would report no diff at all. Diffing
    against the empty tree yields the correct all-added patches.
    """
    try:
        r = subprocess.run(
            [*_hardened_git(), "rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        if r.returncode == 0:
            return "HEAD"
    except (subprocess.SubprocessError, OSError):
        return "HEAD"
    # Fall back to HEAD: downstream git calls fail the same way they always
    # did on an unresolvable base, and their rc checks handle it.
    return _empty_tree_hash(cwd, env, timeout) or "HEAD"


_HARDLINK_REFUSED = "this path is a hard link to another inode, so its content is not served"


def _is_multilink_regular(path: str) -> bool:
    """Whether ``path`` is a REGULAR file with more than one hard link.

    A hard link has no target to resolve, so ``realpath`` and the
    sensitive-path gate both see only the benign repository path — yet
    ``git diff -- <path>`` reopens it BY NAME and would emit the linked
    inode's bytes. Refusing multi-link regular files is the only reliable
    guard for the TRACKED diff path, where the bytes come from git rather
    than from our own descriptor-pinned reader.

    Symlinks and directories are unaffected (a symlink's content is the
    target string we read via ``readlink``). An unstattable path reports
    False so a routine race degrades to the caller's normal not-found
    handling instead of a misleading security refusal.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink > 1


def _hardlinked_tracked_paths(worktree: str, env: dict, budget: Callable[[], float]) -> list[str]:
    """Repo-relative tracked paths that are multi-link inodes.

    METADATA ONLY: `ls-files -z` lists the index (no content read), and each
    candidate is `lstat`'d. Callers exclude these paths from `status` and
    `--numstat`, both of which would otherwise READ a tracked file's content to
    decide whether it differs — leaking a protected file's ±line counts when a
    tracked path has been swapped for a hard link to it.

    Bounded by the same budget as every other probe. A LIST we cannot read in
    full raises ``PermissionError`` (fail closed) rather than returning a
    partial exclusion set — a path beyond the cut would be read by
    status/numstat, which is the leak this preflight prevents. A spawn failure
    likewise propagates to the caller's refusal handling.
    """
    rc, raw, truncated = _run_git_capped_bytes(
        [*_hardened_git(), "ls-files", "-z"],
        cwd=worktree,
        env=env,
        timeout=budget(),
        max_bytes=_HARDLINK_PREFLIGHT_MAX_OUTPUT_BYTES,
    )
    if rc != 0:
        raise PermissionError(
            "this repository's tracked-file list could not be read for hard-link " "verification"
        )
    entries = raw.split(b"\0")
    if truncated:
        # FAIL CLOSED: a capped ls-files read means we do not know the full
        # tracked set, so any path beyond the cut would go UNEXCLUDED and its
        # content would be read by status/numstat — the exact leak this
        # preflight exists to prevent. Refuse the scan instead of silently
        # covering only a prefix.
        raise PermissionError(
            "this repository's tracked-file list is too large to verify for hard links"
        )
    real_entries = [e for e in entries if e]
    if len(real_entries) > _HARDLINK_PREFLIGHT_MAX_PATHS:
        raise PermissionError(
            "this repository has more tracked files than the hard-link preflight " "can verify"
        )
    found: list[str] = []
    for raw_entry in real_entries:
        try:
            rel = raw_entry.decode("utf-8")
        except UnicodeDecodeError:
            # FAIL CLOSED. `continue` here left the path OUT of the exclusion
            # set, so status/numstat would still read it — a non-UTF-8 tracked
            # name hard-linked to a protected inode would bypass the very check
            # this preflight performs. We cannot verify what we cannot name, and
            # the exclusion pathspec needs a decodable string, so the scan is
            # refused instead. (Such names are impossible on macOS/Windows; the
            # OS rejects them. Linux permits them.)
            raise PermissionError(
                "this repository has a tracked path that cannot be decoded for "
                "hard-link verification"
            )
        if _is_multilink_regular(os.path.join(worktree, rel)):
            found.append(rel)
    return found


def _is_gitlink_dir(path: str) -> bool:
    """Whether ``path`` is a submodule mount point (has its own ``.git``).

    Metadata-only: a submodule carries a ``.git`` entry (directory in an old
    layout, gitdir-pointer FILE in a modern one). Used to let the ONE
    legitimate directory row through the file-diff handler while every other
    directory is refused — a plain directory would make ``git diff -- <dir>``
    recurse over tracked descendants the caller never named.
    """
    try:
        return os.path.exists(os.path.join(path, ".git"))
    except OSError:
        return False


def _worktree_new_lines(path: str, root: str) -> list[str] | None:
    """Current worktree content as diff lines, or ``None`` if unavailable.

    ``[]`` means a successfully read empty file. ``None`` means unreadable,
    oversized, binary, hard-linked, or raced away; callers must not synthesize
    a complete replacement patch from that state.

    Reads via ``safe_read_file_bytes_nolink``: the descriptor-pinned read
    rejects multi-link inodes, so a hard link planted at a protected file
    (``ln ~/.ssh/id_rsa repo/innocent.txt`` — the path validator sees only the
    benign project path, because a hard link HAS no target to resolve) is
    refused instead of served as an all-added patch. ``within_root`` pins the
    opened descriptor inside the repository on platforms that can verify it.
    """
    if os.path.islink(path):
        try:
            return [os.readlink(path)]
        except OSError:
            return None
    # In-function import (kept local like handlers/files.py): hooks is a heavy
    # security module, and tests patch kiro_crew.hooks.* — a top-level `from`
    # import would bind the symbol early and defeat those patches.
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        # Type gate BEFORE any open: a FIFO (or device) in the worktree would
        # block the open indefinitely and pin an executor worker, which on a
        # 10s-polled endpoint is a denial of service. safe_read_file_bytes_nolink
        # rejects non-regular files too, but only AFTER its open returns.
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        if os.path.getsize(path) > _FILE_DIFF_MAX_UNTRACKED_BYTES:
            return None
        # within_root passed UNCONDITIONALLY. On POSIX it verifies the opened
        # descriptor's real path lies inside the repository. On Windows
        # `_fd_real_path` cannot resolve a handle, so the helper fails CLOSED
        # and returns nothing — the honest outcome: untracked-file diff
        # synthesis is simply unavailable there, rather than following a path
        # swapped to a sensitive symlink between validation and open.
        raw = safe_read_file_bytes_nolink(path, within_root=root)
        if (
            raw is not None
            and len(raw) <= _FILE_DIFF_MAX_UNTRACKED_BYTES
            and b"\0" not in raw[:8192]
        ):
            return raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    except (OSError, FileTooLargeError):
        return None
    return None


# ── Bounds ────────────────────────────────────────────────────────────────
# Changed files reported for the repo — keeps the response and the UI bounded
# on an enormous dirty tree.
GIT_CHANGES_MAX_FILES = 500
# Per-subprocess ceiling and aggregate wall-clock budget for one scan. The
# per-command timeouts bound individual calls, but one scan spawns several git
# processes and asyncio.to_thread work is not cancelled when the HTTP request
# is — on expiry the scan stops and the response is marked truncated.
_GIT_CHANGES_TIMEOUT_SECS = 10
_GIT_CHANGES_SCAN_DEADLINE_SECS = 10.0
# Byte caps on subprocess stdout (via _run_git_capped) — a repo with an
# enormous dirty tree must not buffer unbounded status/numstat output into a
# 10s-polled endpoint. 2MB comfortably covers GIT_CHANGES_MAX_FILES entries.
_GIT_CHANGES_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
# Cap on the untracked-file diff synthesis in api_file_diff (read + linesplit
# + difflib all scale with file size).
_FILE_DIFF_MAX_UNTRACKED_BYTES = 1024 * 1024
# Cap on api_file_diff's `git show`/`git diff` stdout — a multi-GB tracked
# (or deleted) file must not buffer in full on expand.
_FILE_DIFF_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
# Cap on api_file_diff's single-path `git status --porcelain` probe. Only a
# few entries are ever inspected, but the queried path can be a directory
# whose status expands to one line per contained file.
_FILE_DIFF_MAX_STATUS_BYTES = 64 * 1024
# Cap on the `check-attr` safety probe. Its output echoes REPOSITORY-CONTROLLED
# attribute values, so it is attacker-sized; 64KB is far beyond any legitimate
# filter/diff driver name and truncation fails closed.
_CHECK_ATTR_MAX_OUTPUT_BYTES = 64 * 1024
# Bounds on the object-alternates chain walk (_alternate_object_dirs). Git
# follows objects/info/alternates recursively, so a hostile repo can present a
# deep, wide, or cyclic chain purely to stall the verification that precedes
# attaching its object store.
_ALTERNATES_MAX_DEPTH = 8
_ALTERNATES_MAX_ENTRIES = 64
_ALTERNATES_MAX_BYTES = 64 * 1024
# Tracked paths lstat'd by the hard-link preflight. Bounds the metadata-only
# sweep on a very large index; past the cap the per-row kind:'hardlink' label
# and /api/file-diff's refusal remain the (content-safe) backstop.
_HARDLINK_PREFLIGHT_MAX_PATHS = 5000
# Cap on a single `git config --get` read. The VALUE is repository-controlled,
# so this is attacker-sized; a legitimate bool/`input` token is under 8 bytes.
_REPO_CONFIG_MAX_OUTPUT_BYTES = 4 * 1024
# Cap on the preflight `ls-files` read. Its OWN constant: the status/numstat
# cap is a RESPONSE-size bound, while this must be large enough to enumerate
# the whole index (a truncated read fails the scan closed).
_HARDLINK_PREFLIGHT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def _git_status_label(code: str) -> str:
    """Map a porcelain v1 XY code to a coarse per-file status label."""
    if code == "??":
        return "untracked"
    if "U" in code or code in ("AA", "DD"):
        return "conflicted"
    # Prefer the worktree (Y) column; fall back to the index (X) column.
    key = code[1] if len(code) > 1 and code[1] != " " else code[0]
    return {
        "M": "modified",
        "A": "added",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
        "T": "modified",
    }.get(key, "modified")


def _resolve_repo_root(base: str, env: dict, timeout: float) -> str | None:
    """The git worktree root containing ``base``, or ``None``.

    Single-repo by design: the chat slot's project directory must itself BE a
    repository (or live inside one — ``rev-parse --show-toplevel`` walks the
    ancestors). No child-directory sweep: users who want the panel point the
    chat at a repo. Returns the realpath'd root, refusing sensitive locations.
    A ``TimeoutExpired`` propagates — the caller must report an expired budget
    as a partial scan, not as "no repository here".
    """
    try:
        r = subprocess.run(
            [*_hardened_git(), "rev-parse", "--show-toplevel"],
            cwd=base,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise
    except PermissionError:
        # A security refusal (writable/contained git executable) — NOT "no
        # repository here". PermissionError is an OSError subclass, so it must
        # be re-raised ahead of the degrade-to-None arm below or the caller
        # would report a hostile setup as a clean absence of any repo.
        raise
    except OSError:
        return None
    if r.returncode != 0:
        return None
    # Newline-only trim: a worktree path may legally end in a space.
    top = r.stdout.rstrip("\n")
    if not top:
        return None
    root = os.path.realpath(top)
    if is_sensitive_path(root):
        return None
    # Containment verdict on the FIRST spawn's result: if the pinned git
    # executable lives inside this repository, every further probe would run
    # attacker-supplied code — refuse before any additional spawn.
    _assert_git_outside(root)
    return root


async def api_file_diff(request: web.Request) -> web.Response:
    """GET /api/file-diff?path=... — git diff and HEAD content for one file.

    The file does NOT have to exist in the working tree: a tracked-but-deleted
    path returns its deletion patch (status ``deleted``) so reviewers can
    inspect removed content. ``?lexical=1`` (the Local Changes view) keeps the
    LEXICAL path — realpath would make a changed symlink diff its TARGET
    instead of the changed link entry itself; other consumers keep the
    endpoint's canonical-path behavior (they read file CONTENT through the
    canonical path, so their diffs must too).
    """
    # NOT .strip(): a trailing (or leading) space is a legal POSIX path
    # component, and trimming it retargets a valid file to one that does not
    # exist. Emptiness is checked separately below.
    raw_query_path = request.query.get("path", "")
    lexical = request.query.get("lexical", "").strip() in ("1", "true")
    caller = request.get("user", "dashboard")
    if not raw_query_path:
        _sel().log_api_access(
            caller=caller, operation="file_diff", outcome="allowed", resources="empty_path"
        )
        return web.json_response({"diff": "", "original": ""})
    # See api_git_changes: reject an embedded NUL on the string itself so the
    # verdict does not depend on whether the platform's path calls object.
    if "\x00" in raw_query_path:
        _sel().log_api_access(
            caller=caller,
            operation="file_diff",
            outcome="denied",
            resources=raw_query_path[:256].replace("\x00", "<NUL>"),
            error="malformed path",
        )
        return web.json_response({"error": "Malformed path"}, status=400)

    def _run() -> dict:
        # EVERYTHING filesystem-touching (validation included — realpath and
        # the sensitive-path probes stat the filesystem) runs on this worker
        # thread: a stalled network filesystem must never freeze the event
        # loop of a polled endpoint.
        # In-function import (kept local like handlers/files.py): tests patch
        # kiro_crew.hooks.validate_file_path — a top-level `from` import would
        # bind the symbol early and defeat those patches.
        from kiro_crew.hooks import validate_file_path

        # Gate on the CANONICAL path (central hooks validator: realpath +
        # sensitive check) regardless of the lexical flag — the SEMANTICS git
        # sees are consumer-selected, the security gate is not. A MALFORMED
        # path (embedded NUL, bad surrogate) makes the validator's own os.path
        # calls raise ValueError; that is a bad request, not a server fault.
        try:
            if validate_file_path(raw_query_path) is None:
                return {"_denied": True}
        except (ValueError, OSError):
            return {"_bad_request": True}
        if lexical:
            # abspath: anchors a RELATIVE path against the process CWD
            # lexically — unlike realpath it does not resolve symlinks, so the
            # lexical-path contract holds. Without it, "src/foo.py" would run
            # git from "src" and pass "src/foo.py", targeting "src/src/foo.py".
            raw_path = os.path.abspath(os.path.expanduser(raw_query_path))
        else:
            raw_path = os.path.realpath(os.path.expanduser(raw_query_path))
        # A tracked hard link would be reopened BY NAME by `git diff`, serving
        # the linked inode's bytes even though the queried path itself is
        # benign. Refuse before any content read (see _is_multilink_regular).
        if _is_multilink_regular(raw_path):
            return {
                "diff": "",
                "original": "",
                "status": "filters_unsafe",
                "error": _HARDLINK_REFUSED,
                "_path": raw_path,
            }
        # lexists: a changed symlink (even dangling) is present in the tree —
        # only a truly absent entry takes the deleted path.
        file_missing = not os.path.lexists(raw_path)
        # A DIRECTORY must not reach `git diff -- <path>`: git would recurse and
        # emit a patch for every tracked descendant, so a repo-root request
        # would return content the caller never named and that never passed
        # per-path validation. (The original handler's os.path.isfile() gate did
        # this implicitly; it was replaced by lexists to support deleted paths,
        # which reopened the directory case.) A gitlink — a submodule mount
        # point — is the one legitimate directory row: its "diff" is a one-line
        # commit-pointer change, and --ignore-submodules=dirty keeps git out of
        # the child, so it is allowed through.
        if not os.path.islink(raw_path) and os.path.isdir(raw_path):
            if not _is_gitlink_dir(raw_path):
                return {
                    "diff": "",
                    "original": "",
                    "status": "not_git",
                    "error": "Directories are not diffable",
                    "_path": raw_path,
                }

        # Nearest EXISTING ancestor as the git cwd — the file (or even its
        # directory) may have been deleted.
        dirpath = os.path.dirname(raw_path)
        while dirpath and dirpath != os.path.dirname(dirpath) and not os.path.isdir(dirpath):
            dirpath = os.path.dirname(dirpath)
        if not os.path.isdir(dirpath):
            return {"_not_found": True, "_path": raw_path, "diff": "", "original": ""}

        isolated: _IsolatedGitMetadata | None = None
        try:
            # _hardened_git() raises FileNotFoundError when no git executable
            # exists at all — inside the try so it degrades to not_git.
            _git = _hardened_git()
            base_env = _repo_git_env(dirpath)
            # Establish repository membership before any old-Git fallback or
            # attribute probe — a standalone file must be not_git, never a
            # security refusal caused by check-attr failing outside a repo.
            # --show-toplevel (rather than --git-dir) so the SAME single spawn
            # also yields the worktree root: the containment verdict
            # (_assert_git_outside) then fires before any further probe can
            # run a git executable the repository itself supplies.
            top_r = subprocess.run(
                [*_git, "rev-parse", "--show-toplevel"],
                cwd=dirpath,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
                env=base_env,
            )
            if top_r.returncode != 0 or not top_r.stdout.strip():
                return {"diff": "", "original": "", "status": "not_git", "_path": raw_path}
            _assert_git_outside(os.path.realpath(top_r.stdout.rstrip("\n")))
            attrs_unsafe = _repo_attrs_unsafe(dirpath, base_env, path=raw_path)
            if attrs_unsafe:
                return {
                    "diff": "",
                    "original": "",
                    "status": "filters_unsafe",
                    "error": attrs_unsafe,
                    "_path": raw_path,
                }
            # The pre-check above is not the race boundary. All later Git
            # calls use a private GIT_DIR with minimal config, so swapping the
            # real info/attributes after lstat cannot bind or execute a driver.
            isolated = _isolate_repo_git_metadata(dirpath, base_env, lambda: 5.0)
            _env = isolated.env
            root = _env["GIT_WORK_TREE"]
            rel = os.path.relpath(raw_path, root)
            # Unborn-HEAD safe base: "HEAD", or the empty tree before the
            # first commit (a staged new file then diffs as all-added instead
            # of failing rev resolution and reporting "clean").
            base_rev = _git_diff_base(dirpath, _env)
            # Byte-capped bytes-mode reads: a multi-GB tracked file must not
            # buffer in full (subprocess.run would), and `git show` of a
            # BINARY file must not be decoded strictly (a deleted PNG would
            # raise and be misreported as not_git).
            head_rc, head_bytes, head_trunc = _run_git_capped_bytes(
                [*_git, "show", "--no-textconv", f"{base_rev}:{rel}"],
                cwd=dirpath,
                env=_env,
                timeout=10,
                max_bytes=_FILE_DIFF_MAX_OUTPUT_BYTES,
            )
            head_binary = b"\0" in head_bytes[:8192]
            original = (
                ""
                if (head_rc != 0 or head_binary)
                else head_bytes.decode("utf-8", errors="replace")
            )
            # The diff works for deleted paths too (git diffs against the
            # base). Binary content yields git's short "Binary files ...
            # differ" text.
            diff_rc, diff_text, diff_trunc = _run_git_capped(
                [
                    *_git,
                    "diff",
                    "--no-textconv",
                    "--no-ext-diff",
                    # Same reason as the scan probes: a submodule is a separate
                    # repository whose own config our synthetic GIT_DIR does not
                    # cover, so diffing its content would run the CHILD's
                    # clean/process filters. Applies here too — a per-file
                    # request can name a gitlink path directly.
                    "--ignore-submodules=dirty",
                    base_rev,
                    "--",
                    raw_path,
                ],
                cwd=dirpath,
                env=_env,
                timeout=10,
                max_bytes=_FILE_DIFF_MAX_OUTPUT_BYTES,
            )
            # rstrip("\n") only: `.strip()` would also eat a genuine terminal
            # blank/space-bearing context line, which the row parser preserves
            # by design (see parseUnifiedDiff's no-trailing-cleanup note).
            diff = diff_text.rstrip("\n") if diff_rc == 0 else ""
            # A FAILED diff is not an empty diff: falling through would let the
            # status land on "clean" while porcelain still reports a change.
            diff_failed = diff_rc != 0
            staged_basis = False

            def _flags(out: dict) -> dict:
                # Completeness signals: a capped read returned partial content
                # (possibly ending mid-line). Split per stream — a complete
                # patch alongside a truncated original must not be conflated
                # (Monaco-style consumers diff against `original` and would
                # render the tail of a >cap file as newly added). `truncated`
                # is kept as the OR of both for existing consumers.
                if head_trunc:
                    out["original_truncated"] = True
                if diff_trunc:
                    out["diff_truncated"] = True
                if head_trunc or diff_trunc:
                    out["truncated"] = True
                # A BINARY baseline is blanked above, so there is no text to
                # diff against: without this a two-pane consumer would render
                # the decoded current file as entirely added. The patch itself
                # (git's "Binary files ... differ") still renders in the Local
                # view, which shows the patch rather than a two-way comparison.
                if head_binary and head_rc == 0:
                    out["diff_unavailable"] = True
                    out.setdefault(
                        "error",
                        "The committed version of this file is binary, so there is no "
                        "text baseline to compare against",
                    )
                out["_path"] = raw_path
                return out

            def _staged_fallback() -> tuple[str, bool]:
                # Index-only change: `git status` reports a staged column but
                # the base-vs-worktree diff is empty (e.g. staged edit whose
                # worktree was restored to base, or a staged addition deleted
                # from the worktree). Surface the staged patch so the row is
                # inspectable at all.
                c_rc, c_text, c_trunc = _run_git_capped(
                    [
                        *_git,
                        "diff",
                        "--cached",
                        "--no-textconv",
                        "--no-ext-diff",
                        base_rev,
                        "--",
                        raw_path,
                    ],
                    cwd=dirpath,
                    env=_env,
                    timeout=10,
                    max_bytes=_FILE_DIFF_MAX_OUTPUT_BYTES,
                )
                if c_rc == 0 and c_text.strip():
                    # strip() only as an EMPTINESS test above; the returned
                    # patch keeps its trailing context (rstrip newline only).
                    return c_text.rstrip("\n"), c_trunc
                return "", False

            def _porcelain() -> str:
                # Byte-capped: `path` may be a DIRECTORY (a modified gitlink,
                # or any dir a caller passes), and `git status -- <dir>` with
                # --untracked-files defaults can emit one line per file
                # underneath it. rstrip only: the LEADING character is the
                # index (X) column — " M path" (unstaged-only) must not
                # collapse to "M path".
                st_rc, st_out, _st_trunc = _run_git_capped(
                    [*_git, "status", "--porcelain", "--", raw_path],
                    cwd=dirpath,
                    env=_env,
                    timeout=5,
                    max_bytes=_FILE_DIFF_MAX_STATUS_BYTES,
                )
                return st_out.rstrip() if st_rc == 0 else ""

            if file_missing:
                # Tracked-but-deleted: deletion patch + HEAD content. Gate on
                # `git show` SUCCEEDING, not on `original` being non-empty —
                # a deleted EMPTY (or binary) file has a valid deletion patch.
                if diff and head_rc == 0:
                    return _flags({"diff": diff, "original": original, "status": "deleted"})
                # Staged-then-removed (porcelain "AD"): absent from both base
                # and worktree, so the diff above is empty — the only
                # inspectable content is the staged patch.
                staged_diff, staged_trunc = _staged_fallback()
                if staged_diff:
                    diff_trunc = diff_trunc or staged_trunc
                    return _flags({"diff": staged_diff, "original": original, "status": "deleted"})
                # Anything else missing is simply unknown to git — empty
                # result, matching the endpoint's historical behavior.
                return {"diff": "", "original": "", "_path": raw_path}
            # Staged deletion whose path was RECREATED ("D " + "?? " for one
            # path): `git diff` cannot see untracked content, so the patch
            # above is a pure DELETION even though the file exists — expanding
            # the row would hide the replacement entirely. Synthesize
            # base-content -> current-content instead. Requires a usable
            # original (a binary/unreadable base has nothing to diff).
            if diff and "+++ /dev/null" in diff and head_rc == 0 and not head_binary:
                # Scan every entry, not just the first: this state emits TWO
                # porcelain lines for one path and git prints the index
                # deletion ahead of the untracked replacement.
                if any(line.startswith("??") for line in _porcelain().splitlines()):
                    replacement = _worktree_new_lines(raw_path, root)
                    if head_trunc or replacement is None:
                        return _flags(
                            {
                                "diff": "",
                                "original": original,
                                "status": "modified",
                                "error": (
                                    "Replacement diff unavailable because one side "
                                    "could not be read completely"
                                ),
                                "diff_unavailable": True,
                            }
                        )
                    diff = "".join(
                        difflib.unified_diff(
                            original.splitlines(keepends=True),
                            replacement,
                            fromfile=rel,
                            tofile=rel,
                        )
                    )
                    return _flags({"diff": diff, "original": original, "status": "modified"})
            if not diff:
                porcelain = _porcelain()
                if porcelain.startswith("??"):
                    # Synthesize the all-added patch in Python instead of
                    # `git diff --no-index /dev/null <path>`: /dev/null is not
                    # portable to native Windows, and difflib output is
                    # byte-for-byte adequate for the frontend renderers.
                    # A None replacement means the content was REFUSED
                    # (oversized, binary, hard-linked, or raced away) — that
                    # must surface as diff_unavailable, never collapse into an
                    # authoritative-looking empty patch indistinguishable from
                    # a genuinely empty file.
                    new_lines = _worktree_new_lines(raw_path, root)
                    if new_lines is None:
                        return {
                            "diff": "",
                            "original": "",
                            "status": "untracked",
                            "error": (
                                "Diff unavailable because the file could not " "be read completely"
                            ),
                            "diff_unavailable": True,
                            "_path": raw_path,
                        }
                    diff = "".join(
                        difflib.unified_diff(
                            [],
                            new_lines,
                            fromfile="/dev/null",
                            tofile=rel,
                        )
                    )
                    return {"diff": diff, "original": "", "status": "untracked", "_path": raw_path}
                # Index-only change (porcelain "M ", "A ", ...): worktree
                # matches the base but the index differs — show the staged
                # patch instead of misreporting "clean".
                if porcelain and porcelain[0] not in (" ", "?"):
                    staged_diff, staged_trunc = _staged_fallback()
                    if staged_diff:
                        diff = staged_diff
                        diff_trunc = diff_trunc or staged_trunc
                        staged_basis = True
            status = "modified" if diff else "clean"
            out = _flags({"diff": diff, "original": original, "status": status})
            if diff_failed and not diff:
                # git could not produce the patch at all — report it instead of
                # implying the file matches its baseline.
                out["diff_unavailable"] = True
                out.setdefault("error", "Git could not produce a diff for this file")
            if staged_basis:
                # The patch describes base -> INDEX, but `original` is the base
                # and the editor's buffer is the WORKTREE (identical to base
                # here). A two-pane consumer diffing those would render an
                # empty comparison next to a non-empty patch. The Local view
                # renders the patch itself and is unaffected; every other
                # consumer must treat this as no usable two-way basis.
                out["diff_basis"] = "staged"
                if not lexical:
                    out["diff_unavailable"] = True
                    out.setdefault(
                        "error",
                        "Only a staged (index) change exists for this file, so there "
                        "is no working-tree diff to display",
                    )
            return out
        except PermissionError as exc:
            # A security refusal (git-exe containment, sensitive git dir or
            # worktree from metadata isolation) — surfaced as filters_unsafe
            # with its reason, never masked as not_git. Ordered BEFORE the
            # OSError arm below (PermissionError is an OSError subclass).
            return {
                "diff": "",
                "original": "",
                "status": "filters_unsafe",
                "error": str(exc) or _ATTRS_UNSAFE_SENSITIVE_GITDIR,
                "_path": raw_path,
            }
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            OSError,
            UnicodeDecodeError,
            ValueError,
        ):
            # OSError (not just FileNotFoundError): the safety probes spawn
            # git too, so a permission race on the repo must degrade to
            # not_git rather than escaping as a 500.
            return {"diff": "", "original": "", "status": "not_git", "_path": raw_path}
        finally:
            if isolated is not None:
                isolated.close()

    result = await asyncio.to_thread(_run)
    display_path = result.pop("_path", raw_query_path)
    if result.pop("_bad_request", False):
        _sel().log_api_access(
            caller=caller,
            operation="file_diff",
            outcome="denied",
            resources=raw_query_path[:256],
            error="malformed path",
        )
        return web.json_response({"error": "Malformed path"}, status=400)
    if result.pop("_denied", False):
        _sel().log_api_access(
            caller=caller,
            operation="file_diff",
            outcome="denied",
            resources=raw_query_path,
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied"}, status=403)
    if result.pop("_not_found", False):
        _sel().log_api_access(
            caller=caller,
            operation="file_diff",
            outcome="allowed",
            resources=f"path={display_path}",
            error="not_found",
        )
        return web.json_response(result)
    # A filters_unsafe result is a security REFUSAL — auditing it as "allowed"
    # would hide the denial from SEL (the endpoint served no content).
    if result.get("status") == "filters_unsafe":
        _sel().log_api_access(
            caller=caller,
            operation="file_diff",
            outcome="denied",
            resources=f"path={display_path}",
            error=str(result.get("error", "filters_unsafe")),
        )
    else:
        _sel().log_api_access(
            caller=caller,
            operation="file_diff",
            outcome="allowed",
            resources=f"path={display_path}",
        )
    return web.json_response(result)


async def api_git_changes(request: web.Request) -> web.Response:
    """GET /api/git-changes?dir=... — working-tree changes for the repo at ``dir``.

    Response: ``{"dir": ..., "repo": {"root", "name", "branch", "files":
    [{"path", "rel", "status", "staged", "additions"?, "deletions"?, "kind"?}],
    "truncated"?} | null, "truncated"?, "filters_unsafe"?}``.

    ``repo: null`` means ``dir`` is not inside a git repository (users point
    the chat's project directory at a repo to see its changes — there is
    deliberately NO multi-repo child sweep). A repo with an empty ``files``
    list is clean. Per-file diffs are served by /api/file-diff.
    """
    caller = request.get("user", "dashboard")
    # NOT .strip() — see api_file_diff: whitespace is legal in a path.
    raw_dir = request.query.get("dir", "")
    if not raw_dir:
        # Audited like every other terminal outcome — the one-outcome-per-request
        # contract has no exemption for malformed input.
        _sel().log_api_access(
            caller=caller,
            operation="git_changes",
            outcome="denied",
            resources="missing_dir",
            error="missing dir parameter",
        )
        return web.json_response({"error": "Missing dir parameter"}, status=400)
    # An embedded NUL can never name a real path. Rejected here, on the string
    # itself, rather than relying on a filesystem call to object: POSIX raises
    # ValueError from os.path.realpath while Windows silently treats it as a
    # missing path, which would answer 200 for the same malformed input.
    if "\x00" in raw_dir:
        _sel().log_api_access(
            caller=caller,
            operation="git_changes",
            outcome="denied",
            resources=raw_dir[:256].replace("\x00", "<NUL>"),
            error="malformed path",
        )
        return web.json_response({"error": "Malformed path"}, status=400)

    def _run() -> dict:
        # Validation (realpath + sensitive probes) stats the filesystem too —
        # everything runs on this worker thread, never the event loop.
        # In-function import (kept local like handlers/files.py): tests patch
        # kiro_crew.hooks.validate_file_path — a top-level `from` import would
        # bind the symbol early and defeat those patches.
        from kiro_crew.hooks import validate_file_path

        # A MALFORMED path (embedded NUL, bad surrogate) makes the validator's
        # own os.path calls raise ValueError — a bad request, not a fault.
        try:
            validated = validate_file_path(raw_dir)
        except (ValueError, OSError):
            return {"_bad_request": True}
        if validated is None:
            return {"_denied": True}
        base = validated
        if not os.path.isdir(base):
            return {"dir": base, "repo": None, "_not_found": True}

        # Deadline starts BEFORE repo resolution: the rev-parse it runs is
        # part of the advertised budget, not free work ahead of it.
        deadline = time.monotonic() + _GIT_CHANGES_SCAN_DEADLINE_SECS

        def _budget() -> float:
            """Timeout for the next git call: the SMALLER of the per-command
            limit and the time left in the aggregate budget. EVERY spawn on
            this path takes it, including the environment and safety probes —
            asyncio.to_thread work is not cancelled when the HTTP request is,
            so without this a slow filesystem could pin an executor thread far
            past the documented budget."""
            return max(0.0, min(float(_GIT_CHANGES_TIMEOUT_SECS), deadline - time.monotonic()))

        try:
            root = _resolve_repo_root(base, _hardened_git_env(), _budget() or 0.001)
        except FileNotFoundError:
            # No git executable at all — indistinguishable from "no repo" for
            # a visual panel, and not worth a 500.
            root = None
        except subprocess.TimeoutExpired:
            # Budget expired before the repo was even resolved: a partial
            # scan, NOT "no repository here".
            return {"dir": base, "repo": None, "truncated": True}
        except PermissionError as exc:
            # Containment refusal (_assert_git_outside): a security refusal,
            # surfaced with its reason — never masked as "no repository".
            return {"dir": base, "repo": None, "filters_unsafe": str(exc)}
        if root is None:
            return {"dir": base, "repo": None}

        out: dict = {"dir": base}
        undecodable = False
        scan_failed = False
        counts_incomplete = False
        hardlinks_excluded = False
        base_env = _repo_git_env(root, _budget())
        isolated: _IsolatedGitMetadata | None = None
        try:
            repo_unsafe = _repo_attrs_unsafe(root, base_env, timeout=_budget())
            if repo_unsafe:
                out["repo"] = None
                out["filters_unsafe"] = repo_unsafe
                return out
            branch_r = subprocess.run(
                [*_hardened_git(), "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=_budget(),
                env=base_env,
            )
            branch = branch_r.stdout.strip() if branch_r.returncode == 0 else ""
            isolated = _isolate_repo_git_metadata(root, base_env, _budget)
            _env = isolated.env
            worktree = os.path.normpath(_env["GIT_WORK_TREE"])
            pathspecs = _sensitive_git_pathspecs(worktree)
            # Multi-link inodes must be excluded BEFORE any content-reading
            # command. `status` and `--numstat` both read a tracked file's
            # content to decide whether it differs from the index/base, so a
            # tracked path swapped for a hard link to a protected file would be
            # read on every poll and its ±line counts returned — labeling the
            # row kind:'hardlink' afterwards is too late for that metadata leak.
            # The preflight is metadata-only (`ls-files` + lstat, no content),
            # and its exclusions are appended to the pathspec set so git never
            # opens those paths at all.
            hardlink_rels = _hardlinked_tracked_paths(worktree, _env, _budget)
            if hardlink_rels:
                # These paths are excluded from status/numstat entirely, so we
                # cannot know whether they changed. Marking the scan incomplete
                # keeps an empty result from reading as "working tree clean"
                # when the ONLY change is a hard-linked file. Synthetic rows
                # were the alternative and were rejected: most tracked files are
                # unmodified, so emitting a row per hard link would invent
                # changes that do not exist.
                hardlinks_excluded = True
                if not pathspecs:
                    pathspecs = ["."]
                for rel_excl in hardlink_rels:
                    pathspecs.append(f":(exclude,top,literal){rel_excl}")
            # -z: NUL-delimited entries with VERBATIM paths — no C-style
            # quoting to undo, and filenames with leading/trailing whitespace
            # survive intact. Read as BYTES and decoded STRICTLY per entry:
            # -z also disables core.quotePath, so a non-UTF-8 name arrives as
            # raw bytes, and errors="replace" would coin a U+FFFD path that
            # exists nowhere (its diff and editor-open both fail) and can
            # collide with another mangled name. Such an entry is skipped and
            # the scan reported partial instead.
            # --untracked-files=all: the default reports a NEW DIRECTORY as
            # one "?? newdir/" row whose diff can't render — enumerate the
            # actual files (the byte cap still bounds the output).
            status_rc, status_raw, status_truncated = _run_git_capped_bytes(
                [
                    *_hardened_git(),
                    "status",
                    "--porcelain",
                    "--no-renames",
                    "--untracked-files=all",
                    # A submodule is a SEPARATE repository with its own config,
                    # which our synthetic GIT_DIR does not cover: inspecting a
                    # submodule's dirty CONTENT would run ITS clean/process
                    # filters. `dirty` ignores every change inside the child
                    # work tree (so git never diffs its content) while still
                    # reporting the gitlink itself when the recorded commit
                    # differs — the kind:'dir' row users expect. `all` would
                    # hide the gitlink entirely and silently drop real changes.
                    "--ignore-submodules=dirty",
                    "-z",
                    *(["--", *pathspecs] if pathspecs else []),
                ],
                cwd=worktree,
                env=_env,
                timeout=_budget(),
                max_bytes=_GIT_CHANGES_MAX_OUTPUT_BYTES,
            )
            if status_rc != 0:
                raise subprocess.CalledProcessError(status_rc, "git status")
            # One numstat covers every tracked change (staged + unstaged vs
            # the base) — far cheaper than a per-file diff fan-out. Untracked
            # files have no numstat entry; binary files report "-\t-" and are
            # skipped (counts stay absent). Skipped entirely when status
            # itself overflowed the cap: the tree is too large for counts to
            # be complete anyway.
            counts: dict[str, tuple[int, int]] = {}
            if not status_truncated:
                base_rev = _git_diff_base(worktree, _env, _budget())
                ns_rc, ns_raw, ns_truncated = _run_git_capped_bytes(
                    [
                        *_hardened_git(),
                        "diff",
                        "--numstat",
                        "-z",
                        "--no-textconv",
                        "--no-ext-diff",
                        "--no-renames",
                        # Same reason as the status probe: never diff a
                        # submodule's content under its own config.
                        "--ignore-submodules=dirty",
                        base_rev,
                        *(["--", *pathspecs] if pathspecs else []),
                    ],
                    cwd=worktree,
                    env=_env,
                    timeout=_budget(),
                    max_bytes=_GIT_CHANGES_MAX_OUTPUT_BYTES,
                )
                # A capped or failed numstat means SOME tracked rows will carry
                # no +/- counts. Silently omitting them while the response
                # claims a complete scan misreports the tree, so the result is
                # marked partial (see the truncated flag below).
                if ns_rc != 0 or ns_truncated:
                    counts_incomplete = True
                if ns_rc == 0:
                    entries = ns_raw.split(b"\0")
                    if ns_truncated and entries:
                        # A byte-capped read can cut mid-entry — drop the tail.
                        entries = entries[:-1]
                    for ns_raw_entry in entries:
                        try:
                            ns_entry = ns_raw_entry.decode("utf-8")
                        except UnicodeDecodeError:
                            undecodable = True
                            continue
                        # maxsplit=2: the PATH may itself contain tabs (legal on
                        # POSIX), and an unbounded split would discard those
                        # rows' counts entirely.
                        parts = ns_entry.split("\t", 2)
                        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                            continue
                        counts[parts[2]] = (int(parts[0]), int(parts[1]))
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            PermissionError,
            OSError,
            UnicodeDecodeError,
            ValueError,
        ) as exc:
            # A timeout (including one clipped by the aggregate budget), a
            # filesystem error (permission race, vanished repo), metadata
            # isolation failure, or decode failure: report the scan as
            # incomplete rather than presenting "no repo" or "clean".
            if isinstance(exc, PermissionError):
                out["repo"] = None
                out["filters_unsafe"] = str(exc) or _ATTRS_UNSAFE_SENSITIVE_GITDIR
                return out
            scan_failed = True
            status_raw, status_truncated, counts, branch, worktree = b"", True, {}, "", root
        finally:
            if isolated is not None:
                isolated.close()
        if scan_failed:
            out["repo"] = {
                "root": worktree,
                "name": os.path.basename(worktree) or worktree,
                "branch": branch,
                "files": [],
                "truncated": True,
            }
            out["truncated"] = True
            return out

        files: list[dict] = []
        files_capped = False
        seen_index: dict[str, int] = {}
        status_entries = status_raw.split(b"\0")
        if status_truncated and status_entries:
            # A byte-capped read can cut mid-entry — drop the fragment.
            status_entries = status_entries[:-1]
        for status_raw_entry in status_entries:
            # Entry shape: "XY <path>" — two status columns, one space, then
            # the verbatim path.
            if len(status_raw_entry) < 4:
                continue
            try:
                status_entry = status_raw_entry.decode("utf-8")
            except UnicodeDecodeError:
                # Non-UTF-8 name (possible on Linux; the OS rejects these
                # outright on macOS/Windows). Dropping the row keeps every
                # OTHER row correct, and the scan is marked partial so the
                # omission is visible rather than silent.
                undecodable = True
                continue
            code, rel = status_entry[:2], status_entry[3:]
            if not rel:
                continue
            # The payload keeps the LEXICAL repo path — for a changed symlink,
            # resolving it would point diff/open actions at the TARGET instead
            # of the changed entry. realpath is used only for the
            # sensitive-location check, so a repo symlinking into e.g. ~/.ssh
            # still never leaks.
            lexical_path = os.path.normpath(os.path.join(worktree, rel))
            try:
                if is_sensitive_path(os.path.realpath(lexical_path)):
                    continue
            except OSError:
                continue
            # Coalesce duplicate porcelain entries BEFORE the cap: a staged
            # deletion whose path was RECREATED reports both "D  path" and
            # "?? path" (two rows for one path would collide as React keys and
            # race the same diff fetch). Counting the pair twice also made a
            # repo with exactly MAX_FILES unique changes look truncated. The
            # pair is a REPLACEMENT, not a deletion: the file exists in the
            # worktree, so report it as modified. /api/file-diff detects the
            # same pair and synthesizes a base->current patch.
            if rel in seen_index:
                prior = files[seen_index[rel]]
                statuses = {prior["status"], _git_status_label(code)}
                if statuses == {"deleted", "untracked"}:
                    prior["status"] = "modified"
                    # The numstat counts belong to the DELETION (every line
                    # removed); the untracked replacement has no numstat entry
                    # at all. Keeping them would advertise a deletion's totals
                    # on a row labelled modified.
                    prior.pop("additions", None)
                    prior.pop("deletions", None)
                continue
            if len(files) >= GIT_CHANGES_MAX_FILES:
                # A real NEW path past the cap — the list is partial.
                files_capped = True
                break
            entry: dict = {
                "path": lexical_path,
                "rel": rel,
                "status": _git_status_label(code),
                "staged": code[0] not in (" ", "?"),
            }
            # A modified submodule (gitlink) reports a DIRECTORY path —
            # file-read/open actions would fail on it. islink guard: a symlink
            # to a directory is still an openable link entry.
            try:
                if os.path.islink(lexical_path):
                    entry["kind"] = "symlink"
                elif os.path.isdir(lexical_path):
                    entry["kind"] = "dir"
                elif _is_multilink_regular(lexical_path):
                    # Multi-link inode: /api/file-diff refuses to serve its
                    # content (a link can point at a protected file while the
                    # queried path looks benign). Flag the row so the UI does
                    # not offer a diff or editor action that can only refuse.
                    entry["kind"] = "hardlink"
            except OSError:
                pass
            # +/- line counts from the repo-wide numstat. Absent for untracked
            # and binary files.
            if rel in counts:
                entry["additions"], entry["deletions"] = counts[rel]
            seen_index[rel] = len(files)
            files.append(entry)
        repo_payload: dict = {
            "root": worktree,
            "name": os.path.basename(worktree) or worktree,
            "branch": branch,
            "files": files,
        }
        # Completeness signal: the file list was cut by the byte cap or the
        # per-repo file cap. Lets the UI qualify the list instead of
        # presenting a partial scan as authoritative.
        if status_truncated or files_capped:
            repo_payload["truncated"] = True
        out["repo"] = repo_payload
        # Top-level completeness: a row was dropped for an undecodable
        # filename, the +/- counts could not be read in full, or the scan could
        # not finish — individual files or their totals may be missing, so a
        # clean or empty result must not be presented as "working tree clean".
        if (
            status_truncated
            or files_capped
            or undecodable
            or counts_incomplete
            or hardlinks_excluded
        ):
            out["truncated"] = True
        return out

    result = await asyncio.to_thread(_run)
    if result.pop("_bad_request", False):
        _sel().log_api_access(
            caller=caller,
            operation="git_changes",
            outcome="denied",
            resources=raw_dir[:256],
            error="malformed path",
        )
        return web.json_response({"error": "Malformed path"}, status=400)
    if result.pop("_denied", False):
        _sel().log_api_access(
            caller=caller,
            operation="git_changes",
            outcome="denied",
            resources=raw_dir,
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied"}, status=403)
    if result.pop("_not_found", False):
        _sel().log_api_access(
            caller=caller,
            operation="git_changes",
            outcome="allowed",
            resources=f"dir={result.get('dir', raw_dir)}",
            error="not_found",
        )
        return web.json_response(result)
    # EXACTLY ONE outcome per request: a repo refused for attribute isolation
    # (or executable/alternate containment) is a security REFUSAL, not a
    # partially-allowed read. Emitting `allowed` and then `denied` for the same
    # call left a self-contradictory SEL trail that no consumer can resolve.
    if result.get("filters_unsafe"):
        _sel().log_api_access(
            caller=caller,
            operation="git_changes",
            outcome="denied",
            resources=f"dir={result.get('dir', raw_dir)}",
            error=str(result["filters_unsafe"]),
        )
    else:
        _sel().log_api_access(
            caller=caller,
            operation="git_changes",
            outcome="allowed",
            resources=f"dir={result.get('dir', raw_dir)} repo={bool(result.get('repo'))}",
        )
    return web.json_response(result)
