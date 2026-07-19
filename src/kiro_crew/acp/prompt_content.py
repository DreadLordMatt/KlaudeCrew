"""Shared ACP prompt-content builder.

Both ACP send paths must turn a user-message string into the ACP ``prompt``
content array, inlining any referenced local image files as base64 image
blocks so the model actually receives every attached image:

* ``AcpClient._send_prompt`` — the standalone client / worker-pool (and the
  Claude-Code backend).
* ``AcpSessionHandle.prompt`` — the shared-runtime kiro backend, which is the
  path the **dashboard** actually uses.

Previously only the client path inlined images; the dashboard runtime path
sent a single ``{"type": "text"}`` block, so uploaded images never reached the
model as image content (kiro-cli only ever saw the raw ``![image](path)``
markdown text). Centralizing the logic here guarantees both paths behave
identically.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# Absolute image-path matcher.
#
# The character class allows a literal space (so paths like
# "/a/My Screenshots/x.png" still match) but deliberately NOT arbitrary
# whitespace. The earlier pattern used ``\s`` inside the class, which — with
# the greedy ``+`` — let a single match bridge across multiple
# newline-separated paths, coalescing e.g. "/a/one.png\n/a/two.png" into ONE
# capture. ``Path(...).is_file()`` was then False, so EVERY image was silently
# dropped whenever more than one was attached (only a lone image survived).
# Since both producers emit one path per line — the dashboard as
# "![image](path)" lines and Slack as "\n".join(image_paths) — forbidding
# newlines in the class keeps multi-image uploads intact.
#
# ``svg`` is included in the alternation so SVG uploads (already accepted by
# the uploader and present in IMAGE_EXTENSIONS) are actually inlined.
_IMAGE_PATH_RE = re.compile(
    r"(/[\w./@~ ()\-]+\.(?:png|jpe?g|gif|webp|bmp|svg))",
    re.IGNORECASE,
)


def build_prompt_content(message: str) -> list[dict]:
    """Turn a message string into an ACP ``prompt`` content array.

    Every referenced local image file is read, base64-encoded, and appended as
    a separate ``{"type": "image", "data", "mimeType"}`` block; its path in the
    text is replaced with a ``[image: <name>]`` placeholder. Matches that are
    not existing files, have an unsupported suffix, or cannot be read are left
    untouched in the text (never dropped silently).

    Returns ``[{"type": "text", "text": <remaining>}, <image blocks...>]`` — the
    text block is always first, matching the prior AcpClient behavior.
    """
    images: list[dict] = []
    remaining = message
    for match in _IMAGE_PATH_RE.finditer(message):
        raw = match.group(1)
        p = Path(raw.strip())
        suffix = p.suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            continue
        if not p.is_file():
            continue
        try:
            data = base64.b64encode(p.read_bytes()).decode()
        except Exception:
            # Unreadable file: leave the path in the text, skip the block.
            logger.debug("build_prompt_content: unreadable image %s", p, exc_info=True)
            continue
        media = IMAGE_MEDIA_TYPES.get(suffix, "image/png")
        images.append({"type": "image", "data": data, "mimeType": media})
        remaining = remaining.replace(raw, f"[image: {p.name}]")

    return [{"type": "text", "text": remaining}, *images]
