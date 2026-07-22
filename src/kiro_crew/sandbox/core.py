"""Public sandbox entrypoints: ``wrap_argv`` (OS-level sandbox selection),
the credential env scrubbers, and the ``sandboxed_spawn_argv`` chokepoint."""

from __future__ import annotations

import logging
import os
import sys

from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE

from . import backends, cgroups, launcher, policy, seatbelt
from .backends import (
    _AGENT_DENIED_ENV_KEYS,
    _PYTHON_ENV_PREFIXES,
    _SENSITIVE_ENV_PREFIXES,
)

logger = logging.getLogger("kiro_crew.sandbox")


def _warn_no_isolation(mode: str) -> None:
    """Loudly surface that the agent subprocess is running WITHOUT OS-level
    isolation, so the fallback is never silent (CSE SEC-009).

    When no sandbox backend is available the credential paths (``~/.aws``,
    ``~/.ssh``, ...) are visible to the (untrusted) agent subprocess and only
    the bypassable app-level ``security.py`` checks remain. This is a real
    degradation of the security posture, so it is logged as a WARNING unless
    the operator has explicitly acknowledged it via
    ``agent.sandbox_allow_no_isolation``. Emitted once per process.
    """
    if getattr(wrap_argv, "_warned", False):
        return
    wrap_argv._warned = True  # type: ignore[attr-defined]
    if policy._allow_no_isolation():
        logger.info(
            "OS-level sandbox unavailable (mode=%s); running WITHOUT credential "
            "isolation. Operator opted in via agent.sandbox_allow_no_isolation; "
            "app-level checks are the only remaining boundary.",
            mode,
        )
        return
    logger.warning(
        "SECURITY: no OS-level sandbox backend is available on this host "
        "(mode=%s), so the agent subprocess runs WITHOUT credential isolation — "
        "~/.aws, ~/.ssh and other secrets are readable by it and only the "
        "bypassable app-level security.py checks remain. Install a supported "
        "sandbox (Linux user namespaces, or macOS < 26 sandbox-exec), or set "
        "agent.sandbox_allow_no_isolation=true in ~/.kirocrew/config.json to "
        "acknowledge the risk and silence this warning.",
        mode,
    )


