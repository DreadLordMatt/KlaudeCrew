"""cgroup v2 scope enforcement and the RLIMIT preexec callables (fork-bomb /
memory-DoS / file-descriptor ceilings) for agent-influenced spawns."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from typing import TYPE_CHECKING

try:
    import resource as _resource_mod
except ImportError:  # non-POSIX (Windows)
    _resource_mod = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("kiro_crew.sandbox")


# ── cgroup v2 scope enforcement (fork bomb + memory DoS) ──
# The RLIMIT preexec (resource_limit_preexec) caps a SINGLE process's FDs, but
# RLIMIT is the wrong tool for the finding's headline threats: RLIMIT_NPROC is
# per-real-UID (not per-spawn-subtree) and RLIMIT_AS caps virtual not resident
# memory. cgroup v2 pids.max / memory.max are the correct per-cgroup ceilings —
# they bound the agent + all its MCP-server/tool descendants as one unit, and
# the kernel enforces at fork()/alloc time (no reaper race). We place each
# agent-influenced spawn in a transient systemd --user --scope, which works
# UNPRIVILEGED when the user session has cgroup v2 delegation (pids + memory
# controllers). See docs/resource-protection.md (Talos bdf0d7e5).

# Default cgroup ceilings (per agent scope). Overridable via the same
# ``resource_limits`` config block used by apply_resource_limits.
_CGROUP_DEFAULT_MAX_PROCESSES = 8192  # pids.max counts TASKS (threads), not processes;
# 1024 starved legitimate JVM build trees (Gradle + parallel test workers need
# thousands of threads -> pthread_create EAGAIN / 'unable to create native thread'
# while the host is idle); 8192 still bounds fork bombs which spawn tens of
# thousands of tasks near-instantly. Override via resource_limits.max_processes.

# CPUWeight — proportional CPU share for agent scopes (systemd default is 100).
# Setting 50 makes agent scopes yield to interactive work under CPU contention
# while still using 100% of idle CPU — proportional share, never a hard throttle.
# Both grok-build and OpenClaw ship no default CPU quota; fair-share weight is
# the correct default for agent workloads that include legitimate builds.
_CGROUP_DEFAULT_CPU_WEIGHT = 50

# The memory.max default is HOST-PROPORTIONAL, not a flat cap: the agent
# subprocess tree may occupy up to this fraction of physical RAM before the
# kernel OOM-kills the scope. This is a PER-SCOPE ceiling (each spawn gets its
# own transient scope), so 65% bounds a single runaway tree to a share that
# leaves headroom for the OS + gateway — it is NOT an aggregate host guarantee
# across many concurrent scopes. It gives the agent real headroom on the 16–32
# GB machines this targets (16 GB → ~10.6 GB, 32 GB → ~21.3 GB) — where a flat
# 8 GB cap was both too tight on big boxes and too loose on small ones. There
# is deliberately NO floor: a floor could push a tiny box above 65%, and 65% is
# the ceiling on our take.
_CGROUP_MEMORY_FRACTION = 0.65
# Fallback memory.max (MB) used only when physical RAM can't be read (sysconf
# missing/unknown). The cgroup path is Linux-only, where SC_PHYS_PAGES exists,
# so this is a belt-and-suspenders default, not the normal path.
_CGROUP_FALLBACK_MAX_MEMORY_MB = 8192


def _default_max_memory_mb() -> int:
    """Return the default cgroup ``memory.max`` in MB: a fixed fraction
    (:data:`_CGROUP_MEMORY_FRACTION`) of physical RAM, so the ceiling scales
    with the machine instead of being a flat cap. Falls back to
    :data:`_CGROUP_FALLBACK_MAX_MEMORY_MB` if host RAM can't be determined.
    """
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mb = int(total_bytes * _CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    except (ValueError, OSError, AttributeError):
        pass
    return _CGROUP_FALLBACK_MAX_MEMORY_MB


# Cached (available, reason) probe result — the environment doesn't change
# within a process, and the probe shells out, so compute it once.
_CGROUP_SCOPE_PROBE: tuple[bool, str] | None = None
_CGROUP_WARNED = False


def _probe_cgroup_scope() -> tuple[bool, str]:
    """Return (available, reason) for unprivileged cgroup-v2 scope enforcement.

    Requires, on Linux: a pure cgroup-v2 mount, the ``pids`` and ``memory``
    controllers delegated to our user slice, a ``systemd-run`` binary, and a
    user session bus (XDG_RUNTIME_DIR). Any missing piece → not available.
    """
    global _CGROUP_SCOPE_PROBE
    if _CGROUP_SCOPE_PROBE is None:
        _CGROUP_SCOPE_PROBE = _compute_cgroup_scope_probe()
    return _CGROUP_SCOPE_PROBE


def _compute_cgroup_scope_probe() -> tuple[bool, str]:
    """Uncached capability check backing :func:`_probe_cgroup_scope`."""
    if sys.platform != "linux":
        return (False, "not Linux")
    if shutil.which("systemd-run") is None:
        return (False, "systemd-run not found")
    # A user session bus is required for `systemd-run --user`.
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return (False, "no XDG_RUNTIME_DIR (no systemd user session)")
    # Pure cgroup v2 unified hierarchy.
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            # v2 is a single line beginning "0::".
            if not any(line.startswith("0::") for line in fh):
                return (False, "not a cgroup v2 unified hierarchy")
    except OSError as exc:
        return (False, f"cannot read /proc/self/cgroup: {exc}")
    # The pids + memory controllers must be delegated to our user slice, else
    # systemd-run --scope can set the knobs but the kernel won't enforce them.
    try:
        uid = os.getuid()
        ctrl_path = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/cgroup.controllers"
        with open(ctrl_path, encoding="utf-8") as fh:
            controllers = set(fh.read().split())
        missing = {"pids", "memory"} - controllers
        if missing:
            return (False, f"controllers not delegated: {sorted(missing)}")
    except OSError as exc:
        return (False, f"cannot read delegated controllers: {exc}")
    return (True, "ok")


_CPU_DELEGATED: bool | None = None


def _cpu_controller_delegated() -> bool:
    """Return True when the ``cpu`` controller is delegated to our user slice.

    CPUWeight / CPUQuota on a ``systemd-run --user`` scope are only enforced
    when the cpu controller is delegated; emitting them without delegation is
    a silent no-op at best and a warning at worst, so callers gate the CPU
    properties on this check. Cached alongside the main probe (the environment
    is process-stable). Failure to read → False (skip CPU properties, keep
    pids/memory enforcement).
    """
    global _CPU_DELEGATED
    if _CPU_DELEGATED is None:
        try:
            uid = os.getuid()
            ctrl_path = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/cgroup.controllers"
            with open(ctrl_path, encoding="utf-8") as fh:
                _CPU_DELEGATED = "cpu" in fh.read().split()
        except OSError:
            _CPU_DELEGATED = False
    return _CPU_DELEGATED


def _cgroup_limits_from_config() -> tuple[int, int, int, int]:
    """Return ``(max_processes, max_memory_mb, cpu_weight, max_cpu_percent)``
    for the cgroup scope.

    Reads the same ``resource_limits`` config block as apply_resource_limits;
    falls back to the module defaults. ``0`` (or junk) means "use default" for
    the cgroup ceiling — unlike the RLIMIT path, we never leave the cgroup DoS
    ceiling unset by default (that is the whole point of this control). The
    memory default is host-proportional (see :func:`_default_max_memory_mb`).

    ``max_cpu_percent`` is the OPT-IN hard CPU quota (``CPUQuota``): ``0``
    (the default) means "no quota property emitted at all" — hard CPU caps
    slow legitimate builds, so unlike the other ceilings this one is off
    unless an operator explicitly sets ``resource_limits.max_cpu_percent``.
    """
    max_procs = _CGROUP_DEFAULT_MAX_PROCESSES
    max_mem_mb = _default_max_memory_mb()
    cpu_weight = _CGROUP_DEFAULT_CPU_WEIGHT
    max_cpu_percent = 0  # opt-in: 0 = emit no CPUQuota
    try:
        # circular import: sandbox is a low-level module imported by
        # config/security consumers — importing kiro_crew.config.loader at
        # module load would create an import cycle, so it stays function-level
        # (same pattern as resource_limit_preexec below).
        from kiro_crew.config.loader import _raw_config

        rl = _raw_config().get("resource_limits")
        if isinstance(rl, dict):
            p = rl.get("max_processes")
            if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0:
                max_procs = int(p)
            m = rl.get("max_memory_mb")
            if isinstance(m, (int, float)) and not isinstance(m, bool) and m > 0:
                max_mem_mb = int(m)
            w = rl.get("cpu_weight")
            if isinstance(w, (int, float)) and not isinstance(w, bool) and 1 <= w <= 10000:
                cpu_weight = int(w)
            q = rl.get("max_cpu_percent")
            if isinstance(q, (int, float)) and not isinstance(q, bool) and q > 0:
                max_cpu_percent = int(q)
    except Exception:
        logger.debug("cgroup limits: config unavailable, using defaults")
    return max_procs, max_mem_mb, cpu_weight, max_cpu_percent


def cgroup_scope_argv(argv: list[str]) -> list[str]:
    """Wrap *argv* in a transient systemd --user --scope with cgroup v2 limits.

    Prepends ``systemd-run --user --scope`` with ``TasksMax`` (pids.max, the
    fork-bomb ceiling), ``MemoryMax`` + ``MemorySwapMax=0`` (memory.max, the
    RSS balloon ceiling), and — when the cpu controller is delegated —
    ``CPUWeight`` (proportional fair-share: agents run full speed on an idle
    host but yield to interactive work under contention; never a hard
    throttle) plus an OPT-IN ``CPUQuota`` hard cap
    (``resource_limits.max_cpu_percent``, off by default because hard quotas
    slow legitimate builds), so the spawned agent AND all its MCP-server/tool
    descendants are bounded as one cgroup and the kernel kills the scope on
    breach. ``--scope`` execs into the target (it does NOT fork a wrapper), so
    the returned argv's eventual PID is the real child — parent PID tracking,
    ``killpg``, and descendant scans are unaffected.

    Layers OUTSIDE the OS-level sandbox: callers pass the already-``wrap_argv``-ed
    argv here so the child is filesystem-isolated AND cgroup-bounded.

    On a host without cgroup v2 delegation (older Linux, no systemd user
    session, macOS), returns *argv* unchanged and logs a one-time loud SECURITY
    warning — the RLIMIT_NOFILE preexec still applies, but the fork-bomb/memory
    DoS ceiling is NOT enforced there.
    """
    global _CGROUP_WARNED
    available, reason = _probe_cgroup_scope()
    if not available:
        if not _CGROUP_WARNED:
            _CGROUP_WARNED = True
            logger.warning(
                "SECURITY: cgroup v2 scope enforcement unavailable (%s); agent "
                "subprocess fork-bomb / memory-DoS ceilings are NOT enforced on "
                "this host. RLIMIT_NOFILE still applies. See "
                "docs/resource-protection.md.",
                reason,
            )
        return argv
    max_procs, max_mem_mb, cpu_weight, max_cpu_percent = _cgroup_limits_from_config()
    props = [
        "-p",
        f"TasksMax={max_procs}",
        "-p",
        f"MemoryMax={max_mem_mb}M",
        "-p",
        "MemorySwapMax=0",
    ]
    # CPU properties only when the cpu controller is delegated — otherwise the
    # kernel won't enforce them and systemd may warn on every spawn.
    if _cpu_controller_delegated():
        props += ["-p", f"CPUWeight={cpu_weight}"]
        if max_cpu_percent > 0:
            props += ["-p", f"CPUQuota={max_cpu_percent}%"]
    return [
        "systemd-run",
        "--user",
        "--scope",
        "-q",
        "--slice=kirocrew-agents.slice",
        *props,
        "--",
        *argv,
    ]


# Cached preexec_fn shared by every agent-influenced spawn. Built once from the
# loaded config (limits are process-global, not per-spawn) so the hot path adds
# nothing but a dict lookup. ``_UNSET`` distinguishes "not built yet" from the
# legitimate ``None`` result on non-POSIX platforms.
_UNSET = object()
_RESOURCE_PREEXEC: object = _UNSET


def resource_limit_preexec() -> "Callable[[], None] | None":
    """Return the shared ``preexec_fn`` that caps a spawned child's resources.

    This is the companion to :func:`sandboxed_spawn_argv`: the sandbox wrapper
    gives a child filesystem + credential isolation, and this gives it a
    kernel-enforced ceiling on processes / file descriptors / CPU / memory so a
    fork bomb or runaway allocation in a compromised tool or MCP server cannot
    exhaust the host out from under the gateway. Every agent-influenced spawn
    passes the result as ``preexec_fn=`` (see ``docs/resource-protection.md``).

    Returns the callable from :func:`kiro_crew.security.apply_resource_limits`,
    or ``None`` on non-POSIX platforms (where there is nothing to enforce and
    ``preexec_fn`` must be ``None``). The callable and the underlying config
    read are computed once and cached — the limits are a host-global policy, not
    a per-spawn decision.
    """
    global _RESOURCE_PREEXEC
    if _RESOURCE_PREEXEC is _UNSET:
        if os.name != "posix":
            # Non-POSIX (Windows): preexec_fn is unsupported by
            # create_subprocess_exec and MUST be None — passing any callable
            # (even a no-op) raises ValueError. Cache None to honor the return
            # contract. (apply_resource_limits also no-ops there, but it returns
            # a callable, so we must not forward it.)
            _RESOURCE_PREEXEC = None
            return None
        # Lazy imports: sandbox is a low-level module (see the SEL import note in
        # wrap_argv) and must not import config/security at module load.
        from kiro_crew.security import apply_resource_limits

        cfg: dict | None = None
        try:
            # Raw config.json (process-cached) — carries the unrecognized
            # ``resource_limits`` key an operator may add; the typed config
            # schema drops unknown keys, so read the raw dict here.
            from kiro_crew.config.loader import _raw_config

            cfg = _raw_config()
        except Exception:
            # Config unavailable (early boot, tests) — apply_resource_limits
            # falls back to its safe built-in defaults.
            logger.debug("resource_limit_preexec: config unavailable, using defaults")
        # POSIX: apply_resource_limits returns a callable (a no-op only when
        # every limit is disabled). Cache it; passing a no-op preexec_fn is fine.
        _RESOURCE_PREEXEC = apply_resource_limits(cfg)
    return _RESOURCE_PREEXEC  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Session host preexec — the inverse of resource_limit_preexec.
# ---------------------------------------------------------------------------

_SESSION_HOST_PREEXEC: object = _UNSET


def session_host_preexec() -> "Callable[[], None] | None":
    """Return a ``preexec_fn`` that *raises* NOFILE for a session host process.

    Session hosts (kiro-cli-chat / claude-agent-acp) are **trusted** internal
    processes — they manage a tree of MCP server subprocesses, each consuming
    pipe fd pairs for stdin/stdout communication.  A single session host may
    hold 100-200 fds under normal operation (10+ MCP servers × pipe pairs +
    sockets + log files).

    The default ``resource_limit_preexec()`` caps NOFILE at 1024 to defend
    against compromised *tool* processes, but applying the same cap to the
    trusted session host causes "Too many open files" crashes when subagent
    concurrency or MCP server count is high.

    This preexec raises NOFILE soft+hard to the *gateway's* inherited hard
    limit (typically 10240 from the systemd unit, or 524288 kernel max) so
    the session host has headroom proportional to the gateway itself.  Other
    resource limits (NPROC, CPU, AS) are left at their sandbox values — a
    session host has no legitimate reason to fork-bomb or allocate unbounded
    memory.

    Returns ``None`` on non-POSIX platforms (preexec_fn must be None there).
    """
    global _SESSION_HOST_PREEXEC
    if _SESSION_HOST_PREEXEC is _UNSET:
        if os.name != "posix" or _resource_mod is None:
            _SESSION_HOST_PREEXEC = None
            return None

        res = _resource_mod

        def _raise_nofile() -> None:
            """Raise NOFILE to the hard limit in the child process."""
            try:
                _soft, hard = res.getrlimit(res.RLIMIT_NOFILE)
                if hard == res.RLIM_INFINITY:
                    # Kernel allows unlimited — cap at a sane maximum but never
                    # reduce below the inherited soft limit.
                    target = max(_soft, 65536)
                else:
                    target = hard
                res.setrlimit(res.RLIMIT_NOFILE, (target, hard))
            except (ValueError, OSError):
                pass  # Leave inherited — better than failing the spawn.

        _SESSION_HOST_PREEXEC = _raise_nofile
    return _SESSION_HOST_PREEXEC  # type: ignore[return-value]


# Build workloads (vite/npm/pip) legitimately hold thousands of descriptors —
# the default 1024 NOFILE ceiling EMFILEs them while still being the right cap
# for one-shot tools. Same policy, higher finite descriptor ceiling; every
# other limit still comes from the operator config. Cached like the default.
_BUILD_NOFILE_CEILING = 65536
_BUILD_RESOURCE_PREEXEC: object = _UNSET


def build_resource_limit_preexec() -> "Callable[[], None] | None":
    """``resource_limit_preexec`` variant for build-class children.

    Identical policy except ``max_open_files`` is raised to a still-finite
    65536 (matching the gateway service's own ``LimitNOFILE``); an operator
    ``resource_limits.max_open_files`` override higher than the default wins.
    """
    global _BUILD_RESOURCE_PREEXEC
    if _BUILD_RESOURCE_PREEXEC is _UNSET:
        if os.name != "posix":
            _BUILD_RESOURCE_PREEXEC = None
            return None
        from kiro_crew.security import apply_resource_limits

        cfg: dict | None = None
        try:
            from kiro_crew.config.loader import _raw_config

            cfg = dict(_raw_config() or {})
        except Exception:
            cfg = {}
        raw_limits = (cfg or {}).get("resource_limits")
        limits = dict(raw_limits) if isinstance(raw_limits, dict) else {}
        # Malformed operator values must not break the spawn — resource-limit
        # handling elsewhere ignores bad values, so mirror that here and fall
        # back to the ceiling (bools are ints in Python; exclude them).
        raw = limits.get("max_open_files")
        configured = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        limits["max_open_files"] = max(configured, _BUILD_NOFILE_CEILING)
        _BUILD_RESOURCE_PREEXEC = apply_resource_limits({**(cfg or {}), "resource_limits": limits})
    return _BUILD_RESOURCE_PREEXEC  # type: ignore[return-value]
