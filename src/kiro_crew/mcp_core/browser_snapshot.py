"""Browser-snapshot compression helpers for the browse_* MCP tools.

Split out of ``mcp_core`` (see that module's shim docstring). Pure text
transforms over a Playwright accessibility snapshot — no I/O, no siblings."""

from __future__ import annotations

import re as _re


def _compress_snapshot_to_outline(snapshot: str, max_lines: int = 100) -> str:
    """Compress a full accessibility snapshot into a compact outline.

    Keeps: headings, links, buttons, inputs, images with alt text, and
    structural landmarks. Strips: empty containers, decorative elements,
    redundant whitespace. Returns element refs so agent can interact
    without re-reading the full snapshot.
    """
    if not snapshot:
        return "Empty snapshot — page may not have loaded."

    lines = snapshot.split("\n")
    keep_patterns = _re.compile(
        r"(heading|link|button|textbox|combobox|checkbox|radio|tab|menu"
        r"|img|image|navigation|main|banner|contentinfo|search|alert"
        r"|dialog|listitem|row|cell|ref=)"
    )
    outline: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped == "-":
            continue
        if keep_patterns.search(stripped.lower()):
            indent = len(line) - len(line.lstrip())
            compact_indent = "  " * min(indent // 2, 4)
            outline.append(f"{compact_indent}{stripped}")
            if len(outline) >= max_lines:
                outline.append(f"... (truncated at {max_lines} lines)")
                break

    if not outline:
        total = len([ln for ln in lines if ln.strip()])
        return f"No interactive elements found in snapshot ({total} total lines). Try browser_snapshot with a more specific target."

    return f"Page outline ({len(outline)} elements):\n" + "\n".join(outline)


def _search_snapshot(snapshot: str, query: str, max_results: int = 50) -> str:
    """Search a snapshot for lines matching a query pattern."""
    if not snapshot:
        return "Empty snapshot."
    if not query:
        return "Error: query is required"

    try:
        pattern = _re.compile(query, _re.IGNORECASE)
    except _re.error:
        pattern = _re.compile(_re.escape(query), _re.IGNORECASE)

    lines = snapshot.split("\n")
    matches: list[str] = []
    for i, line in enumerate(lines, 1):
        if pattern.search(line):
            matches.append(f"L{i}: {line.strip()}")
            if len(matches) >= max_results:
                break

    if not matches:
        return f"No matches for '{query}' in snapshot ({len(lines)} lines)."

    return f"Found {len(matches)} matches:\n" + "\n".join(matches)
