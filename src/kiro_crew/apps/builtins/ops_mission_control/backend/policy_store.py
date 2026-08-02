"""The autonomy ceiling — app mode and act-rules — on the keystone floor.

Why this is a separate file from ``config.json`` and not just another key in it:

``mode`` (observe/propose/act) and ``autonomy_rules`` are the app's SECURITY CEILING, not a
preference. ``effective = min(app_mode, rule_mode)`` is only a ceiling if the thing being
minimised cannot be raised by the party it constrains — and the party it constrains is the
agent. In ``config.json`` it could be raised by exactly that party: an app's
``data/config.json`` is served over ``/api/apps/<name>/config`` WITHOUT session auth, and the
file is writable by any auto-approved agent shell. So a prompt-injected agent could set
``mode=act`` plus a matching rule and unlock a write against the user's production incident
tooling the operator never granted. Found in review.

This mirrors ``secrets.py`` exactly: one owner-only JSON file under ``config_dir()``, named
``ops_mission_control_policy.json``, which ``security._CREW_SECRET_LEAVES`` places on the
read+write keystone floor — the agent can neither read nor overwrite it. The authenticated
dashboard PUT handler is the sole writer and opens the path directly (via ``set_mode`` /
``set_rules`` here), bypassing the agent gate, so Settings still works.

Provider config that the agent legitimately reads (regions, prefixes, channel ids) stays in
``config.json``. Only the ceiling moves.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: Keystone leaf. MUST match the entry in ``security._CREW_SECRET_LEAVES`` — a test pins
#: the two equal, because a rename here without the fence entry silently un-protects the
#: ceiling, which is the whole failure this file exists to prevent.
POLICY_FILENAME = "ops_mission_control_policy.json"

_MODE_KEY = "mode"
_RULES_KEY = "autonomy_rules"

#: Every OPERATOR-ONLY key, i.e. every value the agent must not be able to set.
#:
#: Two kinds live here and the second was found by auditing for the first:
#:
#: 1. **The authorization ceiling** — ``mode`` and ``autonomy_rules``. Raising these unlocks a
#:    provider write.
#: 2. **Outbound DESTINATIONS** — where this app sends things. ``ledger_sync_remote`` is the
#:    git remote the shared knowledge ledger is PUSHED to, and ``slack_channel`` is where every
#:    incident title, diagnosis and resource name is mirrored. An agent that can rewrite either
#:    redirects the app's own output to a place it controls: the ledger remote is straight data
#:    exfiltration of the team's accumulated incident knowledge, and the channel leaks the live
#:    incident stream. Verified before fixing: with ``config.json`` writable,
#:    ``ledger_sync.remote()`` returned ``https://attacker.example/exfil.git`` and
#:    ``slack_out.channel()`` returned the attacker's channel, and ``POST /ledger/hygiene`` —
#:    which the agent's own hygiene cron calls — pushes to exactly that remote.
#:
#: The ``*_enabled`` flags come along because a disabled destination is a safety state: being
#: able to turn the exchange ON is most of the way to redirecting it.
#:
#: ``branch`` is deliberately NOT here. It selects a ref inside a remote the operator already
#: chose, is already shape-validated at the write path, and cannot move data off-box.
OPERATOR_ONLY_KEYS: tuple[str, ...] = (
    _MODE_KEY,
    _RULES_KEY,
    "ledger_sync_remote",
    "ledger_sync_enabled",
    "slack_channel",
    "slack_enabled",
)

#: Owner-only, matching the secret file. The value is not a credential, but it IS a control
#: whose confidentiality and integrity both matter, so it gets the same lockdown.
_POLICY_FILE_MODE = 0o600


def policy_path() -> Path:
    """Absolute path to the keystone policy file (honors ``KIROCREW_HOME``)."""
    return config_dir() / POLICY_FILENAME


def _read() -> dict[str, Any]:
    try:
        raw = json.loads(policy_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True)
    atomic_write(policy_path(), payload, mode=_POLICY_FILE_MODE)
    # Fail-loud lockdown, same as the secret store: ``atomic_write``'s mode covers POSIX,
    # and this applies the owner-only DACL on Windows. A lockdown failure must not leave the
    # ceiling world-readable, so unlink and re-raise rather than continue.
    try:
        platform_compat.restrict_to_owner(policy_path())
    except OSError:
        try:
            policy_path().unlink()
        except OSError:
            logger.exception("failed to remove ops policy file after lockdown failure")
        raise


def read_mode(default: str) -> str:
    """The stored app mode, or ``default`` when unset. Validation is the caller's job."""
    value = _read().get(_MODE_KEY, default)
    return str(value) if isinstance(value, str) else default


def read_rules_raw() -> list[Any]:
    """The stored rule dicts, unparsed. ``rotation.load_rules`` validates each one."""
    raw = _read().get(_RULES_KEY)
    return raw if isinstance(raw, list) else []


def set_mode(mode: str) -> None:
    """Persist the app mode. Dashboard-PUT only — see the module docstring."""
    data = _read()
    data[_MODE_KEY] = mode
    _write(data)


def set_rules(rules: list[Any]) -> None:
    """Persist the act-rules. Dashboard-PUT only."""
    data = _read()
    data[_RULES_KEY] = rules
    _write(data)


def get(key: str, default: Any = None) -> Any:
    """Read one operator-only value off the fenced floor, migrating a legacy copy first.

    The generic accessor for the destination keys. ``mode``/``autonomy_rules`` keep their own
    typed readers because they have validation the caller must not skip.
    """
    if key not in OPERATOR_ONLY_KEYS:  # pragma: no cover — programming error, not input
        raise KeyError(f"{key!r} is not an operator-only key; use config.json for it")
    migrate_from_config_if_needed()
    data = _read()
    return data.get(key, default)


def put(key: str, value: Any) -> None:
    """Write one operator-only value. Dashboard-PUT only — see the module docstring."""
    if key not in OPERATOR_ONLY_KEYS:  # pragma: no cover — programming error, not input
        raise KeyError(f"{key!r} is not an operator-only key; use config.json for it")
    data = _read()
    data[key] = value
    _write(data)


def migrate_from_config_if_needed() -> bool:
    """Move a legacy ``mode``/``autonomy_rules`` out of the agent-writable ``config.json``.

    Existing installs wrote the ceiling into ``config.json`` before it was fenced. On first
    read we lift those keys into the keystone file and DELETE them from ``config.json``, so a
    pre-fix install is not left with a live, agent-writable copy shadowing the fenced one.

    Idempotent and best-effort: runs only when the policy file does not yet exist AND
    ``config.json`` still carries at least one of the keys. Returns True when it migrated.

    Deferred import of ``providers`` to avoid a cycle — that package imports this module's
    siblings at load time.
    """
    if policy_path().exists():
        return False
    from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
        read_config,
        write_config,
    )

    cfg = read_config()
    present = [key for key in OPERATOR_ONLY_KEYS if key in cfg]
    if not present:
        return False

    migrated: dict[str, Any] = {key: cfg[key] for key in present}
    _write(migrated)

    # Remove the now-shadowed copies from the agent-writable file. Do this AFTER the fenced
    # write succeeds, so a crash between the two leaves the value readable rather than lost.
    for key in present:
        cfg.pop(key, None)
    write_config(cfg)
    logger.info(
        "ops-mission-control: migrated autonomy ceiling (%s) out of config.json onto the "
        "keystone floor",
        ", ".join(migrated),
    )
    return True
