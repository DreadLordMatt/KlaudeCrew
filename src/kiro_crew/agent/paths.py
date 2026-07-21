"""Path resolution, atomic writes, and MCP invocation for the agent package.

Foundational leaf module of the ``kiro_crew.agent`` package: filesystem
path constants, the atomic JSON writer, ``kirocrew`` binary resolution
(with the mutable ``_KIROCREW_BIN`` cache -- single owner), managed-MCP
invocation, and edition-contributed MCP server discovery.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.platform import current_context, safe_context_call

logger = logging.getLogger("kiro_crew.agent")



def _atomic_json_write(path: Path, data: dict) -> None:
    """Write JSON atomically via tmp+rename to prevent read-of-partial-file.

    kiro-cli reads agent configs at spawn and set_mode.  Non-atomic writes
    (truncate-then-write) can deliver empty or partial JSON, crashing the
    ACP process with exit code 1.  rename() is atomic on Linux when source
    and destination are on the same filesystem.

    Uses mkstemp for a unique temp file per call so concurrent writers
    to the same path don't clobber each other's temp files.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except FileNotFoundError:
                mode = 0o644
            platform_compat.fchmod_safe(f.fileno(), mode)
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"
AGENT_FILENAME = "kirocrew.json"
_KIRO_MCP_JSON = Path.home() / ".kiro" / "settings" / "mcp.json"
_CC_MCP_JSON = Path.home() / ".claude.json"

# Bundled fallback — inside the kiro_crew.config package
_BUNDLED_CFG_DIR = Path(__file__).resolve().parent.parent / "config"


def _project_dir() -> Path | None:
    """Return the project root from KIROCREW_PROJECT_DIR, or None."""
    val = os.environ.get("KIROCREW_PROJECT_DIR")
    if val:
        p = Path(val)
        if p.is_dir():
            return p
    return None


def _shipped_defaults() -> Path:
    """Return defaults.json, preferring project-dir override for development."""
    proj = _project_dir()
    if proj:
        candidate = proj / "agents" / "defaults.json"
        if candidate.is_file():
            return candidate
    return _BUNDLED_CFG_DIR / "defaults.json"


def _shipped_prompt() -> Path:
    """Return prompt.md, preferring project-dir override for development."""
    proj = _project_dir()
    if proj:
        candidate = proj / "agents" / "prompt.md"
        if candidate.is_file():
            return candidate
    return _BUNDLED_CFG_DIR / "prompt.md"


# User overrides
_USER_DIR = Path.home() / ".kirocrew"
_USER_PROMPT = _USER_DIR / "prompt.md"
_USER_OVERRIDES = _USER_DIR / "agent.json"

# kirocrew binary path — resolved lazily to handle gateway restarts
# where PATH may not include the virtualenv at import time.
_KIROCREW_BIN: str | None = None


def _bin_is_usable(path: Path) -> bool:
    """Return True if *path* is a readable file.

    Symbol preserved for callers; the previous Amazon-specific Apollo/Brazil
    wrapper-script rejection logic is a no-op on a public install (those
    binaries are absent), so any readable executable is accepted.
    """
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def _kirocrew_bin_subpath(root: Path) -> Path:
    """The console-script path under an install ``root`` for this OS.

    A venv exposes its entry points under ``bin/kirocrew`` on POSIX but
    ``Scripts/kirocrew.exe`` on Windows — pip generates a ``.exe`` launcher
    there from the ``console_scripts`` entry point. Resolving the POSIX layout
    on Windows finds nothing, which silently drops the built-in
    ``kirocrew-cron`` / ``kirocrew-core`` MCP servers (``command not found:
    .../bin/kirocrew``). Branch on the platform so both layouts resolve.
    """
    if platform_compat.IS_WINDOWS:
        return root / "Scripts" / "kirocrew.exe"
    return root / "bin" / "kirocrew"


