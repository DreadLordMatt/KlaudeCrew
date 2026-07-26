"""Phase 5 — profile store + active-scope resolution.

Covers: per-surface / per-app / per-task binding, deny-by-default on unproven
unattended identity, schema-invalid → deny-all (not the ceiling), ``extends``
narrowing, and mtime hot-reload.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.governance import resolve


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    yield d
    gp.reset_store()


def _write(d, name, body):
    (d / f"{name}.json").write_text(json.dumps(body))


def test_surface_binding_resolves(profiles_dir):
    _write(
        profiles_dir,
        "cron-tight",
        {
            "name": "cron-tight",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("cron:job-7:run-1")
    assert prof is not None and prof.name == "cron-tight"


def test_app_binding_wins_over_surface(profiles_dir):
    _write(
        profiles_dir,
        "deploy",
        {
            "name": "deploy",
            "bind": {"type": "app", "id": "deploy-web"},
            "tools": {"mode": "allow", "allow": ["code"]},
        },
    )
    prof = gp.resolve_active_scope("dashboard:slot1", app="deploy-web")
    assert prof is not None and prof.name == "deploy"


def test_agent_task_binding(profiles_dir):
    _write(
        profiles_dir,
        "researcher",
        {
            "name": "researcher",
            "bind": {"type": "task", "id": "researcher"},
            "capabilities": {"spawn": {"enabled": False}},
        },
    )
    prof = gp.resolve_active_scope("subagent:abc", agent="researcher")
    assert prof is not None and prof.name == "researcher"


def test_unattended_unproven_identity_denies_all(profiles_dir):
    # No bound profile, unattended surface (_hb), unproven → deny-all.
    prof = gp.resolve_active_scope("_hb")
    assert prof is not None
    assert prof.name.startswith("_deny_all")
    # deny-all denies tools.
    assert not resolve(None, prof, "tools", "read").permitted


def test_attended_surface_no_profile_is_none(profiles_dir):
    # cli is attended; no bound profile → None (policy ceiling alone governs).
    assert gp.resolve_active_scope("cli_chat") is None


def test_proven_cron_no_profile_is_none(profiles_dir):
    # A cron job with a real session key (proven identity) and no bound profile
    # → None (policy governs); deny-all only kicks in on UNPROVEN identity.
    assert gp.resolve_active_scope("cron:job-9:run-2") is None


def test_invalid_profile_falls_back_to_deny_all(profiles_dir):
    # Schema-invalid profile (bad bind type) → deny-all sentinel, NOT ceiling.
    _write(
        profiles_dir,
        "broken",
        {"name": "broken", "bind": {"type": "galaxy"}, "tools": {"mode": "allow"}},
    )
    prof = gp.get_store_profile("broken")
    # Fallback keeps the file stem (so any bind index stays coherent) but is
    # behaviorally deny-all — NOT the permissive ceiling.
    assert prof is not None
    assert not resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "capabilities.spawn", "researcher").permitted


def test_invalid_profile_with_valid_bind_still_denies_its_surface(profiles_dir):
    # A profile with a VALID bind but an INVALID control must still bind its
    # surface to deny-all (fail-closed) — NOT be dropped from the bind index and
    # fail open to policy-only.
    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "banana"},  # invalid → parse_profile raises
        },
    )
    prof = gp.resolve_active_scope("cron:job-7:run-1")
    assert prof is not None, "bound surface must resolve to the deny-all fallback, not None"
    assert not resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "capabilities.spawn", "researcher").permitted


def _make_read_text_raise(monkeypatch, target: "object", exc: Exception):
    """Monkeypatch ``Path.read_text`` to raise ``exc`` for ``target`` only.

    Portable simulation of a present-but-unreadable file (chmod 000 is a no-op on
    Windows). ``target`` is compared by resolved path so it matches regardless of
    how the store constructs the Path.
    """
    from pathlib import Path

    real_read_text = Path.read_text
    target_str = str(target)

    def _patched(self, *args, **kwargs):
        if str(self) == target_str:
            raise exc
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _patched)


def _write_host_profile(path):
    path.write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
    )


def _make_ceiling():
    from kiro_crew.platform.governance import parse_policy

    return parse_policy({"version": 1, "boot": {"fail_closed": True}})


def test_unreadable_profile_governed_fleet_boot_aborts(profiles_dir, monkeypatch):
    # F1-1 (corrected): a PRESENT-but-UNREADABLE profile whose bind cannot be
    # recovered must NOT be guessed by filename (the stem does not reliably encode
    # the bind). For a GOVERNED fleet (ceiling present) it fails CLOSED to a
    # boot-abort: assert_profiles_within_ceiling raises PlatformCompositionError
    # rather than run with a silently-dropped restrictive profile.
    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance_profiles import assert_profiles_within_ceiling

    path = profiles_dir / "host.json"
    _write_host_profile(path)
    _make_read_text_raise(monkeypatch, path, OSError("permission denied"))
    gp.reset_store()

    with pytest.raises(PlatformCompositionError):
        assert_profiles_within_ceiling(_make_ceiling())


def test_unreadable_profile_standalone_is_lenient(profiles_dir, monkeypatch):
    # F1-1 (corrected): a standalone/ungoverned host (no ceiling) must NOT crash
    # on an unreadable profile blip. assert_profiles_within_ceiling(None) is a
    # no-op, and the unbound deny-all fallback simply drops out — the surface
    # falls to policy-only (matches pre-split standalone behavior, no regression).
    from kiro_crew.platform.governance_profiles import (
        HOST_SESSION_KEY,
        assert_profiles_within_ceiling,
    )

    path = profiles_dir / "host.json"
    _write_host_profile(path)
    _make_read_text_raise(monkeypatch, path, OSError("permission denied"))
    gp.reset_store()

    assert_profiles_within_ceiling(None)  # no ceiling → no crash
    # Unbound deny-all drops from the bind index → host surface is policy-only.
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is None


def test_invalid_utf8_profile_governed_fleet_boot_aborts(profiles_dir, monkeypatch):
    # F2-1 (UTF-8): an invalid-encoding file raises UnicodeDecodeError (base
    # UnicodeError), which is NOT an OSError. The read guard must catch
    # (OSError, UnicodeError) so it does not escape both handlers and crash boot
    # uncaught. Treated as present-but-unreadable → governed fleet boot-aborts.
    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance_profiles import assert_profiles_within_ceiling

    path = profiles_dir / "host.json"
    # Write raw invalid UTF-8 bytes (0xff is never valid UTF-8).
    path.write_bytes(b'{"name": "host", "bind": {"type": "surface", "id": "\xff\xfe"}}')
    gp.reset_store()

    # Must not escape uncaught; a governed fleet boot-aborts fail-closed.
    with pytest.raises(PlatformCompositionError):
        assert_profiles_within_ceiling(_make_ceiling())


def test_invalid_utf8_profile_standalone_does_not_crash(profiles_dir):
    # F2-1: the SAME invalid-encoding file on a standalone host (no ceiling) must
    # be tolerated — resolve must not raise UnicodeDecodeError.
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    path = profiles_dir / "host.json"
    path.write_bytes(b'{"name": "host", "bind": {"type": "surface", "id": "\xff\xfe"}}')
    gp.reset_store()
    # No crash; unbound deny-all → policy-only.
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is None


def test_transient_read_error_not_cached(profiles_dir, monkeypatch):
    # F2-3: a transient read error must NOT be cached — once the read recovers,
    # the next access re-reads and the real (readable) profile takes effect,
    # instead of the deny-all fallback staying cached until metadata changes.
    from pathlib import Path

    path = profiles_dir / "cron.json"
    path.write_text(
        json.dumps(
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    real_read_text = Path.read_text
    state = {"fail": True}
    target = str(path)

    def _patched(self, *args, **kwargs):
        if str(self) == target and state["fail"]:
            raise OSError("transient NFS blip")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _patched)
    gp.reset_store()
    # First access hits the transient error → unbound deny-all → cron surface
    # policy-only (the fallback dropped out); fingerprint NOT cached.
    assert gp.resolve_active_scope("cron:job-1:run-1") is None
    # Read recovers WITHOUT any file-metadata change.
    state["fail"] = False
    # Next access must re-read (fingerprint was not committed) and pick up the
    # real profile — proving the transient error was not cached.
    prof = gp.resolve_active_scope("cron:job-1:run-1")
    assert prof is not None and prof.name == "cron"


def test_absent_profile_still_yields_policy_only(profiles_dir):
    # GUARDRAIL: the fix must NOT manufacture a deny for a surface that has NO
    # profile at all. An absent host profile → resolve_active_scope returns None
    # (attended/host surface, policy ceiling alone governs), NOT a false deny.
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    # profiles_dir is empty (no host.json).
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is None


def test_vanished_profile_mid_reload_is_absent_not_deny(profiles_dir, monkeypatch):
    # A file present at iterdir() but gone at read (TOCTOU) is treated as ABSENT
    # (skipped), not as a present-but-unreadable deny — a missing file is not a
    # policy. FileNotFoundError is a subclass of OSError, so the reload must
    # distinguish it from the genuine-unreadable case.
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    path = profiles_dir / "host.json"
    path.write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
    )
    _make_read_text_raise(monkeypatch, path, FileNotFoundError("vanished"))
    gp.reset_store()
    # Vanished → absent → policy-only (None), no false deny.
    assert gp.resolve_active_scope(HOST_SESSION_KEY) is None


def test_extends_narrows(profiles_dir):
    _write(
        profiles_dir,
        "base",
        {"name": "base", "tools": {"mode": "allow", "allow": ["read", "grep", "code"]}},
    )
    _write(
        profiles_dir,
        "child",
        {
            "name": "child",
            "extends": "base",
            "bind": {"type": "surface", "id": "dashboard"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("dashboard:x")
    assert prof is not None
    assert resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "tools", "grep").permitted


def test_extends_missing_parent_still_denies_its_surface(profiles_dir):
    # A profile bound to a surface whose ``extends`` parent is MISSING must
    # revert to deny-all WHILE PRESERVING its bind — so the bound surface
    # resolves to deny-all (fail-closed), not None (which would fall through to
    # the policy ceiling alone, bypassing operator narrowing). Mirrors
    # test_invalid_profile_with_valid_bind_still_denies_its_surface for the
    # Pass-2 (extends) path. (security-review blocking finding.)
    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "extends": "does-not-exist",
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("cron:job-7:run-1")
    assert prof is not None, "bound surface must resolve to deny-all, not None (fail-open)"
    assert not resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "capabilities.spawn", "researcher").permitted


def test_extends_chain_still_denies_its_surface(profiles_dir):
    # A non-trivial chain (c -> b -> a, where the parent b ITSELF extends) is
    # rejected to deny-all; the bound surface must still fail CLOSED, not drop to
    # None.  Every profile in the chain allows ``read`` AND ``spawn``, so if c had
    # (wrongly) COMPOSED through the chain it would PERMIT both — asserting both
    # are DENIED distinguishes the deny-all branch from a mere empty-intersection
    # narrowing, which the prior version of this test failed to do.
    # Every member allows ``read`` (non-empty intersection) so a COMPOSE would
    # PERMIT ``tools/read``; and none narrows ``capabilities.spawn`` (default
    # true) so a COMPOSE would PERMIT spawn too — therefore asserting BOTH are
    # DENIED proves the deny-all branch, not a coincidental empty intersection
    # (the gap the prior version of this test had).
    _write(profiles_dir, "a", {"name": "a", "tools": {"mode": "allow", "allow": ["read"]}})
    _write(
        profiles_dir,
        "b",
        {"name": "b", "extends": "a", "tools": {"mode": "allow", "allow": ["read"]}},
    )
    _write(
        profiles_dir,
        "c",
        {
            "name": "c",
            "extends": "b",  # parent b itself extends -> non-trivial chain
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("cron:job:run")
    assert prof is not None, "chained-extends bound surface must resolve to deny-all, not None"
    # deny-all signature: both would be ALLOWED if c had composed through the chain.
    assert not resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "capabilities.spawn", "researcher").permitted


def test_extends_chain_rejection_is_order_independent(profiles_dir):
    # The compose-vs-deny-all verdict must NOT depend on the order the profile
    # files happen to sort.  Here the mid-parent ``mmm`` sorts BEFORE the child
    # ``zzz`` (so it is composed first, clearing its live ``extends``); a verdict
    # read from the live dict would then WRONGLY compose ``zzz`` (fail-open).  The
    # snapshot-of-original-extends fix must still deny-all.
    _write(profiles_dir, "bbb", {"name": "bbb", "tools": {"mode": "allow", "allow": ["read"]}})
    _write(
        profiles_dir,
        "mmm",
        {"name": "mmm", "extends": "bbb", "tools": {"mode": "allow", "allow": ["read"]}},
    )
    _write(
        profiles_dir,
        "zzz",
        {
            "name": "zzz",
            "extends": "mmm",  # mid-parent mmm itself extends -> non-trivial chain
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("cron:job:run")
    assert prof is not None, "chained-extends bound surface must resolve to deny-all, not None"
    assert not resolve(
        None, prof, "tools", "read"
    ).permitted, "non-trivial chain must deny-all regardless of file sort order"


def test_hot_reload_picks_up_edit(profiles_dir):
    _write(
        profiles_dir,
        "cron-tight",
        {
            "name": "cron-tight",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    prof = gp.resolve_active_scope("cron:j:r")
    assert resolve(None, prof, "tools", "read").permitted
    assert not resolve(None, prof, "tools", "code").permitted

    # Edit the file: widen to include code (still bounded by policy at runtime).
    import os

    path = profiles_dir / "cron-tight.json"
    _write(
        profiles_dir,
        "cron-tight",
        {
            "name": "cron-tight",
            "bind": {"type": "surface", "id": "cron"},
            "tools": {"mode": "allow", "allow": ["read", "code"]},
        },
    )
    # Bump mtime explicitly so the fingerprint changes even on coarse clocks.
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    prof2 = gp.resolve_active_scope("cron:j:r")
    assert resolve(None, prof2, "tools", "code").permitted


def test_no_profiles_dir_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "_PROFILES_DIR", tmp_path / "does-not-exist")
    gp.reset_store()
    try:
        # Attended surface, no dir → None; unattended unproven → deny-all.
        assert gp.resolve_active_scope("cli_chat") is None
        assert gp.resolve_active_scope("_bg").name.startswith("_deny_all")
    finally:
        gp.reset_store()
