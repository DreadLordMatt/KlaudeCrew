"""Fork (KlaudeCrew): boot-time claude-backend readiness check.

``check_claude_backend_ready()`` is the loud, non-blocking equivalent of
kiro-cli's ``KiroPrerequisiteService`` for the claude path -- see
klaude/prerequisite.py and cli.md "First-run Kiro CLI prerequisite
onboarding".
"""

from __future__ import annotations

from unittest.mock import patch

from kiro_crew.klaude.prerequisite import check_claude_backend_ready


class TestCheckClaudeBackendReady:
    def test_both_resolve(self, caplog) -> None:
        with (
            patch("kiro_crew.acp.client._resolve_claude_acp_bin", return_value=["node", "x.js"]),
            patch("kiro_crew.acp.client._resolve_claude_code_executable", return_value="/bin/claude"),
        ):
            assert check_claude_backend_ready() is True
        assert "CRITICAL" not in caplog.text

    def test_adapter_missing_logs_critical_and_returns_false(self, caplog) -> None:
        with (
            patch("kiro_crew.acp.client._resolve_claude_acp_bin", return_value=None),
            patch("kiro_crew.acp.client._resolve_claude_code_executable", return_value="/bin/claude"),
        ):
            assert check_claude_backend_ready() is False
        assert "claude-agent-acp was not found" in caplog.text

    def test_claude_executable_missing_logs_critical_and_returns_false(self, caplog) -> None:
        with (
            patch("kiro_crew.acp.client._resolve_claude_acp_bin", return_value=["node", "x.js"]),
            patch("kiro_crew.acp.client._resolve_claude_code_executable", return_value=None),
        ):
            assert check_claude_backend_ready() is False
        assert "no `claude` executable was found" in caplog.text

    def test_resolver_exception_fails_soft(self, caplog) -> None:
        with (
            patch("kiro_crew.acp.client._resolve_claude_acp_bin", side_effect=RuntimeError("boom")),
            patch("kiro_crew.acp.client._resolve_claude_code_executable", return_value="/bin/claude"),
        ):
            assert check_claude_backend_ready() is False  # never raises
