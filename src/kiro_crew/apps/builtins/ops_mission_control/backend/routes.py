"""Ops Mission Control — backend routes.

Builtin-app contract: ``register_routes(app: web.Application) -> None`` registering
FULL paths directly on the gateway router (confirmed against the call site in
``dashboard/server.py``: ``_mod.register_routes(app)``, single argument). This is
NOT the external-app ``AppRoute``-list contract — mixing them up produces routes
that silently never dispatch.

Every handler is wrapped in ``_require_enabled``: builtin routes exist from gateway
startup even while the app is disabled, so a default-disabled opt-in app would
otherwise stay callable.

Secrets are **write-only** over this surface. ``PUT /providers/<id>/secret`` accepts
a token; nothing ever returns one. The read endpoints report only whether a field
is set.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import wraps
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import (
    companion,
    dispatch,
    handover,
    ledger,
    rotation,
    slack_out,
    slot_watch,
    store,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    MODE_ORDER,
    VALID_ACTIONS,
    LedgerEntry,
    Signal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    merge_provider_config,
    provider_config,
    set_top_level,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook as webhook_mod
from kiro_crew.apps.builtins.ops_mission_control.backend.registry import get_registry
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import (
    delete_secret,
    describe_secrets,
    put_secret,
)
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

APP_NAME = store.APP_NAME
_BASE = f"/api/apps/{APP_NAME}"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: Cap on a secret value. Real provider tokens are well under this; a larger body
#: is a misuse (or an attempt to bloat the keystone file) and is refused.
_MAX_SECRET_LEN = 512

#: Cap on an operator-supplied note attached to an action.
_MAX_NOTE_LEN = 4000

#: Cap on the shared-ledger git remote URL. An ssh/https remote is short; a longer
#: value is a paste accident, not a repo.
_MAX_REMOTE_LEN = 512

#: Branch names we will hand to ``git``. Deliberately narrow: letters, digits, and
#: ``._/-``, not starting with ``-`` (which would read as an option). The value is
#: already passed as its own argv entry, never interpolated into a shell string, so
#: this is about failing clearly rather than about injection.
_SAFE_BRANCH_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]{0,98}")


def _require_enabled(handler: Handler) -> Handler:
    """Deny every request while the app is disabled (deny-by-default).

    ``is_app_enabled`` is a synchronous ``installed.json`` read, so it runs off the
    event loop — same treatment as the other builtin apps' gates.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response({"error": f"{APP_NAME} is disabled"}, status=403)
        return await handler(request)

    return _wrapped


async def _json_body(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a 400, not a 500
        return None
    return body if isinstance(body, dict) else None


def _audit(op: str, target: str, outcome: str, *, error: str = "") -> None:
    sel().log_api_access(
        caller=f"core:{APP_NAME}",
        operation=op,
        outcome=outcome,
        resources=target,
        error=error,
    )


# ---------------------------------------------------------------------------
# Board / state
# ---------------------------------------------------------------------------


def _slot_state(request: web.Request, slot_key: str) -> dict[str, Any] | None:
    """Read an investigation slot's live state IN PROCESS.

    Read through the gateway's own ``DashboardState`` rather than by calling our
    own HTTP API: a handler that HTTP-calls its own server has to carry an auth
    token and can deadlock the loop under load. Returns ``None`` when the slot
    does not exist (yet) — which ``slot_watch.derive_status`` treats as "no
    evidence", never as blocked or as done.
    """
    if not slot_key:
        return None
    state = request.app.get("state")
    getter = getattr(state, "get_slot", None)
    if getter is None:
        return None
    try:
        slot = getter(slot_key)
    except Exception:  # noqa: BLE001 — a state read must never 500 the board
        logger.exception("ops-mission-control: slot lookup failed for %r", slot_key)
        return None
    if slot is None:
        return None
    return {
        "running": bool(getattr(slot, "running", False)),
        "pending_approval": bool(getattr(slot, "pending_approval", False))
        or any(not f.done() for f in getattr(slot, "_approval_futures", {}).values()),
        "waiting_for_input": bool(getattr(slot, "waiting_for_input", False)),
        "messages": [
            {"role": getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)}
            for m in (getattr(slot, "messages", None) or [])
        ],
    }


def _ledger_sync_status() -> dict[str, Any]:
    """Shared-ledger sync status, tolerating any failure.

    Deferred import for the same reason the hygiene handler defers it: ``ledger_sync``
    pulls in the git/sandbox machinery. Never raises — ``/state`` paints the whole
    board, so a probe of an optional feature must not be able to blank it.
    """
    try:
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        return ledger_sync.status()
    except Exception:  # noqa: BLE001 — an optional feature must not 500 the board
        logger.exception("ops-mission-control: ledger sync status failed")
        return {"enabled": False, "detail": "Sync status unavailable."}


