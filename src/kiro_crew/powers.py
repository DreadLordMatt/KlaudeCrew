"""Powers — installable capability bundles (POWER.md + optional mcp.json + steering).

A *Power* is a directory bundle packaging MCP tools and/or workflow guidance into
one on-demand capability (Kiro IDE 0.7 concept). This module owns three things:

1. :func:`parse_power_md` — parse + validate a ``POWER.md`` (YAML frontmatter +
   markdown body) into a :class:`PowerMeta`.
2. :class:`PowersStore` — the on-disk store over ``powers_dir()``
   (``<data home>/powers``), tracking per-Power ``source``/``installedAt``
   provenance in ``powers_dir()/installed.json``.
3. The installer (:meth:`PowersStore.install_from_dir`) which materializes a
   fetched bundle onto **existing** KiroCrew primitives — it builds NO parallel
   runtime.

Scope: install is inert
-----------------------
This module gets Powers onto disk and back off again. It does NOT activate them.
An installed bundle is unreachable by construction:

* No MCP server is registered. The bundle's ``mcp.json`` is never parsed for
  specs — only tested for presence, to report ``kind`` — so a declared command
  cannot reach :func:`kiro_crew.dashboard.handlers.mcp._set_kirocrew_entry` or
  any other execution path from here.
* No skill is materialized. ``POWER.md`` and ``steering/*.md`` stay inside
  ``powers_dir()``, which nothing else reads: ``skills.py``, ``context.py`` and
  ``agent.py`` contain no reference to it. Third-party markdown therefore cannot
  enter agent context.

Consequently there is no trust state to manage here, and ``installed.json``
deliberately carries none: a ``trusted`` flag that gates nothing would be a
security claim the code does not make. Activation — trust grant, enable/disable,
namespaced MCP registration via the single existing write path, and the
``skills_dir()/powers/<name>/SKILL.md`` docs wiring — lands as a separate change
that also has to rebuild the rendered agent config, since ``includeMcpJson`` is
false and sessions read ``~/.kiro/agents/kirocrew.json`` rather than
``<data home>/mcp.json``.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml  # type: ignore[import-untyped]

from kiro_crew import platform_compat
from kiro_crew.config.paths import powers_dir
from kiro_crew.executors import maintenance_executor
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# ── Constants / limits ──────────────────────────────────────────────────

POWER_MD_NAME = "POWER.md"
MCP_JSON_NAME = "mcp.json"
STEERING_DIR_NAME = "steering"
INSTALLED_JSON_NAME = "installed.json"


# Reject a POWER.md larger than this (defends the parser against a hostile or
# runaway document).  256 KiB.
MAX_POWER_MD_BYTES = 256 * 1024
# `mcp.json` is a server map, not a document. The whole-bundle budget would bound
# it at 8 MiB, which is orders of magnitude more than any real spec and still large
# enough to be worth refusing before parsing.
MAX_MCP_JSON_BYTES = 256 * 1024

# Install-time bounds over the whole fetched tree (defence-in-depth; the fetch
# layer bounds the download, this bounds what we materialize).
MAX_INSTALL_FILES = 256
MAX_INSTALL_BYTES = 8 * 1024 * 1024  # 8 MiB

# Upstream recognises EXACTLY these five frontmatter fields; unknown keys are
# tolerated (ignored). name/displayName/description are required.
_REQUIRED_FIELDS = ("name", "displayName", "description")

# Power name: lowercase kebab-case, 1–64 chars, no leading/trailing hyphen.
# Doubles as the filesystem-join safety gate (no traversal, no separators).
_POWER_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

# Reserved device basenames on Windows. Creating a directory with one of these
# names fails there regardless of extension, so they are refused on all
# platforms to keep a Power installable everywhere it might be synced.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class PowerSourceConflict(ValueError):
    """An install would replace a bundle that came from somewhere else.

    Powers are keyed by the `name` in their `POWER.md`, and nothing reserves that
    name globally: two registry entries — a monorepo directory and an independent
    author's repo — can declare the same one. Installing the second silently
    replaced the first bundle AND its provenance record, so the store then
    described a Power the user never chose, from a source they never picked.

    A reinstall or upgrade from the SAME source is not a conflict and stays
    allowed, which is what keeps this from breaking the ordinary update path.
    """


class PowerFormatError(ValueError):
    """Raised when a POWER.md is malformed, oversized, or fails validation."""


@dataclass
class PowerMeta:
    """Parsed ``POWER.md``: validated frontmatter + markdown body."""

    name: str
    displayName: str
    description: str
    keywords: list[str] = field(default_factory=list)
    author: str | None = None
    body: str = ""


# ── Frontmatter parsing ─────────────────────────────────────────────────


def _parse_inline_list(val: str) -> list[str]:
    """Parse an inline list literal like ``["a", "b"]`` or ``[a, b]``."""
    try:
        parsed = json.loads(val.replace("'", '"'))
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except (ValueError, json.JSONDecodeError):
        pass
    inner = val.strip()
    if inner.startswith("["):
        inner = inner[1:]
    if inner.endswith("]"):
        inner = inner[:-1]
    return [p.strip().strip("\"'") for p in inner.split(",") if p.strip()]


class _NoAliasSafeLoader(yaml.SafeLoader):
    """A ``SafeLoader`` that refuses YAML aliases.

    ``safe_load`` blocks arbitrary object construction but still expands aliases,
    so a few hundred bytes of nested `&a`/`*a` references ("billion laughs") can
    expand to gigabytes and take the gateway down. ``POWER.md`` is third-party
    content fetched from an arbitrary repository, and the 256 KiB input cap does
    not bound the EXPANDED size — only the source text. Aliases have no
    legitimate use in a five-field frontmatter block, so they are refused
    outright rather than depth-limited.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            raise PowerFormatError("YAML aliases are not allowed in POWER.md frontmatter")
        return super().compose_node(parent, index)


