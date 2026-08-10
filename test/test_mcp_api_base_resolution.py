"""Gateway-port resolution for MCP tool callbacks.

A gateway started with ``--port`` records its effective port only in its run
marker; nothing writes it into config. Before this resolution existed, every tool
that calls back into the gateway (``file_send``, ``learn_add``, ``send_message``,
cron and spawn control) targeted the 5476 default and was refused, silently.
"""

from __future__ import annotations

import importlib
import socket
import urllib.error
from typing import Any

import pytest

from kiro_crew.dashboard.urls import _DEFAULT_PORT
from kiro_crew.instances import discovery


@pytest.fixture(autouse=True)
def _no_env_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIROCREW_PORT", raising=False)


class TestPrecedenceChain:
    """One function owns the order, so a tool call and a CLI command agree."""

    def test_marker_wins_when_nothing_is_configured(self, monkeypatch) -> None:
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        assert discovery.resolve_port("") == 7788

    def test_default_when_no_marker(self, monkeypatch) -> None:
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: None)
        assert discovery.resolve_port("") == _DEFAULT_PORT

    def test_env_beats_marker(self, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PORT", "6001")
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        assert discovery.resolve_port("http://localhost:6002") == 6001

    def test_explicit_url_port_beats_marker(self, monkeypatch) -> None:
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        assert discovery.resolve_port("http://localhost:6002") == 6002

    def test_portless_url_falls_through_to_marker(self, monkeypatch) -> None:
        """A host-only URL names no port, so discovery still applies."""
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        assert discovery.resolve_port("http://my-host.example.com") == 7788

    def test_non_string_url_is_tolerated(self, monkeypatch) -> None:
        """``dashboard.url`` is user-editable JSON and may hold any type."""
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        assert discovery.resolve_port(123) == 7788

    def test_malformed_env_port_is_ignored(self, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PORT", "not-a-port")
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        assert discovery.resolve_port("") == 7788

    def test_ambiguity_is_reported_to_the_caller_not_printed(self, monkeypatch) -> None:
        """The leaf has no opinion about stderr; the CLI supplies the message."""
        seen: list[list[int]] = []
        monkeypatch.setattr(discovery.run_marker, "marker_ports", lambda: [7788, 7799])
        monkeypatch.setattr(discovery, "_gateway_owns_port", lambda p: True)
        assert discovery._marker_port(on_ambiguous=seen.append) is None
        assert seen == [[7788, 7799]]


class TestDialTarget:
    """The host follows the same evidence as the port.

    Ownership is proven against a port NUMBER (the listener probe is family-blind),
    so dialing a hard-coded family can reach a different process entirely: with the
    gateway on ``[::1]:7788`` an IPv4 dial lands on whatever another local user
    bound there, and pinning IPv6 merely inverts the hole.
    """

    def test_ipv4_bound_gateway_is_dialled_over_ipv4(self, monkeypatch) -> None:
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        monkeypatch.setattr(discovery.run_marker, "read_pid", lambda port: 4242)
        monkeypatch.setattr(
            discovery.platform_compat, "listening_host_literals", lambda pid, port: ["127.0.0.1"]
        )
        assert discovery.resolve_dial_target("") == ("127.0.0.1", 7788)

    def test_ipv6_bound_gateway_is_dialled_over_ipv6(self, monkeypatch) -> None:
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        monkeypatch.setattr(discovery.run_marker, "read_pid", lambda port: 4242)
        monkeypatch.setattr(
            discovery.platform_compat, "listening_host_literals", lambda pid, port: ["::1"]
        )
        assert discovery.resolve_dial_target("") == ("[::1]", 7788)

    def test_wildcard_bind_answers_on_ipv4(self, monkeypatch) -> None:
        """An IPv4 wildcard arrives already resolved to IPv4 loopback."""
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        monkeypatch.setattr(discovery.run_marker, "read_pid", lambda port: 4242)
        monkeypatch.setattr(
            discovery.platform_compat, "listening_host_literals", lambda pid, port: ["127.0.0.1"]
        )
        assert discovery.resolve_dial_target("") == ("127.0.0.1", 7788)

    def test_ipv6_wildcard_is_not_dialled_over_ipv4(self, monkeypatch) -> None:
        """``KIROCREW_BIND=::`` binds IPv6 only; the IPv4 side may be someone else."""
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        monkeypatch.setattr(discovery.run_marker, "read_pid", lambda port: 4242)
        monkeypatch.setattr(
            discovery.platform_compat, "listening_host_literals", lambda pid, port: ["::1"]
        )
        assert discovery.resolve_dial_target("") == ("[::1]", 7788)

    def test_undeterminable_address_discards_the_discovered_port(self, monkeypatch) -> None:
        """No address evidence means no discovery: the default is the safe landing."""
        monkeypatch.setattr(discovery, "_marker_port", lambda **_: 7788)
        monkeypatch.setattr(discovery.run_marker, "read_pid", lambda port: 4242)
        monkeypatch.setattr(
            discovery.platform_compat, "listening_host_literals", lambda pid, port: []
        )
        assert discovery.resolve_dial_target("") == (None, _DEFAULT_PORT)

    def test_configured_port_carries_no_verified_host(self, monkeypatch) -> None:
        """An explicitly named port has no ownership proof, so no host is asserted."""
        monkeypatch.setenv("KIROCREW_PORT", "6001")
        assert discovery.resolve_dial_target("") == (None, 6001)


@pytest.fixture
def mcp(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module("kiro_crew.mcp_core")
    monkeypatch.setattr(module, "_API_CACHE", None, raising=False)

    class _Cfg:
        class dashboard:  # noqa: N801 - mirrors the config object's shape
            url = ""

    monkeypatch.setattr(module.KiroCrewConfig, "load", staticmethod(lambda: _Cfg))
    return module


class TestApiBase:
    def test_discovered_ipv6_gateway_is_dialled_over_ipv6(self, mcp: Any, monkeypatch) -> None:
        """Neither family may be pinned blind — the base follows the verified bind."""
        monkeypatch.setattr(mcp, "resolve_dial_target", lambda url: ("[::1]", 7788))
        assert mcp._resolve_api_base() == "http://[::1]:7788"

    def test_discovered_ipv4_gateway_is_dialled_over_ipv4(self, mcp: Any, monkeypatch) -> None:
        monkeypatch.setattr(mcp, "resolve_dial_target", lambda url: ("127.0.0.1", 7788))
        assert mcp._resolve_api_base() == "http://127.0.0.1:7788"

    def test_unverified_port_keeps_localhost(self, mcp: Any, monkeypatch) -> None:
        """With nothing discovered there is no proof either way, so behaviour is unchanged."""
        monkeypatch.setattr(mcp, "resolve_dial_target", lambda url: (None, _DEFAULT_PORT))
        assert mcp._resolve_api_base() == f"http://localhost:{_DEFAULT_PORT}"

    def test_resolved_lazily_not_at_import(self, mcp: Any, monkeypatch) -> None:
        """A gateway that appears after this process booted must still be reachable."""
        targets = iter([(None, _DEFAULT_PORT), ("127.0.0.1", 7788)])
        monkeypatch.setattr(mcp, "resolve_dial_target", lambda url: next(targets))
        assert mcp._api_base() == f"http://localhost:{_DEFAULT_PORT}"
        assert mcp._api_base() == f"http://localhost:{_DEFAULT_PORT}"  # cached
        mcp._invalidate_api_base()
        assert mcp._api_base() == "http://127.0.0.1:7788"

    def test_socket_path_expires_with_the_base(self, mcp: Any, monkeypatch) -> None:
        """A moved gateway must not leave this process on the old port's socket."""
        ports = iter([5476, 7788])
        monkeypatch.setattr(mcp, "resolve_port", lambda url: next(ports))
        monkeypatch.setattr(mcp, "_API_UNIX_SOCKET_CACHE", None, raising=False)
        first = mcp._api_unix_socket()
        assert mcp._api_unix_socket() == first  # cached
        mcp._invalidate_api_base()
        assert mcp._api_unix_socket() != first


class _Resp:
    def __init__(self, payload: bytes = b'{"ok": true}') -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


@pytest.fixture
def refusing_gateway(mcp: Any, monkeypatch):
    """First call refused on the default port; the marker then names 7788."""
    urls: list[str] = []
    targets = iter([(None, _DEFAULT_PORT), ("127.0.0.1", 7788), ("127.0.0.1", 7788)])
    monkeypatch.setattr(mcp, "resolve_dial_target", lambda url: next(targets))
    monkeypatch.setattr(mcp, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mcp, "_resolve_session_key", lambda: "")
    monkeypatch.setattr(mcp, "_session_key_header_error", lambda sk: None)

    def fake_open(req, timeout=None):
        urls.append(req.full_url)
        if f":{_DEFAULT_PORT}" in req.full_url:
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        return _Resp()

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    return mcp, urls


@pytest.mark.parametrize("verb", ["_post", "_get", "_patch", "_put", "_delete"])
def test_every_verb_rediscovers_and_replays(refusing_gateway, verb: str) -> None:
    """A stale base must not survive in one verb after another has learned better."""
    mcp, urls = refusing_gateway
    call = getattr(mcp, verb)
    out = call("/api/x") if verb == "_get" else call("/api/x", {"k": "v"})
    assert out == {"ok": True}
    assert len(urls) == 2
    assert f":{_DEFAULT_PORT}" in urls[0]
    assert ":7788" in urls[1]


def test_no_replay_when_rediscovery_returns_the_same_base(mcp: Any, monkeypatch) -> None:
    """Retrying an unchanged dead base would only double the latency."""
    attempts: list[str] = []
    monkeypatch.setattr(mcp, "resolve_dial_target", lambda url: (None, _DEFAULT_PORT))
    monkeypatch.setattr(mcp, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mcp, "_resolve_session_key", lambda: "")
    monkeypatch.setattr(mcp, "_session_key_header_error", lambda sk: None)

    def fake_open(req, timeout=None):
        attempts.append(req.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    assert "error" in mcp._post("/api/x", {"k": "v"})
    assert len(attempts) == 1


def test_only_post_reports_transport_error(mcp: Any, monkeypatch) -> None:
    """``transport_error`` is spawn_run's signal; other verbs keep their shape."""
    monkeypatch.setattr(mcp, "resolve_dial_target", lambda url: (None, _DEFAULT_PORT))
    monkeypatch.setattr(mcp, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mcp, "_resolve_session_key", lambda: "")
    monkeypatch.setattr(mcp, "_session_key_header_error", lambda sk: None)

    def fake_open(req, timeout=None):
        raise urllib.error.URLError(socket.timeout("slow"))

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    assert mcp._post("/api/x", {}).get("transport_error") is True
    assert "transport_error" not in mcp._get("/api/x")
    assert "transport_error" not in mcp._patch("/api/x", {})


def test_replay_that_fails_after_connecting_stays_ambiguous(mcp: Any, monkeypatch) -> None:
    """A spawn accepted by the rediscovered gateway must not be reported as lost.

    spawn_run reconciles a member down and posts ``/api/spawn/lost`` on a definite
    rejection. If the replay reaches the gateway and only the response read fails,
    acceptance is undetermined — collapsing that to a plain error orphans a
    still-running subagent and closes the batch early.
    """
    monkeypatch.setattr(mcp, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mcp, "_resolve_session_key", lambda: "")
    monkeypatch.setattr(mcp, "_session_key_header_error", lambda sk: None)
    targets = iter([(None, _DEFAULT_PORT), ("127.0.0.1", 7788), ("127.0.0.1", 7788)])
    monkeypatch.setattr(mcp, "resolve_dial_target", lambda url: next(targets))

    def fake_open(req, timeout=None):
        if f":{_DEFAULT_PORT}" in req.full_url:
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        raise TimeoutError("read timed out after the spawn was accepted")

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    out = mcp._post("/api/spawn", {"tasks": ["x"]})
    assert out.get("transport_error") is True


def test_replay_refused_again_is_a_definite_rejection(mcp: Any, monkeypatch) -> None:
    """Refused on both bases means nothing was ever accepted."""
    monkeypatch.setattr(mcp, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mcp, "_resolve_session_key", lambda: "")
    monkeypatch.setattr(mcp, "_session_key_header_error", lambda sk: None)
    targets = iter([(None, _DEFAULT_PORT), ("127.0.0.1", 7788), ("127.0.0.1", 7788)])
    monkeypatch.setattr(mcp, "resolve_dial_target", lambda url: next(targets))

    def fake_open(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    out = mcp._post("/api/spawn", {"tasks": ["x"]})
    assert "error" in out
    assert "transport_error" not in out
