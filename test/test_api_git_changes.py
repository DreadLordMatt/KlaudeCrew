"""Tests for the api_git_changes handler in dashboard/handlers/git_changes.py."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import git_changes as gc
from kiro_crew.dashboard.handlers.git_changes import api_git_changes

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX-only scenario")

_MOD = "kiro_crew.dashboard.handlers.git_changes"


def _req(dir_: str = "") -> make_mocked_request:
    # quote(): a path may legally contain spaces or other characters the URL
    # grammar would otherwise mangle, and the handler must receive it verbatim.
    url = f"/api/git-changes?dir={quote(dir_, safe='')}" if dir_ else "/api/git-changes"
    return make_mocked_request("GET", url)


def _mock_sel():
    sel = MagicMock()
    sel.log_api_access = MagicMock()
    return sel


def _init_repo(path, commit: bool = False):
    subprocess.run(["git", "init", "-b", "main"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)
    if commit:
        (path / ".init-marker").write_text("x")
        subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)


async def _call(dir_: str = "") -> tuple[int, dict]:
    with patch(f"{_MOD}._sel", return_value=_mock_sel()):
        resp = await api_git_changes(_req(dir_))
    return resp.status, json.loads(resp.body)


def _rels(body: dict) -> dict:
    return {f["rel"]: f for f in body["repo"]["files"]}


@pytest.mark.asyncio
async def test_missing_dir_param_is_400():
    status, body = await _call("")
    assert status == 400
    assert "dir" in body["error"]


@pytest.mark.asyncio
async def test_sensitive_dir_is_403():
    with (
        patch("kiro_crew.hooks.validate_file_path", return_value=None),
        patch(f"{_MOD}._sel", return_value=_mock_sel()),
    ):
        resp = await api_git_changes(_req("/home/user/.ssh"))
    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "Access denied"


@pytest.mark.asyncio
async def test_nonexistent_dir_returns_null_repo(tmp_path):
    status, body = await _call(str(tmp_path / "nope"))
    assert status == 200
    assert body["repo"] is None


@pytest.mark.asyncio
@requires_git
async def test_non_repo_dir_returns_null_repo(tmp_path):
    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"] is None
    assert "truncated" not in body


@pytest.mark.asyncio
@requires_git
async def test_clean_repo_returns_empty_files(tmp_path):
    _init_repo(tmp_path, commit=True)
    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"]["files"] == []
    assert body["repo"]["branch"] == "main"
    assert body["repo"]["name"] == tmp_path.name
    assert "truncated" not in body


@pytest.mark.asyncio
@requires_git
async def test_statuses_staged_and_counts(tmp_path):
    """Modified / staged-added / deleted / untracked rows with numstat counts."""
    _init_repo(tmp_path)
    (tmp_path / "mod.txt").write_text("one\ntwo\n")
    (tmp_path / "del.txt").write_text("gone\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "mod.txt").write_text("one\nchanged\nthree\n")
    (tmp_path / "del.txt").unlink()
    (tmp_path / "new.txt").write_text("staged new\n")
    subprocess.run(["git", "add", "new.txt"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "untracked.txt").write_text("hi\n")

    status, body = await _call(str(tmp_path))
    assert status == 200
    rels = _rels(body)
    assert rels["mod.txt"]["status"] == "modified"
    assert rels["mod.txt"]["staged"] is False
    assert rels["mod.txt"]["additions"] == 2
    assert rels["mod.txt"]["deletions"] == 1
    assert rels["del.txt"]["status"] == "deleted"
    assert rels["new.txt"]["status"] == "added"
    assert rels["new.txt"]["staged"] is True
    assert rels["new.txt"]["additions"] == 1
    assert rels["untracked.txt"]["status"] == "untracked"
    assert "additions" not in rels["untracked.txt"]
    # Absolute lexical paths point into the repo.
    assert rels["mod.txt"]["path"] == os.path.join(body["repo"]["root"], "mod.txt")


@pytest.mark.asyncio
@requires_git
async def test_subdirectory_resolves_to_repo_root(tmp_path):
    """dir may be INSIDE the repo — the ancestor toplevel is scanned."""
    _init_repo(tmp_path, commit=True)
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    (tmp_path / "changed.txt").write_text("x\n")

    status, body = await _call(str(sub))
    assert status == 200
    assert body["repo"] is not None
    assert body["repo"]["root"] == os.path.realpath(str(tmp_path))
    assert "changed.txt" in _rels(body)


@pytest.mark.asyncio
@requires_git
async def test_no_child_repo_sweep(tmp_path):
    """A repo in a CHILD directory is deliberately not discovered."""
    child = tmp_path / "project"
    child.mkdir()
    _init_repo(child, commit=True)
    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"] is None


@pytest.mark.asyncio
@requires_git
async def test_unborn_head_staged_files_counted(tmp_path):
    """Fresh `git init` + staged file: counts come from the empty-tree base."""
    _init_repo(tmp_path)
    (tmp_path / "first.txt").write_text("a\nb\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)

    status, body = await _call(str(tmp_path))
    assert status == 200
    rels = _rels(body)
    assert rels["first.txt"]["status"] == "added"
    assert rels["first.txt"]["additions"] == 2


@pytest.mark.asyncio
@requires_git
async def test_staged_delete_recreate_coalesces_to_modified(tmp_path):
    """'D path' + '?? path' collapse into ONE row labelled modified, no counts."""
    _init_repo(tmp_path)
    f = tmp_path / "swap.txt"
    f.write_text("old\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "rm", "swap.txt"], cwd=tmp_path, capture_output=True, check=True)
    f.write_text("new\n")

    status, body = await _call(str(tmp_path))
    assert status == 200
    rows = [f for f in body["repo"]["files"] if f["rel"] == "swap.txt"]
    assert len(rows) == 1
    assert rows[0]["status"] == "modified"
    assert "additions" not in rows[0]
    assert "deletions" not in rows[0]


@pytest.mark.asyncio
@requires_git
async def test_untracked_directory_enumerates_files(tmp_path):
    """--untracked-files=all lists real files, not one 'newdir/' row."""
    _init_repo(tmp_path, commit=True)
    newdir = tmp_path / "newdir"
    newdir.mkdir()
    (newdir / "a.txt").write_text("a\n")
    (newdir / "b.txt").write_text("b\n")

    status, body = await _call(str(tmp_path))
    rels = _rels(body)
    assert "newdir/a.txt" in rels
    assert "newdir/b.txt" in rels
    assert "newdir/" not in rels


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_changed_symlink_reports_kind(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("A\n")
    link = tmp_path / "link"
    link.symlink_to("a.txt")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    link.unlink()
    link.symlink_to("b.txt")  # dangling — still a changed entry

    status, body = await _call(str(tmp_path))
    rels = _rels(body)
    assert rels["link"]["kind"] == "symlink"


@pytest.mark.asyncio
@requires_git
async def test_modified_submodule_reports_kind_dir(tmp_path):
    """A modified gitlink reports kind:'dir' so the UI hides file actions."""
    inner = tmp_path / "inner"
    inner.mkdir()
    _init_repo(inner, commit=True)
    outer = tmp_path / "outer"
    outer.mkdir()
    _init_repo(outer, commit=True)
    r = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(inner), "sub"],
        cwd=outer,
        capture_output=True,
    )
    if r.returncode != 0:
        pytest.skip("git submodule add unavailable in this environment")
    subprocess.run(["git", "commit", "-m", "add sub"], cwd=outer, capture_output=True, check=True)
    # Advance the submodule so the gitlink shows modified.
    (outer / "sub" / "advance.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=outer / "sub", capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance"], cwd=outer / "sub", capture_output=True, check=True
    )

    status, body = await _call(str(outer))
    rels = _rels(body)
    assert rels["sub"]["kind"] == "dir"


@pytest.mark.asyncio
@requires_git
async def test_info_attributes_refuses_repo(tmp_path):
    _init_repo(tmp_path, commit=True)
    info_dir = tmp_path / ".git" / "info"
    info_dir.mkdir(exist_ok=True)
    (info_dir / "attributes").write_text("* filter=evil\n")
    (tmp_path / "x.txt").write_text("y\n")

    mock_sel = _mock_sel()
    with patch(f"{_MOD}._sel", return_value=mock_sel):
        resp = await api_git_changes(_req(str(tmp_path)))
    body = json.loads(resp.body)
    assert body["repo"] is None
    assert "info/attributes" in body["filters_unsafe"]
    outcomes = [c[1]["outcome"] for c in mock_sel.log_api_access.call_args_list]
    assert "denied" in outcomes


@pytest.mark.asyncio
@requires_git
async def test_scan_works_without_attr_source(tmp_path, monkeypatch):
    """A whole-repo scan must NOT be refused merely for lacking GIT_ATTR_SOURCE.

    Config isolation (the private GIT_DIR) is the load-bearing control; the
    attribute redirect is defense in depth and only exists on git >= 2.41.
    Refusing without it made the Local tab dead on stock git 2.34/2.39.
    """
    monkeypatch.setattr(gc, "_git_supports_attr_source", lambda timeout=5.0: False)
    _init_repo(tmp_path, commit=True)
    (tmp_path / "changed.txt").write_text("x\n")

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body.get("filters_unsafe") is None
    assert body["repo"] is not None
    assert "changed.txt" in _rels(body)


@pytest.mark.asyncio
@requires_git
async def test_repo_filter_never_executes_without_attr_source(tmp_path, monkeypatch):
    """The canary must stay unfired even with the attribute redirect absent.

    Executing a driver needs an ATTRIBUTE binding *and* a CONFIG definition.
    The synthetic GIT_DIR removes the config half on every git version, so a
    repo carrying BOTH halves still cannot run anything when GIT_ATTR_SOURCE
    is unavailable. This is the evidence the relaxation above rests on.
    """
    monkeypatch.setattr(gc, "_git_supports_attr_source", lambda timeout=5.0: False)
    _init_repo(tmp_path)
    canary = tmp_path / "CANARY"
    (tmp_path / ".gitattributes").write_text("*.txt filter=evil diff=evil\n")
    for key, value in (
        ("filter.evil.clean", f"touch {canary}"),
        ("filter.evil.smudge", f"touch {canary}"),
        ("diff.evil.textconv", f"touch {canary} && cat"),
    ):
        subprocess.run(["git", "config", key, value], cwd=tmp_path, capture_output=True, check=True)
    f = tmp_path / "hostile.txt"
    f.write_text("base\n")
    subprocess.run(
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
    canary.unlink(missing_ok=True)

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert not canary.exists(), "filter executed with GIT_ATTR_SOURCE absent"
    assert body["repo"] is not None
    assert "hostile.txt" in _rels(body)


@pytest.mark.asyncio
@requires_git
async def test_repo_filter_binding_does_not_execute_during_scan(tmp_path):
    """The whole-repo scan must never run a repo-bound clean/diff filter."""
    _init_repo(tmp_path)
    canary = tmp_path / "CANARY"
    (tmp_path / ".gitattributes").write_text("*.txt filter=evil diff=evil\n")
    subprocess.run(
        ["git", "config", "filter.evil.clean", f"touch {canary}"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    f = tmp_path / "hostile.txt"
    f.write_text("base\n")
    subprocess.run(
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
    canary.unlink(missing_ok=True)

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert not canary.exists(), "repository-defined filter executed during scan"
    assert body["repo"] is not None
    assert "hostile.txt" in _rels(body)


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_sensitive_home_paths_excluded(tmp_path, monkeypatch):
    """A protected home dir inside the repo never reaches the response."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _init_repo(tmp_path, commit=True)
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("PRIVATE KEY MATERIAL\n")
    (tmp_path / "normal.txt").write_text("fine\n")

    status, body = await _call(str(tmp_path))
    rels = _rels(body)
    assert "normal.txt" in rels
    assert not any(r.startswith(".ssh") for r in rels)


