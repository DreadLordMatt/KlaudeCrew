"""Tests for the hardened api_file_diff handler in dashboard/handlers/git_changes.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers.git_changes import api_file_diff

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX-only scenario")

_MOD = "kiro_crew.dashboard.handlers.git_changes"


def _req(path: str = "", lexical: bool = False) -> make_mocked_request:
    """Create a mocked GET request with ?path= query param."""
    url = f"/api/file-diff?path={path}" if path else "/api/file-diff"
    if lexical:
        url += "&lexical=1"
    return make_mocked_request("GET", url)


def _mock_sel():
    sel = MagicMock()
    sel.log_api_access = MagicMock()
    return sel


def _init_repo(path, commit: bool = False):
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)
    if commit:
        (path / ".init-marker").write_text("x")
        subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)


async def _call(path: str = "", lexical: bool = False) -> tuple[int, dict]:
    with patch(f"{_MOD}._sel", return_value=_mock_sel()):
        resp = await api_file_diff(_req(path, lexical=lexical))
    return resp.status, json.loads(resp.body)


@pytest.mark.asyncio
async def test_empty_path_returns_empty():
    """No path param returns empty diff and original."""
    status, body = await _call("")
    assert status == 200
    assert body == {"diff": "", "original": ""}


@pytest.mark.asyncio
@requires_git
async def test_nonexistent_file_outside_repo_returns_not_git(tmp_path):
    """A missing file whose ancestors are not a git repo degrades to not_git."""
    status, body = await _call(str(tmp_path / "nonexistent_abc123.txt"))
    assert status == 200
    assert body["diff"] == ""
    assert body["original"] == ""


@pytest.mark.asyncio
async def test_sensitive_path_returns_403():
    """Paths rejected by the central hooks validator get a 403."""
    with (
        patch("kiro_crew.hooks.validate_file_path", return_value=None),
        patch(f"{_MOD}._sel", return_value=_mock_sel()),
    ):
        resp = await api_file_diff(_req("/home/user/.ssh/id_rsa"))
    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "Access denied"


@pytest.mark.asyncio
@requires_git
async def test_file_not_in_git_repo(tmp_path):
    """File outside a git repo returns not_git status."""
    f = tmp_path / "standalone.txt"
    f.write_text("hello")
    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "not_git"
    assert body["diff"] == ""
    assert body["original"] == ""


@pytest.mark.asyncio
@requires_git
async def test_clean_file_in_git_repo(tmp_path):
    """Committed file with no changes returns clean status."""
    _init_repo(tmp_path)
    f = tmp_path / "clean.txt"
    f.write_text("original content")
    subprocess.run(["git", "add", "clean.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)

    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "clean"
    assert body["diff"] == ""
    assert body["original"] == "original content"


@pytest.mark.asyncio
@requires_git
async def test_modified_file_in_git_repo(tmp_path):
    """Modified file returns diff and original content."""
    _init_repo(tmp_path)
    f = tmp_path / "modified.txt"
    f.write_text("original")
    subprocess.run(["git", "add", "modified.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text("modified content")

    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "modified"
    assert "modified content" in body["diff"]
    assert body["original"] == "original"


@pytest.mark.asyncio
@requires_git
@posix_only  # untracked-diff synthesis needs fd-path containment (POSIX-only)
async def test_untracked_file_synthesized_diff(tmp_path):
    """Untracked file returns an all-added synthesized patch."""
    _init_repo(tmp_path, commit=True)
    f = tmp_path / "untracked.txt"
    f.write_text("new file content\n")

    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "untracked"
    assert "+new file content" in body["diff"]
    assert body["original"] == ""


@pytest.mark.asyncio
@requires_git
async def test_deleted_tracked_file_returns_deletion_patch(tmp_path):
    """A tracked-but-deleted path serves its deletion patch and HEAD content."""
    _init_repo(tmp_path)
    f = tmp_path / "gone.txt"
    # write_bytes: Path.write_text translates \n -> \r\n on Windows, breaking
    # the byte-exact `original` assertion below.
    f.write_bytes(b"doomed line\n")
    subprocess.run(["git", "add", "gone.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.unlink()

    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "deleted"
    assert "-doomed line" in body["diff"]
    assert body["original"] == "doomed line\n"


@pytest.mark.asyncio
@requires_git
@posix_only  # replacement synthesis needs fd-path containment (POSIX-only)
async def test_staged_delete_then_recreate_synthesizes_replacement(tmp_path):
    """'D ' + '??' for one path: base->current patch, not a bare deletion."""
    _init_repo(tmp_path)
    f = tmp_path / "swap.txt"
    f.write_text("old body\n")
    subprocess.run(["git", "add", "swap.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "rm", "swap.txt"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text("replacement body\n")

    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "modified"
    assert "-old body" in body["diff"]
    assert "+replacement body" in body["diff"]


@pytest.mark.asyncio
@requires_git
async def test_staged_only_change_shows_cached_patch(tmp_path):
    """Index-only change (staged edit, worktree restored) is not 'clean'."""
    _init_repo(tmp_path)
    f = tmp_path / "staged.txt"
    f.write_text("base\n")
    subprocess.run(["git", "add", "staged.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text("staged edit\n")
    subprocess.run(["git", "add", "staged.txt"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text("base\n")  # restore worktree to base

    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "modified"
    assert "+staged edit" in body["diff"]


@pytest.mark.asyncio
@requires_git
async def test_unborn_head_staged_file_diffs_as_added(tmp_path):
    """Before the first commit, a staged file diffs against the empty tree."""
    _init_repo(tmp_path)
    f = tmp_path / "first.txt"
    f.write_text("first line\n")
    subprocess.run(["git", "add", "first.txt"], cwd=tmp_path, capture_output=True, check=True)

    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "modified"
    assert "+first line" in body["diff"]


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_lexical_symlink_diffs_link_entry_not_target(tmp_path):
    """?lexical=1 diffs a changed symlink as the link, not its target."""
    _init_repo(tmp_path)
    target_a = tmp_path / "a.txt"
    target_a.write_text("A\n")
    target_b = tmp_path / "b.txt"
    target_b.write_text("B\n")
    link = tmp_path / "link"
    link.symlink_to("a.txt")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    link.unlink()
    link.symlink_to("b.txt")

    status, body = await _call(str(link), lexical=True)
    assert status == 200
    assert body["status"] == "modified"
    assert "-a.txt" in body["diff"]
    assert "+b.txt" in body["diff"]


@pytest.mark.asyncio
@requires_git
async def test_repo_filter_binding_does_not_execute(tmp_path):
    """A hostile .gitattributes + .git/config filter must never run.

    THE core security regression test: the repo binds a clean/smudge filter
    that would drop a canary file if executed. The endpoint must serve the
    diff via isolated metadata with no canary appearing.
    """
    _init_repo(tmp_path)
    canary = tmp_path / "CANARY"
    (tmp_path / ".gitattributes").write_text("*.txt filter=evil diff=evil\n")
    subprocess.run(
        ["git", "config", "filter.evil.clean", f"touch {canary}"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "filter.evil.smudge", f"touch {canary}"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "diff.evil.textconv", f"touch {canary} &&cat"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    f = tmp_path / "hostile.txt"
    f.write_text("base\n")
    subprocess.run(
        # -c override so the test's own add/commit don't fire the canary
        # (the attack we simulate ships the config in .git, not the add).
        ["git", "-c", "filter.evil.clean=cat", "add", "-A"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-c", "filter.evil.clean=cat", "commit", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    f.write_text("changed\n")
    # The setup's own git operations may have run the filter — only the
    # ENDPOINT's reads are under test. Reset the canary before the call.
    canary.unlink(missing_ok=True)

    status, body = await _call(str(f))
    assert status == 200
    assert not canary.exists(), "repository-defined filter executed during diff read"
    assert body["status"] == "modified"
    assert "+changed" in body["diff"]


@pytest.mark.asyncio
@requires_git
async def test_info_attributes_refused(tmp_path):
    """A non-empty .git/info/attributes fails closed as filters_unsafe."""
    _init_repo(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    info_dir = tmp_path / ".git" / "info"
    info_dir.mkdir(exist_ok=True)
    (info_dir / "attributes").write_text("*.txt filter=evil\n")
    f.write_text("changed\n")

    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "filters_unsafe"
    assert body["diff"] == ""
    assert body["original"] == ""
    assert "info/attributes" in body["error"]


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_untracked_hardlink_content_not_served(tmp_path):
    """An untracked hard link's content is refused (multi-link inode)."""
    _init_repo(tmp_path, commit=True)
    secret = tmp_path / "outside-secret"
    secret.write_text("SECRETVALUE\n")
    link = tmp_path / "innocent.txt"
    os.link(secret, link)

    status, body = await _call(str(link))
    assert status == 200
    assert "SECRETVALUE" not in body["diff"]
    assert "SECRETVALUE" not in body["original"]
    # Refused BEFORE any content read: a multi-link inode is rejected outright
    # (the tracked `git diff` path would otherwise reopen it by name).
    assert body["status"] == "filters_unsafe"
    assert "hard link" in body["error"]
    assert body["diff"] == ""