async def _handle_state(request: web.Request) -> web.StreamResponse:
    """Everything the board needs in one call: incidents, sources, rotation, ledger.

    Reconciles each open incident against its investigation slot first, so an
    agent parked on a tool approval shows as ``needs_human`` rather than as
    still-progressing ``dispatched``. Done on read because that is the moment the
    answer is looked at — a stored flag would go stale the instant the operator
    approves from the embedded chat.
    """
    registry = get_registry()
    shift = await registry.resolve_shift()

    for inc in store.open_incidents():
        slot_key = inc.slot_key or f"{APP_NAME}-{inc.incident_id}"
        await asyncio.to_thread(
            slot_watch.reconcile, inc.incident_id, _slot_state(request, slot_key)
        )

    open_incidents = store.open_incidents()
    return web.json_response(
        {
            "incidents": [inc.to_dict() for inc in open_incidents],
            "counts": store.counts_by_status(),
            "blocked": slot_watch.blocked_summary(open_incidents),
            "providers": [_provider_dict(p) for p in registry.catalog()],
            "rotation": rotation.describe(shift),
            "ledger": ledger.stats(),
            # Shared-ledger git sync. ``ledger_sync.status()`` was written to be
            # "surfaced in Settings" — and then never returned by any route, so the
            # team memory-exchange repo was invisible as well as unsettable.
            "ledger_sync": _ledger_sync_status(),
            "slack": slack_out.status(_slack_client(request)),
            # What companion packages are INSTALLED. Reported separately from the
            # provider list because "no companion installed" and "companion
            # installed but rejected by admission" look identical in the provider
            # list and need completely different fixes.
            "companions": companion.companion_summary(),
            "webhook_queue": webhook_mod.queue_depth(),
        }
    )


async def _handle_handover(request: web.Request) -> web.StreamResponse:
    """Shift handover digest — a read-only projection, computed fresh.

    Reconciles open incidents against their live slots first, exactly as ``/state``
    does: the digest's most important section is "waiting on you", and that is derived
    from ``blocked_reason``, which is only true if it has just been reconciled.
    Returns both the structured digest and a rendered text form, so an agent can paste
    it into a handover thread without re-deriving the wording.
    """
    registry = get_registry()
    shift = await registry.resolve_shift()

    for inc in store.open_incidents():
        slot_key = inc.slot_key or f"{APP_NAME}-{inc.incident_id}"
        await asyncio.to_thread(
            slot_watch.reconcile, inc.incident_id, _slot_state(request, slot_key)
        )

    providers = [_provider_dict(p) for p in registry.catalog()]
    digest = await asyncio.to_thread(handover.build, providers, rotation.describe(shift))
    return web.json_response({**digest, "text": handover.render_text(digest)})


#: Incidents returned by ``/incidents`` in one response. The board shows recent work; a
#: responder scrolling to incident 900 is not a workflow this app has. Bounded because
#: the endpoint used to serialize the ENTIRE index — fine at 3 incidents, a growing
#: payload on every dashboard poll once a flapping alarm has minted hundreds.
MAX_INCIDENTS_RESPONSE = 200


async def _handle_incidents(request: web.Request) -> web.StreamResponse:
    status_filter = request.query.get("status", "").strip()
    index = store.read_index()
    matching = [
        inc
        for inc in sorted(index.values(), key=lambda i: i.claimed_at, reverse=True)
        if not status_filter or inc.status == status_filter
    ]
    items = [inc.to_dict() for inc in matching[:MAX_INCIDENTS_RESPONSE]]
    payload: dict[str, Any] = {"incidents": items}
    if len(matching) > len(items):
        # Say so rather than silently truncating: a board that shows 200 of 640 while
        # claiming to be the whole picture is how someone concludes an incident vanished.
        payload["truncated"] = True
        payload["total"] = len(matching)
    return web.json_response(payload)


def _slack_client(request: web.Request) -> Any | None:
    """The gateway's live Slack client, or None when Slack is not configured.

    Passed explicitly into slack_out rather than fetched from a global: KiroCrew
    has no global state accessor, and an explicit dependency is testable.
    """
    return slack_out.client_from_state(request.app.get("state"))