@pytest.mark.asyncio
@requires_git
async def test_custom_data_home_excluded(tmp_path, monkeypatch):
    """A custom KIROCREW_HOME inside the repo is pathspec-excluded."""
    _init_repo(tmp_path, commit=True)
    data_home = tmp_path / ".kirocrew-dev"
    data_home.mkdir()
    (data_home / "security_policy.json").write_text("{}\n")
    (tmp_path / "normal.txt").write_text("fine\n")
    monkeypatch.setattr(gc, "config_dir", lambda: data_home)

    status, body = await _call(str(tmp_path))
    rels = _rels(body)
    assert "normal.txt" in rels
    assert not any(r.startswith(".kirocrew-dev") for r in rels)


@pytest.mark.asyncio
@requires_git
async def test_file_cap_marks_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "GIT_CHANGES_MAX_FILES", 2)
    _init_repo(tmp_path, commit=True)
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x\n")

    status, body = await _call(str(tmp_path))
    assert len(body["repo"]["files"]) == 2
    assert body["repo"]["truncated"] is True
    assert body["truncated"] is True


@pytest.mark.asyncio
@requires_git
async def test_byte_cap_marks_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "_GIT_CHANGES_MAX_OUTPUT_BYTES", 64)
    _init_repo(tmp_path, commit=True)
    for i in range(30):
        (tmp_path / f"file-with-a-long-name-{i}.txt").write_text("x\n")

    status, body = await _call(str(tmp_path))
    assert body["repo"]["truncated"] is True
    assert body["truncated"] is True


