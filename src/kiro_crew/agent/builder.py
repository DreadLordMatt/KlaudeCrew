"""Agent-config building, refresh, normalization, and installation.

Depends on ``paths`` and ``hooks``. ``rebuild_agent_config`` orchestrates
the install and defers imports of ``workers``/``repair`` to break the
cycle (those modules import this one at load time).
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew import agent_state
from kiro_crew.config import config_path as _mc_config_path
from kiro_crew.env import augmented_path
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.agent import hooks as _hooks
from kiro_crew.agent import paths as _paths

logger = logging.getLogger("kiro_crew.agent")

_MAIN_AGENT_NAME = "kirocrew"


# One-time migrations performed automatically on gateway first-run (so the
# desktop app, which never runs `kirocrew setup`, still gets them).
_MIGRATIONS_DIR = _paths._USER_DIR / ".migrations"
_STALE_MCP_PURGE_MARKER = _MIGRATIONS_DIR / "stale_managed_mcp_purged"


def run_first_run_setup() -> None:
    """Deliver the install-time steps the desktop app needs without a terminal.

    The Electron app only runs ``kirocrew gateway`` — never ``kirocrew
    setup`` — yet two concerns aren't covered by the gateway's agent-config
    rebuild. This is invoked from gateway startup to close that gap:

    * **PATH shim** — ``_paths.ensure_kirocrew_on_path()`` is idempotent and only
      writes ``~/.local/bin/kirocrew``, so it runs on every start.
    * **Stale predecessor MCP purge** — ``clean_stale_managed_mcp()`` mutates
      the user's *global* ``~/.kiro/settings/mcp.json``, so it runs ONCE,
      guarded by a marker file, to honor the "KiroCrew owns only the agent
      file" boundary (no global rewrite on subsequent starts).

    Best-effort: never raises — any failure is logged and startup continues.
    """
    # 1. PATH shim — safe and idempotent on every start.
    try:
        shim = _paths.ensure_kirocrew_on_path()
        if shim:
            logger.info("First-run: linked kirocrew shim at %s", shim)
    except Exception:
        logger.warning("First-run: shim install failed", exc_info=True)

    # 2. Admission-policy seed — one-time, self-guarded by its OWN marker.  Run
    #    BEFORE the stale-MCP early return below so an EXISTING install (which
    #    already has the stale-MCP marker) still gets seeded on its next start;
    #    otherwise those installs would have no policy file and newly fail closed.
    try:
        from kiro_crew.platform.admission import seed_default_policy  # noqa: PLC0415

        if seed_default_policy():
            logger.info("First-run: seeded default admission policy")
    except Exception:
        logger.warning("First-run: admission policy seed failed", exc_info=True)

    # 3. Stale managed-MCP purge — one-time, marker-guarded.
    if _STALE_MCP_PURGE_MARKER.exists():
        return
    try:
        from kiro_crew.mcp_cleanup import clean_stale_managed_mcp  # noqa: PLC0415

        removed = clean_stale_managed_mcp()
        if removed:
            logger.info("First-run: purged stale managed MCP entries: %s", removed)
        # Mark done even when nothing was removed, so the global mcp.json is
        # never re-read/rewritten on later starts.
        _MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
        _STALE_MCP_PURGE_MARKER.write_text(
            datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8"
        )
    except Exception:
        logger.warning("First-run: stale MCP purge failed", exc_info=True)


def _prompt_path(mode: str = "") -> Path:
    """Return user prompt if it exists, otherwise shipped prompt.

    When mode="orchestrator", uses the orchestrator prompt.
    The conductor_skill config is independent — it controls agent routing, not the prompt.
    """
    if mode == "orchestrator":
        user_orch = _paths._USER_DIR / "prompt-orchestrator.md"
        if user_orch.is_file():
            return user_orch
        proj = _paths._project_dir()
        if proj:
            candidate = proj / "agents" / "prompt-orchestrator.md"
            if candidate.is_file():
                return candidate
        bundled_orch = _paths._BUNDLED_CFG_DIR / "prompt-orchestrator.md"
        if bundled_orch.is_file():
            return bundled_orch

    if _paths._USER_PROMPT.is_file():
        return _paths._USER_PROMPT
    return _paths._shipped_prompt()


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file, returning ``{}`` on any error or non-dict root.

    ``~/.claude.json`` in particular is user-owned and could theoretically
    contain a top-level array after a hand-edit.  Normalizing to an empty
    dict here means every caller can safely do ``_load_json(p).get(key)``
    without an ``isinstance`` check at each call site.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("Ignoring invalid %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring %s: top-level JSON is not an object", path)
        return {}
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge *override* into *base* (one level deep for dicts)."""
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = {**merged[key], **val}
        else:
            merged[key] = val
    return merged


