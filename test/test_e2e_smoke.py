"""E2E smoke tests using ``spawn_feature_gateway`` harness.

Phase 5 of the KiroCrew Testing & Release Plan. These tests spawn a real
gateway subprocess, hit its HTTP endpoints with the token from the READY
line, and verify core functionality works end-to-end.

Gated behind ``KIROCREW_E2E=1`` because they spawn a real gateway process
(5-15s startup). CI runs them on a shared test fleet; local devs opt in.

Requires: ``kiro_crew.testing.harness`` and composable
CLI flags.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.request

import pytest

# Gate all tests behind KIROCREW_E2E so they don't slow down local pytest runs.
pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="E2E smoke tests. Set KIROCREW_E2E=1 to run.",
)


@pytest.fixture(scope="module")
def gateway():
    """Spawn one gateway for all smoke tests (amortize 5-15s startup).

    No state cleanup between tests; each test must be tolerant of
    prior-test side effects, or use a fresh per-test gateway.
    """
    from kiro_crew.testing import fake_acp_backend
    from kiro_crew.testing.harness import spawn_feature_gateway

    previous = os.environ.get("KIROCREW_KIRO_BIN")
    os.environ["KIROCREW_KIRO_BIN"] = str(fake_acp_backend.__file__)
    try:
        with spawn_feature_gateway(fixture="minimal", approval="reads") as handle:
            yield handle
    finally:
        if previous is None:
            os.environ.pop("KIROCREW_KIRO_BIN", None)
        else:
            os.environ["KIROCREW_KIRO_BIN"] = previous


_openers: dict[int, urllib.request.OpenerDirector] = {}


def _opener(gateway) -> urllib.request.OpenerDirector:
    """Return a cookie-jar opener primed with the durable session cookie.

    The harness hands us a one-time *link* token. Re-presenting it as a
    ``?token=`` query param on every request works for normal routes, but
    ``mixed_internal`` routes (/api/crons, /api/lessons, /api/chat/slots,
    /api/artifacts) validate the query token against the revoked-nonce
    denylist and 403 once the link nonce is revoked after first use. So mint
    the durable ``mc_token_<port>`` session cookie once via a normal-path GET
    (which carries the link token and Set-Cookies the session), then send
    that cookie on every later request with no ``?token=`` -- the minted
    session token has a fresh, non-revoked nonce so mixed_internal writes
    pass.
    """
    opener = _openers.get(gateway.port)
    if opener is None:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        prime = urllib.request.Request(
            f"http://localhost:{gateway.port}/api/status?token={gateway.token}"
        )
        with opener.open(prime, timeout=10):
            pass
        _openers[gateway.port] = opener
    return opener


def _api_get(gateway, path: str) -> dict:
    """GET an API endpoint via the session cookie, return parsed JSON."""
    req = urllib.request.Request(f"http://localhost:{gateway.port}{path}")
    with _opener(gateway).open(req, timeout=10) as resp:
        return json.loads(resp.read())


def _api_post(gateway, path: str, body: dict) -> dict:
    """POST to an API endpoint via the session cookie, return parsed JSON."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://localhost:{gateway.port}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with _opener(gateway).open(req, timeout=30) as resp:
        return json.loads(resp.read())


# --- Smoke tests ---


def test_health_check(gateway):
    """Gateway responds to /api/status with uptime and version."""
    data = _api_get(gateway, "/api/status")
    assert "uptime" in data
    assert "version" in data


def test_sessions_list(gateway):
    """Gateway lists sessions (minimal fixture ships one starter session)."""
    data = _api_get(gateway, "/api/sessions")
    assert "sessions" in data
    # minimal fixture ships dashboard_starter.jsonl
    assert data["total"] >= 1


def test_create_chat_slot(gateway):
    """Can create a new chat slot via the API."""
    data = _api_post(gateway, "/api/chat/slots", {})
    assert "key" in data
    slot_key = data["key"]
    assert slot_key  # non-empty string