@pytest.mark.asyncio
@requires_git
async def test_expired_budget_reports_truncated(tmp_path, monkeypatch):
    """Deadline exhaustion is a partial scan, never 'no repository'."""
    monkeypatch.setattr(gc, "_GIT_CHANGES_SCAN_DEADLINE_SECS", 0.0)
    _init_repo(tmp_path, commit=True)

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["truncated"] is True


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_undecodable_filename_skipped_marks_truncated(tmp_path):
    """A non-UTF-8 filename row is dropped and the scan marked partial."""
    _init_repo(tmp_path, commit=True)
    raw = os.path.join(os.fsdecode(bytes(tmp_path)), "ok.txt")
    with open(raw, "w") as fh:
        fh.write("x\n")
    try:
        bad = os.path.join(bytes(tmp_path), b"bad-\xff.txt")
        with open(bad, "wb") as fh:
            fh.write(b"y\n")
    except (OSError, ValueError):
        pytest.skip("filesystem rejects non-UTF-8 names")

    status, body = await _call(str(tmp_path))
    rels = _rels(body)
    assert "ok.txt" in rels
    assert body.get("truncated") is True
    assert not any("\ufffd" in r for r in rels)


@pytest.mark.asyncio
@requires_git
async def test_git_status_label_mapping():
    assert gc._git_status_label("??") == "untracked"
    assert gc._git_status_label(" M") == "modified"
    assert gc._git_status_label("M ") == "modified"
    assert gc._git_status_label("A ") == "added"
    assert gc._git_status_label(" D") == "deleted"
    assert gc._git_status_label("UU") == "conflicted"
    assert gc._git_status_label("AA") == "conflicted"
    assert gc._git_status_label("DD") == "conflicted"
    assert gc._git_status_label(" T") == "modified"


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_writable_git_executable_is_refused(tmp_path, monkeypatch):
    """A git binary this user can replace in place is not trusted."""
    _init_repo(tmp_path, commit=True)
    (tmp_path / "changed.txt").write_text("x\n")
    shim_dir = tmp_path / "userbin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text('#!/bin/sh\nexec %s "$@"\n' % shutil.which("git"))
    shim.chmod(0o755)  # owned + writable by this user
    monkeypatch.setattr(gc, "_GIT_EXE", str(shim))

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"] is None
    assert "writable" in body["filters_unsafe"]


