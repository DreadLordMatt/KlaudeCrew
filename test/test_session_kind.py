"""The canonical session-key classifier.

Locks in the taxonomy itself, and — more importantly — the specific
mislabellings that six independent prefix parsers used to produce by guessing
Slack whenever nothing else matched.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from kiro_crew.session_kind import (
    KIND_BACKGROUND,
    KIND_CHANNEL,
    KIND_CHANNEL_AGENT,
    KIND_CLI,
    KIND_CRON,
    KIND_DASHBOARD,
    KIND_HEARTBEAT,
    KIND_HOOK,
    KIND_HOST,
    KIND_SIDE,
    KIND_SUBAGENT,
    KIND_TASKRUNNER,
    KIND_UNKNOWN,
    KIND_WORKFLOW_POOL,
    classify,
    is_attended,
    source_of,
)


class TestKinds:
    @pytest.mark.parametrize(
        "key,kind",
        [
            ("dashboard:chat-7", KIND_DASHBOARD),
            ("dashboard_chat-7", KIND_DASHBOARD),
            ("chat-3-1785467660", KIND_DASHBOARD),
            ("cron:abc", KIND_CRON),
            ("cron_abc", KIND_CRON),
            ("subagent:x1", KIND_SUBAGENT),
            ("taskrunner:run_9", KIND_TASKRUNNER),
            ("taskrunner", KIND_TASKRUNNER),
            ("hook:h1", KIND_HOOK),
            ("side:chat-1", KIND_SIDE),
            ("channel:C123", KIND_CHANNEL_AGENT),
            ("wf-pool:r1", KIND_WORKFLOW_POOL),
            ("_bg", KIND_BACKGROUND),
            ("_hb", KIND_HEARTBEAT),
            ("_host", KIND_HOST),
            ("cli_chat", KIND_CLI),
            ("cli_chat:1", KIND_CLI),
        ],
    )
    def test_non_channel_kinds(self, key: str, kind: str) -> None:
        assert classify(key).kind == kind

    def test_empty_key_is_unknown_not_slack(self) -> None:
        """An empty key is the documented ungoverned opt-out, not a Slack session."""
        assert classify("").kind == KIND_UNKNOWN
        assert classify("").channel == ""

    def test_unrecognised_key_is_unknown_not_slack(self) -> None:
        """The bug this module exists to remove.

        Six parsers ended in a terminal ``else`` that returned Slack, so any key
        they did not recognise was recorded as having come from Slack.
        """
        assert classify("random_key").kind == KIND_UNKNOWN
        assert source_of("random_key") == "unknown"


class TestChannels:
    @pytest.mark.parametrize(
        "key,channel",
        [
            ("slack:1785457986.925389", "slack"),
            ("discord:kirocrew:direct:9:gen3", "discord"),
            ("telegram:kirocrew:forum:22:gen1", "telegram"),
            ("teams:kirocrew:direct:a@b.com", "teams"),
            ("webex:kirocrew:direct:a@b.com", "webex"),
            ("wecom:kirocrew:direct:u1", "wecom"),
            ("weixin:kirocrew:direct:u1", "weixin"),
            ("unified:kirocrew", "unified"),
        ],
    )
    def test_every_transport_is_identified(self, key: str, channel: str) -> None:
        """Each transport reports itself — none of them collapses to Slack."""
        kind = classify(key)
        assert kind.kind == KIND_CHANNEL
        assert kind.channel == channel
        assert kind.source == channel

    def test_filename_stem_form_is_recognised(self) -> None:
        """``history._safe_key`` folds ``:`` to ``_``, so both spellings arrive."""
        assert classify("slack_1785457986.925389").channel == "slack"
        assert classify("discord_kirocrew_direct_9").channel == "discord"

    @pytest.mark.parametrize(
        "key",
        [
            "1785457986.925389",  # bare thread ts, pre-namespace
            "C08HZAWV4TP:thread123",  # Slack channel-id composite
            "C123:456.789",
        ],
    )
    def test_legacy_slack_shapes_still_classify(self, key: str) -> None:
        """Old transcripts and the jump-to-source path still carry these."""
        kind = classify(key)
        assert kind.kind == KIND_CHANNEL
        assert kind.channel == "slack"

    def test_namespaced_key_wins_over_the_legacy_pattern(self) -> None:
        """A real ``slack:<ts>`` must not be re-parsed as a channel-id composite."""
        assert classify("slack:1785457986.925389").channel == "slack"


class TestAttended:
    @pytest.mark.parametrize(
        "key", ["dashboard:chat-1", "slack:1.1", "discord:a:direct:1", "cli_chat", "_host"]
    )
    def test_attended(self, key: str) -> None:
        assert is_attended(key) is True

    @pytest.mark.parametrize(
        "key", ["cron:a", "subagent:a", "taskrunner:a", "_bg", "_hb", "hook:a", "side:a"]
    )
    def test_unattended(self, key: str) -> None:
        assert is_attended(key) is False

    def test_unknown_counts_as_attended(self) -> None:
        """Treating an unrecognised key as unattended would turn the governance
        gate's documented no-op into deny-all for every ungoverned caller."""
        assert is_attended("") is True
        assert is_attended("random_key") is True


