"""Session-key resolution + governance vetting for kirocrew-core tools.

Split out of ``mcp_core``. Imports only the ``identity`` leaf (parent-PID
walk); the platform governance-profile calls are function-local imports so
a late import failure can never hard-fail a stdio tool call."""

from __future__ import annotations

import os

from kiro_crew.config.loader import config_dir
from kiro_crew.mcp_core.identity import _get_ppid


def _resolve_session_key() -> str:
    """Return the real session key, falling back to PID file when env var is absent.

    Warm-pool kiro-cli processes have no KIROCREW_SESSION_KEY env var (the pool
    spawns with an empty key so rekey() + PID file provide the correct mapping).

    After rekey, the process tree may be: gateway -> kiro-cli (pool, has PID file)
    -> kiro-cli-chat (forked child) -> MCP server.  os.getppid() returns the
    immediate parent (kiro-cli-chat) which has no PID file.  Walk up ancestors
    until we find a matching file or hit init.
    """
    sk = os.environ.get("KIROCREW_SESSION_KEY", "")
    if sk:
        return sk
    try:
        cfg_dir = config_dir()
        # Sandbox launcher exports its own HOST pid (the pid the gateway keys
        # session_pid files by) — direct lookup works even when this
        # process's pid view diverges from the host's (PID-namespace
        # sandboxing), where the ancestor walk below can never match.
        host_pid = os.environ.get("KIROCREW_HOST_PID", "")
        if host_pid.isdigit():
            pid_file = cfg_dir / f"session_pid_{host_pid}.txt"
            if pid_file.exists():
                return pid_file.read_text(encoding="utf-8").strip()
        pid = os.getppid()
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            pid_file = cfg_dir / f"session_pid_{pid}.txt"
            if pid_file.exists():
                return pid_file.read_text(encoding="utf-8").strip()
            pid = _get_ppid(pid)
    except Exception:
        pass
    return ""


def _resolve_session_key_strict() -> str:
    """Resolve the session key, refusing PID-walked identities.

    Like ``_resolve_session_key`` but drops the ``/proc`` ancestor walk:
    only the gateway-injected ``KIROCREW_SESSION_KEY`` env var is accepted.
    Returns ``""`` when only the PID-walk would have matched.

    Required by state-mutating MCP tools. A subagent spawned via
    ``spawn_run`` lives under the parent slot's process tree, so a
    PID-walk from its MCP-core child silently resolves to the parent —
    which would let the subagent mutate state on the wrong slot.
    Read-only callers (audit, telemetry) keep the lenient resolver
    where misattribution is harmless.
    """
    return os.environ.get("KIROCREW_SESSION_KEY", "")


def _vet_messaging_governance(caller_session: str) -> str | None:
    """Return a denial reason if governance forbids outbound messaging, else None.

    Proactive/outbound messaging is a ``capabilities.messaging`` gate (an exfil
    surface a policy/profile may disable per surface/app).  Runs in the
    ``kirocrew-core`` stdio subprocess, which DOES boot the platform via
    ``cli.main`` — so ``current_context()`` carries the ceiling.  Best-effort:
    a ``PlatformCompositionError`` propagates; any other error returns None.
    Emits no stray stdout/stderr (either would corrupt the JSON-RPC stream); a
    fail-open degrade is audited via the file-backed ``governance_degraded`` SEL
    only (``log_warning=False`` suppresses the logger here).
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.messaging",
            "",
            session_key=caller_session,
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(
                caller_session, "send_message", "capabilities.messaging", decision
            )
            return "outbound messaging blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # No logger here: this runs inside the kirocrew-core stdio MCP server,
        # whose stray stdout/stderr would corrupt the JSON-RPC stream (same
        # constraint as redact_via_context). Still emit the file-backed
        # governance_degraded SEL (no stdout) so the fail-open is auditable.
        # Wrapped so a late-import failure cannot raise ImportError out of this
        # except-branch and hard-fail the stdio tool call (CR-284272012).
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "send_message",
                session_key=caller_session,
                scope="capabilities.messaging",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None


def _vet_channel_governance(caller_session: str, transport: str) -> str | None:
    """Return a denial reason if governance forbids messaging *via transport*.

    The ``channels`` scope (a ScopedMap) is the per-transport allowlist: which
    chat transports (``slack``, future ``discord``/``telegram``) outbound
    messaging may use.  It is finer-grained than the on/off
    ``capabilities.messaging`` gate above — a policy may permit messaging
    generally but restrict it to specific transports (e.g. Slack only).  We
    query the ScopedMap ``members`` allowlist for *transport*.  ``posture`` (the
    per-transport identity ceiling, policy-only) is enforced at the transport's
    own admission path, not here.  Same stdio-silent, fail-closed-CPP discipline
    as :func:`_vet_messaging_governance`.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # A bare member id queries the ScopedMap ``members`` ruleset.
        decision = governance_permits(
            "channels",
            transport,
            session_key=caller_session,
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(
                caller_session, f"send_message:{transport}", "channels", decision
            )
            return f"messaging via transport {transport!r} blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped: a late-import failure must not hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                f"send_message:{transport}",
                session_key=caller_session,
                scope="channels",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None


def _audit_governance_deny(session_key: str, tool_name: str, scope: str, decision: object) -> None:
    """Best-effort SEL audit of a governance denial (writes to the JSONL file,
    NOT stdout — safe in the stdio MCP server). Never raises."""
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=session_key,
            tool_name=tool_name,
            scope=scope,
            outcome="denied",
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=getattr(decision, "reason", ""),
        )
    except Exception:
        # No stdout/stderr in the stdio server; SEL writes to a file so this is
        # safe, but a failure here must never wedge the deny path.
        pass


def _governance_app() -> str:
    """Best-effort active app slug for per-app profile binding, or "".

    An app backend process carries ``KIROCREW_APP_NAME`` (set in
    ``apps.backend.start_app_backend``); when an app's own tool call reaches a
    governance chokepoint in-process, this lets a per-app profile
    (``bind:{type:"app"}``) resolve.  NOTE: the managed ``kirocrew-core`` MCP
    server is spawned by kiro-cli, NOT by an app backend, so this env var is
    absent there — a per-app profile is therefore only reachable for in-app tool
    calls today, not for the agent's MCP-routed ``learn_add``/``send_message``
    (those still resolve the per-SURFACE profile + policy ceiling, which is the
    enforced path).  Returns "" when not in an app context.
    """
    return os.environ.get("KIROCREW_APP_NAME", "")


def _vet_memory_writes_governance(caller_session: str) -> str | None:
    """Return a denial reason if governance forbids durable memory writes, else None.

    A durable memory/lesson write (``learn_add`` → persisted lesson) is an
    instruction-injection surface: content written here is re-injected into
    every future session's context.  The ``capabilities.memory_writes`` gate
    (default ON in the catalog) lets a policy/profile forbid it for a surface/app
    (e.g. a sandboxed app must not be able to plant a durable instruction).  Same
    stdio-silent, fail-closed-CPP discipline as :func:`_vet_messaging_governance`.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.memory_writes",
            "",
            session_key=caller_session,
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(
                caller_session, "learn_add", "capabilities.memory_writes", decision
            )
            return "durable memory writes blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped: a late-import failure must not hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "learn_add",
                session_key=caller_session,
                scope="capabilities.memory_writes",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None