def _all_skill_paths() -> list[str]:
    """Discover all skill directories (AIM, project, user).

    Returns directories containing SKILL.md files from:
    - ``~/.aim/skills`` and ``~/.aim/packages/*/skills`` (AIM-installed)
    - ``KIROCREW_PROJECT_DIR/skills`` (project-level)
    - ``~/.kirocrew/skills`` (user-created)
    """
    paths: set[str] = set()
    # AIM skills — only known locations, not broad rglob
    aim_dir = Path.home() / ".aim"
    if aim_dir.is_dir():
        aim_skills = aim_dir / "skills"
        if aim_skills.is_dir():
            paths.add(str(aim_skills))
            # Resolve symlinks in local/ so skill loaders whose glob skips
            # symlinks can still find them: resolve each symlink target and
            # add its parent dir (only if named "skills").
            local_dir = aim_skills / "local"
            if local_dir.is_dir():
                for entry in local_dir.iterdir():
                    if entry.is_symlink():
                        try:
                            target = entry.resolve(strict=True)
                            parent = target.parent
                            if (
                                target.is_dir()
                                and parent.name == "skills"
                                and not is_sensitive_path(str(parent))
                            ):
                                paths.add(str(parent))
                            elif target.is_dir() and is_sensitive_path(str(parent)):
                                logger.debug(
                                    "Skipping sensitive path: %s",
                                    parent,
                                )
                                try:
                                    sel().log_api_access(
                                        caller="system",
                                        operation="skill_path_rejected",
                                        outcome="denied",
                                        source="agent",
                                        resources=str(parent),
                                        error="sensitive_path",
                                    )
                                except Exception:
                                    logger.debug(
                                        "Failed to emit SEL audit event for sensitive path rejection: %s",
                                        parent,
                                        exc_info=True,
                                    )
                            elif target.is_dir() and parent.name != "skills":
                                # `--local` skill installs always target a
                                # skills/ directory; non-standard layouts are
                                # intentionally skipped for consistency.
                                logger.debug(
                                    "Skipping symlink %s: parent %r is not 'skills'",
                                    entry.name,
                                    parent.name,
                                )
                        except OSError as exc:
                            logger.debug("Skipping unresolvable symlink %s: %s", entry, exc)
        aim_pkgs = aim_dir / "packages"
        if aim_pkgs.is_dir():
            for pkg in aim_pkgs.iterdir():
                if not pkg.is_dir() or pkg.name.startswith("."):
                    continue
                sd = pkg / "skills"
                if sd.is_dir():
                    paths.add(str(sd))
                # Nested variant: ~/.aim/packages/Pkg-1.0/eventId-XXX/skills/
                # Only load from currentEventId to avoid duplicates across snapshots.
                else:
                    manifest = pkg / ".aim" / ".version-manifest.json"
                    current_event = ""
                    if manifest.is_file():
                        try:
                            current_event = json.loads(manifest.read_text(encoding="utf-8")).get(
                                "currentEventId", ""
                            )
                        except (json.JSONDecodeError, OSError):
                            pass
                    for sub in pkg.iterdir():
                        if not sub.is_dir() or sub.name.startswith("."):
                            continue
                        if current_event and sub.name != f"eventId-{current_event}":
                            continue
                        ssd = sub / "skills"
                        if ssd.is_dir():
                            paths.add(str(ssd))
    # Project-level skills (legacy ``<project>/skills/``)
    proj = _paths._project_dir()
    if proj:
        sd = proj / "skills"
        if sd.is_dir():
            paths.add(str(sd))
        # Open-standard workspace location: ``<project>/.kiro/skills/`` —
        # what kiro-cli's native ``skill://`` loader scans.  Adding it here
        # so SkillsLoader sees the same set as kiro-cli does.
        kiro_proj = proj / ".kiro" / "skills"
        if kiro_proj.is_dir() and not is_sensitive_path(str(kiro_proj)):
            paths.add(str(kiro_proj))
    # User-created skills (KiroCrew convention)
    user_skills = Path.home() / ".kirocrew" / "skills"
    if user_skills.is_dir():
        paths.add(str(user_skills))
    # Open-standard global location: ``~/.kiro/skills/`` — canonical home for
    # ``cp -r my-skill ~/.kiro/skills/`` installs and AIM-published skills
    # that follow the spec.  See docs/kiro-cli/skills.md.
    kiro_user = Path.home() / ".kiro" / "skills"
    if kiro_user.is_dir() and not is_sensitive_path(str(kiro_user)):
        paths.add(str(kiro_user))
    return sorted(paths)


# Keep old name as alias for backward compat
_aim_skill_paths = _all_skill_paths


