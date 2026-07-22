"""Sandbox availability probes, backend cache, and shared credential lists.

Foundation module of the ``sandbox`` package (imported by every sibling). Holds
the sensitive-path / env-prefix constants, the platform-context sandbox-policy
accessor, the Linux user-namespace + macOS ``sandbox-exec`` availability probes,
the never-block-on-loop background warm thread, and the cached backend selector
(``detect_backend`` / ``reset_backend``).

Single owner of the mutable process caches ``_backend`` /
``_last_unshare_failure`` / ``_warm_thread``; the package shim republishes
``reset_backend`` by reference so a reset through ``kiro_crew.sandbox`` clears
the real cache here.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import errno
import functools
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

from kiro_crew.platform import current_context

logger = logging.getLogger("kiro_crew.sandbox")


# Sensitive directories to hide from the agent subprocess tree.
# "strict" mode hides all; "standard" mode only hides non-workflow dirs.
_STRICT_DIRS: list[str] = [
    ".aws",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".config/gh",
    ".azure",
    ".docker",
    ".kube",
]

_STANDARD_DIRS: list[str] = [
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker",
]

# CC mode: hides all credential dirs including .aws, but selectively exposes
# .aws/config (needed for credential_process → Bedrock auth). All other .aws
# files (credentials, sso cache, etc.) are filesystem-hidden via bind mount.
_CC_DIRS: list[str] = [
    ".aws",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker",
    ".kube",
]

# CC mode: files to expose read-only inside otherwise-hidden dirs.
# After hiding the parent dir, these are recreated with original content.
_CC_EXPOSE_FILES: list[str] = [
    ".aws/config",
]

# CC mode: individual sensitive files that aren't inside the hidden dirs above.
# These require file-level (not directory-level) sandbox enforcement.
_CC_FILES: list[str] = [
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    ".kirocrew/.env",
]

# Sensitive env var prefixes to scrub from the child environment.
# Scrubbed in ALL modes (standard + strict) — credential_process reads
# from ~/.aws/config, not env vars, so scrubbing is always safe.
_SENSITIVE_ENV_PREFIXES: list[str] = [
    "AWS_SECRET",
    "AWS_SESSION",
    "SSH_AUTH_SOCK",
    "GNUPGHOME",
    "GIT_ASKPASS",
]

# Python interpreter env that must NOT leak into a *foreign* Python subprocess
# launched under the sandbox (e.g. the MCP servers kiro-cli spawns, such as
# ord-mcp, which bundle their own interpreter + deps). KiroCrew's runtime may
# export PYTHONPATH pointing at its own site-packages; a foreign server that
# inherits it prepends KiroCrew's site-packages to sys.path and imports
# KiroCrew's fastmcp/cryptography instead of its own -> ABI collision + init
# hang. Stripped ONLY when the caller passes ``strip_python_env=True`` (the
# kiro-cli / agent spawn path). It is deliberately NOT part of
# ``_SENSITIVE_ENV_PREFIXES`` because KiroCrew's OWN sandboxed Python
# subprocesses (cron scripts, app backends, code-review workers) import
# ``kiro_crew`` via PYTHONPATH and would break if it were stripped.
_PYTHON_ENV_PREFIXES: list[str] = [
    "PYTHONPATH",
    "PYTHONHOME",
]

# Gateway-owned credentials must never reach agent-influenced subprocesses.
# This list feeds the cc/strict launcher scrub, the always-on ``scrub_env``
# parent scrub, and ``scrub_agent_denied_env`` — the parent-level scrub the ACP
# spawn paths apply on EVERY tier (incl. the default auto/standard tier, whose
# launcher does not strip these keys). Loader coverage is pinned by regression
# test.
_AGENT_DENIED_ENV_KEYS: list[str] = [
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_USER_TOKEN",
    "WECOM_BOT_ID",
    "WECOM_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "KIROCREW_OWNER_ID",
]


# ── Platform context accessor ──


def _sandbox_policy():
    """Return the active context's SandboxPolicy adapter.

    The Default adapter delegates to ``_STRICT_DIRS`` / ``_CC_DIRS`` above, so a
    standalone process gets today's exact lists; the Amazon companion extends
    them.
    """
    return current_context().sandbox


# ── Availability probes ──


# unshare(2) flags for the userns probe.
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNS = 0x00020000

# Errnos that indicate a TRANSIENT resource failure (fork/CDLL under momentary
# pressure) — the kernel supports user namespaces, we just couldn't verify it
# right now. These must never be treated as "this host has no sandbox backend"
# (incident 2026-07-18: one EAGAIN during a cron spawn burst fail-closed every
# subsequent spawn for an hour because the failed probe result was cached).
_TRANSIENT_PROBE_ERRNOS = frozenset(
    {errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.ENOSPC}
)

# Delay before the single in-probe retry on a transient failure.
_PROBE_TRANSIENT_RETRY_DELAY_SECS = 0.05

# Detail of the most recent failed userns probe: (transient, reason).
# ``None`` means the last probe succeeded (or none has run yet). Consumed by
# detect_backend() for cache policy and by wrap_argv() for error reporting.
_last_unshare_failure: tuple[bool, str] | None = None


def _probe_unshare_once() -> tuple[bool, bool, str]:
    """One unshare(CLONE_NEWUSER|CLONE_NEWNS) attempt: ``(ok, transient, reason)``.

    The forked child exits with the unshare(2) errno so the parent can
    distinguish a kernel that refuses user namespaces (EPERM/EINVAL/ENOSYS —
    permanent) from momentary resource exhaustion (EAGAIN/ENOMEM/... —
    transient).
    """
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _libc.unshare.argtypes = [ctypes.c_int]
        _libc.unshare.restype = ctypes.c_int
    except OSError as exc:
        return (False, exc.errno in _TRANSIENT_PROBE_ERRNOS, f"libc load failed: {exc}")
    except Exception as exc:  # find_library returning junk, ABI issues, ...
        return (False, False, f"libc load failed: {exc}")
    try:
        pid = os.fork()
    except OSError as exc:
        name = errno.errorcode.get(exc.errno or 0, "?")
        transient = exc.errno in _TRANSIENT_PROBE_ERRNOS
        return (False, transient, f"fork failed with errno {exc.errno} ({name})")
    if pid == 0:
        try:
            ret = _libc.unshare(_CLONE_NEWUSER | _CLONE_NEWNS)
            err = ctypes.get_errno() if ret != 0 else 0
            os._exit(0 if ret == 0 else (err if 0 < err < 256 else 1))
        except BaseException:
            os._exit(1)
    try:
        _, status = os.waitpid(pid, 0)
    except OSError as exc:
        return (False, True, f"waitpid failed: {exc}")
    if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
        return (True, False, "ok")
    if not os.WIFEXITED(status):
        sig = os.WTERMSIG(status) if os.WIFSIGNALED(status) else 0
        return (
            False,
            True,  # child killed by signal is always transient
            f"probe child killed by signal {sig}",
        )
    child_errno = os.WEXITSTATUS(status)
    name = errno.errorcode.get(child_errno, "?")
    transient = child_errno in _TRANSIENT_PROBE_ERRNOS
    return (
        False,
        transient,
        f"unshare(CLONE_NEWUSER|CLONE_NEWNS) failed with errno {child_errno} ({name})",
    )


# ── Background warm thread (never-block-on-loop policy) ──
# The event loop NEVER executes fork/waitpid/sleep for the probe. On-loop
# callers with a cold cache get an immediate transient "none" (fail-closed,
# self-heals in ms) and fire a background daemon thread that populates the
# cache off-loop. Boot sites call prewarm_backend() to fill the cache before
# any on-loop caller ever reaches detect_backend(), so the transient path is
# typically never hit in production.

_warm_thread: threading.Thread | None = None


def _background_warm() -> None:
    """Run the probe off-loop and populate the cache. Thread target."""
    global _backend, _last_unshare_failure
    for attempt in (1, 2):
        ok, transient, reason = _probe_unshare_once()
        if ok:
            _last_unshare_failure = None
            _backend = "namespace"
            logger.info("Background warm: sandbox backend = namespace")
            return
        _last_unshare_failure = (transient, reason)
        if not transient:
            logger.warning("Background warm: probe permanent failure: %s", reason)
            _backend = "none"
            return
        logger.warning("Background warm: probe transient (attempt %d/2): %s", attempt, reason)
        if attempt == 1:
            time.sleep(_PROBE_TRANSIENT_RETRY_DELAY_SECS)
    # Both attempts transient — leave cache uncached (None) so next call re-tries
    logger.warning("Background warm: both attempts transient, cache stays cold")


def _kick_background_warm() -> None:
    """Start the background warm thread if not already running."""
    global _warm_thread
    if _warm_thread is not None and _warm_thread.is_alive():
        return  # dedupe: warm already in progress
    _warm_thread = threading.Thread(
        target=_background_warm, name="sandbox-probe-warm", daemon=True
    )
    _warm_thread.start()


def prewarm_backend() -> None:
    """Fire-and-forget boot hook: start background probe to fill the cache.

    Call early in gateway startup (slack/gateway.py, mcp_gateway/gatewayd.py)
    so the cache is warm before any on-loop spawn path reaches detect_backend().
    """
    if sys.platform != "linux":
        return  # probes are Linux-only
    _kick_background_warm()


def _probe_unshare() -> bool:
    """Return True if user + mount namespaces work (Linux).

    Failures are logged with their errno and classified transient vs
    permanent in :data:`_last_unshare_failure`; a transient failure gets one
    immediate retry (off-loop only).

    **Never-block-on-loop invariant**: when called from a running asyncio
    event loop with a cold cache, this function does NOT probe — it fires
    ``_kick_background_warm()`` and returns False with a transient reason.
    The background thread populates the cache in ms; the next spawn re-checks
    and finds a warm cache. Boot prewarm ensures this path is rarely hit.

    Callers deciding cache policy (detect_backend) MUST consult the
    classification — a transient result is not evidence that the host lacks
    a sandbox backend.
    """
    global _last_unshare_failure
    if sys.platform != "linux":
        _last_unshare_failure = (False, "not Linux")
        return False

    # Fast path: the cache already proved user namespaces work -- no probe
    # needed. Keeps on-loop callers correct after prewarm instead of
    # deferring and returning False.
    if _backend == "namespace":
        return True

    # Detect running event loop — governs whether we probe directly or defer.
    on_loop = False
    try:
        asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        pass

    if on_loop:
        # NEVER probe on the event loop. Kick background warm and fail transient.
        _kick_background_warm()
        _last_unshare_failure = (
            True,
            "probe deferred to background thread (cold cache on event loop); "
            "cache warms in ms — retry",
        )
        return False

    # Off-loop: direct probe with one retry on transient failure.
    for attempt in (1, 2):
        ok, transient, reason = _probe_unshare_once()
        if ok:
            _last_unshare_failure = None
            return True
        _last_unshare_failure = (transient, reason)
        if not transient:
            logger.warning("userns probe failed (permanent): %s", reason)
            return False
        logger.warning("userns probe failed (transient, attempt %d/2): %s", attempt, reason)
        if attempt == 1:
            time.sleep(_PROBE_TRANSIENT_RETRY_DELAY_SECS)
    return False


def userns_available() -> bool:
    """Public: True if unprivileged user + mount namespaces work on this host.

    Stable cross-module entry point for the namespace-support probe, shared by
    the OS-level sandbox here and the JailProvider extension point
    (``platform/interfaces.py``), so consumers do not depend on the private
    ``_probe_unshare`` name.
    """
    return _probe_unshare()


@functools.lru_cache(maxsize=1)
def is_wsl() -> bool:
    """Public: True if this Linux host is running under Windows Subsystem for Linux.

    Centralized host probe (parallel to :func:`userns_available`) so consumers
    never re-implement WSL detection. WSL2 *does* expose working user
    namespaces, so :func:`userns_available` returns True there — but WSL's
    networking is a NAT'd virtual interface, and rootless-namespace jails
    (slirp4netns) make agentic command networking unreachable. A jail backend
    (JailProvider) uses this to opt WSL out of jailing.

    Detection (cheap, in order): the ``WSL_DISTRO_NAME`` / ``WSL_INTEROP`` env
    vars WSL injects into every login shell, then the ``microsoft`` marker the
    WSL kernel stamps into ``/proc/version`` (covers WSL1 + WSL2, both Microsoft
    and -microsoft-standard builds). Result is cached — the host's WSL-ness does
    not change within a process. Always False off Linux.
    """
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def _probe_sandbox_exec() -> bool:
    """Return True if macOS ``sandbox-exec`` actually works.

    Uses a file-based profile with fixed system paths for both
    ``sandbox-exec`` and its ``/usr/bin/true`` target. The probe tests with an
    ``(allow default)`` profile to detect kernel-level rejection, not merely
    executable presence.
    """
    if sys.platform != "darwin":
        return False
    # Decide empirically — do NOT hard-code a macOS version cutoff. An earlier
    # `major >= 26 → return False` gate was wrong: sandbox-exec + the Seatbelt
    # kernel subsystem still work on macOS 26 (Tahoe) — verified that the real
    # generated profile compiles, runs kiro-cli, AND enforces (a strict profile
    # denies `cat ~/.aws/config`). The gate disabled a working sandbox and forced
    # the agent onto the fail-closed no-isolation path. The probe below already
    # detects a genuinely-broken sandbox-exec on any host/version, so trust it.
    # Note: sandbox-exec / sandbox_init() are marked "deprecated" in headers
    # since macOS 10.8, but the Seatbelt kernel subsystem they use is NOT
    # deprecated — it's the same enforcement layer that backs App Sandbox and
    # iOS. All major AI CLIs (Claude Code, Codex, Gemini) rely on it.
    # Rather than hard-coding version checks, we probe empirically below.
    sb = "/usr/bin/sandbox-exec"
    if not os.path.exists(sb):
        return False
    # Probe with a file-based (allow default) profile against a TRUSTED, fixed
    # system binary. We deliberately do NOT probe the (user-writable) kiro-cli
    # binary: the probe runs under (allow default) with KiroCrew's credentials,
    # so exec'ing a user-writable target here could run a planted payload
    # effectively unsandboxed. The probe only needs to confirm the kernel
    # accepts sandbox_apply, which /usr/bin/true validates safely.
    target = "/usr/bin/true"
    if not os.path.exists(target):
        return False
    fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="kirocrew_probe_")
    try:
        os.write(fd, b"(version 1)(allow default)")
        os.close(fd)
        r = subprocess.run(
            [sb, "-f", profile_path, target],
            capture_output=True,
            timeout=5,
        )
        if r.returncode != 0:
            logger.warning(
                "sandbox-exec probe failed (exit %d): %s",
                r.returncode,
                r.stderr.decode(errors="replace").strip(),
            )
        return r.returncode == 0
    except Exception as exc:
        logger.debug("sandbox-exec probe failed: %s", exc)
        return False
    finally:
        try:
            os.unlink(profile_path)
        except OSError:
            pass


# ── Backend cache (single owner; shim republishes reset_backend by reference) ──
_backend: str | None = None  # "namespace", "sandbox-exec", "none"


def detect_backend(config_mode: str = "auto") -> str:
    """Detect the best available sandbox backend.

    Cache policy (incident 2026-07-18 — one transient fork failure poisoned
    the cache and fail-closed every spawn for an hour until restart):

    - A positive result (``"namespace"``/``"sandbox-exec"``) is cached for the
      process lifetime — kernel capability does not change while running.
    - ``"none"`` is cached ONLY when the userns probe failure looks permanent
      (kernel refuses user namespaces: EPERM/EINVAL/ENOSYS). A transient
      resource failure (fork EAGAIN, EMFILE, ...) is never cached — the next
      spawn re-probes and self-heals.
    - ``config_mode="off"`` short-circuits to ``"none"`` without probing and
      without touching the cache. All other modes share one cache entry:
      backend capability is mode-independent, so mode alternation no longer
      forces pointless re-probes.
    """
    global _backend
    if config_mode == "off":
        return "none"
    if _backend is not None:
        return _backend
    if userns_available():
        _backend = "namespace"
    elif _probe_sandbox_exec():
        _backend = "sandbox-exec"
    else:
        transient, reason = _last_unshare_failure or (False, "no probe detail recorded")
        if transient:
            logger.warning(
                "Sandbox backend probe failed transiently (%s); result NOT cached — "
                "the next spawn re-probes",
                reason,
            )
            return "none"
        _backend = "none"
    logger.info("Sandbox backend: %s (config_mode=%s)", _backend, config_mode)
    return _backend


def reset_backend() -> None:
    """Reset cached backend (for testing or config change)."""
    global _backend, _last_unshare_failure
    _backend = None
    _last_unshare_failure = None
