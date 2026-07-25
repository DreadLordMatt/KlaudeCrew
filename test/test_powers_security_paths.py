"""Powers install-provenance write protection.

``powers/.marketplace-cache.json`` decides WHICH repository a marketplace id
resolves to, so a forged cache makes the user install an attacker's repo while
the UI shows a familiar name; ``powers/installed.json`` records where each
installed bundle came from. Neither is re-validated after write, so these tests
pin the agent file-edit gate against the whole subtree so a future edit to the
protected-path list cannot silently drop Powers coverage.

Scope: bundle CONTENTS are inert in this release, so the file-edit gate alone is
proportionate for them. The two state files nothing re-validates after write are
additionally shell-gated via the shared ``_WRITE_PROTECTED_BASH_LEAVES`` matcher,
because a planted `.marketplace-cache.json` substitutes the install target and no
later validation can recover the user's intent.
"""

import pytest

from kiro_crew.security import (
    is_sensitive_bash_command,
    is_sensitive_path,
    is_sensitive_write_path,
)

# Both crew home layouts: the current one and a not-yet-migrated legacy home.
PREFIXES = ("~/.kiro/crew", "~/.kirocrew")

# installed.json  -> the provenance record shown in the UI
# .marketplace-cache.json -> remaps a familiar power name to another repository
# <power>/mcp.json -> bundle contents, protected after the vetted allowlist copy
LEAVES = (
    "powers/installed.json",
    "powers/.marketplace-cache.json",
    "powers/stripe-payments/mcp.json",
)


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("leaf", LEAVES)
class TestPowersFileEditGate:
    def test_write_is_blocked(self, prefix: str, leaf: str) -> None:
        assert is_sensitive_write_path(f"{prefix}/{leaf}") is True

    def test_read_is_still_allowed(self, prefix: str, leaf: str) -> None:
        """The dashboard renders installed Powers; reads must not be gated."""
        assert is_sensitive_path(f"{prefix}/{leaf}") is False


class TestPowersProtectionIsSubtreeWide:
    def test_arbitrary_bundle_file_is_write_protected(self) -> None:
        """Whole-subtree coverage, not an enumerated list of two state files."""
        assert is_sensitive_write_path("~/.kiro/crew/powers/anything/POWER.md") is True

    def test_unrelated_crew_home_file_is_unaffected(self) -> None:
        """Guard against over-blocking the rest of the crew home."""
        assert is_sensitive_write_path("~/.kiro/crew/sessions.db") is False


# The two state files nothing re-validates after write. `.marketplace-cache.json`
# decides WHICH repository a marketplace id installs, so a planted entry
# substitutes the install TARGET under a familiar name; `installed.json` is the
# provenance the dashboard renders. The file-edit gate above covers tool writes;
# these pin the SHELL path via the shared `_WRITE_PROTECTED_BASH_LEAVES` matcher.
BASH_PROTECTED_LEAVES = ("powers/installed.json", "powers/.marketplace-cache.json")


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("leaf", BASH_PROTECTED_LEAVES)
class TestPowersBashGate:
    @pytest.mark.parametrize(
        "template",
        [
            "echo '{{}}' > {path}",
            "tee {path} < evil",
            "sed -i s/a/b/ {path}",
            "cp evil.json {path}",
            "rm {path}",
            "python3 -c \"open('{path}','w')\"",
        ],
    )
    def test_shell_write_is_blocked(self, prefix: str, leaf: str, template: str) -> None:
        """Verb-INDEPENDENT: any command naming the leaf is refused.

        An enumerated write-verb allowlist is bypassable (novel verbs, quoted
        redirects, `open(...,'w')` from a language runtime), which is why the
        shared matcher blocks on the NAME appearing at all.
        """
        cmd = template.format(path=f"{prefix}/{leaf}")
        assert is_sensitive_bash_command(cmd) is not None


class TestPowersBashGateDoesNotOverBlock:
    def test_bundle_contents_are_not_bash_blocked(self) -> None:
        """Only the two non-revalidated state files are shell-gated.

        Bundle contents are inert in this release and stay reachable from a
        shell; the file-edit gate still covers them (see the subtree test above).
        """
        assert is_sensitive_bash_command("echo x > ~/.kiro/crew/powers/s/POWER.md") is None

    def test_same_named_file_outside_powers_is_unaffected(self) -> None:
        """Entries are home-RELATIVE paths, so the anchor is exact."""
        assert is_sensitive_bash_command("echo x > ~/.kiro/crew/installed.json") is None

    def test_unrelated_crew_home_command_is_unaffected(self) -> None:
        assert is_sensitive_bash_command("cat ~/.kiro/crew/sessions.db") is None
