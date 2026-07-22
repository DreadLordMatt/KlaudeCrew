"""macOS Seatbelt profile builder, ``sandbox_exec_argv``, the shared env-unset
helper, and stale sandbox-launcher / profile cleanup."""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

from kiro_crew import platform_compat

from . import launcher
from .backends import (
    _AGENT_DENIED_ENV_KEYS,
    _CC_EXPOSE_FILES,
    _CC_FILES,
    _PYTHON_ENV_PREFIXES,
    _SENSITIVE_ENV_PREFIXES,
    _STANDARD_DIRS,
    _sandbox_policy,
)

logger = logging.getLogger("kiro_crew.sandbox")


# Launcher scripts and seatbelt profiles are read exactly once at child exec.
# Any file older than this threshold is garbage regardless of PID liveness.
_LAUNCHER_MAX_AGE_SECONDS = 3600

# Legacy sandbox launcher directory (before migration to ~/.kirocrew/run/).
_LEGACY_LAUNCHER_DIR = "/tmp"


# ── Backend: macOS sandbox-exec ──

_SEATBELT_PROFILE = """\
(version 1)
(allow default)
{deny_rules}
"""


def _build_seatbelt_profile(sandbox_level: str = "strict") -> str:
    """Build a Seatbelt .sb profile denying reads of sensitive dirs."""
    home = str(Path.home())
    # Source the sensitive-dir lists from the active PlatformContext (Default
    # adapter == today's module globals; Amazon companion adds .midway/.ada).
    if sandbox_level == "standard":
        dirs = _STANDARD_DIRS
    elif sandbox_level == "cc":
        # On macOS, don't hide .aws — credential_process and SSO token
        # caches live under .aws/ and Seatbelt can't do partial exposure
        # as cleanly as Linux bind mounts. Deny patterns still block LLM
        # tool reads of credential files. The .aws-exclusion is applied to the
        # context-sourced list so a companion's extra cc dirs are still hidden.
        dirs = [d for d in _sandbox_policy().cc_dirs() if d != ".aws"]
    else:
        dirs = _sandbox_policy().strict_dirs()
    files = _CC_FILES if sandbox_level in ("cc", "strict") else []
    expose_files = _CC_EXPOSE_FILES if sandbox_level == "cc" else []
    expose_abs = {os.path.join(home, f) for f in expose_files}
    rules: list[str] = []
    for d in dirs:
        target = os.path.join(home, d)
        escaped = target.replace('"', '\\"')
        # Check if any exposed files live under this dir
        exposed_in_dir = [f for f in expose_abs if f.startswith(target + "/")]
        if exposed_in_dir:
            exceptions = " ".join(
                f'(require-not (literal "{f.replace(chr(34), chr(92) + chr(34))}"))'
                for f in exposed_in_dir
            )
            rules.append(f'(deny file-read* (require-all (subpath "{escaped}") {exceptions}))')
        else:
            rules.append(f'(deny file-read* (subpath "{escaped}"))')
        # AVP-23427: deny creating a HARDLINK whose target is under this dir.
        # Seatbelt's file-read* deny is path-based, so a hardlink at a
        # non-denied path (e.g. /tmp) reads the same inode past the deny rule.
        # ``file-link`` fires on the link TARGET, so this stops the sandboxed
        # agent from minting such a hardlink in the first place.  Blanket (no
        # exposed-file exception): the agent never needs to hardlink a
        # credential-dir file, and blocking it is harmless.
        rules.append(f'(deny file-link (subpath "{escaped}"))')
    for f in files:
        target = os.path.join(home, f)
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-read* (literal "{escaped}"))')
        # AVP-23427: also deny hardlinking the protected file (see above).
        rules.append(f'(deny file-link (literal "{escaped}"))')

    # .ssh: deny all access except reading known_hosts (strict only)
    if sandbox_level == "strict":
        ssh_dir = os.path.join(home, ".ssh")
        ssh_escaped = ssh_dir.replace('"', '\\"')
        ssh_kh = os.path.join(ssh_dir, "known_hosts")
        ssh_kh_escaped = ssh_kh.replace('"', '\\"')
        rules.append(
            f'(deny file-read* (require-all (subpath "{ssh_escaped}")'
            f' (require-not (literal "{ssh_kh_escaped}"))))'
        )
        rules.append(f'(deny file-write* (subpath "{ssh_escaped}"))')
        # AVP-23427: block hardlinking any .ssh file (private keys) out of the
        # denied subtree.  Blanket over the whole subpath — no known_hosts
        # exception, since a hardlink to known_hosts has no legitimate use.
        rules.append(f'(deny file-link (subpath "{ssh_escaped}"))')

    return _SEATBELT_PROFILE.format(deny_rules="\n".join(rules))


