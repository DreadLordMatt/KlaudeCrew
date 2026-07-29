"""Tests for linked skill repositories (``skills.sources``).

Split into three layers:

* pure validation / path containment (runs everywhere),
* the sync state ledger,
* real ``git`` against a local repo, with the clone-host SSRF gate patched to
  admit a ``file://`` remote. Those skip on a host where the OS sandbox cannot
  establish isolation, matching ``test_worktree_create``'s posture — that is a
  platform limitation, not a defect in the code under test.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import subprocess
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew import skill_sources as ss
from kiro_crew.config.loader import KiroCrewConfig, SkillSourceConfig

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


@functools.lru_cache(maxsize=1)
def _sandbox_git_reason() -> str:
    """'' when a sandboxed git can run here, else why it cannot.

    A backend-availability probe alone is not enough: some CI runners pass the
    user-namespace probe but deny ``unshare(NEWNS)`` at exec time, which only
    the child can report.
    """
    import asyncio

    from kiro_crew.apps.registry import minimal_env

    async def _probe() -> tuple[int, str]:
        return await ss._run_git(
            ["--version"],
            cwd=None,
            sandbox_mode="strict",
            env=minimal_env(),
            timeout=15,
        )

    try:
        code, out = asyncio.run(_probe())
    except RuntimeError as exc:
        return str(exc) or "sandbox unavailable"
    except OSError as exc:  # pragma: no cover — no git binary at all
        return f"git unavailable: {exc}"
    return "" if code == 0 else (out or "git failed").strip()


def _require_sandbox_git() -> None:
    reason = _sandbox_git_reason()
    if reason:
        pytest.skip(f"sandboxed git cannot run on this host: {reason[:120]}")


@pytest.fixture
def upstream(tmp_path):
    """A local git repo holding two skills on branch ``main``."""
    _require_sandbox_git()
    root = tmp_path / "upstream"
    (root / "skills" / "alpha").mkdir(parents=True)
    (root / "skills" / "beta").mkdir(parents=True)
    (root / "skills" / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha skill\n---\nbody\n", encoding="utf-8"
    )
    (root / "skills" / "beta" / "SKILL.md").write_text(
        "---\nname: beta\ndescription: Beta skill\n---\nbody\n", encoding="utf-8"
    )
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)
    return root


@pytest.fixture
def trust_file_urls(monkeypatch):
    """Admit ``file://`` remotes through the clone-host SSRF gate.

    The gate is host-granular and a ``file://`` URL is hostless, so it fails
    closed by design. Patching it here is what lets the sync path be exercised
    against a local repo instead of the network; the gate's own behaviour is
    asserted separately in ``test_untrusted_host_refused_before_spawn``.
    """
    monkeypatch.setattr(ss, "is_clone_host_trusted", lambda url: True)


def _loader_with_sources(skills_path, cfg):
    """Build a loader and perform the reload that populates linked roots.

    Loader construction deliberately skips the linked-root scan (``__init__`` can
    run on the event loop), so linked skills only become visible after a
    ``reload_extra_paths`` — which production performs in a worker thread from the
    startup sync and from every mutation handler. Tests that assert on linked
    skills must do the same, or they would be asserting against a contract the
    code does not have.
    """
    from kiro_crew.skills import SkillsLoader

    loader = SkillsLoader(skills_path=skills_path, install_builtins=False, config=cfg)
    loader.reload_extra_paths(cfg)
    return loader


def _persist_sources(sources):
    """Write *sources* into the isolated home's config.json.

    ``sync_skill_sources`` re-reads each entry from config under its lock, so a
    batch test must actually persist them — passing bare dataclasses would leave
    every entry looking like one the user had unlinked.
    """
    cfg = KiroCrewConfig.load()
    cfg.skills.sources = list(sources)
    cfg.save()


def _source(tmp_path, upstream_root, **kw) -> SkillSourceConfig:
    return SkillSourceConfig(
        name=kw.get("name", "team-skills"),
        repo=kw.get("repo", f"file://{upstream_root}"),
        branch=kw.get("branch", "main"),
        subdir=kw.get("subdir", "skills"),
        enabled=kw.get("enabled", True),
    )


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class TestNameValidation:
    @pytest.mark.parametrize("name", ["team", "team-skills", "a1", "x-9-y"])
    def test_accepts_kebab_case(self, name):
        assert ss.is_valid_source_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "",
            None,
            123,
            "Team",  # uppercase
            "team_skills",  # underscore
            "../etc",  # traversal
            "team/skills",  # separator
            "-team",  # leading hyphen
            "team-",  # trailing hyphen
            "team--skills",  # empty segment
            ".",
            "a" * 65,  # over the length cap
        ],
    )
    def test_rejects_unsafe(self, name):
        assert not ss.is_valid_source_name(name)

    def test_skill_source_dir_returns_none_for_unsafe_name(self):
        assert ss.skill_source_dir("../escape") is None
        assert ss.skill_source_dir("ok-name") == ss.skill_sources_dir() / "ok-name"


class TestBranchValidation:
    @pytest.mark.parametrize("branch", ["main", "release/2.0", "feat-x", "v1.2.3"])
    def test_accepts_ordinary_branches(self, branch):
        assert ss._is_valid_branch(branch)

    @pytest.mark.parametrize(
        "branch",
        [
            "",
            "--upload-pack=/bin/sh",  # would be parsed as an option
            "-x",
            "/abs",
            "trailing/",
            "a..b",
            "has space",
            "has\tab",
            "ref^",
            "ref~1",
            "a:b",
            "nul\x00byte",
        ],
    )
    def test_rejects_option_like_and_malformed(self, branch):
        assert not ss._is_valid_branch(branch)


class TestSourceSkillRoot:
    def test_none_when_not_cloned(self):
        assert ss.source_skill_root("team-skills", "skills") is None

    def test_resolves_subdir_under_clone(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(ss, "skill_sources_dir", lambda: home / "skill-sources")
        (home / "skill-sources" / "team" / "skills").mkdir(parents=True)
        root = ss.source_skill_root("team", "skills")
        assert root == home / "skill-sources" / "team" / "skills"

    def test_rejects_traversal_subdir(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(ss, "skill_sources_dir", lambda: home / "skill-sources")
        (home / "skill-sources" / "team").mkdir(parents=True)
        (home / "outside").mkdir()
        assert ss.source_skill_root("team", "../outside") is None

    def test_rejects_symlink_escaping_the_clone(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(ss, "skill_sources_dir", lambda: home / "skill-sources")
        clone = home / "skill-sources" / "team"
        clone.mkdir(parents=True)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (clone / "skills").symlink_to(outside, target_is_directory=True)
        # Lexically fine, but resolves outside the clone — containment must win.
        assert ss.source_skill_root("team", "skills") is None


class TestSkillSourceRoots:
    def test_skips_disabled_and_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(ss, "skill_sources_dir", lambda: home / "skill-sources")
        (home / "skill-sources" / "present" / "skills").mkdir(parents=True)
        (home / "skill-sources" / "off" / "skills").mkdir(parents=True)
        roots = ss.skill_source_roots(
            [
                SkillSourceConfig(name="present", repo="r", subdir="skills"),
                SkillSourceConfig(name="off", repo="r", subdir="skills", enabled=False),
                SkillSourceConfig(name="absent", repo="r", subdir="skills"),
            ]
        )
        assert roots == [home / "skill-sources" / "present" / "skills"]

    def test_preserves_configured_order(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(ss, "skill_sources_dir", lambda: home / "skill-sources")
        for n in ("second", "first"):
            (home / "skill-sources" / n).mkdir(parents=True)
        roots = ss.skill_source_roots(
            [
                SkillSourceConfig(name="first", repo="r"),
                SkillSourceConfig(name="second", repo="r"),
            ]
        )
        assert [p.name for p in roots] == ["first", "second"]

    def test_tolerates_empty_and_none(self):
        assert ss.skill_source_roots([]) == []
        assert ss.skill_source_roots(None) == []


class TestCountSkills:
    def test_counts_nested_and_ignores_dot_dirs(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "SKILL.md").write_text("x", encoding="utf-8")
        (tmp_path / "cat" / "b").mkdir(parents=True)
        (tmp_path / "cat" / "b" / "SKILL.md").write_text("x", encoding="utf-8")
        (tmp_path / ".git" / "hidden").mkdir(parents=True)
        (tmp_path / ".git" / "hidden" / "SKILL.md").write_text("x", encoding="utf-8")
        assert ss.count_skills(tmp_path) == 2


# ---------------------------------------------------------------------------
# sync state ledger
# ---------------------------------------------------------------------------


class TestSyncState:
    def test_roundtrip(self):
        ss.record_sync_state(
            ss.SkillSourceSyncResult(
                name="team", ok=True, action="cloned", head="a" * 40, skill_count=3, synced_at=100.0
            )
        )
        state = ss.read_sync_state()
        assert state["team"]["ok"] is True
        assert state["team"]["head"] == "a" * 40
        assert state["team"]["skill_count"] == 3
        assert state["team"]["last_success_at"] == 100.0

    def test_failure_preserves_last_known_good(self):
        ss.record_sync_state(
            ss.SkillSourceSyncResult(
                name="team", ok=True, action="cloned", head="b" * 40, skill_count=2, synced_at=1.0
            )
        )
        ss.record_sync_state(
            ss.SkillSourceSyncResult(
                name="team", ok=False, action="failed", error="fetch_failed", synced_at=2.0
            )
        )
        entry = ss.read_sync_state()["team"]
        # The mirror is still mounted at the old commit, so the row must keep
        # reporting what it is actually serving.
        assert entry["ok"] is False
        assert entry["error"] == "fetch_failed"
        assert entry["head"] == "b" * 40
        assert entry["skill_count"] == 2
        assert entry["last_success_at"] == 1.0
        assert entry["synced_at"] == 2.0

    def test_forget_removes_entry(self):
        ss.record_sync_state(ss.SkillSourceSyncResult(name="team", ok=True, head="c" * 40))
        ss.forget_sync_state("team")
        assert "team" not in ss.read_sync_state()

    def test_concurrent_records_for_different_sources_do_not_clobber(self):
        """The ledger is one file rewritten in full.

        Two syncs for different sources both read-modify-write it, and the
        per-source locks cannot help because the contention is on shared state.
        Unserialized, the second write drops the first's entry.
        """
        import threading

        names = [f"src-{i}" for i in range(12)]
        barrier = threading.Barrier(len(names))

        def _writer(name: str) -> None:
            barrier.wait()  # maximize overlap on the read-modify-write
            ss.record_sync_state(
                ss.SkillSourceSyncResult(
                    name=name, ok=True, action="cloned", head="a" * 40, skill_count=1
                )
            )

        threads = [threading.Thread(target=_writer, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert set(ss.read_sync_state()) == set(names)

    def test_forget_is_serialized_against_records(self):
        import threading

        for i in range(8):
            ss.record_sync_state(ss.SkillSourceSyncResult(name=f"keep-{i}", ok=True))
        ss.record_sync_state(ss.SkillSourceSyncResult(name="doomed", ok=True))
        barrier = threading.Barrier(2)

        def _forget() -> None:
            barrier.wait()
            ss.forget_sync_state("doomed")

        def _record() -> None:
            barrier.wait()
            ss.record_sync_state(ss.SkillSourceSyncResult(name="late", ok=True))

        threads = [threading.Thread(target=_forget), threading.Thread(target=_record)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = ss.read_sync_state()
        assert "doomed" not in state
        assert "late" in state
        assert {f"keep-{i}" for i in range(8)} <= set(state)

    def test_corrupt_ledger_reads_as_empty(self):
        ss.skill_sources_dir().mkdir(parents=True, exist_ok=True)
        ss._state_path().write_text("{not json", encoding="utf-8")
        assert ss.read_sync_state() == {}


# ---------------------------------------------------------------------------
# sync — failure paths that must not spawn git
# ---------------------------------------------------------------------------


class TestSyncGuards:
    @pytest.mark.asyncio
    async def test_untrusted_host_refused_before_spawn(self, monkeypatch):
        called = False

        async def _boom(*a, **kw):
            nonlocal called
            called = True
            return 0, ""

        monkeypatch.setattr(ss, "_run_git", _boom)
        result = await ss.sync_skill_source(
            SkillSourceConfig(name="team", repo="https://evil.internal/x.git")
        )
        assert not result.ok
        assert result.error == "untrusted_host"
        assert called is False

    @pytest.mark.asyncio
    async def test_invalid_name_refused(self):
        result = await ss.sync_skill_source(
            SkillSourceConfig(name="../escape", repo="https://github.com/o/r.git")
        )
        assert (result.ok, result.error) == (False, "invalid_name")

    @pytest.mark.asyncio
    async def test_missing_repo_refused(self):
        result = await ss.sync_skill_source(SkillSourceConfig(name="team", repo=""))
        assert (result.ok, result.error) == (False, "missing_repo")

    @pytest.mark.asyncio
    async def test_unsafe_subdir_refused(self):
        result = await ss.sync_skill_source(
            SkillSourceConfig(name="team", repo="https://github.com/o/r.git", subdir="../etc")
        )
        assert (result.ok, result.error) == (False, "invalid_subdir")

    @pytest.mark.asyncio
    async def test_option_like_branch_refused(self):
        result = await ss.sync_skill_source(
            SkillSourceConfig(
                name="team", repo="https://github.com/o/r.git", branch="--upload-pack=x"
            )
        )
        assert (result.ok, result.error) == (False, "invalid_branch")

    @pytest.mark.asyncio
    async def test_sandbox_unavailable_reported_not_raised(self, monkeypatch, trust_file_urls):
        def _no_sandbox(argv, mode="standard", **kw):
            raise RuntimeError("no sandbox backend")

        monkeypatch.setattr(ss, "wrap_argv", _no_sandbox)
        result = await ss.sync_skill_source(
            SkillSourceConfig(name="team", repo="https://github.com/o/r.git")
        )
        assert (result.ok, result.error) == (False, "sandbox_unavailable")


# ---------------------------------------------------------------------------
# sync — real git
# ---------------------------------------------------------------------------


# Serialized onto one xdist worker: every test in this class spawns a real
# sandboxed git process, and letting several workers do that at once measurably
# starves the timing-sensitive process-reaper tests in test_apps_registry.py
# (which assert a process tree is killed within a fixed timeout).
@pytest.mark.xdist_group(name="skill_sources_real_git")
@pytest.mark.usefixtures("trust_file_urls")
class TestSyncRealGit:
    @pytest.mark.asyncio
    async def test_clone_then_mount(self, tmp_path, upstream):
        src = _source(tmp_path, upstream)
        result = await ss.sync_skill_source(src)
        assert result.ok, result.log
        assert result.action == "cloned"
        assert result.skill_count == 2
        assert len(result.head) == 40
        root = ss.source_skill_root(src.name, src.subdir)
        assert root is not None
        assert (root / "alpha" / "SKILL.md").is_file()

    @pytest.mark.asyncio
    async def test_second_sync_updates_in_place(self, tmp_path, upstream):
        src = _source(tmp_path, upstream)
        first = await ss.sync_skill_source(src)
        assert first.action == "cloned"

        (upstream / "skills" / "gamma").mkdir()
        (upstream / "skills" / "gamma" / "SKILL.md").write_text("g", encoding="utf-8")
        _git("add", "-A", cwd=upstream)
        _git("commit", "-q", "-m", "add gamma", cwd=upstream)

        second = await ss.sync_skill_source(src)
        assert second.ok, second.log
        assert second.action == "updated"
        assert second.skill_count == 3
        assert second.head != first.head

    @pytest.mark.asyncio
    async def test_upstream_deletion_propagates(self, tmp_path, upstream):
        """The whole point of reset --hard over pull --ff-only.

        A removed upstream skill must disappear locally; a merge-style update
        would leave the file (or refuse) and the team would keep loading a skill
        that was deliberately retired.
        """
        src = _source(tmp_path, upstream)
        await ss.sync_skill_source(src)
        _git("rm", "-r", "-q", "skills/beta", cwd=upstream)
        _git("commit", "-q", "-m", "drop beta", cwd=upstream)

        result = await ss.sync_skill_source(src)
        assert result.ok, result.log
        assert result.skill_count == 1
        root = ss.source_skill_root(src.name, src.subdir)
        assert root is not None
        assert not (root / "beta").exists()

    @pytest.mark.asyncio
    async def test_local_edits_to_the_mirror_are_discarded(self, tmp_path, upstream):
        src = _source(tmp_path, upstream)
        await ss.sync_skill_source(src)
        root = ss.source_skill_root(src.name, src.subdir)
        assert root is not None
        (root / "alpha" / "SKILL.md").write_text("locally hacked", encoding="utf-8")
        (root / "stray.txt").write_text("untracked", encoding="utf-8")

        result = await ss.sync_skill_source(src)
        assert result.ok, result.log
        assert "locally hacked" not in (root / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        assert not (root / "stray.txt").exists()

    @pytest.mark.asyncio
    async def test_changing_the_repo_url_retargets_the_existing_mirror(self, tmp_path, upstream):
        """A refresh must follow the CONFIGURED url, not the clone-time remote.

        Otherwise editing a source's repo keeps serving the old repository's
        skills forever, and the clone-host trust gate validates a URL the fetch
        does not actually use.
        """
        src = _source(tmp_path, upstream)
        assert (await ss.sync_skill_source(src)).ok

        other = tmp_path / "other-upstream"
        (other / "skills" / "delta").mkdir(parents=True)
        (other / "skills" / "delta" / "SKILL.md").write_text(
            "---\nname: delta\ndescription: Delta skill\n---\nbody\n", encoding="utf-8"
        )
        _git("init", "-q", "-b", "main", cwd=other)
        _git("config", "user.email", "test@example.com", cwd=other)
        _git("config", "user.name", "Test", cwd=other)
        _git("add", "-A", cwd=other)
        _git("commit", "-q", "-m", "seed other", cwd=other)

        # Same source name (same mirror dir), different repo — the update path.
        retargeted = _source(tmp_path, upstream, repo=f"file://{other}")
        result = await ss.sync_skill_source(retargeted)
        assert result.ok, result.log
        assert result.action == "updated"
        root = ss.source_skill_root(src.name, src.subdir)
        assert root is not None
        assert (root / "delta" / "SKILL.md").is_file()
        assert not (root / "alpha").exists()

    @pytest.mark.asyncio
    async def test_poisoned_core_worktree_cannot_redirect_the_sync(self, tmp_path, upstream):
        """A mirror's own .git/config must not be able to redirect writes.

        The mirror is a git repo, so its repo-local config is mutable — and
        ``core.worktree`` there redirects the destructive half of a sync. Verified
        directly that a bare ``reset --hard`` DOES write into a poisoned
        worktree, so this pins the countermeasure: every in-repo git call passes
        an explicit ``--git-dir``/``--work-tree``, which outranks any config file.
        """
        src = _source(tmp_path, upstream, name="poisoned")
        assert (await ss.sync_skill_source(src)).ok
        dest = ss.skill_source_dir("poisoned")
        assert dest is not None

        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "precious.txt").write_text("PRECIOUS\n", encoding="utf-8")
        _git("config", "core.worktree", str(victim), cwd=dest)

        # Advance upstream so the next sync has real work to check out.
        (upstream / "skills" / "gamma").mkdir()
        (upstream / "skills" / "gamma" / "SKILL.md").write_text(
            "---\nname: gamma\ndescription: Gamma\n---\nbody\n", encoding="utf-8"
        )
        _git("add", "-A", cwd=upstream)
        _git("commit", "-q", "-m", "add gamma", cwd=upstream)

        result = await ss.sync_skill_source(src)
        assert result.ok, result.log
        # Nothing was written into the pointed-at directory...
        assert sorted(p.name for p in victim.iterdir()) == ["precious.txt"]
        # ...and the checkout landed inside the mirror, where it belongs.
        root = ss.source_skill_root("poisoned", "skills")
        assert root is not None
        assert (root / "gamma" / "SKILL.md").is_file()

    @pytest.mark.asyncio
    async def test_in_repo_git_calls_pin_the_worktree_and_disable_hooks(
        self, tmp_path, upstream, monkeypatch
    ):
        """Every in-repo call must carry the pinning and neutralizing options.

        Asserted on argv rather than on observable behaviour because none of the
        commands this module runs (``fetch``, ``reset``, ``clean``, ``rev-parse``)
        fires a repo-local hook, so a behavioural test of ``core.hooksPath``
        would pass whether or not the option was present — it would look like
        coverage while proving nothing. The worktree pinning IS behaviourally
        tested above; this pins the rest of the contract so a future command that
        does run hooks inherits it.
        """
        src = _source(tmp_path, upstream, name="argv-pinned")
        assert (await ss.sync_skill_source(src)).ok
        dest = ss.skill_source_dir("argv-pinned")
        assert dest is not None

        seen: list[list[str]] = []
        real = ss.wrap_argv

        def _spy(argv, mode="standard", **kw):
            seen.append(list(argv))
            return real(argv, mode=mode, **kw)

        monkeypatch.setattr(ss, "wrap_argv", _spy)
        assert (await ss.sync_skill_source(src)).ok

        in_repo = [a for a in seen if any(x.startswith("--git-dir=") for x in a)]
        assert in_repo, f"no in-repo call carried --git-dir: {seen}"
        for argv in in_repo:
            assert f"--work-tree={dest}" in argv
            assert f"--git-dir={dest / '.git'}" in argv
            assert f"core.hooksPath={os.devnull}" in argv
            assert "core.fsmonitor=false" in argv

    @pytest.mark.asyncio
    async def test_missing_subdir_reported(self, tmp_path, upstream):
        src = _source(tmp_path, upstream, subdir="nope")
        result = await ss.sync_skill_source(src)
        assert (result.ok, result.error) == (False, "missing_skill_root")

    @pytest.mark.asyncio
    async def test_upstream_removing_the_subdir_restores_the_previous_commit(
        self, tmp_path, upstream
    ):
        """A post-reset validation failure must not leave the mirror wiped.

        The reset happens before the mount can be validated, so an upstream
        change that invalidates the configured subdir would otherwise report
        "failed" while every shared skill had already been deleted locally.
        """
        src = _source(tmp_path, upstream)
        first = await ss.sync_skill_source(src)
        assert first.ok

        _git("rm", "-r", "-q", "skills", cwd=upstream)
        (upstream / "elsewhere").mkdir()
        (upstream / "elsewhere" / "keep.md").write_text("x", encoding="utf-8")
        _git("add", "-A", cwd=upstream)
        _git("commit", "-q", "-m", "move skills out", cwd=upstream)

        result = await ss.sync_skill_source(src)
        assert (result.ok, result.error) == (False, "missing_skill_root")
        # The mirror is back on the commit it was serving, with its skills intact.
        root = ss.source_skill_root(src.name, src.subdir)
        assert root is not None, result.log
        assert (root / "alpha" / "SKILL.md").is_file()
        assert ss.count_skills(root) == 2

    @pytest.mark.asyncio
    async def test_empty_repo_is_rejected_not_linked(self, tmp_path):
        """A source contributing zero skills is a wrong subdir/branch, not a success."""
        _require_sandbox_git()
        empty = tmp_path / "empty-upstream"
        (empty / "docs").mkdir(parents=True)
        (empty / "docs" / "README.md").write_text("no skills here\n", encoding="utf-8")
        _git("init", "-q", "-b", "main", cwd=empty)
        _git("config", "user.email", "test@example.com", cwd=empty)
        _git("config", "user.name", "Test", cwd=empty)
        _git("add", "-A", cwd=empty)
        _git("commit", "-q", "-m", "seed", cwd=empty)

        src = SkillSourceConfig(name="empty", repo=f"file://{empty}", branch="main", subdir="")
        result = await ss.sync_skill_source(src)
        assert (result.ok, result.error) == (False, "no_skills")

    @pytest.mark.asyncio
    async def test_update_that_would_empty_the_mirror_rolls_back(self, tmp_path, upstream):
        src = _source(tmp_path, upstream)
        assert (await ss.sync_skill_source(src)).ok
        _git("rm", "-r", "-q", "skills/alpha", "skills/beta", cwd=upstream)
        # git removes the now-empty directory, so recreate it: the subdir must
        # still exist upstream or this would test missing_skill_root instead.
        (upstream / "skills").mkdir(exist_ok=True)
        (upstream / "skills" / "README.md").write_text("skills moved\n", encoding="utf-8")
        _git("add", "-A", cwd=upstream)
        _git("commit", "-q", "-m", "drop all skills", cwd=upstream)

        result = await ss.sync_skill_source(src)
        assert (result.ok, result.error) == (False, "no_skills")
        root = ss.source_skill_root(src.name, src.subdir)
        assert root is not None
        assert ss.count_skills(root) == 2

    @pytest.mark.asyncio
    async def test_fresh_clone_failure_has_nothing_to_restore(self, tmp_path, upstream):
        """The restore path is update-only; a first clone must not try to roll back."""
        src = _source(tmp_path, upstream, name="fresh", subdir="nope")
        result = await ss.sync_skill_source(src)
        assert (result.ok, result.error) == (False, "missing_skill_root")
        assert not any("Restoring" in line for line in result.log)

    @pytest.mark.asyncio
    async def test_bad_branch_fails_clone(self, tmp_path, upstream):
        src = _source(tmp_path, upstream, branch="does-not-exist")
        result = await ss.sync_skill_source(src)
        assert not result.ok
        assert result.error == "clone_failed"
        # A failed fresh clone must not leave a partial mirror behind.
        assert not (ss.skill_sources_dir() / src.name).exists()

    @pytest.mark.asyncio
    async def test_repo_root_as_skill_root(self, tmp_path, upstream):
        """A repo whose SKILL.md trees sit at the root needs no subdir."""
        src = _source(tmp_path, upstream, name="rooted", subdir="")
        result = await ss.sync_skill_source(src)
        assert result.ok, result.log
        # skills/alpha + skills/beta are still found by the recursive walk.
        assert result.skill_count == 2

    @pytest.mark.asyncio
    async def test_sync_all_records_each_outcome(self, tmp_path, upstream):
        good = _source(tmp_path, upstream, name="good")
        bad = _source(tmp_path, upstream, name="bad", branch="nope")
        _persist_sources([good, bad])
        results = await ss.sync_skill_sources([good, bad])
        assert [(r.name, r.ok) for r in results] == [("good", True), ("bad", False)]
        state = ss.read_sync_state()
        assert state["good"]["ok"] is True
        assert state["bad"]["ok"] is False

    @pytest.mark.asyncio
    async def test_sync_all_skips_a_source_unlinked_mid_batch(self, tmp_path, upstream):
        """A batch sync is slow; the config can change while it runs.

        Acting on the opening snapshot would re-clone and re-mount a source the
        user removed part-way through, so each entry is re-read under its lock.
        """
        first = _source(tmp_path, upstream, name="first")
        gone = _source(tmp_path, upstream, name="gone")
        # Only ``first`` is actually configured — ``gone`` stands in for an entry
        # the user unlinked after the batch captured its snapshot.
        _persist_sources([first])
        results = await ss.sync_skill_sources([first, gone])
        assert [r.name for r in results] == ["first"]
        assert not (ss.skill_sources_dir() / "gone").exists()

    @pytest.mark.asyncio
    async def test_sync_all_uses_the_current_repo_not_the_snapshot(self, tmp_path, upstream):
        """An entry edited mid-batch is synced in its CURRENT form."""
        other = tmp_path / "other-upstream"
        (other / "skills" / "delta").mkdir(parents=True)
        (other / "skills" / "delta" / "SKILL.md").write_text(
            "---\nname: delta\ndescription: Delta\n---\nbody\n", encoding="utf-8"
        )
        _git("init", "-q", "-b", "main", cwd=other)
        _git("config", "user.email", "test@example.com", cwd=other)
        _git("config", "user.name", "Test", cwd=other)
        _git("add", "-A", cwd=other)
        _git("commit", "-q", "-m", "seed", cwd=other)

        stale = _source(tmp_path, upstream, name="edited")
        current = _source(tmp_path, upstream, name="edited", repo=f"file://{other}")
        _persist_sources([current])
        results = await ss.sync_skill_sources([stale])
        assert [r.ok for r in results] == [True], results[0].log
        root = ss.source_skill_root("edited", "skills")
        assert root is not None
        assert (root / "delta").is_dir()
        assert not (root / "alpha").exists()

    @pytest.mark.asyncio
    async def test_sync_all_skips_disabled(self, tmp_path, upstream):
        off = _source(tmp_path, upstream, name="off", enabled=False)
        _persist_sources([off])
        assert await ss.sync_skill_sources([off]) == []

    @pytest.mark.asyncio
    async def test_remove_clone_deletes_mirror_and_state(self, tmp_path, upstream):
        src = _source(tmp_path, upstream)
        result = await ss.sync_skill_source(src)
        ss.record_sync_state(result)
        assert ss.remove_skill_source_clone(src.name) is True
        assert not (ss.skill_sources_dir() / src.name).exists()
        assert src.name not in ss.read_sync_state()

    def test_remove_clone_rejects_unsafe_name(self):
        assert ss.remove_skill_source_clone("../../etc") is False


# ---------------------------------------------------------------------------
# loader integration + config round-trip
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group(name="skill_sources_real_git")
class TestLoaderIntegration:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("trust_file_urls")
    async def test_mirrored_skills_are_discovered(self, tmp_path, upstream):
        src = _source(tmp_path, upstream)
        assert (await ss.sync_skill_source(src)).ok

        cfg = KiroCrewConfig()
        cfg.skills.sources = [src]
        loader = _loader_with_sources(tmp_path / "local-skills", cfg)
        names = {s["name"] for s in loader.list_skills()}
        assert {"alpha", "beta"} <= names

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("trust_file_urls")
    async def test_local_skill_wins_on_name_collision(self, tmp_path, upstream):
        from kiro_crew.skills import SkillsLoader

        src = _source(tmp_path, upstream)
        assert (await ss.sync_skill_source(src)).ok

        local = tmp_path / "local-skills"
        (local / "alpha").mkdir(parents=True)
        (local / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: LOCAL alpha\n---\nlocal body\n", encoding="utf-8"
        )
        cfg = KiroCrewConfig()
        cfg.skills.sources = [src]
        loader = SkillsLoader(skills_path=local, install_builtins=False, config=cfg)
        alpha = next(s for s in loader.list_skills() if s["name"] == "alpha")
        assert alpha["description"] == "LOCAL alpha"

    def test_broken_source_list_does_not_break_local_skills(self, tmp_path, monkeypatch):
        """A failure resolving mirrors must never take local skills down with it."""
        from kiro_crew import skills as skills_mod

        monkeypatch.setattr(
            ss, "skill_source_roots", lambda _s: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        local = tmp_path / "local-skills"
        (local / "solo").mkdir(parents=True)
        (local / "solo" / "SKILL.md").write_text(
            "---\nname: solo\ndescription: Solo\n---\nbody\n", encoding="utf-8"
        )
        cfg = KiroCrewConfig()
        cfg.skills.sources = [SkillSourceConfig(name="team", repo="https://github.com/o/r.git")]
        loader = skills_mod.SkillsLoader(skills_path=local, install_builtins=False, config=cfg)
        assert {s["name"] for s in loader.list_skills()} == {"solo"}

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("trust_file_urls")
    async def test_reload_picks_up_a_repo_linked_after_construction(self, tmp_path, upstream):
        """Linking a repo must take effect without a gateway restart.

        The loader is a long-lived singleton that resolves its roots once at
        construction, so this is the guard that a freshly linked repo is not
        invisible until the next start.
        """
        from kiro_crew.skills import SkillsLoader

        local = tmp_path / "local-skills"
        local.mkdir()
        cfg = KiroCrewConfig()
        loader = SkillsLoader(skills_path=local, install_builtins=False, config=cfg)
        assert {s["name"] for s in loader.list_skills()} == set()

        src = _source(tmp_path, upstream)
        assert (await ss.sync_skill_source(src)).ok
        cfg.skills.sources = [src]
        loader.reload_extra_paths(cfg)
        assert {"alpha", "beta"} <= {s["name"] for s in loader.list_skills()}

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("trust_file_urls")
    async def test_reload_drops_an_unlinked_repo(self, tmp_path, upstream):
        src = _source(tmp_path, upstream)
        assert (await ss.sync_skill_source(src)).ok
        cfg = KiroCrewConfig()
        cfg.skills.sources = [src]
        loader = _loader_with_sources(tmp_path / "local-skills", cfg)
        assert {"alpha", "beta"} <= {s["name"] for s in loader.list_skills()}

        cfg.skills.sources = []
        ss.remove_skill_source_clone(src.name)
        loader.reload_extra_paths(cfg)
        assert {s["name"] for s in loader.list_skills()} == set()


@pytest.mark.xdist_group(name="skill_sources_real_git")
class TestReadsToleratePartialMirror:
    """A sync replaces mirror files by unlink+create while readers hold paths."""

    def test_load_skill_returns_none_for_a_vanished_file(self, tmp_path):
        from kiro_crew.skills import SkillsLoader

        local = tmp_path / "local"
        (local / "doomed").mkdir(parents=True)
        skill_file = local / "doomed" / "SKILL.md"
        skill_file.write_text("---\nname: doomed\ndescription: D\n---\nbody\n", encoding="utf-8")
        loader = SkillsLoader(skills_path=local, install_builtins=False, config=KiroCrewConfig())
        assert loader.load_skill("doomed") is not None

        skill_file.unlink()
        # Must report unavailable, not raise FileNotFoundError into the caller.
        assert loader.load_skill("doomed") is None

    def test_load_skill_survives_a_path_that_exists_but_cannot_be_read(self, tmp_path):
        """Deterministic stand-in for the TOCTOU window.

        The real race — file present at ``exists()``, gone at ``read_text()`` — is
        not reproducible single-threaded, so this uses the other shape with the
        same control flow: a path that passes an existence check and then fails
        the read. A directory named ``SKILL.md`` raises ``IsADirectoryError``
        (an ``OSError``) exactly where a concurrent unlink would raise
        ``FileNotFoundError``, so it pins the guard rather than the timing.
        """
        from kiro_crew.skills import SkillsLoader

        local = tmp_path / "local"
        (local / "weird" / "SKILL.md").mkdir(parents=True)
        loader = SkillsLoader(skills_path=local, install_builtins=False, config=KiroCrewConfig())
        assert loader.load_skill("weird") is None

    def test_discovery_survives_an_unreadable_skill_file(self, tmp_path):
        """One bad skill must not take down discovery for the others."""
        from kiro_crew.skills import SkillsLoader

        local = tmp_path / "local"
        (local / "keeper").mkdir(parents=True)
        (local / "keeper" / "SKILL.md").write_text(
            "---\nname: keeper\ndescription: keeper\n---\nbody\n", encoding="utf-8"
        )
        (local / "broken" / "SKILL.md").mkdir(parents=True)
        loader = SkillsLoader(skills_path=local, install_builtins=False, config=KiroCrewConfig())
        listed = loader.list_skills()  # must not raise
        assert "keeper" in {s["name"] for s in listed}

    def test_parse_frontmatter_returns_empty_for_a_missing_file(self, tmp_path):
        from kiro_crew.skills import SkillsLoader

        assert SkillsLoader._parse_frontmatter(tmp_path / "nope" / "SKILL.md") == {}


class TestBoundedTraversal:
    """A linked repo is third-party content walked on the gateway event loop."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("trust_file_urls")
    async def test_construction_does_not_scan_linked_roots(self, tmp_path, upstream, monkeypatch):
        """__init__ can run on the event loop, so it must not walk a mirror.

        ``_get_skills`` builds the standalone loader lazily from a request
        handler, so construction is an on-loop path. Linked skills arrive with
        the first ``reload_extra_paths``, which every caller offloads.
        """
        from kiro_crew import skills as skills_mod

        src = _source(tmp_path, upstream)
        assert (await ss.sync_skill_source(src)).ok
        mirror_root = ss.source_skill_root(src.name, src.subdir)
        assert mirror_root is not None

        walked: list[Path] = []
        real = skills_mod._iter_skill_files
        monkeypatch.setattr(
            skills_mod,
            "_iter_skill_files",
            lambda base, **kw: (walked.append(Path(base)), real(base, **kw))[1],
        )
        cfg = KiroCrewConfig()
        cfg.skills.sources = [src]
        loader = skills_mod.SkillsLoader(
            skills_path=tmp_path / "local", install_builtins=False, config=cfg
        )
        assert mirror_root not in walked, f"construction walked the mirror: {walked}"

        # ...and the reload (always off-loop) is what makes them available.
        loader.reload_extra_paths(cfg)
        assert {"alpha", "beta"} <= {s["name"] for s in loader.list_skills()}

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("trust_file_urls")
    async def test_linked_roots_are_not_walked_during_discovery(
        self, tmp_path, upstream, monkeypatch
    ):
        """Discovery runs on the gateway event loop, so it must not walk a mirror.

        The scan happens once in ``_resolve_extra_paths`` (off-loop for every
        live-reload path); ``list_skills`` must then read the precomputed result
        and touch the filesystem for managed roots only.
        """
        from kiro_crew import skills as skills_mod

        src = _source(tmp_path, upstream)
        assert (await ss.sync_skill_source(src)).ok
        cfg = KiroCrewConfig()
        cfg.skills.sources = [src]
        local = tmp_path / "local"
        local.mkdir()
        loader = _loader_with_sources(local, cfg)
        assert {"alpha", "beta"} <= {s["name"] for s in loader.list_skills()}

        mirror_root = ss.source_skill_root(src.name, src.subdir)
        assert mirror_root is not None
        walked: list[Path] = []
        real = skills_mod._iter_skill_files

        def _spy(base, **kw):
            walked.append(Path(base))
            return real(base, **kw)

        monkeypatch.setattr(skills_mod, "_iter_skill_files", _spy)
        loader._invalidate_iter_cache()
        assert {"alpha", "beta"} <= {s["name"] for s in loader.list_skills()}
        assert mirror_root not in walked, f"linked root was walked on the loop: {walked}"
        assert local in walked, "the managed local root should still be walked"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("trust_file_urls")
    async def test_reload_rescans_after_an_upstream_change(self, tmp_path, upstream):
        """Precomputing is only safe if every mirror write re-runs the scan."""
        src = _source(tmp_path, upstream)
        assert (await ss.sync_skill_source(src)).ok
        cfg = KiroCrewConfig()
        cfg.skills.sources = [src]
        loader = _loader_with_sources(tmp_path / "local", cfg)
        assert {"alpha", "beta"} <= {s["name"] for s in loader.list_skills()}

        (upstream / "skills" / "gamma").mkdir()
        (upstream / "skills" / "gamma" / "SKILL.md").write_text(
            "---\nname: gamma\ndescription: Gamma\n---\nbody\n", encoding="utf-8"
        )
        _git("add", "-A", cwd=upstream)
        _git("commit", "-q", "-m", "add gamma", cwd=upstream)
        assert (await ss.sync_skill_source(src)).ok

        loader.reload_extra_paths(cfg)
        assert "gamma" in {s["name"] for s in loader.list_skills()}

    def test_pruning_makes_cost_track_skill_count_not_file_count(self, tmp_path):
        """A skill bundle's payload must not be walked.

        This is what keeps the on-loop walk bounded on a large linked repo:
        nothing below a SKILL.md can define another skill, so the traversal
        should cost one visit per skill directory regardless of how many files
        each bundle carries.
        """
        from kiro_crew.skills import _iter_skill_files

        base = tmp_path / "repo"
        for i in range(3):
            d = base / f"skill{i}"
            (d / "scripts").mkdir(parents=True)
            (d / "SKILL.md").write_text("x", encoding="utf-8")
            for j in range(400):
                (d / "scripts" / f"f{j}.py").write_text("x", encoding="utf-8")

        # Unpruned, the same budget is consumed by bundle payload and the walk
        # stops before finding every skill; pruned, it finds all of them.
        unpruned = _iter_skill_files(base, max_entries=50)
        pruned = _iter_skill_files(base, max_entries=50, prune_at_skill=True)
        assert len(unpruned) < 3
        assert {name for name, _ in pruned} == {"skill0", "skill1", "skill2"}

    def test_pruning_still_finds_nested_category_skills(self, tmp_path):
        """The documented ``category/skill`` layout must survive pruning."""
        from kiro_crew.skills import _iter_skill_files

        base = tmp_path / "repo"
        (base / "kirocrew-dev" / "babysit").mkdir(parents=True)
        (base / "kirocrew-dev" / "babysit" / "SKILL.md").write_text("x", encoding="utf-8")
        (base / "top").mkdir()
        (base / "top" / "SKILL.md").write_text("x", encoding="utf-8")
        found = {name for name, _ in _iter_skill_files(base, prune_at_skill=True)}
        assert found == {"kirocrew-dev/babysit", "top"}

    def test_walk_is_capped_for_a_linked_root(self, tmp_path):
        from kiro_crew.skills import _iter_skill_files

        base = tmp_path / "big"
        for i in range(60):
            d = base / f"pad{i:03d}"
            d.mkdir(parents=True)
            (d / "filler.txt").write_text("x", encoding="utf-8")
        deep = base / "zzz-last" / "skill-a"
        deep.mkdir(parents=True)
        (deep / "SKILL.md").write_text("x", encoding="utf-8")

        # Unbounded finds it; a tight budget stops before reaching it.
        assert len(_iter_skill_files(base)) == 1
        assert _iter_skill_files(base, max_entries=5) == []

    def test_managed_root_stays_unbounded(self, tmp_path, monkeypatch):
        """Only linked roots are budgeted — the local root's behavior is unchanged."""
        from kiro_crew import skills as skills_mod

        calls: list[int | None] = []
        real = skills_mod._iter_skill_files

        def _spy(base, *, max_entries=None, prune_at_skill=False):
            calls.append((max_entries, prune_at_skill))
            return real(base, max_entries=max_entries, prune_at_skill=prune_at_skill)

        monkeypatch.setattr(skills_mod, "_iter_skill_files", _spy)
        extra = tmp_path / "operator-extra"
        extra.mkdir()
        cfg = KiroCrewConfig()
        cfg.skills.extra_paths = [str(extra)]
        loader = skills_mod.SkillsLoader(
            skills_path=tmp_path / "local", install_builtins=False, config=cfg
        )
        loader.list_skills()
        # Local root + operator extra path: unbounded and unpruned.
        assert calls and all(c == (None, False) for c in calls)

    def test_linked_root_gets_a_budget(self, tmp_path, monkeypatch):
        """A linked root must be walked with both the cap and skill-pruning.

        Builds its own mirror directory and repoints ``skill_sources_dir`` at it
        instead of syncing a real repo into the session-shared home. The earlier
        real-git version was order-dependent: the isolated ``KIROCREW_HOME`` is
        per-worker, not per-test, so another test removing a mirror could leave
        this one with no linked root to scan and no budget to observe — it failed
        in roughly half of full-suite runs. Nothing here needs git; the assertion
        is about which arguments the walker receives.
        """
        from kiro_crew import skills as skills_mod

        monkeypatch.setattr(ss, "skill_sources_dir", lambda: tmp_path / "skill-sources")
        mirror = tmp_path / "skill-sources" / "budgeted" / "skills"
        (mirror / "alpha").mkdir(parents=True)
        (mirror / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Alpha\n---\nbody\n", encoding="utf-8"
        )

        budgets: list = []
        real = skills_mod._iter_skill_files

        def _spy(base, *, max_entries=None, prune_at_skill=False):
            budgets.append((max_entries, prune_at_skill))
            return real(base, max_entries=max_entries, prune_at_skill=prune_at_skill)

        monkeypatch.setattr(skills_mod, "_iter_skill_files", _spy)
        cfg = KiroCrewConfig()
        cfg.skills.sources = [
            SkillSourceConfig(name="budgeted", repo="https://github.com/o/r.git", subdir="skills")
        ]
        loader = skills_mod.SkillsLoader(
            skills_path=tmp_path / "local", install_builtins=False, config=cfg
        )
        # The scan runs in reload_extra_paths (off-loop), not in list_skills.
        loader.reload_extra_paths(cfg)
        loader.list_skills()
        # The linked root is both budgeted and pruned at skill boundaries...
        assert (skills_mod._LINKED_ROOT_MAX_ENTRIES, True) in budgets
        # ...and the managed local root is neither.
        assert (None, False) in budgets


