"""Tests for the read-only half of the Powers pull model.

Covers the two tools that never start a process. The confinement cases matter
most: the steering filename reaches the filesystem and originates in a place the
agent may have read from third-party content, so it is treated as untrusted
input rather than as a parameter the model got right.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from kiro_crew import powers_tools
from kiro_crew.powers import PowersStore
from kiro_crew.powers_tools import PowerToolError, power_list, power_steering


def _power_md(name: str = "kb", *, display: str = "KB") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"displayName: {display}\n"
        "description: A knowledge power.\n"
        "keywords: [\"docs\"]\n"
        "author: Someone\n"
        "---\n\n# Guide\n\nBody.\n"
    )


def _bundle(root: Path, name: str = "kb", *, mcp: dict | None = None, steering: dict | None = None) -> Path:
    d = root / f"src-{name}"
    (d / "steering").mkdir(parents=True, exist_ok=True)
    (d / "POWER.md").write_text(_power_md(name), encoding="utf-8")
    if mcp is not None:
        (d / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    for fname, body in (steering or {}).items():
        (d / "steering" / fname).write_text(body, encoding="utf-8")
    return d


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A PowersStore over tmp_path, with the tools pointed at the same root."""
    powers_root = tmp_path / "powers"
    st = PowersStore(powers_path=powers_root)
    monkeypatch.setattr(powers_tools, "_store", lambda: st)
    monkeypatch.setattr(powers_tools, "powers_dir", lambda: powers_root)
    return st, tmp_path


class TestPowerList:
    @pytest.mark.asyncio
    async def test_lists_installed_powers_with_server_names(self, store):
        st, tmp = store
        src = _bundle(
            tmp,
            "supabase",
            mcp={"mcpServers": {"supabase": {"command": "npx", "args": ["-y", "x"]}}},
        )
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        result = power_list()

        assert [p["name"] for p in result["powers"]] == ["supabase"]
        assert result["powers"][0]["mcpServers"] == ["supabase"]
        assert result["powers"][0]["kind"] == "mcp"

    @pytest.mark.asyncio
    async def test_knowledge_power_reports_no_servers(self, store):
        st, tmp = store
        src = _bundle(tmp, "terraform", steering={"modules.md": "# Modules\n"})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        (entry,) = power_list()["powers"]

        assert entry["mcpServers"] == []
        assert entry["steeringFiles"] == ["modules.md"]

    @pytest.mark.asyncio
    async def test_server_command_is_never_returned(self, store):
        """Names only. The argv belongs to the spawn path, which is not in this change.

        Asserted on the serialized payload rather than on the shape, so a future
        field that happens to carry the command through cannot pass silently.
        """
        st, tmp = store
        src = _bundle(
            tmp,
            "supabase",
            mcp={"mcpServers": {"supabase": {"command": "npx", "args": ["-y", "secret-pkg"]}}},
        )
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        payload = json.dumps(power_list())

        assert "supabase" in payload
        assert "npx" not in payload
        assert "secret-pkg" not in payload

    @pytest.mark.asyncio
    async def test_malformed_mcp_json_does_not_break_the_listing(self, store):
        """A broken bundle still lists — it is still installed and still removable."""
        st, tmp = store
        src = _bundle(tmp, "broken", mcp={"mcpServers": {"s": {"command": "x"}}})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        (st._dir / "broken" / "mcp.json").write_text("{not json", encoding="utf-8")

        (entry,) = power_list()["powers"]

        assert entry["name"] == "broken"
        assert entry["mcpServers"] == []

    def test_registry_listing_is_refused_not_ignored(self, store):
        """`installed_only=False` is unimplemented, so it must fail loudly."""
        with pytest.raises(PowerToolError, match="installed Powers only"):
            power_list(installed_only=False)


