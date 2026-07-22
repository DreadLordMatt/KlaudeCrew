"""Config-driven sandbox policy: the no-isolation / unsandboxed-exec opt-ins,
governance ordinal-floor clamping, and the macOS kiro-cli internal-sandbox
mutual-exclusion delegation."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import seatbelt

logger = logging.getLogger("kiro_crew.sandbox")


# kiro-cli >= 2.13 ships its own internal agent sandbox, toggled by the
# "sandbox" key in this settings file. On macOS its in-process seatbelt init
# cannot nest inside KiroCrew's sandbox-exec wrap — the kernel returns EPERM
# even under an (allow default) outer profile (verified 2026-07-20 on
# macOS 26.5.2 / kiro-cli 2.13.0). Exactly one sandbox layer can be active per
# spawn, so on macOS the layers are mutually exclusive:
# kiro's internal sandbox ON  -> KiroCrew's seatbelt OFF for kiro-cli spawns
# kiro's internal sandbox OFF -> KiroCrew's seatbelt ON (unchanged default)
# (``~/.kiro`` is the kiro-cli backend's own directory, distinct from
# ``~/.kirocrew``; the filename is the literal kiro-cli ships.)
_KIRO_INTERNAL_SETTINGS_PATH = "~/.kiro/settings/amazon-internal.json"
_KIRO_INTERNAL_SANDBOX_KEY = "sandbox"

# One loud warning per process for the delegation decision (per-spawn logs
# would spam warm-pool refills); every delegated spawn is still SEL-audited.
_kiro_delegation_warned = False


def kiro_internal_sandbox_enabled() -> bool:
    """True when kiro-cli's own internal agent sandbox is enabled.

    Reads the ``"sandbox"`` key from ``~/.kiro/settings/amazon-internal.json``
    (kiro-cli >= 2.13). Absent file, missing key, or parse failure all return
    False, which keeps KiroCrew's own sandbox engaged — failure resolves
    toward our audited isolation layer, never toward no isolation.

    Deliberately uncached: it is one small-file read per spawn, and caching
    would make a settings flip require a gateway restart (mirrors the
    uncached ``_resolve_kiro_bin`` rationale).
    """
    # Deferred import: sandbox.py is a low-level leaf that deliberately avoids a
    # top-level dependency on hooks (hooks imports sandbox at call time). The
    # read is routed through hooks.safe_read_file (security-controls): the file
    # is user-writable, so the read gets is_sensitive_path() on the RESOLVED
    # target (a symlink into ~/.aws etc. is refused through the link) plus
    # O_NOFOLLOW against a TOCTOU swap of the final component.
    from kiro_crew.hooks import safe_read_file

    try:
        data = json.loads(safe_read_file(_KIRO_INTERNAL_SETTINGS_PATH))
        if not isinstance(data, dict):
            # Valid-but-non-object JSON ([], "str", null, 123) must also
            # resolve toward KiroCrew's own sandbox, not raise.
            return False
        return bool(data.get(_KIRO_INTERNAL_SANDBOX_KEY, False))
    except (OSError, ValueError, RuntimeError):
        # OSError covers missing file / EACCES / PermissionError (sensitive
        # or symlinked target refused by hooks); ValueError covers JSON
        # decode; RuntimeError covers home-directory resolution failure.
        # Every failure resolves toward KiroCrew's own sandbox.
        return False


def _spawns_kiro_cli(argv: list[str]) -> bool:
    """True when *argv* launches kiro-cli (by basename, the same convention
    as ``_resolve_kiro_bin``).

    Only the kiro-cli spawn may delegate isolation to kiro's internal
    sandbox — every other agent-influenced spawn (e.g. an MCP probe or a
    cron script) has no internal sandbox of its own and MUST keep KiroCrew's
    wrap regardless of the kiro settings file.
    """
    return bool(argv) and Path(argv[0]).name == "kiro-cli"


def _delegate_to_kiro_internal_sandbox(
    argv: list[str],
    sandbox_level: str,
    *,
    strip_python_env: bool = False,
) -> tuple[list[str], str | None]:
    """macOS sandbox mutual exclusion: kiro-cli's internal sandbox owns
    isolation for this spawn; KiroCrew's seatbelt is skipped.

    This is NOT the forbidden silent unsandboxed fallback: the child still
    runs under an OS sandbox (kiro's own), the delegation is config-driven and
    deterministic (never a reaction to a wrap failure), it is logged loudly
    once per process, and every delegated spawn is SEL-audited on an
    audit-or-deny basis — if the audit event cannot be written, the delegation
    is refused and the spawn falls back to KiroCrew's own seatbelt. The env
    scrub is applied exactly as the seatbelt wrap would have applied it.

    Deliberately does NOT resolve the real kiro binary: the toolbox shim is
    part of kiro's own sandbox mechanism on this path, so bypassing it here
    would defeat the delegated layer.
    """
    global _kiro_delegation_warned
    try:
        # circular import (pre-emptive, layering): sandbox.py is a low-level
        # leaf imported at module level by many modules including subprocess
        # entry points. A top-level dep on sel would invert the low-level ->
        # high-level layering; deferring to this rarely-taken path keeps
        # sandbox leaf-pure.
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="sandbox",
            agent="system",
            source="sandbox.wrap_argv",
            tool_name=argv[0] if argv else "unknown",
            tool_kind="subprocess",
            outcome="delegated",
            resources=(
                "macOS sandbox mutual exclusion: kiro internal sandbox on -> "
                "KiroCrew seatbelt off for this kiro-cli spawn"
            ),
            # audit-or-deny: written synchronously; a filesystem failure
            # re-raises so an unaudited delegation can never proceed.
            critical=True,
        )
    except Exception:
        # Fail closed (security-controls): a security delegation that cannot
        # be audited does not happen. Fall back to KiroCrew's own seatbelt —
        # the always-safe, audited-by-default layer. If kiro's internal
        # sandbox is enabled this spawn will then fail with the nested-
        # sandbox EPERM rather than run unaudited: safety over availability
        # while SEL is broken.
        logger.warning(
            "SEL audit failed for sandbox delegation — refusing unaudited "
            "delegation; falling back to KiroCrew's seatbelt",
            exc_info=True,
        )
        return seatbelt.sandbox_exec_argv(argv, sandbox_level, strip_python_env=strip_python_env)
    # SEL audit succeeded — delegation is actually proceeding. Only now
    # consume the warn-once flag (a SEL-failed attempt above fell back to
    # seatbelt and must not burn the warning for the first real delegation).
    if not _kiro_delegation_warned:
        _kiro_delegation_warned = True
        logger.warning(
            "SECURITY: kiro-cli's internal sandbox is enabled (%s) — delegating "
            "agent isolation to it and skipping KiroCrew's seatbelt for kiro-cli "
            "spawns (nested seatbelt is impossible on macOS; exactly one layer "
            "can be active). To use KiroCrew's sandbox instead, set "
            '{"sandbox": false} in that file. Env scrubbing still applies.',
            _KIRO_INTERNAL_SETTINGS_PATH,
        )
    unset_args = seatbelt._sandbox_env_unset_args(sandbox_level, strip_python_env)
    if unset_args:
        return ["env", *unset_args, *argv], None
    return list(argv), None


def _allow_no_isolation() -> bool:
    """Whether the operator has explicitly opted into running the agent
    subprocess without OS-level credential isolation.

    Read lazily from config to avoid an import cycle with the config loader
    (sandbox.py is a low-level dependency of much of the codebase).
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return bool(getattr(KiroCrewConfig.load().agent, "sandbox_allow_no_isolation", False))
    except Exception:
        return False


def _allow_unsandboxed_exec() -> bool:
    """Whether the operator has explicitly opted into allowing execution
    without ANY sandbox backend (fail-open behavior).

    When False (default), wrap_argv will RAISE instead of returning unmodified
    argv when no sandbox backend is available. This is the fail-closed behavior
    required by pentest finding P472042906.

    Read lazily from config to avoid an import cycle with the config loader.
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return bool(getattr(KiroCrewConfig.load().agent, "sandbox_allow_unsandboxed_exec", False))
    except Exception:
        return False


# ordinal scale: ``auto`` is an alias that resolves to ``standard`` below.  Only
# this alias mapping lives here; the strictness ORDER is owned solely by
# governance._ORDINAL_SCALES["sandbox"] (the single source of truth) — we never
# re-encode the order, so a new tier added there is honoured here without edit.
_SANDBOX_MODE_ALIASES = {"auto": "standard"}


def _clamp_sandbox_mode(mode: str) -> str:
    """Clamp *mode* UP to the governed ``sandbox.min_level`` floor, if any.

    Derives strictness ranking from the enforcer-owned ordinal registry
    (``OrdinalControl`` over ``_ORDINAL_SCALES['sandbox']``) — NOT a private
    duplicate table — so the floor cannot silently no-op if a tier is added to
    the scale.  Returns *mode* unchanged when there is no governance opinion or
    the floor is already satisfied.

    Fail-closed: a ``PlatformCompositionError`` (a non-standalone host that could
    not compose) propagates — the sandbox floor must never silently downgrade
    from DENY to ALLOW on the very host that is supposed to be governed.  Any
    OTHER (transient) error leaves *mode* as-is (a missing tighten is backstopped
    by the always-on controls), and an unknown floor/mode value raises rather
    than ranking it as 0 (which would fail open).
    """
    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance import _ORDINAL_SCALES, OrdinalControl

    try:
        from kiro_crew.platform.governance_profiles import governance_floor_ordinal

        floor = governance_floor_ordinal("sandbox.min_level")
    except PlatformCompositionError:
        raise
    except Exception:
        return mode
    if not floor:
        return mode
    scale = _ORDINAL_SCALES["sandbox"]
    # The floor already validated through OrdinalControl inside
    # governance_floor_ordinal, so it is in-scale; an unrecognised caller mode is
    # treated as the loosest tier so the floor still clamps it UP (fail-closed —
    # never let an unknown mode skip the tighten).
    cur_value = _SANDBOX_MODE_ALIASES.get(mode, mode)
    floor_rank = OrdinalControl("sandbox", floor).rank()
    cur_rank = scale.index(cur_value) if cur_value in scale else -1
    if floor_rank <= cur_rank:
        return mode
    # The floor's scale value IS a valid wrap_argv mode (off/standard/cc/strict).
    return floor
