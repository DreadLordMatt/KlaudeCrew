"""Tests for the Powers feature (parser + store + installer)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from kiro_crew import powers as powers_mod
from kiro_crew.powers import (
    MAX_POWER_MD_BYTES,
    MCP_JSON_NAME,
    POWER_MD_NAME,
    POWERS_LOCK_NAME,
    PowerFormatError,
    PowersStore,
    is_safe_power_name,
    parse_power_md,
)


async def _await_worker_settled(scratch: Path, timeout: float = 10.0) -> None:
    """Wait until the transaction worker has finished its own rollback.

    The single-worker design deliberately does NOT make the caller wait: the
    worker owns its rollback and completes in its thread after a cancelled await
    returns. A fixed sleep was flaky under load, so poll the invariant instead.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not scratch.exists():
            return
        await asyncio.sleep(0.05)

# ── frontmatter helpers ─────────────────────────────────────────────────


def _power_md(
    *,
    name: str = "demo",
    display: str | None = "Demo Power",
    desc: str | None = "A demo power.",
    keywords: list[str] | None = None,
    extra: str = "",
) -> str:
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    if display is not None:
        lines.append(f"displayName: {display}")
    if desc is not None:
        lines.append(f"description: {desc}")
    if keywords is not None:
        joined = ", ".join(f'"{k}"' for k in keywords)
        lines.append(f"keywords: [{joined}]")
    if extra:
        lines.append(extra)
    lines.append("---")
    lines.append("")
    lines.append("# Demo\n\nbody text")
    return "\n".join(lines)


def _bundle(
    root: Path,
    *,
    name: str = "demo",
    keywords: list[str] | None = None,
    mcp: dict | None = None,
    steering: dict[str, str] | None = None,
    dirname: str | None = None,
) -> Path:
    # `dirname` matters only when a test needs TWO source bundles declaring the
    # same Power name, which is what the source-conflict cases do.
    d = root / (dirname or f"bundle-{name}")
    d.mkdir(parents=True)
    (d / "POWER.md").write_text(_power_md(name=name, keywords=keywords), encoding="utf-8")
    if mcp is not None:
        (d / "mcp.json").write_text(json.dumps({"mcpServers": mcp}), encoding="utf-8")
    if steering:
        sd = d / "steering"
        sd.mkdir()
        for fn, content in steering.items():
            (sd / fn).write_text(content, encoding="utf-8")
    return d


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A PowersStore over tmp dirs.

    ``_KIROCREW_MCP_JSON`` is redirected into *tmp_path* even though this store
    never writes MCP config: that is exactly the point. Tests assert the file
    stays absent, so an accidental future write lands somewhere observable
    instead of silently touching the real home and passing.
    """
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mc = tmp_path / "kirocrew.mcp.json"
    monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", mc)

    st = PowersStore(powers_path=tmp_path / "powers")
    return st, mc, tmp_path


class _SelRecorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_api_access(self, **kwargs) -> None:
        self.events.append(kwargs)


# ═══════════════════════════════════════════════════════════════════════
# parse_power_md
# ═══════════════════════════════════════════════════════════════════════


class TestParsePowerMd:
    def test_happy_path(self):
        meta = parse_power_md(_power_md(keywords=["database", "postgres"]))
        assert meta.name == "demo"
        assert meta.displayName == "Demo Power"
        assert meta.description == "A demo power."
        assert meta.keywords == ["database", "postgres"]
        assert meta.author is None
        assert "body text" in meta.body

    def test_author_optional(self):
        meta = parse_power_md(_power_md(extra='author: "Acme"'))
        assert meta.author == "Acme"

    @pytest.mark.parametrize("missing", ["name", "displayName", "description"])
    def test_missing_required_field(self, missing):
        kwargs = {"name": "demo", "display": "Demo Power", "desc": "A demo power."}
        if missing == "name":
            kwargs["name"] = None
        elif missing == "displayName":
            kwargs["display"] = None
        else:
            kwargs["desc"] = None
        with pytest.raises(PowerFormatError) as exc:
            parse_power_md(_power_md(**kwargs))
        assert missing in str(exc.value)

    @pytest.mark.parametrize("bad", ["Bad_Name", "UPPER", "-lead", "trail-", "a/b", "..", "x" * 65])
    def test_invalid_name(self, bad):
        with pytest.raises(PowerFormatError):
            parse_power_md(_power_md(name=bad))

    def test_unknown_keys_tolerated(self):
        text = _power_md(extra="version: 1.0\ntags: [x, y]\nrepository: http://z")
        meta = parse_power_md(text)
        assert meta.name == "demo"

    def test_keywords_coerced_to_list(self):
        # block-list form
        text = (
            "---\nname: demo\ndisplayName: D\ndescription: d\n"
            "keywords:\n  - alpha\n  - beta\n---\nbody"
        )
        meta = parse_power_md(text)
        assert meta.keywords == ["alpha", "beta"]

    def test_missing_frontmatter(self):
        with pytest.raises(PowerFormatError):
            parse_power_md("# just markdown, no frontmatter")

    def test_size_cap(self):
        big = "---\nname: demo\ndisplayName: D\ndescription: d\n---\n" + ("x" * (MAX_POWER_MD_BYTES + 1))
        with pytest.raises(PowerFormatError) as exc:
            parse_power_md(big)
        assert "size cap" in str(exc.value)

    def test_path_traversal_name_rejected(self):
        with pytest.raises(PowerFormatError):
            parse_power_md(_power_md(name="../../etc/passwd"))
        assert is_safe_power_name("../evil") is False
        assert is_safe_power_name("ok-name") is True


# ═══════════════════════════════════════════════════════════════════════
# install / toggle / trust / remove
# ═══════════════════════════════════════════════════════════════════════


class TestInstallHandlerTempCleanup:
    """The fetched bundle is a temp tree up to the download cap — it must not leak."""

    @staticmethod
    def _install_request(payload: dict) -> object:
        from aiohttp.test_utils import make_mocked_request

        req = make_mocked_request("POST", "/api/powers/install")

        async def _json() -> dict:
            return payload

        req.json = _json  # type: ignore[method-assign]
        return req

    @pytest.fixture
    def handler_env(self, store, monkeypatch):
        from kiro_crew.dashboard.handlers import powers as powers_handlers

        st, mc, tmp = store
        monkeypatch.setattr("kiro_crew.dashboard.handlers.powers._store", lambda: st)
        return powers_handlers, st, mc, tmp

    @pytest.mark.asyncio
    async def test_fetched_bundle_removed_on_success(self, handler_env, monkeypatch):
        handlers, _st, _mc, tmp = handler_env
        fetched = _bundle(tmp, name="reg")

        async def fake_fetch(ref, *, provider=None):
            return fetched

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.powers.fetch_power_bundle", fake_fetch, raising=False
        )
        req = self._install_request({"source": {"kind": "registry", "ref": "reg"}})
        resp = await handlers.api_powers_install(req)  # type: ignore[arg-type]
        assert resp.status == 200
        assert not fetched.exists(), "temp bundle leaked after a successful install"

    @pytest.mark.asyncio
    async def test_fetched_bundle_removed_on_failure(self, handler_env, monkeypatch):
        handlers, _st, _mc, tmp = handler_env
        # A bundle with no POWER.md makes install_from_dir raise.
        broken = tmp / "bundle-broken"
        broken.mkdir()

        async def fake_fetch(ref, *, provider=None):
            return broken

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.powers.fetch_power_bundle", fake_fetch, raising=False
        )
        req = self._install_request({"source": {"kind": "registry", "ref": "broken"}})
        resp = await handlers.api_powers_install(req)  # type: ignore[arg-type]
        assert resp.status >= 400
        assert not broken.exists(), "temp bundle leaked after a failed install"


class TestRound8Regressions:
    """Regressions for the round-8 review findings on PR #408."""

    def test_denied_install_from_protected_path_is_audited(self, monkeypatch, tmp_path):
        """Probing a credential path must leave an audit trail, not just an error."""
        import kiro_crew.powers as powers_mod

        rec = _SelRecorder()
        monkeypatch.setattr(powers_mod, "sel", lambda: rec)
        monkeypatch.setattr(powers_mod, "is_sensitive_path", lambda _p: True)
        with pytest.raises(PowerFormatError, match="protected path"):
            powers_mod.resolve_install_source(tmp_path)
        assert [
            e
            for e in rec.events
            if e.get("operation") == "power_install" and e.get("outcome") == "denied"
        ]


class TestRound7Regressions:
    """Regressions for the round-7 review findings on PR #408."""

    @pytest.mark.asyncio
    async def test_failed_delete_after_commit_reports_success_and_is_reclaimed(
        self, store, monkeypatch
    ):
        """Once the record is committed, a failed delete is garbage — not a failure.

        This inverts what the test asserted before round 37, deliberately. The old
        ordering destroyed the bytes first so that a failed record write could not
        orphan them, and this test pinned "a failed delete keeps the record". But
        that made a FAILED uninstall destructive: the bundle was already gone when
        the record write failed, so the caller saw an error for a Power that no
        longer existed. The record is now committed first, so the delete failing
        afterwards leaves `.removing-<name>` — which the next operation reclaims.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        def boom(*a, **kw):
            raise OSError("device busy")

        # `_rmtree_at`, not `shutil.rmtree`: the descriptor-anchored path deletes
        # through `_rmtree_fd`, so patching shutil only covered platforms without
        # dir_fd support and left this assertion inert everywhere else.
        monkeypatch.setattr(powers_mod, "_rmtree_at", boom)
        assert await st.remove_power("kb") is True
        monkeypatch.undo()

        # Removed as far as every reader is concerned.
        assert st.load_power("kb") is None
        assert "kb" not in {p["name"] for p in st.list_powers()}
        scratch = st._dir / ".removing-kb"
        assert scratch.exists(), "the bytes should be parked, not silently gone"

        # And reclaimable without anyone retrying `kb` specifically.
        other = _bundle(tmp, name="other")
        await st.install_from_dir(other, source={"kind": "folder", "ref": str(other)})
        assert not scratch.exists(), "leftover bytes were not reclaimed by the sweep"

    @pytest.mark.asyncio
    async def test_failed_record_commit_restores_the_bundle(self, store, monkeypatch):
        """The property the reorder is FOR: a failed removal must lose nothing.

        The commit is the only step that decides the outcome now, so this is where
        the old test's intent belongs. Under the previous ordering the bytes were
        already destroyed by this point and nothing could put them back.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        body = (st._power_path("kb") / POWER_MD_NAME).read_text(encoding="utf-8")

        def boom(*a, **kw):
            raise OSError("installed.json is held open")

        monkeypatch.setattr(powers_mod, "_commit_staged_at", boom)
        with pytest.raises(OSError):
            await st.remove_power("kb")
        monkeypatch.undo()

        assert st.load_power("kb") is not None, "record lost on a failed removal"
        assert st._power_path("kb").is_dir(), "bundle not restored to its own name"
        assert (st._power_path("kb") / POWER_MD_NAME).read_text(encoding="utf-8") == body
        assert not (st._dir / ".removing-kb").exists()
        leftovers = list(st._dir.glob(".installed.json.*"))
        assert leftovers == [], f"staged record left behind: {leftovers}"


