"""Auto-research backend — HTTP handlers, route registration, and watchdog
startup/shutdown wiring.

The campaign logic that used to live in this module was split into focused
sibling modules (constants, db, redaction, files, store, exploration,
workflow_mode, watchdog, grill, report). This module now hosts only the aiohttp
request handlers, ``register_routes``, and the app startup/shutdown hooks — and
re-exports every name that was importable from ``auto_research.handlers`` before
the split, so existing ``from ...handlers import X`` call sites keep working.

Dependency DAG (leaves first, no import cycles):

    constants
      -> db
      -> redaction
      -> files            (constants, redaction)
      -> store            (db, constants, files, redaction)
      -> exploration      (db, constants, files, redaction, subquestion_queue)
      -> workflow_mode    (db, constants, files, store, redaction, workflow_template;
                           watchdog._emit_sse imported lazily)
      -> watchdog         (db, constants, files, store, exploration, workflow_mode, redaction)
      -> grill            (stdlib only)
      -> report           (files)
      -> handlers         (all of the above)

Tests that patch a name resolved *internally* within a submodule must target
that submodule (e.g. ``...db.DB_PATH``, ``...files.RESEARCH_DIR``,
``...watchdog._autonudge_instance``) rather than this shim.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from aiohttp import web

from kiro_crew.apps.builtins.auto_research import (
    constants,
    db,
    exploration,
    files,
    grill,
    redaction,
    report,
    store,
    watchdog,
    workflow_mode,
)
from kiro_crew.apps.builtins.auto_research.constants import DEFAULT_IDLE_SECS, CampaignStatus
from kiro_crew.apps.builtins.auto_research.db import _get_db
from kiro_crew.apps.builtins.auto_research.files import (
    _questions_path,
    _safe_campaign_dir,
    _validate_campaign_id,
    _write_brief,
    write_guidance,
)
from kiro_crew.apps.builtins.auto_research.grill import (
    _GRILL_CHILD_CAP,
    _MAX_GRILL_DEPTH,
    _grill_expand_children,
    _new_node_id,
    _node_depth,
)
from kiro_crew.apps.builtins.auto_research.redaction import (
    _audit,
    _redact_finding,
    _redact_tree_node,
)
from kiro_crew.apps.builtins.auto_research.report import (
    _REPORT_TIMEOUT,
    _build_report_prompt,
    _read_report,
    _render_findings_html,
)
from kiro_crew.apps.builtins.auto_research.store import (
    _fork_name,
    create_campaign,
    delete_campaign,
    get_campaign,
    list_campaigns,
    update_campaign_status,
    validate_campaign,
)
from kiro_crew.apps.builtins.auto_research.watchdog import (
    _SSE_QUEUE_MAXSIZE,
    _emit_sse,
    _launch_loop,
    _sse_queues,
    _stop_loop,
    _watchdog_loop,
)
from kiro_crew.apps.builtins.auto_research.workflow_mode import (
    _campaign_execution_mode,
    _launch_workflow,
    _stop_workflow,
)
from kiro_crew.knowledge.llm_pool import LLMPool

try:
    from kiro_crew.artifacts import ArtifactNotFoundError, ArtifactStore

    _HAS_ARTIFACTS = True
except ImportError:
    _HAS_ARTIFACTS = False

logger = logging.getLogger(__name__)

# The watchdog task handle is owned by the startup/shutdown hooks below (which
# manage it via ``global``), so it lives alongside them here. The loop coroutine
# itself is ``watchdog._watchdog_loop``.
_watchdog_task: asyncio.Task | None = None


# --- Auth helper ---


def _require_auth(request: web.Request) -> web.Response | None:
    """Defense-in-depth auth check. Returns 401 response if unauthorized, None if OK.

    Primary auth is enforced by the gateway _auth_middleware in server.py which
    validates tokens against the session store and sets request["user"] on
    success. This check rejects any request where middleware did not run (e.g.
    misconfigured proxy bypass) — we trust only the middleware-set user, never
    a raw token string, to avoid a fail-open bypass.
    """
    if request.get("user") is not None:
        return None
    return web.json_response({"error": "Unauthorized"}, status=401)


# --- HTTP handlers ---


async def _read_json_body(request: web.Request):
    """Parse a JSON object body, or return a 400 ``web.Response``.

    aiohttp's ``request.json()`` raises ``json.JSONDecodeError`` on a malformed
    body; without this a client input error becomes an unhandled 500 (CWE-703).
    Also type-checks the decoded body is a dict so downstream ``.get()``/``[]``
    access can't raise AttributeError/KeyError on a valid-JSON non-object.
    Callers: ``body = await _read_json_body(request); if isinstance(body,
    web.Response): return body``.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object"}, status=400
        )
    return body