def sandbox_exec_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
) -> tuple[list[str], str | None]:
    """Wrap *argv* with ``sandbox-exec -f <profile>``.

    Also scrubs sensitive env vars via ``env -u`` since Seatbelt only
    handles file-level deny rules, not environment variables.

    Returns (new_argv, tmp_profile_path).  Caller should delete the
    profile file after the child exits.
    """
    resolved_argv = list(argv)
    if resolved_argv:
        resolved_argv[0] = launcher._resolve_agent_executable(resolved_argv[0])

    profile = _build_seatbelt_profile(sandbox_level)
    run_dir = launcher._ensure_run_dir()
    fd, path = tempfile.mkstemp(suffix=".sb", prefix=f"kirocrew_sandbox_{os.getpid()}_", dir=run_dir)
    os.write(fd, profile.encode())
    os.close(fd)
    # Build env -u flags for sensitive vars present in current env. cc/strict
    # additionally scrub agent-denied credential keys (Slack tokens, owner id)
    # since loader.py seeds them into os.environ for trusted children only.
    unset_args = _sandbox_env_unset_args(sandbox_level, strip_python_env)
    return ["env", *unset_args, "sandbox-exec", "-f", path, *resolved_argv], path


def _sandbox_env_unset_args(sandbox_level: str, strip_python_env: bool) -> list[str]:
    """``env -u`` flags scrubbing sensitive vars for a sandboxed/delegated spawn.

    Shared by ``sandbox_exec_argv`` (seatbelt wrap) and
    ``_delegate_to_kiro_internal_sandbox`` (macOS mutual-exclusion path) so the
    env-scrub guarantee is identical whether or not KiroCrew's own seatbelt is
    the active isolation layer.
    """
    prefixes = list(_SENSITIVE_ENV_PREFIXES)
    if sandbox_level in ("cc", "strict"):
        prefixes.extend(_AGENT_DENIED_ENV_KEYS)
    if strip_python_env:
        prefixes.extend(_PYTHON_ENV_PREFIXES)
    unset_args: list[str] = []
    for key in os.environ:
        for prefix in prefixes:
            if key.startswith(prefix):
                unset_args.extend(["-u", key])
                break
    return unset_args


def cleanup_stale_sandbox_profiles(*, legacy_dir: str | None = None) -> int:
    """Remove orphan sandbox files from ~/.kirocrew/run/ and legacy /tmp.

    A file is removed when EITHER:
      - The tagged PID is dead (os.kill probe fails), OR
      - The file mtime is older than _LAUNCHER_MAX_AGE_SECONDS (the launcher
        is consumed exactly once at child exec, so old files are garbage
        regardless of PID liveness — this handles the spawner-PID design
        where the gateway PID is always alive for current-generation files).

    Also sweeps legacy /tmp/kirocrew_sandbox_*.py files that predate the
    migration to ~/.kirocrew/run/ — these have no PID segment, so only the
    age threshold applies.

    Called from the periodic cleanup sweep in session.py, offloaded to the
    maintenance executor (blocking I/O).  Safe to call from sync contexts too.

    Returns:
        Number of stale files removed.
    """
    now = time.time()
    if legacy_dir is None:
        legacy_dir = _LEGACY_LAUNCHER_DIR
    run_dir = os.path.join(os.path.expanduser("~"), ".kirocrew", "run")
    removed = 0

    # ── Sweep ~/.kirocrew/run/ (PID + age) ──
    if os.path.isdir(run_dir):
        for entry in os.listdir(run_dir):
            if not entry.startswith("kirocrew_sandbox_"):
                continue
            if entry.endswith(".sb"):
                suffix = ".sb"
            elif entry.endswith(".py"):
                suffix = ".py"
            else:
                continue
            filepath = os.path.join(run_dir, entry)
            # Age check first — handles the spawner-PID design flaw
            try:
                mtime = os.stat(filepath).st_mtime
            except OSError:
                continue
            if (now - mtime) > _LAUNCHER_MAX_AGE_SECONDS:
                try:
                    os.remove(filepath)
                    removed += 1
                except OSError:
                    pass
                continue
            # Fresh file — fall back to PID liveness check
            middle = entry[len("kirocrew_sandbox_"):-len(suffix)]
            pid_str = middle.split("_", 1)[0]
            if not pid_str.isdigit():
                continue
            # Liveness probe via the shim — NEVER raw os.kill(pid, 0), which
            # TERMINATES the target process on Windows (see platform_compat).
            try:
                alive = platform_compat.pid_exists(int(pid_str))
            except OverflowError:
                alive = False  # absurd PID digits from a corrupt filename — stale
            if not alive:
                try:
                    os.remove(filepath)
                    removed += 1
                except OSError:
                    pass

    # ── Sweep legacy /tmp/kirocrew_sandbox_*.py (age only, no PID segment) ──
    if os.path.isdir(legacy_dir):
        try:
            with os.scandir(legacy_dir) as it:
                for dentry in it:
                    if not dentry.name.startswith("kirocrew_sandbox_"):
                        continue
                    if not dentry.name.endswith(".py"):
                        continue
                    try:
                        mtime = dentry.stat().st_mtime
                    except OSError:
                        continue
                    if (now - mtime) > _LAUNCHER_MAX_AGE_SECONDS:
                        try:
                            os.remove(dentry.path)
                            removed += 1
                        except OSError:
                            pass
        except OSError:
            pass

    return removed
