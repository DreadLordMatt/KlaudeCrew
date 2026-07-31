"""Security tests for Ops Mission Control.

These lock in the properties that are expensive to retrofit: the provider tokens
must sit on the keystone floor so the agent cannot read or overwrite its own
credentials, and token shapes must be redacted before any provider payload reaches
a model prompt, a transcript, or Slack.

The keystone test deliberately asserts against the real ``security`` module rather
than a mock. A rename of ``SECRETS_FILENAME`` that forgot to update
``_CREW_SECRET_LEAVES`` would silently drop the protection with no other symptom,
so this is the test that catches it.
"""

import os
import tempfile
import unittest
from pathlib import Path

from kiro_crew import security
from kiro_crew.apps.builtins.ops_mission_control.backend import secrets


class TestKeystoneProtection(unittest.TestCase):
    def test_filename_is_registered_on_the_secret_floor(self):
        """The two lists must agree — see the module docstring."""
        self.assertIn(secrets.SECRETS_FILENAME, security._CREW_SECRET_LEAVES)

    def _secret_path(self) -> str:
        return os.path.expanduser(f"~/.kiro/crew/{secrets.SECRETS_FILENAME}")

    def test_agent_file_tools_cannot_touch_it(self):
        """``is_sensitive_path`` is the shared read+write gate for agent tools."""
        self.assertTrue(security.is_sensitive_path(self._secret_path()))

    def test_agent_shell_cannot_read_it(self):
        self.assertTrue(security.is_sensitive_bash_command(f"cat {self._secret_path()}"))

    def test_agent_shell_cannot_write_it(self):
        for command in (
            f"echo pwned > {self._secret_path()}",
            f"tee {self._secret_path()}",
            f"cp /tmp/x {self._secret_path()}",
        ):
            with self.subTest(command=command):
                self.assertTrue(security.is_sensitive_bash_command(command))

    def test_every_home_prefix_is_covered(self):
        """The floor is built per home prefix — including the legacy home.

        The prefixes are read from ``security._CREW_HOME_PREFIXES`` rather than
        written as a literal ``~/.kirocrew/...``: ``test_runtime_home_write_paths``
        forbids any Python outside ``test/`` from expanding a hardcoded legacy home
        (it is how the legacy dir kept getting re-created), and these tests live
        under ``src/``. Deriving the prefixes also means a future home move is
        covered here automatically.
        """
        for prefix in security._CREW_HOME_PREFIXES:
            with self.subTest(prefix=prefix):
                path = os.path.join(os.path.expanduser("~"), prefix, secrets.SECRETS_FILENAME)
                self.assertTrue(security.is_sensitive_path(path))


class TestRedaction(unittest.TestCase):
    def test_pagerduty_token_shape(self):
        out = secrets.redact_tokens("Authorization: Token token=u+AbCdEfGhIjKlMnOpQrStUv")
        self.assertNotIn("AbCdEfGhIjKlMnOpQrStUv", out)
        self.assertIn(secrets.REDACTED_PLACEHOLDER, out)

    def test_datadog_api_key_shape(self):
        key = "a" * 32
        out = secrets.redact_tokens(f"DD-API-KEY: {key}")
        self.assertNotIn(key, out)

    def test_datadog_app_key_shape(self):
        key = "b" * 40
        out = secrets.redact_tokens(f"app key {key} trailing")
        self.assertNotIn(key, out)

    def test_bearer_carrier(self):
        out = secrets.redact_tokens("Bearer: sk-abcdefghijklmnop")
        self.assertIn(secrets.REDACTED_PLACEHOLDER, out)

    def test_ordinary_text_survives(self):
        """Redaction must not mangle a normal diagnosis."""
        text = "RDS connections hit 800 of 1000; the pool is not being released."
        self.assertEqual(secrets.redact_tokens(text), text)

    def test_empty_input(self):
        self.assertEqual(secrets.redact_tokens(""), "")


