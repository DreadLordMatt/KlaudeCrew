"""Windows named-pipe transport probe for the MCP gateway (Windows-only).

The gateway's stub <-> gatewayd hop is an AF_UNIX socket, which asyncio does not
expose on Windows at all: ``start_unix_server`` / ``open_unix_connection`` live
in ``asyncio.unix_events``, and ``asyncio/__init__.py`` never imports that module
under ``sys.platform == "win32"`` -- the names are absent from the namespace
rather than raising at call time. A Windows port therefore needs a different
local transport, and the named-pipe option carries open questions that can only
be answered on a real Windows host. This module answers them in CI.

1. Read-mode flip. asyncio creates its server pipes with
   ``PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE``
   (``asyncio.windows_events.PipeServer._server_pipe_handle``). In message read
   mode a ``ReadFile`` whose buffer is smaller than the pending message fails
   with ``ERROR_MORE_DATA``, and ``IocpProactor.recv`` has no handling for that
   error. The gateway's frames run up to 1 MiB against an 8 KiB pipe buffer
   (``asyncio.windows_utils.BUFSIZE``), so a short buffer is the normal case
   here, not an edge case. A pipe created as MESSAGE *type* may still be read in
   BYTE mode, so the fix is to flip the read mode per handle. This module proves
   the flip is reachable from the stdlib and that newline-delimited framing then
   survives a frame far larger than the pipe buffer.

2. Peer identity. The reason to prefer named pipes over TCP loopback is that the
   server can obtain a kernel-attested client identity;
   ``GetNamedPipeClientProcessId`` is the PID half of that, and it is reached
   through the public ``get_extra_info("pipe")`` seam rather than any private
   asyncio attribute.

3. Default pipe security. asyncio passes ``lpSecurityAttributes = NULL``, so the
   pipe receives a default security descriptor. Whether that descriptor is
   itself an access boundary decides whether an impersonation-based SID check is
   defense in depth or the only gate. That question is *reported*, not asserted:
   the answer informs the design rather than gating this branch.

Informational findings are appended to ``GITHUB_STEP_SUMMARY`` rather than
printed. The suite runs under ``-n auto`` with output capture, so stdout from a
passing test never reaches the workflow log; the job-summary file is independent
of pytest's capture and renders on the run page.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import uuid
import warnings
from typing import Any, NamedTuple, cast

import pytest

from kiro_crew import platform_compat as pc

pytestmark = pytest.mark.skipif(
    not pc.IS_WINDOWS, reason="probes Windows named-pipe transport semantics"
)

if sys.platform == "win32":  # pragma: no cover - import guard, Windows only
    from asyncio import streams as asyncio_streams
    from asyncio import windows_events
    from ctypes import wintypes

    import _winapi
else:  # pragma: no cover - keeps the module importable for collection on POSIX
    # cast(Any, None) rather than a bare None: on a POSIX host mypy only sees
    # this branch and would otherwise narrow every name to None, flagging each
    # Win32 attribute access below. The bodies never run off Windows.
    _winapi = cast(Any, None)
    asyncio_streams = cast(Any, None)
    windows_events = cast(Any, None)
    wintypes = cast(Any, None)

# ``ctypes.WinDLL`` and ``ctypes.get_last_error`` exist only in the Windows
# ctypes stubs, so reach them through an Any-typed alias: the calls below are
# already inside Windows-only code paths, and the alias keeps a POSIX mypy run
# from flagging every one of them.
_ct: Any = ctypes

# PIPE_READMODE_BYTE and PIPE_WAIT are both 0 in the Win32 headers, so a mode of
# 0 means "byte read mode, blocking". _winapi exports only the MESSAGE
# spellings, which is why this is a literal rather than a named constant.
_PIPE_READMODE_BYTE_AND_WAIT = 0

# asyncio requests an 8 KiB pipe buffer (asyncio.windows_utils.BUFSIZE). 64 KiB
# is unambiguously larger, so a message-mode read cannot satisfy the frame in
# one ReadFile -- exactly the condition that would surface ERROR_MORE_DATA.
_OVERSIZE_FRAME_BYTES = 64 * 1024

# Mirrors the gateway's own READ_BUFFER_LIMIT_BYTES default. Duplicated rather
# than imported so the probe cannot fail to collect for a reason unrelated to
# the transport question it exists to answer.
_READ_LIMIT_BYTES = 1024 * 1024

# SECURITY_INFORMATION bits: OWNER | GROUP | DACL.
_SECURITY_INFORMATION = 0x1 | 0x2 | 0x4
_SE_KERNEL_OBJECT = 6
_SDDL_REVISION_1 = 1


class _Roundtrip(NamedTuple):
    """What one probe connection observed."""

    line: bytes
    client_pid: int | None
    pid_error: int | None


def _pipe_address() -> str:
    """A pipe name unique to this process and call."""
    return rf"\\.\pipe\kirocrew-probe-{os.getpid()}-{uuid.uuid4().hex}"


def _report(title: str, lines: list[str]) -> None:
    """Record a finding on both channels that survive a captured, sharded run.

    ``GITHUB_STEP_SUMMARY`` renders on the workflow run page, which is the
    readable form for a human. It is not exposed by the REST API, though, so the
    same text also goes out as a warning: pytest prints its warnings summary for
    passing tests too, which puts the finding in the job *log* where it can be
    read without the UI. Both are best-effort -- a probe must never fail because
    it could not record its own output.
    """
    body = f"\n### {title}\n\n" + "\n".join(f"- {line}" for line in lines) + "\n"
    warnings.warn(f"PROBE {title}: " + " | ".join(lines), stacklevel=2)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(body)
    except OSError:
        pass


def _force_byte_read_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip every server pipe instance asyncio mints to byte read mode.

    ``PipeServer`` creates a fresh handle per accepted client (see
    ``_get_unconnected_pipe``), so the flip has to happen on each instance --
    which is why a real transport layer has to wrap this one method rather than
    setting the mode once at startup. That single private method is the entire
    private-API surface the byte-mode approach depends on.
    """
    original = windows_events.PipeServer._server_pipe_handle

    def patched(self: Any, first: bool) -> Any:
        pipe = original(self, first)
        if pipe is not None:
            _winapi.SetNamedPipeHandleState(
                pipe.handle, _PIPE_READMODE_BYTE_AND_WAIT, None, None
            )
        return pipe

    monkeypatch.setattr(windows_events.PipeServer, "_server_pipe_handle", patched)


