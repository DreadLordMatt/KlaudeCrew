"""Linux namespace launcher: agent-executable resolution, the embedded namespace
launcher template, and ``namespace_argv``.

The ``_build_launcher_script`` template (a ``{}``-formatted, stdlib-only Python
program that performs the fork / unshare / bind-mount / seccomp work in the
sandboxed child) is moved here VERBATIM from the original ``sandbox.py``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.platform import current_context

from .backends import (
    _AGENT_DENIED_ENV_KEYS,
    _CC_EXPOSE_FILES,
    _CC_FILES,
    _PYTHON_ENV_PREFIXES,
    _SENSITIVE_ENV_PREFIXES,
    _STANDARD_DIRS,
    _sandbox_policy,
)

logger = logging.getLogger("kiro_crew.sandbox")


# ── Backend: Linux namespace sandbox ──


def _resolve_agent_executable(executable: str) -> str:
    """Resolve *executable* through the active edition before sandboxing.

    The public adapter is identity. An edition companion may replace a managed
    launcher with the direct executable it ultimately invokes so KiroCrew can
    apply exactly one OS-level sandbox. A transient adapter failure degrades to
    the original executable, which preserves the secure behavior: the outer
    sandbox still applies and a launcher that cannot run nested fails closed.
    Platform composition failures always propagate through ``safe_context_call``.
    """
    from kiro_crew.platform import safe_context_call

    return safe_context_call(
        lambda: current_context().agent_executable.resolve_executable(executable),
        fallback=executable,
        log_message="Agent executable resolver failed; using the original executable",
    )


@functools.lru_cache(maxsize=None)
def _ssh_supports_accept_new() -> bool:
    """Return True if the installed ssh supports StrictHostKeyChecking=accept-new (OpenSSH >= 7.6)."""
    try:
        r = subprocess.run(["ssh", "-V"], capture_output=True, timeout=5)
        m = re.search(r"OpenSSH_(\d+)\.(\d+)", r.stderr.decode())
        if m:
            return (int(m.group(1)), int(m.group(2))) >= (7, 6)
    except Exception:
        pass
    return False


def _build_launcher_script(
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
) -> str:
    """Build a Python launcher script for the Linux namespace sandbox.

    The launcher is executed as a subprocess.  It:

    1. Forks a child.
    2. Child calls ``unshare(CLONE_NEWUSER)`` and signals the parent.
    3. Parent writes identity UID/GID map (``uid uid 1``) to
       ``/proc/<child>/{setgroups,uid_map,gid_map}`` and signals back.
    4. Child calls ``unshare(CLONE_NEWNS)``, sets mount propagation private,
       bind-mounts empty dirs over credential paths, scrubs env vars,
       and ``exec``s the real command.

    The child retains the real UID/GID — no UID 0, no UID 65534.
    """
    home = str(Path.home())
    uid = os.getuid()
    gid = os.getgid()
    # Source the sensitive-dir lists from the active PlatformContext so the
    # Amazon companion can extend them (+ .midway/.ada).  The Default adapter
    # returns ``list(_STRICT_DIRS)`` / ``list(_CC_DIRS)``, so standalone is
    # unchanged.  ``_STANDARD_DIRS`` is not an extension point (no interface
    # method) and stays on the module global.
    if sandbox_level == "standard":
        dirs = _STANDARD_DIRS
    elif sandbox_level == "cc":
        dirs = _sandbox_policy().cc_dirs()
    else:
        dirs = _sandbox_policy().strict_dirs()
    files = _CC_FILES if sandbox_level in ("cc", "strict") else []
    expose_files = _CC_EXPOSE_FILES if sandbox_level == "cc" else []
    env_prefixes = list(_SENSITIVE_ENV_PREFIXES)
    if sandbox_level in ("cc", "strict"):
        # Block agent subprocesses from reading credentials via os.environ
        # (the file-level bind-mount of ~/.kirocrew/.env hides them on disk;
        # config/loader.py seeds them into os.environ for trusted children
        # only — sandboxed agents must not see them either way).
        env_prefixes = env_prefixes + list(_AGENT_DENIED_ENV_KEYS)
    if strip_python_env:
        # Foreign Python subprocess (kiro-cli's MCP servers) — do not let
        # KiroCrew's PYTHONPATH/PYTHONHOME leak in and shadow their own deps.
        env_prefixes = env_prefixes + list(_PYTHON_ENV_PREFIXES)
    hide_ssh = sandbox_level == "strict"
    dirs_json = json.dumps([os.path.join(home, d) for d in dirs])
    files_json = json.dumps([os.path.join(home, f) for f in files])
    expose_json = json.dumps([(os.path.join(home, f), f.split("/")[-1]) for f in expose_files])
    env_prefixes_json = json.dumps(env_prefixes)
    ssh_dir = json.dumps(os.path.join(home, ".ssh"))
    ssh_known_hosts = json.dumps(os.path.join(home, ".ssh", "known_hosts"))
    strict_host_key_opt = (
        " -o StrictHostKeyChecking=accept-new" if _ssh_supports_accept_new() else ""
    )

    return f'''#!/usr/bin/env python3
"""Namespace sandbox launcher — spawned by KiroCrew."""
import sys
# Harden against stdlib shadowing. This launcher runs as
# ``python ~/.kirocrew/run/kirocrew_sandbox_*.py``, so CPython prepends the
# script's own directory (sys.path[0], typically ~/.kirocrew/run/) to sys.path.
# A stray sibling module left in that directory by another process — e.g.
# struct.py, os.py — then shadows the real stdlib and crashes the imports below
# (seen in the wild: "ImportError: cannot import name 'calcsize' from
# '/tmp/struct.py'", which kills the agent subprocess on spawn). ``sys`` is a
# builtin and cannot be shadowed, so importing it first is safe; drop the
# launcher dir (and any cwd "" entry) before importing anything that resolves
# from the filesystem.
sys.path[:] = [p for p in sys.path if p not in ("", sys.path[0])]
import ctypes
import ctypes.util
import os
import tempfile

_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNS   = 0x00020000
_MS_BIND       = 4096
_MS_REC        = 16384
_MS_PRIVATE    = 1 << 18

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.mount.argtypes = [
    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
    ctypes.c_ulong, ctypes.c_void_p,
]
_libc.mount.restype = ctypes.c_int
_libc.unshare.argtypes = [ctypes.c_int]
_libc.unshare.restype = ctypes.c_int
_libc.prctl = _libc.prctl if hasattr(_libc, "prctl") else None
if _libc.prctl:
    _libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    _libc.prctl.restype = ctypes.c_int

REAL_UID = {uid}
REAL_GID = {gid}
SENSITIVE_DIRS = {dirs_json}
SENSITIVE_FILES = {files_json}
EXPOSE_FILES = {expose_json}
ENV_PREFIXES = {env_prefixes_json}
SSH_DIR = {ssh_dir}
SSH_KNOWN_HOSTS = {ssh_known_hosts}
HIDE_SSH = {hide_ssh}

def main():
    argv = sys.argv[1:]
    if not argv:
        sys.exit("sandbox_launcher: no command given")

    # Export this launcher's HOST pid before any fork/namespace work. The
    # gateway records exactly this pid (its direct Popen child) when it
    # writes ``session_pid_<pid>.txt`` on session claim, so in-sandbox
    # identity resolvers can look the file up directly via this env var
    # instead of walking /proc — which breaks whenever the subtree's view
    # of pids diverges from the host's (PID-namespace sandboxing).
    os.environ["KIROCREW_HOST_PID"] = str(os.getpid())

    # Two pipes for parent↔child synchronization
    c2p_r, c2p_w = os.pipe()  # child signals "unshare done"
    p2c_r, p2c_w = os.pipe()  # parent signals "maps written"

    pid = os.fork()

    if pid > 0:
        # ── Parent: write identity UID/GID map ──
        os.close(c2p_w)
        os.close(p2c_r)
        os.read(c2p_r, 1)  # wait for child to unshare(NEWUSER)
        os.close(c2p_r)
        with open(f"/proc/{{pid}}/setgroups", "w") as f:
            f.write("deny")
        with open(f"/proc/{{pid}}/uid_map", "w") as f:
            f.write(f"{{REAL_UID}} {{REAL_UID}} 1\\n")
        with open(f"/proc/{{pid}}/gid_map", "w") as f:
            f.write(f"{{REAL_GID}} {{REAL_GID}} 1\\n")
        os.write(p2c_w, b"x")  # signal child to proceed
        os.close(p2c_w)
        _, status = os.waitpid(pid, 0)
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        sys.exit(code)
    else:
        # ── Child: unshare, wait for maps, mount, exec ──
        os.close(c2p_r)
        os.close(p2c_w)

        # Step 1: enter user namespace
        if _libc.unshare(_CLONE_NEWUSER) != 0:
            sys.exit(f"sandbox: unshare(NEWUSER) failed: errno {{ctypes.get_errno()}}")
        os.write(c2p_w, b"x")  # tell parent
        os.close(c2p_w)
        os.read(p2c_r, 1)  # wait for maps
        os.close(p2c_r)

        # Step 2: enter mount namespace (now we have a mapped UID)
        if _libc.unshare(_CLONE_NEWNS) != 0:
            sys.exit(f"sandbox: unshare(NEWNS) failed: errno {{ctypes.get_errno()}}")

        # Private mount propagation
        _libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None)

        # Pick a tmpfs-backed source dir for bind-mount empty files/dirs. Same-fs
        # binds (e.g. /tmp on ext4 over ~/.kirocrew/.env on ext4) can corrupt the
        # target's host directory entry via a kernel propagation race when the
        # private NS is torn down — leaving the host file pointing at the empty
        # source inode permanently. Cross-fs binds use distinct inode spaces and
        # cannot leak that way. Fallback chain: /run/user/$UID → /dev/shm.
        # Verify each candidate is on a different filesystem from HOME by
        # comparing st_dev — same-fs candidates provide no isolation benefit.
        _tmpfs_src = None
        try:
            _home_dev = os.stat(os.path.expanduser("~")).st_dev
        except OSError:
            _home_dev = None
        for _candidate in (f"/run/user/{{REAL_UID}}", "/dev/shm"):
            try:
                if _home_dev is not None and os.stat(_candidate).st_dev == _home_dev:
                    continue  # same fs as HOME — no isolation, race still possible
                _probe = tempfile.mkdtemp(dir=_candidate, prefix="kirocrew_sb_")
                os.rmdir(_probe)
                _tmpfs_src = _candidate
                break
            except (OSError, ValueError):
                continue
        # _tmpfs_src=None falls through to system default tempdir (typically /tmp).
        # In that case we accept the kernel-race risk because no tmpfs is
        # available — better to function (with the original regression risk)
        # than to refuse to start.

        # Pre-read files that must survive dir hiding
        expose_data = {{}}
        for src_path, filename in EXPOSE_FILES:
            if os.path.isfile(src_path):
                with open(src_path, "rb") as fh:
                    expose_data[src_path] = fh.read()

        # Bind-mount empty dirs over credential paths (per-dir tmpdir to
        # prevent content leaking across mounts via shared backing dir).
        for d in SENSITIVE_DIRS:
            target = d.encode()
            if os.path.isdir(target):
                per_dir_empty = tempfile.mkdtemp(dir=_tmpfs_src).encode()
                _libc.mount(per_dir_empty, target, None, _MS_BIND, None)

        # Restore selectively exposed files into the now-empty mounts
        for src_path, filename in EXPOSE_FILES:
            if src_path in expose_data:
                parent = os.path.dirname(src_path)
                dest = os.path.join(parent, filename)
                with open(dest, "wb") as fh:
                    fh.write(expose_data[src_path])
                # NOTE: this runs inside the embedded Linux-only namespace
                # launcher script (a standalone /tmp file that imports only
                # stdlib — sys/ctypes/os/tempfile — and never kiro_crew), so it
                # must stay a raw os.chmod, NOT platform_compat.chmod_safe
                # (which is undefined in that process). The launcher never runs
                # on Windows, so there is no portability loss.
                os.chmod(dest, 0o444)

        # Bind-mount empty files over individual sensitive files. Source the
        # empty tempfile from a tmpfs (cross-fs) when available so the bind
        # cannot corrupt the target's host directory entry on namespace exit.
        for f in SENSITIVE_FILES:
            target = f.encode()
            if os.path.isfile(target):
                fd, empty_path = tempfile.mkstemp(dir=_tmpfs_src)
                os.close(fd)
                _libc.mount(empty_path.encode(), target, None, _MS_BIND, None)

        # .ssh: hide keys but expose known_hosts content (strict only)
        if HIDE_SSH and os.path.isdir(SSH_DIR):
            kh_data = b""
            if os.path.isfile(SSH_KNOWN_HOSTS):
                with open(SSH_KNOWN_HOSTS, "rb") as fh:
                    kh_data = fh.read()
            # Cross-fs source for the same kernel-race reason as SENSITIVE_DIRS
            # (line 371) and SENSITIVE_FILES (line 389).
            ssh_tmp = tempfile.mkdtemp(dir=_tmpfs_src).encode()
            _libc.mount(ssh_tmp, SSH_DIR.encode(), None, _MS_BIND, None)
            if kh_data:
                with open(os.path.join(SSH_DIR, "known_hosts"), "wb") as fh:
                    fh.write(kh_data)

        # Scrub sensitive env vars
        for key in list(os.environ):
            for prefix in ENV_PREFIXES:
                if key.startswith(prefix):
                    del os.environ[key]
                    break

        # Fix /etc/ssh/ssh_config.d/ ownership issue: root-owned files
        # appear as nobody:nobody inside the user namespace because UID 0
        # is unmapped. SSH refuses to load them. Bypass with -F /dev/null.
        if not os.environ.get("GIT_SSH_COMMAND"):
            os.environ["GIT_SSH_COMMAND"] = (
                "ssh -F /dev/null -o IdentityFile=~/.ssh/id_rsa"
                " -o IdentityFile=~/.ssh/id_ecdsa"
                " -o IdentityFile=~/.ssh/id_ed25519"
                " -o UserKnownHostsFile=~/.ssh/known_hosts"
                "{strict_host_key_opt}"
            )

        # ── Step 5: Drop capabilities + set NO_NEW_PRIVS (P472042955) ──
        # Inside the user namespace, the child has CAP_SYS_ADMIN (owner of the
        # NS) which lets it umount the credential bind-mounts. Drop ALL
        # capabilities from the bounding set and set NO_NEW_PRIVS before exec.
        import struct as _struct

        _PR_SET_NO_NEW_PRIVS = 38
        _PR_CAPBSET_DROP = 24
        if _libc.prctl:
            # Linux CAP_LAST_CAP is currently 41 (kernel 6.x); iterate 0..63 for
            # forward-compatibility — dropping a non-existent cap just returns -1.
            for _cap in range(64):
                _libc.prctl(_PR_CAPBSET_DROP, _cap, 0, 0, 0)
            # NO_NEW_PRIVS: prevents regaining caps via exec of setuid/setcap bins
            _ret = _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
            if _ret != 0:
                sys.exit("sandbox: BLOCKED — failed to set NO_NEW_PRIVS (prctl returned %d)" % _ret)

        # ── Step 6: Install seccomp-BPF filter (P472042955) ──
        # Deny mount/umount2/unshare/setns/pivot_root/link/linkat to prevent
        # the sandboxed process from undoing bind-mounts or creating hardlinks
        # to protected credential inodes (P472042777).
        #
        # Additionally deny kill(-1, sig) — the signal BROADCAST that reaches
        # every same-uid process on the host (gateway, other sessions). This
        # is the accident-containment redo of the reverted PID-namespace
        # isolation (24c320f6): a static arg filter blocks the hand-slip /
        # runaway-script broadcast without changing the subtree's view of
        # pids, so session identity, claim-push, and systemd stay intact.
        # Only ``kill`` needs arg inspection: tkill/tgkill/pidfd_send_signal
        # are inherently targeted (no broadcast semantics). pid==0 and
        # negative process-group targets stay ALLOWED on purpose — the spawn
        # already setsid()s, so every reachable process group is inside the
        # sandbox session, and denying killpg breaks legitimate tooling
        # (timeout(1), shell job control, cleanup traps).
        if _libc.prctl:
            _PR_SET_SECCOMP = 22
            _SECCOMP_MODE_FILTER = 2
            _SECCOMP_RET_ALLOW = 0x7FFF0000
            _SECCOMP_RET_ERRNO = 0x00050000
            _EPERM = 1
            _BPF_LD = 0x00
            _BPF_W = 0x00
            _BPF_ABS = 0x20
            _BPF_JMP = 0x05
            _BPF_JEQ = 0x10
            _BPF_K = 0x00
            _BPF_RET = 0x06
            # Syscall numbers (x86_64): mount=165, umount2=166, unshare=272,
            # setns=308, pivot_root=155, link=86, linkat=265, kill=62
            # aarch64: mount=40, umount2=39, unshare=97, setns=268,
            # pivot_root=41, link=N/A(use linkat=37), linkat=37, kill=129
            import platform as _plat
            _machine = _plat.machine()
            if _machine == "x86_64":
                _DENY_SYSCALLS = (165, 166, 272, 308, 155, 86, 265)
                _KILL_NR = 62
            elif _machine == "aarch64":
                _DENY_SYSCALLS = (40, 39, 97, 268, 41, 37)
                _KILL_NR = 129
            else:
                _DENY_SYSCALLS = ()  # unknown arch — skip seccomp
                _KILL_NR = None

            if _DENY_SYSCALLS:
                # Architecture constants for seccomp arch validation
                _AUDIT_ARCH_X86_64 = 0xC000003E
                _AUDIT_ARCH_AARCH64 = 0xC00000B7
                _SECCOMP_RET_KILL = 0x00000000
                _expected_arch = _AUDIT_ARCH_X86_64 if _machine == "x86_64" else _AUDIT_ARCH_AARCH64

                # BPF program layout (indices relative to start):
                #   0: LD arch
                #   1: JEQ expected_arch ? skip 1 : fall through
                #   2: RET KILL                (unexpected arch)
                #   3: LD syscall nr
                #   4..4+n-1: JEQ deny_i -> DENY
                #   k   = 4+n: JEQ kill_nr ? fall into arg check : jump ALLOW
                #   k+1: LD args[0] low 32 bits    (seccomp_data offset 16)
                #   k+2: JEQ 0xFFFFFFFF ? jump DENY : fall through
                #   ALLOW = k+3: RET ALLOW
                #   DENY  = k+4: RET ERRNO|EPERM
                #
                # Only the LOW 32 bits of args[0] are inspected. pid_t is a
                # 32-bit int: the kernel truncates the register to 32 bits, so
                # low==0xFFFFFFFF is exactly "pid == -1" regardless of what the
                # upper half holds. The upper half MUST NOT be matched — the
                # x86-64 ABI leaves it undefined for int arguments, and glibc's
                # ``movl`` zero-extends, so kill(-1) typically arrives as
                # 0x00000000_FFFFFFFF (a high==0xFFFFFFFF check silently never
                # fires, which is a filter bypass, not a compat issue).
                _insns = []
                # Load arch: BPF_LD | BPF_W | BPF_ABS, offset=4 (seccomp_data.arch)
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 4))
                # If arch == expected, skip next insn (jt=1); else fall through to kill
                _insns.append(_struct.pack("<HBBI", _BPF_JMP | _BPF_JEQ | _BPF_K, 1, 0, _expected_arch))
                # Kill on unexpected arch (blocks i386 int 0x80 bypass)
                _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_KILL))
                # Load syscall number: BPF_LD | BPF_W | BPF_ABS, offset=0
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 0))
                # For each denied syscall: JEQ -> DENY (at index k+4)
                _n_deny = len(_DENY_SYSCALLS)
                for _i, _nr in enumerate(_DENY_SYSCALLS):
                    _jt = (_n_deny - _i - 1) + 4  # jumps to the DENY RET at k+4
                    _insns.append(_struct.pack("<HBBI",
                        _BPF_JMP | _BPF_JEQ | _BPF_K, _jt, 0, _nr))
                # k: nr == kill ? fall into arg check : jump to ALLOW (k+3)
                _insns.append(_struct.pack("<HBBI",
                    _BPF_JMP | _BPF_JEQ | _BPF_K, 0, 2, _KILL_NR))
                # k+1: load args[0] low word (offset 16, little-endian layout)
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 16))
                # k+2: low == 0xFFFFFFFF (pid -1) ? DENY (skip 1) : fall to ALLOW
                _insns.append(_struct.pack("<HBBI",
                    _BPF_JMP | _BPF_JEQ | _BPF_K, 1, 0, 0xFFFFFFFF))
                # ALLOW: return SECCOMP_RET_ALLOW
                _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_ALLOW))
                # DENY: return SECCOMP_RET_ERRNO | EPERM
                _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_ERRNO | _EPERM))

                _prog_bytes = b"".join(_insns)
                _n_insns = len(_insns)

                # struct sock_fprog {{ unsigned short len; struct sock_filter *filter; }}
                class _SockFprog(ctypes.Structure):
                    _fields_ = [("len", ctypes.c_ushort),
                                ("filter", ctypes.c_char_p)]

                _fprog = _SockFprog()
                _fprog.len = _n_insns
                _fprog.filter = _prog_bytes
                _ret = _libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER,
                                   ctypes.addressof(_fprog), 0, 0)
                if _ret != 0:
                    sys.exit("sandbox: BLOCKED — failed to install seccomp-BPF filter (prctl returned %d)" % _ret)

        # ── Step 7: Pre-exec hardlink scan (P472042777) ──
        # Scan the agent workspace + /tmp for hardlinks (nlink > 1) whose
        # inode matches a protected credential file. If found, refuse to exec.
        _protected_inodes = set()
        for _pd in SENSITIVE_DIRS:
            if os.path.isdir(_pd):
                for _root, _dirs_scan, _files_scan in os.walk(_pd):
                    for _fname in _files_scan:
                        try:
                            _st = os.stat(os.path.join(_root, _fname))
                            _protected_inodes.add((_st.st_dev, _st.st_ino))
                        except OSError:
                            pass
                    break  # depth=1 for credential dirs
        for _pf in SENSITIVE_FILES:
            try:
                _st = os.stat(_pf)
                _protected_inodes.add((_st.st_dev, _st.st_ino))
            except OSError:
                pass

        if _protected_inodes:
            _scan_count = 0
            _MAX_SCAN = 10000
            _dangerous_links = []
            _cwd = os.getcwd()
            for _scan_root in (_cwd, "/tmp"):
                if not os.path.isdir(_scan_root):
                    continue
                for _root2, _dirs2, _files2 in os.walk(_scan_root):
                    # Depth limit: max 5 levels
                    _depth = _root2[len(_scan_root):].count(os.sep)
                    if _depth > 5:
                        _dirs2.clear()
                        continue
                    for _fn2 in _files2:
                        _scan_count += 1
                        if _scan_count > _MAX_SCAN:
                            break
                        _fp2 = os.path.join(_root2, _fn2)
                        try:
                            _st2 = os.lstat(_fp2)
                            if _st2.st_nlink > 1:
                                if (_st2.st_dev, _st2.st_ino) in _protected_inodes:
                                    _dangerous_links.append(_fp2)
                        except OSError:
                            pass
                    if _scan_count > _MAX_SCAN:
                        break
            if _dangerous_links:
                sys.exit(
                    f"sandbox: BLOCKED — found hardlink(s) to protected credential "
                    f"inodes: {{_dangerous_links[:5]}}. Remove them before running."
                )

        os.execvp(argv[0], argv)

if __name__ == "__main__":
    main()
'''


def _ensure_run_dir() -> str:
    """Create ~/.kirocrew/run/ with mode 0o700, falling back to system tmpdir on failure."""
    run_dir = os.path.join(os.path.expanduser("~"), ".kirocrew", "run")
    try:
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        # exist_ok does not re-apply mode on existing dirs — enforce explicitly.
        # 0o700 (owner-only rwx) is deliberately restrictive: this dir holds
        # per-session sandbox launcher scripts and sockets that must NOT be
        # world-readable. Semgrep's 0o644 suggestion is wrong for a directory
        # (needs the execute/traverse bit) and would loosen, not tighten, access.
        os.chmod(run_dir, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    except OSError:
        logger.warning("Cannot create %s; falling back to system tmpdir", run_dir)
        run_dir = tempfile.gettempdir()
    return run_dir


def namespace_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
) -> list[str]:
    """Wrap *argv* via the Python namespace launcher.

    The launcher forks, the parent writes identity UID/GID maps, and the
    child bind-mounts empty dirs over credential paths before exec.
    The child retains the real UID/GID.
    """
    resolved_argv = list(argv)
    if resolved_argv:
        resolved_argv[0] = _resolve_agent_executable(resolved_argv[0])

    script = _build_launcher_script(sandbox_level, strip_python_env=strip_python_env)
    run_dir = _ensure_run_dir()
    fd, path = tempfile.mkstemp(suffix=".py", prefix=f"kirocrew_sandbox_{os.getpid()}_", dir=run_dir)
    os.write(fd, script.encode())
    os.close(fd)
    platform_compat.chmod_safe(path, 0o700)

    return [sys.executable, path, *resolved_argv]