def build_agent_config() -> dict:
    """Return the final agent config (shipped defaults + user overrides + dynamic fields).

    Security-critical fields (``deniedCommands``, ``hooks``) always use the
    bundled config as their base, even when a project-dir override is present.
    This prevents dev overrides from silently dropping security controls.
    User-defined ``kiro_hooks`` from ``~/.kirocrew/config.json`` are then
    additively merged; bundled hooks always run first and cannot be removed.
    """
    config = _load_json(_paths._shipped_defaults())
    config = _deep_merge(config, _load_json(_paths._USER_OVERRIDES))

    # Ensure deniedCommands and hooks always come from the bundled config,
    # even if the project-level defaults.json is stale.
    bundled = _load_json(_paths._BUNDLED_CFG_DIR / "defaults.json")
    bundled_dc = bundled.get("toolsSettings", {}).get("execute_bash", {}).get("deniedCommands")
    if bundled_dc:
        config.setdefault("toolsSettings", {}).setdefault("execute_bash", {})[
            "deniedCommands"
        ] = bundled_dc
    bundled_hooks = bundled.get("hooks")
    if not bundled_hooks:
        raise RuntimeError("Cannot build agent config: hooks missing from bundled defaults")
    config["hooks"] = _hooks._kiro_hooks_only(bundled_hooks)

    # Merge user-defined kiro_hooks from ~/.kirocrew/config.json (additive).
    mc_cfg = _load_json(_mc_config_path()) or {}
    _hooks._apply_user_kiro_hooks(config, mc_cfg)

    # Dynamic fields — always resolved at install time
    config["prompt"] = f"file://{_prompt_path()}"
    mcp = config.setdefault("mcpServers", {})
    for name, spec in _paths._MANAGED_MCP_SERVERS.items():
        if "invocation_fn" in spec:
            cmd, args = spec["invocation_fn"]()
        else:
            cmd = spec.get("command") or spec["command_fn"]()
            args = list(spec["args"])
        entry = {"command": cmd, "args": args}
        if "autoApprove" in spec:
            entry["autoApprove"] = list(spec["autoApprove"])
        mcp[name] = entry

    # Edition-contributed MCP servers (PlatformContext).  ADD-only: standalone
    # contributes {} (unchanged), the Amazon companion adds builder-mcp etc.
    # Entries are already kiro-spec-shaped, so we only extend the map — no spec
    # restructuring, deny_unknown_fields invariant preserved.
    for name, spec in _paths._extra_mcp_servers().items():
        mcp.setdefault(name, dict(spec))

    # Default-model tracking ("managed" vs frozen) is recorded in the
    # agent_state sidecar by the install path (rebuild_agent_config), never as
    # a kiro-spec key — kiro-cli rejects unknown fields and would drop the whole
    # spec. build_agent_config stays pure (no disk writes) so its many
    # read-only callers don't mutate managed-state as a side effect.
    return config