def _parse_frontmatter_block(block: str) -> dict[str, Any]:
    """Parse the YAML frontmatter body into a ``{key: str | list[str]}`` dict.

    Uses ``yaml.safe_load`` rather than a hand-rolled line parser. The bundle
    format documents its frontmatter as YAML, and the previous parser only
    handled the subset it happened to implement: a valid folded or literal block
    scalar (``description: >-`` followed by indented text) was read as the
    literal string ``">-"`` and its body silently dropped, so a Power installed
    with visibly wrong metadata. Parsing the documented format means valid input
    is accepted and the caller's field validation still decides what is kept.

    ``safe_load`` cannot construct arbitrary Python objects, so third-party
    frontmatter cannot execute code. Anything that is not a mapping is refused
    rather than coerced.
    """
    try:
        loaded = yaml.load(block, Loader=_NoAliasSafeLoader)
    except PowerFormatError:
        raise
    except yaml.YAMLError as exc:
        raise PowerFormatError(f"POWER.md frontmatter is not valid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise PowerFormatError("POWER.md frontmatter must be a mapping of fields")
    # Keys are normalised to str; values are left as parsed (scalars, lists) and
    # validated by the caller.
    return {str(k): v for k, v in loaded.items()}


def _coerce_keywords(value: Any) -> list[str]:
    """Coerce a frontmatter ``keywords`` value into ``list[str]``."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [str(value).strip()]


def parse_power_md(text: str) -> PowerMeta:
    """Parse and validate a ``POWER.md`` document.

    Raises :class:`PowerFormatError` on: oversize input, missing/empty
    frontmatter, any missing required field (``name``/``displayName``/
    ``description``), or an invalid ``name``.  ``keywords`` is coerced to
    ``list[str]``; unknown keys are tolerated (ignored).
    """
    if len(text.encode("utf-8")) > MAX_POWER_MD_BYTES:
        raise PowerFormatError(
            f"POWER.md exceeds the {MAX_POWER_MD_BYTES // 1024} KiB size cap"
        )
    # Normalize CRLF/CR line endings up front: a Windows-authored POWER.md uses
    # ``\r\n`` and the frontmatter regex + the block-list scanner below both
    # assume ``\n``, so without this a valid CRLF bundle is rejected outright
    # and any parsed scalar would keep a trailing ``\r``.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---"):
        raise PowerFormatError("POWER.md must begin with a '---' YAML frontmatter block")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        raise PowerFormatError("POWER.md frontmatter block is not properly terminated with '---'")
    fm_block, body = m.group(1), m.group(2)
    data = _parse_frontmatter_block(fm_block)

    for fieldname in _REQUIRED_FIELDS:
        v = data.get(fieldname)
        if not isinstance(v, str) or not v.strip():
            raise PowerFormatError(f"POWER.md is missing required field: {fieldname!r}")

    name = str(data["name"]).strip()
    if not _POWER_NAME_RE.match(name):
        raise PowerFormatError(
            f"invalid power name {name!r}: must be lowercase kebab-case, 1–64 chars, "
            "matching ^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
        )

    author_raw = data.get("author")
    author = str(author_raw).strip() if isinstance(author_raw, str) and author_raw.strip() else None

    return PowerMeta(
        name=name,
        displayName=str(data["displayName"]).strip(),
        description=str(data["description"]).strip(),
        keywords=_coerce_keywords(data.get("keywords")),
        author=author,
        body=body.strip(),
    )


# ── Name / tree safety ──────────────────────────────────────────────────


def is_safe_power_name(name: str) -> bool:
    """Return True if ``name`` is a valid power name safe to join onto a path."""
    if not name or ".." in name or "/" in name or "\\" in name:
        return False
    if not _POWER_NAME_RE.match(name):
        return False
    # Windows reserved device names are rejected on EVERY platform, not just
    # Windows: a bundle named `con` or `aux` passes the character regex, then
    # fails deep inside the directory swap and surfaces as an opaque HTTP 500.
    # Rejecting them up front turns that into a clear 400, and keeps a bundle
    # installed on Linux from being un-syncable to a Windows host.
    return name.upper() not in _WINDOWS_RESERVED_NAMES


def _assert_safe_tree(src: Path) -> None:
    """Reject symlinks in the CONTRACT files and enforce count/byte caps.

    Scoped to what an install actually copies — ``POWER.md``, ``mcp.json`` and
    ``steering/*.md`` — rather than walking the whole source tree. Walking
    everything rejected perfectly valid bundles: a repository that merely
    contains a large binary or a symlink somewhere unrelated failed the caps even
    though none of those files are ever read or copied. Budgeting only the
    contract files makes the limits describe the install, not the neighbourhood.

    Raises :class:`PowerFormatError` on the first violation.
    """
    total_files = 0
    total_bytes = 0

    def _check(path: Path, label: str) -> None:
        nonlocal total_files, total_bytes
        if path.is_symlink():
            raise PowerFormatError(f"symlink not allowed in power bundle: {label!r}")
        if not path.is_file():
            return
        total_files += 1
        if total_files > MAX_INSTALL_FILES:
            raise PowerFormatError(f"power bundle exceeds the {MAX_INSTALL_FILES}-file cap")
        try:
            total_bytes += path.stat().st_size
        except OSError:
            return
        if total_bytes > MAX_INSTALL_BYTES:
            raise PowerFormatError(
                f"power bundle exceeds the {MAX_INSTALL_BYTES // (1024 * 1024)} MiB size cap"
            )

    for fname in (POWER_MD_NAME, MCP_JSON_NAME):
        _check(src / fname, fname)
    steering = src / STEERING_DIR_NAME
    if steering.is_symlink():
        raise PowerFormatError(f"symlinked directory not allowed: {STEERING_DIR_NAME!r}")
    if steering.is_dir():
        for md in sorted(steering.glob("*.md")):
            _check(md, f"{STEERING_DIR_NAME}/{md.name}")


# ── Bundle file helpers ─────────────────────────────────────────────────


def _declares_mcp_servers(power_path: Path) -> bool:
    """Return True when the bundle's ``mcp.json`` declares at least one server.

    A PRESENCE test, deliberately not a spec reader. This PR installs bundles
    and never activates them, so no code path needs a server's command, args or
    env — and not reading them means a malformed or hostile spec cannot reach
    any consumer. The activation PR replaces this with a validating parser.
    """
    path = power_path / MCP_JSON_NAME
    if not path.is_file():
        return False
    try:
        # Capped for the same reason `POWER.md` is: the installed bundle stays
        # editable after the copy, so its size is not fixed at install time, and
        # this runs on `GET /api/powers` for every installed Power. An oversized
        # file is treated as "declares nothing" rather than raising — this
        # function answers a yes/no question used to label a card, and the
        # install-time validation is what refuses malformed bundles.
        with open(path, "rb") as fh:
            raw = fh.read(MAX_MCP_JSON_BYTES + 1)
        if len(raw) > MAX_MCP_JSON_BYTES:
            logger.warning(
                "powers: %s exceeds the %d KiB cap; treating as no servers",
                path.name,
                MAX_MCP_JSON_BYTES // 1024,
            )
            return False
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and bool(servers)


def _copy_power_files(src: Path, dest: Path, *, dest_fd: int | None = None) -> None:
    """Copy ONLY the documented Power contract files from *src* into *dest*.

    A Power is defined as ``POWER.md`` plus an optional ``mcp.json`` and an
    optional ``steering/*.md`` directory. Copying exactly those — instead of
    recursively copying whatever else happens to live alongside them — means an
    install can never relocate unrelated files (credentials, keys, governance
    documents) into the agent-readable powers directory, even when the caller
    points the ``folder`` install kind at a broad ancestor directory that
    happens to contain a valid ``POWER.md``.

    Symlinks are refused rather than followed, and each file is size-capped.

    *dest_fd* is a descriptor for *dest* itself, pinned by the caller with
    ``O_NOFOLLOW``. When given, every file is created relative to it instead of by
    path, so the destination cannot be redirected after it was created. It is
    ``None`` on platforms without ``dir_fd`` support, where the path form is used.
    """
    # One budget for the whole bundle, enforced WHILE streaming, so a source
    # file that grows mid-copy cannot push the install past the cap.
    budget = [MAX_INSTALL_BYTES]
    # Pin the SOURCE directory too. The source is caller-owned and mutable — the
    # same premise that forces the staged POWER.md re-parse — so resolving it by
    # path on every file leaves each resolution independently swappable.
    src_fd = _open_dir_nofollow(src)
    try:
        for fname in (POWER_MD_NAME, MCP_JSON_NAME):
            srcf = src / fname
            if fname == POWER_MD_NAME or _entry_exists_at(src_fd, fname, srcf):
                _copy_regular_file(
                    srcf,
                    dest / fname,
                    required=(fname == POWER_MD_NAME),
                    budget=budget,
                    dest_fd=dest_fd,
                    src_fd=src_fd,
                )
        _copy_steering(src, dest, budget=budget, dest_fd=dest_fd, src_fd=src_fd)
    finally:
        if src_fd is not None:
            with contextlib.suppress(BaseException):
                os.close(src_fd)


def _open_dir_nofollow(path: Path) -> int | None:
    """Open *path* as a directory without following a final symlink.

    Returns None where the platform lacks ``dir_fd`` support, in which case
    callers fall back to path resolution (Windows, where creating a symlink needs
    elevation).
    """
    if not _SUPPORTS_DIR_FD:
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise PowerFormatError(
                f"power bundle source is not a real directory: {path}"
            ) from exc
        raise


def _entry_exists_at(dir_fd: int | None, name: str, fallback: Path) -> bool:
    """True when *name* exists directly under *dir_fd* (or *fallback* by path)."""
    if dir_fd is None:
        return fallback.exists()
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


def _copy_steering(
    src: Path,
    dest: Path,
    *,
    budget: list[int],
    dest_fd: int | None,
    src_fd: int | None,
) -> None:
    """Copy ``steering/*.md``, with the source directory pinned by descriptor.

    Opening ``steering/`` once with ``O_NOFOLLOW|O_DIRECTORY`` and enumerating
    through that handle is what closes the parent-directory race: a swap of the
    directory after this open is invisible to the copy, whereas a
    ``is_dir()``/``is_symlink()`` check followed by ``glob()`` re-resolves the name
    and can be redirected in between — and the files it then finds are ordinary
    regular files, so per-file ``O_NOFOLLOW`` does not object.
    """
    steering_src = src / STEERING_DIR_NAME
    steering_fd: int | None = None
    if src_fd is not None:
        if not _entry_exists_at(src_fd, STEERING_DIR_NAME, steering_src):
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            steering_fd = os.open(STEERING_DIR_NAME, flags, dir_fd=src_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                # A symlinked or non-directory `steering/` is refused outright
                # rather than skipped: skipping would silently install a partial
                # bundle from a source that was tampered with mid-install.
                raise PowerFormatError(
                    "steering/ is a symlink or not a directory"
                ) from exc
            raise
    else:
        # No descriptor support, so this path check is the only guard -- and
        # `is_symlink()` is False for an NTFS directory junction, which the
        # filesystem follows exactly like a symlink. A `steering` junction
        # pointing outside the selected source would have had its Markdown
        # copied into the agent-readable Powers tree. Same class as the store
        # root's own guard, on the branch that actually needs it: the POSIX
        # branch above pins the directory with `O_NOFOLLOW` instead.
        if not steering_src.exists():
            return
        _assert_not_reparse_point(steering_src)
        if not steering_src.is_dir():
            return
    steering_dest = dest / STEERING_DIR_NAME
    steering_dest_fd: int | None = None
    if dest_fd is None:
        steering_dest.mkdir(parents=True, exist_ok=True)
    else:
        os.mkdir(STEERING_DIR_NAME, 0o700, dir_fd=dest_fd)
        steering_dest_fd = _open_dir_at(dest_fd, STEERING_DIR_NAME, steering_dest)
    copied = 0
    try:
        if steering_fd is None:
            names = sorted(md.name for md in steering_src.glob("*.md"))
        else:
            names = sorted(n for n in os.listdir(steering_fd) if n.endswith(".md"))
        for name in names:
            copied += 1
            if copied > MAX_INSTALL_FILES:
                raise PowerFormatError(
                    f"steering/ exceeds the {MAX_INSTALL_FILES}-file cap"
                )
            _copy_regular_file(
                steering_src / name,
                steering_dest / name,
                required=True,
                budget=budget,
                dest_fd=steering_dest_fd,
                src_fd=steering_fd,
            )
    finally:
        for fd in (steering_fd, steering_dest_fd):
            if fd is not None:
                with contextlib.suppress(BaseException):
                    os.close(fd)


def _copy_regular_file(
    srcf: Path,
    destf: Path,
    *,
    required: bool,
    budget: list[int] | None = None,
    dest_fd: int | None = None,
    src_fd: int | None = None,
) -> None:
    """Copy one regular file, refusing symlinks and enforcing the size caps.

    TOCTOU-safe by construction. The previous form checked ``is_symlink()`` and
    ``stat()`` on the PATH and then handed the path to ``shutil.copyfile``, which
    reopens it — so a caller-owned ``folder`` source could swap the file for a
    symlink to a credential store, or grow it past the cap, in the window
    between the check and the copy. Here the file is opened exactly ONCE with
    ``O_NOFOLLOW`` and every decision is made from that descriptor: the kernel
    refuses the open if the final component is a symlink, ``fstat`` describes the
    object actually opened, and the bytes are streamed from the same descriptor,
    so no later swap can be observed.

    *budget* is a single-element list carrying the remaining CUMULATIVE byte
    allowance for the whole bundle; enforcing it while streaming means a file
    that grows during the copy cannot exceed the cap either.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        # Opened relative to the pinned source directory when the caller has one,
        # so no parent component is re-resolved and a swap of the containing
        # directory cannot redirect this read.
        if src_fd is None:
            fd = os.open(srcf, flags)
        else:
            fd = os.open(srcf.name, flags, dir_fd=src_fd)
    except OSError as exc:
        # ELOOP/EMLINK is the kernel refusing a symlinked final component.
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise PowerFormatError(
                f"symlink not allowed in power bundle: {srcf.name!r}"
            ) from exc
        if required:
            raise PowerFormatError(
                f"missing required bundle file: {srcf.name!r}"
            ) from exc
        return
    try:
        st = os.fstat(fd)
        # Portable symlink refusal. `O_NOFOLLOW` does not exist on Windows, where
        # the getattr above degrades to 0 — so relying on it alone left the TOCTOU
        # protection absent on exactly one platform while every test still passed
        # on the others. Comparing what we OPENED (fstat) against what the PATH
        # names right now (lstat, which does not follow) catches a symlinked or
        # swapped final component everywhere: a link's lstat describes the link
        # itself, so the identity cannot match the opened target.
        try:
            if src_fd is None:
                lst = os.lstat(srcf)
            else:
                lst = os.stat(srcf.name, dir_fd=src_fd, follow_symlinks=False)
        except OSError as exc:
            raise PowerFormatError(
                f"bundle file vanished during install: {srcf.name!r}"
            ) from exc
        if stat.S_ISLNK(lst.st_mode) or (lst.st_dev, lst.st_ino) != (st.st_dev, st.st_ino):
            raise PowerFormatError(f"symlink not allowed in power bundle: {srcf.name!r}")
        if not stat.S_ISREG(st.st_mode):
            raise PowerFormatError(f"not a regular file in power bundle: {srcf.name!r}")
        if st.st_nlink > 1:
            # A hardlink defeats the sensitive-path refusal, which is path-based:
            # `steering/guide.md` can share an inode with a protected file, and the
            # copy would then place those bytes in the Powers tree, where an agent
            # can read them back. The symlink check above does not catch it — a
            # hardlink IS the file, so lstat and fstat agree on identity and there
            # is no link to detect. Rejecting extra links is the same rule
            # `hooks.safe_read_file_bytes_nolink` applies on the read side, and the
            # same reason the state write uses `O_EXCL`.
            raise PowerFormatError(
                f"hardlinked file not allowed in power bundle: {srcf.name!r}"
            )
        if st.st_size > MAX_INSTALL_BYTES:
            raise PowerFormatError(f"bundle file exceeds the size cap: {srcf.name!r}")
        remaining = MAX_INSTALL_BYTES if budget is None else budget[0]
        written = 0
        # Anchored to the caller's pinned directory descriptor when available, so
        # the write cannot be redirected by a swap of any parent component.
        # `O_EXCL` because the staging tree was just created empty: an existing
        # name there is not ours.
        if dest_fd is None:
            out_cm = open(destf, "wb")
        else:
            out_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0)
            )
            out_cm = os.fdopen(
                os.open(destf.name, out_flags, 0o600, dir_fd=dest_fd), "wb"
            )
        with out_cm as out:
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > remaining:
                    raise PowerFormatError(
                        f"power bundle exceeds the "
                        f"{MAX_INSTALL_BYTES // (1024 * 1024)} MiB size cap"
                    )
                out.write(chunk)
        if budget is not None:
            budget[0] -= written
    finally:
        os.close(fd)


def resolve_install_source(src: Path | str) -> Path:
    """Normalize and vet a Power install-source directory.

    The ``folder`` install kind takes an API-supplied path, so the source is
    untrusted input that reaches filesystem traversal and a recursive copy.
    Guards, in order: resolve symlinks/``..`` to an absolute real path, refuse
    protected locations via the shared :func:`is_sensitive_path` guard (else a
    credential store holding a valid ``POWER.md`` could be copied into the
    non-sensitive powers directory, where agent file tools could then read it),
    and require a real directory.
    """
    try:
        resolved = Path(src).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PowerFormatError(f"install source is not readable: {src}") from exc
    if is_sensitive_path(str(resolved)):
        # A blocked install attempt is a security decision and must be auditable:
        # the SEL event downstream only fires on a SUCCESSFUL install, so without
        # this a repeated probe of credential paths would leave no trace at all.
        sel().log_api_access(
            caller="dashboard",
            operation="power_install",
            outcome="denied",
            resources=str(resolved),
        )
        raise PowerFormatError("install source is a protected path and cannot be installed from")
    if not resolved.is_dir():
        raise PowerFormatError(f"install source is not a directory: {resolved}")
    return resolved


# ── Store ───────────────────────────────────────────────────────────────

_POWERS_LOCK: asyncio.Lock | None = None


def _get_powers_lock() -> asyncio.Lock:
    """Return the process-wide lock serializing Powers install/remove.

    Install and remove both stage through fixed scratch paths and then
    read-modify-write ``installed.json``, so two concurrent operations on the
    same Power could interleave a swap with another's rollback. Created lazily
    because a module-import-time ``asyncio.Lock()`` binds to whatever loop
    happens to be current at import.

    This is deliberately NOT the MCP file lock: this module no longer writes MCP
    config, so borrowing that lock would serialize Powers installs against every
    unrelated dashboard MCP write for no benefit. The activation change, which
    does write MCP config, takes the MCP lock as well.
    """
    global _POWERS_LOCK
    if _POWERS_LOCK is None:
        _POWERS_LOCK = asyncio.Lock()
    return _POWERS_LOCK


POWERS_LOCK_NAME = ".lock"

# POSIX can anchor filesystem operations to an already-open directory descriptor,
# which is the only way to make "check the root, then mutate under it" atomic: the
# descriptor keeps referring to the directory we validated even if the PATH is
# swapped for a symlink a microsecond later. Windows has no dir_fd support, and
# there the residual exposure is much narrower — creating a symlink requires
# elevation or developer mode — so it keeps the path-based check.
_SUPPORTS_DIR_FD = (
    os.rename in getattr(os, "supports_dir_fd", set())
    and os.unlink in getattr(os, "supports_dir_fd", set())
)


def _assert_not_reparse_point(path: Path) -> None:
    """Refuse a symlink OR a Windows directory junction at *path*.

    ``Path.is_symlink()`` returns False for an NTFS directory junction, which the
    filesystem follows exactly like a symlink. The platform that depends on this
    check is precisely the one where it was incomplete: POSIX pins the root with
    ``O_NOFOLLOW`` and does not need it, while Windows has no ``dir_fd`` support
    and the path check is the only guard. A junctioned root would otherwise let
    ``_rmtree_at`` delete a matching directory outside the store.

    Detected through ``st_file_attributes``' reparse-point bit, which is present
    on Windows stat results; on other platforms that attribute is absent and only
    the symlink test applies.
    """
    if path.is_symlink():
        raise PowerFormatError(f"powers root is a symlink and cannot be used: {path}")
    try:
        st = os.lstat(path)
    except OSError:
        return
    attrs = getattr(st, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse and attrs & reparse:
        raise PowerFormatError(
            f"powers root is a reparse point (junction) and cannot be used: {path}"
        )


@contextlib.contextmanager
def _root_lock(root: Path) -> Iterator[tuple[int, int | None]]:
    """Blocking: hold the cross-process lock AND a validated root handle.

    Yields ``(lock_fd, root_fd)``. ``root_fd`` is an ``O_NOFOLLOW|O_DIRECTORY``
    handle to the powers root where the platform supports directory-relative
    operations, and ``None`` elsewhere — callers must tolerate both.

    Opening the root with ``O_NOFOLLOW`` both rejects a symlinked root at open
    time and pins the identity for every operation performed through the handle,
    so a concurrent swap after validation cannot redirect a later rename or
    delete. That is what the previous check-then-act sequence could not promise
    however tightly the two were placed.
    """
    root.mkdir(parents=True, exist_ok=True)
    _assert_not_reparse_point(root)
    lock_fd = os.open(root / POWERS_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    root_fd: int | None = None
    try:
        platform_compat.acquire_lock(lock_fd, exclusive=True)
        if _SUPPORTS_DIR_FD:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            root_fd = os.open(root, flags)
        yield lock_fd, root_fd
    finally:
        if root_fd is not None:
            with contextlib.suppress(BaseException):
                os.close(root_fd)
        with contextlib.suppress(BaseException):
            platform_compat.release_lock(lock_fd)
        os.close(lock_fd)


def _mkdir_at(root_fd: int | None, leaf: str, root: Path) -> None:
    """Create a directory directly under the root, via the handle when possible."""
    if root_fd is None:
        (root / leaf).mkdir(parents=True)
    else:
        os.mkdir(leaf, 0o700, dir_fd=root_fd)


def _open_dir_at(root_fd: int | None, leaf: str, root: Path) -> int | None:
    """Open a directory under the root ``O_NOFOLLOW``, or None when unsupported.

    Returned descriptors pin the directory's identity the same way ``_root_lock``
    pins the root, so writes into a staging tree cannot be redirected after it is
    created.
    """
    if root_fd is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(leaf, flags, dir_fd=root_fd)


# Bound on the fd-relative delete recursion. The contract tree is two levels
# (`steering/` is the only subdirectory), so anything deeper is either a bug or a
# hostile tree; recursing without a limit would risk the interpreter stack.
_RMTREE_MAX_DEPTH = 16


def _rmtree_fd(parent_fd: int, leaf: str, depth: int = 0) -> None:
    """Recursively delete *leaf* under *parent_fd*, never following a symlink.

    Written by hand rather than delegating to ``shutil.rmtree``: passing it a path
    reopens every component by name, so a root swapped between the containment
    check and the walk redirected the deletion outside the store. ``shutil`` grew
    a ``dir_fd`` parameter too recently to rely on across the versions CI runs.
    """
    if depth > _RMTREE_MAX_DEPTH:
        raise OSError(f"power tree exceeds the {_RMTREE_MAX_DEPTH}-level delete depth")
    try:
        st = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(leaf, dir_fd=parent_fd)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(leaf, flags, dir_fd=parent_fd)
    try:
        for entry in os.listdir(fd):
            _rmtree_fd(fd, entry, depth + 1)
    finally:
        os.close(fd)
    os.rmdir(leaf, dir_fd=parent_fd)


def _read_power_md(path: Path) -> str:
    """Read a `POWER.md` with a byte cap, refusing anything over it.

    `MAX_POWER_MD_BYTES` was enforced only while COPYING a bundle in, but the
    installed `POWER.md` stays editable afterwards and the install source is
    caller-owned to begin with — so both the pre-install parse and every later
    `load_power` read the whole file however large it had become. An oversized
    file then reached `read_text()` inside the MCP process, which is a memory
    exhaustion vector reachable by anything that can write into the bundle.

    Reads one byte past the cap so "at the limit" and "over the limit" are
    distinguishable, and refuses rather than truncating: a truncated `POWER.md`
    would either fail frontmatter parsing with a confusing error or, worse, parse
    as a different Power than the file describes.
    """
    with open(path, "rb") as fh:
        raw = fh.read(MAX_POWER_MD_BYTES + 1)
    if len(raw) > MAX_POWER_MD_BYTES:
        raise PowerFormatError(
            f"{POWER_MD_NAME} exceeds the {MAX_POWER_MD_BYTES // 1024} KiB cap: "
            f"{path.name!r}"
        )
    return raw.decode("utf-8", errors="replace")


def _read_text_at(root_fd: int | None, path: Path) -> str:
    """Read a file directly under the root, via the handle when possible.

    ``O_NOFOLLOW`` on the leaf as well, so a symlink planted at
    ``installed.json`` cannot redirect the read either.
    """
    if root_fd is None:
        return path.read_text(encoding="utf-8")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.name, flags, dir_fd=root_fd)
    with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as fh:
        return fh.read()


def _atomic_write_at(root_fd: int | None, path: Path, text: str) -> None:
    """Atomically replace *path* — anchored to the root handle when possible.

    ``installed.json`` is the store's state, so writing it by path was the last
    mutation a swapped root could redirect: the record describing what is
    installed would land outside the store (or overwrite a file there).
    """
    tmp = _stage_write_at(root_fd, path, text)
    _commit_staged_at(root_fd, tmp, path)


def _stage_write_at(root_fd: int | None, path: Path, text: str) -> str:
    """Write *text* to a fresh temp file beside *path* and return its leaf name.

    Splitting stage from commit exists so a caller can **allocate the space and
    flush the bytes before performing a destructive step**, then finish with a
    rename that needs no space. Uninstall uses this: the record write used to run
    after the bundle was deleted, so a full filesystem could destroy the bundle
    and then fail to record it.

    Unique name plus ``O_EXCL``, never a fixed name with ``O_TRUNC``:
    ``O_NOFOLLOW`` refuses a SYMLINK here but is silent about a HARDLINK, so a
    preplanted link would otherwise have been truncated and then filled with
    store state. The random suffix means the name cannot be pre-created at all.
    """
    tmp = f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    # Staging needs a temp file in the same directory and an atomic replace, not a
    # descriptor. So it runs on EVERY platform: the no-`dir_fd` branch loses
    # descriptor CONFINEMENT (unavoidable) but keeps the DURABILITY property, and
    # a documented "Windows destroys bytes before the record is durable" residual
    # is not something to keep when the fix costs one branch.
    if root_fd is None:
        fd = os.open(path.parent / tmp, flags, 0o600)
    else:
        fd = os.open(tmp, flags, 0o600, dir_fd=root_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(BaseException):
            _discard_staged_at(root_fd, tmp, path)
        raise
    return tmp


def _commit_staged_at(root_fd: int | None, tmp: str, path: Path) -> None:
    """Atomically move a staged file into place. Needs no additional space.

    ``os.replace`` on the path branch rather than ``os.rename``: the destination
    normally exists, and ``os.rename`` fails on Windows when it does.
    """
    if root_fd is None:
        os.replace(path.parent / tmp, path)
        return
    os.rename(tmp, path.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)


def _discard_staged_at(root_fd: int | None, tmp: str, path: Path) -> None:
    """Drop a staged file that will not be committed."""
    with contextlib.suppress(BaseException):
        if root_fd is None:
            (path.parent / tmp).unlink(missing_ok=True)
        else:
            os.unlink(tmp, dir_fd=root_fd)


def _exists_at(root_fd: int | None, leaf: str, root: Path) -> bool:
    """True if *leaf* exists directly under the root, via the handle when possible."""
    if root_fd is None:
        return (root / leaf).exists()
    try:
        os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


_REMOVING_PREFIX = ".removing-"


def _listdir_at(root_fd: int | None, root: Path) -> list[str]:
    """Enumerate the root's entries, through the handle when possible."""
    if root_fd is None:
        return [child.name for child in root.iterdir()]
    return os.listdir(root_fd)


def _rename_at(root_fd: int | None, src: str, dst: str, root: Path) -> None:
    """Rename within the root, anchored to the handle when possible."""
    if root_fd is None:
        os.replace(root / src, root / dst)
    else:
        os.rename(src, dst, src_dir_fd=root_fd, dst_dir_fd=root_fd)


def _rmtree_at(root_fd: int | None, target: Path, root: Path) -> None:
    """Recursively delete *target*, anchored to the handle when possible.

    The WHOLE walk is descriptor-relative where the platform supports it, not
    just the top-level entry: resolving only the first component and then handing
    the path to ``shutil.rmtree`` reopened every component by name, so a root
    swapped mid-walk redirected the deletion outside the store.
    """
    if root_fd is None:
        shutil.rmtree(target, ignore_errors=False)
        return
    _rmtree_fd(root_fd, target.name)


def _source_identity(source: Mapping[str, Any]) -> tuple[str, str]:
    """The (kind, ref) pair a record is compared on, normalised for comparison."""
    kind = str(source.get("kind", "")).strip().lower()
    ref = str(source.get("ref", "")).strip()
    # Refs are compared case-sensitively apart from a trailing slash: GitHub paths
    # are case-sensitive, so folding them would treat two distinct repos as one.
    return kind, ref.rstrip("/")


def _same_source(before: Mapping[str, Any], now: Mapping[str, Any]) -> bool:
    """True when an install would be a reinstall of the same thing."""
    return _source_identity(before) == _source_identity(now)


def _install_conflicts(before: Mapping[str, Any], now: Mapping[str, Any]) -> bool:
    """True when this install would replace a bundle the user did not choose.

    Narrower than "the source differs", deliberately. A local folder on both sides
    is the development loop — pointing the installer at a rebuilt or relocated
    directory — and the path there is incidental, not provenance: the user selected
    that directory explicitly and there is no third party to impersonate. Refusing
    it would break the folder-reinstall path for no security gain, which is what
    `test_failed_reinstall_preserves_existing_bundle` demonstrated.

    Every other combination involves at least one remote source, where the name is
    the only thing tying a bundle to an upstream and two unrelated repositories can
    both claim it. That includes folder -> registry and registry -> folder, since
    losing a recorded upstream to a local directory of the same name is the same
    loss of provenance in the other direction.
    """
    if _same_source(before, now):
        return False
    return not (_source_identity(before)[0] == "folder" == _source_identity(now)[0])


def _describe_source(source: Mapping[str, Any]) -> str:
    """A short, user-facing rendering of a source for the conflict message."""
    kind, ref = _source_identity(source)
    return f"{kind or 'unknown'}:{ref}" if ref else (kind or "unknown")


class PowersStore:
    """On-disk store for installed Powers over ``powers_dir()``.

    ``installed.json`` holds per-Power ``source``/``installedAt`` provenance;
    the Power's own ``POWER.md``/``mcp.json``/``steering/`` live in
    ``<powers_dir>/<name>/``.

    Installed Powers are INERT: this store copies bundle files to disk and
    records where they came from, and nothing more. It registers no MCP server
    and materializes no skill, so no installed bundle can execute or enter agent
    context. Activation state (``trusted``/``enabled``) is deliberately absent
    from the record — it arrives with the surface that can act on it.
    """

    def __init__(self, powers_path: Path | None = None):
        self._dir = powers_path or powers_dir()

    # ── installed.json ──

    def _installed_path(self) -> Path:
        return self._dir / INSTALLED_JSON_NAME

    def _load_installed(self, *, root_fd: int | None = None) -> dict[str, Any]:
        """Load the provenance map from ``installed.json``.

        A MISSING file is the only benign empty state (nothing installed yet). A
        corrupt, unreadable, or non-object file RAISES :class:`PowerFormatError`
        rather than being silently treated as empty: returning ``{}`` on a parse
        error would let the very next install overwrite a damaged
        ``installed.json``, permanently erasing the provenance record for every
        already-installed Power.

        *root_fd* is the transaction's pinned root handle. Inside a transaction the
        read MUST use it: anchoring the write while leaving the read lexical is not
        a partial fix but a worse one — a decoy root supplies foreign state, which
        the anchored write then commits over the real record, erasing provenance
        for every installed Power. Callers outside a transaction pass nothing.
        """
        path = self._installed_path()
        try:
            raw = _read_text_at(root_fd, path)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise PowerFormatError(f"installed.json is unreadable: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PowerFormatError(f"installed.json is corrupt: {exc}") from exc
        if not isinstance(data, dict):
            raise PowerFormatError("installed.json is not a JSON object")
        return data

    def _write_installed(
        self, data: dict[str, Any], *, root_fd: int | None = None
    ) -> None:
        """Persist the installed-Powers record.

        *root_fd* is the transaction's pinned root handle when called from inside
        one; the write is then anchored to it rather than resolving the root path
        again, which a concurrent symlink swap could redirect. Callers outside a
        transaction (read-repair paths) pass nothing and get the plain write.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_at(
            root_fd, self._installed_path(), json.dumps(data, indent=2, sort_keys=True)
        )

    def _drop_record(self, name: str, *, root_fd: int | None = None) -> bool:
        """Blocking: forget *name*. Returns True if a record was removed.

        *root_fd* threads the remove transaction's pinned root handle through to
        the state write, so dropping a record cannot be redirected by a root
        swapped mid-transaction.
        """
        installed = self._load_installed(root_fd=root_fd)
        if name not in installed:
            return False
        del installed[name]
        self._write_installed(installed, root_fd=root_fd)
        return True

    # ── read ──

    def list_powers(self) -> list[dict[str, Any]]:
        """Return the Power contract dict for every installed Power."""
        installed = self._load_installed()
        result: list[dict[str, Any]] = []
        for name in sorted(installed):
            record = self.load_power(name)
            if record is not None:
                result.append(record)
        return result

    def load_power(
        self, name: str, *, root_fd: int | None = None
    ) -> dict[str, Any] | None:
        """Return the Power contract dict for ``name`` (or ``None`` if absent).

        *root_fd* is the transaction's pinned root handle, passed when this runs
        **inside** a transaction so the provenance read is anchored like every
        other in-transaction read (see "Confinement comes from a pinned
        descriptor"). The bundle-file reads below stay path-based either way:
        they are the same read-only posture `list_powers` uses, and `_power_path`
        refuses a symlink at the Power's own root.
        """
        if not is_safe_power_name(name):
            return None
        installed = self._load_installed(root_fd=root_fd)
        record = installed.get(name)
        if not isinstance(record, dict):
            return None
        try:
            power_path = self._power_path(name)
        except PowerFormatError:
            return None
        power_md = power_path / POWER_MD_NAME
        if not power_md.is_file():
            return None
        try:
            meta = parse_power_md(_read_power_md(power_md))
        except (OSError, PowerFormatError):
            return None
        steering_files = self._steering_files(power_path)
        # A hand-edited or legacy record may carry a non-dict ``source`` (e.g. a
        # bare string). Coerce anything that is not a dict to an empty mapping so
        # one malformed record cannot crash ``.get(...)`` and take down the whole
        # ``list_powers`` listing.
        source = record.get("source")
        if not isinstance(source, dict):
            source = {}
        return {
            # The RECORD KEY, not `meta.name`. `POWER.md` lives inside the
            # installed bundle and stays editable, so reading the identity back
            # out of it let a bundle rename itself after install: `api_powers`
            # would report `bar` for the Power installed as `foo`, and the UI's
            # delete would then call `/api/powers/bar` and 404 — leaving `foo`
            # unremovable through the dashboard. The key is what every route and
            # the installed record agree on, so it is the identity.
            "name": name,
            "displayName": meta.displayName,
            "description": meta.description,
            "keywords": meta.keywords,
            "author": meta.author,
            # Declared kind only — descriptive, never operative. An installed
            # bundle is inert: nothing in this module reads its mcp.json specs or
            # materializes its docs, so `kind` tells the user what the Power
            # WOULD do once an activation surface exists, and does nothing else.
            "kind": "mcp" if _declares_mcp_servers(power_path) else "knowledge",
            "steeringFiles": steering_files,
            "source": {
                "kind": str(source.get("kind", "")),
                "ref": str(source.get("ref", "")),
            },
            "installedAt": str(record.get("installedAt", "")),
            "path": str(power_path),
        }

    @staticmethod
    def _steering_files(power_path: Path) -> list[str]:
        steering = power_path / STEERING_DIR_NAME
        if not steering.is_dir():
            return []
        return sorted(p.name for p in steering.glob("*.md") if p.is_file())

    # ── path confinement ──

    def _prune_absent_records(self, root_fd: int | None) -> None:
        """Drop records whose bundle is gone. Runs inside a transaction's lock.

        This is the counterpart to the remove ordering. Destroying the bytes
        before committing the record means an interruption in that window leaves a
        record with no bundle — reported as missing by ``load_power`` and hidden
        from ``list_powers``, but still present on disk. Reconciling it here makes
        that self-healing: the next install or uninstall repairs the store without
        anyone needing to retry the exact Power that was interrupted.

        Records with no bundle can also predate the current ordering, or come from
        a bundle deleted out from under the store by hand, so this stays useful
        independently of how ``remove_power`` sequences its steps.
        """
        try:
            installed = self._load_installed(root_fd=root_fd)
        except PowerFormatError:
            # A corrupt record file is not this method's business to rewrite; the
            # caller's own read will fail closed on it.
            return
        absent = [
            name
            for name in installed
            if is_safe_power_name(name)
            and not _exists_at(root_fd, name, self._dir)
            and not _exists_at(root_fd, f"{_REMOVING_PREFIX}{name}", self._dir)
            # A pending `.backup-*` means an interrupted install still has this
            # bundle in the rollback slot; the recovery step restores it. Pruning
            # the record here would strand the bytes it is about to restore — which
            # is exactly what happened to `test_orphaned_backup_is_recovered_on_
            # next_install` when this ran too early.
            and not _exists_at(root_fd, f".backup-{name}", self._dir)
        ]
        if not absent:
            return
        for name in absent:
            del installed[name]
        logger.info(
            "powers: pruned %d record(s) with no bundle on disk (%s)",
            len(absent),
            ", ".join(sorted(absent)),
        )
        self._write_installed(installed, root_fd=root_fd)

    def _sweep_orphaned_removals(self, root_fd: int | None) -> None:
        """Delete `.removing-<name>` trees that no record names any more.

        This is what makes committing the record before destroying the bundle safe.
        The objection to that ordering was that orphaned bytes are unreclaimable,
        because ``list_powers`` enumerates ``installed.json`` and a tree with no
        record is invisible to it. That is true of a bundle left at its own name —
        but not of one renamed aside first. `.removing-<name>` is derived from the
        record name and lives directly under the root, so it is enumerable on its
        own terms, and reclaiming it needs no record to point at it.

        A `.removing-*` whose name IS still recorded is left alone: that is an
        uninstall interrupted between the rename and the commit under the previous
        ordering, and ``remove_power`` finishes it deliberately.
        """
        try:
            installed = self._load_installed(root_fd=root_fd)
        except PowerFormatError:
            return
        try:
            entries = _listdir_at(root_fd, self._dir)
        except OSError:
            return
        for entry in entries:
            if not entry.startswith(_REMOVING_PREFIX):
                continue
            name = entry[len(_REMOVING_PREFIX):]
            if not is_safe_power_name(name) or name in installed:
                continue
            with contextlib.suppress(OSError):
                _rmtree_at(root_fd, self._dir / entry, self._dir)
            if _exists_at(root_fd, entry, self._dir):
                # Left for a later sweep. Not an error: the record is already gone,
                # so the Power is removed as far as every reader is concerned.
                logger.warning(
                    "powers: leftover %s could not be deleted yet", entry
                )
            else:
                logger.info("powers: reclaimed leftover %s", entry)

    def _reconcile_store(self, root_fd: int | None) -> None:
        """Repair both residuals a interrupted mutation can leave."""
        self._prune_absent_records(root_fd)
        self._sweep_orphaned_removals(root_fd)

    def _power_path(self, name: str) -> Path:
        """Return ``<powers_dir>/<name>``, validated and confined to that root.

        Single barrier for every name-derived filesystem path in this class.
        ``name`` originates in third-party ``POWER.md`` content or a URL path
        segment, so it is untrusted: it is re-validated against the strict name
        pattern here (not only at parse time).

        The child is returned LEXICALLY and a symlink at that path is refused.
        Resolving it was itself the hazard: with ``powers/foo`` symlinked to
        ``powers/bar``, ``resolve()`` yields ``<root>/bar`` — whose parent IS the
        root, so a containment check on the resolved path passes and every
        install/remove for ``foo`` silently operates on ``bar``, overwriting or
        deleting another Power. Because ``is_safe_power_name`` already forbids
        ``/``, ``\\`` and ``..``, the lexical child cannot escape the root, so
        resolution buys nothing and costs that redirect.
        """
        if not is_safe_power_name(name):
            raise PowerFormatError(f"invalid power name: {name!r}")
        # The root is used AS CONFIGURED, deliberately NOT resolved. Resolving it
        # here was the whole exposure: a shell could swap `powers/` for a symlink
        # to an external directory during this call, let `resolve()` capture the
        # external target, then restore the real directory before the mutating
        # worker re-checks it — so the check passed while every derived path
        # pointed outside the store. Without resolution there is nothing to
        # capture: paths stay lexically under the configured root, and
        # `_root_lock` opens the root `O_NOFOLLOW` and pins that identity for
        # the whole transaction, so a swap cannot redirect a mutation.
        root = self._dir
        candidate = root / name
        if candidate.is_symlink():
            raise PowerFormatError(
                f"power path is a symlink and cannot be used: {name!r}"
            )
        if candidate.parent != root:
            raise PowerFormatError(f"power path escapes the powers directory: {name!r}")
        return candidate

    # ── write ──

    async def install_from_dir(self, src: Path | str, *, source: dict[str, Any]) -> dict[str, Any]:
        """Install a Power from a materialized bundle directory *src*.

        Validates ``POWER.md``, rejects symlinks / oversize trees, and copies the
        documented bundle files into ``<powers_dir>/<name>``.

        The install is INERT BY CONSTRUCTION, which is the security property this
        module rests on: no MCP server is registered, no skill is materialized,
        and the bundle's ``mcp.json`` specs are never even read. Nothing outside
        this directory references it — ``skills.py``, ``context.py`` and
        ``agent.py`` do not look at ``powers_dir()`` — so an installed bundle
        cannot execute and cannot reach agent context. Activation (trust, enable,
        MCP registration, skill materialization) is a separate change.

        Raises :class:`PowerFormatError` on any validation failure.
        """
        loop = asyncio.get_running_loop()
        # Vet the source before any traversal: for the `folder` kind this path
        # is API-supplied.
        src = await loop.run_in_executor(maintenance_executor(), resolve_install_source, src)
        power_md = src / POWER_MD_NAME

        def _validate_source() -> PowerMeta:
            """Blocking: every stat, walk and read of the caller's source tree.

            The `is_file()` probe belongs in here with the rest. It looks free
            next to a tree walk, but the source is API-supplied and can be a
            network mount, where a single stat blocks for as long as the mount
            takes to answer -- and on the event loop that stalls chat, the
            heartbeat and every dashboard request, not just this install.
            """
            if not power_md.is_file():
                raise PowerFormatError(f"no {POWER_MD_NAME} found in bundle: {src}")
            # Walks the whole bundle up to the file/byte caps.
            _assert_safe_tree(src)
            return parse_power_md(_read_power_md(power_md))

        meta = await loop.run_in_executor(maintenance_executor(), _validate_source)
        name = meta.name  # already validated safe by parse_power_md

        dest = self._power_path(name)
        staging = self._dir / f".staging-{name}-{os.getpid()}"
        # The backup path is DETERMINISTIC (no pid): a pid-suffixed name is
        # unrecoverable by any later process, so a SIGKILL landing between the
        # `dest -> backup` and `staging -> dest` renames left the bundle stranded
        # under a name nothing would ever look for — the Power vanished from the
        # listing while its bytes sat on disk forever. With a fixed name the next
        # transaction can find and restore it (`_recover_orphaned_backup`).
        # Staging keeps the pid: it is pre-commit scratch that is never the only
        # copy of anything, so a stale one is safe to discard.
        backup = self._dir / f".backup-{name}"

        def _transaction() -> dict[str, Any] | None:
            """Blocking: the whole install — stage, swap, record, read back.

            One callable, for the same reason ``remove_power`` is one: compensation
            that lives in the coroutine is unfixable in principle. A cancelled task
            re-raises from every subsequent ``await`` (proven on 3.10 in CI), so
            awaited compensation silently never runs, while waiting synchronously to
            avoid that stalls the event loop. Both were reported in alternation, and
            each fix for one reintroduced the other. With a single worker there is
            nothing to compensate from outside and nothing to settle: cancelling the
            await cannot interleave with the work, because the worker runs to
            completion in its thread and rolls itself back.

            Containment comes from the pinned root handle rather than a check. The
            previous revision validated the root with ``_assert_root_not_symlinked()``
            and then mutated by path; even placed adjacently those are two operations,
            and the root could be swapped for a symlink in between, aiming a later
            rename or rmtree at files outside the store. ``_root_lock`` opens the root
            ``O_NOFOLLOW|O_DIRECTORY`` once and every mutation below goes through that
            descriptor, so the identity cannot change underneath the transaction.

            Copies ONLY the documented Power files, never the whole source tree. The
            install source is caller-selected and vetting just the selected root is not
            enough: an allowed ancestor (a home directory) can hold a valid
            ``POWER.md`` *and* ``~/.ssh``, and a recursive copy would relocate those
            under the powers directory. An allowlist makes that unrepresentable.
            """
            with _root_lock(self._dir) as (lock_fd, root_fd):
                # Crash recovery: a prior transaction was killed between its two
                # renames, so the live bundle is still sitting in the backup slot.
                # Restore it first, so this install replaces a complete predecessor
                # rather than silently discarding it.
                backup_present = _exists_at(root_fd, backup.name, self._dir)
                dest_present = _exists_at(root_fd, dest.name, self._dir)
                if backup_present and not dest_present:
                    # Unambiguous: the previous transaction died between its two
                    # renames, so the live bundle is only in the backup slot.
                    _rename_at(root_fd, backup.name, dest.name, self._dir)
                elif backup_present and dest_present:
                    # AMBIGUOUS, and the ambiguity is not resolvable from disk: a
                    # kill after the swap leaves new bytes at `dest` and the
                    # previous bundle at `backup`, with no way to tell whether the
                    # record write committed. Deleting the backup here discarded
                    # the only copy of the bundle the live record may still
                    # describe. It is preserved under a timestamped name instead —
                    # bytes are never destroyed to tidy up, and a stale
                    # `.backup-<name>` cannot block this install either.
                    quarantine = f".orphaned-backup-{name}-{int(time.time())}"
                    with contextlib.suppress(BaseException):
                        _rename_at(root_fd, backup.name, quarantine, self._dir)
                    logger.warning(
                        "powers: preserved an ambiguous rollback copy as %s — a "
                        "previous install was interrupted after the swap; remove it "
                        "once you have confirmed %s is the bundle you want",
                        quarantine,
                        name,
                    )
                # Staging is pre-commit scratch and never the only copy of
                # anything, so a stale one is always safe to discard.
                if _exists_at(root_fd, staging.name, self._dir):
                    with contextlib.suppress(BaseException):
                        _rmtree_at(root_fd, staging, self._dir)
                # Reconcile records with no bundle AFTER the recovery above, so a
                # store interrupted mid-uninstall is repaired by the next operation
                # of either kind — and never before a bundle awaiting restore.
                self._reconcile_store(root_fd)
                # Refuse to overwrite a bundle that came from a different source.
                # This runs INSIDE the transaction, after reconciliation: checking
                # before the lock would race a concurrent install, and checking
                # before recovery would compare against a record whose bundle is
                # about to be restored. A same-source reinstall (the upgrade path)
                # is unaffected.
                previous = self._load_installed(root_fd=root_fd).get(name)
                if isinstance(previous, dict):
                    before = previous.get("source")
                    if isinstance(before, dict) and _install_conflicts(before, source):
                        raise PowerSourceConflict(
                            f"{name!r} is already installed from a different source "
                            f"({_describe_source(before)}); remove it first to "
                            f"install {_describe_source(source)}"
                        )
                _mkdir_at(root_fd, staging.name, self._dir)
                had_previous = False
                swapped = False
                staging_fd = _open_dir_at(root_fd, staging.name, self._dir)
                try:
                    # The staging descriptor pins the directory just created, so
                    # the copy cannot be redirected into a pre-created decoy even
                    # if the root is swapped immediately after the mkdir.
                    _copy_power_files(src, staging, dest_fd=staging_fd)
                    if not (staging / POWER_MD_NAME).is_file():
                        raise PowerFormatError("staged bundle lost its POWER.md")
                    # Re-parse the STAGED POWER.md, not the pre-copy one. The source
                    # is caller-owned and mutable, so a rewrite between the earlier
                    # read and this copy would otherwise commit a different Power
                    # under the name already validated — leaving load_power()
                    # reporting an identity that does not match what is on disk.
                    staged_meta = parse_power_md(_read_power_md(staging / POWER_MD_NAME))
                    if staged_meta.name != name:
                        raise PowerFormatError(
                            f"staged bundle declares {staged_meta.name!r}, expected {name!r} "
                            f"— install source changed mid-install"
                        )
                    had_previous = _exists_at(root_fd, dest.name, self._dir)
                    if had_previous:
                        _rename_at(root_fd, dest.name, backup.name, self._dir)
                    _rename_at(root_fd, staging.name, dest.name, self._dir)
                    swapped = True
                    # The record is written INSIDE the transaction, so there is no
                    # window in which the tree is live and unrecorded, and no second
                    # worker whose cancellation would need compensating.
                    installed = self._load_installed(root_fd=root_fd)
                    installed[name] = {
                        "source": {
                            "kind": str(source.get("kind", "")),
                            "ref": str(source.get("ref", "")),
                        },
                        "installedAt": datetime.now(tz=timezone.utc).isoformat(),
                    }
                    self._write_installed(installed, root_fd=root_fd)
                    # Every write succeeded — only now is the backup expendable.
                    if _exists_at(root_fd, backup.name, self._dir):
                        with contextlib.suppress(BaseException):
                            _rmtree_at(root_fd, backup, self._dir)
                    # Read the response record back HERE, under the lock. Doing it
                    # after the transaction returned left a window in which a
                    # concurrent remove_power could drop the record, making an
                    # install that had already committed report a failure.
                    return self.load_power(name, root_fd=root_fd)
                except BaseException:
                    # Self-rollback. Restore the previous bundle when one was
                    # displaced, otherwise remove the tree this transaction put
                    # there, so a failed install leaves the store as it was.
                    with contextlib.suppress(BaseException):
                        if swapped:
                            if _exists_at(root_fd, dest.name, self._dir):
                                _rmtree_at(root_fd, dest, self._dir)
                            if had_previous and _exists_at(
                                root_fd, backup.name, self._dir
                            ):
                                _rename_at(root_fd, backup.name, dest.name, self._dir)
                        elif had_previous and _exists_at(
                            root_fd, backup.name, self._dir
                        ):
                            _rename_at(root_fd, backup.name, dest.name, self._dir)
                    # ANY uncommitted staging tree must go: it can hold up to the
                    # MAX_INSTALL_BYTES (8 MiB) cap. A successful swap consumes
                    # staging, so this only fires on the failure paths.
                    with contextlib.suppress(BaseException):
                        if _exists_at(root_fd, staging.name, self._dir):
                            _rmtree_at(root_fd, staging, self._dir)
                    raise
                finally:
                    if staging_fd is not None:
                        with contextlib.suppress(BaseException):
                            os.close(staging_fd)
            del lock_fd  # released by the context manager

        # Shielded so a cancelled await cannot abandon a half-applied store: the
        # executor thread does not stop when the awaiting coroutine is cancelled,
        # and the transaction is the only thing that can finish it correctly.
        # The transaction returns the response record it read back under its own
        # lock. Reading it out here instead cost nothing on the happy path but
        # allowed a concurrent delete to make a committed install look failed.
        record = await asyncio.shield(
            loop.run_in_executor(maintenance_executor(), _transaction)
        )
        if record is None:
            # Reachable only if the record vanished while the lock was held, which
            # would mean the store was mutated by something that did not take the
            # lock. Report it as an install failure rather than asserting: an
            # assertion is stripped under `python -O`, and this path must not
            # degrade into `None["kind"]`.
            raise PowerFormatError(
                f"install of {name!r} committed but its record could not be read back"
            )
        sel().log_api_access(
            caller="dashboard",
            operation="power_install",
            outcome="ok",
            resources=f"{name} kind={record['kind']}",
        )
        return record

    async def remove_power(self, name: str) -> bool:
        """Uninstall a Power: remove its disk tree and its record.

        Returns ``True`` if the Power existed and was removed.

        The ENTIRE transaction — lock, mutations, rollback, unlock — runs inside
        one blocking callable, and that shape is the point. Earlier revisions
        split the work across several executor calls and compensated from the
        coroutine afterwards, which is unfixable in principle: a cancelled task
        re-raises from every subsequent ``await`` (proven on 3.10 in CI), so
        awaited compensation silently never runs, while a synchronous wait to
        avoid that stalls the event loop. Both were reported, and each fix for
        one reintroduced the other.

        With a single worker there is nothing to compensate and nothing to wait
        for: cancelling the await cannot interleave with the work, because the
        worker owns its own rollback and runs to completion in its thread. The
        lock is taken and dropped inside the same callable, so it can neither
        leak nor be released while mutations are still in flight.
        """
        if not is_safe_power_name(name):
            raise PowerFormatError(f"invalid power name: {name!r}")

        power_path = self._power_path(name)
        scratch_path = power_path.parent / f"{_REMOVING_PREFIX}{name}"

        def _transaction() -> bool:
            """Blocking: the whole uninstall, including its own rollback."""
            with _root_lock(self._dir) as (lock_fd, root_fd):
                # Repair any record left behind by an interruption between a
                # previous delete and its record commit, before doing anything
                # else. Skips the Power being removed, whose own bundle is about to
                # go, and any name with a `.removing-*` tree still to reconcile.
                self._reconcile_store(root_fd)
                # Ordering: rename aside, commit the record, then destroy the
                # bytes. The rename is what makes committing first safe — it proves
                # the tree can be moved, and parks it at a name derived from the
                # record's own name, so a delete that fails afterwards leaves an
                # enumerable `.removing-<name>` rather than an invisible bundle.
                if not _exists_at(root_fd, power_path.name, self._dir):
                    # The bundle is not at its own name. A leftover
                    # `.removing-<name>` means a previous uninstall was
                    # interrupted between the rename and whatever came next.
                    scratch_present = _exists_at(root_fd, scratch_path.name, self._dir)
                    recorded = name in self._load_installed(root_fd=root_fd)
                    if scratch_present and recorded:
                        # The record still claims this Power and the scratch tree is
                        # the ONLY copy of its bytes. Deleting here and dropping the
                        # record afterwards would repeat the very ordering this
                        # round removed: a record replacement that fails (a
                        # held-open `installed.json` on Windows) would leave the
                        # bundle permanently destroyed. Restore it to its own name
                        # and fall through to the durable path below, which commits
                        # the record while the bytes still exist.
                        _rename_at(root_fd, scratch_path.name, power_path.name, self._dir)
                    elif scratch_present:
                        # Nothing records this Power, so the removal already
                        # committed and these are its unreclaimed bytes. Reporting a
                        # failure for a removal that succeeded is what the strict
                        # arm used to do, so the leftover is swept instead.
                        with contextlib.suppress(OSError):
                            _rmtree_at(root_fd, scratch_path, self._dir)
                        return False
                    else:
                        # No bundle and no scratch: only a record can remain.
                        return recorded and self._drop_record(name, root_fd=root_fd)
                if _exists_at(root_fd, scratch_path.name, self._dir):
                    _rmtree_at(root_fd, scratch_path, self._dir)
                _rename_at(root_fd, power_path.name, scratch_path.name, self._dir)
                staged = True
                staged_record: str | None = None
                try:
                    # The record write is still STAGED before anything is
                    # destroyed, so a full filesystem cannot get past this point:
                    # staging allocates and fsyncs the bytes, and the commit is a
                    # rename within one directory that needs no space.
                    installed = self._load_installed(root_fd=root_fd)
                    existed = name in installed
                    if not existed:
                        # No record names this bundle, so nothing is authoritative
                        # yet and a failed delete is simply a failed removal.
                        _rmtree_at(root_fd, scratch_path, self._dir)
                        staged = False
                        if _exists_at(root_fd, scratch_path.name, self._dir):
                            raise OSError(
                                f"power files could not be removed: {scratch_path}"
                            )
                        return False
                    pending = {k: v for k, v in installed.items() if k != name}
                    staged_record = _stage_write_at(
                        root_fd,
                        self._installed_path(),
                        json.dumps(pending, indent=2, sort_keys=True),
                    )
                    _commit_staged_at(root_fd, staged_record, self._installed_path())
                    staged_record = None
                    # Committed: the Power is removed, and the rollback arm below
                    # must no longer put the tree back.
                    staged = False
                    # A delete that fails from here is reclaimable garbage, not a
                    # failed removal, so it must not raise. Destroying the bytes
                    # first instead — the previous ordering — made a FAILED
                    # uninstall destructive: a held-open `installed.json` on
                    # Windows fails the record replacement after the bundle is
                    # already gone, so the caller sees an error for a Power that is
                    # in fact deleted.
                    with contextlib.suppress(OSError):
                        _rmtree_at(root_fd, scratch_path, self._dir)
                    if _exists_at(root_fd, scratch_path.name, self._dir):
                        logger.warning(
                            "powers: %s is removed but its files are still at %s; "
                            "the next operation will reclaim them",
                            name,
                            scratch_path.name,
                        )
                    return True
                except BaseException:
                    # A staged-but-uncommitted record must not linger: it describes
                    # a removal that did not happen.
                    if staged_record is not None:
                        _discard_staged_at(
                            root_fd, staged_record, self._installed_path()
                        )
                    # Self-rollback: put the tree back if it is still staged.
                    if staged:
                        with contextlib.suppress(BaseException):
                            if not _exists_at(root_fd, power_path.name, self._dir):
                                _rename_at(
                                    root_fd, scratch_path.name, power_path.name, self._dir
                                )
                    raise
            del lock_fd  # released by the context manager

        loop = asyncio.get_running_loop()
        existed = await asyncio.shield(
            loop.run_in_executor(maintenance_executor(), _transaction)
        )

        if existed:
            sel().log_api_access(
                caller="dashboard",
                operation="power_remove",
                outcome="ok",
                resources=name,
            )
        return existed