class TestConsumersAgree:
    """The adapters keep their own public vocabulary but one classification."""

    def test_sel_audit_source_reports_the_true_transport(self) -> None:
        from kiro_crew.sel import _infer_source

        assert _infer_source("discord:kirocrew:direct:9:gen1") == "discord"
        assert _infer_source("slack:1785.1") == "slack"
        assert _infer_source("random_key") == "unknown"

    def test_governance_binding_is_deliberately_unchanged(self) -> None:
        """Every transport still binds to the ``slack`` surface.

        The classifier reports the true transport, but the governance bind target
        is pinned: adopting it would move an existing Discord/Telegram/Teams
        session off a Slack-bound profile onto the policy ceiling alone, which
        RELAXES policy for anyone relying on that profile. Splitting it needs an
        owner and a migration note, so it is not a side effect of fixing labels.
        """
        from kiro_crew.platform.governance_profiles import _infer_surface

        assert _infer_surface("discord:kirocrew:direct:9:gen1") == "slack"
        assert _infer_surface("telegram:kirocrew:direct:9:gen1") == "slack"
        assert _infer_surface("random_key") == "slack"
        # The empty key keeps its ungoverned opt-out.
        assert _infer_surface("") == "unknown"
        # Non-channel kinds are untouched.
        assert _infer_surface("cron:job") == "cron"
        assert _infer_surface("dashboard:chat-1") == "dashboard"

    def test_use_case_keeps_its_persisted_spellings(self) -> None:
        """``Artifact.source`` and a metric attribute read these values."""
        from kiro_crew.validation import infer_use_case

        assert infer_use_case("taskrunner:run1") == "task-runner"
        assert infer_use_case("_bg") == "subagent"
        assert infer_use_case("1785457986.925389") == "slack"
        assert infer_use_case("discord:a:direct:1") == "discord"
        assert infer_use_case("") == "unknown"

    def test_every_use_case_value_is_an_allowed_artifact_source(self) -> None:
        """A source the classifier can emit must pass artifact validation."""
        from kiro_crew.artifacts import allowed_sources
        from kiro_crew.validation import infer_use_case

        for key in (
            "dashboard:chat-1",
            "slack:1.1",
            "discord:a:direct:1",
            "telegram:a:direct:1",
            "teams:a:direct:b@c.com",
            "webex:a:direct:b@c.com",
            "wecom:a:direct:u",
            "weixin:a:direct:u",
            "unified:a",
            "cron:j",
            "subagent:s",
            "taskrunner:r",
            "hook:h",
            "side:s",
            "channel:C1",
            "wf-pool:w",
            "cli_chat",
            "_bg",
            "_hb",
            "_host",
            "",
        ):
            assert infer_use_case(key) in allowed_sources(), key

    def test_mcp_session_type_is_one_implementation(self) -> None:
        """The claim and stub paths shared a hand-mirrored copy that guessed Slack."""
        from kiro_crew.mcp_gateway.claim import classify_session_type

        # The existing wire value for Slack is preserved verbatim.
        assert classify_session_type("slack:1785.1") == "slack-thread"
        assert classify_session_type("1785457986.925389") == "slack-thread"
        # Other transports are no longer reported as Slack threads.
        assert classify_session_type("discord:a:direct:1:gen1") == "discord"
        assert classify_session_type("telegram:a:direct:1:gen1") == "telegram"
        assert classify_session_type("cron:j") == "cron"
        assert classify_session_type("hook:h") == "hook"
        assert classify_session_type("dashboard:chat-1") == "dashboard"
        assert classify_session_type("") == "unknown"

    def test_runtime_display_name_covers_every_transport(self) -> None:
        from kiro_crew.context import _runtime_display_name

        assert _runtime_display_name("slack:1.1") == "Slack"
        assert _runtime_display_name("discord:a:direct:1") == "Discord"
        assert _runtime_display_name("teams:a:direct:b@c.com") == "Teams"
        assert _runtime_display_name("dashboard:chat-1") == "KiroCrew dashboard"