class TestPowerSteering:
    @pytest.mark.asyncio
    async def test_reads_a_steering_file(self, store):
        st, tmp = store
        src = _bundle(tmp, "kb", steering={"getting-started.md": "# Start here\n"})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        result = power_steering("kb", "getting-started.md")

        # Newlines normalized before comparing: the fixture is written in text
        # mode, which translates "\n" to "\r\n" on Windows, so an exact match
        # asserted the platform's line ending rather than the tool's behaviour.
        assert result["content"].replace("\r\n", "\n") == "# Start here\n"

    @pytest.mark.asyncio
    async def test_output_is_redacted(self, store):
        """Guidance is third-party text on its way into a transcript."""
        st, tmp = store
        secret = "AKIA" + "I" * 16
        src = _bundle(tmp, "kb", steering={"creds.md": f"key: {secret}\n"})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        result = power_steering("kb", "creds.md")

        assert secret not in result["content"]

    @pytest.mark.asyncio
    async def test_oversize_steering_file_is_refused_not_truncated(self, store, monkeypatch):
        """Over the cap is a refusal, and that is deliberate.

        The read now goes through `hooks.safe_read_file_bytes_nolink`, which raises
        rather than truncating. That is the better contract here: handing an agent a
        guidance document cut off mid-sentence and calling it the Power's guidance is
        worse than saying the file is too large.
        """
        st, tmp = store
        monkeypatch.setattr(powers_tools, "MAX_STEERING_BYTES", 1024)
        src = _bundle(tmp, "kb", steering={"big.md": "x" * (512 * 1024)})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        with pytest.raises(PowerToolError, match="exceeds the"):
            power_steering("kb", "big.md")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("bad", "reason"),
        [
            ("../secret.md", "not a path"),
            ("sub/dir.md", "not a path"),
            ("sub\\dir.md", "not a path"),
            (".hidden.md", "not a path"),
            ("notes.txt", "only .md"),
            ("POWER.md", "no such steering file"),
            ("", "invalid steering file name"),
        ],
    )
    async def test_refuses_anything_but_a_plain_md_leaf(self, store, bad, reason):
        """Asserts the REASON, not merely that something raised.

        A first version matched any `PowerToolError`, which made the traversal
        cases vacuous: `../secret.md` reached the filesystem, missed, and raised
        "no such steering file" — so the test passed with the path guard removed.
        Pinning the message means a case can only pass through the guard that is
        supposed to stop it.
        """
        st, tmp = store
        src = _bundle(tmp, "kb", steering={"ok.md": "# ok\n"})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        # Make the traversal target REAL, so a naive join succeeds instead of
        # failing on a missing file.
        (st._dir / "kb" / "secret.md").write_text("ESCAPED", encoding="utf-8")

        with pytest.raises(PowerToolError, match=reason):
            power_steering("kb", bad)

    @pytest.mark.asyncio
    async def test_refuses_an_invalid_power_name(self, store):
        with pytest.raises(PowerToolError, match="invalid power name"):
            power_steering("../evil", "ok.md")

    @pytest.mark.asyncio
    async def test_refuses_a_power_that_is_not_installed(self, store):
        with pytest.raises(PowerToolError, match="not installed"):
            power_steering("absent", "ok.md")

    @pytest.mark.asyncio
    async def test_symlinked_steering_file_is_refused(self, store, tmp_path):
        """A symlink inside steering/ must not become a read of its target.

        Install refuses symlinks in the bundle, but the tree stays writable by the
        same user afterwards, so the read path cannot assume install-time vetting
        still holds.
        """
        st, tmp = store
        src = _bundle(tmp, "kb", steering={"ok.md": "# ok\n"})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        secret = tmp_path / "outside-secret.md"
        secret.write_text("SECRET-VALUE", encoding="utf-8")
        link = st._dir / "kb" / "steering" / "link.md"
        os.symlink(secret, link)

        with pytest.raises(PowerToolError, match="symlink"):
            power_steering("kb", "link.md")

    @pytest.mark.asyncio
    async def test_symlinked_steering_directory_is_refused(self, store, tmp_path):
        """The directory is a redirectable component, not just the leaf.

        A symlinked `steering/` sends every read inside it elsewhere, and the
        files found there are ordinary regular files that `O_NOFOLLOW` on the leaf
        does not object to. The decoy therefore holds a REAL `.md` file, so a
        path-resolving implementation succeeds and discloses it — the disclosure,
        not an exception, is the signal.
        """
        st, tmp = store
        src = _bundle(tmp, "kb", steering={"ok.md": "# ok\n"})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        decoy = tmp_path / "decoy-steering"
        decoy.mkdir()
        (decoy / "ok.md").write_text("SECRET-VALUE", encoding="utf-8")

        steering = st._dir / "kb" / "steering"
        shutil.rmtree(steering)
        os.symlink(decoy, steering)

        with pytest.raises(PowerToolError, match="steering directory is a symlink"):
            power_steering("kb", "ok.md")

    @pytest.mark.asyncio
    async def test_steering_read_goes_through_the_safety_gate(self, store, monkeypatch):
        """The gate is the only reader — no bespoke open remains.

        Asserted by making the gate refuse: if `power_steering` still had its own
        reader, the call would succeed and return content anyway. This replaces an
        earlier byte-counting test whose `os.read` patch did not observe
        `Path.read_bytes` and so passed with the bounded read reverted.
        """
        st, tmp = store
        src = _bundle(tmp, "kb", steering={"ok.md": "# ok\n"})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})

        monkeypatch.setattr(powers_tools, "safe_read_file_bytes_nolink", lambda *a, **k: None)

        with pytest.raises(PowerToolError, match="refused by the file-safety gate"):
            power_steering("kb", "ok.md")

    @pytest.mark.asyncio
    async def test_oversize_mcp_json_reports_no_servers(self, store, monkeypatch):
        """An oversized mcp.json degrades to "no servers" without a full read.

        Same technique as above: the unbounded call is made to raise, so passing
        requires the bounded path.
        """
        st, tmp = store
        monkeypatch.setattr(powers_tools, "_MAX_MCP_JSON_BYTES", 256)
        src = _bundle(tmp, "kb", mcp={"mcpServers": {"s": {"command": "x"}}})
        await st.install_from_dir(src, source={"kind": "folder", "ref": str(src)})
        (st._dir / "kb" / "mcp.json").write_text(
            '{"mcpServers": {' + ",".join(f'"s{i}": {{}}' for i in range(200)) + "}}",
            encoding="utf-8",
        )

        def _no_unbounded_read(self, *a, **kw):
            raise AssertionError(f"read the whole file instead of a bounded read: {self}")

        monkeypatch.setattr(Path, "read_bytes", _no_unbounded_read)
        (entry,) = power_list()["powers"]

        assert entry["mcpServers"] == []