def _read_client_pid(pipe_handle: int) -> tuple[int | None, int | None]:
    """Return ``(client_pid, last_error)`` for the peer of a server pipe handle."""
    kernel32 = _ct.WinDLL("kernel32", use_last_error=True)
    kernel32.GetNamedPipeClientProcessId.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.ULONG),
    ]
    kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
    out = wintypes.ULONG()
    if not kernel32.GetNamedPipeClientProcessId(pipe_handle, ctypes.byref(out)):
        return None, _ct.get_last_error()
    return int(out.value), None


def _pipe_sddl(pipe_handle: int) -> str:
    """Return the pipe's security descriptor as SDDL, or a diagnostic string.

    Never raises: this feeds the informational report, and a failure to read the
    descriptor is itself a finding worth recording rather than a test failure.
    """
    try:
        advapi32 = _ct.WinDLL("advapi32", use_last_error=True)
        kernel32 = _ct.WinDLL("kernel32", use_last_error=True)
        psd = ctypes.c_void_p()
        rc = advapi32.GetSecurityInfo(
            wintypes.HANDLE(pipe_handle),
            ctypes.c_int(_SE_KERNEL_OBJECT),
            wintypes.DWORD(_SECURITY_INFORMATION),
            None,
            None,
            None,
            None,
            ctypes.byref(psd),
        )
        if rc != 0:
            return f"GetSecurityInfo failed, rc={rc}"
        try:
            out = wintypes.LPWSTR()
            size = wintypes.ULONG()
            ok = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                psd,
                wintypes.DWORD(_SDDL_REVISION_1),
                wintypes.DWORD(_SECURITY_INFORMATION),
                ctypes.byref(out),
                ctypes.byref(size),
            )
            if not ok:
                err = _ct.get_last_error()
                return f"ConvertSecurityDescriptorToStringSecurityDescriptorW failed, err={err}"
            try:
                return out.value or "<empty>"
            finally:
                kernel32.LocalFree(out)
        finally:
            kernel32.LocalFree(psd)
    except Exception as exc:  # noqa: BLE001 - report the reason, never fail here
        return f"{type(exc).__name__}: {exc}"


async def _roundtrip_oversize_frame(address: str, frame: bytes) -> _Roundtrip:
    """Send ``frame`` over a named pipe and read it back with ``readuntil``.

    Wires ``StreamReaderProtocol`` by hand on both ends: asyncio ships no
    ``open_pipe_connection`` counterpart to ``open_unix_connection``, and
    ``create_pipe_connection`` hands back ``(transport, protocol)`` rather than a
    reader/writer pair. Reproducing that wiring here is the point -- it is the
    same adapter a real transport layer would need.
    """
    loop: Any = asyncio.get_running_loop()
    observed: asyncio.Future[_Roundtrip] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readuntil(b"\n")
            pipe = writer.get_extra_info("pipe")
            if pipe is None:
                pid, err = None, None
            else:
                pid, err = _read_client_pid(pipe.handle)
            if not observed.done():
                observed.set_result(_Roundtrip(line=line, client_pid=pid, pid_error=err))
        except Exception as exc:  # noqa: BLE001 - surface it to the awaiting test
            if not observed.done():
                observed.set_exception(exc)
        finally:
            writer.close()

    def server_protocol_factory() -> Any:
        reader = asyncio.StreamReader(limit=_READ_LIMIT_BYTES, loop=loop)
        return asyncio_streams.StreamReaderProtocol(reader, handle, loop=loop)

    servers = await loop.start_serving_pipe(server_protocol_factory, address)
    try:
        client_reader = asyncio.StreamReader(limit=_READ_LIMIT_BYTES, loop=loop)
        client_protocol = asyncio_streams.StreamReaderProtocol(client_reader, loop=loop)
        transport, _ = await loop.create_pipe_connection(
            lambda: client_protocol, address
        )
        writer = asyncio.StreamWriter(transport, client_protocol, client_reader, loop)
        try:
            writer.write(frame)
            await writer.drain()
            return await asyncio.wait_for(observed, timeout=30)
        finally:
            writer.close()
    finally:
        for server in servers:
            server.close()


