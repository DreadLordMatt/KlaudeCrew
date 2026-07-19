"""Tests for kiro_crew.acp.prompt_content.build_prompt_content.

Regression coverage for the multi-image inlining bug: the previous regex
included ``\\s`` in its character class, so multiple newline-separated image
paths were greedily coalesced into one invalid path and every image was
dropped. These tests pin the behavior for both producers (dashboard
"![image](path)" lines and Slack "\\n".join(paths)), SVG support, and the
non-image / missing-file passthrough.
"""

from __future__ import annotations

import base64

from kiro_crew.acp.prompt_content import build_prompt_content

# 1x1 PNG (smallest valid), reused as raw bytes for on-disk fixtures.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"/>'


def _write(path, data=_PNG_BYTES):
    path.write_bytes(data)
    return str(path)


def _images(content):
    return [b for b in content if b.get("type") == "image"]


def _text(content):
    assert content[0]["type"] == "text", "text block must be first"
    return content[0]["text"]


def test_single_image_inlined(tmp_path):
    p = _write(tmp_path / "a.png")
    content = build_prompt_content(f"look at ![image]({p})")
    imgs = _images(content)
    assert len(imgs) == 1
    assert imgs[0]["mimeType"] == "image/png"
    assert imgs[0]["data"] == base64.b64encode(_PNG_BYTES).decode()
    assert "[image: a.png]" in _text(content)
    assert p not in _text(content)


def test_multiple_images_dashboard_markdown(tmp_path):
    """Dashboard emits '![image](path)' lines joined by newlines."""
    paths = [_write(tmp_path / f"img{i}.png") for i in range(3)]
    msg = "\n".join(f"![image]({p})" for p in paths) + "\n\nwhat are these?"
    content = build_prompt_content(msg)
    assert len(_images(content)) == 3
    assert "what are these?" in _text(content)


def test_multiple_images_bare_newline_paths(tmp_path):
    """Slack path: bare absolute paths joined by '\\n' (the regression case).

    The old '\\s'-in-class regex merged these into one match that failed
    is_file(), dropping every image. All must now be inlined.
    """
    paths = [_write(tmp_path / f"s{i}.png") for i in range(3)]
    content = build_prompt_content("\n".join(paths))
    assert len(_images(content)) == 3


def test_svg_is_inlined(tmp_path):
    p = _write(tmp_path / "logo.svg", _SVG_BYTES)
    content = build_prompt_content(f"![image]({p})")
    imgs = _images(content)
    assert len(imgs) == 1
    assert imgs[0]["mimeType"] == "image/svg+xml"


def test_path_with_space_still_matches(tmp_path):
    d = tmp_path / "My Screenshots"
    d.mkdir()
    p = _write(d / "shot.png")
    content = build_prompt_content(f"![image]({p})")
    assert len(_images(content)) == 1


def test_missing_file_left_in_text(tmp_path):
    missing = str(tmp_path / "nope.png")
    content = build_prompt_content(f"see {missing}")
    assert _images(content) == []
    assert missing in _text(content)


def test_no_images_returns_single_text_block():
    content = build_prompt_content("just text, no attachments")
    assert content == [{"type": "text", "text": "just text, no attachments"}]


def test_mixed_existing_and_missing(tmp_path):
    good = _write(tmp_path / "good.png")
    missing = str(tmp_path / "gone.jpg")
    content = build_prompt_content(f"![image]({good})\n![image]({missing})")
    assert len(_images(content)) == 1
    assert "[image: good.png]" in _text(content)
    assert missing in _text(content)
