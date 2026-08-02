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


class TestLegacyConfigIsMigrated(_HomeIsolated):
    """A pre-fix install wrote the ceiling into config.json. First read must lift it out."""

    def _seed_legacy_config(self, **keys: object) -> Path:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            CONFIG_FILENAME,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.store import APP_NAME
        from kiro_crew.apps.manager import app_data_dir

        cfg = app_data_dir(APP_NAME) / CONFIG_FILENAME
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(keys), encoding="utf-8")
        return cfg

    def test_a_legacy_mode_is_lifted_onto_the_floor_and_removed_from_config(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        self._seed_legacy_config(mode="act", autonomy_rules=[{"mode": "act", "source": "x", "resource_glob": "*"}], region="us-east-1")

        # First read triggers the migration.
        self.assertEqual(rotation.app_mode(), "act")

        # Ceiling now on the fenced floor...
        self.assertEqual(policy_store.read_mode("observe"), "act")
        self.assertTrue(policy_store.policy_path().exists())
        # ...and GONE from the agent-writable file, so no live shadow copy remains.
        remaining = read_config()
        self.assertNotIn("mode", remaining)
        self.assertNotIn("autonomy_rules", remaining)
        # Non-ceiling config the agent legitimately reads is untouched.
        self.assertEqual(remaining.get("region"), "us-east-1")

    def test_migration_does_not_clobber_an_existing_policy_file(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation

        policy_store.set_mode("propose")  # operator already set one on the floor
        self._seed_legacy_config(mode="act")  # a stale config value must NOT win

        self.assertEqual(rotation.app_mode(), "propose")

    def test_migration_is_idempotent_and_noop_without_legacy_keys(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        self.assertFalse(policy_store.migrate_from_config_if_needed())


if __name__ == "__main__":
    unittest.main()