def _refresh_dynamic_fields(config: dict) -> None:
    """Update security-critical and dynamic fields in an existing config.

    Called when ``kirocrew.json`` already exists so user customizations are
    preserved while security controls and runtime paths stay current.
    """
    # Prompt URI — always resolve at install time
    config["prompt"] = f"file://{_prompt_path()}"

    # Managed MCP servers — ensure present and up-to-date.
    # Only refresh command/args; preserve user customizations (e.g. autoApprove).
    mcp = config.setdefault("mcpServers", {})
    for name, spec in _paths._MANAGED_MCP_SERVERS.items():
        is_new = name not in mcp
        entry = mcp.setdefault(name, {})
        if "invocation_fn" in spec:
            entry["command"], entry["args"] = spec["invocation_fn"]()
        else:
            entry["command"] = spec.get("command") or spec["command_fn"]()
            entry["args"] = list(spec["args"])
        # Strip any stale remote-transport fields from older builds: these
        # servers are stdio-only, and a leftover ``url`` would otherwise
        # propagate into the CC config and shadow the command. (Root fix for
        # the downstream stdio-force in cc_agent / acp.client.)
        entry.pop("url", None)
        entry.pop("headers", None)
        # Seed autoApprove only for genuinely new entries; if the user
        # deliberately removed autoApprove from an existing entry we
        # must not re-add it on every refresh.
        if "autoApprove" in spec and is_new:
            entry["autoApprove"] = list(spec["autoApprove"])

    # Edition-contributed MCP servers (PlatformContext).  ADD-only: only seed a
    # server the user doesn't already have, so user customizations on a refresh
    # are preserved.  Standalone contributes {} (unchanged); Amazon adds
    # builder-mcp etc.  Already kiro-spec-shaped — no restructuring.
    for name, extra_spec in _paths._extra_mcp_servers().items():
        mcp.setdefault(name, dict(extra_spec))

    # Security: deniedCommands and hooks always from bundled config.
    # Hard-fail if bundled defaults are missing — deny-by-default.
    bundled = _load_json(_paths._BUNDLED_CFG_DIR / "defaults.json")
    if bundled is None:
        raise RuntimeError(
            "Cannot refresh security fields: bundled defaults.json is missing or unreadable"
        )
    if not isinstance(bundled, dict):
        raise RuntimeError(
            "Cannot refresh security fields: bundled defaults.json is not a JSON object"
        )

    bundled_dc = bundled.get("toolsSettings", {}).get("execute_bash", {}).get("deniedCommands")
    if not bundled_dc:
        raise RuntimeError(
            "Cannot refresh security fields: deniedCommands missing from bundled defaults"
        )
    config.setdefault("toolsSettings", {}).setdefault("execute_bash", {})[
        "deniedCommands"
    ] = bundled_dc

    bundled_hooks = bundled.get("hooks")
    if not bundled_hooks:
        raise RuntimeError("Cannot refresh security fields: hooks missing from bundled defaults")
    config["hooks"] = _hooks._kiro_hooks_only(bundled_hooks)

    # Merge user-defined kiro_hooks from ~/.kirocrew/config.json (additive).
    mc_cfg = _load_json(_mc_config_path()) or {}
    _hooks._apply_user_kiro_hooks(config, mc_cfg)

    # Model migration — replace deprecated model names with current equivalents.
    # Uses the canonical map from chat.py plus legacy pre-4.6 models.
    _model_migration = {
        "claude-opus-4.6-1m": "claude-opus-4.6",
        "claude-sonnet-4.6-1m": "claude-sonnet-4.6",
    }
    cur_model = config.get("model", "")
    if cur_model in _model_migration:
        config["model"] = _model_migration[cur_model]

    # Self-heal: lift any stray KiroCrew bookkeeping keys into the sidecar and
    # strip them from the spec so kiro-cli (deny_unknown_fields) accepts it.
    # This is the steady-state safety net that cleans specs polluted by older
    # builds on the next refresh; the one-time migrate_agent_specs() at startup
    # handles the rest of ~/.kiro/agents/.
    name = config.get("name") or _MAIN_AGENT_NAME
    if "model_managed" in config:
        if agent_state.get_model_managed(name) is None:
            agent_state.set_model_managed(name, bool(config["model_managed"]))
        del config["model_managed"]
    if "cc_model" in config:
        if agent_state.get_cc_model(name) is None and config["cc_model"]:
            agent_state.set_cc_model(name, str(config["cc_model"]))
        del config["cc_model"]

    # Default-model tracking: when the model is managed (not an explicit user
    # pick), re-sync it from the shipped defaults.json so a default bump
    # propagates to existing installs. Agents with no sidecar entry are
    # grandfathered and left untouched (never force-changed).
    if agent_state.get_model_managed(name):
        shipped_model = (_load_json(_paths._shipped_defaults()) or {}).get("model")
        if shipped_model:
            config["model"] = shipped_model

    # config.json agent.model is the user-facing authority (kirocrew config set
    # agent.model). An explicit pick (not the "auto" sentinel) is propagated into
    # the agent file so kiro-cli's --agent startup load matches it; otherwise the
    # stale agent-file model shadows config.json and session/set_model loses the
    # startup race (Mesh-2292). "auto" defers to managed/shipped resolution above.
    mc_model = (mc_cfg.get("agent") or {}).get("model")
    if mc_model and mc_model != "auto":
        config["model"] = mc_model

    # Ensure kiro-cli uses agent-level mcpServers exclusively (not global
    # mcp.json).  Existing configs created before this field was added lack
    # it, causing kiro-cli to fall back to the (possibly empty) global file.
    config["includeMcpJson"] = False

    # Seed workspace-relative resources (steering files, AGENTS.md, etc.)
    # only when the user hasn't customized them.  kiro-cli normalizes
    # missing ``resources`` to ``[]`` on read, so existing users created
    # before this field shipped end up with an empty list that prevents
    # ``.kiro/steering/**/*.md`` and friends from auto-loading.  If the user
    # has explicitly listed their own resources, leave them alone.
    bundled_resources = bundled.get("resources")
    if isinstance(bundled_resources, list) and bundled_resources and not config.get("resources"):
        config["resources"] = list(bundled_resources)

    # tools/allowedTools: intentionally not modified on existing configs.
    # User controls these lists entirely.


def get_shipped_tools() -> dict[str, list[str]]:
    """Return shipped tool lists. Public API for cross-module use."""
    shipped = _load_json(_paths._shipped_defaults()) or {}
    return {k: shipped.get(k, []) for k in ("tools", "allowedTools")}