@pytest.mark.asyncio
@requires_git
async def test_untracked_oversized_file_reports_diff_unavailable(tmp_path, monkeypatch):
    """An untracked file over the synthesis cap is diff_unavailable, not empty."""
    from kiro_crew.dashboard.handlers import git_changes as gc

    monkeypatch.setattr(gc, "_FILE_DIFF_MAX_UNTRACKED_BYTES", 4)
    _init_repo(tmp_path, commit=True)
    f = tmp_path / "big.txt"
    f.write_text("well over four bytes\n")

    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "untracked"
    assert body["diff_unavailable"] is True
    assert body["diff"] == ""
    assert "could not be read completely" in body["error"]


@pytest.mark.asyncio
@requires_git
@posix_only  # the fake-git shim is a #!/bin/sh script Windows cannot exec
async def test_git_inside_repo_refused_before_content_reads(tmp_path, monkeypatch):
    """A pinned git executable INSIDE the repo yields filters_unsafe."""
    from kiro_crew.dashboard.handlers import git_changes as gc

    _init_repo(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text("changed\n")
    fake_git = tmp_path / "git"
    fake_git.write_text('#!/bin/sh\nexec %s "$@"\n' % shutil.which("git"))
    fake_git.chmod(0o755)
    monkeypatch.setattr(gc, "_GIT_EXE", str(fake_git))

    status, body = await _call(str(f))
    assert status == 200
    assert body["status"] == "filters_unsafe"
    assert "executable" in body["error"]
    assert body["diff"] == ""


@pytest.mark.asyncio
@requires_git
async def test_binary_original_blanked(tmp_path):
    """A modified binary file must not decode garbage into `original`."""
    _init_repo(tmp_path)
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02base")
    subprocess.run(["git", "add", "blob.bin"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_bytes(b"\x00\x01\x02changed")

    status, body = await _call(str(f))
    assert status == 200
    assert body["original"] == ""
    # git reports binary files with a short marker patch
    assert body["status"] in ("modified", "clean")


@pytest.mark.asyncio
@requires_git
async def test_directory_request_is_refused(tmp_path):
    """A directory must never reach `git diff -- <dir>`.

    git would recurse and emit a patch for every tracked descendant, returning
    content the caller never named and that never passed per-path validation.
    """
    _init_repo(tmp_path)
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "secret-ish.txt").write_text("SENSITIVE\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    (sub / "secret-ish.txt").write_text("CHANGED-SENSITIVE\n")

    # The repo root and a plain subdirectory are both refused.
    for target in (tmp_path, sub):
        status, body = await _call(str(target))
        assert status == 200
        assert body["diff"] == ""
        assert "SENSITIVE" not in body["diff"]
        assert body["status"] == "not_git"


@pytest.mark.asyncio
async def test_malformed_path_is_400():
    """An embedded NUL is a bad request on every platform, not a 500 or a 200."""
    status, body = await _call("/tmp/bad\x00path")
    assert status == 400
    assert "Malformed" in body["error"]


@pytest.mark.asyncio
async def test_timeout_returns_not_git(tmp_path):
    """Subprocess timeout degrades to not_git status."""
    f = tmp_path / "timeout.txt"
    f.write_text("content")

    def timeout_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    with (
        patch(f"{_MOD}._sel", return_value=_mock_sel()),
        patch(f"{_MOD}.subprocess.run", side_effect=timeout_run),
    ):
        resp = await api_file_diff(_req(str(f)))
    assert resp.status == 200
    assert json.loads(resp.body)["status"] == "not_git"


@pytest.mark.asyncio
@requires_git
async def test_hardened_env_and_flags(tmp_path):
    """Every git spawn uses the allowlisted env + textconv/attributes overrides."""
    _init_repo(tmp_path)
    f = tmp_path / "test.txt"
    f.write_text("content")
    subprocess.run(["git", "add", "test.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text("changed")

    run_calls: list[tuple[list, dict]] = []
    popen_calls: list[tuple[list, dict]] = []
    orig_run = subprocess.run
    orig_popen = subprocess.Popen

    def spy_run(cmd, **kwargs):
        run_calls.append((cmd, kwargs))
        return orig_run(cmd, **kwargs)

    def spy_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return orig_popen(cmd, **kwargs)

    with (
        patch(f"{_MOD}._sel", return_value=_mock_sel()),
        patch(f"{_MOD}.subprocess.run", side_effect=spy_run),
        patch(f"{_MOD}.subprocess.Popen", side_effect=spy_popen),
    ):
        resp = await api_file_diff(_req(str(f)))
    assert resp.status == 200

    # The content-reading diff/show commands run via the byte-capped Popen.
    diff_cmds = [c for c, _ in popen_calls if "diff" in c]
    assert diff_cmds, "expected byte-capped diff spawn"
    for cmd, kwargs in [*run_calls, *popen_calls]:
        if os.path.basename(str(cmd[0])).startswith("git"):
            env = kwargs.get("env") or {}
            assert env.get("GIT_ATTR_NOSYSTEM") == "1"
            assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
            assert env.get("GIT_TERMINAL_PROMPT") == "0"
            # No unrelated gateway env leaks into repo-adjacent processes.
            assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "-c" in diff_cmds[0]
    assert "diff.textconv=" in diff_cmds[0]
    assert f"core.attributesFile={os.devnull}" in diff_cmds[0]
    # Content reads run under the ISOLATED private GIT_DIR.
    content_envs = [k.get("env") or {} for c, k in popen_calls if "diff" in c or "show" in c]
    assert content_envs and all("GIT_DIR" in e for e in content_envs)
    assert all(e["GIT_DIR"] != str(tmp_path / ".git") for e in content_envs)


@pytest.mark.asyncio
async def test_sel_audit_logging_on_success(tmp_path):
    """SEL audit log is called on (degraded) success."""
    f = tmp_path / "audit.txt"
    f.write_text("content")

    mock_sel = _mock_sel()
    with (
        patch(f"{_MOD}._sel", return_value=mock_sel),
        patch(f"{_MOD}.subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")),
    ):
        await api_file_diff(_req(str(f)))

    mock_sel.log_api_access.assert_called_once()
    call_kwargs = mock_sel.log_api_access.call_args
    assert call_kwargs[1]["operation"] == "file_diff"
    assert call_kwargs[1]["outcome"] == "allowed"


@pytest.mark.asyncio
@requires_git
async def test_filters_unsafe_audited_as_denied(tmp_path):
    """A filters_unsafe refusal is SEL-audited as denied, not allowed."""
    _init_repo(tmp_path, commit=True)
    info_dir = tmp_path / ".git" / "info"
    info_dir.mkdir(exist_ok=True)
    (info_dir / "attributes").write_text("* filter=evil\n")
    f = tmp_path / "x.txt"
    f.write_text("y")

    mock_sel = _mock_sel()
    with patch(f"{_MOD}._sel", return_value=mock_sel):
        await api_file_diff(_req(str(f)))
    outcomes = [c[1]["outcome"] for c in mock_sel.log_api_access.call_args_list]
    assert "denied" in outcomes
