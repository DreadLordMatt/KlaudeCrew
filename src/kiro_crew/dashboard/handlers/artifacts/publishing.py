"""Artifact publishing / sharing / sync handlers + the publish-governance gate.

Holds the Artifactory-style publish/unpublish/sharing/pull/overwrite handlers, the
provider-negotiation endpoint, and ``_publish_governance_denied`` — the Plane-C
``capabilities.publish`` chokepoint imported (lazily by :mod:`.core`, directly by
:mod:`.comments`/:mod:`.remote`) wherever bytes egress to a provider.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aiohttp import web

from kiro_crew import publish_sync
from kiro_crew.artifacts import (
    ArtifactNotFoundError,
    ArtifactValidationError,
    get_default_store,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.publish_provider import (
    KindSupport,
    NotPublishedError,
    PublishConflictError,
    PublishError,
    PublishUnavailableError,
    list_providers,
)

from .core import (
    _audit,
    _err,
    _json_response,
    _notify_artifact_update,
    _read_json_body,
    _run_off_loop,
    _session_key,
    sel,
)
from .redaction import (
    _SERIALIZE_REDACTED_KEYS,
    _redact_remote_response,
    _redact_text,
    _serialize,
)

logger = logging.getLogger(__name__)


# Publish-provider name grammar. Upstream imports this from ``validation`` where
# it also backs the MCP publish-tool FieldSpecs; the public fork's validation
# module doesn't carry those tools, so the constraint lives here at the sole
# HTTP boundary that accepts a provider name.
_ARTIFACT_PROVIDER_RE = re.compile(r"^[a-z0-9-]{1,32}$")


# ── Publishing / sharing (Artifactory — Mesh-1880) ───────────────────────────

_VALID_VISIBILITY = ("PRIVATE", "SHARED", "PUBLIC")


def _validate_sharing_body(body: dict[str, Any]) -> tuple[str, list[str]]:
    """Extract and validate (visibility, shared_with) from a request body.

    Raises ``ArtifactValidationError`` (→ 400) on any problem.
    """
    visibility = body.get("visibility") or "PRIVATE"
    if visibility not in _VALID_VISIBILITY:
        raise ArtifactValidationError("visibility must be PRIVATE, SHARED, or PUBLIC")
    shared_with = body.get("shared_with") or []
    if not isinstance(shared_with, list) or not all(isinstance(a, str) for a in shared_with):
        raise ArtifactValidationError("shared_with must be a list of alias strings")
    if visibility == "SHARED" and not shared_with:
        raise ArtifactValidationError(
            "SHARED visibility requires at least one alias in shared_with"
        )
    return visibility, shared_with


def _sync_error_response(
    tool: str, request: web.Request, slug: str, exc: Exception
) -> web.Response:
    """Map an Artifactory sync exception to an audited HTTP error response."""
    if isinstance(exc, ArtifactNotFoundError):
        status, outcome = 404, "error"
    elif isinstance(exc, ArtifactValidationError):
        status, outcome = 400, "denied"
    elif isinstance(exc, PublishUnavailableError):
        status, outcome = 503, "error"
    elif isinstance(exc, PublishConflictError):
        status, outcome = 409, "error"
    elif isinstance(exc, NotPublishedError):
        status, outcome = 409, "denied"
    elif isinstance(exc, PublishError):
        status, outcome = 502, "error"
    else:
        status, outcome = 500, "error"
    # The exception text can originate from untrusted Artifactory MCP responses
    # — redact credentials / exfiltration URLs before it reaches the dashboard
    # AND the SEL audit log (AUTOSDE security-controls).
    safe_msg = _redact_text(str(exc))
    _audit(tool=tool, request=request, outcome=outcome, error=safe_msg, extra={"slug": slug})
    return _err(safe_msg, status=status)


def _publish_governance_denied(request: web.Request, provider_name: str) -> str | None:
    """Plane-C governance chokepoint for artifact publishing.

    Publishing is a user-driven dashboard HTTP action ("NOT LLM tools"), so the
    host PreToolUse gate never sees it — this is where the ``capabilities.publish``
    ceiling is enforced. Returns a denial reason (caller → 403) or ``None`` to
    permit. Enforces, tightest-wins:
      1. governance ceiling ∩ profile — ``capabilities.publish`` gate AND its
         inner ``destinations`` ruleset (item ``destinations:<provider>``);
      2. the standalone operator's ``config.publish.allowed_destinations``
         allowlist (default-open, narrow-only — cannot widen past the ceiling).
    A ``PlatformCompositionError`` propagates (fail-closed CPP); any other
    governance error fails CLOSED (DENY) — publishing is an authorization
    decision (bytes leave the box), so unlike the messaging/cron chokepoints it
    must NOT degrade-to-permit. The DENY is produced inside ``governance_permits``
    (``fail_closed=True``), because that helper swallows its own internal errors —
    the handler-level ``except`` here only catches errors raised OUTSIDE it.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    session_key = _session_key(request)
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.publish",
            f"destinations:{provider_name}",
            session_key=session_key,
            # Authorization chokepoint: a governance-evaluation error must DENY
            # (bytes leave the box). governance_permits swallows its own internal
            # errors, so the fail-closed DENY has to be produced INSIDE it — the
            # handler-level ``except`` below only ever sees errors raised outside
            # governance_permits (e.g. the audit call).
            fail_closed=True,
        )
        # Default to DENY (permitted=False) if the Decision is malformed: this is
        # an exfil authorization chokepoint documented as "must NOT
        # degrade-to-permit", so a missing/odd attr must fail closed, not open.
        if not getattr(decision, "permitted", False):
            try:
                sel().log_governance_decision(
                    session_key=session_key,
                    tool_name=f"artifact_publish:{provider_name}",
                    scope="capabilities.publish",
                    item=f"destinations:{provider_name}",
                    outcome="denied",
                    rule=getattr(decision, "rule", ""),
                    layer=getattr(decision, "layer", ""),
                    reason=getattr(decision, "reason", ""),
                )
            except Exception:
                logger.debug("publish governance deny audit failed", exc_info=True)
            return getattr(decision, "reason", "publishing not permitted by policy")
    except PlatformCompositionError:
        raise
    except Exception:
        # Fail CLOSED: publishing is an authorization decision (bytes leave the
        # box to an external destination), so an unexpected error must DENY
        # rather than degrade-to-permit. governance_permits(fail_closed=True)
        # already denies on ITS own internal errors; this branch is the belt-and-
        # suspenders catch for anything raised OUTSIDE it (e.g. the deny-audit
        # call above), keeping the whole helper deny-on-error.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "artifact_publish", session_key=session_key, scope="capabilities.publish"
            )
        except Exception:
            logger.debug("publish governance degrade audit unavailable", exc_info=True)
        return "publishing denied: governance could not be evaluated"

    # Config allowlist (default-open, narrow-only). Empty list allows any
    # registered destination; a non-empty list restricts to those provider ids.
    # A config-read failure also fails CLOSED for the same reason as above.
    try:
        allowed = KiroCrewConfig.load().publish.allowed_destinations
    except Exception:
        logger.debug("publish config load failed; failing closed", exc_info=True)
        return "publishing denied: publish config could not be loaded"
    if allowed and provider_name not in allowed:
        return f"publish destination {provider_name!r} is not in the operator allowlist"
    return None


