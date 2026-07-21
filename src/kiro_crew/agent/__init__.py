"""Backward-compatible re-export shim for the agent package.

``agent.py`` was split into focused submodules (paths, hooks, prompts,
builder, workers, repair). Every name that was importable from
``kiro_crew.agent`` before the split remains importable here: this package
``__init__`` imports each submodule and republishes their module-level names
into this namespace, so existing ``from kiro_crew.agent import X`` call sites
keep working unchanged.

Module dependency DAG (leaves first, no import cycles):

    paths     (stdlib + platform_compat + platform context)
    hooks     (stdlib + security + sel; no intra-package deps)
    prompts   (string constants only)
    builder   -> paths, hooks   (defers workers/repair inside rebuild)
    workers   -> paths, prompts, builder
    repair    -> paths, hooks, builder, workers

Runtime-mutated module globals live in exactly one module and are accessed by
reference there (never copied into the shim):
  * ``_KIROCREW_BIN``                       -> paths
  * ``_denied_cmd_mtimes`` / ``_last_skipped_set`` / ``_hooks_sanitized_mtimes``
                                            -> repair

Tests that patch a name resolved *internally* within a submodule must target
that submodule (e.g. ``kiro_crew.agent.paths.KIRO_AGENTS_DIR`` or
``kiro_crew.agent.repair._enforce_denied_commands``) rather than this shim.

The backward-compat aliases ``install_agent = rebuild_agent_config`` and
``_aim_skill_paths = _all_skill_paths`` are defined in ``builder`` and are
therefore republished here automatically.
"""

from __future__ import annotations

from . import (
    builder,
    hooks,
    paths,
    prompts,
    repair,
    workers,
)

# Republish every module-level name from each submodule. Iteration order
# mirrors the dependency DAG (leaves first); later modules win on any name
# collision (e.g. the shared ``logger``), which is harmless since colliding
# names refer to equivalent objects. This reproduces the flat namespace of the
# former agent.py, including private (underscore-prefixed) helpers that call
# sites and tests import directly.
_submodules = (
    paths,
    hooks,
    prompts,
    builder,
    workers,
    repair,
)

for _mod in _submodules:
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_mod, _name)

del _mod, _name, _submodules
