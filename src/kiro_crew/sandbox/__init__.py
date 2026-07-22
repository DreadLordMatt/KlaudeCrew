"""Backward-compatible re-export shim for the sandbox package.

``sandbox.py`` was split into focused submodules (backends, launcher, seatbelt,
policy, cgroups, core). Every name that was importable from ``kiro_crew.sandbox``
before the split remains importable here: this package ``__init__`` imports each
submodule and republishes their module-level names into this namespace, so
existing ``from kiro_crew.sandbox import X`` call sites (and
``kiro_crew.sandbox._STRICT_DIRS`` / ``_CC_DIRS`` reads from
``platform/defaults.py``) keep working unchanged.

Module dependency DAG (leaves first, no import cycles):

    backends   (stdlib + kiro_crew.platform; owns the shared credential/env
                lists, the availability probes, and the backend cache)
    launcher  -> backends            (namespace launcher template + argv)
    seatbelt  -> backends, launcher  (Seatbelt profile + sandbox-exec argv)
    policy    -> seatbelt            (config opt-ins, mode clamp, kiro delegation)
    cgroups    (standalone)          (cgroup v2 scope + RLIMIT preexecs)
    core      -> backends, launcher, seatbelt, policy, cgroups (wrap_argv +
                scrubbers + sandboxed_spawn_argv)

The mutable process caches (``_backend`` / ``_last_unshare_failure`` /
``_warm_thread`` in ``backends``; the cgroup/preexec caches in ``cgroups``;
``_kiro_delegation_warned`` in ``policy``) are single-owned by their submodule.
``reset_backend()`` is a function that mutates ``backends._backend`` via
``global``, so a reset through this shim clears the real cache. Tests that
directly assign or patch a name resolved *internally* within a submodule must
target that submodule (e.g. ``kiro_crew.sandbox.backends.detect_backend``,
``kiro_crew.sandbox.cgroups._RESOURCE_PREEXEC``) rather than this shim.
"""

from __future__ import annotations

from . import (  # noqa: F401  (submodules republished below)
    backends,
    cgroups,
    core,
    launcher,
    policy,
    seatbelt,
)

# Republish every module-level name from each submodule, reproducing the flat
# namespace of the former ``sandbox.py`` (including private, underscore-prefixed
# helpers that call sites / tests import directly). Iteration order mirrors the
# dependency DAG (leaves first) for readability only. Names shared across
# submodules (e.g. ``logger``, ``_STANDARD_DIRS``) resolve to a single object,
# so re-binding is idempotent.
_submodules = (backends, launcher, seatbelt, policy, cgroups, core)

for _mod in _submodules:
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_mod, _name)

del _mod, _name, _submodules