class TestToolSurface:
    def test_both_tools_are_advertised_and_dispatchable(self):
        """A tool the server does not advertise is unreachable to the agent."""
        from kiro_crew import mcp_core

        names = {t["name"] for t in mcp_core._list_tools()}
        assert {"power_list", "power_steering"} <= names

    def test_tool_error_text_is_redacted(self):
        """A refusal must not persist the credential-shaped input it refused.

        The error message embeds the caller's own arguments, so a filename carrying
        a credential would have been written verbatim into the transcript by the
        very refusal meant to protect it.
        """
        from kiro_crew import mcp_core

        secret = "AKIA" + "J" * 16
        # `.txt` so the extension refusal fires and ECHOES the filename. A first
        # version used `.md`, which reached the "power is not installed" check
        # instead — that message names only the power, so the test passed with the
        # redaction reverted.
        out = mcp_core._call_tool_inner(
            "power_steering", {"power": "kb", "file": f"{secret}.txt"}
        )
        assert out.startswith("Error:")
        assert "only .md" in out, f"expected the extension refusal, got: {out}"
        assert secret not in out, "the refusal echoed the credential it refused"

    def test_execution_tools_are_absent_from_this_change(self):
        """`power_learn` / `power_use` need consent + sandboxed spawn; not here yet.

        Asserted so the read-only scope of this change is enforced rather than
        merely described — the same discipline `PowersTab.test.tsx` uses to assert
        the absence of an activation toggle.
        """
        from kiro_crew import mcp_core

        names = {t["name"] for t in mcp_core._list_tools()}
        assert "power_learn" not in names
        assert "power_use" not in names