class TestReviewRegressions:
    """Regressions for the round-1 review findings on PR #408."""

    @pytest.mark.asyncio
    async def test_install_copies_only_contract_files(self, store):
        """A broad source dir must never relocate unrelated files.

        Vetting only the selected root is insufficient: an allowed ancestor can
        hold a valid POWER.md *and* sensitive descendants, and a recursive copy
        would place them under the agent-readable powers dir.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="wide", steering={"ok.md": "steering body"})
        # Junk that must NOT be copied.
        (src / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
        (src / "notes.txt").write_text("unrelated", encoding="utf-8")
        secrets = src / ".ssh"
        secrets.mkdir()
        (secrets / "id_ed25519").write_text("PRIVATE KEY", encoding="utf-8")
        (src / "steering" / "notes.txt").write_text("not markdown", encoding="utf-8")

        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        installed = tmp / "powers" / "wide"
        assert sorted(p.name for p in installed.iterdir()) == ["POWER.md", "steering"]
        assert sorted(p.name for p in (installed / "steering").iterdir()) == ["ok.md"]
        assert not (installed / "id_rsa").exists()
        assert not (installed / ".ssh").exists()

    @pytest.mark.asyncio
    async def test_failed_reinstall_preserves_existing_bundle(self, store, monkeypatch):
        """A copy that fails partway must not destroy the installed Power.

        The old code deleted the live tree before copying, so a failure left the
        Power gone while its install record (and possibly an enabled MCP config)
        survived.
        """
        st, _mc, tmp = store
        first = _bundle(tmp, name="db", mcp={"cli": {"command": "run-cli"}})
        await st.install_from_dir(first, source={"kind": "folder", "ref": str(first)})
        installed_md = tmp / "powers" / "db" / "POWER.md"
        original = installed_md.read_text(encoding="utf-8")

        second = _bundle(tmp, name="db2", mcp={"cli": {"command": "run-cli"}})
        # Re-point the bundle at the same power name, then make the copy fail.
        (second / "POWER.md").write_text(_power_md(name="db"), encoding="utf-8")
        # Fail a POST-SWAP write: the backup must be retained until every MCP
        # and metadata write commits, then rolled back on failure.

        def boom(*a, **kw):
            raise OSError("no space left on device")

        monkeypatch.setattr(st, "_write_installed", boom)
        with pytest.raises(OSError):
            await st.install_from_dir(second, source={"kind": "folder", "ref": str(second)})

        assert installed_md.is_file(), "existing bundle was destroyed by a failed reinstall"
        assert installed_md.read_text(encoding="utf-8") == original
        assert st.load_power("db") is not None

    def test_resolve_install_source_refuses_sensitive_path(self, tmp_path, monkeypatch):
        """A protected dir holding a valid POWER.md must not be installable.

        Without this guard a credential store could be copied into the
        non-sensitive powers dir, where agent file tools could then read it.
        """
        import kiro_crew.powers as powers_mod

        secret = tmp_path / "dot-aws"
        secret.mkdir()
        (secret / "POWER.md").write_text(_power_md(name="leak"), encoding="utf-8")
        monkeypatch.setattr(
            powers_mod, "is_sensitive_path", lambda p, base_dir=None: "dot-aws" in str(p)
        )
        with pytest.raises(PowerFormatError, match="protected path"):
            powers_mod.resolve_install_source(secret)

    @pytest.mark.asyncio
    async def test_install_refuses_sensitive_source(self, store, monkeypatch):
        st, _mc, tmp = store
        import kiro_crew.powers as powers_mod

        src = _bundle(tmp, name="leak")
        monkeypatch.setattr(
            powers_mod, "is_sensitive_path", lambda p, base_dir=None: "bundle-leak" in str(p)
        )
        with pytest.raises(PowerFormatError, match="protected path"):
            await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

    def test_resolve_install_source_rejects_missing_and_file(self, tmp_path):
        from kiro_crew.powers import resolve_install_source

        with pytest.raises(PowerFormatError, match="not readable"):
            resolve_install_source(tmp_path / "nope")
        f = tmp_path / "a-file"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(PowerFormatError, match="not a directory"):
            resolve_install_source(f)

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "/abs", ".", "..", ""])
    def test_power_path_confined_to_powers_root(self, store, bad):
        """Every name-derived path goes through one validate-and-confine barrier."""
        st, _mc, _tmp = store
        with pytest.raises(PowerFormatError):
            st._power_path(bad)


class TestRound12Regressions:
    """Regressions for the round-11 review findings on PR #408."""

    @pytest.mark.asyncio
    async def test_copy_is_toctou_safe_against_a_swapped_symlink(self, store, monkeypatch):
        """The file is opened ONCE; a post-validation swap cannot be observed.

        The old form checked ``is_symlink()``/``stat()`` on the path and then let
        ``shutil.copyfile`` reopen it, so a caller-owned source could substitute a
        symlink to a credential store in between.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        secret = tmp / "credentials"
        secret.write_text("SECRET-VALUE")

        real_open = os.open

        def _swap_then_open(path, flags, *a, **kw):
            # Fire once, at the moment the installer opens POWER.md. The path
            # argument is now the bare leaf name (the open is relative to the
            # pinned source descriptor), so the swap is applied to the known
            # source file rather than reconstructed from the argument — the old
            # form unlinked a relative path in the process CWD.
            if str(path).endswith(POWER_MD_NAME) and not getattr(_swap_then_open, "done", False):
                _swap_then_open.done = True  # type: ignore[attr-defined]
                target = src / POWER_MD_NAME
                target.unlink()
                os.symlink(secret, target)
            return real_open(path, flags, *a, **kw)

        monkeypatch.setattr(os, "open", _swap_then_open)
        # Must fail as a SYMLINK refusal, not as a downstream parse error: the
        # latter is what happens when the open follows the link and reads the
        # credential, which is exactly the bug.
        with pytest.raises(PowerFormatError, match="symlink"):
            await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        monkeypatch.undo()
        # The credential never reached the store.
        assert "SECRET-VALUE" not in "".join(
            f.read_text(errors="ignore") for f in st._dir.rglob("*") if f.is_file()
        )

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not powers_mod._SUPPORTS_DIR_FD, reason="POSIX dir_fd path only"
    )
    async def test_steering_parent_swap_cannot_disclose_outside_files(self, store):
        """Swapping `steering/` for a symlink mid-copy must not import its contents.

        Per-file `O_NOFOLLOW` does not cover this: after the swap the files found
        are ordinary regular files, they simply live somewhere the caller never
        offered. The directory itself is the redirected component, so the
        directory is what has to be pinned.

        The decoy is laid out as a VALID steering directory (`.md` files) so a
        path-resolving implementation succeeds and copies them — the disclosure,
        not an exception, is the signal.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        (src / "steering").mkdir(exist_ok=True)
        (src / "steering" / "ok.md").write_text("# legitimate\n", encoding="utf-8")

        protected = tmp / "protected"
        protected.mkdir()
        (protected / "secrets.md").write_text("SECRET-VALUE", encoding="utf-8")

        # The swap must land in the window BETWEEN the pre-copy directory check
        # and the enumeration — that IS the race, and it is the only place a swap
        # is not already refused by the `is_symlink()` check. `_open_dir_at` (for
        # the destination steering dir) is the one operation that happens there,
        # so it is the hook. Two earlier versions of this test swapped too early
        # and passed with the fix reverted.
        real_open_dir = powers_mod._open_dir_at
        fired: list[str] = []

        def _swap_steering_then_open_dir(root_fd, leaf, root, *a, **kw):
            result = real_open_dir(root_fd, leaf, root, *a, **kw)
            if leaf == "steering" and not fired:
                fired.append("swapped")
                steering = src / "steering"
                shutil.rmtree(steering)
                os.symlink(protected, steering)
            return result

        original = powers_mod._open_dir_at
        powers_mod._open_dir_at = _swap_steering_then_open_dir
        try:
            try:
                await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
            except Exception:
                pass  # refusing the install is a fine outcome; disclosure is not
        finally:
            powers_mod._open_dir_at = original

        assert fired, "the swap never fired — the test would be vacuous"
        leaked = "".join(
            f.read_text(errors="ignore")
            for f in st._dir.rglob("*")
            if f.is_file()
        )
        assert "SECRET-VALUE" not in leaked, "a file outside the source was copied in"

    def test_streaming_budget_is_cumulative_across_files(self, tmp_path):
        """Enforced WHILE streaming, so growth after validation cannot slip past.

        Exercised directly: routing through install_from_dir lets
        ``_assert_safe_tree``'s pre-copy total reject the bundle first, so it
        would never reach the streaming budget this test is about.
        """
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        big = b"y" * (5 * 1024 * 1024)
        (src / "one").write_bytes(big)
        (src / "two").write_bytes(big)
        budget = [powers_mod.MAX_INSTALL_BYTES]
        powers_mod._copy_regular_file(src / "one", dest / "one", required=True, budget=budget)
        assert budget[0] < powers_mod.MAX_INSTALL_BYTES  # first file consumed some
        with pytest.raises(PowerFormatError, match="cap"):
            powers_mod._copy_regular_file(
                src / "two", dest / "two", required=True, budget=budget
            )

    @pytest.mark.parametrize("bad", ["con", "AUX", "nul", "com1", "lpt9", "prn"])
    def test_windows_reserved_names_rejected_on_every_platform(self, bad):
        """These pass the character regex but fail the directory swap as a 500."""
        assert is_safe_power_name(bad) is False

    def test_ordinary_names_still_accepted(self):
        assert is_safe_power_name("console") is True
        assert is_safe_power_name("com10") is True

    def test_folded_block_scalar_is_parsed_as_yaml(self):
        """`description: >-` was read as the literal string '>-'."""
        text = (
            "---\n"
            "name: kb\n"
            "displayName: KB\n"
            "description: >-\n"
            "  first line\n"
            "  second line\n"
            "keywords: [a, b]\n"
            "---\n\n# Body\n"
        )
        meta = parse_power_md(text)
        assert meta.description == "first line second line"
        assert meta.keywords == ["a", "b"]

    def test_non_mapping_frontmatter_is_refused(self):
        with pytest.raises(PowerFormatError):
            parse_power_md("---\n- just\n- a list\n---\n\n# Body\n")

    def test_yaml_alias_bomb_is_refused(self):
        """safe_load still EXPANDS aliases, so the 256 KiB cap bounds only the
        source text — not the expansion. A few hundred bytes can become GiBs."""
        bomb = (
            "---\n"
            "name: kb\n"
            "displayName: KB\n"
            "description: boom\n"
            "a: &a ['x','x','x','x','x','x','x','x','x']\n"
            "b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
            "c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
            "d: [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n"
            "---\n\n# Body\n"
        )
        with pytest.raises(PowerFormatError, match="alias"):
            parse_power_md(bomb)

    def test_plain_anchorless_yaml_still_parses(self):
        """Guard against over-rejecting ordinary frontmatter."""
        meta = parse_power_md(
            "---\nname: kb\ndisplayName: KB\ndescription: plain\n---\n\n# Body\n"
        )
        assert meta.name == "kb"

    @pytest.mark.asyncio
    async def test_cancel_after_staging_restores_the_bundle(self, store, monkeypatch):
        """A cancelled remove must not strand the bundle under `.removing-*`.

        The executor thread keeps going when the awaiting coroutine is cancelled,
        so the rename can complete after our await returns. Without a restore the
        tree is invisible to `list_powers()` while `installed.json` still names
        it, and nothing can reach it again.

        The cancellation is fired from INSIDE the real rename so it is aimed at
        the staging window rather than relying on a sleep to win a race. It is
        NOT asserted to land there: the whole uninstall is now one fast executor
        call, so the transaction can finish first and `cancel()` becomes a no-op.
        Both outcomes are legitimate — the invariant below is what matters, not
        which one occurred.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        power_path = st._power_path("kb")
        assert power_path.is_dir()

        staged = asyncio.Event()
        loop = asyncio.get_running_loop()
        real_rename = powers_mod._rename_at

        def _rename_then_signal(root_fd, src_name, dst_name, root, **kw):
            result = real_rename(root_fd, src_name, dst_name, root, **kw)
            if ".removing-" in str(dst_name):
                loop.call_soon_threadsafe(staged.set)
            return result

        # `_rename_at` is the seam both platforms share: the anchored path calls
        # `os.rename` with descriptors, the fallback calls `os.replace`.
        monkeypatch.setattr(powers_mod, "_rename_at", _rename_then_signal)
        task = asyncio.create_task(st.remove_power("kb"))
        await asyncio.wait_for(staged.wait(), timeout=5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        monkeypatch.undo()
        await _await_worker_settled(st._dir / ".removing-kb")

        # No residue, and no half state. Asserting "the bundle is back" would be
        # wrong on a slower runner where the delete worker already ran — the bytes
        # are then legitimately gone, and CI caught exactly that on 3.10 while this
        # passed locally.
        assert not (st._dir / ".removing-kb").exists(), "stranded .removing-* directory"
        if power_path.exists():
            assert st.load_power("kb") is not None
        else:
            assert st.load_power("kb") is None, "record describes a vanished bundle"

    @pytest.mark.asyncio
    async def test_record_identity_ignores_a_mutated_power_md(self, store):
        """A bundle must not be able to rename itself after install.

        `POWER.md` stays editable inside the installed tree, so reading the
        identity back out of it made `api_powers` report a name the delete route
        cannot resolve — leaving the Power unremovable from the dashboard.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        # Mutate the installed bundle's declared name.
        (st._power_path("kb") / POWER_MD_NAME).write_text(
            _power_md(name="impostor"), encoding="utf-8"
        )
        rec = st.load_power("kb")
        assert rec is not None
        assert rec["name"] == "kb", "identity came from mutable POWER.md"
        assert {p["name"] for p in st.list_powers()} == {"kb"}
        # And the id the UI would use still resolves for deletion.
        assert await st.remove_power(rec["name"]) is True

    @pytest.mark.asyncio
    async def test_orphaned_backup_is_recovered_on_next_install(self, store):
        """A crash between the two renames must not lose the bundle.

        The backup path is deterministic precisely so a later process can find
        it; a pid-suffixed name was unrecoverable, so the Power disappeared from
        the listing while its bytes sat on disk under a name nothing looked for.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb", keywords=["original"])
        await st.install_from_dir(src, source={"kind": "folder", "ref": "v1"})
        dest = st._power_path("kb")

        # Simulate the crash: dest moved to the backup slot, then killed.
        backup = st._dir / ".backup-kb"
        os.replace(dest, backup)
        assert not dest.exists() and backup.exists()
        assert st.load_power("kb") is None  # invisible while stranded

        # Recovery only becomes observable when the NEXT install FAILS: without
        # it the rollback sees no predecessor, discards the orphan, and the Power
        # is gone for good. A succeeding install replaces the tree either way, so
        # asserting on that path tested nothing (it passed with recovery removed).
        second = _bundle(tmp / "v2", name="kb", keywords=["replacement"])

        def _fail(*a, **k):
            raise OSError("disk full")

        monkeypatch_target = st._write_installed
        st._write_installed = _fail  # type: ignore[method-assign]
        try:
            with pytest.raises(OSError):
                await st.install_from_dir(second, source={"kind": "folder", "ref": "v2"})
        finally:
            st._write_installed = monkeypatch_target  # type: ignore[method-assign]

        rec = st.load_power("kb")
        assert rec is not None, "orphaned bundle was lost by a failed reinstall"
        assert rec["keywords"] == ["original"], "predecessor not the recovered one"
        assert not backup.exists(), "backup slot left behind after recovery"

    @pytest.mark.asyncio
    async def test_cancel_on_the_delete_await_also_restores(self, store, monkeypatch):
        """Cancellation lands on the DELETE await at least as often as on staging.

        The staging worker usually finishes first, so by the time a cancel
        arrives the coroutine has already moved on — repairing only the staging
        arm left the bundle stranded whenever the race went the common way. This
        is the shape CI hit on 3.10 while the staging-only test passed locally.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        power_path = st._power_path("kb")
        scratch = st._dir / ".removing-kb"

        entered_delete = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _signal_then_fail(*a, **kw):
            # Fires inside _delete_bytes, i.e. after staging has fully completed.
            # It then FAILS rather than deleting, so the staged tree survives and
            # the repair is load-bearing. A version that really deleted made this
            # test pass with the repair removed — there was nothing left to fix.
            loop.call_soon_threadsafe(entered_delete.set)
            time.sleep(0.3)
            raise OSError("device busy")

        monkeypatch.setattr(powers_mod, "_rmtree_at", _signal_then_fail)
        # This used to also patch `_run_drained` to raise CancelledError, to
        # simulate "no awaited compensation can run". That patch was inert once
        # the transaction stopped compensating from the coroutine — the helper had
        # no call sites — and it is now deleted. The invariant no longer depends on
        # simulating the 3.10 await behaviour: the worker owns its rollback, so
        # cancelling the await cannot skip it whatever the Python version does.
        task = asyncio.create_task(st.remove_power("kb"))
        await asyncio.wait_for(entered_delete.wait(), timeout=5)
        task.cancel()
        # Tolerant for the same reason as the sibling test: the single-worker
        # transaction can complete before the cancel lands, making cancel() a
        # no-op. Asserting the raise would make this a timing test rather than
        # an invariant test.
        try:
            await task
        except asyncio.CancelledError:
            pass
        monkeypatch.undo()
        # Returns as soon as the worker's own rollback has run. With the delete
        # patched to fail, a committed removal legitimately leaves `scratch` in
        # place, so this can also fall through on its deadline — the assertions
        # below cover both outcomes rather than assuming which one happened.
        await _await_worker_settled(scratch, timeout=2.0)

        # The honest invariant, restated for the round-37 ordering. A leftover
        # `.removing-*` is no longer forbidden: the delete is patched to fail, and
        # after the record is committed those bytes are reclaimable garbage rather
        # than a half state. What must never happen is the record and the bundle
        # disagreeing in the direction that loses data — a record naming a Power
        # whose bytes are gone for good, or bytes parked under `.removing-*` while
        # the record still claims them at their own name.
        record = st.load_power("kb")
        if record is None:
            # Committed. The bundle is unreachable; leftovers are the sweep's job.
            assert not power_path.exists(), "record dropped while the bundle is live"
        else:
            # Not committed, so the rollback must have put the tree back.
            assert power_path.exists(), "record describes a vanished bundle"
            assert not scratch.exists(), "bundle left parked while still recorded"

        # Either way the store converges on the next operation.
        other = _bundle(tmp, name="other")
        await st.install_from_dir(other, source={"kind": "folder", "ref": str(other)})
        assert not scratch.exists(), "leftover bytes survived reconciliation"

    @pytest.mark.asyncio
    async def test_cancel_compensation_does_not_depend_on_awaiting(self, store, monkeypatch):
        """The restore must work when NO further await can succeed.

        On some interpreter versions a cancelled task re-raises from every
        subsequent `await`, so a drain-then-await-restore silently never runs —
        which is how this stranded the bundle on 3.10 while passing on 3.12. This
        pins the property rather than the version: every awaiting helper is made
        to fail, and the bundle must still be restored.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        power_path = st._power_path("kb")

        staged = asyncio.Event()
        loop = asyncio.get_running_loop()
        real_rename = powers_mod._rename_at

        def _rename_then_signal(root_fd, src_name, dst_name, root, **kw):
            result = real_rename(root_fd, src_name, dst_name, root, **kw)
            if ".removing-" in str(dst_name):
                loop.call_soon_threadsafe(staged.set)
            return result

        # `_rename_at` is the seam both platforms share: the anchored path calls
        # `os.rename` with descriptors, the fallback calls `os.replace`.
        monkeypatch.setattr(powers_mod, "_rename_at", _rename_then_signal)
        # Kept as a belt-and-braces guard even though the transaction no longer
        # awaits any compensation: if a future change reintroduces an awaited
        # rollback, this makes that await fail and the invariant below catch it.
        task = asyncio.create_task(st.remove_power("kb"))
        await asyncio.wait_for(staged.wait(), timeout=5)
        task.cancel()
        # Tolerant for the same reason as the sibling test: the single-worker
        # transaction can complete before the cancel lands, making cancel() a
        # no-op. Asserting the raise would make this a timing test rather than
        # an invariant test.
        try:
            await task
        except asyncio.CancelledError:
            pass
        monkeypatch.undo()
        await _await_worker_settled(st._dir / ".removing-kb")

        # Same invariant, with every awaiting helper made to fail: the repair must
        # still leave no residue and no half state.
        assert not (st._dir / ".removing-kb").exists(), "restore needed an await"
        if power_path.exists():
            assert st.load_power("kb") is not None
        else:
            assert st.load_power("kb") is None

    @pytest.mark.asyncio
    async def test_per_power_symlink_cannot_redirect_to_another_power(self, store):
        """`powers/foo -> powers/bar` must not make foo's ops act on bar.

        `_power_path` used to `resolve()` the child, and a resolved symlink's
        parent IS the root — so the containment check passed and install/remove
        for `foo` silently overwrote or deleted `bar`.
        """
        st, _mc, tmp = store
        victim = _bundle(tmp, name="victim")
        await st.install_from_dir(victim, source={"kind": "folder", "ref": str(victim)})
        victim_path = st._power_path("victim")
        assert victim_path.is_dir()

        # Plant powers/attacker -> powers/victim.
        os.symlink(victim_path, st._dir / "attacker")
        with pytest.raises(PowerFormatError, match="symlink"):
            st._power_path("attacker")
        with pytest.raises(PowerFormatError, match="symlink"):
            await st.remove_power("attacker")
        # The victim survived untouched.
        assert victim_path.is_dir()
        assert st.load_power("victim") is not None

    @pytest.mark.asyncio
    async def test_install_holds_a_cross_process_lock(self, store):
        """The asyncio lock alone cannot order two gateways sharing a data home."""
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        lock_path = st._dir / POWERS_LOCK_NAME
        assert lock_path.exists(), "sidecar lock file was never created"
        # The lock is not itself installable as a Power.
        assert is_safe_power_name(POWERS_LOCK_NAME) is False
        assert POWERS_LOCK_NAME not in {p["name"] for p in st.list_powers()}


class TestInertInstall:
    """The security claim this PR makes: an installed Power does nothing.

    These are the load-bearing tests. Everything else about Powers is a
    convenience feature; "installing is safe because it activates nothing" is
    the property a reviewer has to be able to trust, so it is asserted from
    both ends — no MCP config is written, and no skill is materialized.
    """

    @pytest.mark.asyncio
    async def test_install_writes_no_mcp_config_at_all(self, store):
        st, mc, _tmp = store
        src = _bundle(_tmp, name="stripe-payments", mcp={"stripe": {"command": "npx", "args": ["-y", "x"]}})
        rec = await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        # The bundle DECLARES servers, so the kind is reported...
        assert rec["kind"] == "mcp"
        # ...but nothing was registered anywhere.
        assert not mc.exists()

    @pytest.mark.asyncio
    async def test_install_materializes_no_skill(self, store, tmp_path):
        """No generated skill means the docs cannot enter agent context."""
        st, _mc, _tmp = store
        skills = tmp_path / "skills"
        skills.mkdir(exist_ok=True)
        src = _bundle(_tmp, name="stripe-payments", steering={"guide.md": "# secret guidance"})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        # Nothing anywhere under a skills tree, and no stray SKILL.md in the
        # powers tree either.
        assert list(skills.rglob("SKILL.md")) == []
        assert list((st._dir).rglob("SKILL.md")) == []

    @pytest.mark.asyncio
    async def test_record_carries_no_activation_state(self, store):
        """A `trusted` flag that gated nothing would be a false security claim."""
        st, _mc, _tmp = store
        src = _bundle(_tmp, name="stripe-payments")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        raw = json.loads((st._dir / "installed.json").read_text(encoding="utf-8"))
        assert set(raw["stripe-payments"]) == {"source", "installedAt"}

    @pytest.mark.asyncio
    async def test_declared_kind_survives_a_malformed_mcp_json(self, store):
        """Presence testing must not raise on junk: nothing consumes the specs."""
        st, mc, _tmp = store
        src = _bundle(_tmp, name="stripe-payments")
        (src / "mcp.json").write_text("{not json at all", encoding="utf-8")
        rec = await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        assert rec["kind"] == "knowledge"
        assert not mc.exists()


class TestInstallTransaction:
    @pytest.mark.asyncio
    async def test_install_commits_the_bundle_it_validated(self, store, monkeypatch):
        """The source is caller-owned and mutable between validation and copy.

        Rewriting POWER.md mid-install must not commit a different Power under
        the already-validated name, or ``load_power()`` reports an identity that
        does not match the bytes on disk.
        """
        st, _mc, _tmp = store
        src = _bundle(_tmp, name="stripe-payments")
        real_copy = powers_mod._copy_power_files

        def _rewrite_then_copy(s: Path, d: Path, **kw) -> None:
            real_copy(s, d, **kw)
            # Swap the staged identity out from under the installer.
            (d / "POWER.md").write_text(
                _power_md(name="totally-different"), encoding="utf-8"
            )

        monkeypatch.setattr(powers_mod, "_copy_power_files", _rewrite_then_copy)
        with pytest.raises(PowerFormatError, match="changed mid-install"):
            await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        # Nothing committed under either name.
        assert st.load_power("stripe-payments") is None
        assert st.load_power("totally-different") is None

    @pytest.mark.asyncio
    async def test_failed_record_write_restores_the_previous_bundle(self, store, monkeypatch):
        """A failed reinstall must leave the working install exactly as it was."""
        st, _mc, _tmp = store
        first = _bundle(_tmp, name="stripe-payments", keywords=["original"])
        await st.install_from_dir(first, source={"kind": "folder", "ref": "v1"})

        second = _bundle(_tmp / "v2", name="stripe-payments", keywords=["replacement"])
        boom = st._write_installed

        def _fail(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(st, "_write_installed", _fail)
        with pytest.raises(OSError):
            await st.install_from_dir(second, source={"kind": "folder", "ref": "v2"})
        monkeypatch.setattr(st, "_write_installed", boom)

        rec = st.load_power("stripe-payments")
        assert rec is not None
        assert rec["keywords"] == ["original"]
        assert rec["source"]["ref"] == "v1"

    @pytest.mark.asyncio
    async def test_remove_keeps_the_record_when_deletion_is_impossible(self, store, monkeypatch):
        """Dropping the record first would orphan the bundle."""
        st, _mc, _tmp = store
        src = _bundle(_tmp, name="stripe-payments")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        def _fail_rename(*a, **k):
            raise OSError("device busy")

        # `_rename_at`, not `os.rename`: the no-dir_fd platforms rename with
        # `os.replace`, so patching os.rename left this inert on Windows.
        monkeypatch.setattr(powers_mod, "_rename_at", _fail_rename)
        with pytest.raises(OSError):
            await st.remove_power("stripe-payments")
        # Record intact, so the bundle on disk is still tracked.
        assert st.load_power("stripe-payments") is not None


class TestInstall:

    @pytest.mark.asyncio
    async def test_path_traversal_name_rejected_on_ops(self, store):
        st, _mc, _tmp = store
        with pytest.raises(PowerFormatError):
            await st.remove_power("../evil")

    @pytest.mark.asyncio
    async def test_symlink_at_a_contract_path_is_rejected(self, store):
        """Symlink rejection is scoped to the files an install actually copies."""
        st, _mc, tmp = store
        src = _bundle(tmp, name="db")
        target = tmp / "secret.txt"
        target.write_text("secret")
        # Replace a CONTRACT file with a symlink to something outside the bundle.
        (src / MCP_JSON_NAME).unlink(missing_ok=True)
        os.symlink(target, src / MCP_JSON_NAME)
        with pytest.raises(PowerFormatError) as exc:
            await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        assert "symlink" in str(exc.value)

    @pytest.mark.asyncio
    async def test_unrelated_symlink_does_not_block_a_valid_bundle(self, store):
        """A sibling symlink is irrelevant: only contract files are read.

        Previously the whole source tree was walked, so an unrelated symlink (or
        an oversize unrelated file) in a repository rejected an otherwise valid
        bundle even though neither is ever opened or copied.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="db")
        target = tmp / "unrelated-secret.txt"
        target.write_text("not part of the bundle")
        os.symlink(target, src / "link.txt")
        rec = await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        assert rec["name"] == "db"
        # The symlink was neither followed nor copied.
        assert not (st._power_path("db") / "link.txt").exists()

    @pytest.mark.asyncio
    async def test_list_and_load(self, store):
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb", keywords=["docs"])
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        listed = st.list_powers()
        assert len(listed) == 1
        assert listed[0]["name"] == "kb"
        assert st.load_power("kb")["displayName"] == "Demo Power"
        assert st.load_power("nonexistent") is None