def test_cron_list_fixture_crons(gateway):
    """Cron list returns the fixture's crons (minimal has 2: active + paused)."""
    data = _api_get(gateway, "/api/crons")
    assert "jobs" in data
    # minimal fixture ships 2 crons
    assert len(data["jobs"]) >= 2


def test_memory_workspace_exists(gateway):
    """Memory workspace directory exists in the seeded home."""
    workspace_dir = gateway.home / "workspace" / "memory"
    assert workspace_dir.is_dir()
    assert (workspace_dir / "preferences.md").is_file()


# --- API round-trip tests ---


def _api_delete(gateway, path: str) -> dict:
    """DELETE an API endpoint via the session cookie, return parsed JSON."""
    req = urllib.request.Request(f"http://localhost:{gateway.port}{path}", method="DELETE")
    with _opener(gateway).open(req, timeout=10) as resp:
        return json.loads(resp.read())


def _api_post_with_session(
    gateway, path: str, body: dict, session_key: str = "dashboard:ui"
) -> dict:
    """POST via the session cookie + X-Session-Key header."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://localhost:{gateway.port}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Session-Key", session_key)
    with _opener(gateway).open(req, timeout=30) as resp:
        return json.loads(resp.read())


def test_cron_create_and_delete(gateway):
    """Can create a cron job via API and then delete it."""
    body = {"name": "e2e-test-cron", "every": 3600, "message": "hello from e2e"}
    data = _api_post(gateway, "/api/crons", body)
    assert "id" in data
    job_id = data["id"]
    assert job_id

    try:
        # Verify it appears in the list
        crons = _api_get(gateway, "/api/crons")
        assert any(j["id"] == job_id for j in crons["jobs"])
    finally:
        _api_delete(gateway, f"/api/crons/{job_id}")


def test_lessons_create_and_list(gateway):
    """Can create a lesson and see it in the list."""
    body = {"rule": "e2e test lesson", "category": "knowledge"}
    _api_post_with_session(gateway, "/api/lessons", body)

    data = _api_get(gateway, "/api/lessons")
    assert "lessons" in data
    assert any("e2e test lesson" in str(lesson) for lesson in data["lessons"])


def test_chat_slot_lifecycle(gateway):
    """Create a slot, get its detail, then delete it."""
    # Create
    create_data = _api_post(gateway, "/api/chat/slots", {})
    slot_key = create_data["key"]
    assert slot_key

    try:
        # Detail
        detail = _api_get(gateway, f"/api/chat/slots/{slot_key}")
        assert detail["key"] == slot_key
        assert "messages" in detail
    finally:
        _api_delete(gateway, f"/api/chat/slots/{slot_key}")


def test_session_detail(gateway):
    """Can fetch detail for the fixture-seeded session (returns message list).

    The minimal fixture ships ``dashboard_starter.jsonl`` -- assert against
    that known session rather than depending on ambient state.
    """
    sessions = _api_get(gateway, "/api/sessions")
    assert sessions["total"] >= 1, "minimal fixture must seed at least one session"
    # Use the fixture-seeded session (known precondition)
    first_key = sessions["sessions"][0]["key"]

    detail = _api_get(gateway, f"/api/sessions/{first_key}")
    assert isinstance(detail, list)


def test_session_search(gateway):
    """Session search endpoint returns results without errors."""
    data = _api_get(gateway, "/api/sessions/search?q=test")
    assert "sessions" in data


# --- Send -> assert (fake ACP backend) ---------------------------------------
#
# The control-plane tests above never exercise a model turn. This section
# stands up a *fake* ACP backend and drives a real chat turn end-to-end
# (gateway HTTP -> chat runner -> subprocess spawn -> ACP handshake -> streamed
# agent_message_chunk -> assembled assistant reply), fully offline.
#
# Seam: the gateway's provider already defaults to ``acp`` (the minimal fixture
# omits ``agent.provider``), so pointing ``KIROCREW_KIRO_BIN`` at the fake makes
# a turn spawn the fake instead of a real ``kiro-cli`` -- no new provider enum,
# no config-key override, nothing remotely selectable.


@pytest.fixture()
def acp_gateway(monkeypatch):
    """Gateway whose ``kiro-cli`` is the offline fake ACP backend.

    Function-scoped (separate from the module ``gateway``): only these tests
    want the fake wired in, and it must not leak ``KIROCREW_KIRO_BIN`` into the
    control-plane gateway. ``monkeypatch`` restores the env on teardown.
    """
    from kiro_crew.testing import fake_acp_backend
    from kiro_crew.testing.harness import spawn_feature_gateway

    # Execute the packaged fake itself so readiness probes and ACP turns use
    # the same deterministic backend rather than a test-only copy.
    monkeypatch.setenv("KIROCREW_KIRO_BIN", str(fake_acp_backend.__file__))

    with spawn_feature_gateway(fixture="minimal", approval="reads") as handle:
        # The KIROCREW_KIRO_BIN seam only fires when the provider resolves to
        # ``acp`` (the minimal fixture omits ``agent.provider``, which defaults
        # to acp). Fail fast with a clear message if that assumption ever
        # breaks, rather than surfacing it 60s later as a "no reply" timeout.
        cfg = handle.home / "config.json"
        if cfg.is_file():
            provider = (
                json.loads(cfg.read_text(encoding="utf-8")).get("agent", {}).get("provider", "acp")
            )
            assert provider == "acp", (
                "acp_gateway needs provider=acp for the KIROCREW_KIRO_BIN seam; "
                f"minimal fixture resolved provider={provider!r}"
            )
        yield handle


def _await_assistant_reply(gateway, slot: str, timeout: float = 60.0) -> dict:
    """Poll a slot's history until a non-empty assistant reply appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = _api_get(gateway, f"/api/chat/slots/{slot}")
        for msg in detail.get("messages", []):
            if msg.get("role") == "assistant" and str(msg.get("content", "")).strip():
                return msg
        time.sleep(0.5)
    raise AssertionError(f"no non-empty assistant reply within {timeout:.0f}s")


