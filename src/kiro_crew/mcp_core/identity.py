"""Caller identity + local-token helpers for the kirocrew-core MCP server.

Leaf module (bottom of the mcp_core split DAG): the local IPC secret, the
user-scoped token exchange + its process-wide cache, the cross-platform
parent-PID walk, and the workspace-bucket resolvers. ``_local_user_token``
needs ``transport._API`` but imports it lazily inside the function so this
module has no module-level sibling imports (transport imports us)."""

from __future__ import annotations

import json
import platform
import subprocess
import time
import urllib.request
from pathlib import Path

from kiro_crew.config.loader import config_dir


def _internal_secret() -> str:
    """Read the per-session secret for IPC authentication."""
    try:
        return (config_dir() / ".local_secret").read_text().strip()
    except Exception:
        return ""


# Cached user-scoped token for routes that reject ``X-Internal-Secret`` and
# require a real session token (e.g. ``/api/autonudge*``). Bootstrapped on
# demand from ``/api/token/local`` using the same local secret we already hold,
# and refreshed once it nears expiry. ``(token, expires_at_monotonic)``.
_USER_TOKEN_CACHE: tuple[str, float] = ("", 0.0)


def _local_user_token() -> str:
    """Exchange the local secret for a short-lived user-scoped token.

    A few routes (notably ``/api/autonudge*``) deliberately reject the
    machine-to-machine ``X-Internal-Secret`` handshake and require a
    user-scoped token instead. ``GET /api/token/local`` mints one for any
    loopback caller that presents the local secret via the ``X-Local-Secret``
    header. We cache the token in-process and refresh it shortly before
    expiry so a self-halting loop doesn't pay the round-trip every call.

    Returns ``""`` if the exchange fails; callers surface that as the usual
    ``{"error": ...}`` path rather than crashing.
    """
    global _USER_TOKEN_CACHE
    from kiro_crew.mcp_core.transport import _API  # lazy: avoid import cycle
    cached, expires_at = _USER_TOKEN_CACHE
    # 30s safety margin so a token doesn't expire mid-request.
    if cached and time.monotonic() < expires_at - 30:
        return cached
    secret = _internal_secret()
    if not secret:
        return ""
    req = urllib.request.Request(
        f"{_API}/api/token/local?ttl=15m",
        headers={"X-Local-Secret": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return ""
    token = str(data.get("token", ""))
    if not token:
        return ""
    ttl = float(data.get("expires_in", 900) or 900)
    _USER_TOKEN_CACHE = (token, time.monotonic() + ttl)
    return token


def _ppid_via_libproc(pid: int) -> int:
    """macOS parent-PID lookup via libproc's ``proc_pidinfo`` (stdlib ctypes).

    macOS has no ``/proc``, and the app sandbox denies spawning ``ps``
    (``Operation not permitted``). ``proc_pidinfo`` is an information syscall
    (no ``exec``), so the sandbox allows it — the same primitive psutil uses,
    but with zero third-party dependency. Returns 0 on any failure so the caller
    can fall back.
    """
    import ctypes
    import struct

    proc_pidtbsdinfo = 3
    # sizeof(struct proc_bsdinfo) is 232 on 64-bit Darwin; over-allocate.
    buf_size = 256
    try:
        libproc = ctypes.CDLL("libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.restype = ctypes.c_int
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        buf = ctypes.create_string_buffer(buf_size)
        n = libproc.proc_pidinfo(pid, proc_pidtbsdinfo, 0, buf, buf_size)
        # pbi_ppid is the 5th uint32 (offset 16); need at least that many bytes.
        if n <= 16:
            return 0
        # struct proc_bsdinfo starts: pbi_flags, pbi_status, pbi_xstatus,
        # pbi_pid, pbi_ppid (5 x uint32) — pbi_ppid is index 4.
        return int(struct.unpack_from("<5I", buf.raw, 0)[4])
    except Exception:
        return 0


def _get_ppid(pid: int) -> int:
    """Get parent PID cross-platform. Returns 0 on failure.

    Standard-library only — deliberately NO third-party dependency (e.g.
    psutil), so the shipped app needs nothing extra bundled or code-signed and
    works across OS versions out of the box.

    - Linux: read ``/proc/<pid>/status`` (plain file read).
    - macOS: ``proc_pidinfo`` via libproc (see ``_ppid_via_libproc``). The old
      code shelled out to ``ps`` here, which the macOS app sandbox denies
      (``Operation not permitted``) — that broke the ancestor PID-walk in
      ``_resolve_session_key``, leaving spawned sub-agents unable to resolve
      their parent session key (empty ``KIROCREW_SESSION_KEY``) and surfacing
      spurious tool-approval cards on trusted sessions. libproc needs no
      ``exec``, so it works under the sandbox.
    - Other/unknown platforms: fall back to ``ps`` (may be blocked, then 0).
    """
    system = platform.system()
    try:
        if system == "Linux":
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("PPid:"):
                    return int(line.split()[1])
        elif system == "Darwin":
            ppid = _ppid_via_libproc(pid)
            if ppid:
                return ppid
        # Last-resort fallback (unknown platform, or a libproc/proc miss): ``ps``.
        # May be sandbox-blocked, in which case this raises and we return 0.
        out = subprocess.check_output(["ps", "-o", "ppid=", "-p", str(pid)], text=True, timeout=2)
        return int(out.strip())
    except Exception:
        pass
    return 0


def _ws_bucket(meta_ws: object) -> str:
    """Normalize a session's workspace value to a comparable bucket.

    ``update_metadata`` accepts arbitrary JSON for ``workspace``; a non-string
    (or empty) value must bucket to "default" rather than compare unequal to a
    real workspace name and silently hide the session from its owner.
    """
    return meta_ws if isinstance(meta_ws, str) and meta_ws else "default"


def _caller_workspace(cl: "object", session_key: str) -> str:
    """Resolve the calling session's workspace bucket for scope filtering.

    Read from the caller's own session metadata (normalized via _ws_bucket).
    Known limitation: on a brand-new session whose metadata file has not been
    written yet, this returns "default". A multi-workspace caller in that narrow
    window is scoped to the default bucket (fail-CLOSED — they see fewer results,
    never another workspace's). Fully fixing it needs the gateway to carry the
    workspace in CallerContext (the register payload does not today), so it is
    tracked as a separate gateway change rather than papered over here.
    """
    if not session_key:
        return "default"
    return _ws_bucket(cl.get_metadata(session_key).get("workspace"))  # type: ignore[attr-defined]