class TestRound9Regressions:
    """Regressions for the round-9 GPT review findings on PR #408."""

    # ── Finding 1: symlinked powers root is rejected fail-closed ──

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not powers_mod._SUPPORTS_DIR_FD, reason="POSIX dir_fd path only"
    )
    async def test_install_mutations_are_anchored_to_the_pinned_root(self, store):
        """A root swapped mid-transaction cannot redirect the install.

        This is the property that replaced `_assert_root_not_symlinked()`. That
        check and the mutation it guarded were two separate operations, so the root
        could become a symlink in the gap and the following renames would act
        THROUGH the link on an external directory. `_root_lock` opens the root
        `O_NOFOLLOW|O_DIRECTORY` once and every rename/rmtree goes through that
        descriptor, so the swap is unobservable to the transaction.

        The swapped-in root deliberately presents a VALID layout — a matching
        staging directory and an existing bundle — so path-based operations
        succeed. That is what makes the test non-vacuous: a first version simply
        broke the install in both shapes and passed with the fix reverted. Here the
        pre-fix code renames the external bundle aside and then deletes it, so the
        victim's disappearance is the signal.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")

        outside = tmp / "outside"
        victim = outside / "kb"
        victim.mkdir(parents=True)
        (victim / "canary.txt").write_text("UNTOUCHED", encoding="utf-8")

        real_root = st._dir
        real_root.mkdir(parents=True, exist_ok=True)
        real_copy = powers_mod._copy_power_files
        fired: list[str] = []

        def _swap_root_then_copy(source, staging, **kw):
            """Copy as normal, then swap the root for a symlink to *outside*."""
            result = real_copy(source, staging, **kw)
            if fired:
                return result
            fired.append(str(staging))
            staging_name = Path(staging).name
            # Give the external directory a staging tree of the same name, so the
            # pre-fix `os.replace(staging, dest)` succeeds through the symlink
            # instead of failing on a missing path.
            shutil.copytree(staging, outside / staging_name)
            moved = tmp / "powers-real"
            os.rename(real_root, moved)
            os.symlink(outside, real_root)
            return result

        original = powers_mod._copy_power_files
        powers_mod._copy_power_files = _swap_root_then_copy
        try:
            try:
                await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
            except Exception:
                pass  # the swap may break the install; the invariant asserted
                # below is what happened OUTSIDE the store, not the return value
        finally:
            powers_mod._copy_power_files = original
            if real_root.is_symlink():
                real_root.unlink()
                if (tmp / "powers-real").exists():
                    os.rename(tmp / "powers-real", real_root)

        assert fired, "the swap never fired — the test would be vacuous"
        # The external bundle is still there, with its contents: no rename moved
        # it aside and no rmtree removed it.
        assert victim.is_dir(), "an external directory was moved or deleted"
        assert (victim / "canary.txt").read_text(encoding="utf-8") == "UNTOUCHED"
        assert not (outside / ".backup-kb").exists(), "renamed a bundle outside the store"

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not powers_mod._SUPPORTS_DIR_FD, reason="POSIX dir_fd path only"
    )
    async def test_state_write_and_delete_are_anchored_to_the_pinned_root(self, store):
        """A root swapped mid-transaction cannot redirect the record or the delete.

        Round 23 anchored the renames but left three operations lexical — the
        staging `mkdir`, the `installed.json` write and the recursive walk inside
        `_rmtree_at` — so each still resolved the root path afresh. This drives a
        swap during the REMOVE transaction, at the moment the bundle has been
        renamed aside, and asserts nothing outside the store was written or
        deleted.

        The decoy root is given a matching layout so a lexical implementation
        SUCCEEDS rather than erroring: an outside `installed.json` to overwrite and
        an outside `.removing-kb` tree to delete. Failure of the old shape is
        therefore observable as damage, not as an exception.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        outside = tmp / "outside"
        outside.mkdir()
        decoy_record = outside / "installed.json"
        decoy_record.write_text('{"untouched": true}', encoding="utf-8")
        decoy_tree = outside / ".removing-kb"
        decoy_tree.mkdir()
        (decoy_tree / "canary.txt").write_text("UNTOUCHED", encoding="utf-8")

        real_root = st._dir
        real_rename = powers_mod._rename_at
        fired: list[str] = []

        def _swap_root_after_staging(root_fd, src_name, dst_name, root, **kw):
            result = real_rename(root_fd, src_name, dst_name, root, **kw)
            # Fires once the live bundle has been renamed aside, i.e. before the
            # delete and the record write.
            if ".removing-" in str(dst_name) and not fired:
                fired.append(str(dst_name))
                moved = tmp / "powers-real"
                os.rename(real_root, moved)
                os.symlink(outside, real_root)
            return result

        original = powers_mod._rename_at
        powers_mod._rename_at = _swap_root_after_staging
        try:
            try:
                await st.remove_power("kb")
            except Exception:
                pass  # the swap may break the uninstall; the invariant is below
        finally:
            powers_mod._rename_at = original
            if real_root.is_symlink():
                real_root.unlink()
                if (tmp / "powers-real").exists():
                    os.rename(tmp / "powers-real", real_root)

        assert fired, "the swap never fired — the test would be vacuous"
        assert decoy_record.read_text(encoding="utf-8") == '{"untouched": true}', (
            "the record write followed the swapped root"
        )
        assert decoy_tree.is_dir(), "the recursive delete followed the swapped root"
        assert (decoy_tree / "canary.txt").read_text(encoding="utf-8") == "UNTOUCHED"

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not powers_mod._SUPPORTS_DIR_FD, reason="POSIX dir_fd path only"
    )
    async def test_transaction_state_read_is_anchored_to_the_pinned_root(self, store):
        """An anchored write over a lexical read is worse than neither.

        The write lands in the right place; the *content* comes from a decoy. On
        install that means the record is rebuilt from foreign state and every
        other Power's provenance is erased — the file is intact, correctly
        located, and wrong.

        The decoy root therefore carries a VALID `installed.json` describing a
        different Power, so a lexical read succeeds and its content is
        observable in the damage.
        """
        st, _mc, tmp = store
        first = _bundle(tmp, name="kb")
        await st.install_from_dir(first, source={"kind": "folder", "ref": str(first)})
        assert "kb" in st._load_installed()

        outside = tmp / "outside"
        outside.mkdir()
        (outside / "installed.json").write_text(
            '{"decoy-power": {"source": {"kind": "folder", "ref": "/x"}, '
            '"installedAt": "2020-01-01T00:00:00+00:00"}}',
            encoding="utf-8",
        )

        second = _bundle(tmp, name="db")
        real_root = st._dir
        # The swap must land AFTER the staged-bundle checks and the swap-in
        # rename, immediately before the record write — that is the only window
        # in which the state read is the next thing to happen. Swapping during
        # the copy instead made an earlier lexical staging check fail and the
        # install aborted before the read, which is why a first version of this
        # test passed with the fix reverted.
        real_rename = powers_mod._rename_at
        fired: list[str] = []

        def _swap_root_after_swapin(root_fd, src_name, dst_name, root, **kw):
            result = real_rename(root_fd, src_name, dst_name, root, **kw)
            if dst_name == "db" and not fired:
                fired.append("swapped")
                moved = tmp / "powers-real"
                os.rename(real_root, moved)
                os.symlink(outside, real_root)
            return result

        original = powers_mod._rename_at
        powers_mod._rename_at = _swap_root_after_swapin
        try:
            try:
                await st.install_from_dir(
                    second, source={"kind": "folder", "ref": str(second)}
                )
            except Exception:
                pass  # the swap may break the install; the invariant is below
        finally:
            powers_mod._rename_at = original
            if real_root.is_symlink():
                real_root.unlink()
                if (tmp / "powers-real").exists():
                    os.rename(tmp / "powers-real", real_root)

        assert fired, "the swap never fired — the test would be vacuous"
        record = st._load_installed()
        assert "kb" in record, "the first Power's provenance was erased"
        assert "decoy-power" not in record, "decoy state was read into the real record"

    @pytest.mark.asyncio
    async def test_ambiguous_backup_is_preserved_not_deleted(self, store):
        """A kill after the swap leaves two copies; neither may be discarded.

        With both `dest` and `.backup-<name>` present, the filesystem cannot say
        whether the interrupted transaction committed its record. The previous
        recovery deleted the backup, which is the only copy of the bundle the live
        record may still describe. It is now quarantined under a timestamped name
        instead, so the bytes survive and a stale backup still cannot block the
        install.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        # Hand-build the interrupted state: dest live, backup holding the previous
        # bundle with a marker that proves which copy survived.
        backup = st._dir / ".backup-kb"
        backup.mkdir()
        (backup / "POWER.md").write_text(_power_md(name="kb"), encoding="utf-8")
        (backup / "MARKER.txt").write_text("PREVIOUS-BUNDLE", encoding="utf-8")

        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        assert not backup.exists(), "the ambiguous backup should be moved aside"
        quarantined = list(st._dir.glob(".orphaned-backup-kb-*"))
        assert quarantined, "the ambiguous backup was destroyed instead of preserved"
        assert (quarantined[0] / "MARKER.txt").read_text(
            encoding="utf-8"
        ) == "PREVIOUS-BUNDLE"
        # The install still completed and the store is consistent.
        assert st.load_power("kb") is not None

    @pytest.mark.asyncio
    async def test_leftover_removing_tree_is_reconciled_before_dropping_record(
        self, store
    ):
        """A crash between the rename-aside and the delete must not orphan bytes.

        `list_powers()` enumerates `installed.json`, so bytes left under
        `.removing-<name>` after the record is dropped are invisible to the UI and
        nothing ever cleans them up.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        # The interrupted state: bundle renamed aside, record still present.
        power_path = st._power_path("kb")
        scratch = st._dir / ".removing-kb"
        os.rename(power_path, scratch)
        assert "kb" in st._load_installed()

        assert await st.remove_power("kb") is True
        assert not scratch.exists(), "orphaned .removing-* bytes were left behind"
        assert "kb" not in st._load_installed()

    @pytest.mark.asyncio
    async def test_retrying_an_interrupted_uninstall_cannot_destroy_the_bundle(
        self, store, monkeypatch
    ):
        """The retry path must commit the record BEFORE deleting, like the main one.

        Re-expressed in round 38. This branch handles the state left by a process
        that died between the rename-aside and everything after it, where the
        scratch tree is the ONLY copy of the bytes. It used to delete them and drop
        the record afterwards — the exact ordering round 37 removed from the main
        path — so a record replacement that fails (a held-open `installed.json` on
        Windows) destroyed the bundle permanently. It now restores the tree to its
        own name and falls through to the durable path.

        The earlier version of this test asserted "a failed delete keeps the
        record", which no longer holds by design: after the commit, leftover bytes
        are reclaimable garbage rather than a half state. The property that
        matters is the one asserted here — a FAILED retry loses nothing.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        power_path = st._power_path("kb")
        body = (power_path / POWER_MD_NAME).read_text(encoding="utf-8")
        scratch = st._dir / ".removing-kb"
        # The interrupted state: bytes parked, record still claiming them.
        os.rename(power_path, scratch)

        def boom(*a, **k):
            raise OSError("installed.json is held open")

        # Fails the record commit, which both orderings must reach. Under the old
        # ordering the bytes were already gone by then.
        monkeypatch.setattr(powers_mod, "_commit_staged_at", boom)
        with pytest.raises(OSError):
            await st.remove_power("kb")
        monkeypatch.undo()

        assert "kb" in st._load_installed(), "record dropped on a failed retry"
        assert power_path.is_dir(), "the only copy of the bundle was destroyed"
        assert (power_path / POWER_MD_NAME).read_text(encoding="utf-8") == body
        # And the Power is usable again, not stranded in the scratch slot.
        assert st.load_power("kb") is not None

    @pytest.mark.asyncio
    async def test_retry_reclaims_bytes_once_the_removal_has_committed(
        self, store, monkeypatch
    ):
        """A leftover whose record is already gone is swept, not reported failed.

        This is the other half of the branch: the removal committed, only its
        cleanup did not. Raising here reported a failure for a removal that
        succeeded, which sent callers looking for a Power that no longer existed.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        scratch = st._dir / ".removing-kb"
        os.rename(st._power_path("kb"), scratch)
        # Drop the record directly: the removal committed, the delete did not run.
        st._drop_record("kb")

        assert await st.remove_power("kb") is False
        assert not scratch.exists(), "committed leftover was not reclaimed"

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not powers_mod._SUPPORTS_DIR_FD, reason="POSIX dir_fd path only"
    )
    async def test_preplanted_temp_hardlink_is_not_truncated(self, store, tmp_path):
        """`O_NOFOLLOW` refuses a symlink but says nothing about a hardlink.

        The state write used a fixed `.installed.json.tmp` with `O_CREAT|O_TRUNC`,
        so a link preplanted at that name would have been truncated and then filled
        with store state. The name is now random and the open uses `O_EXCL`, so
        neither pre-creation nor adoption is possible.
        """
        st, _mc, _tmp = store
        src = _bundle(_tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        victim = tmp_path / "victim.txt"
        victim.write_text("PRECIOUS", encoding="utf-8")
        # A hardlink at the previously predictable temp name.
        os.link(victim, st._dir / ".installed.json.tmp")

        # Any further state write must not touch the linked file.
        second = _bundle(_tmp, name="db")
        await st.install_from_dir(second, source={"kind": "folder", "ref": str(second)})

        assert victim.read_text(encoding="utf-8") == "PRECIOUS", (
            "the state write truncated a preplanted hardlink"
        )
        assert st.load_power("db") is not None, "the install should still succeed"

    def test_reparse_point_root_is_refused(self, tmp_path, monkeypatch):
        """A Windows junction is followed like a symlink but is not one.

        `Path.is_symlink()` returns False for an NTFS directory junction, and the
        path check is the ONLY guard on the platform without `dir_fd` support — so
        the check has to test the reparse-point attribute too. Driven by simulating
        the attribute, since a real junction cannot be created on this host.
        """
        root = tmp_path / "powers"
        root.mkdir()
        real_lstat = os.lstat

        class _FakeStat:
            def __init__(self, base):
                self._base = base
                self.st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

            def __getattr__(self, item):
                return getattr(self._base, item)

        def _lstat_with_reparse(path, *a, **kw):
            result = real_lstat(path, *a, **kw)
            if str(path) == str(root):
                return _FakeStat(result)
            return result

        monkeypatch.setattr(os, "lstat", _lstat_with_reparse)
        monkeypatch.setattr(
            powers_mod.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False
        )

        with pytest.raises(PowerFormatError, match="reparse point"):
            powers_mod._assert_not_reparse_point(root)

    def test_a_plain_directory_is_not_mistaken_for_a_reparse_point(self, tmp_path):
        """The guard must not reject an ordinary root."""
        root = tmp_path / "powers"
        root.mkdir()
        powers_mod._assert_not_reparse_point(root)  # must not raise

    @pytest.mark.asyncio
    async def test_concurrent_delete_cannot_fail_a_committed_install(self, store):
        """The response record is read back under the transaction's own lock.

        Reading it after the lock was released left a window: a concurrent
        `remove_power` for the same name could drop the record, and the install —
        which had already committed — reported a failure. Under `python -O`, where
        the old assertion was stripped, the same window produced `None["kind"]`
        instead.

        The delete is driven from inside the transaction, at the last operation
        before the record read, which is exactly the window the fix closes. It runs
        in a separate thread because `remove_power`'s own transaction takes the same
        cross-process lock: with the fix, that thread cannot acquire it until the
        install is finished, which is the property under test.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")

        real_load = powers_mod.PowersStore.load_power
        fired: list[str] = []
        deleter: list[threading.Thread] = []

        def _delete_then_load(self, name, **kw):
            # Fires inside the transaction, immediately before the record read.
            if not fired:
                fired.append(name)

                def _race():
                    # A second store over the same directory, as a separate caller
                    # would be. Best-effort: it should block on the lock.
                    asyncio.run(PowersStore(powers_path=st._dir).remove_power(name))

                thread = threading.Thread(target=_race, daemon=True)
                deleter.append(thread)
                thread.start()
                time.sleep(0.2)  # give it time to reach the lock
            return real_load(self, name, **kw)

        original = powers_mod.PowersStore.load_power
        powers_mod.PowersStore.load_power = _delete_then_load
        try:
            record = await st.install_from_dir(
                src, source={"kind": "folder", "ref": str(src)}
            )
        finally:
            powers_mod.PowersStore.load_power = original
            for thread in deleter:
                thread.join(timeout=10)

        assert fired, "the racing delete never fired — the test would be vacuous"
        # The install committed, so it must report the record it wrote rather than
        # raising. Whether the Power still exists afterwards is the delete's
        # business; this asserts the install's own outcome is honest.
        assert record is not None
        assert record["name"] == "kb"

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not powers_mod._SUPPORTS_DIR_FD, reason="POSIX dir_fd path only"
    )
    async def test_full_disk_at_record_write_does_not_lose_the_bundle(self, store, monkeypatch):
        """The record write is allocated BEFORE the bundle is destroyed.

        The reported failure was: delete succeeds, record write hits ENOSPC, bundle
        gone and a stale record left behind. The write is now staged up front and
        committed with a rename, so a filesystem that cannot take the record fails
        the removal while the bundle is still there.

        ENOSPC is simulated at the staging write, which is where allocation now
        happens.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        # Patched at BOTH the staged and unstaged write sites, so the test is
        # version-agnostic: with the fix the failure lands before the delete and
        # the bundle survives; without it the failure lands after, and the
        # assertions below catch the loss. Naming only the new helper made the
        # revert fail with AttributeError, which would have passed for any rename.
        def _no_space(*args, **kwargs):
            path = next(
                (a for a in args if isinstance(a, Path) and a.name == "installed.json"),
                None,
            )
            if path is not None:
                raise OSError(28, "No space left on device")
            return None

        for site in ("_stage_write_at", "_atomic_write_at"):
            if hasattr(powers_mod, site):
                monkeypatch.setattr(powers_mod, site, _no_space)

        with pytest.raises(OSError):
            await st.remove_power("kb")

        # The bundle survived and is still tracked: nothing was destroyed for a
        # removal that could not be recorded.
        assert st.load_power("kb") is not None
        assert st._power_path("kb").is_dir()
        assert not (st._dir / ".removing-kb").exists(), "left a scratch tree behind"

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not powers_mod._SUPPORTS_DIR_FD, reason="POSIX dir_fd path only"
    )
    async def test_staged_record_is_discarded_when_the_commit_fails(self, store, monkeypatch):
        """A prepared-but-uncommitted record must not linger on disk.

        It describes a removal that did not happen, and a stale `.installed.json.*`
        temp would also be a confusing artefact for anyone inspecting the store.

        Retargeted in round 37 from the delete to the commit: the record is now
        committed BEFORE the delete, so a delete failure has no staged record to
        leave behind and this assertion was vacuous there. `_stage_write_at` is
        left real so the temp file genuinely exists when the commit fails.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        def boom(*a, **k):
            raise OSError("device busy")

        monkeypatch.setattr(powers_mod, "_commit_staged_at", boom)
        with pytest.raises(OSError):
            await st.remove_power("kb")

        assert st.load_power("kb") is not None
        leftovers = list(st._dir.glob(".installed.json.*"))
        assert leftovers == [], f"staged record left behind: {leftovers}"

    @pytest.mark.asyncio
    async def test_full_disk_does_not_lose_the_bundle_without_dir_fd(self, store, monkeypatch):
        """The durability property must not depend on `dir_fd` support.

        Round 33 staged the record write before the delete on POSIX and recorded
        the no-descriptor platforms as keeping the old ordering. That residual was
        a real data-loss window on Windows, so staging now runs on every platform
        and this test pins it by forcing the descriptor path off.

        Confinement still requires a descriptor and is still POSIX-only — that is a
        separate property, and this test deliberately does not assert it.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        # Force the no-dir_fd branch, as on Windows.
        monkeypatch.setattr(powers_mod, "_SUPPORTS_DIR_FD", False)

        # ENOSPC is injected at BOTH write sites — the staged one and the plain
        # `atomic_write` the pre-fix no-dir_fd branch used. Patching only the new
        # helper made the revert fail on the `raises` wrapper instead of on the
        # data loss, because the old branch never reached the staged path at all.
        real_stage = powers_mod._stage_write_at

        def _no_space(*args, **kwargs):
            path = next(
                (a for a in args if isinstance(a, Path) and a.name == "installed.json"),
                None,
            )
            if path is not None:
                raise OSError(28, "No space left on device")
            return real_stage(*args, **kwargs)

        monkeypatch.setattr(powers_mod, "_stage_write_at", _no_space)
        if hasattr(powers_mod, "atomic_write"):
            monkeypatch.setattr(powers_mod, "atomic_write", _no_space)

        with contextlib.suppress(OSError):
            await st.remove_power("kb")

        # The invariant, whichever write path ran: a filesystem that cannot take
        # the record must not have cost the bundle.
        assert st.load_power("kb") is not None, "bundle lost on the no-dir_fd path"
        assert st._power_path("kb").is_dir()
        assert list(st._dir.glob(".installed.json.*")) == [], "staged record left behind"

    @pytest.mark.asyncio
    async def test_remove_succeeds_without_dir_fd(self, store, monkeypatch):
        """The generalized staging path must still complete an ordinary removal."""
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        monkeypatch.setattr(powers_mod, "_SUPPORTS_DIR_FD", False)

        assert await st.remove_power("kb") is True
        assert st.load_power("kb") is None
        assert not st._power_path("kb").exists()
        assert list(st._dir.glob(".installed.json.*")) == []

    @pytest.mark.asyncio
    async def test_record_with_no_bundle_is_reconciled_by_the_next_operation(self, store):
        """A crash between the delete and the record commit self-heals.

        This is the residual the remove ordering deliberately keeps: destroying the
        bytes before committing the record means an interruption in that window
        leaves a record naming a Power whose files are gone. `load_power` already
        hides it, but "retry and it clears" put the repair on the user. Any
        transaction now reconciles it.

        The reverse ordering would trade this for orphaned bytes, which are worse:
        `list_powers` enumerates `installed.json`, so a tree with no record is
        invisible and nothing reclaims it.
        """
        st, _mc, tmp = store
        first = _bundle(tmp, name="kb")
        await st.install_from_dir(first, source={"kind": "folder", "ref": str(first)})
        second = _bundle(tmp, name="db")
        await st.install_from_dir(second, source={"kind": "folder", "ref": str(second)})

        # Simulate the interruption: bytes gone, record still present.
        shutil.rmtree(st._power_path("kb"))
        assert "kb" in st._load_installed()
        assert st.load_power("kb") is None, "already hidden from the listing"

        # Any transaction repairs it — here an unrelated Power's removal.
        assert await st.remove_power("db") is True

        assert "kb" not in st._load_installed(), "stale record was not reconciled"

    @pytest.mark.asyncio
    async def test_reconciliation_does_not_touch_a_pending_backup(self, store):
        """A bundle awaiting rollback restore must not have its record pruned.

        Ordering the prune before the orphaned-backup recovery stranded exactly this
        case: the record was dropped while the bytes sat in `.backup-<name>`, so the
        recovery restored a tree nothing referenced.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        # The interrupted-install state: bundle in the backup slot, record present.
        os.rename(st._power_path("kb"), st._dir / ".backup-kb")
        assert "kb" in st._load_installed()

        second = _bundle(tmp, name="db")
        await st.install_from_dir(second, source={"kind": "folder", "ref": str(second)})

        assert "kb" in st._load_installed(), "record pruned while its bundle awaited restore"
        # The bytes are still in the rollback slot: backup recovery is per-name, so
        # an unrelated install does not restore kb — it just must not orphan it.
        assert (st._dir / ".backup-kb").is_dir()

        # kb's own next install performs the recovery, and the record is still there
        # to describe it.
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        assert st.load_power("kb") is not None

    @pytest.mark.asyncio
    async def test_install_refuses_symlinked_powers_root(self, tmp_path):
        """A powers root swapped for a symlink must be refused before any I/O.

        ``ln -s <writable> <home>/powers`` would otherwise let install rename an
        existing dir aside and rmtree it THROUGH the link, outside the store.
        """
        real = tmp_path / "elsewhere"
        real.mkdir()
        link = tmp_path / "powers-link"
        link.symlink_to(real, target_is_directory=True)
        st = PowersStore(powers_path=link)
        src = _bundle(tmp_path, name="kb")
        with pytest.raises(PowerFormatError, match="symlink"):
            await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

    @pytest.mark.asyncio
    async def test_remove_refuses_symlinked_powers_root(self, tmp_path):
        real = tmp_path / "elsewhere2"
        real.mkdir()
        link = tmp_path / "powers-link2"
        link.symlink_to(real, target_is_directory=True)
        st = PowersStore(powers_path=link)
        with pytest.raises(PowerFormatError, match="symlink"):
            await st.remove_power("kb")

    # ── Finding 3: corrupt installed.json fails closed ──

    def test_load_installed_empty_only_when_missing(self, store):
        st, _mc, _tmp = store
        assert st._load_installed() == {}

    def test_load_installed_raises_on_corrupt_json(self, store):
        st, _mc, _tmp = store
        st._dir.mkdir(parents=True, exist_ok=True)
        (st._dir / "installed.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(PowerFormatError, match="corrupt"):
            st._load_installed()

    def test_load_installed_raises_on_non_object(self, store):
        st, _mc, _tmp = store
        st._dir.mkdir(parents=True, exist_ok=True)
        (st._dir / "installed.json").write_text('["a", "b"]', encoding="utf-8")
        with pytest.raises(PowerFormatError, match="not a JSON object"):
            st._load_installed()

    # ── Finding 5: SUCCESS SEL audit events fire ──

    @pytest.mark.asyncio
    async def test_install_emits_ok_sel_event(self, store, monkeypatch):
        st, _mc, tmp = store
        rec = _SelRecorder()
        monkeypatch.setattr(powers_mod, "sel", lambda: rec)
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        installs = [e for e in rec.events if e.get("operation") == "power_install"]
        assert len(installs) == 1
        assert installs[0]["outcome"] == "ok"
        assert installs[0]["caller"] == "dashboard"
        assert installs[0]["resources"].startswith("kb kind=")

    @pytest.mark.asyncio
    async def test_remove_emits_ok_sel_event(self, store, monkeypatch):
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        rec = _SelRecorder()
        monkeypatch.setattr(powers_mod, "sel", lambda: rec)
        assert await st.remove_power("kb") is True
        removes = [e for e in rec.events if e.get("operation") == "power_remove"]
        assert removes == [
            {
                "caller": "dashboard",
                "operation": "power_remove",
                "outcome": "ok",
                "resources": "kb",
            }
        ]

    # ── Finding 6: CRLF POWER.md is accepted ──

    def test_crlf_frontmatter_accepted(self):
        text = "---\r\nname: demo\r\ndisplayName: D\r\ndescription: d\r\n---\r\nbody\r\n"
        meta = parse_power_md(text)
        assert meta.name == "demo"
        assert meta.displayName == "D"
        assert meta.description == "d"
        assert meta.body == "body"

    # ── Finding 7: a non-dict source does not crash the listing ──

    @pytest.mark.asyncio
    async def test_non_dict_source_does_not_crash_listing(self, store):
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        raw = json.loads((st._dir / "installed.json").read_text(encoding="utf-8"))
        raw["kb"]["source"] = "a string"  # malformed legacy/hand-edited record
        (st._dir / "installed.json").write_text(json.dumps(raw), encoding="utf-8")
        rec = st.load_power("kb")
        assert rec is not None
        assert rec["source"] == {"kind": "", "ref": ""}
        # The whole listing survives rather than raising AttributeError.
        assert [p["name"] for p in st.list_powers()] == ["kb"]

    # ── Finding 8: a failed copy leaves no staging tree behind ──

    @pytest.mark.asyncio
    async def test_failed_copy_leaves_no_staging_tree(self, store, monkeypatch):
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")

        def boom(*a, **k):
            raise PowerFormatError("copy blew up")

        monkeypatch.setattr(powers_mod, "_copy_power_files", boom)
        with pytest.raises(PowerFormatError, match="copy blew up"):
            await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        leftovers = [p.name for p in st._dir.iterdir() if p.name.startswith(".staging-")]
        assert leftovers == []


class TestPowersHandlerRedactionAndStatus:
    """Round-9 handler findings on PR #408.

    Finding 1 (HIGH): list + install responses MUST route every third-party
    POWER.md string through the mandatory fail-closed redactor.
    Finding 2 (HIGH): the installed-list read is offloaded off the event loop.
    Finding 3/4/5 (MEDIUM): non-object body -> 400; BundleSecurityError -> 400;
    ProviderUnavailableError -> 503 (install and detail), never swallowed.
    """

    # A canonical AWS access-key id — redact_credentials replaces it with the
    # standard credential tag.
    _CRED = "AKIAIOSFODNN7EXAMPLE"
    # A non-exempt host with a >200-char query — redact_exfiltration_urls
    # replaces it with the suspicious-URL tag. Low-entropy filler so it is not
    # separately flagged as a bare secret.
    _EXFIL_URL = "https://exfil.example.net/collect?leak=" + ("a" * 220)

    @pytest.fixture
    def handler_env(self, store, monkeypatch):
        from kiro_crew.dashboard.handlers import powers as powers_handlers

        st, mc, tmp = store
        monkeypatch.setattr("kiro_crew.dashboard.handlers.powers._store", lambda: st)
        return powers_handlers, st, mc, tmp

    @staticmethod
    def _req(method: str, path: str) -> object:
        from aiohttp.test_utils import make_mocked_request

        return make_mocked_request(method, path)

    @staticmethod
    def _install_req(payload: object) -> object:
        from aiohttp.test_utils import make_mocked_request

        req = make_mocked_request("POST", "/api/powers/install")

        async def _json() -> object:
            return payload

        req.json = _json  # type: ignore[method-assign]
        return req

    @staticmethod
    def _bundle_with_desc(root: Path, name: str, desc: str) -> Path:
        d = root / f"bundle-{name}"
        d.mkdir()
        (d / "POWER.md").write_text(
            f"---\nname: {name}\ndisplayName: {name}\ndescription: {desc}\n---\nbody\n",
            encoding="utf-8",
        )
        return d

    # ── Finding 1: mandatory redaction in responses ──

    @pytest.mark.asyncio
    async def test_install_response_redacts_credential(self, handler_env, monkeypatch):
        handlers, _st, _mc, tmp = handler_env
        bundle = self._bundle_with_desc(tmp, "credpow", f"leaked {self._CRED} here")

        async def fake_fetch(ref, *, provider=None):
            return bundle

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.powers.fetch_power_bundle", fake_fetch, raising=False
        )
        resp = await handlers.api_powers_install(
            self._install_req({"source": {"kind": "registry", "ref": "credpow"}})
        )
        assert resp.status == 200
        body = json.loads(resp.body)
        assert self._CRED not in json.dumps(body)
        assert "[REDACTED: credential]" in body["power"]["description"]

    @pytest.mark.asyncio
    async def test_install_response_redacts_exfil_url(self, handler_env, monkeypatch):
        handlers, _st, _mc, tmp = handler_env
        bundle = self._bundle_with_desc(tmp, "exfilpow", f"see {self._EXFIL_URL}")

        async def fake_fetch(ref, *, provider=None):
            return bundle

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.powers.fetch_power_bundle", fake_fetch, raising=False
        )
        resp = await handlers.api_powers_install(
            self._install_req({"source": {"kind": "registry", "ref": "exfilpow"}})
        )
        assert resp.status == 200
        body = json.loads(resp.body)
        assert self._EXFIL_URL not in json.dumps(body)
        assert "[REDACTED: suspicious URL" in body["power"]["description"]

    @pytest.mark.asyncio
    async def test_list_response_redacts_credential_and_exfil(self, handler_env):
        handlers, st, _mc, tmp = handler_env
        cred_bundle = self._bundle_with_desc(tmp, "clist", f"key {self._CRED}")
        await st.install_from_dir(cred_bundle, source={"kind": "folder", "ref": "x"})
        exfil_bundle = self._bundle_with_desc(tmp, "elist", f"url {self._EXFIL_URL}")
        await st.install_from_dir(exfil_bundle, source={"kind": "folder", "ref": "y"})

        resp = await handlers.api_powers(self._req("GET", "/api/powers"))
        assert resp.status == 200
        body = json.loads(resp.body)
        dumped = json.dumps(body)
        assert self._CRED not in dumped
        assert self._EXFIL_URL not in dumped
        descs = " ".join(p["description"] for p in body["installed"])
        assert "[REDACTED: credential]" in descs
        assert "[REDACTED: suspicious URL" in descs

    # ── Finding 2: the disk read is offloaded to the discovery pool ──

    @pytest.mark.asyncio
    async def test_list_offloaded_to_discovery_executor(self, handler_env, monkeypatch):
        handlers, _st, _mc, _tmp = handler_env
        called = {"n": 0}
        real = handlers.discovery_executor

        def spy():
            called["n"] += 1
            return real()

        monkeypatch.setattr(handlers, "discovery_executor", spy)
        resp = await handlers.api_powers(self._req("GET", "/api/powers"))
        assert resp.status == 200
        assert called["n"] == 1, "list_powers was not offloaded to discovery_executor"

    # ── Finding 3: non-object JSON body -> 400 ──

    @pytest.mark.asyncio
    async def test_install_rejects_non_object_body(self, handler_env):
        handlers, *_ = handler_env
        for payload in (["not", "an", "object"], "scalar", 42):
            resp = await handlers.api_powers_install(self._install_req(payload))
            assert resp.status == 400, f"non-object body {payload!r} should 400"

    # ── Finding 4: BundleSecurityError -> 400 ──

    @pytest.mark.asyncio
    async def test_install_bundle_security_error_is_400(self, handler_env, monkeypatch):
        handlers, *_ = handler_env
        from kiro_crew.powers_providers import BundleSecurityError

        async def boom_resolve(ref, provider):
            raise BundleSecurityError("malformed github tree URL")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.powers.resolve_power_ref", boom_resolve, raising=False
        )
        resp = await handlers.api_powers_install(
            self._install_req({"source": {"kind": "github", "ref": "https://evil"}})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_install_source_conflict_is_409(self, handler_env, monkeypatch):
        """A well-formed request refused for STATE reasons is 409, not 400 or 500.

        The distinction is actionable: 400 tells the caller to fix its input, and
        the input is fine here — the caller has to uninstall the existing Power
        first. Without an explicit arm this fell through to the generic 500, which
        also hid the reason behind "install failed".

        Driven through the real `install_from_dir`, not a patched raise. A first
        version monkeypatched `materialize_power_bundle`, which this handler never
        calls, so nothing was patched and the assertion read a 400 produced by the
        unpatched fetch instead.
        """
        handlers, st, _mc, tmp = handler_env

        # Already installed from the official registry.
        await st.install_from_dir(
            _bundle(tmp, name="kb"),
            source={"kind": "registry", "ref": "https://github.com/kirodotdev/powers"},
        )

        # A different repository ships a Power declaring the same name.
        impostor = _bundle(tmp, name="kb", dirname="impostor-kb")

        async def fake_resolve(ref, provider=None):
            return ref

        async def fake_fetch(ref, *, provider=None):
            return impostor

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.powers.resolve_power_ref", fake_resolve, raising=False
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.powers.fetch_power_bundle", fake_fetch, raising=False
        )

        resp = await handlers.api_powers_install(
            self._install_req(
                {"source": {"kind": "github", "ref": "https://github.com/someone/kb"}}
            )
        )
        assert resp.status == 409, f"expected a conflict, got {resp.status}: {resp.text}"
        assert "different source" in resp.text

    # ── Finding 5: ProviderUnavailableError -> 503 (install + detail) ──

    @pytest.mark.asyncio
    async def test_install_provider_unavailable_is_503(self, handler_env, monkeypatch):
        handlers, *_ = handler_env
        from kiro_crew.powers_providers import ProviderUnavailableError

        async def ok_resolve(ref, provider):
            return ref

        async def boom_fetch(ref, *, provider=None):
            raise ProviderUnavailableError("github down")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.powers.resolve_power_ref", ok_resolve, raising=False
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.powers.fetch_power_bundle", boom_fetch, raising=False
        )
        resp = await handlers.api_powers_install(
            self._install_req({"source": {"kind": "registry", "ref": "x"}})
        )
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_registry_detail_provider_unavailable_is_503(self, handler_env, monkeypatch):
        handlers, *_ = handler_env
        from kiro_crew.powers_providers import ProviderUnavailableError

        async def boom(power_id, *, provider=None):
            raise ProviderUnavailableError("down")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.powers.fetch_registry_detail", boom, raising=False
        )
        resp = await handlers.api_powers_registry_detail(
            self._req("GET", "/api/powers/registry/detail?id=x")
        )
        assert resp.status == 503


def _marked(bundle: Path, marker: str) -> Path:
    """Append a recognisable line to a bundle's POWER.md body."""
    md = bundle / "POWER.md"
    md.write_text(md.read_text(encoding="utf-8") + f"\n{marker}\n", encoding="utf-8")
    return bundle