def _resolve_kirocrew_bin() -> str:
    """Resolve the absolute path of the ``kirocrew`` executable.

    Resolution order (first existing + executable wins):

    0. Frozen/PyInstaller app (the shipped desktop app): ``sys.executable``
       *is* the kirocrew CLI — e.g. ``.../kirocrew-backend`` — which accepts
       the ``mcp-core`` / ``mcp-cron`` subcommands. The bundle has no
       ``bin/kirocrew`` and nothing named ``kirocrew`` on PATH, so this is the
       only reliable handle; without it kirocrew-core/kirocrew-cron are dropped.
    1. Same install as the current process: walk up from ``kiro_crew.__file__``
       looking for a ``bin/kirocrew`` sibling. Covers venv-based installs and
       source-tree dev trees.
    2. ``shutil.which('kirocrew')`` — respects PATH order.
    3. Bare ``"kirocrew"`` — last resort, may fail but surfaces the problem
       instead of caching a known-bad absolute path.

    Every candidate is validated with ``is_file()`` and ``os.access(X_OK)``
    before being returned, so stale paths from previous installs are skipped.
    """
    global _KIROCREW_BIN
    if _KIROCREW_BIN:
        return _KIROCREW_BIN

    def _usable(p: str | Path) -> bool:
        sp = str(p)
        if not (sp and os.path.isfile(sp) and os.access(sp, os.X_OK)):
            return False
        return _bin_is_usable(Path(sp))

    # Frozen/PyInstaller app (shipped desktop app): ``sys.executable`` is the
    # bundled ``kirocrew-backend`` binary, which *is* the kirocrew CLI and
    # accepts the ``mcp-core`` / ``mcp-cron`` subcommands. The bundle ships no
    # ``bin/kirocrew`` and nothing named ``kirocrew`` on PATH, so this is the
    # only reliable handle — without it kirocrew-core / kirocrew-cron (and
    # therefore spawn_run / cron_add / learn_add …) get dropped.
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if _usable(exe):
            _KIROCREW_BIN = exe
            return _KIROCREW_BIN

    # 0. Prefer the venv entrypoint for source-tree installs (editable
    #    install with a sibling .venv directory, e.g. project/src/kiro_crew
    #    + project/.venv/bin/kirocrew).
    #    NOTE: For pip-into-venv installs where pkg_dir is inside .venv/,
    #    the pyvenv.cfg guard below breaks early and step 1 handles it.
    try:
        # Circular import: kiro_crew.agent is loaded during kiro_crew
        # package initialization, so importing kiro_crew at module level
        # would create a circular dependency. Deferring here resolves
        # after the package is fully loaded.
        import kiro_crew as _mc  # noqa: PLC0415  circular import

        pkg_dir = Path(_mc.__file__).resolve().parent
        for parent in pkg_dir.parents:
            venv_candidate = _kirocrew_bin_subpath(parent / ".venv")
            if _usable(venv_candidate):
                _KIROCREW_BIN = str(venv_candidate)
                return _KIROCREW_BIN
            if (parent / "pyvenv.cfg").exists():
                break
    except Exception:
        logger.debug("kirocrew venv bin check failed", exc_info=True)

    # 1. Walk up from the running package to find bin/kirocrew
    try:
        import kiro_crew as _mc  # noqa: PLC0415  circular import

        pkg_dir = Path(_mc.__file__).resolve().parent
        for parent in pkg_dir.parents:
            candidate = _kirocrew_bin_subpath(parent)
            if _usable(candidate):
                _KIROCREW_BIN = str(candidate)
                return _KIROCREW_BIN
            if (parent / "pyvenv.cfg").exists():
                break  # reached venv root without finding the binary
    except Exception:
        logger.debug("kirocrew bin walk failed", exc_info=True)

    # 2. PATH lookup (also validated)
    found = shutil.which("kirocrew")
    if found and _usable(found):
        _KIROCREW_BIN = found
        return _KIROCREW_BIN

    # 3. Last resort — don't cache, so a future call can retry
    logger.warning(
        "Could not resolve kirocrew binary to an existing file; "
        "falling back to bare 'kirocrew' (MCP probes may fail)"
    )
    return "kirocrew"


