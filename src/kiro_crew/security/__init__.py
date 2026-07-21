"""Backward-compatible re-export shim for the security package.

``security.py`` was split into focused submodules (shell, resource_limits,
credentials, exfiltration, sensitive_paths, git_push, denylist, injection).
Every name that was importable from ``kiro_crew.security`` before the split
remains importable here: this package ``__init__`` imports each submodule and
republishes their module-level names into this namespace, so existing
``from kiro_crew.security import X`` call sites keep working unchanged.

Module dependency DAG (leaves first, no import cycles):

    shell            (stdlib only)
    resource_limits  (stdlib only)
    credentials      (stdlib only; redact() defers the exfiltration import)
    exfiltration  -> credentials
    sensitive_paths -> shell
    git_push      -> shell
    denylist      -> git_push (-> shell)
    injection     -> credentials, exfiltration, denylist

Tests that patch a name resolved *internally* within a submodule must target
that submodule (e.g. ``kiro_crew.security.denylist._emit_deny_event``) rather
than this shim.
"""

from __future__ import annotations

from . import (
    credentials,
    denylist,
    exfiltration,
    git_push,
    injection,
    resource_limits,
    sensitive_paths,
    shell,
)

# Republish every module-level name from each submodule. Iteration order mirrors
# the dependency DAG (leaves first) for readability only; Python resolves the
# submodule imports on demand regardless of order. Names are single objects
# shared across submodules (e.g. ``redact_credentials``), so re-binding is
# idempotent. This reproduces the flat namespace of the former security.py,
# including the private (underscore-prefixed) helpers that tests import directly.
_submodules = (
    shell,
    resource_limits,
    credentials,
    exfiltration,
    sensitive_paths,
    git_push,
    denylist,
    injection,
)

for _mod in _submodules:
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_mod, _name)

del _mod, _name, _submodules