def _load_existing_config(path: Path) -> tuple[dict, bool]:
    """Load and refresh an existing kirocrew.json.

    Returns (config, fresh_install).  Falls back to build_agent_config()
    when the file is corrupt or refresh fails.
    """
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        config = None
    if not isinstance(config, dict):
        return build_agent_config(), True
    try:
        _refresh_dynamic_fields(config)
    except (AttributeError, TypeError, RuntimeError) as exc:
        logger.error("Refresh failed, rebuilding from defaults: %s", exc)
        return build_agent_config(), True
    return config, False


def _norm_mcp_spec(spec: Any) -> Any:
    """Return the comparison form of an ``mcpServers`` spec for dedup.

    Setup / re-installs re-emit the same server across runs with slightly
    different optional-key *shapes*: a bare ``{"command": ...}`` one run, then
    ``"env": {}`` or ``"args": []`` the next. Comparing raw dicts treats those
    as distinct servers, so ``_normalize_mcp_server_keys`` mints an ever-growing
    ``-2``/``-3``... suffix on every build / reinstall / update (Mesh-2593).
    Dropping empty optional collections makes semantically identical re-merges
    collapse onto the canonical alias. An empty ``env``/``args`` is a launch
    no-op for kiro-cli (missing == empty), so this is also the cleaner spec to
    persist.
    """
    if not isinstance(spec, dict):
        return spec
    return {k: v for k, v in spec.items() if not (k in ("env", "args") and not v)}


