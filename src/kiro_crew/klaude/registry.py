"""Fork (KlaudeCrew): the ProviderRegistry that re-enables the claude seam.

Wired in with a single swap in ``platform/bootstrap.py``
(``providers=KlaudeProviderRegistry()`` in place of the upstream
``DefaultProviderRegistry()``). ``create_factory`` stays pure identity --
backend selection itself is config-driven inside
``KiroCrewConfig.create_provider_factory()`` (see ``config/loader.py``), not
this seam. ``register_acp_backends`` is the one real override: it attaches the hook
methods ``AcpClient`` already looks up via ``getattr`` at spawn/cleanup time
onto the class, exactly as ``platform/defaults.py``'s own docstring for
``DefaultProviderRegistry`` describes an "internal companion" doing:

- ``_claude_session_mcp_servers`` -> ``mcp_servers.claude_session_servers``
- ``_write_claude_local_settings`` -> ``settings_seed.write_claude_local_settings``
- ``_claude_session_transcript_exists`` -> ``transcripts.claude_transcript_exists``
- ``_claude_cleanup_transcript`` -> ``transcripts.delete_claude_transcript``
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.klaude.mcp_servers import claude_session_servers
from kiro_crew.klaude.settings_seed import write_claude_local_settings
from kiro_crew.klaude.transcripts import claude_transcript_exists, delete_claude_transcript

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)


def _claude_session_mcp_servers(self: Any) -> list:
    return claude_session_servers(self._agent)


def _claude_session_transcript_exists(self: Any, session_id: str) -> bool:
    return claude_transcript_exists(self._work_dir, session_id)


def _claude_cleanup_transcript(self: Any, session_id: str) -> None:
    delete_claude_transcript(self._work_dir, session_id)


class KlaudeProviderRegistry:
    """See module docstring."""

    def create_factory(self, cfg: "KiroCrewConfig") -> Callable[..., Any]:
        return cfg.create_provider_factory()

    def register_acp_backends(self) -> None:
        from kiro_crew.acp.client import AcpClient

        # Idempotent: bootstrap can run more than once in a process (tests,
        # gateway restart-in-place) and re-attaching the same function is
        # harmless, but guard anyway so a future caller that checks "is this
        # already registered" has something to check.
        AcpClient._claude_session_mcp_servers = _claude_session_mcp_servers
        AcpClient._write_claude_local_settings = write_claude_local_settings
        AcpClient._claude_session_transcript_exists = _claude_session_transcript_exists
        AcpClient._claude_cleanup_transcript = _claude_cleanup_transcript
        logger.info("klaude: claude ACP backend registration glue attached")