def wrap_argv(
    argv: list[str],
    mode: str = "auto",
    *,
    strip_python_env: bool = False,
) -> tuple[list[str], str | None]:
    """Wrap a command argv with OS-level sandbox if available.

    Args:
        argv: Original command + args.
        mode: ``"auto"``/``"standard"`` (expose .aws/.ssh/.kube),
              ``"cc"`` (hide .aws but expose .aws/config for Bedrock auth),
              ``"strict"`` (hide everything), ``"off"`` (no sandbox).

    Returns:
        (wrapped_argv, cleanup_path_or_None).
        *cleanup_path* is a temp file to delete after the child exits
        (macOS seatbelt profile or Linux launcher script).
        ``None`` when no cleanup is needed.

    Raises:
        RuntimeError: When no sandbox backend is available, mode is not "off",
            and ``agent.sandbox_allow_unsandboxed_exec`` is False (default).
            This is the fail-closed behavior — the agent subprocess is NOT
            allowed to run without OS-level isolation unless explicitly opted in.
    """
    # Governance ordinal floor: a policy/profile may require a MINIMUM sandbox
    # tier (off < standard < cc < strict).  Clamp the requested mode up to that
    # floor before resolving the level — so an enterprise "min_level: cc" makes
    # even a mode="off" call run confined.  Cheap no-op when ungoverned.
    mode = policy._clamp_sandbox_mode(mode)

    if mode == "off":
        return argv, None

    # "auto"/"standard" allows git-over-SSH, AWS CLI, kubectl.
    # "cc" hides .aws (exposes only .aws/config for Bedrock credential_process).
    # "strict" hides everything.
    if mode == "strict":
        sandbox_level = "strict"
    elif mode == "cc":
        sandbox_level = "cc"
    else:
        sandbox_level = "standard"

    # macOS sandbox mutual exclusion: kiro-cli >= 2.13's internal sandbox cannot
    # initialize nested inside KiroCrew's seatbelt (kernel EPERM even under an
    # allow-all outer profile), so exactly one layer can own isolation. When
    # kiro's internal sandbox is enabled, it is that layer for kiro-cli spawns;
    # KiroCrew's sandbox stays on for everything else and whenever kiro's is off.
    # Checked before backend detection so delegation also applies where our own
    # probe found no backend. macOS only — Linux namespace isolation is
    # unaffected.
    if sys.platform == "darwin" and policy._spawns_kiro_cli(argv) and policy.kiro_internal_sandbox_enabled():
        return policy._delegate_to_kiro_internal_sandbox(
            argv, sandbox_level, strip_python_env=strip_python_env
        )

    backend = backends.detect_backend(config_mode=mode)

    if backend == "namespace":
        wrapped = launcher.namespace_argv(argv, sandbox_level, strip_python_env=strip_python_env)
        # The launcher script is argv[1] — caller should clean it up
        return wrapped, wrapped[1]
    if backend == "sandbox-exec":
        return seatbelt.sandbox_exec_argv(argv, sandbox_level, strip_python_env=strip_python_env)

    if backend == "none":
        # FAIL-CLOSED: refuse to execute without sandbox unless explicitly opted in.
        # This addresses pentest finding P472042906 — the previous behavior silently
        # returned unmodified argv, allowing the agent subprocess to access all
        # credential paths without any OS-level isolation.
        if not policy._allow_unsandboxed_exec():
            transient, probe_reason = backends._last_unshare_failure or (
                False,
                "no probe detail recorded",
            )
            if transient:
                guidance = (
                    "This probe failure looks TRANSIENT (momentary resource "
                    "pressure) — it is not cached and the next spawn re-probes "
                    "automatically. Do NOT disable the sandbox for this; retry "
                    "instead. "
                )
            else:
                guidance = (
                    "If this host genuinely lacks a sandbox backend, set "
                    "agent.sandbox_allow_unsandboxed_exec=true in "
                    "~/.kirocrew/config.json to explicitly allow unsandboxed "
                    "execution, or install a supported sandbox backend "
                    "(Linux user namespaces, or macOS sandbox-exec). "
                )
            # Emit SEL audit event for this security-relevant denial so it
            # appears in the tamper-evident audit log (AutoSDE requirement).
            try:
                from kiro_crew.sel import sel  # circular import: sandbox is low-level

                sel().log_tool_invocation(
                    session_key="sandbox",
                    agent="system",
                    source="sandbox.wrap_argv",
                    tool_name=argv[0] if argv else "unknown",
                    tool_kind="subprocess",
                    outcome="denied",
                    error=(
                        "No sandbox backend available and allow_unsandboxed_exec "
                        f"is not set (probe: {probe_reason})"
                    ),
                )
            except Exception:
                logger.warning("Failed to emit SEL audit event for sandbox denial", exc_info=True)
            raise RuntimeError(
                "Sandbox backend unavailable and allow_unsandboxed_exec is not set. "
                "No OS-level sandbox backend is available on this host, and the "
                "agent subprocess cannot be safely isolated. "
                f"Probe detail: {probe_reason}. " + guidance
            )
        # Opted in: warn (or info) and return unmodified argv
        _warn_no_isolation(mode)
    return argv, None


# Environment keys always scrubbed from an agent-influenced subprocess'
# environment, regardless of sandbox backend. These are the credential-bearing
# names that must never reach a spawn whose command, arguments, or working
# directory the agent (or a hostile MCP-config / repo) can influence. The OS
# sandbox launcher already drops these when a backend is present (see
# ``ENV_PREFIXES`` in ``namespace_argv`` / ``sandbox_exec_argv``), but scrubbing
# at the parent level too means the guarantee holds even on the opted-in
# ``sandbox_allow_unsandboxed_exec`` fail-open path where no launcher runs.
# Prefix match via ``startswith`` (mirrors the launcher's ENV_PREFIXES check).
_SPAWN_SCRUB_ENV_PREFIXES: list[str] = list(_SENSITIVE_ENV_PREFIXES) + list(_AGENT_DENIED_ENV_KEYS)


def scrub_env(
    env: dict[str, str] | None = None,
    *,
    extra_prefixes: list[str] | None = None,
) -> dict[str, str]:
    """Return a copy of *env* (default ``os.environ``) with credential-bearing
    keys removed.

    Drops every key whose name starts with one of ``_SPAWN_SCRUB_ENV_PREFIXES``
    (AWS secret/session vars, SSH_AUTH_SOCK, GNUPGHOME, GIT_ASKPASS, and the
    Slack/owner tokens seeded into ``os.environ`` for trusted children). Used to
    build the environment for agent-influenced spawns so a spawned process
    cannot read secrets straight out of the inherited environment.

    *extra_prefixes* adds more name prefixes to drop (e.g.
    ``_PYTHON_ENV_PREFIXES`` when the spawn is a foreign Python child).
    """
    prefixes = _SPAWN_SCRUB_ENV_PREFIXES + (extra_prefixes or [])
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if not any(k.startswith(p) for p in prefixes)}