@pytest.mark.asyncio
@requires_git
async def test_sensitive_transitive_alternate_is_refused(tmp_path, monkeypatch):
    """A protected store reached through objects/info/alternates fails closed."""
    _init_repo(tmp_path, commit=True)
    (tmp_path / "changed.txt").write_text("x\n")
    # Chain: repo objects -> hop -> "protected" store (classified sensitive).
    hop = tmp_path / "hop-objects"
    (hop / "info").mkdir(parents=True)
    protected = tmp_path / "protected-objects"
    protected.mkdir()
    (hop / "info" / "alternates").write_text(f"{protected}\n")
    info = tmp_path / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(f"{hop}\n")
    real_protected = os.path.realpath(str(protected))
    monkeypatch.setattr(gc, "is_sensitive_path", lambda p: os.path.realpath(p) == real_protected)

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"] is None
    assert body["filters_unsafe"]


@pytest.mark.asyncio
@requires_git
async def test_alternates_cycle_is_bounded(tmp_path):
    """A cyclic alternates chain terminates instead of spinning."""
    _init_repo(tmp_path, commit=True)
    (tmp_path / "changed.txt").write_text("x\n")
    a = tmp_path / "a-objects"
    b = tmp_path / "b-objects"
    (a / "info").mkdir(parents=True)
    (b / "info").mkdir(parents=True)
    (a / "info" / "alternates").write_text(f"{b}\n")
    (b / "info" / "alternates").write_text(f"{a}\n")
    info = tmp_path / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(f"{a}\n")

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert "changed.txt" in _rels(body)