class TestSecretBackend(unittest.TestCase):
    """The store is write-only over the API: values go in, only names come out."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp())
        self.backend = secrets.KeystoneFileBackend(self.tmp / "secrets.json")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_put_then_get(self):
        self.backend.put("pagerduty", "api_token", "u+secretvalue")
        self.assertEqual(self.backend.get("pagerduty", "api_token"), "u+secretvalue")

    def test_missing_returns_empty_not_raise(self):
        self.assertEqual(self.backend.get("nope", "nope"), "")

    def test_configured_fields_reports_names_only(self):
        self.backend.put("datadog", "api_key", "x" * 32)
        fields = self.backend.configured_fields("datadog")
        self.assertEqual(fields, frozenset({"api_key"}))

    def test_blank_value_is_not_configured(self):
        self.backend.put("datadog", "api_key", "")
        self.assertNotIn("api_key", self.backend.configured_fields("datadog"))

    def test_delete_removes_all_fields(self):
        self.backend.put("datadog", "api_key", "x" * 32)
        self.backend.put("datadog", "app_key", "y" * 40)
        self.assertTrue(self.backend.delete("datadog"))
        self.assertEqual(self.backend.configured_fields("datadog"), frozenset())

    def test_delete_unknown_is_false(self):
        self.assertFalse(self.backend.delete("never-configured"))

    def test_file_is_owner_only(self):
        """A world-readable token file would defeat the whole design."""
        from kiro_crew import platform_compat

        self.backend.put("pagerduty", "api_token", "u+secretvalue")
        path = self.tmp / "secrets.json"
        self.assertTrue(path.exists())
        if platform_compat.IS_POSIX:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_file_degrades_to_empty(self):
        (self.tmp / "secrets.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.backend.get("pagerduty", "api_token"), "")


class TestDescribeSecrets(unittest.TestCase):
    def test_never_returns_a_value(self):
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        backend = secrets.KeystoneFileBackend(tmp / "s.json")
        backend.put("pagerduty", "api_token", "u+thisisthesecret")
        secrets.register_secret_backend(backend)
        try:
            described = secrets.describe_secrets("pagerduty", ("api_token",))
            self.assertEqual(described["api_token"], secrets.REDACTED_PLACEHOLDER)
            self.assertNotIn("thisisthesecret", str(described))
        finally:
            secrets.register_secret_backend(secrets.KeystoneFileBackend())
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestCrossPlatform(unittest.TestCase):
    """AGENTS.md requires macOS + Linux + Windows for every change.

    This app spawns two external binaries (`git` for ledger sync, `gh` for the rotation
    login) and does timezone math, which is where the Windows differences actually bite.
    Asserted from source rather than by running on Windows, because CI here is POSIX —
    the point is to catch a raw POSIX call at review time, not to simulate the platform.
    """

    APP_FILES = ("backend/ledger_sync.py", "backend/providers/schedule_file.py")

    def _sources(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        root = Path(ledger_sync.__file__).resolve().parent.parent
        return {name: (root / name).read_text(encoding="utf-8") for name in self.APP_FILES}

    def test_preexec_fn_comes_from_the_shim_not_a_raw_callable(self):
        """``preexec_fn`` is unsupported on Windows — passing ANY callable raises.

        ``resource_limit_preexec()`` returns ``None`` off POSIX, so routing through it is
        what makes these spawns portable. A hand-rolled ``preexec_fn=lambda: ...`` would
        work locally and raise ValueError on every Windows spawn.
        """
        for name, src in self._sources().items():
            if "preexec_fn" not in src:
                continue
            with self.subTest(file=name):
                self.assertIn("resource_limit_preexec", src)
                for line in src.splitlines():
                    if "preexec_fn=" in line:
                        self.assertIn(
                            "resource_limit_preexec()",
                            line,
                            f"{name}: preexec_fn must come from the shim (Windows-safe)",
                        )

    def test_no_raw_posix_process_calls(self):
        """Per the platform_compat shim table. `os.kill(pid, 0)` TERMINATES on Windows."""
        banned = ("os.killpg", "os.getpgid", "os.getuid", "fcntl.", "signal.SIGKILL")
        for name, src in self._sources().items():
            with self.subTest(file=name):
                for token in banned:
                    self.assertNotIn(token, src, f"{name} uses POSIX-only {token}")

    def test_no_posix_only_paths_or_shell(self):
        """A hardcoded `/bin/sh` or `/tmp` is a Windows failure and a sandbox bypass."""
        for name, src in self._sources().items():
            with self.subTest(file=name):
                self.assertNotIn("/bin/sh", src)
                self.assertNotIn("shell=True", src)
                self.assertNotIn('"/tmp/', src)

    def test_timezone_lookup_degrades_instead_of_raising(self):
        """Windows ships no system IANA database, so `ZoneInfo(...)` can raise.

        `tzdata` is a declared Windows dependency, but an install that somehow lacks it
        must still resolve a rotation rather than crash the 5-minute cron. Verified by
        making the import itself fail, which is the shape of the real failure.
        """
        import builtins
        from datetime import datetime, timezone
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            schedule_file,
        )

        tmp = Path(tempfile.mkdtemp())
        prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(tmp)
        try:
            path = schedule_file.schedule_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "timezone: America/Los_Angeles\n"
                "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n",
                encoding="utf-8",
            )
            real_import = builtins.__import__

            def _no_zoneinfo(name, *args, **kwargs):
                if name == "zoneinfo":
                    raise ImportError("No module named 'zoneinfo'")
                return real_import(name, *args, **kwargs)

            with mock.patch.object(schedule_file, "_resolve_login_sync", return_value="octocat"):
                with mock.patch.object(builtins, "__import__", _no_zoneinfo):
                    status = schedule_file.resolve_now(
                        datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
                    )
            # A definitive answer, not the fail-open "unknown" — the window still resolves,
            # just in UTC.
            self.assertTrue(status.on_shift)
            self.assertFalse(status.unknown)
            self.assertEqual(status.who, "octocat")
        finally:
            if prev is None:
                os.environ.pop("KIROCREW_HOME", None)
            else:
                os.environ["KIROCREW_HOME"] = prev
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
