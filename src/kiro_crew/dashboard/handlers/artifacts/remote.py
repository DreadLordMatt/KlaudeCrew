"""Remote (provider-routed) artifact browse / clone / fork handlers."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from kiro_crew import publish_sync
from kiro_crew.artifacts import ArtifactValidationError, get_default_store
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.publish_provider import PublishUnavailableError, get_provider

from .core import (
    _audit,
    _err,
    _json_response,
    _read_json_body,
    _run_off_loop,
)
from .publishing import _publish_governance_denied
from .redaction import (
    _SERIALIZE_REDACTED_KEYS,
    _redact_remote_response,
    _redact_text,
    _serialize,
)

logger = logging.getLogger(__name__)


# ── Remote artifacts (provider-routed browse / clone / fork) ─────────────────


def _annotate_local_slugs(out: dict[str, Any], index: dict[str, str], provider: str = "") -> None:
    """Annotate each browse row with ``local_slug`` (the local copy if already
    cloned/forked) so the UI shows open-vs-clone without a round-trip.

    ``index`` is a prebuilt map (one off-loop store scan via
    ``ArtifactStore.index_by_artifact_id``) — NOT a per-row store scan on the
    event loop. Lookups are provider-namespaced (``provider\\x00id``) so a
    browse against provider B never annotates provider A's local copy that
    happens to share an id; the bare-id key is tried only as a legacy fallback
    for records that predate provider tracking. Must run on the UN-redacted
    rows: a high-entropy ``external_id`` can be rewritten to
    ``[REDACTED: credential]`` by the credential heuristic, which would miss the
    local match and wrongly offer Clone instead of Open."""
    from kiro_crew.publish_provider import DEFAULT_PROVIDER

    items = out.get("artifacts")
    if not isinstance(items, list):
        return
    # A legacy no-provider record only emits a bare-id key (see
    # index_by_artifact_id), and such a record originated from the DEFAULT
    # provider — so the bare-id fallback may resolve ONLY a browse against that
    # same default provider. Applying it to an arbitrary provider B would let
    # B's row inherit provider A's legacy slug on a shared id, wrongly marking
    # B's artifact already-local and hiding its clone/fork action (mirrors the
    # _provider_ok gate in find_by_artifact_id).
    allow_bare = provider == DEFAULT_PROVIDER or not provider
    for item in items:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("external_id") or item.get("artifactId") or item.get("id") or "")
        if not aid:
            item["local_slug"] = None
            continue
        scoped = index.get(get_default_store().artifact_index_key(provider, aid))
        if scoped is not None:
            item["local_slug"] = scoped
        else:
            item["local_slug"] = index.get(aid) if allow_bare else None


async def api_remote_artifacts_browse(request: web.Request) -> web.Response:
    """GET /api/remote-artifacts/{provider}/browse?scope=&q= — provider-routed
    discovery via ``list_remote`` / ``search_remote``.

    A non-empty ``q`` runs full-text ``search_remote`` (providers whose
    ``discovery_model().full_text_search`` is True); otherwise
    ``list_remote(scope)``. ``None`` from the provider means that discovery
    primitive isn't supported (400). Gated like other reads-with-state. In the
    public edition the registry is empty, so ``get_provider`` raises
    ``PublishUnavailableError`` and every browse returns 404 — the surface is
    inert until a companion registers a provider.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_browse_remote",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot browse remote artifacts", status=403)
    provider_name = request.match_info.get("provider", "")
    scope = request.rel_url.query.get("scope", "mine")
    query = request.rel_url.query.get("q") or ""
    page_token = request.rel_url.query.get("pageToken")
    try:
        provider = get_provider(provider_name)
    except PublishUnavailableError as exc:
        # No provider registered under this name — inert public edition or a
        # companion misconfiguration. 503 (not 404), matching the clone/fork
        # handlers: the surface exists, the provider tooling doesn't.
        return _err(_redact_text(str(exc)), status=503)
    except Exception as exc:
        return _err(_redact_text(str(exc)), status=502)
    try:
        if query:
            result = await provider.search_remote(query=query, page_token=page_token)
        else:
            result = await provider.list_remote(scope=scope, page_token=page_token)
    except Exception as exc:
        _audit(
            tool="artifact_browse_remote",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"provider": provider_name},
        )
        return _err(_redact_text(str(exc)), status=502)
    if result is None:
        verb = "full-text search" if query else f"{scope} listing"
        return _err(f"{provider_name} does not support {verb}", status=400)
    # Annotate on the UN-redacted rows (external_ids intact) using a single
    # off-loop store scan, THEN redact — annotating after redaction would look up
    # a credential-shaped external_id in its ``[REDACTED]`` form and miss the
    # local match. The per-row scan is replaced by one indexed scan off the loop.
    index = await _run_off_loop(lambda: get_default_store().index_by_artifact_id())
    _annotate_local_slugs(result, index, provider_name)
    out = _redact_remote_response(result)
    _audit(
        tool="artifact_browse_remote",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "scope": "search" if query else scope},
    )
    return _json_response(out)


async def api_remote_artifacts_clone(request: web.Request) -> web.Response:
    """POST /api/remote-artifacts/{provider}/clone (``external_id`` in the JSON
    body) — provider-routed bidirectional clone (sets a ``publication``;
    collab_mode from the provider).

    Governance: a clone arms future egress — ``clone_from_remote`` sets
    ``auto_sync=True``, so every later local snapshot auto-pushes to the remote
    via the ungated ``push_version``. The ``capabilities.publish`` ceiling is
    therefore enforced HERE, before the clone binds the two copies (same
    fail-closed gate as publish). Fork (pull-only lineage) stays ungated.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_clone",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot clone artifacts", status=403)
    provider_name = request.match_info.get("provider", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    external_id = str(body.get("external_id") or "").strip()
    if not external_id:
        return _err("external_id is required")
    gov_denial = _publish_governance_denied(request, provider_name)
    if gov_denial is not None:
        _audit(
            tool="artifact_clone",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(gov_denial, status=403)
    try:
        art = await publish_sync.clone_from_remote(external_id, provider_name=provider_name)
    except PublishUnavailableError as exc:
        _audit(
            tool="artifact_clone",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=503)
    except Exception as exc:
        _audit(
            tool="artifact_clone",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=502)
    _audit(
        tool="artifact_clone",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "external_id": external_id, "slug": art.slug},
    )
    return _json_response(
        _redact_remote_response(
            _serialize(art, include_content=True), already_redacted=_SERIALIZE_REDACTED_KEYS
        ),
        status=201,
    )


async def api_remote_artifacts_fork(request: web.Request) -> web.Response:
    """POST /api/remote-artifacts/{provider}/fork (``external_id`` in the JSON
    body) — provider-routed fork (independent copy with pull-only
    ``fork_metadata`` lineage). Ingress only — never arms a push — so no publish
    governance gate."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_fork",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot fork artifacts", status=403)
    provider_name = request.match_info.get("provider", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    external_id = str(body.get("external_id") or "").strip()
    if not external_id:
        return _err("external_id is required")
    try:
        art = await publish_sync.fork_from_remote(external_id, provider_name=provider_name)
    except PublishUnavailableError as exc:
        _audit(
            tool="artifact_fork",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=503)
    except Exception as exc:
        _audit(
            tool="artifact_fork",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=502)
    _audit(
        tool="artifact_fork",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "external_id": external_id, "slug": art.slug},
    )
    return _json_response(
        _redact_remote_response(
            _serialize(art, include_content=True), already_redacted=_SERIALIZE_REDACTED_KEYS
        ),
        status=201,
    )