class TestLinkedRootSymlinkContainment:
    """A linked repo is third-party content and can commit symlinks."""

    def test_skill_symlink_escaping_the_mirror_is_skipped(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(ss, "skill_sources_dir", lambda: home / "skill-sources")
        mirror = home / "skill-sources" / "team" / "skills"
        (mirror / "honest").mkdir(parents=True)
        (mirror / "honest" / "SKILL.md").write_text(
            "---\nname: honest\ndescription: Honest\n---\nbody\n", encoding="utf-8"
        )
        # A committed symlink pointing at a file outside the mirror. Skill
        # discovery must not turn this into an arbitrary-file read.
        secret = tmp_path / "outside-secret.json"
        secret.write_text('{"api_key": "should-never-load"}\n', encoding="utf-8")
        (mirror / "leak").mkdir()
        (mirror / "leak" / "SKILL.md").symlink_to(secret)

        cfg = KiroCrewConfig()
        cfg.skills.sources = [SkillSourceConfig(name="team", repo="r", subdir="skills")]
        loader = _loader_with_sources(tmp_path / "local", cfg)
        names = {s["name"] for s in loader.list_skills()}
        assert "honest" in names
        assert "leak" not in names

    def test_symlink_inside_the_mirror_still_loads(self, tmp_path, monkeypatch):
        """Containment must not reject a repo's own internal symlinks.

        Asserted on ``_iter_uncached`` (relative-path keys) rather than
        ``list_skills``, because two entries resolving to the same SKILL.md
        share a frontmatter name and would collapse in the public view — that
        would hide whether containment accepted the entry, which is the thing
        under test.
        """
        home = tmp_path / "home"
        monkeypatch.setattr(ss, "skill_sources_dir", lambda: home / "skill-sources")
        mirror = home / "skill-sources" / "team" / "skills"
        (mirror / "real").mkdir(parents=True)
        (mirror / "real" / "SKILL.md").write_text(
            "---\nname: real\ndescription: Real\n---\nbody\n", encoding="utf-8"
        )
        (mirror / "aliased").mkdir()
        (mirror / "aliased" / "SKILL.md").symlink_to(mirror / "real" / "SKILL.md")

        cfg = KiroCrewConfig()
        cfg.skills.sources = [SkillSourceConfig(name="team", repo="r", subdir="skills")]
        loader = _loader_with_sources(tmp_path / "local", cfg)
        assert {"real", "aliased"} <= {name for name, _ in loader._iter_uncached()}

    def test_managed_roots_keep_their_prior_symlink_behavior(self, tmp_path):
        """The containment check is scoped to linked roots only.

        A symlinked SKILL.md *file* pointing outside an operator-configured
        ``extra_paths`` root still loads, exactly as it did before this change —
        the new restriction must not silently tighten existing installs. (A
        symlinked *directory* resolving outside its base was already pruned by
        ``_iter_skill_files``; that behavior is unchanged too.)
        """
        from kiro_crew import skills as skills_mod

        outside = tmp_path / "outside" / "provided"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text(
            "---\nname: provided\ndescription: Provided\n---\nbody\n", encoding="utf-8"
        )
        extra = tmp_path / "extra"
        (extra / "provided").mkdir(parents=True)
        (extra / "provided" / "SKILL.md").symlink_to(outside / "SKILL.md")

        cfg = KiroCrewConfig()
        cfg.skills.extra_paths = [str(extra)]
        loader = skills_mod.SkillsLoader(
            skills_path=tmp_path / "local", install_builtins=False, config=cfg
        )
        assert "provided" in {s["name"] for s in loader.list_skills()}

    def test_load_skill_refuses_an_escaping_symlink(self, tmp_path, monkeypatch):
        """Containment must hold on the by-name path, not just discovery.

        ``load_skill`` reaches files directly rather than through
        ``_iter_uncached``, so a ``$skillname`` / skill_search lookup bypasses the
        discovery-time check entirely. Closing only discovery left the escape
        reachable: the skill stays invisible in the list yet still returns the
        symlink target's contents on demand.
        """
        from kiro_crew import skills as skills_mod

        home = tmp_path / "home"
        monkeypatch.setattr(ss, "skill_sources_dir", lambda: home / "skill-sources")
        mirror = home / "skill-sources" / "team" / "skills"
        mirror.mkdir(parents=True)
        secret = tmp_path / "outside-secret.json"
        secret.write_text('{"api_key": "should-never-load"}\n', encoding="utf-8")
        (mirror / "leak").mkdir()
        (mirror / "leak" / "SKILL.md").symlink_to(secret)

        cfg = KiroCrewConfig()
        cfg.skills.sources = [SkillSourceConfig(name="team", repo="r", subdir="skills")]
        loader = skills_mod.SkillsLoader(
            skills_path=tmp_path / "local", install_builtins=False, config=cfg
        )
        loader.reload_extra_paths(cfg)
        assert loader.load_skill("leak") is None

    def test_load_skill_still_serves_a_contained_linked_skill(self, tmp_path, monkeypatch):
        """The containment check must not block legitimate linked skills."""
        from kiro_crew import skills as skills_mod

        home = tmp_path / "home"
        monkeypatch.setattr(ss, "skill_sources_dir", lambda: home / "skill-sources")
        mirror = home / "skill-sources" / "team" / "skills"
        (mirror / "honest").mkdir(parents=True)
        (mirror / "honest" / "SKILL.md").write_text(
            "---\nname: honest\ndescription: Honest\n---\nreal body\n", encoding="utf-8"
        )

        cfg = KiroCrewConfig()
        cfg.skills.sources = [SkillSourceConfig(name="team", repo="r", subdir="skills")]
        loader = skills_mod.SkillsLoader(
            skills_path=tmp_path / "local", install_builtins=False, config=cfg
        )
        loader.reload_extra_paths(cfg)
        body = loader.load_skill("honest")
        assert body is not None and "real body" in body

    def test_count_skills_ignores_escaping_symlinks(self, tmp_path):
        """Link-time validation must agree with what the loader will expose.

        Counting an escaping symlink let a repo whose only "skills" were such
        links report a positive count and link successfully while contributing
        nothing loadable.
        """
        root = tmp_path / "mirror"
        (root / "real").mkdir(parents=True)
        (root / "real" / "SKILL.md").write_text("x", encoding="utf-8")
        outside = tmp_path / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        (root / "leak").mkdir()
        (root / "leak" / "SKILL.md").symlink_to(outside)

        assert ss.count_skills(root) == 1

    def test_count_skills_ignores_a_directory_named_skill_md(self, tmp_path):
        """Only readable files count — a directory of that name is not a skill."""
        root = tmp_path / "mirror"
        (root / "weird" / "SKILL.md").mkdir(parents=True)
        assert ss.count_skills(root) == 0

    def test_is_within_accepts_the_root_and_paths_under_it(self, tmp_path):
        from kiro_crew.skill_sources import is_within_root as _is_within

        root = tmp_path / "mirror"
        (root / "a").mkdir(parents=True)
        root_real = os.path.realpath(root)
        assert _is_within(str(root / "a"), root_real) is True
        assert _is_within(str(root), root_real) is True

    def test_is_within_rejects_a_sibling_sharing_a_name_prefix(self, tmp_path):
        """A string-prefix comparison would wrongly accept ``mirror-evil``."""
        from kiro_crew.skill_sources import is_within_root as _is_within

        root = tmp_path / "mirror"
        root.mkdir()
        sibling = tmp_path / "mirror-evil"
        (sibling / "a").mkdir(parents=True)
        assert _is_within(str(sibling / "a"), os.path.realpath(root)) is False

    def test_is_within_rejects_an_unrelated_path(self, tmp_path):
        from kiro_crew.skill_sources import is_within_root as _is_within

        root = tmp_path / "mirror"
        root.mkdir()
        outside = tmp_path / "outside" / "secret"
        outside.mkdir(parents=True)
        assert _is_within(str(outside), os.path.realpath(root)) is False

    def test_is_within_allows_everything_when_unrestricted(self):
        from kiro_crew.skill_sources import is_within_root as _is_within

        assert _is_within(str(Path.cwd()), "") is True


class TestBoundedReads:
    """Reads happen on the event loop, so third-party file size must be bounded."""

    def test_read_helper_stops_at_the_cap(self, tmp_path):
        from kiro_crew.skills import _read_text_if_present

        big = tmp_path / "big.md"
        big.write_text("x" * 5_000, encoding="utf-8")
        assert len(_read_text_if_present(big, max_bytes=100)) == 100
        assert len(_read_text_if_present(big)) == 5_000

    def test_read_helper_tolerates_a_cap_mid_codepoint(self, tmp_path):
        """A cap can land inside a multi-byte character; that must not raise."""
        from kiro_crew.skills import _read_text_if_present

        f = tmp_path / "utf.md"
        f.write_text("é" * 50, encoding="utf-8")  # 2 bytes each
        out = _read_text_if_present(f, max_bytes=5)
        assert out is not None and len(out) > 0

    def test_bounded_read_normalizes_crlf_like_read_text(self, tmp_path):
        """The bounded path must apply universal-newline translation.

        A CRLF checkout is the normal case on Windows. Reading bytes and decoding
        by hand skips the translation ``read_text`` performs, which leaves
        ``---\\r\\n`` and makes the frontmatter regex fail — silently stripping
        every skill's metadata. Asserted here because it only reproduces on a
        CRLF file, which a POSIX checkout never produces on its own.
        """
        from kiro_crew.skills import SkillsLoader, _read_text_if_present

        f = tmp_path / "crlf.md"
        f.write_bytes(b"---\r\nname: crlf\r\ndescription: CRLF skill\r\n---\r\nbody\r\n")
        bounded = _read_text_if_present(f, max_bytes=10_000)
        unbounded = _read_text_if_present(f)
        assert bounded == unbounded
        assert "\r" not in bounded
        # ...and the frontmatter parser (which uses the bounded path) still works.
        meta = SkillsLoader._parse_frontmatter(f)
        assert meta.get("description") == "CRLF skill"

    def test_bounded_read_reports_invalid_utf8_as_unreadable(self, tmp_path):
        """Strict decoding must survive the cap — callers rely on None here."""
        from kiro_crew.skills import _read_text_if_present

        f = tmp_path / "bad.md"
        f.write_bytes(b"---\nname: bad\n---\n\xff\xfe not utf-8\n")
        assert _read_text_if_present(f, max_bytes=10_000) is None

    def test_frontmatter_is_parsed_from_a_capped_head(self, tmp_path):
        """Frontmatter is at the top, so the cap must not lose it."""
        from kiro_crew.skills import SkillsLoader

        f = tmp_path / "SKILL.md"
        f.write_text(
            "---\nname: huge\ndescription: Huge skill\n---\n" + ("filler\n" * 200_000),
            encoding="utf-8",
        )
        meta = SkillsLoader._parse_frontmatter(f)
        assert meta.get("name") == "huge"
        assert meta.get("description") == "Huge skill"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("trust_file_urls")
    async def test_oversized_linked_skill_body_is_capped(self, tmp_path, upstream, monkeypatch):
        from kiro_crew import skills as skills_mod

        # Make the cap small so the test does not need a real megabyte file.
        monkeypatch.setattr(skills_mod, "_LINKED_SKILL_MAX_BYTES", 500)
        # Own source name: this test rewrites a file inside the mirror, and the
        # isolated KIROCREW_HOME is per-session, so reusing the default name
        # would leak mutated content into any other test using that mirror.
        src = _source(tmp_path, upstream, name="capped-body")
        assert (await ss.sync_skill_source(src)).ok
        root = ss.source_skill_root(src.name, src.subdir)
        assert root is not None
        (root / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Alpha\n---\n" + ("y" * 10_000), encoding="utf-8"
        )

        cfg = KiroCrewConfig()
        cfg.skills.sources = [src]
        loader = _loader_with_sources(tmp_path / "local", cfg)
        body = loader.load_skill("alpha")
        assert body is not None
        assert len(body) <= 500

    def test_managed_root_body_is_not_capped(self, tmp_path):
        """The cap is scoped to linked roots; local skills read in full."""
        from kiro_crew.skills import SkillsLoader

        local = tmp_path / "local"
        (local / "mine").mkdir(parents=True)
        (local / "mine" / "SKILL.md").write_text(
            "---\nname: mine\ndescription: Mine\n---\n" + ("z" * 200_000), encoding="utf-8"
        )
        loader = SkillsLoader(skills_path=local, install_builtins=False, config=KiroCrewConfig())
        body = loader.load_skill("mine")
        assert body is not None and len(body) > 100_000


class TestGitCancellation:
    @pytest.mark.asyncio
    async def test_cancellation_kills_the_git_process_group(self, monkeypatch):
        """A cancelled task must not leave git rewriting the mirror.

        Otherwise a gateway shutdown mid-``reset --hard`` leaves a partial tree
        that the next start would mount.
        """
        killed: list[object] = []

        class _Proc:
            pid = 4242
            returncode = None

            async def communicate(self):
                raise asyncio.CancelledError()

        async def _fake_exec(*a, **kw):
            return _Proc()

        async def _fake_kill(proc):
            killed.append(proc)

        monkeypatch.setattr(ss.asyncio, "create_subprocess_exec", _fake_exec)
        monkeypatch.setattr(ss, "_kill_process_group", _fake_kill)
        monkeypatch.setattr(ss, "wrap_argv", lambda argv, mode="standard", **kw: (list(argv), None))
        monkeypatch.setattr(ss, "cgroup_scope_argv", lambda argv: argv)

        with pytest.raises(asyncio.CancelledError):
            await ss._run_git(
                ["fetch"], cwd=None, sandbox_mode="strict", env={}, timeout=5
            )
        assert len(killed) == 1, "the git process group was not killed on cancellation"


class TestConfigRoundTrip:
    """``skills.sources`` must survive the real disk round-trip.

    Uses ``load()`` against a seeded ``config.json`` rather than a synthetic
    deserializer, because ``load()`` is the only path production ever takes and
    it is where the per-entry filtering lives.
    """

    @staticmethod
    def _load(tmp_path, data: dict) -> KiroCrewConfig:
        (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_dir", return_value=tmp_path
        ):
            return KiroCrewConfig.load()

    def test_sources_survive_save_and_load(self, tmp_path):
        cfg = KiroCrewConfig()
        cfg.skills.sources = [
            SkillSourceConfig(
                name="team-skills",
                repo="https://github.com/org/team-skills.git",
                branch="trunk",
                subdir="skills",
                enabled=False,
            )
        ]
        data = cfg.to_dict()
        assert data["skills"]["sources"][0]["branch"] == "trunk"
        restored = self._load(tmp_path, data)
        assert len(restored.skills.sources) == 1
        got = restored.skills.sources[0]
        assert (got.name, got.repo, got.branch, got.subdir, got.enabled) == (
            "team-skills",
            "https://github.com/org/team-skills.git",
            "trunk",
            "skills",
            False,
        )

    def test_entries_without_name_or_repo_are_dropped(self, tmp_path):
        restored = self._load(
            tmp_path,
            {
                "skills": {
                    "sources": [
                        {"name": "ok", "repo": "https://github.com/o/r.git"},
                        {"name": "", "repo": "https://github.com/o/r.git"},
                        {"name": "no-repo", "repo": ""},
                        "not-a-dict",
                    ]
                }
            },
        )
        assert [s.name for s in restored.skills.sources] == ["ok"]

    def test_branch_defaults_to_main(self, tmp_path):
        restored = self._load(
            tmp_path,
            {"skills": {"sources": [{"name": "ok", "repo": "https://github.com/o/r.git"}]}},
        )
        assert restored.skills.sources[0].branch == "main"

    def test_non_list_sources_tolerated(self, tmp_path):
        restored = self._load(tmp_path, {"skills": {"sources": "nonsense"}})
        assert restored.skills.sources == []