@pytest.mark.asyncio
@requires_git
async def test_submodule_content_filter_never_executes(tmp_path):
    """A hostile submodule's clean filter must not run during the parent scan."""
    inner = tmp_path / "inner"
    inner.mkdir()
    _init_repo(inner, commit=True)
    outer = tmp_path / "outer"
    outer.mkdir()
    _init_repo(outer, commit=True)
    r = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(inner), "sub"],
        cwd=outer,
        capture_output=True,
    )
    if r.returncode != 0:
        pytest.skip("git submodule add unavailable in this environment")
    subprocess.run(["git", "commit", "-m", "add sub"], cwd=outer, capture_output=True, check=True)
    # The SUBMODULE binds a canary command to its own content, in its OWN
    # config — which the parent's synthetic GIT_DIR does not cover.
    canary = tmp_path / "CANARY"
    sub = outer / "sub"
    (sub / ".gitattributes").write_text("*.txt filter=evil\n")
    subprocess.run(
        ["git", "config", "filter.evil.clean", f"touch {canary}"],
        cwd=sub,
        capture_output=True,
        check=True,
    )
    (sub / "dirty.txt").write_text("content\n")
    canary.unlink(missing_ok=True)

    status, body = await _call(str(outer))
    assert status == 200
    assert not canary.exists(), "submodule content filter executed during parent scan"


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_hardlinked_row_marked_kind(tmp_path):
    """A multi-link tracked file is flagged so the UI hides diff/editor actions."""
    _init_repo(tmp_path)
    target = tmp_path / "real.txt"
    target.write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    link = tmp_path / "linked.txt"
    os.link(target, link)

    status, body = await _call(str(tmp_path))
    rels = _rels(body)
    assert rels["linked.txt"]["kind"] == "hardlink"


@pytest.mark.asyncio
@requires_git
async def test_tab_in_filename_keeps_counts(tmp_path):
    """numstat parsing must not drop counts for a path containing a tab."""
    _init_repo(tmp_path, commit=True)
    try:
        weird = tmp_path / "ta\tb.txt"
        weird.write_bytes(b"one\ntwo\n")
    except OSError:
        pytest.skip("filesystem rejects tabs in filenames")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)

    status, body = await _call(str(tmp_path))
    row = _rels(body).get("ta\tb.txt")
    assert row is not None
    assert row["additions"] == 2


