"""Fork (KlaudeCrew): coverage for the claude-backend `_bg` session path --
`SessionManager._bg_provider_is_kiro()`'s backend dispatch and
`_ProviderBgSession`'s AcpSessionHandle-compatible surface (session_id,
served_model, set_model, prompt, reject_tool, destroy) including the
first-touch semaphore rule that keeps a caller's model pin and its following
prompt atomic on the now-shared `_bg` session. See issue #3 and
docs/system-specs/modules/session.md "Multiplexed _bg runtime".
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.config import KiroCrewConfig
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session import BACKGROUND_AGENT, BACKGROUND_KEY, SessionManager, _ProviderBgSession


def _make_manager(acp_backend: str = "claude", provider_factory=None) -> SessionManager:
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = acp_backend
    return SessionManager(cfg, provider_factory=provider_factory)


def _fake_acp_provider(*, resolved_model_id: str = "", session_id: str = "fake-sid"):
    """A real AcpProvider instance (isinstance checks pass) with heavy
    __init__ skipped and only the attributes _ProviderBgSession/its
    properties actually touch faked in."""
    provider = AcpProvider.__new__(AcpProvider)
    client = MagicMock()
    client.set_model = AsyncMock()
    client._resolved_model_id = resolved_model_id
    provider._client = client
    provider._session_id = session_id
    return provider


def _new_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(1)


class TestBgProviderIsKiro:
    """Truth table for the backend-dispatch predicate. `agent.provider` is a
    single-valued enum in this fork (always "acp") -- the real switch is
    agent.acp_backend, and this predicate must consult it."""

    def test_claude_backend_is_not_kiro(self):
        mgr = _make_manager(acp_backend="claude")
        assert mgr._bg_provider_is_kiro() is False

    def test_kiro_backend_is_kiro(self):
        mgr = _make_manager(acp_backend="kiro")
        assert mgr._bg_provider_is_kiro() is True

    def test_missing_acp_backend_attr_is_not_kiro(self):
        mgr = _make_manager(acp_backend="claude")
        mgr._cfg.agent = SimpleNamespace(provider="acp")  # no acp_backend attr at all
        assert mgr._bg_provider_is_kiro() is False

    def test_non_acp_provider_is_not_kiro_even_with_kiro_backend(self):
        mgr = _make_manager(acp_backend="kiro")
        mgr._cfg.agent.provider = "something-else"
        assert mgr._bg_provider_is_kiro() is False


class TestGetBgSessionClaudeDispatch:
    @pytest.mark.asyncio
    async def test_claude_backend_returns_provider_bg_session_never_touches_acp_runtime(self):
        provider = _fake_acp_provider()
        provider.start = AsyncMock()

        def factory(session_key=None, agent=None, **kwargs):
            assert session_key == BACKGROUND_KEY
            assert agent == BACKGROUND_AGENT
            return provider

        mgr = _make_manager(acp_backend="claude", provider_factory=factory)

        # AcpRuntime is kiro-only; the claude dispatch must never construct
        # one, so make any attempt to do so fail loudly instead of silently
        # spawning a real kiro-cli process.
        import kiro_crew.acp.runtime as runtime_mod

        original = runtime_mod.AcpRuntime
        runtime_mod.AcpRuntime = MagicMock(
            side_effect=AssertionError("claude _bg dispatch must not construct AcpRuntime")
        )
        try:
            session = await mgr.get_bg_session()
        finally:
            runtime_mod.AcpRuntime = original

        assert isinstance(session, _ProviderBgSession)
        provider.start.assert_awaited_once()
        await session.destroy()


class TestProviderBgSessionDelegation:
    @pytest.mark.asyncio
    async def test_served_model_delegates_to_provider(self):
        provider = _fake_acp_provider(resolved_model_id="sonnet")
        sess = SimpleNamespace(provider=provider)
        bg = _ProviderBgSession(sess)  # type: ignore[arg-type]

        assert bg.served_model == "sonnet"

    @pytest.mark.asyncio
    async def test_served_model_auto_sentinel_reads_as_empty(self):
        # DEFAULT_MODEL ("auto") means "unknown/inconclusive" -- filtered to "".
        provider = _fake_acp_provider(resolved_model_id="auto")
        sess = SimpleNamespace(provider=provider)
        bg = _ProviderBgSession(sess)  # type: ignore[arg-type]

        assert bg.served_model == ""

    @pytest.mark.asyncio
    async def test_set_model_delegates_to_provider_client(self):
        provider = _fake_acp_provider()
        sess = SimpleNamespace(provider=provider, semaphore=_new_semaphore())
        bg = _ProviderBgSession(sess)  # type: ignore[arg-type]

        await bg.set_model("sonnet")

        provider._client.set_model.assert_awaited_once_with("sonnet")

    @pytest.mark.asyncio
    async def test_set_model_on_non_acp_provider_raises(self):
        # A non-AcpProvider _bg provider (should not occur in practice, since
        # only AcpProvider backs claude/kiro sessions) has no set_model seam.
        sess = SimpleNamespace(provider=object(), semaphore=_new_semaphore())
        bg = _ProviderBgSession(sess)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="has no set_model"):
            await bg.set_model("sonnet")


class TestProviderBgSessionSemaphoreFirstTouch:
    @pytest.mark.asyncio
    async def test_set_model_acquires_and_holds_semaphore(self):
        provider = _fake_acp_provider()
        sem = _new_semaphore()
        sess = SimpleNamespace(provider=provider, semaphore=sem)
        bg = _ProviderBgSession(sess)  # type: ignore[arg-type]

        assert not sem.locked()
        await bg.set_model("sonnet")
        assert sem.locked()

        await bg.destroy()
        assert not sem.locked()

    @pytest.mark.asyncio
    async def test_prompt_does_not_double_acquire_after_set_model(self):
        """The core first-touch guarantee: set_model then prompt() on the SAME
        _ProviderBgSession must not deadlock trying to re-acquire a
        Semaphore(1) it already holds."""
        provider = _fake_acp_provider()

        async def _fake_stream(message):
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="hi")
            yield AcpEvent(kind=EVENT_COMPLETE)

        provider.stream = _fake_stream
        sem = _new_semaphore()
        sess = SimpleNamespace(provider=provider, semaphore=sem)
        bg = _ProviderBgSession(sess)  # type: ignore[arg-type]

        await bg.set_model("sonnet")
        assert sem.locked()

        chunks = []

        async def _drain():
            async for event in bg.prompt("hello"):
                chunks.append(event)

        await asyncio.wait_for(_drain(), timeout=1.0)  # hangs forever if double-acquired

        assert [e.kind for e in chunks] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        await bg.destroy()
        assert not sem.locked()

    @pytest.mark.asyncio
    async def test_prompt_without_prior_set_model_still_acquires(self):
        """A caller that never overrides the model (the common case) must
        still get the normal acquire-on-first-use behavior."""
        provider = _fake_acp_provider()

        async def _fake_stream(message):
            yield AcpEvent(kind=EVENT_COMPLETE)

        provider.stream = _fake_stream
        sem = _new_semaphore()
        sess = SimpleNamespace(provider=provider, semaphore=sem)
        bg = _ProviderBgSession(sess)  # type: ignore[arg-type]

        assert not sem.locked()
        events = [e async for e in bg.prompt("hello")]
        assert [e.kind for e in events] == [EVENT_COMPLETE]
        assert not sem.locked()  # released on generator completion

    @pytest.mark.asyncio
    async def test_destroy_before_any_acquire_is_a_noop(self):
        provider = _fake_acp_provider()
        sem = _new_semaphore()
        sess = SimpleNamespace(provider=provider, semaphore=sem)
        bg = _ProviderBgSession(sess)  # type: ignore[arg-type]

        await bg.destroy()  # must not raise / must not release an unheld semaphore
        assert not sem.locked()


class TestRunBgOnelinerStrictModelOverProviderBgSession:
    """End-to-end: run_bg_oneliner(strict_model=True) -- the poisoned-
    conversation canary's mode -- actually works over _ProviderBgSession now
    that it exposes set_model/served_model. Before the fix this raised
    RuntimeError("...requires a session with set_model()") on every claude
    backend, permanently disabling the canary there."""

    @pytest.mark.asyncio
    async def test_strict_model_succeeds_when_session_serves_requested_model(self):
        provider = _fake_acp_provider()

        async def _set_model(model_id):
            provider._client._resolved_model_id = model_id

        provider._client.set_model = AsyncMock(side_effect=_set_model)

        async def _fake_stream(message):
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield AcpEvent(kind=EVENT_COMPLETE)

        provider.stream = _fake_stream
        sess = SimpleNamespace(provider=provider, semaphore=_new_semaphore())
        bg = _ProviderBgSession(sess)  # type: ignore[arg-type]

        sessions = SimpleNamespace(get_bg_session=AsyncMock(return_value=bg))

        result = await run_bg_oneliner(sessions, "canary prompt", model="sonnet", strict_model=True)

        assert result == "ok"
        provider._client.set_model.assert_awaited_once_with("sonnet")

    @pytest.mark.asyncio
    async def test_strict_model_raises_when_session_serves_a_different_model(self):
        provider = _fake_acp_provider(resolved_model_id="haiku")  # never actually switches
        sess = SimpleNamespace(provider=provider, semaphore=_new_semaphore())
        bg = _ProviderBgSession(sess)  # type: ignore[arg-type]

        sessions = SimpleNamespace(get_bg_session=AsyncMock(return_value=bg))

        with pytest.raises(RuntimeError, match="strict_model=True"):
            await run_bg_oneliner(sessions, "canary prompt", model="sonnet", strict_model=True)

        # destroy() must still run (via run_bg_oneliner's finally) and release
        # the semaphore even though the strict check raised.
        assert not sess.semaphore.locked()