class TestNoImportCycle:
    """``session_kind`` must not create a startup import cycle.

    The first revision imported ``session_kind`` at ``artifacts`` module scope.
    That reached the transport registry through ``messaging``, whose package
    ``__init__`` imports ``acp.runtime`` -> ``validation`` -> back into a partially
    initialized ``artifacts``, so ``import kiro_crew.mcp_core`` raised ImportError
    and the MCP server could not start. The whole test suite stayed green because
    pytest imports these modules in a different order.

    Each module is imported in a COLD interpreter, which is the only way to catch
    an ordering cycle -- inside this process the modules are already loaded.
    """

    @pytest.mark.parametrize(
        "module",
        [
            "kiro_crew.mcp_core",
            "kiro_crew.artifacts",
            "kiro_crew.validation",
            "kiro_crew.sel",
            "kiro_crew.session_kind",
            "kiro_crew.context",
            "kiro_crew.security_posture",
            "kiro_crew.platform.governance_profiles",
        ],
    )
    def test_module_imports_standalone(self, module: str) -> None:
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, (
            f"cold import of {module} failed — likely an import cycle:\n{proc.stderr[-2000:]}"
        )

    def test_session_kind_is_a_leaf_module(self) -> None:
        """Importing it must not drag in the transport or ACP stack.

        This is what keeps it safe to import from module scope anywhere. If it
        starts pulling ``messaging`` in eagerly, the cycle above comes back.
        """
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import kiro_crew.session_kind; "
                "heavy=[m for m in ('kiro_crew.messaging','kiro_crew.acp') if m in sys.modules]; "
                "print(','.join(heavy))",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert proc.stdout.strip() == "", (
            f"session_kind eagerly imported: {proc.stdout.strip()} — keep it a leaf"
        )


class TestVocabulariesAreDerivedNotHandTyped:
    """Every consumer vocabulary is derived from the ONE transport registry.

    The first cut of this module hand-typed the transport list into three separate
    places and omitted ``whatsapp`` -- a registered transport. The classifier
    reported ``"whatsapp"``, the artifact allowlist rejected it, and every artifact
    save from a WhatsApp session began returning 400. These tests iterate the
    registry itself so the probe set cannot drift out of sync with it again.
    """

    def test_every_registered_transport_is_a_reportable_source(self) -> None:
        from kiro_crew.messaging.link import CHANNEL_SESSION_NAMESPACES
        from kiro_crew.session_kind import all_sources

        missing = set(CHANNEL_SESSION_NAMESPACES) - all_sources()
        assert not missing, f"transports registered but not reportable: {missing}"

    def test_governance_unattended_set_is_the_taxonomy_set(self) -> None:
        """Governance must not keep its own copy of "which kinds are unattended".

        It did, and the copy had already drifted narrower -- it omitted ``hook``,
        ``side``, ``channel-agent`` and ``wf-pool`` -- so the first consumer of
        ``is_attended`` would have disagreed with the governance containment check
        about the same session.
        """
        from kiro_crew.platform.governance_profiles import _UNATTENDED_SURFACES
        from kiro_crew.session_kind import UNATTENDED_KINDS

        assert _UNATTENDED_SURFACES is UNATTENDED_KINDS

    def test_widening_the_unattended_set_kept_the_deny_all_path_identical(self) -> None:
        """Deriving the set widened it; that must not deny anything new.

        The deny-all branch is reachable only on UNPROVEN identity, which is true
        solely for an empty key and the ``_bg``/``_hb`` sentinels. Every newly
        included kind always arrives on a non-empty key, so it proves identity and
        resolves to the no-profile path exactly as before.
        """
        from kiro_crew.platform.governance_profiles import resolve_active_scope
        from kiro_crew.session_kind import UNATTENDED_KINDS

        newly_included = UNATTENDED_KINDS - {
            "cron",
            "subagent",
            "background",
            "heartbeat",
            "taskrunner",
        }
        assert newly_included, "test is vacuous if the set did not widen"
        for kind in sorted(newly_included):
            assert resolve_active_scope(f"{kind}:conv", agent=None) is None

    def test_slack_permalink_shape_is_an_allowlist_not_a_prefix_denylist(self) -> None:
        """Only a real ``<channel-id>:<ts>`` pair may become a Slack permalink.

        The denylist this replaced had to name every non-Slack prefix, so any kind
        registered later would have started minting fabricated links again.
        """
        from kiro_crew.messaging.link import CHANNEL_SESSION_NAMESPACES
        from kiro_crew.session_kind import all_sources, is_slack_channel_composite

        assert is_slack_channel_composite("C08HZAWV4TP:1785457986.925389")
        assert is_slack_channel_composite("D01ABCDEF:1.2")
        # No namespaced session key carries a channel id, so none may build a link.
        for ns in sorted(set(CHANNEL_SESSION_NAMESPACES) | all_sources()):
            assert not is_slack_channel_composite(f"{ns}:conv"), ns
        # A bare thread ts has no channel half to split off.
        assert not is_slack_channel_composite("1785457986.925389")

    def test_every_source_is_a_stampable_audit_source(self) -> None:
        from kiro_crew.sel import audit_sources
        from kiro_crew.session_kind import all_sources

        missing = all_sources() - set(audit_sources())
        assert not missing, f"sources SEL cannot stamp: {missing}"

    def test_every_source_is_an_allowed_artifact_source(self) -> None:
        from kiro_crew.artifacts import allowed_sources
        from kiro_crew.session_kind import all_sources

        missing = all_sources() - allowed_sources()
        assert not missing, f"sources that would 400 an artifact save: {missing}"

    def test_every_audit_source_has_a_posture_gloss(self) -> None:
        from kiro_crew.security_posture import _AUDIT_SURFACE_DETAIL
        from kiro_crew.sel import audit_sources

        missing = set(audit_sources()) - set(_AUDIT_SURFACE_DETAIL)
        assert not missing, f"audited surfaces with no operator-facing gloss: {missing}"

    def test_every_registered_transport_saves_an_artifact(self) -> None:
        """The end-to-end shape of the WhatsApp regression, per transport."""
        from kiro_crew.artifacts import _validate_source
        from kiro_crew.messaging.link import CHANNEL_SESSION_NAMESPACES
        from kiro_crew.validation import infer_use_case

        for ns in sorted(CHANNEL_SESSION_NAMESPACES):
            _validate_source(infer_use_case(f"{ns}:some-conversation-id"))

    def test_every_registered_transport_has_a_runtime_display_name(self) -> None:
        from kiro_crew.context import _RUNTIME_DISPLAY
        from kiro_crew.messaging.link import CHANNEL_SESSION_NAMESPACES

        missing = set(CHANNEL_SESSION_NAMESPACES) - set(_RUNTIME_DISPLAY)
        assert not missing, f"transports the model would see as a raw key: {missing}"