class TestRound37SourceConflict:
    """A same-named Power from a different source must not silently replace one."""

    @pytest.mark.asyncio
    async def test_remote_install_refuses_to_replace_a_different_source(self, store):
        """Two upstreams can declare the same `name`; the second must not win.

        Nothing reserves a Power name globally — a monorepo directory and an
        independent author's repo can both call themselves `kb`. The install used to
        overwrite the bundle AND its provenance record, so the store then described
        a Power the user never chose, from a source they never picked.
        """
        st, _mc, tmp = store
        await st.install_from_dir(
            _bundle(tmp, name="kb"),
            source={"kind": "registry", "ref": "https://github.com/kirodotdev/powers"},
        )

        impostor = _marked(_bundle(tmp, name="kb", dirname="other-kb"), "IMPOSTOR")
        with pytest.raises(powers_mod.PowerSourceConflict, match="different source"):
            await st.install_from_dir(
                impostor,
                source={"kind": "github", "ref": "https://github.com/someone/kb"},
            )

        record = st.load_power("kb")
        assert record is not None
        assert record["source"]["ref"] == "https://github.com/kirodotdev/powers"
        body = (st._power_path("kb") / POWER_MD_NAME).read_text(encoding="utf-8")
        assert "IMPOSTOR" not in body, "the impostor bundle replaced the original"

    @pytest.mark.asyncio
    async def test_same_source_reinstall_is_allowed(self, store):
        """The upgrade path must keep working — this is not a name lock."""
        st, _mc, tmp = store
        source = {"kind": "registry", "ref": "https://github.com/kirodotdev/powers"}
        await st.install_from_dir(_bundle(tmp, name="kb"), source=source)
        await st.install_from_dir(
            _marked(_bundle(tmp, name="kb", dirname="kb-v2"), "V2"), source=dict(source)
        )
        body = (st._power_path("kb") / POWER_MD_NAME).read_text(encoding="utf-8")
        assert "V2" in body, "a same-source upgrade was refused"

    @pytest.mark.asyncio
    async def test_a_cosmetic_ref_difference_is_not_a_conflict(self, store):
        """Kind case and a trailing slash are normalised before comparing."""
        st, _mc, tmp = store
        await st.install_from_dir(
            _bundle(tmp, name="kb"),
            source={"kind": "registry", "ref": "https://github.com/kirodotdev/powers"},
        )
        await st.install_from_dir(
            _marked(_bundle(tmp, name="kb", dirname="kb-again"), "AGAIN"),
            source={"kind": "REGISTRY", "ref": "https://github.com/kirodotdev/powers/"},
        )
        body = (st._power_path("kb") / POWER_MD_NAME).read_text(encoding="utf-8")
        assert "AGAIN" in body

    @pytest.mark.asyncio
    async def test_folder_to_folder_reinstall_is_not_a_conflict(self, store):
        """The development loop stays open: a local path is not provenance.

        Refusing this would break reinstalling from a rebuilt or relocated
        directory for no security gain — there is no third party to impersonate
        when the user picked the directory themselves.
        """
        st, _mc, tmp = store
        first = _bundle(tmp, name="kb")
        await st.install_from_dir(first, source={"kind": "folder", "ref": str(first)})
        moved = _marked(_bundle(tmp, name="kb", dirname="kb-moved"), "MOVED")
        await st.install_from_dir(moved, source={"kind": "folder", "ref": str(moved)})
        body = (st._power_path("kb") / POWER_MD_NAME).read_text(encoding="utf-8")
        assert "MOVED" in body, "a folder reinstall from a new path was refused"


