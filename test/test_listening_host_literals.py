"""Parsing of ``lsof`` listener output into dial-ready host literals.

lsof prints ``*:<port>`` for an IPv4 wildcard and for an IPv6 one alike, so the
TYPE column is the only thing that distinguishes them. Reading an IPv6 wildcard as
IPv4 would send a request — carrying the gateway's internal secret — to an address
this gateway may not hold at all, where another local user's listener can be.
"""

from __future__ import annotations

import pytest

from kiro_crew import platform_compat

HEADER = "COMMAND   PID    USER   FD   TYPE  DEVICE SIZE/OFF NODE NAME\n"


def _stub_lsof(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: "/usr/bin/lsof")
    monkeypatch.setattr(
        platform_compat.subprocess, "check_output", lambda *a, **k: HEADER + body
    )


@pytest.mark.parametrize(
    "line,expected",
    [
        ("python  4242 me   7u  IPv4 0x1  0t0  TCP 127.0.0.1:7788 (LISTEN)\n", ["127.0.0.1"]),
        ("python  4242 me   7u  IPv6 0x1  0t0  TCP [::1]:7788 (LISTEN)\n", ["::1"]),
        # both wildcards print the same NAME; only TYPE separates them
        ("python  4242 me   7u  IPv4 0x1  0t0  TCP *:7788 (LISTEN)\n", ["127.0.0.1"]),
        ("python  4242 me   7u  IPv6 0x1  0t0  TCP *:7788 (LISTEN)\n", ["::1"]),
        ("python  4242 me   7u  IPv4 0x1  0t0  TCP 0.0.0.0:7788 (LISTEN)\n", ["127.0.0.1"]),
    ],
)
def test_family_is_preserved(monkeypatch, line: str, expected: list[str]) -> None:
    _stub_lsof(monkeypatch, line)
    assert platform_compat.listening_host_literals(4242, 7788) == expected


def test_rows_for_other_ports_are_ignored(monkeypatch) -> None:
    _stub_lsof(
        monkeypatch,
        "python  4242 me   7u  IPv4 0x1  0t0  TCP 127.0.0.1:9999 (LISTEN)\n"
        "python  4242 me   8u  IPv6 0x1  0t0  TCP [::1]:7788 (LISTEN)\n",
    )
    assert platform_compat.listening_host_literals(4242, 7788) == ["::1"]


def test_a_dual_stack_gateway_reports_both(monkeypatch) -> None:
    _stub_lsof(
        monkeypatch,
        "python  4242 me   7u  IPv4 0x1  0t0  TCP 127.0.0.1:7788 (LISTEN)\n"
        "python  4242 me   8u  IPv6 0x1  0t0  TCP [::1]:7788 (LISTEN)\n",
    )
    assert platform_compat.listening_host_literals(4242, 7788) == ["127.0.0.1", "::1"]


def test_non_posix_refuses_rather_than_guessing(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    assert platform_compat.listening_host_literals(4242, 7788) == []


def test_missing_tool_refuses_rather_than_guessing(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    assert platform_compat.listening_host_literals(4242, 7788) == []


def test_tool_failure_refuses_rather_than_guessing(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: "/usr/bin/lsof")

    def boom(*a, **k):
        raise OSError("no such tool")

    monkeypatch.setattr(platform_compat.subprocess, "check_output", boom)
    assert platform_compat.listening_host_literals(4242, 7788) == []
