"""Fork (KlaudeCrew): the Claude Code ACP-backend registration glue.

Upstream KiroCrew ships an inert ``ACP_BACKEND_CLAUDE`` seam in
``acp/client.py`` — every protocol divergence between kiro-cli and
``claude-agent-acp`` is already handled there, but two things are left for
"an internal companion" to supply: the MCP-server array claude-agent-acp
needs handed to it directly (it reads no config file — see
``mcp_servers.py``), and the per-session ``settings.local.json`` seed that
routes its tool-permission decisions back through KiroCrew (see
``settings_seed.py``).

This package IS that companion, wired in via the sanctioned
``ProviderRegistry`` extension point (``registry.py`` -> one-line swap in
``platform/bootstrap.py``) rather than editing ``acp/client.py`` itself, so
the fork's core-file diff against upstream stays a handful of isolated hunks.
"""
