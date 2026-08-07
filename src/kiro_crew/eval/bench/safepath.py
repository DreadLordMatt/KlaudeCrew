"""One filesystem guard for every caller-influenced path in the benchmark harness.

Why a shared helper rather than a check at each call site: the first two rounds of
review on this code found the same class of hole twice, in mirror image. Round one
gated the report *read* (``bench compare <path>``); round two found the report
*write* (``--out-dir`` + ``--stem``) still ungated, which is strictly worse — a read
discloses, a write destroys. Fixing that one site would have left three more:
``--out-dir``'s ``mkdir``, and the corpus cache root, which ``KIROCREW_BENCH_CACHE``
can point anywhere. Point-wise patching is how the second hole survived the first
fix, so the guard lives in one place and every argv- or env-influenced path calls it.

The threat model is the same one that justifies the read gate. These values arrive
from argv and the environment, and in this product neither is necessarily set by the
human who owns the machine: an agent can run any CLI command. So

    kirocrew bench retrieval --out-dir ~/.kiro/crew --stem security_policy

is a reachable invocation that would overwrite a governance policy file with a
benchmark report. Nothing about the benchmark needs to write there, so it is refused
rather than made careful.

Write protection is deliberately stricter than read protection. ``is_sensitive_path``
answers "is this path inside a protected location"; for a directory that is about to
receive files, the question is also "does a protected location lie *under* it", which
is what ``path_contains_sensitive`` answers. A ``--out-dir`` of ``~`` is not itself
sensitive, but writing a tree there is not something this command should do.
"""

from __future__ import annotations

from pathlib import Path

from .errors import BenchRefusal


class UnsafePathError(BenchRefusal):
    """Raised instead of touching a protected location. Carries an actionable message."""


def _resolve(path: str | Path) -> Path:
    # Canonicalize before checking, so a symlink cannot launder the target. The
    # gate helpers do their own resolution too; doing it here keeps the message
    # honest about what was actually going to be touched.
    return Path(path).expanduser().resolve()


def guard_read_path(path: str | Path, *, what: str) -> Path:
    """Refuse to read *path* when it resolves into a protected location."""
    from kiro_crew.security import is_sensitive_path

    resolved = _resolve(path)
    if is_sensitive_path(str(resolved)):
        raise UnsafePathError(
            f"refusing to read the {what}: it resolves into a protected location "
            "(a credential store or the governance trust root). Nothing the "
            "benchmark needs lives there."
        )
    return resolved


def guard_write_path(path: str | Path, *, what: str) -> Path:
    """Refuse to write *path* when it is protected, or sits under a protected root."""
    from kiro_crew.security import is_sensitive_path

    resolved = _resolve(path)
    if is_sensitive_path(str(resolved)):
        raise UnsafePathError(
            f"refusing to write the {what} to {resolved.name!r}: the destination "
            "resolves into a protected location (a credential store or the "
            "governance trust root). Choose an --out-dir outside it."
        )
    return resolved


def guard_output_dir(path: str | Path, *, what: str) -> Path:
    """Refuse an output directory that is protected OR that contains a protected tree.

    The second half is why this is not just :func:`guard_write_path`. ``~`` is not a
    sensitive path, but it *contains* ``~/.ssh`` and the crew data home, and a
    command that creates directories and files under it is doing something no
    benchmark run needs to do.
    """
    from kiro_crew.security import is_sensitive_path, path_contains_sensitive

    resolved = _resolve(path)
    if is_sensitive_path(str(resolved)):
        raise UnsafePathError(
            f"refusing to use {resolved} as the {what}: it resolves into a "
            "protected location (a credential store or the governance trust root)."
        )
    if path_contains_sensitive(str(resolved)):
        raise UnsafePathError(
            f"refusing to use {resolved} as the {what}: a protected location lies "
            "under it, so writing a tree there could reach a credential store or "
            "the governance trust root. Choose a narrower directory."
        )
    return resolved


# ── Check-to-use: guarding a path by NAME is not enough ──────────────────────
#
# Guarding a directory does not guard the files derived from it. The corpus cache
# root is checked, but the download's ``.part`` staging file and the ``.sha256``
# sidecar are separate final components inside it, and a symlink planted at either
# name redirects the open to wherever it points. Reading the sidecar through such a
# link puts the target's bytes into the "expected checksum" mismatch message —
# printing a credential file to stdout — and writing through one truncates whatever
# it points at.
#
# Anything that can plant that link is anything running as this user, which by this
# harness's own threat model includes an agent. And resolving the name then opening
# the name leaves a window in which the final component can be swapped between the
# two, so the guard has to be paired with an open that refuses to follow a link
# rather than repeated more carefully.


