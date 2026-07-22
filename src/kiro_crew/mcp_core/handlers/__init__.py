"""Per-family tool dispatchers for the kirocrew-core MCP server.

Each submodule exposes ``handle(name, args) -> str | object``. A dispatcher
returns the sentinel :data:`_UNHANDLED` when *name* is not one of its tools,
so ``mcp_core._call_tool_inner`` can try each family in order and fall
through — preserving the original flat ``if name == ...: return`` chain's
routing and early-return semantics.
"""

from __future__ import annotations

# Sentinel meaning "this dispatcher does not handle the given tool name".
# A module-level singleton so every family + the shim compare the SAME object.
_UNHANDLED = object()