def scrub_agent_denied_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of *env* with gateway-owned channel credentials removed.

    Drops every key matching ``_AGENT_DENIED_ENV_KEYS`` — the Slack/WeCom/
    Telegram tokens and owner id that ``config/loader.load_credentials()`` seeds
    into ``os.environ`` for trusted children only.

    This is the PARENT-level complement to the OS-sandbox launcher scrub. The
    launcher (``namespace_argv`` / ``sandbox_exec_argv``) only strips these keys
    for the ``cc``/``strict`` tiers; on the default ``auto``/``standard`` tier
    they are left in place. The production ACP spawn paths
    (:meth:`AcpRuntime._spawn` / :meth:`AcpClient._spawn`) copy a raw
    ``os.environ`` and call :func:`wrap_argv` directly (not
    :func:`sandboxed_spawn_argv`), so without this scrub the channel credentials
    would be inherited by the agent subprocess on the default tier — reachable
    via ``env`` / ``os.environ`` and usable to control those channel identities
    outside KiroCrew.

    Unlike :func:`scrub_env`, this deliberately does NOT strip
    ``_SENSITIVE_ENV_PREFIXES`` (AWS/SSH/GPG): the ``standard`` sandbox is
    designed to leave git-over-SSH, the AWS CLI and kubectl usable, so those
    vars must survive the parent scrub. Prefix match via ``startswith`` mirrors
    the launcher's ENV_PREFIXES check.
    """
    return {
        k: v
        for k, v in env.items()
        if not any(k.startswith(p) for p in _AGENT_DENIED_ENV_KEYS)
    }


def sandboxed_spawn_argv(
    argv: list[str],
    mode: str = "standard",
    *,
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
) -> tuple[list[str], dict[str, str], str | None]:
    """Single chokepoint for agent-influenced subprocess spawns.

    Wraps *argv* with the OS-level sandbox (:func:`wrap_argv`) AND returns a
    credential-scrubbed environment (:func:`scrub_env`), so every caller gets
    both the filesystem-isolation and the environment-hiding layer without
    having to remember to apply each separately. This is the wrapper the
    subprocess-spawn audit test (``test/test_spawn_audit.py``) requires every
    agent-influenced spawn in ``src/kiro_crew`` to route through.

    Args:
        argv: Original command + args.
        mode: Sandbox mode passed to :func:`wrap_argv` (default ``"standard"``:
            hides non-workflow credential dirs while leaving git-over-SSH and
            the AWS CLI usable).
        env: Base environment to scrub (default ``os.environ``). Pass a
            pre-augmented env (e.g. with a resolved ``PATH``) to have the scrub
            applied on top of it.
        strip_python_env: Strip ``PYTHONPATH``/``PYTHONHOME`` so a foreign
            Python child does not inherit KiroCrew's interpreter paths. Applied
            BOTH inside :func:`wrap_argv`'s launcher AND to the returned env, so
            the strip holds even on the fail-open path where no launcher runs.

    Returns:
        ``(wrapped_argv, scrubbed_env, cleanup_path_or_None)``. The caller MUST
        pass *scrubbed_env* as the subprocess ``env=`` and unlink *cleanup_path*
        (a temp launcher/profile) after the child exits.
    """
    wrapped, cleanup = wrap_argv(argv, mode=mode, strip_python_env=strip_python_env)
    # cgroup v2 scope (OUTERMOST layer): bound the spawned process tree with
    # pids.max + memory.max. Applied here so every sandboxed_spawn_argv caller
    # gets the fork-bomb / memory-DoS ceiling without threading it through each
    # site. No-op (with a one-time loud warning) where cgroup delegation is
    # unavailable. Safe re: the cleanup path — that is returned separately, not
    # re-derived from an argv index, so prepending systemd-run does not disturb
    # it. See docs/resource-protection.md (Talos bdf0d7e5).
    wrapped = cgroups.cgroup_scope_argv(wrapped)
    # ``wrap_argv`` only strips PYTHONPATH/PYTHONHOME inside the launcher script,
    # so on the fail-open path (no sandbox backend, opted-in unsandboxed exec) it
    # returns argv unmodified and the strip never happens. Apply the same strip
    # to the scrubbed env here so ``strip_python_env=True`` holds regardless of
    # whether a backend is available.
    extra = _PYTHON_ENV_PREFIXES if strip_python_env else None
    scrubbed = scrub_env(env, extra_prefixes=extra)
    # Positive-identity marker for the orphan sweep: every tree spawned through
    # this chokepoint (and its descendants, via env inheritance) is identifiable
    # as KiroCrew-spawned even when its cmdline carries no KiroCrew fingerprint
    # (e.g. ``npx @playwright/mcp``).
    scrubbed[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
    return wrapped, scrubbed, cleanup