@pytest.mark.asyncio
@requires_git
async def test_numstat_truncation_marks_partial(tmp_path, monkeypatch):
    """Capped numstat output means counts are incomplete — report it."""
    monkeypatch.setattr(gc, "_GIT_CHANGES_MAX_OUTPUT_BYTES", 48)
    _init_repo(tmp_path)
    for i in range(20):
        (tmp_path / f"tracked-file-number-{i}.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_malformed_dir_is_400():
    """An embedded NUL is a bad request, not a 500."""
    with patch(f"{_MOD}._sel", return_value=_mock_sel()):
        resp = await api_git_changes(_req("/tmp/bad\x00path"))
    assert resp.status == 400
    assert "Malformed" in json.loads(resp.body)["error"]


@pytest.mark.asyncio
@requires_git
async def test_refusal_audits_exactly_one_outcome(tmp_path):
    """A filters_unsafe scan must not emit both allowed and denied."""
    _init_repo(tmp_path, commit=True)
    info_dir = tmp_path / ".git" / "info"
    info_dir.mkdir(exist_ok=True)
    (info_dir / "attributes").write_text("* filter=evil\n")

    mock_sel = _mock_sel()
    with patch(f"{_MOD}._sel", return_value=mock_sel):
        await api_git_changes(_req(str(tmp_path)))
    outcomes = [c[1]["outcome"] for c in mock_sel.log_api_access.call_args_list]
    assert outcomes == ["denied"]


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_symlinked_alternates_file_is_refused(tmp_path, monkeypatch):
    """info/alternates as a SYMLINK is refused, never read through."""
    _init_repo(tmp_path, commit=True)
    (tmp_path / "changed.txt").write_text("x\n")
    secret = tmp_path / "secret-target"
    secret.write_text("PRIVATE\n")
    info = tmp_path / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").symlink_to(secret)

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"] is None
    assert body["filters_unsafe"]


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_fifo_alternates_file_does_not_hang(tmp_path):
    """A FIFO in place of info/alternates is refused instead of blocking."""
    _init_repo(tmp_path, commit=True)
    (tmp_path / "changed.txt").write_text("x\n")
    info = tmp_path / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    os.mkfifo(str(info / "alternates"))

    status, body = await asyncio.wait_for(_call(str(tmp_path)), timeout=25)
    assert status == 200
    assert body["repo"] is None
    assert body["filters_unsafe"]


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_symlinked_index_is_refused(tmp_path, monkeypatch):
    """A .git/index symlinked at a protected file is refused."""
    _init_repo(tmp_path, commit=True)
    (tmp_path / "changed.txt").write_text("x\n")
    secret = tmp_path / "protected-file"
    secret.write_text("PRIVATE\n")
    index = tmp_path / ".git" / "index"
    index.unlink(missing_ok=True)
    index.symlink_to(secret)
    real_secret = os.path.realpath(str(secret))
    monkeypatch.setattr(gc, "is_sensitive_path", lambda p: os.path.realpath(p) == real_secret)

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"] is None
    assert body["filters_unsafe"]


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_fifo_untracked_file_does_not_hang(tmp_path):
    """An untracked FIFO must not block an executor worker."""
    _init_repo(tmp_path, commit=True)
    os.mkfifo(str(tmp_path / "pipe"))
    (tmp_path / "normal.txt").write_text("ok\n")

    status, body = await asyncio.wait_for(_call(str(tmp_path)), timeout=25)
    assert status == 200
    assert "normal.txt" in _rels(body)


@pytest.mark.asyncio
@requires_git
async def test_data_home_equal_to_repo_root_still_excludes_secrets(tmp_path, monkeypatch):
    """When the data home IS the repo root, secret leaves are still excluded."""
    _init_repo(tmp_path, commit=True)
    monkeypatch.setattr(gc, "config_dir", lambda: tmp_path)
    (tmp_path / "security_policy.json").write_text("{}\n")
    (tmp_path / "normal.txt").write_text("fine\n")

    status, body = await _call(str(tmp_path))
    assert status == 200
    rels = _rels(body)
    assert "normal.txt" in rels
    assert "security_policy.json" not in rels


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_hardlink_only_change_is_not_reported_clean(tmp_path):
    """A repo whose ONLY change is a hard-linked file must not read as clean.

    The path is excluded from status/numstat (its content must not be read), so
    the scan cannot know whether it changed -- it is marked incomplete instead.
    """
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    other = tmp_path / "other-inode"
    other.write_text("different\n")
    tracked.unlink()
    os.link(other, tracked)

    status, body = await _call(str(tmp_path))
    assert status == 200
    # Never presented as an authoritative clean tree.
    assert body["truncated"] is True


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_undecodable_tracked_path_fails_closed(tmp_path):
    """A non-UTF-8 tracked path cannot be verified, so the scan is refused."""
    _init_repo(tmp_path, commit=True)
    try:
        bad = os.path.join(bytes(tmp_path), b"bad-\xff.txt")
        with open(bad, "wb") as fh:
            fh.write(b"x\n")
    except (OSError, ValueError):
        pytest.skip("filesystem rejects non-UTF-8 names")
    r = subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
    if r.returncode != 0:
        pytest.skip("git refused to track the non-UTF-8 path")

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"] is None
    assert "decode" in body["filters_unsafe"]


@pytest.mark.asyncio
@requires_git
async def test_deep_alternates_chain_fails_closed(tmp_path):
    """A chain deeper than the bound is REFUSED, not silently left unverified."""
    _init_repo(tmp_path, commit=True)
    (tmp_path / "changed.txt").write_text("x\n")
    prev = tmp_path / ".git" / "objects"
    for i in range(gc._ALTERNATES_MAX_DEPTH + 3):
        nxt = tmp_path / f"hop{i}-objects"
        (nxt / "info").mkdir(parents=True)
        (prev / "info").mkdir(parents=True, exist_ok=True)
        (prev / "info" / "alternates").write_text(f"{nxt}\n")
        prev = nxt

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"] is None
    assert "too deep" in body["filters_unsafe"]


@pytest.mark.asyncio
@requires_git
async def test_split_index_repo_is_refused(tmp_path):
    """core.splitIndex keeps entries in the real git dir — refuse, don't misreport."""
    _init_repo(tmp_path, commit=True)
    (tmp_path / "changed.txt").write_text("x\n")
    r = subprocess.run(["git", "update-index", "--split-index"], cwd=tmp_path, capture_output=True)
    if r.returncode != 0:
        pytest.skip("git update-index --split-index unavailable")

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"] is None
    assert "split index" in body["filters_unsafe"]


@pytest.mark.asyncio
@requires_git
@posix_only  # ':' is not a legal path character on Windows
async def test_repo_path_with_path_separator_is_readable(tmp_path):
    """A repo whose path contains ':' must not misparse as two alternates."""
    weird = tmp_path / "co:lon"
    weird.mkdir()
    _init_repo(weird, commit=True)
    (weird / "changed.txt").write_text("x\n")

    status, body = await _call(str(weird))
    assert status == 200
    assert body["repo"] is not None
    assert "changed.txt" in _rels(body)


@pytest.mark.asyncio
async def test_missing_dir_is_audited():
    """The 400 for a missing dir param still emits exactly one SEL outcome."""
    mock_sel = _mock_sel()
    with patch(f"{_MOD}._sel", return_value=mock_sel):
        resp = await api_git_changes(make_mocked_request("GET", "/api/git-changes"))
    assert resp.status == 400
    outcomes = [c[1]["outcome"] for c in mock_sel.log_api_access.call_args_list]
    assert outcomes == ["denied"]


@pytest.mark.asyncio
@requires_git
@posix_only
async def test_hardlinked_tracked_path_leaks_no_counts(tmp_path):
    """status/numstat must never read a hard-linked tracked file.

    The row may still be reported (labelled kind:'hardlink'), but it must carry
    NO +/- counts -- those would be derived from the linked inode's content,
    which is exactly the metadata leak the preflight exclusion closes.
    """
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\ntwo\nthree\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    # Replace the tracked path's inode with a hard link to a file whose content
    # differs -- git would otherwise diff it and report line counts.
    other = tmp_path / "other-inode"
    other.write_text("A\nB\nC\nD\nE\nF\n")
    tracked.unlink()
    os.link(other, tracked)

    status, body = await _call(str(tmp_path))
    assert status == 200
    row = _rels(body).get("tracked.txt")
    if row is not None:
        assert row.get("kind") == "hardlink"
        assert "additions" not in row
        assert "deletions" not in row


@pytest.mark.asyncio
@requires_git
async def test_hardlink_preflight_fails_closed_past_cap(tmp_path, monkeypatch):
    """An unverifiable tracked-file list refuses the scan (no partial coverage).

    Covering only a prefix would leave paths past the cut UNEXCLUDED, so their
    content would be read by status/numstat -- the exact leak the preflight
    exists to close.
    """
    monkeypatch.setattr(gc, "_HARDLINK_PREFLIGHT_MAX_PATHS", 2)
    _init_repo(tmp_path)
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "f0.txt").write_text("changed\n")

    status, body = await _call(str(tmp_path))
    assert status == 200
    assert body["repo"] is None
    assert "hard-link" in body["filters_unsafe"]