def test_chat_send_receives_reply(acp_gateway):
    """A chat turn against the fake ACP backend yields a non-empty reply.

    This is the send->assert gate: it proves the whole gateway<->subprocess<->
    ACP path works (spawn, initialize/session-new handshake, prompt, streamed
    ``agent_message_chunk``, reply assembly + persistence) with a deterministic
    offline backend -- no Bedrock, no network, no auth.
    """
    slot = _api_post(acp_gateway, "/api/chat/slots", {})["key"]
    assert slot

    # ws=1: POST returns immediately; the reply is delivered over the WebSocket
    # AND persisted to the slot's message history, which we poll below.
    _api_post(
        acp_gateway,
        "/api/chat?ws=1",
        {"message": "ping", "slot": slot, "agent": "kirocrew"},
    )

    reply = _await_assistant_reply(acp_gateway, slot)
    # Search the serialized message so the assertion doesn't couple to the
    # exact content field name after display preparation/redaction.
    assert "pong from the fake ACP backend" in json.dumps(reply)


def test_chat_tool_call_renders(acp_gateway):
    """A ``[[TOOL]]`` prompt drives the fake's tool-call path end-to-end.

    The fake emits a ``tool_call`` update + a ``tool_call_update`` (no
    permission) before the reply. Asserting the final reply still arrives
    proves the gateway processed the tool-call updates without stalling the
    turn -- the offline tool-card coverage the Playwright E2E builds on. The
    ``[[PERMISSION]]`` approval-modal path needs a UI to resolve the modal, so
    it is exercised by Playwright, not this headless suite.
    """
    slot = _api_post(acp_gateway, "/api/chat/slots", {})["key"]
    assert slot

    _api_post(
        acp_gateway,
        "/api/chat?ws=1",
        {"message": "please [[TOOL]] run the demo", "slot": slot, "agent": "kirocrew"},
    )

    reply = _await_assistant_reply(acp_gateway, slot)
    assert "pong from the fake ACP backend" in json.dumps(reply)