def _normalize_mcp_server_keys(config: dict) -> None:
    """Rewrite any slash-containing ``mcpServers`` key to its slash-free alias.

    Mutates ``config`` in place: moves each affected server spec under its
    alias key and rewrites (and de-duplicates) the matching ``@oldkey`` ->
    ``@alias`` reference in ``tools``/``allowedTools``.  Migrates already-broken
    existing configs.  Idempotent: slash-free keys are left untouched and a
    re-merged duplicate collapses onto the canonical alias (no churn).

    Dedup is by *normalized* spec (:func:`_norm_mcp_spec`), so a re-added key
    that differs only by an empty ``env``/``args`` reuses the existing alias
    instead of accumulating a fresh ``-N`` suffix on every build / reinstall /
    update (Mesh-2593). Convergence: any already-suffixed sibling that is an
    equivalent duplicate is folded back onto the surviving alias (its ``@ref``
    is redirected), so a config already polluted by the pre-fix bug self-heals.

    Collision: if the alias is held by a *genuinely different* spec, the server
    is preserved under the lowest free numeric-suffixed alias (``-2``, ``-3``)
    -- never dropped. Managed servers (slash-free by construction) are skipped
    so their dynamic-field refresh is never disturbed.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return
    managed = set(_paths._MANAGED_MCP_SERVERS)

    def _is_family(key: str, base: str) -> bool:
        """True if ``key`` is ``base`` or a ``base-<n>`` numeric-suffixed sibling."""
        return key == base or (key.startswith(f"{base}-") and key[len(base) + 1 :].isdigit())

    def _rewrite_ref(old_ref: str, new_ref: str) -> None:
        for key in ("tools", "allowedTools"):
            lst = config.get(key)
            if isinstance(lst, list):
                config[key] = list(dict.fromkeys(new_ref if t == old_ref else t for t in lst))

    for old_key in [k for k in servers if "/" in k and k not in managed]:
        spec = _norm_mcp_spec(servers.pop(old_key))
        base = mcp_server_alias(old_key)

        # Reuse an existing home for an equivalent spec — the canonical alias or
        # any already-suffixed sibling — instead of minting a new suffix, so
        # repeated re-merges converge rather than accumulate.
        alias = next(
            (k for k in servers if _is_family(k, base) and _norm_mcp_spec(servers[k]) == spec),
            None,
        )
        if alias is None:
            # Genuinely distinct spec (or nothing here yet): take the canonical
            # alias if free, else the lowest free numeric suffix (never drop a
            # distinct server).
            alias = base
            if alias in servers:
                n = 2
                while f"{alias}-{n}" in servers:
                    n += 1
                alias = f"{alias}-{n}"
        servers[alias] = spec
        _rewrite_ref(f"@{old_key}", f"@{alias}")

        # Converge any OTHER sibling that duplicates the spec we just placed
        # (self-heals configs polluted by the pre-fix bug): drop it and redirect
        # its @ref onto the surviving alias.
        for dup in [
            k
            for k in list(servers)
            if k != alias and _is_family(k, base) and _norm_mcp_spec(servers[k]) == spec
        ]:
            del servers[dup]
            _rewrite_ref(f"@{dup}", f"@{alias}")

        logger.info("Normalized MCP server key %r -> %r (kiro-safe)", old_key, alias)


def migrate_agent_specs() -> int:
    """Strip KiroCrew bookkeeping keys from kiro agent specs into the sidecar.

    kiro-cli validates ``~/.kiro/agents/*.json`` with ``deny_unknown_fields``
    and rejects the entire spec on any unknown field (``model_managed`` /
    ``cc_model``), then silently falls back to the default agent. This lifts
    those values into ``agent_state`` and removes them from each spec so every
    agent loads. Idempotent and cheap (a handful of small JSON files); safe to
    run on every gateway start. Returns the number of spec files cleaned.
    """
    if not _paths.KIRO_AGENTS_DIR.is_dir():
        return 0
    cleaned = 0
    for spec_path in sorted(_paths.KIRO_AGENTS_DIR.glob("*.json")):
        try:
            data = json.loads(spec_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if "model_managed" not in data and "cc_model" not in data:
            continue
        name = data.get("name") or spec_path.stem
        if "model_managed" in data:
            # Don't clobber an authoritative sidecar value with a stale spec one.
            if agent_state.get_model_managed(name) is None:
                agent_state.set_model_managed(name, bool(data["model_managed"]))
            del data["model_managed"]
        if "cc_model" in data:
            if agent_state.get_cc_model(name) is None and data["cc_model"]:
                agent_state.set_cc_model(name, str(data["cc_model"]))
            del data["cc_model"]
        try:
            _paths._atomic_json_write(spec_path, data)
            cleaned += 1
        except OSError as exc:
            logger.warning("Could not rewrite cleaned agent spec %s: %s", spec_path, exc)
    if cleaned:
        logger.info("Cleaned %d kiro agent spec(s) of KiroCrew bookkeeping keys", cleaned)
    return cleaned


def rebuild_agent_config(*, clean: bool = False) -> Path:
    """Rebuild and write the merged kirocrew.json to ~/.kiro/agents/.

    This is the single authoritative function for producing the agent config.
    It reads all source files, merges with correct priority, resolves commands,
    and injects fresh AIM skill paths.

    Merge priority (highest wins):
      1. ~/.kirocrew/mcp.json (agent-specific overrides)
      2. ~/.kiro/settings/mcp.json (kiro global, fills gaps)
      3. Existing kirocrew.json (preserves user customizations)
      4. Bundled defaults (security, managed servers)

    --skill-paths are always resolved fresh from AIM manifests regardless
    of what any source file contains.

    When the config already exists and *clean* is False, the existing file
    is used as the base so that **all** user customizations are preserved.
    Only security-critical fields (``deniedCommands``, ``hooks``) and
    dynamic fields (``prompt`` URI, kirocrew MCP server commands) are
    refreshed from defaults.

    Args:
        clean: If True, ignore existing config and regenerate from defaults.
    """
    # Deferred import breaks the builder<->workers/repair cycle: rebuild
    # orchestrates the install helpers, which import builder at load time.
    from kiro_crew.agent import repair as _repair  # noqa: PLC0415
    from kiro_crew.agent import workers as _workers  # noqa: PLC0415

    _paths.KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _paths.KIRO_AGENTS_DIR / _paths.AGENT_FILENAME

    # One-time (idempotent) self-heal: strip KiroCrew bookkeeping keys from
    # every kiro agent spec into the sidecar so kiro-cli accepts them all.
    migrate_agent_specs()

    # Managed MCP sync happens after config is fully built (see below).

    if not clean and path.exists():
        # Existing config — preserve user customizations, only refresh
        # security-critical and dynamic fields.
        config, fresh_install = _load_existing_config(path)
    else:
        config = build_agent_config()
        fresh_install = True

    # Seed default-model tracking for a fresh/clean build. A clean regen always
    # resumes tracking the shipped default; a first-time install seeds tracking
    # only when the sidecar has no prior (possibly frozen) choice to preserve.
    main_name = config.get("name") or _MAIN_AGENT_NAME
    if fresh_install and (clean or agent_state.get_model_managed(main_name) is None):
        agent_state.set_model_managed(main_name, True)

    # Merge shared MCP servers from ~/.kiro/settings/mcp.json (Kiro user-level
    # config) FIRST.  KiroCrew is kiro-first (ACP/kiro-cli only), so Kiro
    # global OUTRANKS the Claude Code global on collisions — setdefault makes
    # the first writer win.  Skip managed servers — their command/args are set
    # by _refresh_dynamic_fields() and must not be overwritten by stale global
    # entries.  Write-through is never done here (KiroCrew reads globals but
    # never mutates them).
    #
    # NOTE: this reverses the prior "CC global wins over Kiro global" rule.
    # See docs/mcp-architecture.md. CC global is kept only as a gap-filler so
    # the Claude Code provider can be re-enabled later without rework; it must
    # not shadow a Kiro-global entry.
    managed_names = set(_paths._MANAGED_MCP_SERVERS)
    shared_mcp = _load_json(_paths._KIRO_MCP_JSON).get("mcpServers", {})
    for name, spec in shared_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            # Copy so config never aliases the source dict — a later update()
            # (kirocrew merge) must not mutate shared_mcp, which is reused as a
            # fallback candidate during command validation below.
            config.setdefault("mcpServers", {}).setdefault(name, dict(spec))

    # Merge shared MCP servers from ~/.claude.json (Claude Code user-level
    # config) — now LOWER priority than Kiro global; setdefault is a no-op when
    # Kiro already populated the same key, so CC only fills gaps.
    cc_shared_mcp = _load_json(_paths._CC_MCP_JSON).get("mcpServers", {})
    for name, spec in cc_shared_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            # Copy (see note above) so cc_shared_mcp stays pristine for the
            # fallback-candidate lookup.
            config.setdefault("mcpServers", {}).setdefault(name, dict(spec))

    # ~/.kirocrew/mcp.json overrides kiro mcp.json for the kirocrew agent —
    # kirocrew-specific config wins in a tie.
    # Uses update() to merge into existing specs, preserving user-set fields
    # like autoApprove while letting kirocrew's command/args/env win.
    # Skip managed servers for the same reason as above.
    kirocrew_mcp = _load_json(_paths._USER_DIR / "mcp.json").get("mcpServers", {})
    for name, spec in kirocrew_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            mcps = config.setdefault("mcpServers", {})
            if name in mcps and isinstance(mcps[name], dict):
                # mcps[name] is a private copy (globals were copied in above),
                # so update() no longer mutates any source dict.
                mcps[name].update(spec)
            else:
                mcps[name] = dict(spec)

    # Resolve MCP commands to absolute paths and validate.
    #
    # Resolution-aware fallback: a server can be defined in several sources
    # with different commands.  If the merged winner's command does not
    # resolve (e.g. a bare command whose binary isn't on the rebuild PATH —
    # the classic builder-mcp shadowing case), fall back to the SAME server's
    # spec from the other sources before dropping it, in priority order
    # (kirocrew > kiro-global > cc-global).  This prevents one source's
    # unresolvable command from killing a server another source can resolve.
    def _resolve_command(cmd: str, env: dict | None) -> str | None:
        """Resolve an MCP command to an absolute path, or None if not found.

        Accepts an absolute path directly when the file exists and is
        executable — shutil.which can fail inside user-namespace sandboxes
        even when the file is fine.

        Searches the server's own env.PATH first, then the same augmented
        PATH the MCP probe uses (mcp_discovery.probe_server →
        env.augmented_path). Sharing augmented_path — instead of a hand-built
        dir list — keeps agent-config-build resolution and probe resolution
        from diverging: a divergence previously let a server probe healthy on
        the dashboard while being silently dropped from the generated agent
        config ("command not found: kirocrew", Mesh-2329). augmented_path
        covers ~/.aim/mcp-servers and ~/.toolbox/bin (previously hardcoded
        here) and appends the running interpreter's console-scripts dir
        (venv ``Scripts\\`` on Windows, ``bin/`` on POSIX) as a last-resort
        fallback for pip-generated wrappers like ``kirocrew``.
        """
        if not cmd:
            return None
        if os.path.isabs(cmd) and os.path.isfile(cmd) and os.access(cmd, os.X_OK):
            return cmd
        env_path = (env or {}).get("PATH", "")
        base = os.pathsep.join(filter(None, [env_path, os.environ.get("PATH", "")]))
        return shutil.which(cmd, path=augmented_path(base))

    valid_servers: dict[str, Any] = {}
    for name, spec in config.get("mcpServers", {}).items():
        if not isinstance(spec, dict):
            continue
        # Remote Streamable HTTP servers — preserve as-is (url-based, no command)
        if spec.get("url"):
            valid_servers[name] = spec
            continue
        # Build candidate specs in priority order: the merged winner first,
        # then the same server from each source as a resolution fallback.
        candidates: list[tuple[str, dict]] = [("winner", spec)]
        for label, src in (
            ("kirocrew", kirocrew_mcp),
            ("kiro-global", shared_mcp),
            ("cc-global", cc_shared_mcp),
        ):
            alt = src.get(name)
            if isinstance(alt, dict) and alt is not spec:
                candidates.append((label, alt))

        resolved: str | None = None
        chosen: dict = spec
        tried: list[str] = []
        had_any_command = False
        for label, cand in candidates:
            cmd = cand.get("command", "")
            if cmd:
                had_any_command = True
            r = _resolve_command(cmd, cand.get("env"))
            tried.append(f"{label}={cmd or '<none>'}{' -> ok' if r else ''}")
            if r:
                resolved = r
                chosen = cand
                break

        if resolved:
            # Start from the merged winner so user-set NON-command fields
            # (autoApprove, disabled, ...) are preserved.  When we fall back to
            # a *different* source, adopt that source's command/args/env as a
            # unit (args belong with their command) — drop the winner's stale
            # args/env so we never pair one source's command with another's
            # args.
            merged = dict(spec)
            merged["command"] = resolved
            if chosen is not spec:
                merged.pop("args", None)
                merged.pop("env", None)
                if "args" in chosen:
                    merged["args"] = chosen["args"]
                if "env" in chosen:
                    merged["env"] = chosen["env"]
            valid_servers[name] = merged
        elif not had_any_command:
            # No candidate defined a command at all — distinct from a command
            # that was defined but couldn't be resolved.
            logger.warning("Dropping MCP server %r: no command", name)
        else:
            logger.warning(
                "Dropping MCP server %r: command not found: %s",
                name,
                spec.get("command", ""),
            )
            logger.debug("MCP %r resolution failed; tried %s", name, "; ".join(tried))
    config["mcpServers"] = valid_servers

    # Rewrite slash-containing server keys to kiro-safe aliases (also migrates
    # already-broken configs); runs after merges so global-only servers and
    # their stale @refs are normalized too. See mcp_server_alias / Mesh-1956.
    _normalize_mcp_server_keys(config)

    # Sync shared (user-installed) servers to tools/allowedTools.
    # These are explicitly installed by the user via `aim mcp install` or
    # manual mcp.json edits — unlike managed servers, they should always
    # be registered regardless of fresh/existing config state.
    _shared_added: list[str] = []
    _shared_removed: list[str] = []
    for name, spec in itertools.chain(cc_shared_mcp.items(), shared_mcp.items()):
        if not isinstance(spec, dict) or name in managed_names:
            continue
        alias = mcp_server_alias(name)
        ref = f"@{alias}"
        if spec.get("disabled"):
            for key in ("tools", "allowedTools"):
                lst = config.get(key)
                if lst is not None and ref in lst:
                    lst.remove(ref)
                    if ref not in _shared_removed:
                        _shared_removed.append(ref)
        elif alias in valid_servers:
            valid_servers[alias].pop("disabled", None)
            for key in ("tools", "allowedTools"):
                if ref not in config.get(key, []):
                    config.setdefault(key, []).append(ref)
                    if ref not in _shared_added:
                        _shared_added.append(ref)
    if _shared_added:
        sel().log_api_access(
            caller="system",
            operation="mcp_tools_added",
            outcome="ok",
            source="install_agent",
            resources=f"{', '.join(_shared_added)} added to tools/allowedTools (shared)",
        )
    if _shared_removed:
        sel().log_api_access(
            caller="system",
            operation="mcp_tools_removed",
            outcome="ok",
            source="install_agent",
            resources=f"{', '.join(_shared_removed)} removed from tools/allowedTools (disabled)",
        )

    # On fresh installs, ensure managed MCP tools are in tools (but NOT
    # allowedTools — new MCPs may have destructive tools; user opts in).
    # On existing configs, don't touch tools/allowedTools — user controls those.
    if fresh_install:
        added_refs: list[str] = []
        for mcp_name in _paths._MANAGED_MCP_SERVERS:
            ref = f"@{mcp_name}"
            if mcp_name in valid_servers:
                if ref not in config.get("tools", []):
                    config.setdefault("tools", []).append(ref)
                    added_refs.append(ref)
        if added_refs:
            sel().log_api_access(
                caller="system",
                operation="mcp_tools_added",
                outcome="ok",
                source="install_agent",
                resources=f"{', '.join(added_refs)} added to tools (fresh install)",
            )

    # Final dedup (preserves order).
    for key in ("tools", "allowedTools"):
        config[key] = list(dict.fromkeys(config.get(key, [])))

    _paths._atomic_json_write(path, config)
    logger.info("Installed agent config: %s", path)

    # Install KiroCrew AIM capabilities package (includes kirocrew-lite)
    _workers._install_aim_capabilities()

    # Install kirocrew-knowledge agent (used by Knowledge Library LLMPool)
    try:
        _workers._install_knowledge_agent()
    except Exception:
        logger.debug("kirocrew-knowledge agent install failed", exc_info=True)

    # Install kirocrew-research agent (used by the Research Lab campaign loop)
    try:
        _workers._install_research_agent()
    except Exception:
        logger.debug("kirocrew-research agent install failed", exc_info=True)

    # Install kirocrew-heartbeat agent (used by HeartbeatService for unattended polling)
    try:
        _workers._install_heartbeat_agent()
    except Exception:
        logger.debug("kirocrew-heartbeat agent install failed", exc_info=True)

    # Bidirectional sync: ensure packages installed for one provider
    # are also available for the other (agents↔plugins, skills).
    _workers.sync_aim_packages()

    # Security: enforce deniedCommands + sanitize invalid hook keys
    _repair.repair_agent_configs()

    return path


# Backward-compat alias — callers may still use the old name.
install_agent = rebuild_agent_config