async def _handle_validate(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    _audit("campaign_validate", "*")
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, validate_campaign, body)
    return web.json_response(result)


async def _handle_grill_expand(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    question = (body.get("question") or "").strip()
    if len(question) < 20:
        return web.json_response({"error": "Question too short"}, status=400)
    tree = body.get("tree") or []
    node_id = body.get("node_id")
    if not isinstance(tree, list):
        return web.json_response({"error": "tree must be a list"}, status=400)
    if node_id is not None:
        depth = _node_depth(tree, node_id)
        if depth < 0:
            return web.json_response({"error": "Unknown node_id"}, status=400)
        if depth >= _MAX_GRILL_DEPTH:
            return web.json_response({"nodes": [], "reason": "max_depth"})
    _audit("grill_expand", "*")
    pool = request.app.get("auto_research_llm_pool")
    raw = await _grill_expand_children(pool, question, tree, node_id)
    nodes = []
    for ch in raw[:_GRILL_CHILD_CAP]:
        kind = ch.get("kind") if ch.get("kind") in ("clarifier", "research") else "research"
        text = str(ch.get("text", "")).strip()
        if not text:
            continue
        nodes.append(
            {
                "id": _new_node_id(),
                "parent": node_id,
                "kind": kind,
                "text": text,
                "recommended": (
                    str(ch.get("recommended", "")).strip() if kind == "clarifier" else ""
                ),
                "answer": "",
                "origin": "grill" if kind == "research" else "",
                "status": "open",
            }
        )
    return web.json_response(_redact_finding({"nodes": nodes}))


async def _handle_create(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    loop = asyncio.get_running_loop()
    v = await loop.run_in_executor(None, validate_campaign, body)
    if not v["can_start"]:
        return web.json_response({"error": "Validation failed", **v}, status=400)
    result = await loop.run_in_executor(None, create_campaign, body)
    result["name"] = _redact_finding({"v": result["name"]})["v"]
    return web.json_response(result, status=201)


async def _handle_list(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    _audit("campaign_list", "*")
    loop = asyncio.get_running_loop()
    campaigns = await loop.run_in_executor(None, list_campaigns)
    return web.json_response(campaigns)


async def _handle_get(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    _audit("campaign_get", cid)
    loop = asyncio.get_running_loop()
    c = await loop.run_in_executor(None, get_campaign, cid)
    return web.json_response(c) if c else web.json_response({"error": "Not found"}, status=404)


async def _handle_report(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    _audit("campaign_report", cid)
    # FINDINGS.md is agent-authored — redact before serving to the dashboard.
    report_text = _redact_finding({"v": _read_report(cid)})["v"]
    return web.json_response({"report": report_text})


async def _handle_action(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    action = body.get("action")
    status_map = {
        "start": CampaignStatus.RUNNING,
        "pause": CampaignStatus.PAUSED,
        "resume": CampaignStatus.RUNNING,
        "stop": CampaignStatus.STOPPED,
    }
    if action not in status_map and action != "fork":
        return web.json_response({"error": f"Unknown action: {action}"}, status=400)

    # Fork: creates a new child campaign from a completed parent.
    if action == "fork":
        db_conn = _get_db()
        parent = db_conn.execute(
            "SELECT id, question, sources, status FROM campaigns WHERE id = ?",
            (cid,),
        ).fetchone()
        db_conn.close()
        if parent is None:
            return web.json_response({"error": "Not found"}, status=404)
        if parent["status"] not in (CampaignStatus.COMPLETE, CampaignStatus.STOPPED):
            return web.json_response(
                {"error": "Can only fork a completed or stopped campaign"}, status=409
            )
        # Build the fork config from the request body (sub_questions come from
        # the frontend's challenge-mode grill tree).
        fork_config = {
            "question": body.get("question") or parent["question"],
            "name": _fork_name(body.get("name") or body.get("question") or parent["question"]),
            "sub_questions": body.get("sub_questions", []),
            "sources": json.loads(parent["sources"] or "[]"),
            "max_cycles": body.get("max_cycles", 30),
            "idle_secs": body.get("idle_secs", DEFAULT_IDLE_SECS),
            "success_criteria": body.get("success_criteria"),
            "auto_approve": body.get("auto_approve", False),
            "parent_id": cid,
            "grill_tree": body.get("grill_tree"),
        }
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, create_campaign, fork_config)
        # Copy parent FINDINGS.md as context into the fork's dir. Use the
        # path-traversal-guarded _safe_campaign_dir (resolve + is_relative_to)
        # for both ids — defense-in-depth even though both are already
        # format-validated (cid via _validate_campaign_id, result["id"] is a
        # freshly generated uuid) — consistent with _handle_grill_tree /
        # get_findings.
        parent_dir = _safe_campaign_dir(cid)
        fork_dir = _safe_campaign_dir(result["id"])
        if parent_dir is None or fork_dir is None:
            return web.json_response({"error": "Invalid campaign ID"}, status=400)
        fork_dir.mkdir(parents=True, exist_ok=True)
        parent_findings = parent_dir / "FINDINGS.md"
        if parent_findings.exists():
            (fork_dir / "parent_findings.md").write_text(parent_findings.read_text())
        _audit("campaign_forked", result["id"], parent=cid)
        return web.json_response(result, status=201)

    # Guard invalid source-state transitions (e.g. start on a running campaign,
    # which would reset started_at and relaunch a duplicate worker loop).
    allowed = {
        "start": {CampaignStatus.READY},
        "resume": {
            CampaignStatus.PAUSED,
            CampaignStatus.STAGNANT,
            CampaignStatus.NEEDS_INPUT,
            CampaignStatus.FAILED,
            CampaignStatus.COMPLETE,
            CampaignStatus.STOPPED,
        },
        "pause": {CampaignStatus.RUNNING},
        "stop": {
            CampaignStatus.READY,
            CampaignStatus.RUNNING,
            CampaignStatus.PAUSED,
            CampaignStatus.STAGNANT,
            CampaignStatus.NEEDS_INPUT,
        },
    }
    db_conn = _get_db()
    srow = db_conn.execute("SELECT status FROM campaigns WHERE id = ?", (cid,)).fetchone()
    db_conn.close()
    if srow is None:
        return web.json_response({"error": "Not found"}, status=404)
    if srow["status"] not in allowed[action]:
        return web.json_response(
            {"error": f"Cannot {action} a campaign in '{srow['status']}' state"}, status=409
        )
    result = update_campaign_status(cid, status_map[action])
    if "error" in result:
        return web.json_response(result, status=404)
    if action in ("start", "resume"):
        mode = _campaign_execution_mode(cid)
        if mode == "workflow":
            await _launch_workflow(request, cid)
        else:
            await _launch_loop(request, cid)
    elif action == "pause":
        mode = _campaign_execution_mode(cid)
        if mode == "workflow":
            await _stop_workflow(request, cid)
        else:
            await _stop_loop(cid, remove=False)
    elif action == "stop":
        mode = _campaign_execution_mode(cid)
        if mode == "workflow":
            await _stop_workflow(request, cid)
        else:
            await _stop_loop(cid, remove=True)
    return web.json_response(result)


async def _handle_delete(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    # Tear down any running worker (agent loop or workflow run) first.
    mode = _campaign_execution_mode(cid)
    if mode == "workflow":
        await _stop_workflow(request, cid)
    else:
        await _stop_loop(cid, remove=True)
    result = delete_campaign(cid)
    if "error" in result:
        return web.json_response(result, status=404)
    _audit("campaign_deleted", cid)
    return web.json_response(result)


async def _handle_nudge(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    # Workflow-mode campaigns are driven by a deterministic DW script; guidance
    # injected mid-run has no effect (the script doesn't read guidance.txt).
    if _campaign_execution_mode(cid) == "workflow":
        return web.json_response(
            {
                "error": "Nudge/guidance not supported in workflow mode — the script "
                "runs autonomously. Use agent mode for interactive guidance."
            },
            status=409,
        )
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    text = body.get("text", "")
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    write_guidance(cid, text)
    # If the agent paused awaiting input, clear the question and resume.
    qp = _questions_path(cid)
    if qp and qp.exists():
        qp.unlink()
        update_campaign_status(cid, CampaignStatus.RUNNING)
    _audit("campaign_nudge", cid)
    return web.json_response({"ok": True})


async def _handle_report_status(request: web.Request) -> web.Response:
    """GET /campaigns/{id}/report-status -- has a report artifact already been
    exported for this campaign, and does it still exist?

    Returns ``{slug}`` (the live artifact slug) or ``{slug: null}``. Read-only
    status probe so the UI can show "View report" + "Regenerate" upfront
    instead of a bare "Export". Degrades gracefully when artifacts are off.
    """
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    if not _HAS_ARTIFACTS:
        return web.json_response({"slug": None})
    db_conn = _get_db()
    row = db_conn.execute(
        "SELECT report_artifact_slug FROM campaigns WHERE id = ?", (cid,)
    ).fetchone()
    db_conn.close()
    if row is None:
        return web.json_response({"error": "Not found"}, status=404)
    slug = row["report_artifact_slug"]
    if not slug:
        return web.json_response({"slug": None})
    # Verify the artifact still exists so the UI never offers a dead link.
    try:
        ArtifactStore().get(slug)
    except ArtifactNotFoundError:
        return web.json_response({"slug": None})
    except Exception:
        logger.exception("report-status lookup failed for %s", cid)
        return web.json_response({"slug": None})
    return web.json_response({"slug": slug})


async def _handle_to_artifact(request: web.Request) -> web.Response:
    """POST /campaigns/{id}/to-artifact -- author an HTML report artifact.

    The report is LLM-authored (a polished, synthesized document) so it is nice
    to read; if the LLM pool is unavailable or returns nothing, we fall back to
    a mechanical render of FINDINGS.md so the action never hard-fails.
    """
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    # Fail fast before any filesystem / DB / render work if artifacts are off.
    if not _HAS_ARTIFACTS:
        return web.json_response({"error": "Artifact system unavailable"}, status=503)
    d = _safe_campaign_dir(cid)
    if d is None:
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    findings_path = d / "FINDINGS.md"
    if not findings_path.exists():
        return web.json_response({"error": "No findings yet"}, status=404)
    db_conn = _get_db()
    row = db_conn.execute(
        "SELECT question, sub_questions, total_cycles, status, report_artifact_slug "
        "FROM campaigns WHERE id = ?",
        (cid,),
    ).fetchone()
    db_conn.close()
    if row is None:
        return web.json_response({"error": "Not found"}, status=404)
    question = row["question"]
    findings_md = findings_path.read_text()
    subs = json.loads(row["sub_questions"] or "[]")

    # Prefer an LLM-authored report (synthesized + nicely formatted). Cap the
    # findings fed to the prompt so a huge report doesn't blow the context.
    authored: str | None = None
    pool = request.app.get("auto_research_llm_pool")
    if pool is not None:
        try:
            prompt = _build_report_prompt(question, subs, findings_md[:24000], row["total_cycles"])
            raw = (await pool.send(prompt, timeout=_REPORT_TIMEOUT)).strip()
            # LLMs often wrap HTML in a ```html … ``` fence despite instructions.
            raw = re.sub(r"^```[a-zA-Z0-9]*\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
            if raw:
                authored = raw
        except Exception:
            logger.exception("LLM report authoring failed for %s; using fallback", cid)
    # Graceful fallback: mechanical render of the (escaped) findings.
    html: str = (
        authored
        if authored is not None
        else _render_findings_html(question, subs, findings_md, row["total_cycles"], cid)
    )

    # Redact agent/user-authored content before it lands in a shareable,
    # publishable artifact (HTML-escaping does NOT remove leaked credentials /
    # exfil URLs — that's this step). Applied uniformly to both paths.
    html = _redact_finding({"v": html})["v"]
    store_obj = ArtifactStore()
    safe_q = _redact_finding({"v": question})["v"]
    name = f"Research: {safe_q[:50]}"
    # Reuse-or-create so repeated exports update ONE artifact (new version)
    # instead of spawning a fresh duplicate on every click. We only reuse a
    # stored slug if the artifact still exists — if the user deleted it, fall
    # through to create and re-bind a new slug.
    existing_slug = row["report_artifact_slug"]
    art = None
    regenerated = False
    if existing_slug:
        try:
            store_obj.get(existing_slug)  # existence probe
            art = store_obj.update(
                existing_slug,
                content=html,
                name=name,
                description=f"Research findings for campaign {cid}",
                actor="agent",
                snapshot=True,
            )
            regenerated = True
        except ArtifactNotFoundError:
            art = None  # stored slug is dead — create a fresh one below
    if art is None:
        art = store_obj.create(
            name=name,
            content=html,
            kind="html",
            source="subagent",
            description=f"Research findings for campaign {cid}",
            tags=["research"],
        )
    # Persist the slug so the next export regenerates this same artifact and
    # the UI can show "View report" upfront.
    if art.slug != existing_slug:
        db_conn = _get_db()
        db_conn.execute(
            "UPDATE campaigns SET report_artifact_slug = ? WHERE id = ?", (art.slug, cid)
        )
        db_conn.commit()
        db_conn.close()
    _audit("campaign_to_artifact", cid, slug=art.slug)
    return web.json_response(
        {"slug": art.slug, "name": name, "regenerated": regenerated},
        status=200 if regenerated else 201,
    )


async def _handle_knowledge_status(request: web.Request) -> web.Response:
    """GET /campaigns/{id}/knowledge-status -- has this campaign's findings
    already been ingested into the Knowledge Library?

    Read-only status probe so the UI can render "Already in Knowledge" upfront
    instead of discovering it via a 409 after the user clicks. Degrades
    gracefully (``in_library: false``) when the Knowledge Library is
    unavailable -- a status check must never surface a 503.
    """
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    d = _safe_campaign_dir(cid)
    if d is None:
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    state = request.app.get("state")
    if state is None or not hasattr(state, "knowledge_store"):
        return web.json_response({"in_library": False})
    store_obj = state.knowledge_store
    # Mirror _handle_to_knowledge's dedup key: the resolved path of the
    # sanitized copy. resolve() works even if the file hasn't been written yet
    # (it has not, until the user adds it), so no filesystem side effects here.
    uri = str((d / "findings_for_knowledge.md").resolve())
    try:
        existing = store_obj.get_source_by_uri(uri)
    except Exception:
        logger.exception("knowledge-status lookup failed for %s", cid)
        return web.json_response({"in_library": False})
    if existing:
        return web.json_response({"in_library": True, "source_id": existing["id"]})
    return web.json_response({"in_library": False})


async def _handle_to_knowledge(request: web.Request) -> web.Response:
    """POST /campaigns/{id}/to-knowledge -- ingest FINDINGS.md into Knowledge Library."""
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    d = _safe_campaign_dir(cid)
    if d is None:
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    findings_path = d / "FINDINGS.md"
    if not findings_path.exists():
        return web.json_response({"error": "No findings yet"}, status=404)
    # Access knowledge store and pipeline from app state
    state = request.app.get("state")
    if state is None or not hasattr(state, "knowledge_store"):
        return web.json_response({"error": "Knowledge Library unavailable"}, status=503)
    store_obj = state.knowledge_store
    pipeline = request.app.get("knowledge_pipeline")
    if pipeline is None:
        return web.json_response({"error": "Knowledge pipeline unavailable"}, status=503)
    # The Knowledge Library is an external surface (content surfaces to users and
    # agents via RAG/search), so redact credentials + exfil URLs before ingestion.
    # The agent may have encountered secrets mid-research; ingesting raw would
    # leak them. Write a sanitized copy and ingest THAT, never the raw file.
    redacted = _redact_finding({"v": findings_path.read_text()})["v"]
    sanitized_path = d / "findings_for_knowledge.md"
    sanitized_path.write_text(redacted)
    uri = str(sanitized_path.resolve())
    # Dedup check
    existing = store_obj.get_source_by_uri(uri)
    if existing:
        return web.json_response(
            {"error": "Already in Knowledge Library", "id": existing["id"]}, status=409
        )
    # Add source and trigger ingestion
    db_conn = _get_db()
    row = db_conn.execute("SELECT question FROM campaigns WHERE id = ?", (cid,)).fetchone()
    db_conn.close()
    # The Knowledge Library is an external surface (RAG/search), so even the
    # source name metadata must be redacted before ingestion — matching the
    # treatment _handle_to_artifact applies to its artifact name.
    name = (
        f"Research: {_redact_finding({'v': row['question'][:60]})['v']}"
        if row
        else f"Research: {cid}"
    )
    sid = store_obj.add_source(name=name, source_type="local_file", uri=uri, properties={})
    store_obj.db.execute("UPDATE sources SET sync_status = 'syncing' WHERE id = ?", (sid,))
    store_obj.db.commit()

    async def _bg_ingest() -> None:
        try:
            await pipeline.ingest_file(uri, source_id=sid)
            store_obj.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (sid,))
            store_obj.db.commit()
        except Exception:
            logger.exception("Research findings ingestion failed for %s", cid)
            store_obj.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (sid,))
            store_obj.db.commit()

    task = asyncio.create_task(_bg_ingest())
    app_tasks = request.app.setdefault("_bg_tasks", set())
    app_tasks.add(task)
    task.add_done_callback(app_tasks.discard)
    _audit("campaign_to_knowledge", cid, source_id=sid)
    return web.json_response({"id": sid, "status": "ingesting"}, status=201)


async def _handle_add_question(request: web.Request) -> web.Response:
    """Append a user-authored sub-question to a campaign mid-run."""
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    # Workflow-mode campaigns plan sub-questions at launch (the DW script
    # decomposes them internally); adding questions mid-run has no effect.
    if _campaign_execution_mode(cid) == "workflow":
        return web.json_response(
            {
                "error": "Adding questions mid-run not supported in workflow mode — "
                "sub-questions are planned at launch. Use agent mode for "
                "interactive exploration."
            },
            status=409,
        )
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    db_conn = _get_db()
    row = db_conn.execute(
        "SELECT sub_questions, question, sources, scope_constraints, max_cycles, "
        "idle_secs, success_criteria, auto_approve FROM campaigns WHERE id = ?",
        (cid,),
    ).fetchone()
    if row is None:
        db_conn.close()
        return web.json_response({"error": "Not found"}, status=404)
    subs = json.loads(row["sub_questions"] or "[]")
    subs.append({"text": text, "origin": "manual", "status": "open"})
    db_conn.execute("BEGIN")
    db_conn.execute("UPDATE campaigns SET sub_questions = ? WHERE id = ?", (json.dumps(subs), cid))
    db_conn.commit()
    # Re-read the row so _write_brief sees the updated sub_questions.
    # parallel_workers MUST be included — _write_brief defaults it to 1 when
    # absent, which would silently drop the parallel instruction from the brief.
    row = db_conn.execute(
        "SELECT question, sub_questions, sources, scope_constraints, max_cycles, "
        "idle_secs, success_criteria, auto_approve, parallel_workers "
        "FROM campaigns WHERE id = ?",
        (cid,),
    ).fetchone()
    db_conn.close()
    # Regenerate brief.md so the agent sees the new question next cycle.
    _write_brief(cid, row)
    _audit("campaign_add_question", cid)
    _emit_sse({"type": "question_added", "campaign_id": cid})
    return web.json_response({"ok": True, "sub_questions": subs})


async def _handle_stream(request: web.Request) -> web.StreamResponse:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    _audit("campaign_stream", cid)
    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
    await resp.prepare(request)
    q: asyncio.Queue = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
    _sse_queues.append(q)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                if event.get("campaign_id") == cid:
                    # Findings are already redacted at the source
                    # (get_findings -> _redact_finding); avoid re-redacting.
                    data = json.dumps(event)
                    await resp.write(f"data: {data}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        _sse_queues.remove(q)
    return resp


async def _handle_grill_tree(request: web.Request) -> web.Response:
    """Serve the persisted grill tree for a campaign (for revisiting / challenge mode)."""
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    d = _safe_campaign_dir(cid)
    if d is None:
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    tree_path = d / "grill_tree.json"
    if not tree_path.exists():
        return web.json_response({"tree": []})
    try:
        tree = json.loads(tree_path.read_text())
    except (json.JSONDecodeError, OSError):
        tree = []
    # Never trust LLM output: node text/recommended fields are model-generated,
    # so redact credentials + exfiltration URLs before serving to the dashboard
    # (same treatment as cycle findings via _redact_finding).
    if not isinstance(tree, list):
        # Fail-closed: a non-list payload (file corruption/tampering) is not a
        # valid grill tree and can't be element-redacted — drop it entirely
        # rather than serving unscanned LLM-generated content to the client.
        tree = []
    else:
        # Scan EVERY element, not just dicts: stray strings would otherwise be
        # served unredacted.
        tree = [_redact_tree_node(n) for n in tree]
    return web.json_response({"tree": tree})


# --- Route registration ---


def register_routes(app: web.Application) -> None:
    app.router.add_post("/api/apps/auto-research/validate", _handle_validate)
    app.router.add_post("/api/apps/auto-research/grill/expand", _handle_grill_expand)
    app.router.add_post("/api/apps/auto-research/campaigns", _handle_create)
    app.router.add_get("/api/apps/auto-research/campaigns", _handle_list)
    app.router.add_get("/api/apps/auto-research/campaigns/{id}", _handle_get)
    app.router.add_get("/api/apps/auto-research/campaigns/{id}/report", _handle_report)
    app.router.add_get("/api/apps/auto-research/campaigns/{id}/grill-tree", _handle_grill_tree)
    app.router.add_patch("/api/apps/auto-research/campaigns/{id}", _handle_action)
    app.router.add_delete("/api/apps/auto-research/campaigns/{id}", _handle_delete)
    app.router.add_post("/api/apps/auto-research/campaigns/{id}/nudge", _handle_nudge)
    app.router.add_post("/api/apps/auto-research/campaigns/{id}/questions", _handle_add_question)
    app.router.add_post("/api/apps/auto-research/campaigns/{id}/to-knowledge", _handle_to_knowledge)
    app.router.add_get(
        "/api/apps/auto-research/campaigns/{id}/knowledge-status", _handle_knowledge_status
    )
    app.router.add_post("/api/apps/auto-research/campaigns/{id}/to-artifact", _handle_to_artifact)
    app.router.add_get(
        "/api/apps/auto-research/campaigns/{id}/report-status", _handle_report_status
    )
    app.router.add_get("/api/apps/auto-research/campaigns/{id}/stream", _handle_stream)

    async def _start_watchdog(_app: web.Application) -> None:
        global _watchdog_task
        # Dedicated LLM pool for the grill expand endpoint — isolated from the
        # Knowledge Library's pool so the two apps don't share workers.
        _app["auto_research_llm_pool"] = LLMPool(pool_size=1)
        _watchdog_task = asyncio.create_task(_watchdog_loop(_app))

    async def _stop_watchdog(_app: web.Application) -> None:
        if _watchdog_task and not _watchdog_task.done():
            _watchdog_task.cancel()
            try:
                await _watchdog_task
            except asyncio.CancelledError:
                pass
        pool = _app.get("auto_research_llm_pool")
        if pool is not None:
            await pool.shutdown()

    app.on_startup.append(_start_watchdog)
    app.on_shutdown.append(_stop_watchdog)


# --- Backward-compat re-export shim -------------------------------------------
# Republish every module-level name from each submodule so the pre-split flat
# namespace of ``auto_research.handlers`` is preserved (including the private
# underscore helpers imported directly by tests). ``setdefault`` never overrides
# a name this module already defines (its own HTTP handlers, ``logger``,
# ``_watchdog_task``, stdlib imports), so the shim only fills the gaps.
_submodules = (
    constants,
    db,
    redaction,
    files,
    store,
    exploration,
    workflow_mode,
    watchdog,
    grill,
    report,
)
for _mod in _submodules:
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals().setdefault(_name, getattr(_mod, _name))

del _mod, _name, _submodules