def _supports_pinned_walk() -> bool:
    """Whether this platform can open relative to a directory descriptor.

    ``O_NOFOLLOW`` is part of the requirement, not an extra: a pinned walk without it
    would open each ancestor happily through whatever link sits there, which is the
    hole being closed. Found by the Windows-simulation tests, which delete
    ``os.O_NOFOLLOW`` and would otherwise have taken this path and crashed.
    """
    import os

    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
    )


def _open_in_pinned_parent(
    resolved_parent: str, name: str, *, flags: int, mode: int, what: str
) -> int:
    """Open *name* under *resolved_parent*, refusing any component that is now a link.

    One ``openat`` per component, each relative to the previous component's descriptor
    and each carrying ``O_NOFOLLOW``. Two properties come out of that:

    * a component that became a symlink after *resolved_parent* was computed fails
      ``O_NOFOLLOW`` and is refused -- this is the check-to-use swap, and it is the
      reason a single ``os.open(parent, O_DIRECTORY)`` is not enough: that call follows
      such a link silently and then pins its target;
    * once a component is open, its descriptor cannot be re-pointed, so everything
      already traversed is fixed.

    *resolved_parent* must be resolved by the CALLER, once, before this runs. Resolving
    it here would re-follow whatever an ancestor points at by now, which is the exact
    mistake that made an earlier version of this defensible-looking and useless.

    *name* is opened as given, so a link at the final name is refused by the same flag.

    Not closed: a component swapped before *resolved_parent* was computed is followed
    by that resolution. Refusing every symlinked ancestor would close it and would also
    break ``--out-dir /tmp/...`` on macOS, where ``/tmp`` is itself a link.
    """
    import errno
    import os
    from pathlib import PurePath

    parts = PurePath(resolved_parent).parts
    if not parts:  # pragma: no cover - a resolved path always has parts
        raise UnsafePathError(f"refusing to open the {what}: empty parent path")

    if os.path.isabs(resolved_parent):
        dir_fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
        rest = parts[1:]
    else:  # pragma: no cover - realpath returns absolute paths
        dir_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
        rest = parts

    try:
        for component in rest:
            try:
                nxt = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=dir_fd,
                )
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise UnsafePathError(
                        f"refusing to write the {what}: the directory {component!r} on "
                        "the way to it became a symbolic link after the path was "
                        "checked. A parent swapped for a link redirects the write "
                        "however carefully the final name is opened, so it is refused."
                    ) from exc
                raise
            os.close(dir_fd)
            dir_fd = nxt
        return os.open(name, flags, mode, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _refuse_hardlink_alias(fd: int, *, what: str, name: str) -> None:
    """Reject a descriptor that is one of several names for the same inode.

    A hardlink is invisible to every path-based guard: it shares the target's inode,
    so ``realpath`` yields the alias's own name, ``is_symlink()`` is False, and
    ``O_NOFOLLOW`` has no link to refuse. A planted alias therefore let an O_TRUNC
    write destroy a protected file, and let a read hand back its bytes.

    Checked on the DESCRIPTOR rather than the path, which is what makes it
    race-free: this fd already refers to the inode being judged.

    The cost is honest and small: a corpus file that legitimately has more than one
    link -- a dedup-ing backup tool, a deliberate alias -- is refused. Copy it or
    point the cache elsewhere.
    """
    import os

    links = os.fstat(fd).st_nlink
    if links > 1:
        os.close(fd)
        raise UnsafePathError(
            f"refusing to use the {what}: {name!r} has {links} hard links, so it is "
            "another name for a file this command was not pointed at. A path guard "
            "cannot see that -- the alias shares the target's inode -- so it is "
            "refused on the open descriptor instead. Remove the extra link or use a "
            "different path."
        )


def open_write_nofollow(path: str | Path, *, what: str) -> int:
    """Open *path* for writing, guarded, and refusing to follow a final symlink.

    ``O_NOFOLLOW`` fails with ``ELOOP`` when the final component is a link, which
    closes both the pre-planted link and the check-to-use swap: the guard decides
    what the name means, and the flag guarantees the descriptor is that file and not
    a redirect to another one.

    Returns a raw fd; wrap it with :func:`os.fdopen`. Mode is 0o600 because a corpus
    cache file has no reason to be group- or world-readable.

    Note which path is opened: the guard resolves in order to answer "does this name
    mean somewhere protected", but the ``open`` must use the path **as given**, not the
    resolved one. Opening the resolved path makes ``O_NOFOLLOW`` a no-op -- resolution
    has already followed the link, so the flag inspects the target's final component
    instead of the link, and the write lands on the redirect exactly as if there were
    no flag at all. ``O_NOFOLLOW`` only covers the final component; ancestor
    directories are covered by guarding the containing root.

    **Windows has no ``O_NOFOLLOW``.** ``getattr(os, "O_NOFOLLOW", 0)`` returns 0
    there, so the flag contributes nothing and an explicit ``is_symlink()``
    pre-check stands in for it. Be precise about what that buys:

    * it DOES refuse a link already sitting at the name, which is the realistic case
      (something planted it, then the harness ran);
    * it does NOT close the check-to-use window. Between the ``is_symlink`` call and
      the ``open``, a link can be swapped in and Windows will follow it. That is a
      real gap, not a hedge.

    The stronger option is a ctypes ``CreateFileW`` with
    ``FILE_FLAG_OPEN_REPARSE_POINT``, which opens the reparse point itself and has no
    window. Deliberately not used: it cannot be exercised on the machine this harness
    is developed on, and unverifiable security code in a benchmark tool is a worse
    trade than a weaker guard whose limit is written down.
    """
    import errno
    import os

    # The guard answers "does this name mean somewhere protected" and its return value
    # is deliberately NOT used as the open path: it has the final symlink already
    # followed, so opening it would undo the final-component protection. The pinned
    # walk below resolves the PARENT chain and opens the final name as given.
    guard_write_path(path, what=what)
    as_given = Path(path).expanduser()
    if not hasattr(os, "O_NOFOLLOW") and as_given.is_symlink():
        # Windows stand-in for the missing flag. See the docstring for the window
        # this does NOT close.
        raise UnsafePathError(
            f"refusing to write the {what}: {as_given.name!r} is a symbolic link. "
            "A link at that name redirects the write to whatever it points at, so "
            "it is refused rather than followed. Delete it and re-run.\n"
            "(Detected by an explicit check: this platform has no O_NOFOLLOW, so a "
            "link swapped in after the check would still be followed.)"
        )
    # O_TRUNC is deliberately ABSENT here. Truncating at open time destroys the file
    # before anything can inspect what it actually is, and a hardlink alias is only
    # recognisable once the descriptor exists. Open, judge the inode, then truncate.
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        if _supports_pinned_walk():
            # Parent pinned by a descriptor, final name opened relative to it. After
            # the parent is open, swapping its name for a symlink cannot redirect
            # this write.
            # Resolved ONCE, here, and passed down as a string. The helper must not
            # re-resolve: doing so re-follows whatever an ancestor points at by then,
            # which is what defeated an earlier version of this.
            resolved_parent = os.path.realpath(as_given.parent or Path("."))
            fd = _open_in_pinned_parent(
                resolved_parent,
                as_given.name,
                flags=flags,
                mode=0o600,
                what=what,
            )
        else:
            # Windows: no dir_fd support in the stdlib, so ancestors are unprotected
            # here in the same way O_NOFOLLOW is absent. Both gaps are documented.
            fd = os.open(as_given, flags, 0o600)
        _refuse_hardlink_alias(fd, what=what, name=as_given.name)
        os.ftruncate(fd, 0)
        return fd
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise UnsafePathError(
                f"refusing to write the {what}: {as_given.name!r} is a symbolic "
                "link. A link at that name redirects the write to whatever it "
                "points at, so it is refused rather than followed. Delete it and "
                "re-run."
            ) from exc
        raise


def read_text_nofollow(path: str | Path, *, what: str) -> str:
    """Read *path* as UTF-8, guarded, refusing to follow a final symlink.

    Does what ``hooks.safe_read_file`` does -- canonicalize, re-check the RESOLVED
    target against ``is_sensitive_path``, open the canonical path with ``O_NOFOLLOW``
    -- and adds a hardlink rejection on the open descriptor.

    Inlined rather than delegated for exactly that last part: a hardlink alias is only
    recognisable from the fd (``st_nlink``), and a helper that returns text has
    already read the bytes by the time it could be judged. ``hooks.safe_read_file``
    is used repo-wide, so the check lives here instead of widening its contract.
    """
    import os

    from kiro_crew.security import is_sensitive_path

    guard_read_path(path, what=what)
    # Mirrors `safe_read_file` -- resolve, re-check the RESOLVED target, open the
    # canonical path with O_NOFOLLOW -- and adds the hardlink rejection, which has to
    # happen on the descriptor and therefore cannot be delegated to a helper that
    # returns text. Opening the resolved path (not the path as given) is deliberate:
    # a link to an ordinary file stays readable, which is the documented read/write
    # asymmetry.
    try:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
        if is_sensitive_path(resolved):
            raise UnsafePathError(
                f"refusing to read the {what}: {resolved} is a protected location."
            )
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except UnsafePathError:
        raise
    except OSError as exc:
        raise UnsafePathError(f"refusing to read the {what}: {exc}") from exc
    _refuse_hardlink_alias(fd, what=what, name=os.path.basename(resolved))
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read()