async def _handle_incident(request: web.Request) -> web.StreamResponse:
    incident_id = request.query.get("id", "").strip()
    incident = store.get_incident(incident_id) if incident_id else None
    if incident is None:
        return web.json_response({"error": "unknown incident"}, status=404)
    return web.json_response({"incident": incident.to_dict(), "log": store.read_log(incident_id)})


async def _handle_transition(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "request body must be a JSON object"}, status=400)
    incident_id = str(body.get("id", "")).strip()
    new_status = str(body.get("status", "")).strip()
    if not incident_id or not new_status:
        return web.json_response({"error": "id and status are required"}, status=400)

    updates: dict[str, Any] = {}
    for field_name in ("diagnosis", "resolution", "slot_key", "slack_thread_ts"):
        if field_name in body:
            updates[field_name] = str(body[field_name])
    try:
        incident = await asyncio.to_thread(store.transition, incident_id, new_status, **updates)
    except KeyError:
        return web.json_response({"error": "unknown incident"}, status=404)
    except ValueError as exc:
        # An illegal transition is a client error, not a server fault.
        return web.json_response({"error": str(exc)}, status=409)
    _audit("incident_transition", f"{incident_id}->{new_status}", "success")

    # Refresh the Slack pin board so its line tracks the new state, and put any
    # new diagnosis/resolution in the thread. Both are no-ops when Slack output is
    # off, and neither can fail the transition — the state change is already
    # durable at this point.
    client = _slack_client(request)
    await slack_out.publish(incident, client)
    detail = updates.get("resolution") or updates.get("diagnosis") or ""
    if detail:
        await slack_out.post_detail(incident, detail, client)

    return web.json_response({"incident": incident.to_dict()})


async def _handle_claim(request: web.Request) -> web.StreamResponse:
    """Manually claim a signal the operator picked off the board."""
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "request body must be a JSON object"}, status=400)
    raw_signal = body.get("signal")
    if not isinstance(raw_signal, dict):
        return web.json_response({"error": "signal object is required"}, status=400)
    signal = Signal.from_dict(raw_signal)
    if not signal.id:
        return web.json_response({"error": "signal must carry an id"}, status=400)

    mode = rotation.resolve_mode(signal)
    incident = await asyncio.to_thread(store.claim, signal, operating_mode=mode)
    if incident is None:
        return web.json_response({"error": "signal is already claimed"}, status=409)

    # Attach what the ledger already knows, exactly as the heartbeat does — a
    # manual claim from the board must not start colder than an automatic one.
    claimed = await asyncio.to_thread(dispatch.attach_ledger_matches, incident)
    # Broker the provider evidence too, for the same reason: the agent that picks this
    # up has no AWS credentials, so the gateway is the only thing that can read the
    # alarm history and logs it needs to diagnose. Non-fatal.
    claimed.evidence = await dispatch.gather_evidence_safely(get_registry(), signal)
    # Onto the pin board, exactly as the heartbeat does — a hand-claimed incident
    # must not be invisible to the channel watching the board.
    await slack_out.publish(claimed.incident, _slack_client(request))
    _audit("incident_claim", incident.incident_id, "success")
    return web.json_response({**claimed.to_dict(), "brief": dispatch.investigation_brief(claimed)})


async def _handle_dispatch(request: web.Request) -> web.StreamResponse:
    """Run one dispatch cycle: poll, claim, match the ledger, release stale work.

    This is what the dispatch cron calls. It returns ``changed: false`` when
    nothing happened, which is the cron's signal to stay completely silent.

    **Deliberately NOT shift-gated**, unlike ``authorize_action``. Audited after the
    off-shift write hole, since this has the same shape — a route reachable independently
    of the tier that pauses its cron. The difference is what it does: claiming a signal and
    reading evidence changes nothing in the operator's tooling, whereas
    ``rotation.authorize_action`` guards an actual provider write.

    Its two callers are the dispatch cron (paused off shift by the tier gate, so the
    automated path IS gated) and the dashboard's "Check now" button — a deliberate human
    action. Blocking the button off shift would stop an operator from proving a
    freshly-configured provider works, which is the one thing they most need right after
    setup; and claiming is idempotent across the team because ``store.claim`` is a
    compare-and-set, so a second instance finds nothing left to claim rather than
    duplicating work.

    The residual exposure is a duplicate *investigation session* if two instances both
    dispatch by hand at once. That is a wasted turn, not a production change — the same
    trade the claim design already accepts (see ``store.claim``).
    """
    result = await dispatch.run_cycle(slack_client=_slack_client(request))
    payload = result.to_dict()
    # Give the caller a ready-to-use brief per claim so the investigating agent
    # does not spend its first turn re-fetching context Python already has.
    payload["briefs"] = {
        c.incident.incident_id: dispatch.investigation_brief(c) for c in result.claimed
    }
    return web.json_response(payload)


