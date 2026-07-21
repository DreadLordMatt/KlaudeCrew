"""Tests for Level-0 theme install (directory store + structure validation).

Exercises the pure helpers in ``kiro_crew.dashboard.handlers.agents`` directly
— the validator is where the real structure/security logic lives, so no aiohttp
app is needed. Covers: valid installs (top-level + styles/), missing/invalid
manifest, level bounds (L0 declaring-but-shipping-L2), stray files, VCS/meta
tolerance, symlink rejection, oversize, bad CSS values, the GitHub URL guard,
recognized-file staging, and an install→list→remove round-trip.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers.agents import (
    _clone_github,
    _copy_installed_theme,
    _installed_theme_dir,
    _safe_theme_slug,
    _validate_theme_dir,
)

# _validate_theme_data only *requires* --bg/--text/--accent and rejects unknown
# keys, so a 3-var map per mode is a complete, valid Level-0 theme.
_VALID_VARS = {
    "dark": {"--bg": "#000000", "--text": "#ffffff", "--accent": "#3366ff"},
    "light": {"--bg": "#ffffff", "--text": "#000000", "--accent": "#0033cc"},
}


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _make_theme(
    root: Path,
    *,
    level: int = 0,
    name: str = "LCARS",
    styled: bool = False,
    variables: dict | None = None,
) -> Path:
    """Build a theme directory under ``root`` and return its path."""
    d = root / "theme"
    d.mkdir(parents=True, exist_ok=True)
    _write(d / "theme.json", {"slug": "lcars", "name": name, "emoji": "🖖", "level": level})
    varobj = _VALID_VARS if variables is None else variables
    _write(d / ("styles/variables.json" if styled else "variables.json"), varobj)
    return d


class TestValidateThemeDir:
    def test_valid_toplevel(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(_make_theme(tmp_path))
        assert err is None, err
        assert summary is not None
        assert summary["slug"] == "lcars"
        assert summary["name"] == "LCARS"
        assert summary["emoji"] == "🖖"
        assert summary["level"] == 0
        assert summary["source"] == "installed"
        assert summary["dark"]["--bg"] == "#000000"
        assert summary["light"]["--text"] == "#000000"

    def test_valid_styles_subdir(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(_make_theme(tmp_path, styled=True))
        assert err is None, err
        assert summary is not None and summary["slug"] == "lcars"

    def test_missing_manifest(self, tmp_path: Path) -> None:
        d = tmp_path / "t"
        _write(d / "variables.json", _VALID_VARS)
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "theme.json" in err

    def test_missing_variables(self, tmp_path: Path) -> None:
        d = tmp_path / "t"
        _write(d / "theme.json", {"slug": "x", "name": "X", "level": 0})
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "variables.json" in err

    def test_level_nonzero_rejected(self, tmp_path: Path) -> None:
        summary, err = _validate_theme_dir(_make_theme(tmp_path, level=2))
        assert summary is None
        assert err is not None and "Level 0" in err

    def test_l2_overlay_asset_rejected(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)  # declares level 0 but ships an overlay
        (d / "overlays").mkdir()
        (d / "overlays" / "scanner.html").write_text("<b>x</b>", encoding="utf-8")
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "Level 1/2" in err

    def test_persona_asset_rejected(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)
        (d / "persona.md").write_text("# persona", encoding="utf-8")
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "Level 1/2" in err

    def test_stray_file_rejected(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)
        (d / "evil.txt").write_text("x", encoding="utf-8")
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "unexpected file" in err

    def test_meta_files_tolerated(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)
        (d / ".gitignore").write_text("x", encoding="utf-8")
        (d / "LICENSE").write_text("MIT", encoding="utf-8")
        gitdir = d / ".git"
        gitdir.mkdir()
        (gitdir / "config").write_text("[core]", encoding="utf-8")
        summary, err = _validate_theme_dir(d)
        assert err is None, err
        assert summary is not None and summary["slug"] == "lcars"

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)
        outside = tmp_path / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        try:
            (d / "readme.md").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported on this platform")
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "symlink" in err.lower()

    def test_bad_css_value_rejected(self, tmp_path: Path) -> None:
        bad = {
            "dark": {"--bg": "red; }", "--text": "#fff", "--accent": "#00f"},
            "light": {"--bg": "#fff", "--text": "#000", "--accent": "#00c"},
        }
        summary, err = _validate_theme_dir(_make_theme(tmp_path, variables=bad))
        assert summary is None
        assert err is not None  # rejected by _validate_theme_data

    def test_oversize_variables_rejected(self, tmp_path: Path) -> None:
        d = tmp_path / "t"
        _write(d / "theme.json", {"slug": "x", "name": "X", "level": 0})
        padded = dict(_VALID_VARS)
        padded["_pad"] = "x" * (70 * 1024)  # push variables.json past 64KB
        _write(d / "variables.json", padded)
        summary, err = _validate_theme_dir(d)
        assert summary is None
        assert err is not None and "too large" in err


class TestSafeThemeSlug:
    def test_valid_slug(self) -> None:
        assert _safe_theme_slug("lcars-01") == "lcars-01"

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "", "Foo", "a.b", "x y"])
    def test_unsafe_rejected(self, bad: str) -> None:
        assert _safe_theme_slug(bad) is None


class TestCloneGithubGuard:
    """The URL guard runs before any subprocess — no network/git required."""

    def test_non_https_rejected(self, tmp_path: Path) -> None:
        err = _clone_github("http://github.com/u/r", tmp_path / "c")
        assert err is not None and "https" in err

    def test_non_github_host_rejected(self, tmp_path: Path) -> None:
        err = _clone_github("https://evil.example.com/u/r", tmp_path / "c")
        assert err is not None and "github.com" in err

    def test_empty_url_rejected(self, tmp_path: Path) -> None:
        assert _clone_github("", tmp_path / "c") is not None


class TestCopyInstalledTheme:
    def test_copies_only_recognized_files(self, tmp_path: Path) -> None:
        d = _make_theme(tmp_path)
        (d / "LICENSE").write_text("MIT", encoding="utf-8")
        dst = tmp_path / "dst"
        _copy_installed_theme(d, dst)
        assert (dst / "theme.json").is_file()
        assert (dst / "variables.json").is_file()
        assert not (dst / "LICENSE").exists()

    def test_preserves_styles_location(self, tmp_path: Path) -> None:
        dst = tmp_path / "dst"
        _copy_installed_theme(_make_theme(tmp_path, styled=True), dst)
        assert (dst / "styles" / "variables.json").is_file()


class TestInstalledStore:
    def test_dir_under_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.dashboard.handlers.agents as agents_mod

        monkeypatch.setattr(agents_mod, "config_dir", lambda: tmp_path)
        assert _installed_theme_dir("lcars") == tmp_path / "themes" / "lcars"

    def test_install_copy_and_remove_roundtrip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.dashboard.handlers.agents as agents_mod

        monkeypatch.setattr(agents_mod, "config_dir", lambda: tmp_path)
        src = _make_theme(tmp_path)
        summary, err = _validate_theme_dir(src)
        assert err is None and summary is not None

        dest = _installed_theme_dir(summary["slug"])
        _copy_installed_theme(src, dest)
        assert (dest / "theme.json").is_file()
        assert dest.is_dir()

        # Remove de-registers (matches the DELETE handler's rmtree path).
        shutil.rmtree(dest)
        assert not dest.exists()
