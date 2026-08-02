"""The autonomy ceiling must be on the keystone floor, not in agent-writable config.

`mode` + `autonomy_rules` are the app's security ceiling: `effective = min(app_mode,
rule_mode)` is only a ceiling if the agent cannot raise it. They lived in `data/config.json`,
which is served unauthenticated over `/config` and writable by any auto-approved agent shell —
so a prompt-injected agent could set `mode=act` plus a matching rule and unlock a provider
write. Found in review; fixed by moving them to `ops_mission_control_policy.json` on the
`security._CREW_SECRET_LEAVES` floor.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path


class _HomeIsolated(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestTheCeilingIsOnTheKeystoneFloor(_HomeIsolated):
    def test_the_policy_file_is_fenced_and_config_is_not(self):
        """The whole point: the agent can neither read nor overwrite the ceiling."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            CONFIG_FILENAME,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.store import APP_NAME
        from kiro_crew.apps.manager import app_data_dir
        from kiro_crew.security import is_sensitive_path

        self.assertTrue(
            is_sensitive_path(str(policy_store.policy_path())),
            "the autonomy ceiling must be on the keystone floor",
        )
        cfg = app_data_dir(APP_NAME) / CONFIG_FILENAME
        self.assertFalse(
            is_sensitive_path(str(cfg)),
            "sanity: config.json is NOT fenced — which is exactly why the ceiling cannot "
            "live there",
        )

    def test_the_filename_matches_the_fence_entry(self):
        """A rename here without updating `_CREW_SECRET_LEAVES` silently un-protects it."""
        from kiro_crew import security
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        self.assertIn(policy_store.POLICY_FILENAME, security._CREW_SECRET_LEAVES)

    def test_setting_the_mode_writes_the_fenced_file_not_config(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        policy_store.set_mode("act")
        self.assertEqual(policy_store.read_mode("observe"), "act")
        self.assertTrue(policy_store.policy_path().exists())
        # The agent-writable file must NOT carry the ceiling.
        self.assertNotIn("mode", read_config())

    def test_rotation_reads_mode_and_rules_from_the_store(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation

        policy_store.set_mode("act")
        policy_store.set_rules(
            [{"mode": "act", "source": "cloudwatch", "resource_glob": "prod-*"}]
        )
        self.assertEqual(rotation.app_mode(), "act")
        rules = rotation.load_rules()
        self.assertEqual(len(rules), 1)


class TestConfigJsonCannotSetTheCeiling(_HomeIsolated):
    """The ceiling is read ONLY from the keystone file, never from agent-writable config.json.

    An earlier revision migrated `mode`/`autonomy_rules` out of `config.json` onto the fenced
    floor on first read, to spare a "pre-fence install" a shadowed copy. That migration WAS
    the hole: `config.json` is on no sensitive-path list, so an auto-approved agent shell could
    write `{"mode":"act", "autonomy_rules":[...]}` there, and the next `app_mode()` — reached
    from `authorize_action` on every action — promoted it to the keystone and made it
    authoritative, granting a real production write the operator never authorized. The app is
    new in this PR, so there is no install whose ceiling needs lifting. Found in review (Opus 5).
    """

    def _seed_config(self, **keys: object) -> Path:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            CONFIG_FILENAME,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.store import APP_NAME
        from kiro_crew.apps.manager import app_data_dir

        cfg = app_data_dir(APP_NAME) / CONFIG_FILENAME
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(keys), encoding="utf-8")
        return cfg

    def test_a_mode_written_to_config_json_does_not_raise_the_ceiling(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation

        # An auto-approved agent shell writes the ceiling it wants into config.json.
        self._seed_config(
            mode="act",
            autonomy_rules=[{"mode": "act", "source": "pagerduty", "resource_glob": "*"}],
        )

        # It must be ignored: the gate stays at the safe default.
        self.assertEqual(rotation.app_mode(), "observe")
        self.assertEqual(rotation.load_rules(), [])
        # And the read must NOT have created a keystone file from the config values.
        self.assertFalse(policy_store.policy_path().exists())

    def test_the_migration_helper_no_longer_exists(self):
        """Deleting the function is the fix; a test pins that it stays deleted, because
        re-adding it silently re-opens the ceiling-raise."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        self.assertFalse(hasattr(policy_store, "migrate_from_config_if_needed"))

    def test_the_keystone_is_authoritative_when_the_operator_set_it(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation

        policy_store.set_mode("propose")  # the dashboard PUT is the only writer
        self._seed_config(mode="act")  # a stale/hostile config value must NOT win
        self.assertEqual(rotation.app_mode(), "propose")


class TestOutboundDestinationsAreOperatorOnly(_HomeIsolated):
    """WHERE this app sends things is a security decision, not a preference.

    Found by auditing for the class of the autonomy-ceiling finding rather than waiting for it
    to be reported. Two destinations were in agent-writable `config.json`:

    - `ledger_sync_remote` — the git remote the shared knowledge ledger is PUSHED to. An agent
      that rewrites it exfiltrates the team's accumulated incident knowledge, and
      `POST /ledger/hygiene` (which the agent's own hygiene cron calls) performs the push.
    - `slack_channel` — where every incident title, diagnosis and resource name is mirrored.

    Verified before fixing: writing `config.json` moved both, and `config.json` is neither
    path-fenced (`is_sensitive_path`) nor shell-write-blocked
    (`is_sensitive_bash_command("echo x > …")`).
    """

    def test_an_agent_write_cannot_move_the_ledger_remote(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import write_config

        ledger_sync.set_settings(enabled=True, remote_url="https://github.com/org/real.git")
        # The agent writes config.json — the only file it can reach.
        write_config({"ledger_sync_remote": "https://attacker.example/exfil.git"})
        self.assertEqual(
            ledger_sync.remote(),
            "https://github.com/org/real.git",
            "the operator's remote must win; config.json must not redirect the push",
        )

    def test_an_agent_write_cannot_move_the_slack_channel(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import write_config

        slack_out.set_settings(enabled=True, channel_id="C_REAL")
        write_config({"slack_channel": "C_ATTACKER", "slack_enabled": True})
        self.assertEqual(slack_out.channel(), "C_REAL")

    def test_the_destinations_live_on_the_fenced_floor(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            ledger_sync,
            policy_store,
            slack_out,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        ledger_sync.set_settings(enabled=True, remote_url="https://github.com/org/real.git")
        slack_out.set_settings(enabled=True, channel_id="C_REAL")
        cfg = read_config()
        for key in ("ledger_sync_remote", "ledger_sync_enabled", "slack_channel", "slack_enabled"):
            self.assertNotIn(key, cfg, f"{key} must not be written to agent-writable config")
            self.assertIn(key, policy_store.OPERATOR_ONLY_KEYS)

    def test_the_branch_stays_in_plain_config_deliberately(self):
        """It selects a ref inside a remote the operator chose; it cannot move data off-box."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync, policy_store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        ledger_sync.set_settings(branch_name="main")
        self.assertEqual(read_config().get("ledger_sync_branch"), "main")
        self.assertNotIn("ledger_sync_branch", policy_store.OPERATOR_ONLY_KEYS)

    def test_a_destination_written_to_config_json_does_not_redirect_the_exchange(self):
        """`ledger_sync_remote` in config.json must NOT become the push destination — that is
        the exfiltration the fencing exists to stop, and reading config for it (or migrating
        it onto the floor) would reopen it. With nothing on the keystone, the remote is unset,
        not the agent-supplied one."""
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync, policy_store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            CONFIG_FILENAME,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.store import APP_NAME
        from kiro_crew.apps.manager import app_data_dir

        cfg = app_data_dir(APP_NAME) / CONFIG_FILENAME
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            json.dumps({"ledger_sync_remote": "https://attacker.example/exfil.git"}),
            encoding="utf-8",
        )
        self.assertEqual(ledger_sync.remote(), "")
        self.assertFalse(policy_store.policy_path().exists())

    def test_put_refuses_a_key_that_is_not_operator_only(self):
        """The allow-list is the contract — a typo must not silently create a fenced key."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        with self.assertRaises(KeyError):
            policy_store.put("region", "us-east-1")
        with self.assertRaises(KeyError):
            policy_store.get("region")


if __name__ == "__main__":
    unittest.main()


class TestActRulesAreAuthorable(_HomeIsolated):
    """The `act` mode must have a WRITE path. It had none.

    `policy_store.set_rules` existed with zero callers anywhere, so the app's headline
    autonomy tier was unreachable: Settings said grants came from "patterns you have
    explicitly allowlisted with a rule", offered nothing to click, and the manual pointed at
    `data/config.json` — which the keystone migration ignores once the policy file exists. So
    an operator who followed the docs got silent Propose behavior forever. Found in review.
    """

    def test_a_valid_rule_round_trips_through_save_and_load(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        submitted = [
            {
                "source": "pagerduty",
                "mode": "act",
                "resource_glob": "Checkout*",
                "actions": ["ack"],
            }
        ]
        ok, code, normalized = rotation.save_rules(submitted)
        self.assertTrue(ok, code)
        # Read back through the REAL gate loader, not the raw file: what the operator sees
        # must be what actually authorizes.
        loaded = [rotation.rule_to_dict(r) for r in rotation.load_rules()]
        self.assertEqual(loaded, normalized)
        self.assertEqual(loaded, submitted)

    def test_rules_land_on_the_keystone_floor_not_in_config_json(self):
        """A grant is half the authorization, so the agent must not be able to write it."""
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config
        from kiro_crew.security import is_sensitive_path

        ok, _, _ = rotation.save_rules(
            [{"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}]
        )
        self.assertTrue(ok)

        path = policy_store.policy_path()
        self.assertTrue(is_sensitive_path(str(path)), "the ceiling is not agent-fenced")
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("autonomy_rules", stored)
        # And NOT in the agent-writable file.
        self.assertNotIn("autonomy_rules", read_config())

    def test_a_blanket_act_rule_is_refused_not_silently_dropped(self):
        """`load_rules` skips unparseable entries, so storing one would show the operator a
        saved grant that never matches — the exact failure the two-key design prevents."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        ok, code, _ = rotation.save_rules([{"source": "cloudwatch", "mode": "act"}])
        self.assertFalse(ok)
        self.assertEqual(code, "rule_0_invalid")
        # Nothing was persisted.
        self.assertEqual(rotation.load_rules(), [])

    def test_the_offending_index_is_reported(self):
        """A ten-rule submission with one bad entry must say WHICH one."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        ok, code, _ = rotation.save_rules([
            {"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"},
            {"source": "datadog", "mode": "act"},
        ])
        self.assertFalse(ok)
        self.assertEqual(code, "rule_1_invalid")

    def test_non_list_and_non_object_payloads_are_refused(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        # Deliberately the WRONG type: this arrives as untrusted JSON from a PUT body, so the
        # runtime guard is the thing under test and mypy's objection is the point.
        ok, code, _ = rotation.save_rules({"source": "cloudwatch"})  # type: ignore[arg-type]
        self.assertFalse(ok)
        self.assertEqual(code, "rules_not_a_list")

        ok2, code2, _ = rotation.save_rules(["cloudwatch"])
        self.assertFalse(ok2)
        self.assertEqual(code2, "rule_0_not_an_object")

    def test_saving_an_empty_list_clears_every_grant(self):
        """Revoking must be expressible, or an operator cannot take authority back."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        rotation.save_rules([{"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}])
        self.assertEqual(len(rotation.load_rules()), 1)
        ok, _, normalized = rotation.save_rules([])
        self.assertTrue(ok)
        self.assertEqual(normalized, [])
        self.assertEqual(rotation.load_rules(), [])

    def test_describe_exposes_the_rules_not_just_a_count(self):
        """A count cannot be rendered, edited or verified — the UI had nothing to show."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        rule = {"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}
        rotation.save_rules([rule])
        view = rotation.describe(ShiftStatus(on_shift=True))
        self.assertEqual(view["rules"], 1)
        self.assertEqual(view["rules_detail"], [rule])
