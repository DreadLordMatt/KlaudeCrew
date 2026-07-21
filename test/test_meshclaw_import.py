"""Tests for the legacy ~/.meshclaw -> ~/.kirocrew one-time import migration.

All tests operate against throwaway tmp HOME dirs via monkeypatched seams
(``_home`` and ``config_dir``); the real ``~/.meshclaw`` is NEVER touched.

Covers: detect true/false cases + metadata, idempotency (done marker /
backup-present guards), additive never-clobber, old-wins overwrites, crons
imported paused, manifest parity (apps full vs shared-data vs stub-skip,
skills skip rules, json merges, mcp/.env absent-only, config.json never
copied), the reversible rename, the two-phase intent flow, and the
all-or-nothing copy phase (fail-closed atomic backup, journaled additive
residue, full rollback, bounded rename retry, uncaught-exception seam).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.migrations.meshclaw_import as mod


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _raise_oserror(*_a, **_k):
    raise OSError("simulated rename failure")


def _raise_runtimeerror(*_a, **_k):
    raise RuntimeError("simulated step failure")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    src = home / ".meshclaw"
    dest = home / ".kirocrew"
    src.mkdir(parents=True)
    dest.mkdir(parents=True)
    monkeypatch.setattr(mod, "_home", lambda: home)
    monkeypatch.setattr(mod, "config_dir", lambda: dest)
    mod._size_estimate_cache.clear()
    return SimpleNamespace(home=home, src=src, dest=dest)


def _seed_full_source(env) -> None:
    """A realistic legacy source tree exercising every manifest branch."""
    s = env.src
    _write(s / "sessions" / "old-a.jsonl", '{"a":1}\n')
    _write(s / "session_map.json", json.dumps({"dup": "OLD", "k1": "v1"}))
    _write(s / "folders.json", json.dumps([{"id": "f_old"}, {"id": "f_dup"}]))
    _write(s / "memory.db", "OLD_MEMORY")
    _write(s / "workspace" / "notes.md", "note")
    _write(s / "workspace" / "memory" / "preferences.md", "OLD_PREFS")
    _write(s / "workspace" / "memory" / "history" / "h1.md", "hist1")
    _write(s / "workspace" / "knowledge" / "knowledge.db", "OLD_KDB")
    _write(s / "workspace" / ".kiro" / "steering.md", "steer")
    # apps: one FULL, one SHARED (data-only), one stub (no installed.json)
    _write(s / "apps" / "board" / "installed.json", "{}")
    _write(s / "apps" / "board" / "state" / "b.json", "board-state")
    _write(s / "apps" / "projects" / "data" / "p.json", "proj-data")
    _write(s / "apps" / "projects" / "code.py", "should-not-copy")
    _write(s / "apps" / "zzz-stub" / "manifest.json", "stub")  # no installed.json
    # skills: custom (copy), builtin (skip), name colliding with dest (skip)
    _write(s / "skills" / "custom-skill" / "SKILL.md", "custom")
    _write(s / "skills" / "meshclaw-builtin" / "SKILL.md", "builtin")
    _write(s / "skills" / "existing-skill" / "SKILL.md", "SRC_EXISTING")
    _write(s / "artifacts" / "art1.txt", "a1")
    _write(s / "uploads" / "u1.txt", "u1")
    _write(s / "crons.json", json.dumps({"jobs": [
        {"id": "j1", "enabled": True, "name": "one"},
        {"id": "j2", "enabled": True, "user_paused": False},
    ]}))
    _write(s / "crons" / "log.json", "cronlog")
    _write(s / "cron-history" / "h.json", "chist")
    _write(s / "mcp.json", "SRC_MCP")
    _write(s / "mcp-servers" / "srv.json", "srv")
    _write(s / ".env", "SECRET=old")
    _write(s / "lessons.jsonl", "lesson")
    _write(s / "plan_memory" / "pm.json", "plan")
    _write(s / "config.json", "SRC_CONFIG")


# ── detect ────────────────────────────────────────────────────────────────
class TestDetect:
    def test_available_true(self, env):
        _write(env.src / "sessions" / "a.jsonl", "{}\n")
        _write(env.src / "sessions" / "b.jsonl", "{}\n")
        info = mod.detect_meshclaw_import_available()
        assert info["available"] is True
        assert info["sourcePath"] == "~/.meshclaw"
        assert info["sessionCount"] == 2
        assert info["sizeEstimateBytes"] > 0

    def test_false_when_source_missing(self, env, monkeypatch):
        monkeypatch.setattr(mod, "_home", lambda: env.home / "nope")
        info = mod.detect_meshclaw_import_available()
        assert info["available"] is False
        assert info["sizeEstimateBytes"] == 0 and info["sessionCount"] == 0

    def test_false_when_backup_exists(self, env):
        (env.home / ".meshclaw.bak").mkdir()
        assert mod.detect_meshclaw_import_available()["available"] is False

    def test_false_when_done_marker_present(self, env):
        mod._write_done_marker(reason="test")
        assert mod.detect_meshclaw_import_available()["available"] is False

    def test_false_when_source_is_dest(self, env, monkeypatch):
        # Running as legacy MeshClaw itself: config_dir == source.
        monkeypatch.setattr(mod, "config_dir", lambda: env.src)
        assert mod.detect_meshclaw_import_available()["available"] is False

    def test_size_estimate_skips_models_dir(self, env):
        _write(env.src / "memory.db", "abc")
        _write(env.src / "models" / "big.bin", "x" * 100000)
        info = mod.detect_meshclaw_import_available()
        assert 0 < info["sizeEstimateBytes"] < 100000

    def test_size_estimate_manifest_scope_and_memo(self, env):
        _write(env.src / "memory.db", "abc")
        _write(env.src / "gateway.log", "x" * 5000)
        _write(env.src / "gateway.log.1", "x" * 5000)
        _write(env.src / "security_events.jsonl", "x" * 5000)
        _write(env.src / "config.json", "x" * 5000)
        _write(env.src / "apps" / "stub-app" / "manifest.json", "x" * 5000)  # no installed.json
        _write(env.src / "apps" / "real-app" / "installed.json", "{}")
        size = mod._estimate_size_bytes(env.src)
        assert size == 3 + 2  # memory.db + real-app/installed.json only
        # memoized: a second call does not re-walk (new files invisible)
        _write(env.src / "memory2.db", "x" * 999)
        assert mod._estimate_size_bytes(env.src) == size

    def test_false_when_dest_has_sessions(self, env):
        _write(env.src / "memory.db", "x")
        _write(env.dest / "sessions" / "live.jsonl", "{}\n")
        assert mod.detect_meshclaw_import_available()["available"] is False

    def test_false_when_dest_memory_db_not_fresh(self, env):
        _write(env.src / "memory.db", "x")
        _write(env.dest / "memory.db", "x" * (mod._FRESH_MEMORY_DB_MAX_BYTES + 1))
        assert mod.detect_meshclaw_import_available()["available"] is False

    def test_true_when_dest_memory_db_small(self, env):
        _write(env.src / "memory.db", "x")
        _write(env.dest / "memory.db", "tiny-fresh-init")
        assert mod.detect_meshclaw_import_available()["available"] is True


# ── idempotency / guards ────────────────────────────────────────────────────
class TestGuards:
    def test_noop_when_done_marker_present(self, env):
        _seed_full_source(env)
        mod._write_done_marker(reason="prior")
        res = mod.run_meshclaw_import()
        assert res["status"] == "already_done" and res["renamed"] is False
        # Nothing copied, source left intact.
        assert env.src.is_dir()
        assert not (env.dest / "sessions" / "old-a.jsonl").exists()

    def test_backup_exists_guard(self, env):
        _seed_full_source(env)
        (env.home / ".meshclaw.bak").mkdir()
        res = mod.run_meshclaw_import()
        assert res["status"] == "already_done_backup" and res["renamed"] is False
        assert mod._done_marker_path().exists()
        # Source untouched, no copy performed.
        assert env.src.is_dir()
        assert not (env.dest / "sessions" / "old-a.jsonl").exists()

    def test_no_source_guard(self, env, monkeypatch):
        monkeypatch.setattr(mod, "_home", lambda: env.home / "gone")
        res = mod.run_meshclaw_import()
        assert res["status"] == "no_source" and res["renamed"] is False

    def test_source_is_dest_guard(self, env, monkeypatch):
        monkeypatch.setattr(mod, "config_dir", lambda: env.src)
        res = mod.run_meshclaw_import()
        assert res["status"] == "source_is_dest" and res["renamed"] is False
        assert env.src.is_dir()

    def test_double_run_is_noop(self, env):
        _seed_full_source(env)
        first = mod.run_meshclaw_import()
        assert first["renamed"] is True
        second = mod.run_meshclaw_import()
        assert second["status"] == "already_done" and second["renamed"] is False


# ── rename (reversible) + crash-safe retry semantics ────────────────────────
class TestRename:
    def test_rename_occurs_and_done_marker_written(self, env):
        _seed_full_source(env)
        res = mod.run_meshclaw_import()
        assert res["status"] == "ok" and res["renamed"] is True
        assert not env.src.exists()
        assert (env.home / ".meshclaw.bak").is_dir()
        assert mod._done_marker_path().exists()
        # Backup is a faithful copy (reversible): legacy data still present.
        assert (env.home / ".meshclaw.bak" / "config.json").read_text() == "SRC_CONFIG"

    def test_failed_rename_returns_copied_no_rename(self, env, monkeypatch):
        _seed_full_source(env)
        monkeypatch.setattr(mod.os, "rename", _raise_oserror)
        res = mod.run_meshclaw_import()
        assert res["status"] == "copied_no_rename" and res["renamed"] is False
        # no partial .bak (os.rename only, no move fallback)
        assert not (env.home / ".meshclaw.bak").exists()
        assert env.src.is_dir()
        assert mod._copied_marker_path().exists()
        assert not mod._done_marker_path().exists()

    def test_retry_after_failed_rename_does_not_recopy(self, env, monkeypatch):
        _seed_full_source(env)
        real_rename = os.rename
        monkeypatch.setattr(mod.os, "rename", _raise_oserror)
        first = mod.run_meshclaw_import()
        assert first["status"] == "copied_no_rename"
        # Post-import: user works in the new home; source also changes.
        (env.dest / "memory.db").write_text("POST_IMPORT_EDIT", encoding="utf-8")
        (env.src / "memory.db").write_text("CHANGED_SRC", encoding="utf-8")
        monkeypatch.setattr(mod.os, "rename", real_rename)
        second = mod.run_meshclaw_import()
        assert second["status"] == "ok" and second["renamed"] is True
        assert second["steps"] == []  # copy phase skipped entirely
        # retry did NOT re-copy / clobber the post-import dest change
        assert (env.dest / "memory.db").read_text() == "POST_IMPORT_EDIT"
        assert not env.src.exists()
        assert mod._done_marker_path().exists()

    def test_copy_failure_rolls_back_and_skips_rename(self, env, monkeypatch):
        _seed_full_source(env)
        monkeypatch.setattr(mod, "_merge_session_map", _raise_runtimeerror)
        res = mod.run_meshclaw_import()
        assert res["status"] == "rolled_back" and res["renamed"] is False
        assert any(s["step"] == "session_map" and not s["ok"] for s in res["steps"])
        # source untouched, no markers written, no rename
        assert env.src.is_dir()
        assert not (env.home / ".meshclaw.bak").exists()
        assert not mod._done_marker_path().exists()
        assert not mod._copied_marker_path().exists()

    def test_rename_gave_up_after_cap(self, env, monkeypatch):
        _seed_full_source(env)
        mod.write_import_intent()
        monkeypatch.setattr(mod.os, "rename", _raise_oserror)
        for _ in range(mod._MAX_RENAME_ATTEMPTS):
            res = mod.run_pending_meshclaw_import()
            assert res["status"] == "copied_no_rename"
            assert mod._intent_marker_path().exists()  # retryable: retained
            # Age the intent past the TTL: the staleness gate guards consent
            # for the COPY only — a committed copy's rename must still be
            # retried on boots arbitrarily far in the future.
            old = datetime.now(timezone.utc) - timedelta(
                seconds=mod._INTENT_MAX_AGE_SECONDS + 60
            )
            mod._intent_marker_path().write_text(
                json.dumps({"requested_at": old.isoformat()}), encoding="utf-8"
            )
        final = mod.run_pending_meshclaw_import()
        assert final["status"] == "rename_gave_up"
        assert not mod._intent_marker_path().exists()  # gave up: cleared
        # dest keeps the COMPLETE import; source untouched; no done marker
        assert (env.dest / "memory.db").read_text() == "OLD_MEMORY"
        assert env.src.is_dir()
        assert not (env.home / ".meshclaw.bak").exists()
        assert not mod._done_marker_path().exists()
        # subsequent boots are silent no-ops (intent gone)
        assert mod.run_pending_meshclaw_import() == {"ran": False, "reason": "no_intent"}


# ── additive / old-wins semantics ───────────────────────────────────────────
class TestCopySemantics:
    def test_sessions_additive_never_clobber(self, env):
        _seed_full_source(env)
        _write(env.dest / "sessions" / "old-a.jsonl", "DEST_KEEP")  # collision
        _write(env.dest / "sessions" / "dest-only.jsonl", "keep2")
        mod.run_meshclaw_import()
        # existing dest file preserved (additive), source's copy skipped
        assert (env.dest / "sessions" / "old-a.jsonl").read_text() == "DEST_KEEP"
        assert (env.dest / "sessions" / "dest-only.jsonl").read_text() == "keep2"

    def test_artifacts_and_uploads_additive(self, env):
        _seed_full_source(env)
        _write(env.dest / "artifacts" / "art1.txt", "DEST_ART")
        mod.run_meshclaw_import()
        assert (env.dest / "artifacts" / "art1.txt").read_text() == "DEST_ART"
        assert (env.dest / "uploads" / "u1.txt").read_text() == "u1"  # new file added

    def test_memory_db_old_wins_and_index_dropped(self, env):
        _seed_full_source(env)
        _write(env.dest / "memory.db", "FRESH_MEMORY")
        _write(env.dest / "memory.db-wal", "wal")
        _write(env.dest / "memory.db-shm", "shm")
        _write(env.dest / "memory_index.db", "idx")
        mod.run_meshclaw_import()
        assert (env.dest / "memory.db").read_text() == "OLD_MEMORY"  # old wins
        assert not (env.dest / "memory.db-wal").exists()
        assert not (env.dest / "memory.db-shm").exists()
        assert not (env.dest / "memory_index.db").exists()

    def test_workspace_prefs_old_wins_history_additive(self, env):
        _seed_full_source(env)
        _write(env.dest / "workspace" / "memory" / "preferences.md", "FRESH_PREFS")
        _write(env.dest / "workspace" / "memory" / "history" / "dest.md", "dkeep")
        mod.run_meshclaw_import()
        assert (env.dest / "workspace" / "memory" / "preferences.md").read_text() == "OLD_PREFS"
        assert (env.dest / "workspace" / "memory" / "history" / "dest.md").read_text() == "dkeep"
        assert (env.dest / "workspace" / "memory" / "history" / "h1.md").read_text() == "hist1"
        assert (env.dest / "workspace" / "notes.md").read_text() == "note"  # docs additive

    def test_knowledge_db_old_wins_with_dest_backup(self, env):
        _seed_full_source(env)
        _write(env.dest / "workspace" / "knowledge" / "knowledge.db", "FRESH_KDB")
        _write(env.dest / "workspace" / "knowledge" / "knowledge.db-wal", "w")
        mod.run_meshclaw_import()
        kdir = env.dest / "workspace" / "knowledge"
        assert (kdir / "knowledge.db").read_text() == "OLD_KDB"
        # stale dest -wal dropped (source has none)
        assert not (kdir / "knowledge.db-wal").exists()
        # pre-overwrite backup of the fresh install's db lives in the
        # dedicated dest-backup dir (replaces the old .new-backup file)
        bdir = mod._dest_backup_dir()
        assert (bdir / "workspace" / "knowledge" / "knowledge.db").read_text() == "FRESH_KDB"
        assert not (kdir / "knowledge.db.new-backup").exists()

    def test_source_wal_copied_for_consistent_snapshot(self, env):
        _seed_full_source(env)
        _write(env.src / "memory.db-wal", "SRC_WAL")
        _write(env.dest / "memory.db-shm", "STALE_SHM")
        _write(env.dest / "memory_index.db", "idx")
        mod.run_meshclaw_import()
        assert (env.dest / "memory.db-wal").read_text() == "SRC_WAL"  # snapshot
        assert not (env.dest / "memory.db-shm").exists()  # stale dest dropped
        assert not (env.dest / "memory_index.db").exists()  # always dropped

    def test_pre_overwrite_dest_backup_made(self, env):
        _seed_full_source(env)
        _write(env.dest / "memory.db", "FRESH_MEMORY")
        _write(env.dest / "workspace" / "memory" / "preferences.md", "FRESH_PREFS")
        mod.run_meshclaw_import()
        bdir = mod._dest_backup_dir()
        assert (bdir / "memory.db").read_text() == "FRESH_MEMORY"
        assert (bdir / "workspace" / "memory" / "preferences.md").read_text() == "FRESH_PREFS"
        # only targets that existed in dest are backed up
        assert not (bdir / "crons.json").exists()
        assert not (bdir / "workspace" / "memory" / "projects.md").exists()
        # atomic backup writes: no tmp residue at the backup paths
        assert not list(bdir.rglob("*.tmp-import"))

    def test_dir_symlink_preserved_as_symlink(self, env):
        _seed_full_source(env)
        real = env.src / "artifacts" / "real-dir"
        _write(real / "f.txt", "rf")
        link = env.src / "artifacts" / "linked"
        link.symlink_to(real, target_is_directory=True)
        target = os.readlink(link)
        mod.run_meshclaw_import()
        d = env.dest / "artifacts" / "linked"
        assert d.is_symlink()
        assert os.readlink(d) == target
        # the real dir is still copied as a dir
        assert (env.dest / "artifacts" / "real-dir" / "f.txt").read_text() == "rf"


# ── apps / skills manifest parity ───────────────────────────────────────────
class TestAppsAndSkills:
    def test_apps_full_shared_and_stub_skip(self, env):
        _seed_full_source(env)
        mod.run_meshclaw_import()
        # FULL app copied entirely
        assert (env.dest / "apps" / "board" / "installed.json").exists()
        assert (env.dest / "apps" / "board" / "state" / "b.json").read_text() == "board-state"
        # SHARED app: only data/ imported
        assert (env.dest / "apps" / "projects" / "data" / "p.json").read_text() == "proj-data"
        assert not (env.dest / "apps" / "projects" / "code.py").exists()
        # stub app (no installed.json, not in either list) never touched
        assert not (env.dest / "apps" / "zzz-stub").exists()

    def test_skills_skip_builtin_and_existing(self, env):
        _seed_full_source(env)
        _write(env.dest / "skills" / "existing-skill" / "SKILL.md", "DEST_EXISTING")
        mod.run_meshclaw_import()
        assert (env.dest / "skills" / "custom-skill" / "SKILL.md").read_text() == "custom"
        assert not (env.dest / "skills" / "meshclaw-builtin").exists()
        # pre-existing skill dir untouched (not clobbered by src)
        assert (env.dest / "skills" / "existing-skill" / "SKILL.md").read_text() == "DEST_EXISTING"


# ── json merges ─────────────────────────────────────────────────────────────
class TestJsonMerges:
    def test_session_map_union_new_wins(self, env):
        _seed_full_source(env)
        _write(env.dest / "session_map.json", json.dumps({"dup": "NEW", "k2": "v2"}))
        mod.run_meshclaw_import()
        merged = json.loads((env.dest / "session_map.json").read_text())
        assert merged == {"dup": "NEW", "k1": "v1", "k2": "v2"}  # new wins on collision

    def test_folders_union_new_wins(self, env):
        _seed_full_source(env)
        _write(env.dest / "folders.json", json.dumps([{"id": "f_dup"}, {"id": "f_new"}]))
        mod.run_meshclaw_import()
        ids = [f["id"] for f in json.loads((env.dest / "folders.json").read_text())]
        assert set(ids) == {"f_dup", "f_new", "f_old"}
        assert ids.index("f_new") < ids.index("f_old")  # new listed first


# ── crons imported paused / merged ──────────────────────────────────────────
class TestCrons:
    def test_crons_imported_paused(self, env):
        _seed_full_source(env)
        mod.run_meshclaw_import()
        data = json.loads((env.dest / "crons.json").read_text())
        assert data["jobs"] and all(
            j["enabled"] is False and j["user_paused"] is True for j in data["jobs"]
        )
        assert (env.dest / "crons" / "log.json").exists()
        assert (env.dest / "cron-history" / "h.json").exists()

    def test_crons_merge_keeps_dest_jobs_dest_wins(self, env):
        _seed_full_source(env)  # legacy has j1, j2
        _write(env.dest / "crons.json", json.dumps({"jobs": [
            {"id": "j1", "enabled": True, "name": "dest-one"},
            {"id": "d9", "enabled": True},
        ], "schema": 2}))
        mod.run_meshclaw_import()
        data = json.loads((env.dest / "crons.json").read_text())
        by_id = {j["id"]: j for j in data["jobs"]}
        # dest wins on id collision: j1 stays enabled with dest's name
        assert by_id["j1"]["enabled"] is True and by_id["j1"]["name"] == "dest-one"
        # dest-only job kept untouched
        assert by_id["d9"]["enabled"] is True
        # legacy-only job appended, paused
        assert by_id["j2"]["enabled"] is False and by_id["j2"]["user_paused"] is True
        # other dest top-level keys preserved
        assert data["schema"] == 2

    def test_crons_legacy_list_form_normalized_and_disabled(self, env):
        _write(env.src / "memory.db", "m")
        _write(env.src / "crons.json", json.dumps([
            {"id": "L1", "enabled": True},
            {"id": "L2", "enabled": True, "user_paused": False},
        ]))
        mod.run_meshclaw_import()
        data = json.loads((env.dest / "crons.json").read_text())
        assert isinstance(data, dict)
        assert {j["id"] for j in data["jobs"]} == {"L1", "L2"}
        assert all(j["enabled"] is False and j["user_paused"] is True for j in data["jobs"])


# ── mcp.json / .env / config.json ───────────────────────────────────────────
class TestSensitiveFiles:
    def test_mcp_json_copied_when_absent(self, env):
        _seed_full_source(env)
        mod.run_meshclaw_import()
        assert (env.dest / "mcp.json").read_text() == "SRC_MCP"
        assert (env.dest / "mcp-servers" / "srv.json").exists()

    def test_mcp_json_parked_when_present(self, env):
        _seed_full_source(env)
        _write(env.dest / "mcp.json", "DEST_MCP")
        mod.run_meshclaw_import()
        assert (env.dest / "mcp.json").read_text() == "DEST_MCP"  # new preserved
        assert (env.dest / "mcp.json.from-meshclaw").read_text() == "SRC_MCP"

    def test_env_absent_only(self, env):
        _seed_full_source(env)
        mod.run_meshclaw_import()
        assert (env.dest / ".env").read_text() == "SECRET=old"

    def test_env_not_overwritten_when_present(self, env):
        _seed_full_source(env)
        _write(env.dest / ".env", "SECRET=new")
        mod.run_meshclaw_import()
        assert (env.dest / ".env").read_text() == "SECRET=new"

    def test_config_json_never_copied(self, env):
        _seed_full_source(env)
        _write(env.dest / "config.json", "DEST_CONFIG")
        mod.run_meshclaw_import()
        assert (env.dest / "config.json").read_text() == "DEST_CONFIG"

    def test_misc_state_additive(self, env):
        _seed_full_source(env)
        mod.run_meshclaw_import()
        assert (env.dest / "lessons.jsonl").read_text() == "lesson"
        assert (env.dest / "plan_memory" / "pm.json").read_text() == "plan"


# ── two-phase intent flow ───────────────────────────────────────────────────
class TestTwoPhase:
    def test_run_pending_noop_without_intent(self, env):
        _seed_full_source(env)
        res = mod.run_pending_meshclaw_import()
        assert res == {"ran": False, "reason": "no_intent"}
        assert env.src.is_dir()  # untouched
        assert not (env.dest / "sessions" / "old-a.jsonl").exists()

    def test_intent_then_run_pending_performs_import(self, env):
        _seed_full_source(env)
        p = mod.write_import_intent()
        assert p.exists() and mod._intent_marker_path().exists()
        res = mod.run_pending_meshclaw_import()
        assert res["ran"] is True and res["renamed"] is True
        assert not env.src.exists()
        assert (env.home / ".meshclaw.bak").is_dir()
        assert mod._done_marker_path().exists()
        # intent cleared once complete
        assert not mod._intent_marker_path().exists()

    def test_run_pending_noop_when_done(self, env):
        mod._write_done_marker(reason="prior")
        mod.write_import_intent()  # stale intent
        res = mod.run_pending_meshclaw_import()
        assert res == {"ran": False, "reason": "already_done"}
        # stale intent cleaned up
        assert not mod._intent_marker_path().exists()

    def test_write_import_intent_creates_marker_dir(self, env):
        assert not mod._marker_dir().exists()
        mod.write_import_intent()
        assert mod._marker_dir().is_dir()
        assert mod._intent_marker_path().exists()

    def test_intent_cleared_on_no_source(self, env, monkeypatch):
        mod.write_import_intent()
        monkeypatch.setattr(mod, "_home", lambda: env.home / "gone")
        res = mod.run_pending_meshclaw_import()
        assert res["ran"] is True and res["status"] == "no_source"
        # terminal no-op: intent cleared so it doesn't repeat every boot
        assert not mod._intent_marker_path().exists()

    def test_intent_cleared_on_rolled_back_copy(self, env, monkeypatch):
        _seed_full_source(env)
        mod.write_import_intent()
        monkeypatch.setattr(mod, "_merge_session_map", _raise_runtimeerror)
        res = mod.run_pending_meshclaw_import()
        assert res["ran"] is True and res["status"] == "rolled_back"
        # NO retained retry state: a failed copy is fully rolled back and
        # the intent is cleared — a future attempt is a fresh consent-gated
        # offer, not an automatic retry.
        assert not mod._intent_marker_path().exists()
        assert not mod._copied_marker_path().exists()

    def test_intent_retained_on_copied_no_rename(self, env, monkeypatch):
        _seed_full_source(env)
        mod.write_import_intent()
        monkeypatch.setattr(mod.os, "rename", _raise_oserror)
        res = mod.run_pending_meshclaw_import()
        assert res["ran"] is True and res["status"] == "copied_no_rename"
        # rename retried on next boot: intent must survive
        assert mod._intent_marker_path().exists()


# ── all-or-nothing copy phase (backup / journal / rollback) ─────────────────
class TestTransactional:
    def test_backup_failure_aborts_before_any_overwrite(self, env, monkeypatch):
        _seed_full_source(env)
        _write(env.dest / "memory.db", "FRESH_MEMORY")
        mod.write_import_intent()

        def _boom(*_a, **_k):
            raise OSError("simulated backup copy failure")

        monkeypatch.setattr(mod.shutil, "copy2", _boom)
        res = mod.run_meshclaw_import()
        assert res["status"] == "backup_failed" and res["renamed"] is False
        # fail-closed: the ONLY step recorded is the failed backup — no
        # manifest step ran, so nothing in dest was overwritten
        assert res["steps"] == [
            {"step": "dest_backup", "ok": False, "error": "simulated backup copy failure"}
        ]
        assert (env.dest / "memory.db").read_text() == "FRESH_MEMORY"
        assert not (env.dest / "sessions").exists()
        assert env.src.is_dir()
        # terminal: partial backup dir and intent are cleared — a future
        # attempt is the normal consent-gated offer again
        assert not mod._dest_backup_dir().exists()
        assert not mod._intent_marker_path().exists()
        assert mod.detect_meshclaw_import_available()["available"] is True

    def test_torn_backup_never_at_final_path(self, env, monkeypatch):
        _write(env.dest / "memory.db", "PRISTINE")
        captured = []

        def _torn(s, d, **_k):
            # simulate a crash mid-copy that leaves a partial file at ``d``
            captured.append(str(d))
            Path(d).write_text("PARTIAL", encoding="utf-8")
            raise OSError("crash mid-backup")

        monkeypatch.setattr(mod.shutil, "copy2", _torn)
        with pytest.raises(OSError):
            mod._backup_dest_targets(env.dest)
        # the write went to a tmp sibling, never the final backup path...
        assert captured and all(p.endswith(".tmp-import") for p in captured)
        b = mod._dest_backup_dir() / "memory.db"
        assert not b.exists()  # no truncated file the restore would trust
        # ...and the tmp itself was cleaned up
        assert not list(mod._dest_backup_dir().rglob("*.tmp-import"))

    def test_midmanifest_failure_full_rollback(self, env, monkeypatch):
        _seed_full_source(env)
        _write(env.dest / "memory.db", "FRESH_MEMORY")
        mod.write_import_intent()
        state = {"fail": True}
        real_import_crons = mod._import_crons

        def _flaky(src, dst):
            if state["fail"]:
                raise RuntimeError("simulated crons failure")
            return real_import_crons(src, dst)

        monkeypatch.setattr(mod, "_import_crons", _flaky)
        first = mod.run_meshclaw_import()
        assert first["status"] == "rolled_back"
        # crons.json fails AFTER memory.db + workspace were overwritten:
        # rollback must restore memory.db from the backup...
        assert (env.dest / "memory.db").read_text() == "FRESH_MEMORY"
        # ...remove overwrite targets that did not exist pre-import...
        assert not (env.dest / "workspace" / "memory" / "preferences.md").exists()
        assert not (env.dest / "crons.json").exists()
        assert not (env.dest / "session_map.json").exists()
        # ...and delete ALL additive residue this run created
        assert not (env.dest / "sessions").exists()
        assert not (env.dest / "apps").exists()
        assert not (env.dest / "skills").exists()
        assert not (env.dest / "artifacts").exists()
        assert not (env.dest / "uploads").exists()
        assert not (env.dest / "mcp.json").exists()
        assert not (env.dest / ".env").exists()
        assert not (env.dest / "lessons.jsonl").exists()
        assert not (env.dest / "workspace").exists()
        # all state cleared: no markers, no backup dir, no intent
        assert not mod._copied_marker_path().exists()
        assert not mod._intent_marker_path().exists()
        assert not mod._dest_backup_dir().exists()
        # source never touched
        assert env.src.is_dir()
        assert not (env.home / ".meshclaw.bak").exists()
        # dest is back to fresh: the import offer is available again
        assert mod.detect_meshclaw_import_available()["available"] is True

        # a fresh consent-gated attempt then succeeds normally
        state["fail"] = False
        mod.write_import_intent()
        second = mod.run_pending_meshclaw_import()
        assert second["status"] == "ok" and second["renamed"] is True
        assert (env.dest / "memory.db").read_text() == "OLD_MEMORY"
        assert (env.dest / "workspace" / "memory" / "preferences.md").read_text() == "OLD_PREFS"
        assert mod._done_marker_path().exists()
        assert not env.src.exists()

    def test_uncaught_exception_rolls_back(self, env, monkeypatch):
        # S7 seam: an exception ESCAPING the guarded manifest (e.g. the
        # copied-marker write failing with OSError) must trigger the same
        # full rollback instead of leaving intent + partial state behind.
        _seed_full_source(env)
        _write(env.dest / "memory.db", "FRESH_MEMORY")
        mod.write_import_intent()
        monkeypatch.setattr(
            mod, "_write_copied_marker", _raise_oserror
        )
        res = mod.run_meshclaw_import()
        assert res["status"] == "rolled_back" and res["renamed"] is False
        assert any(s["step"] == "unexpected" and not s["ok"] for s in res["steps"])
        # full rollback despite the copy itself having succeeded
        assert (env.dest / "memory.db").read_text() == "FRESH_MEMORY"
        assert not (env.dest / "sessions").exists()
        assert not (env.dest / "workspace").exists()
        assert not mod._copied_marker_path().exists()
        assert not mod._intent_marker_path().exists()
        assert not mod._dest_backup_dir().exists()
        assert env.src.is_dir()
        assert mod.detect_meshclaw_import_available()["available"] is True

    def test_freshness_recheck_bails_and_clears_intent(self, env):
        _seed_full_source(env)
        mod.write_import_intent()
        # dest accrued real data since consent (a session transcript)
        _write(env.dest / "sessions" / "accrued.jsonl", "DEST_DATA")
        res = mod.run_meshclaw_import()
        assert res["status"] == "dest_not_fresh" and res["steps"] == []
        assert not (env.dest / "memory.db").exists()  # nothing copied
        assert (env.dest / "sessions" / "accrued.jsonl").read_text() == "DEST_DATA"
        assert env.src.is_dir()
        assert not mod._done_marker_path().exists()
        # simple clear+bail: no bypass, no retained retry state
        assert not mod._intent_marker_path().exists()

    def test_stale_intent_ttl_expiry_bails(self, env):
        _seed_full_source(env)
        p = mod.write_import_intent()
        # age the intent past the TTL by rewriting its requested_at
        old = datetime.now(timezone.utc) - timedelta(
            seconds=mod._INTENT_MAX_AGE_SECONDS + 60
        )
        p.write_text(
            json.dumps({"requested_at": old.isoformat()}), encoding="utf-8"
        )
        res = mod.run_pending_meshclaw_import()
        assert res == {"ran": False, "reason": "stale_intent"}
        assert not mod._intent_marker_path().exists()  # dropped, not honored
        assert not (env.dest / "memory.db").exists()  # nothing imported
        assert env.src.is_dir()

    def test_nesting_guard_refuses(self, env, monkeypatch):
        _seed_full_source(env)
        nested_dest = env.src / "nested-home"
        nested_dest.mkdir(parents=True)
        monkeypatch.setattr(mod, "config_dir", lambda: nested_dest)
        assert mod.detect_meshclaw_import_available()["available"] is False
        res = mod.run_meshclaw_import()
        assert res["status"] == "nested_paths" and res["renamed"] is False
        assert env.src.is_dir()
        assert not (env.home / ".meshclaw.bak").exists()

    def test_unlink_failure_raises_not_write_through_symlink(self, env, tmp_path):
        outside = tmp_path / "outside.txt"
        _write(outside, "SAFE")
        srcdir = env.src / "adir"
        _write(srcdir / "f.txt", "NEW")
        dstdir = env.dest / "adir"
        dstdir.mkdir(parents=True)
        (dstdir / "f.txt").symlink_to(outside)
        os.chmod(dstdir, 0o500)  # unlink in this dir now fails
        try:
            with pytest.raises(OSError):
                mod._copytree_overwrite(srcdir, dstdir)
            # the failure surfaced instead of writing through the symlink
            assert outside.read_text() == "SAFE"
            assert (dstdir / "f.txt").is_symlink()
        finally:
            os.chmod(dstdir, 0o755)

    def test_dest_dir_symlink_replaced_not_written_through(self, env, tmp_path):
        # S4: a dest DIRECTORY position occupied by a symlink must be
        # strictly unlinked and replaced with a real dir — never written
        # through into the outside tree.
        outside = tmp_path / "outside"
        outside.mkdir()
        srcdir = env.src / "apps" / "board"
        _write(srcdir / "sub" / "f.txt", "NEW")
        dstdir = env.dest / "apps" / "board"
        dstdir.mkdir(parents=True)
        (dstdir / "sub").symlink_to(outside, target_is_directory=True)
        mod._copytree_overwrite(srcdir, dstdir)
        assert not (dstdir / "sub").is_symlink()  # replaced with a real dir
        assert (dstdir / "sub" / "f.txt").read_text() == "NEW"
        assert not (outside / "f.txt").exists()  # outside tree untouched

    def test_dest_dir_symlink_unlink_failure_raises(self, env, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        srcdir = env.src / "apps" / "board"
        _write(srcdir / "sub" / "f.txt", "NEW")
        dstdir = env.dest / "apps" / "board"
        dstdir.mkdir(parents=True)
        (dstdir / "sub").symlink_to(outside, target_is_directory=True)
        os.chmod(dstdir, 0o500)  # unlinking the dir symlink now fails
        try:
            with pytest.raises(OSError):
                mod._copytree_overwrite(srcdir, dstdir)
            assert (dstdir / "sub").is_symlink()  # strict: kept, not followed
            assert not (outside / "f.txt").exists()
        finally:
            os.chmod(dstdir, 0o755)


# ── crash recovery (durable on-disk backup record) ─────────────────────────
class TestCrashRecovery:
    def test_crashed_run_recovered_without_import(self, env):
        _seed_full_source(env)
        _write(env.dest / "memory.db", "FRESH_MEMORY")
        _write(env.dest / "workspace" / "memory" / "preferences.md", "FRESH_PREFS")
        # simulate the durable state a SIGKILL mid-copy leaves behind: a
        # COMPLETE dest backup, overwritten targets, no copied marker.
        mod._backup_dest_targets(env.dest)
        _write(env.dest / "memory.db", "IMPORTED_OLD")
        _write(env.dest / "workspace" / "memory" / "preferences.md", "OLD_PREFS")
        _write(env.dest / "crons.json", "{}")  # crashed run created it (no backup)
        mod.write_import_intent()
        res = mod.run_pending_meshclaw_import()
        assert res["ran"] is True and res["status"] == "recovered_crashed_run"
        # overwrite targets restored from the backup...
        assert (env.dest / "memory.db").read_text() == "FRESH_MEMORY"
        assert (
            env.dest / "workspace" / "memory" / "preferences.md"
        ).read_text() == "FRESH_PREFS"
        # ...a target with no backup entry did not exist pre-import: removed
        assert not (env.dest / "crons.json").exists()
        # backup consumed, intent cleared, and NOTHING was imported
        assert not mod._dest_backup_dir().exists()
        assert not mod._intent_marker_path().exists()
        assert not mod._copied_marker_path().exists()
        assert not (env.dest / "sessions").exists()
        assert env.src.is_dir()
        assert not (env.home / ".meshclaw.bak").exists()
        # a leftover backup can never poison a later consented run: a fresh
        # re-consent starts with no backup dir at all
        mod.write_import_intent()
        second = mod.run_pending_meshclaw_import()
        assert second["status"] == "ok" and second["renamed"] is True
        assert (env.dest / "memory.db").read_text() == "OLD_MEMORY"

    def test_crashed_run_recovered_even_without_intent(self, env):
        # SIGKILL + a much later boot: the intent is stale or gone entirely —
        # the on-disk backup alone must trigger recovery.
        _write(env.src / "memory.db", "OLD")
        _write(env.dest / "memory.db", "FRESH")
        mod._backup_dest_targets(env.dest)
        _write(env.dest / "memory.db", "IMPORTED")
        res = mod.run_pending_meshclaw_import()
        assert res["ran"] is True and res["status"] == "recovered_crashed_run"
        assert (env.dest / "memory.db").read_text() == "FRESH"
        assert not mod._dest_backup_dir().exists()

    def test_crashed_backup_phase_discards_partial_backup(self, env):
        _seed_full_source(env)
        _write(env.dest / "memory.db", "FRESH_MEMORY")
        _write(env.dest / "session_map.json", "{}")
        # a crash DURING the backup phase: one target copied, but the
        # completion marker never written — and dest never modified.
        # (session_map.json existed pre-import but was NOT yet backed up.)
        _write(mod._dest_backup_dir() / "memory.db", "FRESH_MEMORY")
        mod.write_import_intent()
        res = mod.run_pending_meshclaw_import()
        assert res["status"] == "recovered_crashed_run"
        # the partial backup is discarded WITHOUT restoring from it — a
        # restore would have DELETED session_map.json (no backup entry)
        assert not mod._dest_backup_dir().exists()
        assert (env.dest / "memory.db").read_text() == "FRESH_MEMORY"
        assert (env.dest / "session_map.json").read_text() == "{}"
        assert not mod._intent_marker_path().exists()
        assert not (env.dest / "sessions").exists()

    def test_recovery_incomplete_keeps_backup(self, env, monkeypatch):
        _seed_full_source(env)
        _write(env.dest / "memory.db", "FRESH_MEMORY")
        mod._backup_dest_targets(env.dest)
        _write(env.dest / "memory.db", "IMPORTED")
        monkeypatch.setattr(mod, "_restore_dest_targets", lambda dest: ["memory.db"])
        mod.write_import_intent()
        res = mod.run_pending_meshclaw_import()
        assert res["ran"] is True and res["status"] == "recovery_incomplete"
        assert res["restore_failures"] == ["memory.db"]
        # never reports clean: the backup is KEPT with an incomplete note
        assert mod._dest_backup_dir().is_dir()
        assert (
            mod._dest_backup_dir() / mod._RESTORE_INCOMPLETE_MARKER
        ).is_file()
        assert not mod._intent_marker_path().exists()
        # and nothing was imported
        assert not (env.dest / "sessions").exists()
        assert env.src.is_dir()

    def test_copied_marker_present_is_not_a_crash(self, env, monkeypatch):
        # backup dir + copied marker = the committed rename-retry path, not
        # a crash — recovery must NOT fire and clobber the completed import.
        _seed_full_source(env)
        mod.write_import_intent()
        real_rename = os.rename
        state = {"fail": True}

        def _maybe_rename(srcp, dstp):
            if state["fail"]:
                raise OSError("simulated rename failure")
            return real_rename(srcp, dstp)

        monkeypatch.setattr(mod.os, "rename", _maybe_rename)
        first = mod.run_pending_meshclaw_import()
        assert first["status"] == "copied_no_rename"
        assert mod._dest_backup_dir().is_dir()  # backup dir still on disk
        state["fail"] = False
        second = mod.run_pending_meshclaw_import()
        assert second["status"] == "ok" and second["renamed"] is True
        assert (env.dest / "memory.db").read_text() == "OLD_MEMORY"

    def test_keyboard_interrupt_rolls_back_and_reraises(self, env, monkeypatch):
        _seed_full_source(env)
        _write(env.dest / "memory.db", "FRESH_MEMORY")
        mod.write_import_intent()

        def _ki(*_a, **_k):
            raise KeyboardInterrupt()

        monkeypatch.setattr(mod, "_import_crons", _ki)
        with pytest.raises(KeyboardInterrupt):
            mod.run_meshclaw_import()
        # the full rollback happened BEFORE the re-raise
        assert (env.dest / "memory.db").read_text() == "FRESH_MEMORY"
        assert not (env.dest / "sessions").exists()
        assert not (env.dest / "workspace").exists()
        assert not mod._dest_backup_dir().exists()
        assert not mod._copied_marker_path().exists()
        assert not mod._intent_marker_path().exists()
        assert env.src.is_dir()
        assert not (env.home / ".meshclaw.bak").exists()


# ── fail-closed restore (WAL-set ordering, failure collection) ──────────────
class TestFailClosedRestore:
    def test_rollback_incomplete_keeps_backup(self, env, monkeypatch):
        _seed_full_source(env)
        _write(env.dest / "memory.db", "FRESH_MEMORY")
        mod.write_import_intent()
        monkeypatch.setattr(mod, "_import_crons", _raise_runtimeerror)
        monkeypatch.setattr(mod, "_restore_dest_targets", lambda dest: ["memory.db"])
        res = mod.run_meshclaw_import()
        assert res["status"] == "rollback_incomplete" and res["renamed"] is False
        # never reports clean: backup KEPT with an incomplete note
        assert mod._dest_backup_dir().is_dir()
        assert (
            mod._dest_backup_dir() / mod._RESTORE_INCOMPLETE_MARKER
        ).is_file()
        # run state still cleared; no rename attempted
        assert not mod._copied_marker_path().exists()
        assert not mod._intent_marker_path().exists()
        assert not (env.home / ".meshclaw.bak").exists()
        assert env.src.is_dir()

    def test_wal_removed_before_db_replace(self, env, monkeypatch):
        _write(env.dest / "memory.db", "FRESH")
        mod._backup_dest_targets(env.dest)
        # simulate the failed run's overwrite: replaced db + foreign WAL
        _write(env.dest / "memory.db", "IMPORTED")
        _write(env.dest / "memory.db-wal", "FOREIGN_WAL")
        real_replace = os.replace
        seen = {}

        def _watch(srcp, dstp):
            if str(dstp).endswith("memory.db"):
                seen["wal_gone_at_replace"] = not (
                    env.dest / "memory.db-wal"
                ).exists()
            return real_replace(srcp, dstp)

        monkeypatch.setattr(mod.os, "replace", _watch)
        failures = mod._restore_dest_targets(env.dest)
        assert failures == []
        assert (env.dest / "memory.db").read_text() == "FRESH"
        assert not (env.dest / "memory.db-wal").exists()
        # the foreign WAL was gone BEFORE the db was swapped in
        assert seen.get("wal_gone_at_replace") is True

    def test_backed_up_sidecars_restored_with_db(self, env):
        _write(env.dest / "memory.db", "FRESH")
        _write(env.dest / "memory.db-wal", "FRESH_WAL")
        mod._backup_dest_targets(env.dest)
        _write(env.dest / "memory.db", "IMPORTED")
        _write(env.dest / "memory.db-wal", "IMPORTED_WAL")
        failures = mod._restore_dest_targets(env.dest)
        assert failures == []
        assert (env.dest / "memory.db").read_text() == "FRESH"
        assert (env.dest / "memory.db-wal").read_text() == "FRESH_WAL"

    def test_restore_collects_failures_best_effort(self, env):
        _write(env.dest / "memory.db", "FRESH_MEM")
        _write(env.dest / "workspace" / "memory" / "preferences.md", "FRESH_PREFS")
        mod._backup_dest_targets(env.dest)
        _write(env.dest / "memory.db", "IMPORTED")
        (env.dest / "workspace" / "memory" / "preferences.md").write_text(
            "IMPORTED_PREFS", encoding="utf-8"
        )
        os.chmod(env.dest / "workspace" / "memory", 0o500)  # prefs restore fails
        try:
            failures = mod._restore_dest_targets(env.dest)
        finally:
            os.chmod(env.dest / "workspace" / "memory", 0o755)
        # NOT silent: the failed target is reported...
        assert failures == ["workspace/memory/preferences.md"]
        # ...and the other targets were still restored (best effort goes on)
        assert (env.dest / "memory.db").read_text() == "FRESH_MEM"


# ── onboarding /start restart guard (dashboard handler) ─────────────────────
class TestRestartInflightFlag:
    @staticmethod
    def _make_request(state):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        app = web.Application()
        app["state"] = state
        return make_mocked_request(
            "POST", "/api/onboarding/meshclaw-import/start", app=app
        )

    @staticmethod
    def _state():
        return SimpleNamespace(
            meshclaw_import_restart_inflight=False,
            _background_tasks=set(),
        )

    @staticmethod
    async def _settle(state):
        # Drain until quiescent: done-callbacks (scheduled via call_soon)
        # may spawn follow-up tasks (e.g. the intent-cleanup to_thread task).
        for _ in range(10):
            tasks = list(state._background_tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for _ in range(3):
                await asyncio.sleep(0)
            if not state._background_tasks:
                break

    @pytest.mark.asyncio
    async def test_flag_released_when_restart_returns_without_exec(
        self, env, monkeypatch
    ):
        import kiro_crew.dashboard.handlers.core as core_mod
        import kiro_crew.dashboard.handlers.updates as updates_mod

        _seed_full_source(env)
        monkeypatch.setattr(
            core_mod, "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **_kw: None),
        )

        async def _no_exec_restart(_state):
            # e.g. the invalid-executable early return: the task completes
            # normally but NO exec happened — the process keeps running.
            return None

        monkeypatch.setattr(updates_mod, "_restart_gateway", _no_exec_restart)
        state = self._state()
        resp = await core_mod.api_meshclaw_import_start(self._make_request(state))
        assert resp.status == 200
        await self._settle(state)
        # reaching the done-callback means no exec happened: the guard MUST
        # be released so the endpoint doesn't 409 forever
        assert state.meshclaw_import_restart_inflight is False
        assert not state._background_tasks
        # the restart this intent was written for never happened — the
        # intent must NOT linger for an unrelated later restart to consume
        assert not mod._intent_marker_path().exists()

    @pytest.mark.asyncio
    async def test_flag_released_when_restart_raises(self, env, monkeypatch):
        import kiro_crew.dashboard.handlers.core as core_mod
        import kiro_crew.dashboard.handlers.updates as updates_mod

        _seed_full_source(env)
        monkeypatch.setattr(
            core_mod, "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **_kw: None),
        )

        async def _boom(_state):
            raise RuntimeError("restart failed")

        monkeypatch.setattr(updates_mod, "_restart_gateway", _boom)
        state = self._state()
        resp = await core_mod.api_meshclaw_import_start(self._make_request(state))
        assert resp.status == 200
        await self._settle(state)
        assert state.meshclaw_import_restart_inflight is False
        assert not state._background_tasks
        # a failed restart is equally a no-exec outcome: intent cleared
        assert not mod._intent_marker_path().exists()