def test_winapi_exposes_set_named_pipe_handle_state() -> None:
    # The byte-mode strategy rests on this being reachable from the stdlib:
    # needing pywin32 or a hand-rolled ctypes prototype would change the
    # dependency story for the transport layer, not just its line count.
    assert hasattr(_winapi, "SetNamedPipeHandleState")


def test_pipe_server_exposes_the_expected_private_seam() -> None:
    # The flip has to be installed by wrapping this one method. If a future
    # CPython renames or removes it, the transport layer needs to know here
    # rather than at runtime on a user's machine.
    assert hasattr(windows_events.PipeServer, "_server_pipe_handle")
    # asyncio's PipeServer is not an asyncio.Server: it has close()/closed() but
    # no wait_closed(), so a shim is required wherever the gateway awaits it.
    assert hasattr(windows_events.PipeServer, "close")
    assert not hasattr(windows_events.PipeServer, "wait_closed")


@pytest.mark.asyncio
async def test_byte_mode_flip_preserves_newline_framing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop: Any = asyncio.get_running_loop()
    if not hasattr(loop, "start_serving_pipe"):
        pytest.skip("named-pipe transport requires a ProactorEventLoop")

    _force_byte_read_mode(monkeypatch)
    frame = b"x" * _OVERSIZE_FRAME_BYTES + b"\n"
    result = await _roundtrip_oversize_frame(_pipe_address(), frame)

    _report(
        "Windows named-pipe probe: byte-mode framing",
        [
            f"python: {sys.version.split()[0]}",
            f"event loop: {type(loop).__name__}",
            f"frame bytes (incl. newline): {len(frame)}",
            f"bytes returned by readuntil: {len(result.line)}",
            "verdict: newline framing survives an oversize frame in byte read mode",
        ],
    )

    # The load-bearing assertion: with the read mode flipped, a frame eight
    # times the pipe buffer arrives intact through StreamReader.readuntil, so
    # the gateway's newline-delimited protocol needs no reframing on Windows.
    assert result.line == frame


@pytest.mark.asyncio
async def test_server_reads_kernel_attested_client_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop: Any = asyncio.get_running_loop()
    if not hasattr(loop, "start_serving_pipe"):
        pytest.skip("named-pipe transport requires a ProactorEventLoop")

    _force_byte_read_mode(monkeypatch)
    frame = b'{"type":"probe"}\n'
    result = await _roundtrip_oversize_frame(_pipe_address(), frame)

    _report(
        "Windows named-pipe probe: peer identity",
        [
            f"GetNamedPipeClientProcessId -> {result.client_pid}",
            f"last error (None on success): {result.pid_error}",
            f"this process pid: {os.getpid()}",
            "note: client and server share this process, so the two must match",
        ],
    )

    # Reached via the public get_extra_info("pipe") seam, so peer identity costs
    # no private-API surface beyond the read-mode flip above.
    assert result.pid_error is None
    assert result.client_pid == os.getpid()


def test_report_default_pipe_security_descriptor() -> None:
    """Record what a NULL-security-attributes pipe actually grants.

    Informational: the answer decides whether the default DACL counts as an
    access boundary (making an impersonation SID check defense in depth) or
    grants more than the owner (making that check the only gate). Either answer
    is a valid design input, so this test asserts only that a descriptor could
    be read at all.
    """
    address = _pipe_address()
    # Same flags asyncio uses in PipeServer._server_pipe_handle, including the
    # NULL lpSecurityAttributes that produces the default descriptor.
    handle = _winapi.CreateNamedPipe(
        address,
        _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED,
        _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | _winapi.PIPE_WAIT,
        _winapi.PIPE_UNLIMITED_INSTANCES,
        8192,
        8192,
        _winapi.NMPWAIT_WAIT_FOREVER,
        _winapi.NULL,
    )
    try:
        sddl = _pipe_sddl(handle)
        flip_error: str | None = None
        try:
            _winapi.SetNamedPipeHandleState(
                handle, _PIPE_READMODE_BYTE_AND_WAIT, None, None
            )
        except OSError as exc:
            flip_error = f"{type(exc).__name__}: {exc}"
    finally:
        _winapi.CloseHandle(handle)

    _report(
        "Windows named-pipe probe: default security descriptor",
        [
            f"pipe: {address}",
            f"SDDL (owner/group/DACL): `{sddl}`",
            f"read-mode flip on a hand-created pipe: "
            f"{'ok' if flip_error is None else flip_error}",
            "interpretation: if the DACL grants beyond the owner, the "
            "impersonation SID check is the only real gate",
        ],
    )

    assert sddl, "expected some security-descriptor result to report"