async def _handle_action(request: web.Request) -> web.StreamResponse:
    """Execute (or refuse) a provider action for an incident.

    The autonomy gate runs BEFORE the sink is touched: a sink does not police its
    own authority. A refusal returns 403 with the reason, which is what the UI
    renders as "needs a rule to do this".
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "request body must be a JSON object"}, status=400)
    incident_id = str(body.get("id", "")).strip()
    action = str(body.get("action", "")).strip()
    sink_id = str(body.get("sink", "")).strip()
    note = str(body.get("note", ""))[:_MAX_NOTE_LEN]

    if action not in VALID_ACTIONS:
        return web.json_response(
            {"error": f"action must be one of {sorted(VALID_ACTIONS)}"}, status=400
        )
    incident = store.get_incident(incident_id) if incident_id else None
    if incident is None:
        return web.json_response({"error": "unknown incident"}, status=404)

    allowed, reason = rotation.authorize_action(incident.signal, action)
    if not allowed:
        return web.json_response({"error": reason, "authorized": False}, status=403)

    registry = get_registry()
    sink = registry.action_sink(sink_id) if sink_id else None
    if sink is None:
        # Default to the sink that owns this signal's provider, falling back to
        # observe-only so a proposal always has somewhere to land.
        sink = registry.action_sink(incident.signal.source) or registry.action_sink("noop")
    if sink is None:
        return web.json_response({"error": "no action sink available"}, status=503)

    result = await sink.execute(incident.signal, action, {"note": note})
    _audit(
        "incident_action",
        f"{incident_id} {action} via {sink.id}",
        "success" if result.ok else "failed",
        error=result.error,
    )
    return web.json_response(
        {
            "ok": result.ok,
            "action": result.action,
            "detail": result.detail,
            "error": result.error,
        },
        status=200 if result.ok else 502,
    )


# ---------------------------------------------------------------------------
# Signals / providers
# ---------------------------------------------------------------------------


async def _handle_signals(request: web.Request) -> web.StreamResponse:
    registry = get_registry()
    signals, errors = await registry.poll_all()
    claimed = {inc.signal.id for inc in store.read_index().values()}
    return web.json_response(
        {
            "signals": [s.to_dict() for s in signals],
            "unclaimed": [s.to_dict() for s in signals if s.id not in claimed],
            "errors": errors,
        }
    )


def _provider_dict(info: Any) -> dict[str, Any]:
    return {
        "id": info.id,
        "display_name": info.display_name,
        "roles": list(info.roles),
        "configured": info.configured,
        "config_fields": list(info.config_fields),
        "secret_fields": list(info.secret_fields),
        "detail": info.detail,
        # Non-secret config is safe to echo; secrets report set/unset only.
        "config": provider_config(info.id),
        "secrets": describe_secrets(info.id, tuple(info.secret_fields)),
    }


async def _handle_providers(request: web.Request) -> web.StreamResponse:
    return web.json_response({"providers": [_provider_dict(p) for p in get_registry().catalog()]})


async def _handle_put_provider_config(request: web.Request) -> web.StreamResponse:
    """Update one provider's NON-SECRET config (enable flag, region, ids, …).

    Two guards, both load-bearing because this file is served unauthenticated:

    1. Only keys the adapter declares in ``config_fields`` are accepted — an
       unknown key cannot become a place to stash data.
    2. Any key matching the adapter's ``secret_fields`` is REFUSED. A settings
       form that accidentally posted a token here would otherwise write it into a
       world-readable-over-the-port file; secrets must go to the keystone route.
    """
    provider_id = request.match_info.get("provider_id", "").strip()
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    known = {p.id: p for p in get_registry().catalog()}
    info = known.get(provider_id)
    if info is None:
        return web.json_response({"error": "unknown provider"}, status=404)

    allowed = set(info.config_fields)
    secret_names = set(info.secret_fields)
    updates: dict[str, Any] = {}
    for key, value in body.items():
        name = str(key)
        if name in secret_names:
            _audit(
                "provider_config_put",
                f"{provider_id}.{name}",
                "rejected",
                error="secret field submitted to the non-secret config route",
            )
            return web.json_response(
                {
                    "error": (
                        f"{name!r} is a secret field — use "
                        f"PUT /providers/{provider_id}/secret so it lands in the "
                        f"protected store, not the unauthenticated config file"
                    )
                },
                status=400,
            )
        if name not in allowed:
            return web.json_response(
                {"error": f"provider {provider_id!r} has no config field {name!r}"},
                status=400,
            )
        # Coerce to the JSON-safe scalars the adapters read.
        updates[name] = value if isinstance(value, (bool, int, float, list)) else str(value)

    if not updates:
        return web.json_response({"error": "no config fields supplied"}, status=400)

    saved = await asyncio.to_thread(merge_provider_config, provider_id, updates)
    _audit("provider_config_put", f"{provider_id}:{sorted(updates)}", "success")
    return web.json_response({"ok": True, "provider": provider_id, "config": saved})


async def _handle_put_settings(request: web.Request) -> web.StreamResponse:
    """Update app-level settings: autonomy mode, primary flag, cycle tuning.

    ``mode`` is the autonomy ceiling, so an unrecognized value is refused rather
    than silently falling back — a typo must not quietly change what the agent is
    allowed to do.
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    applied: dict[str, Any] = {}
    if "mode" in body:
        mode = str(body["mode"]).strip()
        if mode not in MODE_ORDER:
            return web.json_response(
                {"error": f"mode must be one of {sorted(MODE_ORDER)}"}, status=400
            )
        await asyncio.to_thread(set_top_level, "mode", mode)
        applied["mode"] = mode

    if "primary_instance" in body:
        flag = bool(body["primary_instance"])
        await asyncio.to_thread(set_top_level, "primary_instance", flag)
        applied["primary_instance"] = flag

    # Slack output. A channel ID is not a credential, so it belongs here rather
    # than in the secret store — and this app stores no Slack token at all, it
    # reuses KiroCrew's own client (see slack_out for why).
    if "slack_enabled" in body:
        flag = bool(body["slack_enabled"])
        await asyncio.to_thread(slack_out.set_settings, enabled=flag)
        applied["slack_enabled"] = flag

    if "slack_channel" in body:
        chan = str(body["slack_channel"]).strip()
        await asyncio.to_thread(slack_out.set_settings, channel_id=chan)
        applied["slack_channel"] = chan

    # Shared-ledger git sync: the team's memory-exchange repo. A remote URL and a
    # branch name are not credentials (auth is the operator's own git/ssh/gh
    # config), so they belong in plain app config like the Slack channel above.
    #
    # These were previously settable ONLY by hand-editing ``data/config.json``:
    # ``ledger_sync.set_settings`` existed and worked, but nothing outside the
    # tests ever called it, so the app's headline team feature had no way in. An
    # operator looking for "where do I point this at my team repo?" correctly
    # found nothing.
    if (
        "ledger_sync_remote" in body
        or "ledger_sync_branch" in body
        or "ledger_sync_enabled" in body
    ):
        # Deferred import, matching the hygiene handler below: ``ledger_sync`` pulls in
        # the git/sandbox machinery, and this module is imported at gateway start.
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        remote_url = (
            str(body["ledger_sync_remote"]).strip() if "ledger_sync_remote" in body else None
        )
        branch_name = (
            str(body["ledger_sync_branch"]).strip() if "ledger_sync_branch" in body else None
        )
        sync_enabled = bool(body["ledger_sync_enabled"]) if "ledger_sync_enabled" in body else None
        if remote_url is not None and len(remote_url) > _MAX_REMOTE_LEN:
            return web.json_response({"error": "ledger_sync_remote is too long"}, status=400)
        if branch_name and not _SAFE_BRANCH_RE.fullmatch(branch_name):
            # A branch name reaches a ``git`` argv. It is already passed as its own
            # argument and never interpolated into a shell string, so this is about
            # refusing option-like or whitespace-bearing values up front rather than
            # letting them surface later as a confusing sync failure.
            return web.json_response({"error": "ledger_sync_branch is not a valid ref"}, status=400)
        await asyncio.to_thread(
            ledger_sync.set_settings,
            enabled=sync_enabled,
            remote_url=remote_url,
            branch_name=branch_name,
        )
        for sync_key, sync_value in (
            ("ledger_sync_remote", remote_url),
            ("ledger_sync_branch", branch_name),
            ("ledger_sync_enabled", sync_enabled),
        ):
            if sync_value is not None:
                applied[sync_key] = sync_value

    for numeric_key in ("max_claims_per_cycle", "stale_after_secs"):
        if numeric_key not in body:
            continue
        try:
            value = int(body[numeric_key])
        except (TypeError, ValueError):
            return web.json_response({"error": f"{numeric_key} must be an integer"}, status=400)
        if value <= 0:
            return web.json_response({"error": f"{numeric_key} must be positive"}, status=400)
        await asyncio.to_thread(set_top_level, numeric_key, value)
        applied[numeric_key] = value

    if not applied:
        return web.json_response({"error": "no recognized settings supplied"}, status=400)
    _audit("settings_put", f"{sorted(applied)}", "success")
    return web.json_response({"ok": True, "applied": applied})