# --- Fork (KlaudeCrew): claude-backend send -> assert -------------------------
#
# The two sections above exercise the kiro dialect via KIROCREW_KIRO_BIN, which
# only the kiro branch of the ACP client consults. This section is the claude
# equivalent: the fake ACP backend is already directly executable
# (shebang + exec bit, same as the kiro path resolves it), so pointing
# CLAUDE_AGENT_ACP_BIN at it exercises the SAME _is_claude branches a real
# claude-agent-acp spawn would, end to end through a real gateway subprocess --
# the offline smoke test for M2's MCP-injection contract and the general
# claude wire shape (integer protocolVersion, session/set_config_option, etc.),
# without needing the actual Node adapter or a live `claude` install.


@pytest.fixture()
def claude_gateway(tmp_path, monkeypatch):
    """Gateway whose claude-agent-acp is the offline fake ACP backend, in
    claude-dialect mode.

    Mirrors ``acp_gateway`` above but for the claude branch:
    ``spawn_feature_gateway(acp_backend="claude")`` sets
    ``KIROCREW_TEST_ACP_BACKEND=claude`` so the spawned gateway's
    ``agent.acp_backend`` resolves to claude regardless of the real default,
    and ``CLAUDE_AGENT_ACP_BIN`` + ``FAKE_ACP_DIALECT=claude`` route that
    spawn to the fake's claude wire shape instead of a real Node adapter.
    """
    from kiro_crew.testing import fake_acp_backend
    from kiro_crew.testing.harness import spawn_feature_gateway

    mcp_probe = tmp_path / "mcp_probe.json"
    monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(fake_acp_backend.__file__))
    monkeypatch.setenv("FAKE_ACP_DIALECT", "claude")
    monkeypatch.setenv("FAKE_ACP_MCP_PROBE_PATH", str(mcp_probe))
    # The claude path writes <work_dir>/.claude/settings.local.json against
    # the real filesystem (klaude/settings_seed.py) -- isolate it under the
    # harness's own tmp KIROCREW_HOME rather than the operator's ~/.claude.
    monkeypatch.setenv("KIROCREW_CC_ISOLATE", "1")

    with spawn_feature_gateway(
        fixture="minimal", approval="reads", acp_backend="claude"
    ) as handle:
        cfg = handle.home / "config.json"
        if cfg.is_file():
            backend = (
                json.loads(cfg.read_text(encoding="utf-8"))
                .get("agent", {})
                .get("acp_backend", "claude")
            )
            assert backend != "kiro", (
                "claude_gateway needs the spawned gateway to resolve the "
                "claude backend; config.json explicitly pinned it to kiro"
            )
        yield handle


def test_claude_chat_send_receives_reply(claude_gateway):
    """A chat turn against the claude-dialect fake yields a non-empty reply.

    Proves the whole path -- gateway HTTP -> chat runner -> subprocess spawn
    via CLAUDE_AGENT_ACP_BIN -> the claude `initialize` handshake (integer
    protocolVersion) -> `session/new` with a real mcpServers array ->
    `session/prompt` -> streamed `agent_message_chunk` -> assembled reply --
    works with the fork's claude backend selected, not just kiro.
    """
    slot = _api_post(claude_gateway, "/api/chat/slots", {})["key"]
    assert slot

    _api_post(
        claude_gateway,
        "/api/chat?ws=1",
        {"message": "ping", "slot": slot, "agent": "kirocrew"},
    )

    reply = _await_assistant_reply(claude_gateway, slot)
    assert "pong from the fake ACP backend" in json.dumps(reply)


