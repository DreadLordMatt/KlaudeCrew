"""Unix-socket lifecycle helpers for the MCP gateway daemon.

Split out of :mod:`kiro_crew.mcp_gateway.gatewayd` (LOC refactor). Owns socket
directory preparation, the race-free singleton advisory lock, stale-socket
removal, and the blocking liveness probe. Leaf module — imports nothing from
the rest of the ``mcp_gateway`` split.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket as _socket
import stat
from pathlib import Path
from typing import Optional

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)


def _prepare_socket_dir(socket_path: Path) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode is masked by umask and is NOT applied to a pre-existing
    # directory; re-chmod so the documented owner-only (0700) containing-dir
    # guarantee holds even when $KIROCREW_HOME/mcp-gateway already existed
    # with looser permissions (matches how the socket is chmod'd to 0600).
    try:
        socket_path.parent.chmod(0o700)
    except OSError:
        pass


_SINGLETON_LOCK_SUFFIX = ".lock"


def _acquire_singleton_lock(socket_path: Path) -> Optional[int]:
    """Acquire an exclusive, non-blocking advisory lock guarding ``socket_path``.

    Returns the held lock fd on success, or ``None`` if another live gatewayd
    already holds it. The fd must stay open for the daemon's lifetime; the
    kernel releases the flock automatically when the holder dies, so there is
    no stale-lock failure mode and the guard is race-free even when multiple
    daemons start in the same instant (only one wins ``LOCK_EX``).

    ``O_CLOEXEC`` keeps the lock fd from leaking into the MCP backend
    subprocesses gatewayd spawns — otherwise a backend would hold the lock
    open past the daemon's own exit and block the next daemon from starting.
    """
    lock_path = socket_path.parent / (socket_path.name + _SINGLETON_LOCK_SUFFIX)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    if not platform_compat.try_acquire_lock(fd, exclusive=True):
        os.close(fd)
        return None
    return fd


async def _remove_stale_socket(socket_path: Path) -> None:
    """Remove a socket left behind by a prior crash.

    Distinguishes a *real* stale socket (file that is not a socket, or a
    socket with no listener) from a live peer (another daemon currently
    bound). Refuses to unlink anything that looks like a live socket —
    ``asyncio.start_unix_server`` will fail later with ``EADDRINUSE``,
    which is the correct user-visible error.

    The blocking ``socket.connect()`` probe is offloaded to a thread via
    :func:`asyncio.to_thread` so the event loop is never blocked on a
    potentially slow or hanging unix-socket connect.
    """
    try:
        st = os.stat(socket_path)
    except FileNotFoundError:
        return
    # S_IFSOCK == 0o140000. For non-socket files this is operator error;
    # removing them is not our call.
    if not stat.S_ISSOCK(st.st_mode):
        logger.warning(
            "path %s exists and is not a socket (mode=%o); leaving in place",
            socket_path,
            st.st_mode,
        )
        return
    # Probe whether the socket is live before unlinking. If connect
    # succeeds, another daemon is actively listening — don't unlink;
    # let asyncio.start_unix_server fail with EADDRINUSE instead.
    # The blocking connect is offloaded to a thread so the event loop
    # is never stalled.
    is_live = await asyncio.to_thread(_probe_socket_live, socket_path)
    if is_live:
        logger.warning(
            "socket %s is live (connect succeeded); refusing to unlink — "
            "another gatewayd instance may be running",
            socket_path,
        )
        return
    try:
        socket_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("could not remove stale socket %s: %s", socket_path, exc)


def _probe_socket_live(socket_path: Path) -> bool:
    """Blocking probe: return True if a listener is bound to ``socket_path``.

    Designed to run inside :func:`asyncio.to_thread` so the event loop is
    never blocked by the connect syscall.
    """
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.settimeout(1.0)
        s.connect(str(socket_path))
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        s.close()
