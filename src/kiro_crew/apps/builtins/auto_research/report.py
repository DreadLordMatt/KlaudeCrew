"""Auto-research report helpers: LLM report prompt, mechanical HTML fallback
render, and the cumulative FINDINGS.md reader.

Depends only on ``files`` (for the path-safe campaign dir).
"""

from __future__ import annotations

import html as html_mod

from kiro_crew.apps.builtins.auto_research.files import _safe_campaign_dir

_REPORT_TIMEOUT = 90.0


def _read_report(campaign_id: str) -> str:
    """Read the agent's cumulative FINDINGS.md report (empty if none yet)."""
    d = _safe_campaign_dir(campaign_id)
    if not d:
        return ""
    p = d / "FINDINGS.md"
    try:
        return p.read_text() if p.exists() else ""
    except OSError:
        return ""


def _build_report_prompt(question: str, subs: list, findings_md: str, total_cycles: int) -> str:
    """Prompt the LLM to author a polished, self-contained HTML report."""
    sub_lines = []
    for s in subs:
        if isinstance(s, dict):
            st = "answered" if s.get("status") == "answered" else "open"
            sub_lines.append(f"- [{st}] {s.get('text', '')}")
        else:
            sub_lines.append(f"- {s}")
    subs_block = "\n".join(sub_lines) if sub_lines else "(none)"
    return (
        "You are formatting a completed research campaign into a polished, "
        "self-contained HTML report for sharing.\n\n"
        f"# Research question\n{question}\n\n"
        f"# Sub-questions\n{subs_block}\n\n"
        f"# Cycles run\n{total_cycles}\n\n"
        f"# Findings (markdown, authored during research)\n{findings_md}\n\n"
        "Produce a SINGLE self-contained HTML document (no external assets) that "
        "presents this research clearly and attractively:\n"
        "- A header with the question and a one-paragraph executive summary you synthesize.\n"
        "- A 'Key findings' section highlighting the most important, well-evidenced points.\n"
        "- A 'Sub-questions' section showing which were answered vs still open.\n"
        "- Preserve any source citations / links present in the findings.\n"
        "- Use clean, modern inline CSS (system font, readable ~800px width, light theme).\n"
        "- Do NOT invent facts that are not present in the findings.\n"
        "Output ONLY the raw HTML document, starting with <!DOCTYPE html>. "
        "Do not wrap it in markdown code fences."
    )


def _render_findings_html(
    question: str, subs: list, findings_md: str, total_cycles: int, cid: str
) -> str:
    """Render campaign findings into a self-contained HTML document."""
    q = html_mod.escape(question)
    sub_items = ""
    for s in subs:
        text = html_mod.escape(s.get("text", "") if isinstance(s, dict) else str(s))
        origin = html_mod.escape(s.get("origin", "grill") if isinstance(s, dict) else "grill")
        status = s.get("status", "open") if isinstance(s, dict) else "open"
        icon = "✅" if status == "answered" else "🔍"
        sub_items += f"<li>{icon} {text} <em>({origin})</em></li>\n"
    # Convert markdown to basic HTML (just escape and preserve structure)
    body_html = html_mod.escape(findings_md).replace("\n\n", "</p><p>").replace("\n", "<br>")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Research: {q}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 2em auto; padding: 0 1em; line-height: 1.6; color: #1a1a1a; }}
h1 {{ font-size: 1.4em; }}
h2 {{ font-size: 1.1em; margin-top: 1.5em; border-bottom: 1px solid #eee; padding-bottom: 0.3em; }}
.meta {{ color: #666; font-size: 0.85em; }}
ul {{ padding-left: 1.5em; }}
li {{ margin: 0.3em 0; }}
.findings {{ background: #f9f9f9; padding: 1em; border-radius: 6px; margin-top: 1em; }}
p {{ margin: 0.5em 0; }}
</style></head><body>
<h1>🔬 {q}</h1>
<div class="meta">{total_cycles} cycles · Campaign {html_mod.escape(cid)}</div>
<h2>Sub-questions</h2>
<ul>{sub_items}</ul>
<h2>Findings</h2>
<div class="findings"><p>{body_html}</p></div>
</body></html>"""