def test_claude_session_new_carries_mcp_servers(claude_gateway, tmp_path):
    """The claude branch actually populates `session/new`'s `mcpServers`.

    This is M2's whole point: claude-agent-acp reads no config file, so
    without `klaude/mcp_servers.claude_session_servers()` a claude session
    gets ZERO MCP tools (no cron, no subagents, no memory). The fake records
    exactly what it received in FAKE_ACP_MCP_PROBE_PATH -- assert the
    managed kirocrew-core/-cron servers are actually in that array, not just
    that a turn completed (which would also "pass" with an empty array).

    ``tmp_path`` here resolves to the SAME directory the ``claude_gateway``
    fixture used to build ``FAKE_ACP_MCP_PROBE_PATH`` -- pytest hands one
    ``tmp_path`` per test node, shared by every fixture the test requests.
    """
    mcp_probe = tmp_path / "mcp_probe.json"
    slot = _api_post(claude_gateway, "/api/chat/slots", {})["key"]
    _api_post(
        claude_gateway,
        "/api/chat?ws=1",
        {"message": "ping", "slot": slot, "agent": "kirocrew"},
    )
    _await_assistant_reply(claude_gateway, slot)

    deadline = time.monotonic() + 10.0
    servers = None
    while time.monotonic() < deadline:
        if mcp_probe.is_file():
            try:
                servers = json.loads(mcp_probe.read_text())
            except (OSError, json.JSONDecodeError):
                servers = None
            if servers is not None:
                break
        time.sleep(0.2)

    assert servers, "fake ACP backend never received a populated mcpServers array"
    names = {s.get("name") for s in servers if isinstance(s, dict)}
    assert "kirocrew-core" in names
    assert "kirocrew-cron" in names


def test_claude_tool_call_renders(claude_gateway):
    """A ``[[TOOL]]`` prompt drives the fake's tool-call path over the claude
    branch too -- same structural coverage as the kiro-side
    ``test_chat_tool_call_renders``, proving tool_call/tool_call_update
    handling isn't kiro-dialect-specific."""
    slot = _api_post(claude_gateway, "/api/chat/slots", {})["key"]
    assert slot

    _api_post(
        claude_gateway,
        "/api/chat?ws=1",
        {"message": "please [[TOOL]] run the demo", "slot": slot, "agent": "kirocrew"},
    )

    reply = _await_assistant_reply(claude_gateway, slot)
    assert "pong from the fake ACP backend" in json.dumps(reply)


def test_claude_api_models_serves_advertised_list(claude_gateway):
    """Fork (KlaudeCrew) picker feature, end to end through a REAL gateway:
    after a session has spawned (the chat turn), GET /api/models on the
    claude backend serves the fake's advertised catalog -- real HTTP, real
    subprocess, the fake's live-verified configOptions[id="model"] wire
    shape, agents.py's _cc_models merge, no kiro-cli anywhere."""
    slot = _api_post(claude_gateway, "/api/chat/slots", {})["key"]
    _api_post(
        claude_gateway,
        "/api/chat?ws=1",
        {"message": "ping", "slot": slot, "agent": "kirocrew"},
    )
    _await_assistant_reply(claude_gateway, slot)

    # The session's advertised list is captured at session/new; poll briefly
    # in case the capture races the reply assertion.
    deadline = time.monotonic() + 15.0
    rows = None
    while time.monotonic() < deadline:
        try:
            rows = _api_get(claude_gateway, "/api/models")
            break
        except Exception:
            time.sleep(0.5)  # 503 cc_models_cold_start until a session captures

    assert rows, "GET /api/models never served the advertised list"
    names = [r["model_name"] for r in rows]
    assert names[0] == "auto"
    assert "opus[1m]" in names  # verbatim advertised wire value
    assert "sonnet-4.6-1m" in names  # advertised "sonnet" folded onto canonical
    # Never a Bedrock id on the non-Bedrock claude path.
    assert not any(n.startswith("global.anthropic.") for n in names)
