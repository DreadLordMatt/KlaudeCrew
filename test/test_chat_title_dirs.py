"""Tests for folder-reference stripping in auto-title text (chat_title)."""

from __future__ import annotations

from kiro_crew.dashboard.chat_title import (
    _TITLE_MAX_ATTACHMENT_FILES,
    _TITLE_MAX_ATTACHMENT_PATH_LENGTH,
    _TITLE_SOURCE_SCAN_LIMIT,
    _TITLE_TEXT_LIMIT,
    _build_title_prompt,
    _fallback_title_from_messages,
    _message_dir_paths,
    _title_text,
)

DIR = "/repo/src/pages"
FILE = "/repo/data.csv"


class TestTitleTextDirTokens:
    def test_strips_standalone_dir_token(self):
        out = _title_text(f"[attached_dir 1] {DIR}", (), (DIR,))
        assert "attached_dir" not in out
        assert DIR not in out
        # The basename survives: an attachment-only message would otherwise have
        # no content left for the titler to name.
        assert out == "pages"

    def test_keeps_surrounding_user_text(self):
        out = _title_text(f"review [attached_dir 1] {DIR} today", (), (DIR,))
        assert "attached_dir" not in out
        assert out == "review pages today"

    def test_strips_dir_token_without_metadata(self):
        """History replay: no meta, so the whitespace fallback must handle it."""
        out = _title_text(f"review [attached_dir 1] {DIR} today")
        assert "attached_dir" not in out
        assert DIR not in out
        assert out == "review pages today"

    def test_strips_both_marker_families(self):
        content = f"look at [attached_file 1] {FILE} and [attached_dir 1] {DIR} please"
        out = _title_text(content, (FILE,), (DIR,))
        assert "attached_file" not in out
        assert "attached_dir" not in out
        assert FILE not in out
        assert DIR not in out
        assert out == "look at data.csv and pages please"

    def test_same_numbered_markers_resolve_against_own_lists(self):
        """[attached_file 1] and [attached_dir 1] index different tuples."""
        content = f"[attached_file 1] {FILE} [attached_dir 1] {DIR} end"
        out = _title_text(content, (FILE,), (DIR,))
        assert out == "data.csv pages end"

    def test_strips_dir_path_containing_spaces(self):
        spaced = "/repo/my docs"
        out = _title_text(f"check [attached_dir 1] {spaced} now", (), (spaced,))
        assert "attached_dir" not in out
        assert "/repo/my docs" not in out
        # The basename keeps its internal space; only the parent path is dropped.
        assert out == "check my docs now"

    def test_file_only_message_unaffected(self):
        out = _title_text(f"see [attached_file 1] {FILE} ok", (FILE,))
        assert "attached_file" not in out
        assert out == "see data.csv ok"

    def test_plain_text_unaffected(self):
        assert _title_text("just a normal message") == "just a normal message"

    def test_unknown_dir_index_falls_back_to_whitespace_capture(self):
        """A token numbered beyond the tuple still gets stripped."""
        out = _title_text(f"a [attached_dir 9] {DIR} b", (), (DIR,))
        assert "attached_dir" not in out
        assert DIR not in out


class TestMessageDirPaths:
    def test_reads_meta_dirs_in_order(self):
        msg = {"meta": {"dirs": [DIR, "/repo/docs"]}}
        assert _message_dir_paths(msg) == (DIR, "/repo/docs")

    def test_missing_meta_returns_empty(self):
        assert _message_dir_paths({}) == ()

    def test_non_dict_meta_returns_empty(self):
        assert _message_dir_paths({"meta": "nope"}) == ()

    def test_non_list_dirs_returns_empty(self):
        assert _message_dir_paths({"meta": {"dirs": "nope"}}) == ()

    def test_caps_the_number_of_dirs(self):
        many = [f"/repo/d{i}" for i in range(_TITLE_MAX_ATTACHMENT_FILES + 5)]
        assert len(_message_dir_paths({"meta": {"dirs": many}})) == _TITLE_MAX_ATTACHMENT_FILES

    def test_blanks_overlong_paths_preserving_index(self):
        long_path = "/repo/" + "x" * (_TITLE_MAX_ATTACHMENT_PATH_LENGTH + 10)
        got = _message_dir_paths({"meta": {"dirs": [long_path, DIR]}})
        assert got == ("", DIR), "index order must be preserved so token N still resolves"

    def test_blanks_non_string_entries(self):
        assert _message_dir_paths({"meta": {"dirs": [None, DIR]}}) == ("", DIR)


class TestTitlePromptWithDirs:
    def test_prompt_body_has_no_dir_marker(self):
        msgs = [
            {
                "role": "user",
                "content": f"summarize [attached_dir 1] {DIR} for me",
                "meta": {"dirs": [DIR]},
            },
        ]
        prompt = _build_title_prompt(msgs)
        assert prompt is not None
        assert "attached_dir" not in prompt
        assert DIR not in prompt
        assert "summarize" in prompt

    def test_dir_only_message_yields_the_folder_name(self):
        """A folder-only message is titled from the folder name.

        It previously produced no transcript at all, so the titler had nothing to
        name and answered SKIP.
        """
        msgs = [
            {"role": "user", "content": f"[attached_dir 1] {DIR}", "meta": {"dirs": [DIR]}},
        ]
        prompt = _build_title_prompt(msgs)
        assert prompt is not None
        assert "user: pages" in prompt
        assert DIR not in prompt


