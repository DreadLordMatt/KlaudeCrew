"""Tests for the Windows venv-launcher fix (issue #1575).

The gateway manager must spawn the *real* interpreter, not the Windows venv
launcher stub. ``platform_compat.daemon_executable()`` resolves straight to
``sys._base_executable``, which on Windows skips the launcher shim and on
POSIX equals ``sys.executable`` (no behavioral change).

These tests verify:
1. ``daemon_executable()`` returns the base interpreter on every platform.
2. ``GatewayManager._spawn_once`` passes the resolved interpreter into the
   subprocess argv, not raw ``sys.executable``.
3. On Linux, the spawned process IS the real interpreter (no launcher parent)
   and ``terminate`` reaps the daemon directly.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import manager as mgr

# ─── daemon_executable() unit tests ──────────────────────────────────────────


class TestDaemonExecutable:
    """Verify daemon_executable() resolves the real interpreter."""

    def test_returns_base_executable(self) -> None:
        """Must match sys._base_executable (the real interpreter)."""
        expected = getattr(sys, "_base_executable", sys.executable)
        assert platform_compat.daemon_executable() == expected

    def test_is_a_real_file(self) -> None:
        """The resolved path must be an existing file."""
        exe = platform_compat.daemon_executable()
        assert os.path.isfile(exe), f"daemon_executable() -> {exe!r} is not a file"

    def test_fallback_when_base_executable_absent(self) -> None:
        """If _base_executable is somehow missing, falls back to sys.executable."""
        with patch.object(sys, "_base_executable", new=None, create=False):
            # Simulate absence by deleting the attribute
            saved = sys._base_executable
            try:
                del sys._base_executable  # type: ignore[attr-defined]
                result = platform_compat.daemon_executable()
                assert result == sys.executable
            finally:
                sys._base_executable = saved  # type: ignore[attr-defined]

    def test_on_posix_equals_or_resolves_same_binary(self) -> None:
        """On Linux/macOS the base executable resolves the same interpreter."""
        if platform_compat.IS_WINDOWS:
            pytest.skip("POSIX-only assertion")
        exe = platform_compat.daemon_executable()
        # On POSIX, _base_executable and executable point at the same
        # interpreter (possibly through a symlink chain).
        assert os.path.realpath(exe) == os.path.realpath(sys.executable)


# ─── Manager spawn integration ───────────────────────────────────────────────


class TestManagerUsesRealInterpreter:
    """Verify _spawn_once passes daemon_executable(), not sys.executable."""

    @pytest.mark.asyncio
    async def test_spawn_argv_uses_daemon_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first element of the spawn argv must be daemon_executable()."""
        captured_argv: list[str] = []

        async def fake_create_subprocess_exec(
            *args: Any, **kwargs: Any
        ) -> MagicMock:
            captured_argv.extend(args)
            proc = MagicMock()
            proc.pid = 12345
            proc.returncode = None
            proc.wait = AsyncMock(return_value=0)
            return proc

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", fake_create_subprocess_exec
        )
        # Stub restrict_to_owner so it doesn't touch the filesystem
        monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: None)

        spec = MagicMock()
        spec.socket_path = tmp_path / "gateway.sock"
        spec.idle_timeout_secs = 60
        spec.max_backends = 4
        spec.prewarm_count = 0
        spec.mcp_target_env = {}
        manager = mgr.GatewayManager(spec)
        # Stub credential watch paths
        monkeypatch.setattr(manager, "_credential_watch_paths", lambda: [])

        await manager._spawn_once()

        expected_exe = platform_compat.daemon_executable()
        assert captured_argv[0] == expected_exe
        # Must NOT be the raw sys.executable if they differ (Windows venv case)
        assert captured_argv[0] == getattr(sys, "_base_executable", sys.executable)


# ─── Live process test: spawned daemon IS the real interpreter ────────────────


class TestSpawnedProcessIsRealInterpreter:
    """On Linux, verify a process spawned with daemon_executable() has no
    launcher parent and can be terminated directly."""

    @pytest.mark.asyncio
    async def test_spawned_process_no_launcher_parent(self) -> None:
        """Spawn a trivial script with daemon_executable(). The process
        should be directly reachable — its parent is us, not a launcher."""
        if platform_compat.IS_WINDOWS:
            pytest.skip("Windows has no reliable ppid check from Python")

        exe = platform_compat.daemon_executable()
        # Spawn a process that just sleeps briefly
        proc = await asyncio.create_subprocess_exec(
            exe, "-c", "import time; time.sleep(30)",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # The process should be alive and its parent should be us
            assert proc.pid is not None
            assert proc.returncode is None

            # Verify our pid is the parent (no intermediate launcher)
            ppid_path = Path(f"/proc/{proc.pid}/stat")
            if ppid_path.exists():
                stat_fields = ppid_path.read_text().split()
                # Field index 3 is ppid in /proc/[pid]/stat
                ppid = int(stat_fields[3])
                assert ppid == os.getpid(), (
                    f"Spawned process ppid={ppid} != our pid={os.getpid()}; "
                    f"a launcher shim is sitting in between"
                )

            # Verify terminate() actually reaches the process
            proc.terminate()
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                pytest.fail("terminate() did not reach the daemon process")
            # SIGTERM -> negative return code on POSIX
            assert rc == -signal.SIGTERM
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