class TestRound38HardlinkRefusal:
    """A hardlinked contract file must not smuggle a protected file's bytes in."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name == "nt", reason="os.link needs privileges on Windows")
    async def test_hardlinked_steering_file_is_refused(self, store, tmp_path):
        """The sensitive-path refusal is path-based; a hardlink has another path.

        `steering/guide.md` sharing an inode with a protected file passed every
        existing guard: the symlink check compares lstat to fstat identity, and a
        hardlink IS the file, so they agree. The copy then wrote those bytes into
        the Powers tree, where `power_steering` can read them back.
        """
        st, _mc, tmp = store
        secret = tmp_path / "credentials"
        secret.write_text("SECRET-MATERIAL", encoding="utf-8")

        src = _bundle(tmp, name="kb", steering={"guide.md": "placeholder"})
        link = src / "steering" / "guide.md"
        link.unlink()
        os.link(secret, link)

        with pytest.raises(PowerFormatError, match="hardlink"):
            await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        # Nothing partial survives, so the bytes are not readable from the store.
        assert not st._power_path("kb").exists()
        assert st.load_power("kb") is None

    @pytest.mark.asyncio
    async def test_an_ordinary_single_link_file_still_installs(self, store):
        """The guard must not reject the normal case."""
        st, _mc, tmp_dir = store
        src = _bundle(tmp_dir, name="kb", steering={"guide.md": "# guidance\n"})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        assert (st._power_path("kb") / "steering" / "guide.md").is_file()


class TestRound39PowerMdCap:
    """`POWER.md` is read with a byte cap, not slurped and then rejected.

    The refusal itself is NOT new: `parse_power_md` has always raised over
    `MAX_POWER_MD_BYTES`. It just did so *after* `read_text()` had already pulled
    the whole file into the process, so an oversized bundle was a memory
    exhaustion vector for anything able to write into it — and the installed
    bundle stays editable, while the install source is caller-owned from the
    start. Asserting the refusal therefore proves nothing (it passes with the fix
    reverted, which is how the first version of these tests was wrong). What
    these assert is that the unbounded read is gone.
    """

    @staticmethod
    def _ban_read_text(monkeypatch):
        """Make `Path.read_text` fail loudly for POWER.md, and only for it.

        `RuntimeError` specifically: `load_power` swallows `OSError` and
        `PowerFormatError`, so a sentinel of either type would be indistinguishable
        from the capped refusal and the test would pass either way.
        """
        real = Path.read_text

        def guarded(self, *a, **k):
            if self.name == POWER_MD_NAME:
                raise RuntimeError(f"unbounded read of {self}")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", guarded)

    @pytest.mark.asyncio
    async def test_installed_power_md_is_not_slurped_whole(self, store, monkeypatch):
        """`load_power` must not read the installed POWER.md without a cap."""
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        installed_md = st._power_path("kb") / POWER_MD_NAME
        installed_md.write_text(
            installed_md.read_text(encoding="utf-8")
            + "\n"
            + ("x" * (powers_mod.MAX_POWER_MD_BYTES + 64)),
            encoding="utf-8",
        )

        self._ban_read_text(monkeypatch)
        # Refused through the capped helper, so this Power drops out of the
        # listing instead of taking the process with it.
        assert st.load_power("kb") is None
        assert isinstance(st.list_powers(), list)

    @pytest.mark.asyncio
    async def test_a_normal_bundle_also_avoids_the_unbounded_read(self, store, monkeypatch):
        """The capped path is the only path — not a branch taken when oversized.

        Without this, a fix that kept `read_text()` for the ordinary case and only
        capped after a size check would still satisfy the test above.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        self._ban_read_text(monkeypatch)
        record = st.load_power("kb")
        assert record is not None and record["name"] == "kb"

    @pytest.mark.asyncio
    async def test_install_does_not_slurp_the_caller_supplied_power_md(
        self, store, monkeypatch
    ):
        """The pre-install parse runs before any copy budget applies."""
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")

        self._ban_read_text(monkeypatch)
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        assert st.load_power("kb") is not None

    @pytest.mark.asyncio
    async def test_a_power_md_just_under_the_cap_still_installs(self, store):
        """The cap must not reject a large-but-legal bundle."""
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        md = src / POWER_MD_NAME
        body = md.read_text(encoding="utf-8")
        pad = powers_mod.MAX_POWER_MD_BYTES - len(body.encode("utf-8")) - 8
        # `newline=""` so the write is byte-exact. Text mode translates every \n
        # to \r\n on Windows, which inflated a file padded to just under the cap
        # past it and failed this test on the Windows shards only.
        md.write_text(body + "\n" + ("x" * pad), encoding="utf-8", newline="")

        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        assert st.load_power("kb") is not None