class TestTitleScanBudgetCoversBothFamilies:
    def test_scan_limit_budgets_for_two_families(self):
        """Each family gets its own 20-entry allowance, so the budget covers both.

        A one-family budget would let a message carrying long file AND folder
        paths be truncated before its user text.
        """
        per_family = _TITLE_MAX_ATTACHMENT_FILES * (_TITLE_MAX_ATTACHMENT_PATH_LENGTH + 32)
        assert _TITLE_SOURCE_SCAN_LIMIT >= _TITLE_TEXT_LIMIT + 2 * per_family

    def test_user_text_survives_a_full_pair_of_attachment_families(self):
        """Max-length paths in both families must not crowd out the user text."""
        long_dirs = [f"/d{i}/" + "x" * 3_000 for i in range(_TITLE_MAX_ATTACHMENT_FILES)]
        long_files = [f"/f{i}/" + "y" * 3_000 for i in range(_TITLE_MAX_ATTACHMENT_FILES)]
        tokens = "\n".join(
            [f"[attached_file {i + 1}] {p}" for i, p in enumerate(long_files)]
            + [f"[attached_dir {i + 1}] {p}" for i, p in enumerate(long_dirs)]
        )
        content = f"{tokens}\nplease summarize this"
        text = _title_text(content, tuple(long_files), tuple(long_dirs))
        assert "please summarize this" in text
        assert "attached_file" not in text
        assert "attached_dir" not in text
        # Full paths are still excluded; only basenames are substituted.
        assert long_dirs[0] not in text
        assert long_files[0] not in text

    def test_a_path_containing_the_other_family_marker_is_not_mangled(self):
        """Marker families must not rescan each other's substituted output.

        Each pass replaces its marker+path with the attachment's BASENAME. If a
        basename itself contains the literal text of the other family's marker,
        the second pass scans the first pass's output, finds that substring, and
        mangles it — the same rescan defect the send-path serializer had.

        A directory name may legitimately contain `[`, `]` and spaces on both
        POSIX and Windows, so this is reachable from a real filesystem.
        """
        weird_file = "/repo/[attached_dir 1] notes"
        content = f"[attached_file 1] {weird_file} summarize this"
        text = _title_text(content, (weird_file,), ())
        assert "summarize this" in text, "the user's text must survive"
        # The file pass substitutes the basename "[attached_dir 1] notes". The
        # folder pass must NOT then treat that substituted text as one of its own
        # markers and eat part of it. Asserting only the absence of
        # "attached_dir" would pass for the wrong reason — the mangling itself
        # removes the substring — so assert the label survives whole.
        assert "[attached_dir 1] notes" in text, (
            "the folder pass rescanned and mangled the file pass's substituted basename"
        )
        assert "attached_file" not in text


class TestFallbackTitleWithDirs:
    def test_fallback_title_strips_the_folder_marker(self):
        """The fallback path (LLM titler unavailable) must strip dir markers too.

        It previously passed only the file paths, so a folder mention surfaced
        raw as "Tell me about [attached_dir 1] /repo/docs" in the chat title.
        """
        msgs = [
            {
                "role": "user",
                "content": f"Tell me about [attached_dir 1] {DIR}",
                "meta": {"dirs": [DIR]},
            },
        ]
        title = _fallback_title_from_messages(msgs)
        assert "attached_dir" not in title
        assert DIR not in title
        assert title.startswith("Tell me about")

    def test_fallback_title_strips_both_families(self):
        msgs = [
            {
                "role": "user",
                "content": f"compare [attached_file 1] {FILE} with [attached_dir 1] {DIR}",
                "meta": {"files": [FILE], "dirs": [DIR]},
            },
        ]
        title = _fallback_title_from_messages(msgs)
        assert "attached_file" not in title
        assert "attached_dir" not in title
        assert "compare" in title

    def test_fallback_title_strips_a_spaced_folder_path(self):
        spaced = "/repo/my docs"
        msgs = [
            {
                "role": "user",
                "content": f"summarize [attached_dir 1] {spaced} please",
                "meta": {"dirs": [spaced]},
            },
        ]
        title = _fallback_title_from_messages(msgs)
        assert "attached_dir" not in title
        assert spaced not in title
        assert "summarize" in title
        # The fallback drops attachment names entirely (only the LLM prompt keeps
        # them), so no part of the spaced path may survive. Without the ordered
        # dir paths the whitespace fallback would stop at the space and leak the
        # tail "docs" into the title.
        assert "docs" not in title, "the spaced path tail leaked into the title"


class TestAttachmentLabelBudget:
    def test_many_folder_labels_do_not_crowd_out_the_caption(self):
        """The shared label budget protects the user's own text.

        Substituting a name per attachment is only safe while the total stays
        bounded: the transcript line handed to the titler is itself capped, so
        20 long folder names would push the caption out entirely.
        """
        dirs = [f"/repo/{'d' * 30}{i}" for i in range(20)]
        content = (
            " ".join(f"[attached_dir {i + 1}] {p}" for i, p in enumerate(dirs))
            + " please summarize these"
        )
        text = _title_text(content, (), tuple(dirs))
        assert "please summarize these" in text
        assert "attached_dir" not in text
        assert len(text) < 400, "labels consumed an unbounded share of the line"

    def test_colliding_folder_names_are_disambiguated(self):
        """Three folders all named docs must not read as "docs and docs and docs"."""
        dirs = ["/repo/docs", "/repo/website/docs", "/repo/src/kiro_crew/docs"]
        content = (
            f"tell me about [attached_dir 1] {dirs[0]} and [attached_dir 2] {dirs[1]}"
            f" and [attached_dir 3] {dirs[2]}"
        )
        text = _title_text(content, (), tuple(dirs))
        assert "repo/docs" in text
        assert "website/docs" in text
        assert "kiro_crew/docs" in text
