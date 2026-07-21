"""kiro-cli hook validation, autoimport, and merge logic.

Leaf module: no intra-package (kiro_crew.agent) dependencies. Validates
user-supplied hook command paths, auto-imports executable hook scripts,
and merges them into the agent config with dedup + caps.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import SecurityEvent, sel

logger = logging.getLogger("kiro_crew.agent")



# Allowlist for hook-command paths (config.json is LLM-writable, so this guards
# against indirect command injection). The intent is to reject shell
# metacharacters (; | & $ ` spaces quotes ( ) etc.) — the path is later exec'd as
# an argv element, never through a shell. On Windows an absolute path is
# `D:\Users\...`, so backslash and the drive-letter colon MUST be allowed there or
# EVERY Windows hook path is rejected (autoimport silently loads nothing). `\` and
# `:` are not shell-injection vectors for an argv path, and the is_sensitive_path
# + absolute-path + resolve() checks below still apply. POSIX keeps the original,
# tighter allowlist (no backslash/colon).
if platform_compat.IS_WINDOWS:
    _SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9/_.\-\\:]+$")
else:
    _SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9/_.\-]+$")
_SAFE_MATCHER_RE = re.compile(r"^[a-zA-Z0-9_.*\-]+$")
_MAX_MATCHER_LEN = 200


def _validate_hook_command(command: str, event: str) -> str | None:
    """Validate a user-supplied hook command path.

    Returns the resolved absolute path if safe, or None on failure.
    Since config.json is LLM-writable, this guards against indirect
    command injection.  Uses an allowlist regex for path characters.
    """
    if not _SAFE_PATH_RE.match(command):
        logger.warning("kiro_hooks[%s]: command contains disallowed characters: %r", event, command)
        return None
    if not os.path.isabs(command):
        logger.warning("kiro_hooks[%s]: command must be absolute path, got %r", event, command)
        return None
    resolved = str(Path(command).resolve())
    if not _SAFE_PATH_RE.match(resolved):
        logger.warning(
            "kiro_hooks[%s]: resolved path contains disallowed characters: %r", event, resolved
        )
        return None
    if is_sensitive_path(resolved):
        logger.warning(
            "kiro_hooks[%s]: command points to sensitive path %r, skipping", event, command
        )
        return None
    if not os.path.isfile(resolved):
        logger.warning("kiro_hooks[%s]: command not found: %s", event, command)
        return None
    return resolved


def _sel_hook_rejected(event: str, command: str, reason: str) -> None:
    """Emit a SEL audit event when a user hook entry is rejected."""
    try:
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="config_hooks_merge",
                caller_identity="agent_install",
                agent="kirocrew",
                source="cli",
                operation="kiro_hooks_rejected",
                outcome="rejected",
                resources=redact(f"event={event} command={command[:200]}"),
                error=reason,
            )
        )
    except Exception:
        logger.debug("SEL audit for rejected hook failed", exc_info=True)


_VALID_HOOK_EVENTS = frozenset(
    {"preToolUse", "postToolUse", "userPromptSubmit", "agentSpawn", "stop"}
)


def _kiro_hooks_only(hooks: dict) -> dict:
    """Return only kiro-cli valid hook keys, stripping KiroCrew-internal ones."""
    return {k: v for k, v in hooks.items() if k in _VALID_HOOK_EVENTS}


_MAX_USER_HOOKS_PER_EVENT = 10
_MAX_TOTAL_USER_HOOKS = 20

# kiro-cli documents hook events in PascalCase (PreToolUse, PostToolUse, ...).
# The agent config stores them in camelCase (preToolUse, ...).  Script headers
# ("# event: PreToolUse") use kiro-cli's PascalCase convention; this map
# normalizes both casings back to the canonical camelCase form.
_HOOK_EVENT_CANONICAL = {
    "pretooluse": "preToolUse",
    "posttooluse": "postToolUse",
    "userpromptsubmit": "userPromptSubmit",
    "agentspawn": "agentSpawn",
    "stop": "stop",
}

# Default hooks directory matches kiro-cli's discovery path.
_DEFAULT_KIRO_HOOKS_DIR = Path.home() / ".kiro" / "hooks"

# Recognize hook event from filename suffix when no "# event:" header is set.
# Ordering matters: check more specific suffixes first.
_FILENAME_EVENT_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("-post.sh", "postToolUse"),
    ("-prompt.sh", "userPromptSubmit"),
    ("-spawn.sh", "agentSpawn"),
    ("-stop.sh", "stop"),
    ("-pre.sh", "preToolUse"),
)

# Header parsing — only inspect the first few lines so the scan stays O(K).
_HOOK_HEADER_SCAN_LINES = 5
_HOOK_HEADER_RE = re.compile(r"^\s*#\s*(event|matcher)\s*:\s*(\S.*?)\s*$", re.IGNORECASE)


def _parse_hook_script_headers(path: Path) -> tuple[str | None, str | None]:
    """Read the first few lines of a hook script and extract ``# event:`` / ``# matcher:`` directives.

    Returns ``(event_header, matcher_header)``.  Either may be ``None`` if not present.
    Values are returned unparsed; callers normalize/validate them.
    """
    event_header: str | None = None
    matcher_header: str | None = None
    try:
        # Read at most a handful of lines; hook scripts can be large, and we
        # only care about headers immediately after the shebang.
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _HOOK_HEADER_SCAN_LINES:
                    break
                m = _HOOK_HEADER_RE.match(line)
                if not m:
                    continue
                key = m.group(1).lower()
                val = m.group(2)
                if key == "event" and event_header is None:
                    event_header = val
                elif key == "matcher" and matcher_header is None:
                    matcher_header = val
    except OSError:
        logger.debug("kiro_hooks_autoimport: could not read %s for headers", path, exc_info=True)
    return event_header, matcher_header


def _infer_hook_event(script_path: Path, event_header: str | None) -> str | None:
    """Resolve a script's kiro hook event.

    Precedence:
      1. Explicit ``# event:`` header (normalized to camelCase).  Unknown values
         return ``None`` so the caller can WARN and skip.
      2. Filename suffix convention (``*-post.sh`` -> ``postToolUse`` etc.).
      3. Default: ``preToolUse``.
    """
    if event_header is not None:
        canonical = _HOOK_EVENT_CANONICAL.get(
            event_header.lower().replace("-", "").replace("_", "")
        )
        return canonical  # None if unknown -- caller decides what to do

    name = script_path.name.lower()
    for suffix, event in _FILENAME_EVENT_SUFFIXES:
        if name.endswith(suffix):
            return event
    return "preToolUse"


def _autoimport_kiro_hooks(hooks_dir: Path) -> dict[str, list[dict[str, str]]]:
    """Scan ``hooks_dir`` for executable ``*.sh`` files and return a ``kiro_hooks``-shaped dict.

    Each discovered script becomes an entry under its resolved event (camelCase).
    Returns an empty dict if the directory is missing or contains no usable scripts.

    Security parity with the explicit config path:
      * Each script's resolved path goes through ``_validate_hook_command``.
      * ``# matcher:`` headers are validated against ``_SAFE_MATCHER_RE`` / ``_MAX_MATCHER_LEN``.
      * Non-executable files are skipped (INFO log).
      * Sensitive paths are skipped (via ``_validate_hook_command``).

    Final dedup, per-event cap, and total cap are enforced by ``_merge_kiro_hooks``
    which runs on the returned dict.  That keeps explicit config precedence correct:
    callers should invoke ``_merge_kiro_hooks`` with the already-merged ``hooks``
    (bundled + explicit) so auto-imported scripts that duplicate an explicit entry
    are deduped out rather than taking its slot.
    """
    result: dict[str, list[dict[str, str]]] = {}
    try:
        resolved_hooks_dir = hooks_dir.resolve()
    except (OSError, ValueError):
        # OSError: ENAMETOOLONG, ELOOP, EACCES on a path component.
        # ValueError: null bytes (``"\x00"``) reject at Path construction.
        # Emit SEL audit so an auditor sees a distinct "hooks_dir
        # unresolvable" signal — same symmetry principle as the
        # per-entry ``cannot resolve entry`` branch below.
        logger.debug("kiro_hooks_autoimport: cannot resolve %s, skipping", hooks_dir, exc_info=True)
        _sel_hook_rejected("autoimport", str(hooks_dir), "cannot resolve hooks_dir")
        return result
    try:
        entries = sorted(resolved_hooks_dir.iterdir())
    except FileNotFoundError:
        logger.debug("kiro_hooks_autoimport: directory %s does not exist, skipping", hooks_dir)
        return result
    except OSError:
        logger.warning("kiro_hooks_autoimport: cannot read %s, skipping", hooks_dir, exc_info=True)
        # Emit SEL audit so an auditor reconstructing agent-install
        # activity sees a distinct "hooks dir unreadable" signal rather
        # than only the merge-summary ``requested_autoimport=0`` (which
        # looks identical to the no-scripts-configured case).  Same
        # symmetry principle as the per-script rejection branches.
        _sel_hook_rejected("autoimport", str(hooks_dir), "cannot read hooks_dir")
        return result

    loaded = 0
    for entry in entries:
        if not entry.is_file() or entry.suffix != ".sh":
            continue

        # Resolve once up-front and reuse the resolved path for all subsequent
        # checks (stat, validation).  This closes two issues:
        # * TOCTOU: repeated resolve() in _validate_hook_command could race
        #   with an attacker swapping the symlink target between calls.
        # * Symlink escape: entry.is_file() follows symlinks, so a symlink
        #   inside the hooks dir pointing at /tmp/attacker.sh would otherwise
        #   pass (not in _SENSITIVE_HOME_DIRS).  Require the resolved target
        #   to stay under the resolved hooks dir.
        try:
            resolved_entry = entry.resolve()
        except (OSError, ValueError):
            # OSError: typical filesystem failures.  ValueError: filename
            # from ``iterdir()`` carries a null byte or other malformed
            # character that ``Path.resolve()`` rejects.  Without this
            # catch, a maliciously-named file in hooks_dir crashes agent
            # bootstrap.
            logger.warning(
                "kiro_hooks_autoimport: cannot resolve %s, skipping", entry, exc_info=True
            )
            _sel_hook_rejected("autoimport", str(entry), "cannot resolve entry")
            continue
        if (
            resolved_entry != resolved_hooks_dir
            and resolved_hooks_dir not in resolved_entry.parents
        ):
            logger.warning(
                "kiro_hooks_autoimport: %s resolves outside %s (to %s), skipping",
                entry,
                resolved_hooks_dir,
                resolved_entry,
            )
            _sel_hook_rejected("autoimport", str(entry), "resolved path escapes hooks dir")
            continue

        try:
            resolved_entry.stat()  # surface a stat error (broken symlink, perms) as a skip
        except OSError:
            logger.warning("kiro_hooks_autoimport: cannot stat %s, skipping", entry)
            _sel_hook_rejected("autoimport", str(entry), "cannot stat entry")
            continue
        # Executable check is platform-aware: POSIX requires the execute bit (so
        # `chmod -x` disables a hook); Windows has no execute bit, so requiring
        # X_OK there would skip EVERY hook and silently break the whole autoimport
        # — instead a known script extension (.sh/.ps1/.cmd/...) is treated as
        # runnable. See platform_compat.is_executable_file.
        if not platform_compat.is_executable_file(resolved_entry):
            logger.info("kiro_hooks_autoimport: %s is not executable, skipping", entry)
            # Audit parity with the other rejection branches
            # (symlink-escape, cannot-resolve, cannot-stat,
            # failed-validation, unknown-event, invalid-matcher,
            # cannot-read-dir): the non-executable skip is also a
            # permission decision — it determines that a discovered
            # ``.sh`` file will NOT be loaded as a hook — so it must
            # emit a SEL audit event per AUTOSDE.yaml security-controls
            # rule.  Without this call, an auditor reconstructing
            # agent-install activity from SEL would not see scripts
            # that were skipped for lacking the execute bit.
            _sel_hook_rejected("autoimport", str(entry), "not executable")
            continue

        # Defense-in-depth: run the full validation (including
        # is_sensitive_path) BEFORE any file I/O on the script.  The
        # symlink-escape check above already rejects most attacks, but
        # running _validate_hook_command first keeps the "no reads on
        # sensitive paths" invariant intact even if the resolved-path
        # check is ever loosened.  The ``"autoimport"`` event label
        # below is a log tag only - _validate_hook_command uses ``event``
        # solely for log formatting, never as a policy key (e.g. it is
        # never matched against _VALID_HOOK_EVENTS).  The real event is
        # computed from headers after this call succeeds.
        validated_command = _validate_hook_command(str(resolved_entry), "autoimport")
        if validated_command is None:
            # _validate_hook_command already emitted a WARNING with the reason.
            _sel_hook_rejected("autoimport", str(entry), "failed validation")
            continue

        event_header, matcher_header = _parse_hook_script_headers(resolved_entry)
        event = _infer_hook_event(entry, event_header)
        if event is None:
            logger.warning(
                "kiro_hooks_autoimport: %s declares unknown event %r, skipping",
                entry,
                event_header,
            )
            # Match the other three rejection branches in this function
            # (symlink-escape, failed-validation, invalid-matcher): every
            # rejection must emit a SEL audit event per AUTOSDE.yaml's
            # security-controls rule.  Without this call, an auditor
            # reconstructing agent-install activity from SEL would not
            # see scripts that were dropped for declaring unknown event
            # names, which defeats the purpose of the audit trail.
            _sel_hook_rejected("autoimport", str(entry), "unknown event header")
            continue

        entry_dict: dict[str, str] = {"command": validated_command}
        if matcher_header is not None:
            if len(matcher_header) > _MAX_MATCHER_LEN or not _SAFE_MATCHER_RE.match(matcher_header):
                # An invalid matcher is treated as a validation failure:
                # promoting a tool-scoped hook to unscoped (firing on every
                # tool call) would be a silent privilege expansion.
                logger.warning(
                    "kiro_hooks_autoimport: %s matcher %r is invalid, skipping script",
                    entry,
                    matcher_header,
                )
                _sel_hook_rejected("autoimport", str(entry), "invalid matcher")
                continue
            entry_dict["matcher"] = matcher_header

        result.setdefault(event, []).append(entry_dict)
        loaded += 1

    if loaded:
        logger.info("kiro_hooks_autoimport: loaded %d scripts from %s", loaded, hooks_dir)
    else:
        logger.debug("kiro_hooks_autoimport: no scripts loaded from %s", hooks_dir)
    return result


def _merge_kiro_hooks(hooks: dict, user_hooks: dict) -> dict:
    """Append user-defined kiro_hooks to bundled hooks (per event type).

    Bundled hooks are always first.  User hooks are appended, deduped by
    ``(command, matcher)`` tuple so the same hook doesn't fire twice.
    Malformed entries (missing ``command``) are silently skipped.
    Commands are validated: must be absolute paths to existing files,
    with no shell metacharacters and not in sensitive locations.
    """
    if not isinstance(user_hooks, dict):
        logger.warning("kiro_hooks is not a dict, ignoring")
        return hooks
    merged = dict(hooks)
    total_added = 0
    for event, entries in user_hooks.items():
        if event not in _VALID_HOOK_EVENTS:
            logger.warning("kiro_hooks: unknown event type %r, skipping", event)
            # Audit parity with every other rejection branch in this
            # function: per AUTOSDE.yaml security-controls, rejecting an
            # entire event-bucket is a permission decision that must be
            # SEL-audited.  Use the (invalid) event name as the tag so
            # auditors can correlate with the config input.
            _sel_hook_rejected(str(event), str(entries)[:200], "unknown event type")
            continue
        if not isinstance(entries, list):
            logger.warning("kiro_hooks[%s] is not a list, skipping", event)
            # Same audit-parity rationale: dropping a non-list
            # entries-bucket removes all configured hooks for that
            # event.  SEL must record the decision so auditors can
            # distinguish "0 configured" from "N dropped as non-list".
            _sel_hook_rejected(event, str(entries)[:200], "entries not a list")
            continue
        existing = list(merged.get(event, []))
        existing_keys = {
            (e.get("command"), e.get("matcher")) for e in existing if isinstance(e, dict)
        }
        added = 0
        for entry in entries:
            if added >= _MAX_USER_HOOKS_PER_EVENT:
                logger.warning(
                    "kiro_hooks[%s]: limit of %d reached, ignoring remaining",
                    event,
                    _MAX_USER_HOOKS_PER_EVENT,
                )
                # Audit parity with every other rejection branch in this
                # function (missing command, failed validation, non-string
                # matcher, invalid matcher): hitting the per-event cap is
                # a permission decision - configured hooks are being
                # prevented from loading - and must emit a SEL audit
                # event per AUTOSDE.yaml security-controls.  Without
                # this, an auditor cannot distinguish "user configured 15
                # preToolUse hooks and 5 were cap-dropped" from "user
                # configured 10 and all loaded".
                _sel_hook_rejected(
                    event,
                    (
                        str(entry.get("command", ""))[:200]
                        if isinstance(entry, dict)
                        else str(entry)[:200]
                    ),
                    "per-event limit exceeded",
                )
                break
            if total_added >= _MAX_TOTAL_USER_HOOKS:
                logger.warning(
                    "kiro_hooks: global limit of %d reached, ignoring remaining",
                    _MAX_TOTAL_USER_HOOKS,
                )
                # Same audit-parity rationale as the per-event cap above:
                # hitting the global cap drops remaining hooks across all
                # events, and auditors need a SEL signal to distinguish
                # "25 configured, 5 cap-dropped" from "20 configured, all
                # loaded".
                _sel_hook_rejected(
                    event,
                    (
                        str(entry.get("command", ""))[:200]
                        if isinstance(entry, dict)
                        else str(entry)[:200]
                    ),
                    "global limit exceeded",
                )
                break
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("command"), str)
                or not entry["command"]
            ):
                logger.warning("kiro_hooks[%s]: skipping entry without command", event)
                _sel_hook_rejected(event, str(entry)[:200], "missing or invalid command")
                continue
            resolved = _validate_hook_command(entry["command"], event)
            if resolved is None:
                _sel_hook_rejected(event, entry["command"], "failed validation")
                continue
            matcher = entry.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                logger.warning("kiro_hooks[%s]: matcher must be a string, skipping", event)
                _sel_hook_rejected(event, entry["command"], "non-string matcher")
                continue
            if isinstance(matcher, str) and (
                len(matcher) > _MAX_MATCHER_LEN or not _SAFE_MATCHER_RE.match(matcher)
            ):
                logger.warning(
                    "kiro_hooks[%s]: matcher contains disallowed characters or is too long, skipping",
                    event,
                )
                _sel_hook_rejected(event, entry["command"], "invalid matcher")
                continue
            key = (resolved, matcher)
            if key not in existing_keys:
                sanitized = {"command": resolved}
                if isinstance(matcher, str):
                    sanitized["matcher"] = matcher
                existing.append(sanitized)
                existing_keys.add(key)
                added += 1
                total_added += 1
        merged[event] = existing
    return merged


def _apply_user_kiro_hooks(config: dict, mc_cfg: dict) -> None:
    """Merge user-defined kiro_hooks from kirocrew config into *config* (additive).

    Two sources, explicit first then auto-discovered:

      1. ``agent.kiro_hooks`` in ``~/.kirocrew/config.json`` -- explicit entries
         the user wrote by hand.  Unchanged behavior.
      2. ``agent.kiro_hooks_autoimport`` (default true): scan
         ``agent.kiro_hooks_dir`` (default ``~/.kiro/hooks``) for executable
         ``*.sh`` scripts and merge each as a hook entry.  Event is parsed from
         an optional ``# event:`` header, inferred from a filename suffix, or
         defaults to ``preToolUse``.  Optional ``# matcher:`` header gives the
         same tool-name matcher as explicit entries.

    Autoimport runs in a single merge pass with explicit entries listed first,
    so autoimported scripts that duplicate an explicit entry are deduped out
    (explicit wins) and caps (``_MAX_USER_HOOKS_PER_EVENT`` and
    ``_MAX_TOTAL_USER_HOOKS``) are enforced across both sources combined,
    not per-source.
    """
    agent_cfg = mc_cfg.get("agent") if isinstance(mc_cfg.get("agent"), dict) else {}
    user_hooks = agent_cfg.get("kiro_hooks") if isinstance(agent_cfg, dict) else None
    autoimport_enabled = True
    hooks_dir = _DEFAULT_KIRO_HOOKS_DIR
    if isinstance(agent_cfg, dict):
        if "kiro_hooks_autoimport" in agent_cfg:
            autoimport_enabled = bool(agent_cfg.get("kiro_hooks_autoimport"))
        custom_dir = agent_cfg.get("kiro_hooks_dir")
        if isinstance(custom_dir, str) and custom_dir:
            # config.json is LLM-writable; a malicious override could point
            # hooks_dir at /tmp, a world-writable mount, or ~/Downloads.
            # Require the resolved path to live under the user's HOME and
            # not match a sensitive location.  On any failure, log + SEL
            # audit and fall back to the default (~/.kiro/hooks) rather
            # than turning autoimport off entirely - the safe default is
            # still available.
            requested = Path(os.path.expanduser(custom_dir))
            try:
                resolved = requested.resolve()
                home = Path.home().resolve()
            except (OSError, ValueError):
                # OSError: ENAMETOOLONG, ELOOP (symlink loop), EACCES.
                # ValueError: Path() / resolve() reject strings with null
                # bytes (``"\x00"``) or similar malformed Unicode.  An
                # LLM-writable ``kiro_hooks_dir: "\x00"`` would otherwise
                # propagate ValueError up through install_agent() and
                # crash agent bootstrap (denial of service).
                resolved = None
                home = None
            if (
                resolved is None
                or home is None
                # Strict containment: require ``resolved`` to be *under*
                # HOME, not equal to it.  ``~`` alone would otherwise scan
                # the entire home directory for executable ``*.sh`` files,
                # auto-registering anything a user (or attacker) drops
                # anywhere under ``$HOME``.  ``Path.parents`` of e.g.
                # ``/home/user`` is ``(/, /home)`` and does NOT include
                # ``/home/user`` itself, so a bare ``home not in parents``
                # rejects ``resolved == home``.
                or home not in resolved.parents
                or is_sensitive_path(str(resolved))
            ):
                logger.warning(
                    "kiro_hooks_autoimport: kiro_hooks_dir %r rejected "
                    "(must resolve under %s and not be sensitive), "
                    "falling back to %s",
                    custom_dir,
                    home,
                    _DEFAULT_KIRO_HOOKS_DIR,
                )
                _sel_hook_rejected(
                    "autoimport", str(requested), "kiro_hooks_dir outside HOME or sensitive"
                )
            else:
                # Store the already-resolved path, not the unresolved
                # ``requested``.  Keeping ``requested`` would leave a
                # symlink-swap window: a path component could be swapped
                # between this resolve() and the one inside
                # _autoimport_kiro_hooks, bypassing the HOME containment
                # check we just performed.
                hooks_dir = resolved

    explicit_hooks: dict = user_hooks if isinstance(user_hooks, dict) and user_hooks else {}
    has_explicit = bool(explicit_hooks)
    if not has_explicit and not autoimport_enabled:
        return

    before = sum(len(v) for v in config.get("hooks", {}).values() if isinstance(v, list))

    # Collect both sources up-front and merge in a SINGLE ``_merge_kiro_hooks``
    # pass.  Rationale: ``_merge_kiro_hooks`` initializes ``total_added = 0`` on
    # each call, so invoking it twice would allow the per-call
    # ``_MAX_TOTAL_USER_HOOKS`` cap (20) to apply to each source independently —
    # yielding up to 40 user hooks total instead of the intended 20.  A single
    # pass enforces the per-event cap AND the total cap across the combined
    # set.  Explicit entries are listed first in each event's list so they
    # claim the dedup key before any duplicate from autoimport, preserving the
    # "explicit wins" precedence.
    # Count explicit entries AND audit any non-list buckets as we go.
    # Using a plain loop rather than a generator expression so we can
    # emit WARNING + SEL audit for each dropped event bucket -- dropping
    # a whole event's hooks is a permission decision per AUTOSDE.yaml
    # security-controls, and the caller-side filter must audit it
    # (``_merge_kiro_hooks``'s internal defensive check never fires here
    # because this filter runs first).
    requested_explicit = 0
    for event, entries in explicit_hooks.items():
        if isinstance(entries, list):
            requested_explicit += len(entries)
        else:
            logger.warning("kiro_hooks[%s] is not a list, skipping", event)
            _sel_hook_rejected(str(event), str(entries)[:200], "entries not a list")
    requested_autoimport = 0
    discovered: dict[str, list[dict[str, str]]] = {}
    if autoimport_enabled:
        discovered = _autoimport_kiro_hooks(hooks_dir)
        requested_autoimport = sum(len(v) for v in discovered.values() if isinstance(v, list))

    if requested_explicit == 0 and requested_autoimport == 0:
        # Nothing to merge; keep config["hooks"] untouched (or create empty
        # dict for shape consistency if it wasn't there).
        if "hooks" not in config:
            config["hooks"] = {}
        return

    combined_user_hooks: dict[str, list[dict[str, str]]] = {}
    for src in (explicit_hooks, discovered):
        if not isinstance(src, dict):
            continue
        for event, entries in src.items():
            if not isinstance(entries, list):
                # Already WARN+SEL-audited in the ``requested_explicit``
                # loop above (for explicit_hooks) or filtered out at
                # return-time of ``_autoimport_kiro_hooks`` (discovered
                # never contains non-list values).  Defensive continue.
                continue
            combined_user_hooks.setdefault(event, []).extend(entries)

    config["hooks"] = _merge_kiro_hooks(config.get("hooks", {}), combined_user_hooks)

    after = sum(len(v) for v in config["hooks"].values() if isinstance(v, list))
    added = after - before
    try:
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="config_hooks_merge",
                caller_identity="agent_install",
                agent="kirocrew",
                source="cli",
                operation="kiro_hooks_merge",
                outcome="completed",
                resources=redact(
                    f"requested_explicit={requested_explicit} "
                    f"requested_autoimport={requested_autoimport} added={added}"
                ),
            )
        )
    except Exception:
        logger.debug("SEL audit for kiro_hooks merge failed", exc_info=True)