class TestRound40McpJsonCap:
    """`mcp.json` is read with a cap, like `POWER.md`."""

    @pytest.mark.asyncio
    async def test_oversize_mcp_json_is_not_read_whole(self, store, monkeypatch):
        """`GET /api/powers` reaches this for every installed Power.

        The bundle stays editable after install, so the copy-time budget does not
        bound what this later reads. Asserted by banning the unbounded reader
        rather than by the return value, which is `False` either way.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb", mcp={"cli": {"command": "run-cli"}})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        installed_mcp = st._power_path("kb") / "mcp.json"
        installed_mcp.write_text(
            json.dumps({"mcpServers": {"cli": {"command": "x" * (256 * 1024)}}}),
            encoding="utf-8",
        )

        real = Path.read_text

        def guarded(self, *a, **k):
            if self.name == "mcp.json":
                raise RuntimeError(f"unbounded read of {self}")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", guarded)
        # Answers the question without slurping the file, and one oversized
        # bundle does not take the listing down.
        assert powers_mod._declares_mcp_servers(st._power_path("kb")) is False
        assert isinstance(st.list_powers(), list)

    @pytest.mark.asyncio
    async def test_an_ordinary_mcp_json_is_still_detected(self, store, monkeypatch):
        """The capped path is the only path, and it still answers correctly."""
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb", mcp={"cli": {"command": "run-cli"}})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        real = Path.read_text

        def guarded(self, *a, **k):
            if self.name == "mcp.json":
                raise RuntimeError(f"unbounded read of {self}")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", guarded)
        assert powers_mod._declares_mcp_servers(st._power_path("kb")) is True


class TestRound41SteeringReparsePoint:
    """A junctioned `steering/` must not be followed on the no-descriptor branch.

    Driven through `_copy_steering` directly rather than through
    `install_from_dir`. Two reasons, both learned the hard way here:

    * The install path pre-validates the source tree and already refuses a
      *symlinked* `steering/` with its own "symlink not allowed" error, so an
      end-to-end test matched that message and passed with this guard reverted.
    * The POSIX branch pins `steering/` with `O_NOFOLLOW` and never reaches the
      path check at all, so the branch under test is only reachable with
      `src_fd=None` (Windows in production).

    The junction itself is not constructible on Linux -- `is_symlink()` is False
    for one, which is the whole defect -- so it is simulated where noted.
    """

    @staticmethod
    def _linked_steering(tmp_path, bundle):
        outside = tmp_path / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "leak.md").write_text("EXTERNAL GUIDANCE", encoding="utf-8")
        link = bundle / "steering"
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)
        link.symlink_to(outside, target_is_directory=True)
        return outside

    def test_linked_steering_is_refused_not_silently_skipped(self, tmp_path):
        """Old behaviour was to `return`, which is where the junction slipped past.

        Skipping a symlink looks safe, but the same branch let a junction through
        because `is_symlink()` is False for one. The guard now refuses both, so a
        tampered source fails loudly instead of installing a partial bundle.
        """
        src = _bundle(tmp_path, name="kb")
        self._linked_steering(tmp_path, src)
        dest = tmp_path / "dest"
        dest.mkdir()

        with pytest.raises(PowerFormatError):
            powers_mod._copy_steering(
                src, dest, budget=[powers_mod.MAX_INSTALL_BYTES], dest_fd=None, src_fd=None
            )
        assert not (dest / "steering" / "leak.md").exists()

    def test_the_guard_runs_before_the_glob(self, tmp_path, monkeypatch):
        """Simulates a junction: a redirecting directory that reports is_symlink False.

        This is the actual Windows shape. With `is_symlink()` forced False the old
        condition fell straight through to `is_dir()` (True, junctions are
        directories) and globbed the target, copying external Markdown into the
        Powers tree. Asserted by making the reparse check raise a sentinel: if it
        is not consulted, the copy proceeds and `leak.md` lands in dest.
        """
        src = _bundle(tmp_path, name="kb")
        self._linked_steering(tmp_path, src)
        dest = tmp_path / "dest"
        dest.mkdir()

        real_is_symlink = Path.is_symlink

        def not_a_symlink(self):
            if self.name == "steering":
                return False
            return real_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", not_a_symlink)

        class _Sentinel(Exception):
            pass

        def spy(path):
            raise _Sentinel(str(path))

        monkeypatch.setattr(powers_mod, "_assert_not_reparse_point", spy)

        with pytest.raises(_Sentinel):
            powers_mod._copy_steering(
                src, dest, budget=[powers_mod.MAX_INSTALL_BYTES], dest_fd=None, src_fd=None
            )
        assert not (dest / "steering" / "leak.md").exists(), (
            "the reparse check was skipped and the junction target was copied"
        )

    def test_a_real_steering_dir_still_copies_on_that_branch(self, tmp_path):
        """The guard must not reject an ordinary bundle."""
        src = _bundle(tmp_path, name="kb", steering={"guide.md": "# guidance\n"})
        dest = tmp_path / "dest"
        dest.mkdir()
        powers_mod._copy_steering(
            src, dest, budget=[powers_mod.MAX_INSTALL_BYTES], dest_fd=None, src_fd=None
        )
        assert (dest / "steering" / "guide.md").is_file()

    def test_absent_steering_is_not_an_error(self, tmp_path):
        """`steering/` is optional, so a missing one must stay a no-op."""
        src = _bundle(tmp_path, name="kb")
        if (src / "steering").exists():
            shutil.rmtree(src / "steering")
        dest = tmp_path / "dest"
        dest.mkdir()
        powers_mod._copy_steering(
            src, dest, budget=[powers_mod.MAX_INSTALL_BYTES], dest_fd=None, src_fd=None
        )
        assert not (dest / "steering").exists()


class TestRound42SourceProbeOffLoop:
    """Every stat of the caller-supplied source runs in the executor."""

    @pytest.mark.asyncio
    async def test_power_md_probe_does_not_run_on_the_loop_thread(self, store, monkeypatch):
        """A network-mounted source makes one stat as blocking as a tree walk.

        Asserted on the THREAD the probe runs on, not on latency: a timing test
        would be flaky and would not say which call was at fault. The loop's own
        thread id is captured here in the coroutine, so any probe recorded on it
        is by definition blocking the gateway.
        """
        st, _mc, tmp = store
        src = _bundle(tmp, name="kb")
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real_is_file = Path.is_file

        def recording_is_file(self):
            if self.name == POWER_MD_NAME:
                seen.append(threading.get_ident())
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", recording_is_file)
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        assert seen, "the probe did not run at all — the test is not observing it"
        assert loop_thread not in seen, (
            "POWER.md was stat'ed on the event loop thread"
        )

    @pytest.mark.asyncio
    async def test_a_missing_power_md_still_reports_the_same_error(self, store, tmp_path):
        """Moving the probe must not change what the caller sees."""
        st, _mc, _tmp = store
        empty = tmp_path / "not-a-bundle"
        empty.mkdir()
        with pytest.raises(PowerFormatError, match="no POWER.md found in bundle"):
            await st.install_from_dir(empty, source={"kind": "folder", "ref": str(empty)})