async def _handle_put_secret(request: web.Request) -> web.StreamResponse:
    """Store a provider secret. Write-only: the value is never readable back."""
    provider_id = request.match_info.get("provider_id", "").strip()
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "request body must be a JSON object"}, status=400)
    field_name = str(body.get("field", "")).strip()
    value = str(body.get("value", ""))
    if not provider_id or not field_name:
        return web.json_response({"error": "provider_id and field are required"}, status=400)
    if not value:
        return web.json_response({"error": "value must not be empty"}, status=400)
    if len(value) > _MAX_SECRET_LEN:
        return web.json_response({"error": "value is too long"}, status=400)

    known = {p.id: p for p in get_registry().catalog()}
    info = known.get(provider_id)
    if info is None:
        return web.json_response({"error": "unknown provider"}, status=404)
    if field_name not in info.secret_fields:
        # Reject unknown field names so the keystone file cannot be used as
        # arbitrary agent-inaccessible storage.
        return web.json_response(
            {"error": f"provider {provider_id!r} has no secret field {field_name!r}"},
            status=400,
        )

    await asyncio.to_thread(put_secret, provider_id, field_name, value)
    return web.json_response({"ok": True, "provider": provider_id, "field": field_name})


async def _handle_delete_secret(request: web.Request) -> web.StreamResponse:
    provider_id = request.match_info.get("provider_id", "").strip()
    if not provider_id:
        return web.json_response({"error": "provider_id is required"}, status=400)
    removed = await asyncio.to_thread(delete_secret, provider_id)
    return web.json_response({"ok": True, "removed": removed})


