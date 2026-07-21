"""Security enforcement + hook sanitization across installed agent specs.

Owns the runtime mtime caches (``_denied_cmd_mtimes``,
``_hooks_sanitized_mtimes``, ``_last_skipped_set``). Depends on ``paths``,
``hooks``, ``builder``, and ``workers``.
"""
from __future__ import annotations

import json
import logging

from kiro_crew.aim_agents import installed_kiro_packages_missing_from_cc
from kiro_crew.config import KiroCrewConfig
from kiro_crew.sel import sel
from kiro_crew.agent import builder as _builder
from kiro_crew.agent import hooks as _hooks
from kiro_crew.agent import paths as _paths
from kiro_crew.agent import workers as _workers

logger = logging.getLogger("kiro_crew.agent")



def repair_agent_configs() -> None:
    """Enforce security controls and sanitize invalid keys in all agent configs."""
    _enforce_denied_commands()
    _sanitize_agent_hooks()


def _ensure_cc_parity_for_kiro_packages() -> None:
    """No-op on public installs (AIM package manager absent).

    Previously warned when an AIM package was installed for the kiro
    provider but not for Claude Code.  AIM is Amazon-internal, so
    ``installed_kiro_packages_missing_from_cc`` returns an empty list on a
    public machine and there is nothing to warn about.  The call is retained
    (and stays harmless) to preserve the cross-provider parity contract if a
    user happens to have an ``aim`` binary on PATH.
    """
    try:
        _ = installed_kiro_packages_missing_from_cc()
    except Exception:
        logger.debug("CC parity check failed", exc_info=True)


_denied_cmd_mtimes: dict[str, float] = {}
_last_skipped_set: frozenset[str] = frozenset()


def _enforce_denied_commands() -> None:
    """Inject deniedCommands from bundled defaults into agent configs.

    Scope controlled by ``agent.enforce_denied_commands`` in config:
      - ``"all"`` (default): enforce on every installed agent.
      - ``"kirocrew"``: only enforce on kirocrew.json, skip other agents.

    Runs at: install_agent(), start_pool() (gateway startup),
    _cleanup_loop() (~60s periodic). Uses mtime to skip unchanged files.
    """
    bundled = _builder._load_json(_paths._BUNDLED_CFG_DIR / "defaults.json")
    denied = bundled.get("toolsSettings", {}).get("execute_bash", {}).get("deniedCommands")
    if not denied:
        return

    # Determine scope from config
    try:
        scope = KiroCrewConfig.load().agent.enforce_denied_commands
    except Exception as exc:
        logger.debug("Failed to load enforce_denied_commands scope, defaulting to 'all': %s", exc)
        scope = "all"

    kirocrew_names = frozenset(
        f.name for f in _paths.KIRO_AGENTS_DIR.glob("*.json") if "kirocrew" in f.name.lower()
    )

    skipped: list[str] = []

    for f in _paths.KIRO_AGENTS_DIR.glob("*.json"):
        if f.name in _workers._LITE_AGENT_NAMES:
            continue
        if scope == "kirocrew" and f.name not in kirocrew_names:
            skipped.append(f.name)
            continue
        try:
            mtime = f.stat().st_mtime
            if _denied_cmd_mtimes.get(str(f)) == mtime:
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        # A valid-JSON-but-non-object root (e.g. an AppleDouble ._foo.json stub,
        # or a hand-edited/tool-emitted "[]"/42) would raise AttributeError on
        # the setdefault below — which is NOT in the except tuple above, so it
        # would propagate out of this per-file loop and abort enforcement for
        # every agent config after it (deniedCommands, a security control,
        # silently left un-refreshed). Skip the bad file and keep enforcing the
        # rest. _builder._load_json() already guards non-dict roots the same way.
        if not isinstance(data, dict):
            continue
        ts = data.setdefault("toolsSettings", {})
        bash = ts.setdefault("execute_bash", {})
        shell = ts.setdefault("shell", {})
        existing_bash = set(bash.get("deniedCommands", []))
        existing_shell = set(shell.get("deniedCommands", []))
        required = set(denied)

        if existing_bash == required and existing_shell == required:
            _denied_cmd_mtimes[str(f)] = mtime
            continue
        # Replace entirely — bundled defaults are the canonical source.
        # User-added patterns via dashboard are not supported; all
        # security patterns must ship in agents/defaults.json.
        bash["deniedCommands"] = sorted(required)
        shell["deniedCommands"] = sorted(required)
        _paths._atomic_json_write(f, data)
        _denied_cmd_mtimes[str(f)] = f.stat().st_mtime
        logger.info("Enforced deniedCommands on %s", f.name)

    if skipped:
        skipped_set = frozenset(skipped)
        global _last_skipped_set
        if skipped_set != _last_skipped_set:
            _last_skipped_set = skipped_set
            sel().log_api_access(
                caller="system",
                operation="enforce_denied_commands.skip",
                outcome="ok",
                source="agent",
                resources=",".join(sorted(skipped)),
            )


_hooks_sanitized_mtimes: dict[str, float] = {}


def _sanitize_agent_hooks() -> None:
    """Remove KiroCrew-internal hook keys from kiro-cli agent configs.

    Kiro-cli rejects unknown variants in the ``hooks`` field (e.g.
    ``auto_approve_tools``), causing it to silently fall back to the
    default agent — losing kirocrew-core, kirocrew-cron.

    Runs alongside ``_enforce_denied_commands`` to auto-repair configs
    for users who already have the invalid key from prior versions.
    """
    for f in _paths.KIRO_AGENTS_DIR.glob("*.json"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if _hooks_sanitized_mtimes.get(str(f)) == mtime:
            continue
        data = _builder._load_json(f)
        if not data:
            continue
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            _hooks_sanitized_mtimes[str(f)] = mtime
            continue
        clean_hooks = _hooks._kiro_hooks_only(hooks)
        if len(clean_hooks) == len(hooks):
            _hooks_sanitized_mtimes[str(f)] = mtime
            continue
        invalid_keys = [k for k in hooks if k not in _hooks._VALID_HOOK_EVENTS]
        data["hooks"] = clean_hooks
        _paths._atomic_json_write(f, data)
        _hooks_sanitized_mtimes[str(f)] = f.stat().st_mtime
        logger.info("Removed invalid hook keys %s from %s", invalid_keys, f.name)
        sel().log_api_access(
            caller="system",
            operation="sanitize_agent_hooks",
            outcome="ok",
            source="agent",
            resources=f"{f.name}: removed {invalid_keys}",
        )