async def api_artifact_publish(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/publish — publish (or re-publish) to a
    registered publish destination.

    Body: ``{visibility, shared_with[]}``. Returns the full serialized artifact
    (now carrying the ``publication`` block). A side-panel file that isn't yet
    an artifact is auto-saved first by the frontend (POST /api/artifacts), so
    this endpoint is always slug-based.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_publish",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot publish artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
        visibility, shared_with = _validate_sharing_body(body)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    # Provider is validated generically (any registered provider); the share
    # picker only offers providers whose kind_support() != UNSUPPORTED.
    requested_provider = body.get("provider") if isinstance(body, dict) else None
    provider_name = requested_provider or "artifactory"
    if not isinstance(provider_name, str) or not _ARTIFACT_PROVIDER_RE.match(provider_name):
        return _err("provider must match ^[a-z0-9-]{1,32}$")
    # Resolve the EFFECTIVE destination BEFORE the governance gate. For an
    # already-published artifact, publish_sync.publish() ignores provider_name
    # and re-pushes to publication.provider — so the gate must evaluate THAT
    # provider, not the (default) requested one, or a re-publish with no explicit
    # provider would gate on "artifactory" and permit bytes to a DENIED existing
    # destination. Mirrors api_artifact_update_sharing (which gates on the
    # existing publication's provider).
    try:
        # ≤25 MiB store read — offload off the event loop.
        existing_pub = (await _run_off_loop(lambda: get_default_store().get(slug))).publication
    except ArtifactNotFoundError:
        existing_pub = None
    # nrb review #19: reject an explicit provider switch on an already-published
    # artifact rather than silently ignoring it. publish() reuses the existing
    # publication's provider, so honoring a switch here would leave the original
    # remote orphaned — require an explicit unpublish first.
    if (
        requested_provider
        and existing_pub is not None
        and existing_pub.provider
        and requested_provider != existing_pub.provider
    ):
        return _err(
            f"artifact is already published to {existing_pub.provider!r}; "
            f"unpublish it before publishing to {requested_provider!r}",
            status=409,
        )
    # Effective provider: the existing publication's (re-publish dispatches to it)
    # else the requested/default. This is the destination bytes actually go to.
    effective_provider = (
        existing_pub.provider if existing_pub and existing_pub.provider else provider_name
    )
    # Governance chokepoint (Plane-C): the capabilities.publish ceiling + the
    # operator destination allowlist gate publishing here — the host PreToolUse
    # gate never sees this HTTP action. Runs BEFORE any provider dispatch.
    gov_denial = _publish_governance_denied(request, effective_provider)
    if gov_denial is not None:
        _audit(
            tool="artifact_publish",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"slug": slug, "provider": effective_provider},
        )
        return _err(gov_denial, status=403)
    is_mcp = request.headers.get("X-Internal-Secret") is not None
    actor = "agent" if is_mcp else "user"
    try:
        await publish_sync.publish(
            slug,
            visibility=visibility,
            shared_with=shared_with,
            actor=actor,
            provider_name=provider_name,
        )
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except Exception as exc:
        return _sync_error_response("artifact_publish", request, slug, exc)
    _audit(
        tool="artifact_publish",
        request=request,
        outcome="success",
        extra={"slug": slug, "visibility": visibility, "provider": effective_provider},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_update_sharing(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/sharing — change visibility / shared-with.

    Body: ``{visibility, shared_with[]}``. No re-upload. Returns the serialized
    artifact with the updated publication block.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_update_sharing",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot change artifact sharing", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
        visibility, shared_with = _validate_sharing_body(body)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    # Changing sharing (e.g. PRIVATE -> PUBLIC) is an outbound-publish mutation,
    # so it MUST pass the same capabilities.publish governance gate as the
    # initial publish — otherwise an already-published artifact could be widened
    # to public after policy revocation. Gate on the existing publication's
    # provider (default provider when the block hasn't loaded).
    try:
        existing_pub = (await _run_off_loop(lambda: get_default_store().get(slug))).publication
    except ArtifactNotFoundError:
        existing_pub = None
    share_provider = (
        existing_pub.provider if existing_pub and existing_pub.provider else "artifactory"
    )
    gov_denial = _publish_governance_denied(request, share_provider)
    if gov_denial is not None:
        _audit(
            tool="artifact_update_sharing",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"slug": slug, "provider": share_provider},
        )
        return _err(gov_denial, status=403)
    try:
        await publish_sync.update_sharing(slug, visibility=visibility, shared_with=shared_with)
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except Exception as exc:
        return _sync_error_response("artifact_update_sharing", request, slug, exc)
    _audit(
        tool="artifact_update_sharing",
        request=request,
        outcome="success",
        extra={"slug": slug, "visibility": visibility},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_unpublish(request: web.Request) -> web.Response:
    """DELETE /api/artifacts/{slug}/publish — remove from Artifactory.

    Deletes the Artifactory artifact (best-effort) and clears the local
    publication block. Returns the serialized artifact (now with
    ``publication: null``).
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_unpublish",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot unpublish artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        await publish_sync.unpublish(slug)
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except Exception as exc:
        return _sync_error_response("artifact_unpublish", request, slug, exc)
    _audit(
        tool="artifact_unpublish",
        request=request,
        outcome="success",
        extra={"slug": slug},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_refresh_sharing(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/publish/refresh — reconcile local sharing
    state with the live destination.

    Pulls the destination's current visibility / shared-with (e.g. after the
    user changed them directly in the Artifactory UI) and updates the stored
    publication so the dashboard reflects truth. Gated like other mutations
    since it can update meta.json.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_refresh_sharing",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot refresh artifact sharing", status=403)
    slug = request.match_info.get("slug", "")
    try:
        await publish_sync.refresh_publication(slug)
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_refresh_sharing",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_refresh_sharing",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover — refresh is best-effort
        return _sync_error_response("artifact_refresh_sharing", request, slug, exc)
    _audit(
        tool="artifact_refresh_sharing",
        request=request,
        outcome="success",
        extra={"slug": slug},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_pull_latest(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/pull-latest — pull upstream into a fork."""

    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_pull_latest",
            request=request,
            outcome="denied",
            error="restricted session",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot pull latest", status=403)

    slug = request.match_info.get("slug", "")
    store = get_default_store()
    try:
        art = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    if art.publication is None and art.fork_metadata is None:
        return _err("artifact does not track an upstream", status=400)

    # Delegate to the unified pull engine — works for a fork's origin lineage
    # AND my own publication (a collaborator edited my cloud copy). ``source``
    # (publication|origin|auto) selects which tracked upstream to pull. The
    # engine pulls into a NEW local snapshot, never auto-republishes, and
    # surfaces a conflict (never clobbers) for an owned copy with unsynced
    # local edits. Read-only ingress — no publish governance gate (no bytes
    # leave the box).
    source = request.rel_url.query.get("source", "auto")
    try:
        # ``pull_upstream`` is best-effort for provider/network failures (it
        # returns a result dict, never raises for those), so the realistic
        # raises here are store/registry errors: a concurrent delete during the
        # remote-fetch window (ArtifactNotFoundError → 404) or an unregistered
        # provider (PublishUnavailableError → 503). Map those like the other
        # sync handlers instead of collapsing every error into a 502.
        result = await publish_sync.pull_upstream(slug, source=source)
        art = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _sync_error_response("artifact_pull_latest", request, slug, exc)
    except PublishUnavailableError as exc:
        return _sync_error_response("artifact_pull_latest", request, slug, exc)
    except Exception as exc:  # pragma: no cover — pull is best-effort
        _audit(
            tool="artifact_pull_latest",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(_redact_text(str(exc)), status=502)

    _audit(
        tool="artifact_pull_latest",
        request=request,
        outcome="success",
        extra={"slug": slug, "pulled": bool(result.get("pulled"))},
    )
    # A pull lands upstream content as a new local snapshot (Mesh-2772).
    if result.get("pulled"):
        _notify_artifact_update(state, slug, art.version)
    # ``_serialize`` already ran the redactors over the (≤25 MiB) content body
    # and the other LLM-originated fields, so don't rescan ``content`` — that
    # double pass is a redundant multi-second regex scan on the event loop.
    payload = _redact_remote_response(
        _serialize(art, include_content=True), already_redacted=_SERIALIZE_REDACTED_KEYS
    )
    payload["pull_result"] = _redact_remote_response(result)
    return _json_response(payload)


async def api_artifact_upstream_status(request: web.Request) -> web.Response:
    """GET /api/artifacts/{slug}/upstream-status — cheap (metadata-only) check
    of whether the tracked upstream has changes to pull. Read-only; drives the
    detail page's non-blocking pull-available / conflict banner. Best-effort —
    a provider failure reports ``tracked`` with ahead/conflict defaulted False
    so opening an artifact never blocks on the network."""
    slug = request.match_info.get("slug", "")
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_upstream_status",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": slug},
        )
        return _err("restricted session cannot query upstream status", status=403)
    try:
        status = await publish_sync.upstream_status(slug)
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_upstream_status",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_upstream_status",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    _audit(
        tool="artifact_upstream_status",
        request=request,
        outcome="success",
        extra={"slug": slug, "upstream_ahead": bool(status.get("upstream_ahead"))},
    )
    return _json_response(status)


async def api_artifact_overwrite_remote(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/overwrite-remote — force the local content to
    become the remote's current version even when the remote moved ahead,
    WITHOUT pulling the remote's (possibly untrusted) bytes into the local
    store. The superseded remote version stays in the provider's history (no
    delete-version primitive). See ``publish_sync.overwrite_upstream``.

    Egress chokepoint: bytes leave the box to the tracked destination, and
    ``publish_sync.overwrite_upstream`` has NO internal gate (``push_version``
    is ungated), so the ``capabilities.publish`` governance ceiling is enforced
    HERE — on the resolved ``publication.provider`` — before any provider
    dispatch (same fail-closed gate as publish / update-sharing).
    """
    slug = request.match_info.get("slug", "")
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_overwrite_remote",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": slug},
        )
        return _err("restricted session cannot overwrite the remote", status=403)
    store = get_default_store()
    try:
        existing = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_overwrite_remote",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    # Resolve the destination the bytes actually go to: the existing
    # publication's provider. An unpublished artifact can't be overwritten —
    # publish_sync reports that cleanly, but it must not bypass the gate, so
    # gate on the default provider name in that case.
    overwrite_provider = (
        existing.publication.provider
        if existing.publication is not None and existing.publication.provider
        else "artifactory"
    )
    gov_denial = _publish_governance_denied(request, overwrite_provider)
    if gov_denial is not None:
        _audit(
            tool="artifact_overwrite_remote",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"slug": slug, "provider": overwrite_provider},
        )
        return _err(gov_denial, status=403)
    try:
        result = await publish_sync.overwrite_upstream(slug)
        art = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_overwrite_remote",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except Exception as exc:  # pragma: no cover — overwrite is best-effort
        _audit(
            tool="artifact_overwrite_remote",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(_redact_text(str(exc)), status=502)
    _audit(
        tool="artifact_overwrite_remote",
        request=request,
        outcome="success",
        extra={"slug": slug, "overwritten": bool(result.get("overwritten"))},
    )
    payload = _redact_remote_response(
        _serialize(art, include_content=True), already_redacted=_SERIALIZE_REDACTED_KEYS
    )
    payload["overwrite_result"] = _redact_remote_response(result)
    return _json_response(payload)


# ── Provider negotiation (Mesh-2445) ─────────────────────────────────────────


def _sharing_model_dict(sm: Any) -> dict[str, Any]:
    return {
        "supports_private": sm.supports_private,
        "supports_shared": sm.supports_shared,
        "supports_public": sm.supports_public,
        "principal_kind": sm.principal_kind,
        "supports_roles": sm.supports_roles,
        "supports_expiration": sm.supports_expiration,
        "programmable": sm.programmable,
        "out_of_band_url": sm.out_of_band_url,
    }


async def api_artifact_publish_providers(request: web.Request) -> web.Response:
    """GET /api/artifacts/publish-providers?kind=<kind> — available publishing
    providers with per-kind support + sharing/sync/discovery descriptors.

    Drives the share-panel picker: the FE shows a provider selector only when
    >1 *available* provider can host the artifact's kind (``kind_support !=
    unsupported``), and renders the right sharing controls per provider. Read-
    only; no mutation, so no restricted-session gate (matches the list endpoint).
    """
    kind = request.query.get("kind") or "widget"
    out: list[dict[str, Any]] = []
    for p in list_providers():
        try:
            avail = p.available()
            # A not-yet-installed provider still shows when it can self-install
            # on first publish (ensure_ready) — hiding it entirely would make
            # the destination undiscoverable until the user installs by hand.
            if not avail and not p.installable():
                continue
            ks = p.kind_support(kind)
            sm = p.sharing_model()
            sy = p.sync_model()
            dm = p.discovery_model()
        except Exception as exc:  # pragma: no cover — a flaky provider must not break the picker
            logger.warning("publish-providers: skipping %r: %s", getattr(p, "name", "?"), exc)
            continue
        out.append(
            {
                "name": p.name,
                "display_name": p.display_name,
                "capabilities": sorted(c.value for c in p.capabilities()),
                "kind_support": ks.value,
                "capable": ks != KindSupport.UNSUPPORTED,
                # False + present in this list ⇒ installs on first publish; the
                # FE may surface an "installs on first use" hint.
                "available": avail,
                "sharing_model": _sharing_model_dict(sm),
                "sync_model": {
                    "authority": sy.authority,
                    "concurrency": sy.concurrency,
                    "collab_mode": sy.collab_mode,
                },
                "discovery_model": {
                    "list_mine": dm.list_mine,
                    "list_shared_with_me": dm.list_shared_with_me,
                    "list_public": dm.list_public,
                    "full_text_search": dm.full_text_search,
                    "pull_by_id": dm.pull_by_id,
                },
            }
        )
    return _json_response({"providers": out, "kind": kind})
