"""The one place that answers "what kind of session is this key?".

A session key currently encodes where the session was born — ``dashboard:chat-7``,
``slack:1785457986.925389``, ``cron:abc``, ``telegram:kirocrew:direct:9:gen3``. Four
independent parsers grew up reading that prefix, each with its own vocabulary and
each ending in a terminal ``else`` that guessed **Slack**:

* ``sel._infer_source`` — the SEL audit log's ``source`` field
* ``context._runtime_display_name`` — the runtime name shown to the model
* ``validation.infer_use_case`` — persisted provenance (``Artifact.source``, metrics)
* ``sync_bridge`` — the session list's source badge

plus two more copies of a narrower variant in ``mcp_gateway.claim`` and
``mcp_gateway.stub``. Six parsers, one taxonomy, and every one of them labelled a
Discord, Telegram, Teams, Webex, WeCom or Weixin session as *Slack* — because Slack
was the fallback rather than a match.

This module owns the taxonomy. The callers above keep their own public vocabulary
(``Artifact.source`` is persisted data and the SEL ``source`` field is queried, so
neither can be renamed) but they now all derive it from one classification instead
of six prefix chains.

Deliberately NOT in scope here: which *surface* a session's governance profile binds
to. That reads the same taxonomy but changing it changes which policy applies, so
``governance_profiles._infer_surface`` pins its current behaviour explicitly rather
than inheriting a relabelling by accident — see the note there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


@lru_cache(maxsize=1)
def _link() -> object:
    """The transport registry module, imported on first use.

    ``messaging.link`` lives under a package whose ``__init__`` pulls in the
    transport driver and ``acp``, so importing it at module scope would make this
    low-level taxonomy module drag the whole stack in -- and several of those
    modules import back into callers of this one. Keeping this module a LEAF means
    anything can ``import session_kind`` at module scope safely.
    """
    from kiro_crew.messaging import link

    return link


def _is_channel_session_key(session_key: str) -> bool:
    return bool(_link().is_channel_session_key(session_key))  # type: ignore[attr-defined]


def _channel_namespace_of(session_key: str) -> str:
    return str(_link().channel_namespace_of(session_key))  # type: ignore[attr-defined]


#: Legacy Slack session keys that predate the ``slack:`` namespace: a bare thread
#: timestamp, or a Slack channel-id composite. Matched only after the namespaced
#: forms so a real ``slack:<ts>`` never reaches here.
_LEGACY_SLACK_TS_RE = re.compile(r"^\d+\.\d+$")
_SLACK_COMPOSITE_RE = re.compile(r"^[CDG][A-Z0-9]+:.+$")

#: A conversation a human is present for. Everything else runs unattended.
KIND_DASHBOARD = "dashboard"
KIND_CHANNEL = "channel"
KIND_CLI = "cli"

#: Unattended / machine-driven origins.
KIND_CRON = "cron"
KIND_SUBAGENT = "subagent"
KIND_TASKRUNNER = "taskrunner"
KIND_BACKGROUND = "background"
KIND_HEARTBEAT = "heartbeat"
KIND_HOOK = "hook"
KIND_SIDE = "side"
KIND_CHANNEL_AGENT = "channel-agent"
KIND_WORKFLOW_POOL = "wf-pool"
KIND_HOST = "host"
KIND_UNKNOWN = "unknown"

#: Kinds with no human on the other end. Mirrors the intent of
#: ``governance_profiles._UNATTENDED_SURFACES``, stated once here.
UNATTENDED_KINDS = frozenset(
    {
        KIND_CRON,
        KIND_SUBAGENT,
        KIND_TASKRUNNER,
        KIND_BACKGROUND,
        KIND_HEARTBEAT,
        KIND_HOOK,
        KIND_SIDE,
        KIND_CHANNEL_AGENT,
        KIND_WORKFLOW_POOL,
    }
)


@lru_cache(maxsize=1)
def channel_sources() -> frozenset[str]:
    """Every transport that can appear as a ``source``.

    Read from the ONE registry in ``messaging.link`` rather than restated, so
    adding a transport there flows to every consumer automatically.
    """
    return frozenset(_link().CHANNEL_SESSION_NAMESPACES)  # type: ignore[attr-defined]


@lru_cache(maxsize=1)
def all_sources() -> frozenset[str]:
    """Every value ``SessionKind.source`` can return.

    Consumers that must keep their own vocabulary -- a persisted enum, an audit
    field, a display map -- derive it from this instead of hand-typing transports.
    A hand-typed list is how ``whatsapp`` was omitted on the first cut of this
    module: it is registered in ``messaging.link`` but was missing from three
    separate lists, which turned a working artifact save into a 400.

    ``KIND_CHANNEL`` is deliberately absent -- ``source`` never returns it, it
    collapses to the transport name.
    """
    return channel_sources() | {
        KIND_DASHBOARD,
        KIND_CLI,
        KIND_CRON,
        KIND_SUBAGENT,
        KIND_TASKRUNNER,
        KIND_BACKGROUND,
        KIND_HEARTBEAT,
        KIND_HOOK,
        KIND_SIDE,
        KIND_CHANNEL_AGENT,
        KIND_WORKFLOW_POOL,
        KIND_HOST,
        KIND_UNKNOWN,
    }


#: Literal sentinel keys, matched before any prefix.
_SENTINELS: dict[str, str] = {
    "_host": KIND_HOST,
    "_bg": KIND_BACKGROUND,
    "_hb": KIND_HEARTBEAT,
    "cli_chat": KIND_CLI,
}

#: ``prefix -> kind``. Both separators are listed because a session key that has
#: round-tripped through a filename carries ``_`` where it started with ``:``
#: (``history._safe_key`` folds every non-word character).
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("dashboard:", KIND_DASHBOARD),
    ("dashboard_", KIND_DASHBOARD),
    ("chat-", KIND_DASHBOARD),
    ("cron:", KIND_CRON),
    ("cron_", KIND_CRON),
    ("subagent:", KIND_SUBAGENT),
    ("subagent_", KIND_SUBAGENT),
    # Separator-less on purpose: the previous classifier matched
    # ``startswith("taskrunner")``, so a bare "taskrunner" key must keep
    # classifying rather than falling through to unknown.
    ("taskrunner", KIND_TASKRUNNER),
    ("hook:", KIND_HOOK),
    ("hook_", KIND_HOOK),
    ("side:", KIND_SIDE),
    ("side_", KIND_SIDE),
    ("channel:", KIND_CHANNEL_AGENT),
    ("wf-pool:", KIND_WORKFLOW_POOL),
    ("cli_chat:", KIND_CLI),
)


@dataclass(frozen=True)
class SessionKind:
    """What a session key says about its own origin.

    ``kind`` is one of the ``KIND_*`` constants. ``channel`` is the transport
    namespace (``"slack"``, ``"discord"``, …) when ``kind == KIND_CHANNEL``, and
    ``""`` otherwise — so a caller that needs the transport reads it directly
    instead of re-parsing the key.
    """

    kind: str
    channel: str = ""

    @property
    def attended(self) -> bool:
        """True when a human is on some surface of this session.

        ``KIND_UNKNOWN`` counts as attended: an empty or unrecognised key is the
        documented opt-out for the governance gate, and treating it as unattended
        would turn every ungoverned caller into a deny-all.
        """
        return self.kind not in UNATTENDED_KINDS

    @property
    def source(self) -> str:
        """The flat label callers persist or display.

        Collapses ``KIND_CHANNEL`` to its transport, so a Slack session reads
        ``"slack"`` and a Discord session reads ``"discord"`` — rather than both
        reading ``"slack"`` because Slack was the fallback.
        """
        if self.kind == KIND_CHANNEL and self.channel:
            return self.channel
        return self.kind


def classify(session_key: str) -> SessionKind:
    """Classify *session_key*. Never raises; unrecognised keys are ``KIND_UNKNOWN``.

    Order matters: sentinels, then channel namespaces, then prefixes. Channels are
    checked before the prefix table because a channel key's namespace is data
    (``messaging.link.CHANNEL_SESSION_NAMESPACES``) rather than a literal here —
    keeping one registry instead of a second list that drifts from it.
    """
    if not session_key:
        return SessionKind(KIND_UNKNOWN)
    sentinel = _SENTINELS.get(session_key)
    if sentinel is not None:
        return SessionKind(sentinel)
    if _is_channel_session_key(session_key):
        return SessionKind(KIND_CHANNEL, _channel_namespace_of(session_key))
    if _LEGACY_SLACK_TS_RE.match(session_key) or _SLACK_COMPOSITE_RE.match(session_key):
        # Pre-namespace Slack shapes, still present in old transcripts and in the
        # notification jump-to-source path: a bare thread ts ("1785457986.925389")
        # and a channel-id composite ("C08HZAWV4TP:thread123").
        return SessionKind(KIND_CHANNEL, "slack")
    for prefix, kind in _PREFIXES:
        if session_key.startswith(prefix):
            return SessionKind(kind)
    return SessionKind(KIND_UNKNOWN)


def source_of(session_key: str) -> str:
    """Shorthand for ``classify(session_key).source``."""
    return classify(session_key).source


def is_attended(session_key: str) -> bool:
    """Shorthand for ``classify(session_key).attended``."""
    return classify(session_key).attended


def is_slack_channel_composite(session_key: str) -> bool:
    """True for the ONE key shape that carries a real Slack ``<channel-id>:<ts>``.

    Callers that want to reconstruct a Slack URL need both halves of that pair.
    Every other namespaced key -- ``slack:<ts>`` (no channel), ``discord:<id>``,
    ``cron:``, ``hook:``, ``wf-pool:`` -- has a colon but no channel id, so
    splitting it on the colon fabricates a link to a conversation that does not
    exist.

    Deliberately an ALLOWLIST keyed on the channel-id shape rather than a list of
    prefixes to skip: a denylist has to be extended every time a session kind is
    registered, and the one it replaced had already fallen behind (it let
    unrecognised uppercase keys through). A new kind is now excluded by default.
    """
    return bool(_SLACK_COMPOSITE_RE.match(session_key))
