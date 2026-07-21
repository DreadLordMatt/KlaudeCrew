"""Auto-research grill question-tree helpers.

Grows a first-principles clarifier/research question tree via a fast LLM pool.
Near-leaf module (stdlib only); redaction of the produced nodes is applied by
the HTTP handler that calls these.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# --- Grill question tree ---
# Node JSON contract (see grill-question-tree-design.md):
#   { id, parent|null, kind: "root"|"clarifier"|"research", text,
#     recommended (clarifier only), answer (clarifier only),
#     origin: "grill"|"emergent" (research only), status }
_MAX_GRILL_DEPTH = 4  # a node at this depth can no longer be expanded
_GRILL_CHILD_CAP = 5  # max children returned per expand


def _new_node_id() -> str:
    return "n" + uuid.uuid4().hex[:8]


def _node_depth(tree: list[dict], node_id: str) -> int:
    """Depth of node_id (root=0). Returns -1 if node_id is not in the tree."""
    by_id = {n["id"]: n for n in tree if isinstance(n, dict) and "id" in n}
    if node_id not in by_id:
        return -1
    depth = 0
    seen: set = set()
    cur: dict | None = by_id[node_id]
    while cur is not None and cur.get("parent") and cur["id"] not in seen:
        seen.add(cur["id"])
        depth += 1
        cur = by_id.get(cur["parent"])
    return depth


_GRILL_EXPAND_PROMPT = (
    "You are helping a user scope a research campaign by growing a question tree. "
    "Reason from FIRST PRINCIPLES. Given the main question, the tree so far, and the "
    "target node to expand, propose at most 5 children — the highest-value next nodes. "
    "Each child is either:\n"
    '  - "clarifier": a DECISION question to ask the user — something that narrows '
    "scope or surfaces an unknown they may not have considered. These must be genuine "
    "decisions only the user can make, NOT facts discoverable by exploring code/docs/"
    'tools. Include a "recommended" best-guess answer.\n'
    '  - "research": a well-formed, distinct sub-question the campaign should '
    "investigate (use only when it is already a concrete research target).\n"
    "Rules:\n"
    "- Distinct, non-overlapping angles; no generic restatements.\n"
    "- Never propose a clarifier for something the agent could look up itself "
    "(codebase structure, API signatures, existing config, prior decisions in the tree).\n"
    "- Each clarifier should be ONE focused question — asking multiple things in one "
    "node is bewildering and produces shallow answers.\n"
    "Output ONLY a JSON "
    'array like [{"kind":"clarifier","text":"...","recommended":"..."},'
    '{"kind":"research","text":"..."}].'
)


def _compact_tree(tree: list[dict]) -> str:
    """One line per node (id/kind/text + answer) as LLM context."""
    lines = []
    for n in tree:
        if not isinstance(n, dict):
            continue
        line = f"- [{n.get('id', '?')}] {n.get('kind', '?')}: {n.get('text', '')}"
        if n.get("answer"):
            line += f" → answered: {n['answer']}"
        lines.append(line)
    return "\n".join(lines) if lines else "(empty — this is the first round)"


def _parse_grill_nodes(raw: str) -> list[dict]:
    """Extract child node dicts {kind, text, recommended?} from an LLM reply."""
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text", "")).strip()
        kind = it.get("kind")
        if not text or kind not in ("clarifier", "research"):
            continue
        node = {"kind": kind, "text": text}
        if kind == "clarifier":
            node["recommended"] = str(it.get("recommended", "")).strip()
        out.append(node)
    return out


async def _grill_expand_children(
    pool: Any, question: str, tree: list[dict], node_id: str | None
) -> list[dict]:
    """Return raw child dicts {kind, text, recommended?} for the target node.

    Uses the dedicated auto_research_llm_pool (CC worker is haiku-backed — the
    fast model the grill wants); empty-on-failure so the UI degrades gracefully.
    """
    if pool is None:
        return []
    target = "the root question (propose the first round of children)"
    if node_id is not None:
        node = next((n for n in tree if isinstance(n, dict) and n.get("id") == node_id), None)
        if node:
            target = f"[{node_id}] {node.get('kind')}: {node.get('text', '')}"
            ans = node.get("answer") or node.get("recommended")
            if ans:
                target += f" (answer: {ans})"
    prompt = (
        f"{_GRILL_EXPAND_PROMPT}\n\nMain question: {question}\n\n"
        f"Tree so far:\n{_compact_tree(tree)}\n\nExpand: {target}"
    )
    try:
        raw = await pool.send(prompt, timeout=18.0)
    except Exception as exc:
        logger.warning("auto_research grill expand failed: %s", exc)
        return []
    return _parse_grill_nodes(raw)