class TestGovernanceBindingIsPinned:
    """``_infer_surface`` must be behaviour-identical to the pre-unification parser.

    Relabelling is correct for audit and display but NOT for policy: a profile is
    bound by surface name, so renaming a session's surface moves it off a
    Slack-bound profile onto the policy ceiling -- a relaxation. The first cut
    pinned only channel and unrecognised keys, leaving ``hook:``, ``side:``,
    ``channel:`` and ``wf-pool:`` free to escape.
    """

    @staticmethod
    def _pre_unification_surface(key: str) -> str:
        """Verbatim transcription of the parser this PR replaced."""
        if not key:
            return "unknown"
        if key == "_host":
            return "host"
        if key.startswith("dashboard:"):
            return "dashboard"
        if key.startswith("cron:"):
            return "cron"
        if key.startswith("subagent:"):
            return "subagent"
        if key.startswith("taskrunner"):
            return "taskrunner"
        if key == "_bg":
            return "background"
        if key == "_hb":
            return "heartbeat"
        if key == "cli_chat":
            return "cli"
        return "slack"

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "_host",
            "_bg",
            "_hb",
            "cli_chat",
            "cli_chat:x",
            "dashboard:chat-1",
            "dashboard_chat-1",
            "chat-7",
            "cron:abc",
            "cron_abc",
            "subagent:x",
            "subagent_x",
            "taskrunner",
            "taskrunner:1",
            # The four the first cut let escape to the policy ceiling.
            "hook:abc",
            "side:q",
            "channel:agent1",
            "wf-pool:7",
            "hook_abc",
            "side_q",
            # Legacy Slack shapes.
            "1785457986.925389",
            "C08HZAWV4TP:thread123",
            "random_key",
        ],
    )
    def test_matches_the_parser_it_replaced(self, key: str) -> None:
        from kiro_crew.platform.governance_profiles import _infer_surface

        assert _infer_surface(key) == self._pre_unification_surface(key)

    def test_every_registered_transport_still_binds_to_slack(self) -> None:
        from kiro_crew.messaging.link import CHANNEL_SESSION_NAMESPACES
        from kiro_crew.platform.governance_profiles import _infer_surface

        for ns in sorted(CHANNEL_SESSION_NAMESPACES):
            assert _infer_surface(f"{ns}:conv") == "slack"

    def test_an_unpinned_new_kind_defaults_to_slack_not_the_ceiling(self) -> None:
        """The allowlist shape is what makes this safe by default.

        A kind added to the classifier later must fall to the conservative pin
        rather than silently escaping onto the policy ceiling. Asserted through a
        source name that is deliberately absent from the allowlist.
        """
        from kiro_crew.platform.governance_profiles import (
            _PRE_UNIFICATION_SURFACES,
            _infer_surface,
        )

        assert "wf-pool" not in _PRE_UNIFICATION_SURFACES
        assert _infer_surface("wf-pool:new-kind") == "slack"