@requires_git
def test_oversized_config_value_is_capped_and_falls_back(tmp_path):
    """A repo-controlled config VALUE is byte-capped, not buffered whole.

    Targets _repo_bool directly: an oversized value makes git itself reject
    most commands, so an end-to-end scan never reaches the read. The buffering
    vector is the untyped `config --get`, whose stdout echoes the raw value.
    """
    _init_repo(tmp_path, commit=True)
    subprocess.run(
        ["git", "config", "core.bigvalue", "z" * 6000],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    env = gc._hardened_git_env()
    value = gc._repo_bool(
        str(tmp_path),
        env,
        "core.bigvalue",
        5.0,
        default="false",
        allowed=("true", "false", "input"),
    )
    # Truncated read -> not in the allowlist -> documented default.
    assert value == "false"


@pytest.mark.asyncio
@requires_git
@posix_only  # Windows silently trims trailing spaces from path components
async def test_query_path_whitespace_is_preserved(tmp_path):
    """A directory name ending in a space must still resolve."""
    spaced = tmp_path / "dir with trailing "
    try:
        spaced.mkdir()
    except OSError:
        pytest.skip("filesystem rejects trailing-space directory names")
    _init_repo(spaced, commit=True)
    (spaced / "changed.txt").write_text("x\n")

    status, body = await _call(str(spaced))
    assert status == 200
    assert body["repo"] is not None
    assert "changed.txt" in _rels(body)


@pytest.mark.asyncio
@requires_git
@posix_only  # the fake-git shim is a #!/bin/sh script Windows cannot exec
async def test_git_inside_scanned_repo_is_refused(tmp_path, monkeypatch):
    """A pinned git executable INSIDE the scanned repo fails closed."""
    _init_repo(tmp_path, commit=True)
    (tmp_path / "changed.txt").write_text("x\n")
    fake_git = tmp_path / "git"
    real_git = shutil.which("git")
    fake_git.write_text('#!/bin/sh\nexec %s "$@"\n' % real_git)
    fake_git.chmod(0o755)
    monkeypatch.setattr(gc, "_GIT_EXE", str(fake_git))

    status, body = await _call(str(tmp_path))
    assert status == 200
    # Deterministic refusal on the FIRST spawn's result: no repo payload, and
    # the reason names the executable containment.
    assert body["repo"] is None
    assert "executable" in body["filters_unsafe"]
