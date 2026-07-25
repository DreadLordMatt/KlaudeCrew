"""_send_prompt must leave `[attached_dir N]` folder paths untouched.

Image attachments are extracted from the prompt text and inlined as base64
`image` content blocks. Folder references are plain paths with no image
extension and are not files, so they must survive verbatim in the text block —
otherwise the agent would receive a mangled path it cannot explore.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.client import AcpClient


async def _captured_prompt(message: str) -> list[dict]:
    """Run _send_prompt with the transport stubbed; return the prompt blocks."""
    client = AcpClient()
    client._session_id = "s1"
    client._send_request = AsyncMock(return_value=1)
    await client._send_prompt(message)
    return client._send_request.call_args[0][1]["prompt"]


def _text_of(prompt: list[dict]) -> str:
    return next(b["text"] for b in prompt if b["type"] == "text")


class TestSendPromptDirPaths:
    @pytest.mark.asyncio
    async def test_dir_marker_survives_verbatim(self, tmp_path):
        d = tmp_path / "pages"
        d.mkdir()
        msg = f"review [attached_dir 1] {d}"
        prompt = await _captured_prompt(msg)
        assert _text_of(prompt) == msg
        assert not [b for b in prompt if b["type"] == "image"]

    @pytest.mark.asyncio
    async def test_dir_named_like_an_image_is_not_inlined(self, tmp_path):
        """A DIRECTORY whose name ends in .png must not be read as an image.

        The path regex matches the extension, so the is_file() guard is the only
        thing preventing an attempt to base64 a directory.
        """
        d = tmp_path / "screenshots.png"
        d.mkdir()
        msg = f"see [attached_dir 1] {d}"
        prompt = await _captured_prompt(msg)
        assert _text_of(prompt) == msg
        assert str(d) in _text_of(prompt)
        assert not [b for b in prompt if b["type"] == "image"]

    @pytest.mark.asyncio
    async def test_dir_path_with_spaces_survives(self, tmp_path):
        d = tmp_path / "my docs"
        d.mkdir()
        msg = f"check [attached_dir 1] {d} please"
        prompt = await _captured_prompt(msg)
        assert _text_of(prompt) == msg

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "_send_prompt's image path regex is anchored to a leading '/', so it "
            "never matches a native Windows path. That POSIX-only extraction "
            "predates folder support; this test asserts a dir reference coexists "
            "with an inlined image, which requires extraction to fire."
        ),
    )
    @pytest.mark.asyncio
    async def test_image_still_inlined_alongside_a_dir_reference(self, tmp_path):
        d = tmp_path / "pages"
        d.mkdir()
        img = tmp_path / "shot.png"
        # 1x1 PNG header bytes are enough: the client only base64s the content.
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        msg = f"look at {img} and [attached_dir 1] {d}"
        prompt = await _captured_prompt(msg)
        images = [b for b in prompt if b["type"] == "image"]
        assert len(images) == 1
        assert images[0]["mimeType"] == "image/png"
        text = _text_of(prompt)
        # The image path is replaced by a placeholder; the dir path is not.
        assert "[image: shot.png]" in text
        assert str(d) in text
        assert str(img) not in text

    @pytest.mark.asyncio
    async def test_file_marker_unchanged_by_dir_support(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("a,b\n")
        msg = f"read [attached_file 1] {f}"
        prompt = await _captured_prompt(msg)
        assert _text_of(prompt) == msg
        assert not [b for b in prompt if b["type"] == "image"]