async def _handle_rotation(request: web.Request) -> web.StreamResponse:
    shift = await get_registry().resolve_shift()
    return web.json_response(rotation.describe(shift))


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


async def _handle_get_ledger(request: web.Request) -> web.StreamResponse:
    entries = await asyncio.to_thread(ledger.read_entries)
    entries.sort(key=lambda e: (-e.use_count, e.pattern))
    return web.json_response({"entries": [e.to_dict() for e in entries], "stats": ledger.stats()})


async def _handle_ledger_contradictions(request: web.Request) -> web.StreamResponse:
    """Entry pairs claiming different fixes for the same fingerprint.

    A read-only diagnostic for the hygiene SOP, which is told to "resolve contradictions"
    and previously had to find them by eye across the whole ledger. Detection is
    deterministic and cheap; the resolution (split the two patterns so each names its own
    cause) needs the model, so this endpoint deliberately changes nothing.
    """
    found = await asyncio.to_thread(ledger.find_contradictions)
    return web.json_response({"contradictions": found, "count": len(found)})


async def _handle_post_ledger(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    if body is None:
        return web.json_response({"error": "request body must be a JSON object"}, status=400)
    pattern = str(body.get("pattern", "")).strip()
    fix = str(body.get("fix", "")).strip()
    if not pattern or not fix:
        return web.json_response({"error": "pattern and fix are required"}, status=400)
    raw_fps = body.get("fingerprints")
    entry = LedgerEntry.create(
        pattern=pattern,
        fix=fix,
        fingerprints=[str(f) for f in raw_fps] if isinstance(raw_fps, list) else [],
        confidence=str(body.get("confidence", "medium")),
        trust=str(body.get("trust", "observed")),
        source=str(body.get("source", "human")),
    )
    stored = await asyncio.to_thread(ledger.upsert, entry)
    return web.json_response({"entry": stored.to_dict()})


async def _handle_ledger_hygiene(request: web.Request) -> web.StreamResponse:
    """Run the deterministic ledger maintenance pass: sync, hygiene, index.

    Called by the ledger-hygiene cron. Deterministic Python rather than an agent
    judgement call, so the mechanical part costs no tokens and the SOP's model
    time goes to the parts that need reasoning (contradictions, promotions).

    **Order is load-bearing:** pull → hygiene → index → push.

    - Pull FIRST so hygiene sees teammates' entries. Deduping before the merge would
      leave freshly-arrived duplicates to sit until tomorrow's pass.
    - Index AFTER hygiene so we do not embed rows hygiene is about to prune, and so a
      promoted ``observed → verified`` entry is indexed at its new importance.
    - Push LAST, carrying hygiene's result — otherwise every instance re-derives the
      same dedupe locally and the repo never converges.

    This is also where the two halves of the git-native memory loop finally get a
    caller. ``ledger_sync`` and ``ledger_index.import_pending`` were both built,
    tested, and **wired to nothing**: sync had no caller at all, and the semantic-recall
    search in ``dispatch`` was querying an index that nothing ever populated — so recall
    silently returned zero hits forever on a real install. A daily cadence is right for
    both: shared lessons are not latency-sensitive, and embedding is the expensive step.

    Every stage is independently fault-tolerant. A missing remote, an offline network, a
    conflicted ledger, or an absent embedding model each degrade to a reported
    sub-result; none prevents the local dedupe/decay/prune from running, because local
    hygiene is the part that always works and always matters.
    """
    from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

    pulled = await ledger_sync.sync_safely(direction="pull")
    summary = await asyncio.to_thread(ledger.hygiene)
    indexed = await asyncio.to_thread(_index_ledger_safely)
    # Retire old CLOSED incidents. Here rather than on the claim path because pruning is
    # maintenance: doing it in `claim` would make an ordinary claim occasionally pay for a
    # large rewrite. Open work is never pruned, whatever the age.
    incidents_pruned = await asyncio.to_thread(store.prune_closed)
    pushed = await ledger_sync.sync_safely(direction="push")

    changed = any(summary.get(k) for k in ("deduped", "decayed", "pruned"))
    if changed or indexed.get("written") or pulled or incidents_pruned:
        _audit(
            "ledger_hygiene",
            f"{summary} pull={pulled or 'skipped'} index={indexed}",
            "success",
        )
    return web.json_response(
        {
            "summary": summary,
            # Empty strings when sync is unconfigured, which is the common single-user
            # case — the UI shows nothing rather than a scary "not configured".
            "sync": {"pull": pulled, "push": pushed},
            "index": indexed,
            "incidents_pruned": incidents_pruned,
            # ``changed`` drives whether the cron speaks at all, so it must reflect
            # anything a human would want to hear about — including a pull that brought
            # in a teammate's lesson, which changes what the agent knows tomorrow.
            "changed": bool(changed or pulled or indexed.get("written") or incidents_pruned),
        }
    )


def _index_ledger_safely() -> dict[str, int]:
    """Project new ledger entries into the vector store. Never raises.

    Resolves the store here rather than holding one open: an install with no vector
    store (model still downloading, or a deliberately minimal setup) must complete
    hygiene exactly as before. Mirrors ``dispatch._attach_similar_safely``.
    """
    from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_index

    store_obj = None
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.vector_memory import VectorMemoryStore

        store_obj = VectorMemoryStore(embedding_dim=KiroCrewConfig.load().memory.embedding_dim)
        store_obj.init()
        return ledger_index.import_pending(store_obj)
    except Exception:  # noqa: BLE001 — no store, or a broken one, is a supported state
        logger.debug(
            "ops-mission-control: ledger indexing unavailable; hygiene still ran",
            exc_info=True,
        )
        return {"scanned": 0, "written": 0, "skipped": 0, "embedded": 0}
    finally:
        if store_obj is not None:
            try:
                store_obj.close()
            except Exception:  # noqa: BLE001
                logger.debug("ops-mission-control: vector store close failed", exc_info=True)


async def _handle_delete_ledger(request: web.Request) -> web.StreamResponse:
    entry_id = request.query.get("id", "").strip()
    if not entry_id:
        return web.json_response({"error": "id is required"}, status=400)
    removed = await asyncio.to_thread(ledger.remove, entry_id)
    return web.json_response({"ok": True, "removed": removed}, status=200 if removed else 404)


# ---------------------------------------------------------------------------
# Webhook ingress
# ---------------------------------------------------------------------------


#: Rejections that mean "I don't trust you" rather than "your body is wrong".
#: ``enqueue`` returns these before parsing anything, so they are the only ones that
#: are genuinely authentication/authorization failures.
_WEBHOOK_AUTH_REJECTIONS = frozenset(
    {
        "webhook source is not enabled",
        "no signing secret configured",
        "signature mismatch",
    }
)

#: Rejections that mean "you are trusted, but this body is wrong" — a 400. Listed
#: explicitly rather than inferred as "everything else" so an unclassified reason
#: falls through to 401 instead of being silently reported as a body fault.
_WEBHOOK_PAYLOAD_REJECTIONS = frozenset(
    {
        "malformed JSON",
        "payload must be a JSON object",
        "payload has no title",
    }
)


def _webhook_reject_status(detail: str) -> int:
    """Map a rejection reason to its HTTP status.

    Everything used to return 401, including "malformed JSON" and "payload has no
    title" — which are *authenticated* requests with a bad body. A sender debugging
    a payload was told "Unauthorized" and would go re-check credentials that were
    fine, while a real signature failure looked identical to a typo. Payload faults
    are 400; only the trust checks are 401. Defaults to 401 for an unrecognized
    reason, so a newly-added rejection is treated as auth-ish rather than
    accidentally advertised as "your request was fine".
    """
    if detail in _WEBHOOK_AUTH_REJECTIONS:
        return 401
    if detail == "body too large":
        return 413
    if detail in _WEBHOOK_PAYLOAD_REJECTIONS:
        return 400
    # Unrecognized: fail toward 401 rather than 400. A new rejection reason added to
    # ``enqueue`` without classifying it here is more likely to be a trust check than
    # a body complaint, and telling a caller "your request was fine, just malformed"
    # about a refusal we do not understand is the wrong default.
    return 401


async def _handle_webhook(request: web.Request) -> web.StreamResponse:
    """Accept a signed inbound signal.

    Fail-closed on the HMAC: an unsigned or mis-signed delivery is rejected, so
    enabling this adapter cannot open an unauthenticated path that manufactures
    work on the board. Note the check ORDER in ``webhook.enqueue`` — enabled →
    secret → size → signature → parse. Nothing unauthenticated is ever parsed, and
    an oversized body is refused before it is hashed.
    """
    raw = await request.read()
    signature = request.headers.get(webhook_mod.SIGNATURE_HEADER, "")
    accepted, detail = await asyncio.to_thread(webhook_mod.enqueue, raw, signature)
    _audit(
        "webhook_ingest",
        detail,
        "success" if accepted else "rejected",
        error="" if accepted else detail,
    )
    if not accepted:
        return web.json_response({"error": detail}, status=_webhook_reject_status(detail))
    return web.json_response({"ok": True, "signal": detail, "queued": webhook_mod.queue_depth()})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_routes(app: web.Application) -> None:
    """Register Ops Mission Control's routes on the gateway application."""
    add = app.router
    add.add_get(f"{_BASE}/state", _require_enabled(_handle_state))
    add.add_get(f"{_BASE}/incidents", _require_enabled(_handle_incidents))
    add.add_get(f"{_BASE}/incident", _require_enabled(_handle_incident))
    add.add_post(f"{_BASE}/incident/transition", _require_enabled(_handle_transition))
    add.add_post(f"{_BASE}/incident/claim", _require_enabled(_handle_claim))
    add.add_post(f"{_BASE}/incident/action", _require_enabled(_handle_action))
    add.add_post(f"{_BASE}/dispatch", _require_enabled(_handle_dispatch))
    add.add_get(f"{_BASE}/signals", _require_enabled(_handle_signals))
    add.add_get(f"{_BASE}/handover", _require_enabled(_handle_handover))
    add.add_get(f"{_BASE}/providers", _require_enabled(_handle_providers))
    add.add_put(
        f"{_BASE}/providers/{{provider_id}}/config",
        _require_enabled(_handle_put_provider_config),
    )
    add.add_put(f"{_BASE}/providers/{{provider_id}}/secret", _require_enabled(_handle_put_secret))
    add.add_delete(
        f"{_BASE}/providers/{{provider_id}}/secret", _require_enabled(_handle_delete_secret)
    )
    add.add_put(f"{_BASE}/settings", _require_enabled(_handle_put_settings))
    add.add_get(f"{_BASE}/rotation", _require_enabled(_handle_rotation))
    add.add_get(f"{_BASE}/ledger", _require_enabled(_handle_get_ledger))
    add.add_get(f"{_BASE}/ledger/contradictions", _require_enabled(_handle_ledger_contradictions))
    add.add_post(f"{_BASE}/ledger", _require_enabled(_handle_post_ledger))
    add.add_post(f"{_BASE}/ledger/hygiene", _require_enabled(_handle_ledger_hygiene))
    add.add_delete(f"{_BASE}/ledger", _require_enabled(_handle_delete_ledger))
    add.add_post(f"{_BASE}/webhook", _require_enabled(_handle_webhook))
    logger.info("ops-mission-control: routes registered")