def _kirocrew_mcp_invocation(subcommand: str) -> tuple[str, list[str]]:
    """Resolve a CWD- and shebang-independent invocation for a built-in
    MCP server (``kirocrew-cron`` / ``kirocrew-core``).

    Prefers a standalone ``kirocrew`` binary when one resolves. Falls back
    to ``<interpreter> -m kiro_crew <subcommand>`` when
    :func:`_resolve_kirocrew_bin` cannot find a usable standalone binary --
    e.g. an install whose launcher is not on the service PATH (the gateway
    running as a systemd user service is the common case): there
    ``_resolve_kirocrew_bin`` returns the bare ``"kirocrew"`` sentinel, the
    command fails to validate, and the server gets dropped from
    ``kirocrew.json`` on every config refresh.

    ``sys.executable`` is the absolute path of the running interpreter, so it
    needs no PATH entry and ignores any broken launcher. ``python -m
    kiro_crew`` dispatches the same CLI as the ``kirocrew`` console script.
    """
    bin_path = _resolve_kirocrew_bin()
    if bin_path == "kirocrew":  # unresolved sentinel from _resolve_kirocrew_bin
        return sys.executable, ["-m", "kiro_crew", subcommand]
    return bin_path, [subcommand]


# ---------------------------------------------------------------------------
# Managed MCP servers — single source of truth.
#
# Every server here is dynamically injected into the agent config at install
# time (both fresh and existing configs).  Adding a new managed server =
# one entry here.
# ---------------------------------------------------------------------------
_MANAGED_MCP_SERVERS: dict[str, dict] = {
    "kirocrew-cron": {"invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-cron")},
    "kirocrew-core": {"invocation_fn": lambda: _kirocrew_mcp_invocation("mcp-core")},
}


def _extra_mcp_servers() -> dict[str, dict]:
    """Edition-contributed MCP servers from the active PlatformContext.

    The Default adapter returns ``{}`` so the standalone spec is byte-for-byte
    what it is today; the Amazon companion contributes builder-mcp (and other
    internal servers).  Entries are already in kiro-cli's ``mcpServers`` shape
    (``{"command", "args", optional "autoApprove", ...}``) — the consumer
    *merges* them into the ``mcpServers`` map rather than restructuring the
    spec, preserving the ``deny_unknown_fields`` invariant.
    """
    # Fail-closed via safe_context_call: a non-standalone host that cannot
    # compose its context re-raises PlatformCompositionError (never silently
    # degrades to the empty OSS server set); any other lookup failure -> none.
    # Annotate the target so safe_context_call's TypeVar binds from here, not
    # from the empty ``fallback={}`` literal (which would infer dict[Never, Never]
    # and clash with extra_mcp_servers()'s dict[str, dict] return).
    extra: dict[str, dict] = safe_context_call(
        lambda: current_context().mcp_tooling.extra_mcp_servers(),
        fallback={},
        log_message="extra_mcp_servers lookup failed; using none",
    )
    return dict(extra) if extra else {}


def ensure_kirocrew_on_path(bin_dir: Path | None = None) -> str | None:
    """Ensure a ``kirocrew`` launcher is reachable on the user's PATH.

    The source ``install.sh`` symlinks ``~/.local/bin/kirocrew`` → the venv
    entry point, but install paths that don't run it (notably the packaged
    Electron app) leave no ``kirocrew`` on PATH — breaking the ``kirocrew``
    terminal command. This mirrors that symlink step in Python so it runs from
    ``kirocrew setup``. Best-effort and idempotent:

    * No-op if ``kirocrew`` already resolves on PATH to the same binary.
    * No-op if no concrete binary can be resolved (nothing to point at).
    * Otherwise (re)create ``<bin_dir>/kirocrew`` → the resolved binary.

    Args:
        bin_dir: Target directory for the shim. Defaults to ``~/.local/bin``.

    Returns:
        The shim path if one was created/updated, else ``None``.
    """
    target = _resolve_kirocrew_bin()
    # Nothing concrete to point at — bare "kirocrew" or a non-executable file.
    if not (os.path.isabs(target) and os.path.isfile(target) and os.access(target, os.X_OK)):
        return None

    # Already reachable on PATH as the same binary? Then there's nothing to do.
    existing = shutil.which("kirocrew")
    if existing and os.path.realpath(existing) == os.path.realpath(target):
        return None

    bin_dir = bin_dir or (Path.home() / ".local" / "bin")
    link = bin_dir / "kirocrew"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            if os.path.realpath(link) == os.path.realpath(target):
                return None
            link.unlink()
        link.symlink_to(target)
    except OSError:
        logger.warning("Could not create kirocrew shim at %s", link, exc_info=True)
        return None
    logger.info("Linked kirocrew shim: %s -> %s", link, target)
    return str(link)
