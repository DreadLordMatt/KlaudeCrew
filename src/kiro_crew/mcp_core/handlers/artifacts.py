"""artifact_* tool handlers + artifact link/folder/reemit/anchor helpers."""

from __future__ import annotations

import json
import re as _re
import unicodedata
import urllib.request
from typing import Any
from urllib.parse import urlencode

from kiro_crew.artifacts import _infer_kind
from kiro_crew.mcp_core.governance import _resolve_session_key
from kiro_crew.mcp_core.handlers import _UNHANDLED
from kiro_crew.mcp_core.identity import _internal_secret
from kiro_crew.mcp_core.transport import (
    _API,
    _delete,
    _get,
    _patch,
    _post,
)
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import (
    ARTIFACT_AGENT_MARKER,
    ARTIFACT_DELETE_COMMENT_SCHEMA,
    ARTIFACT_DELETE_SCHEMA,
    ARTIFACT_FOLDER_CREATE_SCHEMA,
    ARTIFACT_FOLDER_DELETE_SCHEMA,
    ARTIFACT_FOLDER_LIST_SCHEMA,
    ARTIFACT_FOLDER_MOVE_SCHEMA,
    ARTIFACT_FOLDER_RENAME_SCHEMA,
    ARTIFACT_GET_COMMENTS_SCHEMA,
    ARTIFACT_GET_SCHEMA,
    ARTIFACT_LIST_SCHEMA,
    ARTIFACT_MARK_REVIEW_SCHEMA,
    ARTIFACT_MOVE_SCHEMA,
    ARTIFACT_POST_COMMENT_SCHEMA,
    ARTIFACT_REPLY_COMMENT_SCHEMA,
    ARTIFACT_REVERT_SCHEMA,
    ARTIFACT_SAVE_SCHEMA,
    ARTIFACT_UPDATE_SCHEMA,
    ARTIFACT_VERSIONS_SCHEMA,
    validate_tool_args,
)


def _artifact_ref_link(slug: str, name: str) -> str:
    """Render a clickable ``[<name>](/artifacts/<slug>)`` markdown link.

    The chat renderer turns this into an anchor the frontend intercepts to open
    the artifact in the side panel; ``/artifacts/<slug>`` is the canonical
    full-page route, so it also degrades to a normal navigation if interception
    is absent. Used for non-widget kinds, which (unlike widgets) don't
    round-trip via ``<mcwidget>`` and otherwise have no clickable form in chat.
    """
    # name/slug are LLM-influenced and rendered verbatim on the dashboard, so
    # scrub for credential / exfiltration patterns (same guard as other
    # tool-result paths).
    label = name or slug
    label, _ = redact_exfiltration_urls(label)
    label, _ = redact_credentials(label)
    # Unescaped ']' would break the markdown link syntax.
    label = label.replace("[", "(").replace("]", ")")
    # A literal newline in the label splits the link text across lines, breaking
    # the single-line markdown anchor — collapse CR/LF to spaces so a crafted
    # name can't fragment the rendered link.
    label = label.replace("\r", " ").replace("\n", " ")
    safe_slug, _ = redact_exfiltration_urls(slug or "")
    safe_slug, _ = redact_credentials(safe_slug)
    # Constrain to the slug charset so a crafted value can't inject ')'/markdown
    # out of the URL.
    safe_slug = _re.sub(r"[^a-z0-9-]", "", safe_slug.lower())
    # If sanitization leaves no slug (e.g. the '?' fallback or an all-redacted
    # value), a link would dangle at /artifacts/ with no target — degrade to
    # plain text so the name still surfaces without a broken anchor.
    if not safe_slug:
        return label
    return f"[{label}](/artifacts/{safe_slug})"


def _resolve_artifact_folder_id(ref: str) -> tuple[str, str | None]:
    """Resolve an artifact-folder reference (id or human path) to a folder id.

    Read-only: fetches ``/api/artifact-folders`` and matches by id, then walks
    ``/``-separated path segments against folder names (case-insensitive). Used
    by the rename/move/delete MCP tools, which must address an existing folder
    (no auto-create — that only happens on save/move to an artifact folder,
    handled server-side). Returns ``(folder_id, error)``; ``""`` = root.
    """
    ref = str(ref or "").strip()
    if not ref or ref.lower() == "root":
        return "", None
    d = _get("/api/artifact-folders")
    if d.get("error"):
        return "", d["error"]
    folders = d.get("folders", [])
    by_id = {f.get("id"): f for f in folders if isinstance(f, dict) and f.get("id")}
    if ref in by_id:
        return ref, None
    segments = [s.strip().lower() for s in ref.split("/") if s.strip()]
    if not segments:
        return "", None
    parent = ""
    cur = ""
    for seg in segments:
        match = next(
            (
                f
                for f in folders
                if str(f.get("parent_id") or "") == parent
                and str(f.get("name", "")).strip().lower() == seg
            ),
            None,
        )
        if match is None:
            safe_ref, _ = redact_exfiltration_urls(ref)
            safe_ref, _ = redact_credentials(safe_ref)
            return "", f"folder not found: {safe_ref}"
        cur = str(match.get("id") or "")
        parent = cur
    return cur, None


def _artifact_reemit_hint(slug: str, name: str, kind: str = "widget") -> str:
    """Render the canonical re-emit-this-artifact-in-chat instruction.

    Appended to artifact_save / artifact_get / artifact_update tool
    responses so the agent has the exact tag string in context at the
    moment it's about to render the artifact in chat. The artifacts
    skill says ``slug=`` is required on every re-emission of a saved
    artifact, but skill rules can be overlooked at emission time —
    Mesh-1715 session logs confirmed an LLM had the slug in front of
    it twice (artifact_get response + artifact_update response) and
    still emitted ``<mcwidget title="...">`` without the attribute,
    creating a duplicate artifact when the user clicked save.

    The hint reduces this to "copy the tag I just gave you."
    """
    if kind != "widget":
        # Non-widget artifacts (markdown, html, svg, json, text) don't
        # round-trip through `<mcwidget>` — they render via the artifact
        # detail page or MarkdownPanel. No re-emit hint needed.
        return ""
    safe_name = (name or "").replace('"', "'")
    return (
        "When you re-emit this widget in chat, use this exact opening tag\n"
        "(slug attribute is REQUIRED — without it, the user clicking save\n"
        "creates a duplicate artifact):\n\n"
        f'<mcwidget title="{safe_name}" slug="{slug}">'
    )


def _format_anchor(anchor: dict) -> str:
    """Format an anchor quote for the artifact_get_comments output (Mesh-2503).

    Short quotes (≤300 chars) are shown in full. Longer quotes are bookended
    with the first and last 100 chars plus an explicit TRUNCATED marker
    (never ambiguous with literal user text). Offsets are always included
    when available so the agent can locate the range in the document.
    """
    quote = anchor.get("quote", "")
    start = anchor.get("start_offset")
    end = anchor.get("end_offset")
    offset_info = ""
    if start is not None and end is not None:
        offset_info = f", chars {start}:{end}"
    if len(quote) <= 300:
        return f' [on: "{quote}"{offset_info}]'
    head = quote[:100]
    tail = quote[-100:]
    omitted = len(quote) - 200
    return f' [on: "{head}" [TRUNCATED: {omitted} chars omitted' f'{offset_info}] "{tail}"]'


def handle(name, args):
    if name == "artifact_save":
        args = validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)
        save_body: dict[str, Any] = {
            "name": args["name"],
            "content": args["content"],
        }
        for k in ("slug", "kind", "source", "description", "tags", "folder", "webapp_metadata"):
            if k in args and args[k] is not None:
                save_body[k] = args[k]
        # Pre-save dedup probe: when saving a chat-source widget, check for
        # an existing widget artifact with the same NFC-normalized name.
        # If one exists we still allow the save (the agent may have a real
        # reason to create a parallel artifact), but we attach a hint so
        # the agent can self-correct on the next turn — typically that
        # means deleting the just-created duplicate and using
        # ``artifact_update`` on the pre-existing slug instead. Without
        # this hint, the agent's only signal that a duplicate happened is
        # the user noticing in the library, which is exactly the failure
        # mode Mesh-1715 surfaced (Fight Club: agent created
        # ``rules-of-fight-club`` even though ``a07ece9a8c3309aa`` named
        # "The Rules of Fight Club" already existed).
        # Resolve the kind the same way the store will (CR-1 kind inference):
        # an explicit kind wins, else infer from the inline content. The MCP
        # save path never forwards a source_path, so content sniff is the only
        # signal. This keeps the widget-only duplicate probe below from firing
        # on a markdown/text deliverable that merely shares a name with a widget.
        kind_for_dedup = args.get("kind") or _infer_kind(args.get("content", ""), "", None)
        source_for_dedup = args.get("source", "chat")
        explicit_slug = args.get("slug")
        target_name = args.get("name", "")
        dedup_hint = ""
        if (
            kind_for_dedup == "widget"
            and source_for_dedup == "chat"
            and not explicit_slug
            and isinstance(target_name, str)
            and target_name
            and target_name.lower() != "widget"
        ):
            try:
                qs = urlencode(
                    {
                        "kind": "widget",
                        "source": "chat",
                        "q": target_name,
                    }
                )
                listing = _get(f"/api/artifacts?{qs}")
                if listing.get("error"):
                    raise ValueError(listing["error"])
                candidates = listing.get("artifacts") or []
                target_norm = unicodedata.normalize("NFC", target_name).lower()
                conflicts = [
                    a
                    for a in candidates
                    if isinstance(a, dict)
                    and isinstance(a.get("name"), str)
                    and isinstance(a.get("slug"), str)
                    and unicodedata.normalize("NFC", a["name"]).lower() == target_norm
                ]
                if conflicts:
                    # Sort newest first, mirror frontend dedup.
                    conflicts.sort(
                        key=lambda a: a.get("updated_at") or "",
                        reverse=True,
                    )
                    existing_slug = conflicts[0]["slug"]
                    if len(conflicts) > 1:
                        dedup_hint = (
                            "\n\n⚠️  Possible duplicate: a widget artifact named "
                            f'"{target_name}" already exists at '
                            f"slug={existing_slug!r} (and {len(conflicts) - 1} "
                            "other same-named match(es))."
                        )
                    else:
                        dedup_hint = (
                            "\n\n⚠️  Possible duplicate: a widget artifact named "
                            f'"{target_name}" already exists at '
                            f"slug={existing_slug!r}."
                        )
                    dedup_hint += (
                        " If you intended to capture a new version of that "
                        "artifact, delete the duplicate just created and "
                        "call `artifact_update` on the existing slug "
                        "instead. If both artifacts are genuinely needed, "
                        "rename one to disambiguate."
                    )
            except Exception:
                # Probe failure is non-fatal — proceed with the save and
                # skip the hint. Don't let a transient list failure block
                # legitimate save calls. We deliberately swallow without
                # logging because mcp_core.py runs as a stdio MCP server
                # — any stdout/stderr writes corrupt the JSON-RPC stream.
                pass
        d = _post("/api/artifacts", save_body)
        if d.get("error"):
            return f"Error: {d['error']}"
        slug = d.get("slug", "?")
        version = d.get("version", 1)
        name = d.get("name", args.get("name", ""))
        kind = d.get("kind", args.get("kind", "widget"))
        # FU-3: the artifact-deploy skill requires webapp producers to fill
        # projected cost estimates at save time, but nothing enforced it —
        # field-tested agents skipped it and the card's cost area rendered
        # blank until deploy. Attach a soft warning hint (never a hard
        # reject: existing flows must keep working) so the agent
        # self-corrects on the next turn.
        cost_hint = ""
        wm = args.get("webapp_metadata")
        if kind == "webapp" and isinstance(wm, dict):
            cost = wm.get("cost") or {}
            if not (isinstance(cost, dict) and cost.get("estimates")):
                cost_hint = (
                    "\n\n⚠️  webapp_metadata.cost.estimates is empty — the "
                    "artifact card's cost area will render blank. Call "
                    "`artifact_update` with projected what-if estimates "
                    "(e.g. views buckets with usd amounts) per the "
                    "artifact-deploy skill contract."
                )
        # Widgets re-surface via the re-emit tag; only non-widgets need the link.
        ref_link = "" if kind == "widget" else f"{_artifact_ref_link(slug, name)}\n\n"
        return (
            f"Saved artifact: slug={slug} version={version}\n\n"
            f"{ref_link}"
            f"{_artifact_reemit_hint(slug, name, kind)}"
            f"{dedup_hint}"
            f"{cost_hint}"
        )

    if name == "artifact_get":
        args = validate_tool_args(args, ARTIFACT_GET_SCHEMA)
        slug = args["slug"]
        version = args.get("version")
        path = f"/api/artifacts/{slug}"
        if version:
            path = f"/api/artifacts/{slug}/versions/{int(version)}"
        d = _get(path)
        if d.get("error"):
            return f"Error: {d['error']}"

        content = d.get("content") or ""
        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)
        meta_lines = [
            f"slug: {d.get('slug', '?')}",
            f"name: {d.get('name', '?')}",
            f"kind: {d.get('kind', '?')}",
            f"version: {d.get('version', '?')}",
            f"updated_at: {d.get('updated_at', '?')}",
        ]
        if d.get("description"):
            meta_lines.append(f"description: {d['description']}")
        if d.get("tags"):
            meta_lines.append(f"tags: {', '.join(d['tags'])}")
        out_body = "\n".join(meta_lines) + "\n\n--- content ---\n" + content
        # Append a re-emit hint for widgets so the agent has the exact tag
        # string it should use when surfacing the artifact in chat. Without
        # this the slug rule from the artifacts skill is easy to overlook
        # at emission time even though it's right there at the top of this
        # response — verified by Mesh-1715 session logs where the LLM had
        # the slug in front of it twice and still emitted without it.
        kind = d.get("kind", "widget")
        if kind == "widget":
            out_body += "\n\n" + _artifact_reemit_hint(d.get("slug", "?"), d.get("name", ""), kind)
        else:
            out_body += "\n\n" + _artifact_ref_link(d.get("slug", "?"), d.get("name", ""))
        return out_body

    if name == "artifact_update":
        args = validate_tool_args(args, ARTIFACT_UPDATE_SCHEMA)
        slug = args["slug"]
        update_body = {k: v for k, v in args.items() if k != "slug" and v is not None}
        if not update_body:
            return "Error: nothing to update (provide content/name/description/tags)"
        # Note: 'actor' is no longer set in the body — the API handler infers
        # it from the X-Internal-Secret header presence (MCP=agent,
        # dashboard=user). This is more secure than trusting a body field
        # and saves the agent from having to remember to set it.
        # _post helper sends POST; we need PATCH. Use urllib.request directly
        # (already imported at module top).
        data = json.dumps(update_body).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(),
        }
        sk = _resolve_session_key()
        if sk:
            headers["X-Session-Key"] = sk
        req = urllib.request.Request(
            f"{_API}/api/artifacts/{slug}", data=data, headers=headers, method="PATCH"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as http_resp:
                d = json.loads(http_resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err_body = json.loads(exc.read()).get("error", str(exc))
            except Exception:
                err_body = str(exc)
            return f"Error: {err_body}"
        except Exception as exc:
            return f"Error: {exc}"
        out = [f"Updated artifact: slug={d.get('slug', slug)} version={d.get('version', '?')}"]
        # Surface source_path so the agent can emit unified-diff headers
        # when summarising the change in chat (powers the dashboard's
        # Open file affordance on diff blocks). See artifacts skill for
        # the exact format.
        sp = d.get("source_path") or ""
        if sp:
            out.append(f"source_path: {sp}")
        # Re-emit hint for widget-kind updates — same rationale as in
        # artifact_get above. Iterate flow especially needs this because
        # the agent's next step is almost always re-emitting the updated
        # widget in chat, and forgetting the slug at that point is the
        # single largest source of duplicate-artifact creation.
        if d.get("kind", "widget") == "widget":
            out.append("")
            out.append(_artifact_reemit_hint(d.get("slug", slug), d.get("name", ""), "widget"))
        else:
            out.append("")
            out.append(_artifact_ref_link(d.get("slug", slug), d.get("name", "")))
        return "\n".join(out)

    if name == "artifact_revert":
        args = validate_tool_args(args, ARTIFACT_REVERT_SCHEMA)
        slug = args["slug"]
        target_version = int(args["target_version"])
        # Step 1: read the target version's content. Using the API endpoint
        # so the actor / session_id inference from the PATCH stays consistent
        # — we don't bypass the auth-aware handler.
        target = _get(f"/api/artifacts/{slug}/versions/{target_version}")
        if target.get("error"):
            return f"Error: cannot fetch version {target_version}: {target['error']}"
        target_content = target.get("content") or ""
        # Step 2: PATCH the artifact with the target's content + reverted
        # event metadata. Snapshot is forced True for reverted updates by
        # the handler — this becomes a new version pinned to the timeline.
        body = {
            "content": target_content,
            "event_type": "reverted",
            "from_version": target_version,
        }
        data = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(),
        }
        sk = _resolve_session_key()
        if sk:
            headers["X-Session-Key"] = sk
        req = urllib.request.Request(
            f"{_API}/api/artifacts/{slug}", data=data, headers=headers, method="PATCH"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as http_resp:
                d = json.loads(http_resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err_body = json.loads(exc.read()).get("error", str(exc))
            except Exception:
                err_body = str(exc)
            return f"Error: {err_body}"
        except Exception as exc:
            return f"Error: {exc}"
        # Surface source_path on the response so the calling agent can build
        # a proper unified-diff header (--- <path>\n+++ <path>) when
        # summarising the revert in chat. The dashboard's diff renderer
        # reads those headers to show the "Open file" button — without
        # them, the user sees a diff with no way to drop into the file
        # in the side panel (Mesh-1654 round 7 follow-up).
        live_version = d.get("version", "?")
        source_path = d.get("source_path") or ""
        out_lines = [
            f"Reverted {slug} to v{target_version}'s content. "
            f"Live state is now v{live_version} (snapshot of v{target_version}).",
        ]
        if source_path:
            out_lines.append(f"source_path: {source_path}")
            out_lines.append(
                "When summarising in chat, emit a ```diff fenced block "
                f"with `--- {source_path}` and `+++ {source_path}` "
                "headers so the dashboard's Open file button is operable."
            )
        return "\n".join(out_lines)

    if name == "artifact_get_comments":
        args = validate_tool_args(args, ARTIFACT_GET_COMMENTS_SCHEMA)
        slug = args["slug"]
        d = _get(f"/api/artifacts/{slug}/comments")
        if d.get("error"):
            return f"Error: {d['error']}"
        comments = d.get("comments", [])
        if not comments:
            return f"No comments on artifact `{slug}`."
        lines = []
        for c in comments:
            # Agent provenance rides on the structured is_agent field, not the
            # persisted body — prefix a plain-text marker on this CLI/text surface
            # (the dashboard shows a lucide Bot icon from the same field).
            prefix = ARTIFACT_AGENT_MARKER if c.get("is_agent") else ""
            comment_body = str(c.get("body", ""))
            anchor = ""
            if c.get("anchor") and c["anchor"].get("quote"):
                anchor = _format_anchor(c["anchor"])
            indent = "  ↳ " if c.get("parent_id") else "• "
            # Surface the comment id: it is the handle the agent must pass to
            # artifact_mark_review / artifact_delete_comment, so omitting it left
            # those follow-up tools uncallable from a get_comments result.
            cid = c.get("id")
            id_tag = f" (id={cid})" if cid else ""
            lines.append(
                f"{indent}{prefix}{c.get('author', '?')}: {comment_body}"
                f"{anchor} [{c.get('status', 'open')}]{id_tag}"
            )
        result_str = f"Comments on `{slug}` ({len(comments)}):\n" + "\n".join(lines)
        # Route verbatim comment egress through the canonical context-aware shim
        # (not the raw redact_credentials/redact_exfiltration_urls pair) so a
        # companion's extra credential patterns apply, matching the chat-history
        # egress in this same file.
        return redact(result_str)

    if name == "artifact_post_comment":
        args = validate_tool_args(args, ARTIFACT_POST_COMMENT_SCHEMA)
        slug = args["slug"]
        text = args["text"]
        scope = args.get("scope") or "private"
        # Never trust LLM output — redact before posting to the dashboard. Route
        # through the canonical context-aware shim so a companion's extra
        # credential patterns apply on this egress path too. (The SEL audit log
        # is redacted centrally in call_tool_with_logging, so the raw text can't
        # leak into the audit resources either.)
        text = redact(text)
        d = _post(
            f"/api/artifacts/{slug}/comments",
            {
                # Store the body verbatim; agent provenance is the structured
                # is_agent flag (no emoji persisted into the body — CLAUDE.md).
                "text": text,
                "scope": scope,
                "is_agent": True,
                "author": "agent",
            },
        )
        if d.get("error"):
            return f"Error: {d['error']}"
        cmt = d.get("comment", {})
        return f"Comment posted (id={cmt.get('id', '?')}, sync={cmt.get('sync_state', '?')})"

    if name == "artifact_reply_comment":
        args = validate_tool_args(args, ARTIFACT_REPLY_COMMENT_SCHEMA)
        slug = args["slug"]
        parent_id = args["parent_id"]
        text = args["text"]
        # Never trust LLM output — redact before posting to the dashboard. Route
        # through the canonical context-aware shim so a companion's extra
        # credential patterns apply on this egress path too.
        text = redact(text)
        d = _post(
            f"/api/artifacts/{slug}/comments/{parent_id}/reply",
            {
                # Store the body verbatim; agent provenance is the structured
                # is_agent flag (no emoji persisted into the body — CLAUDE.md).
                "text": text,
                "is_agent": True,
                "author": "agent",
            },
        )
        if d.get("error"):
            return f"Error: {d['error']}"
        cmt = d.get("comment", {})
        return f"Reply posted (id={cmt.get('id', '?')}, sync={cmt.get('sync_state', '?')})"

    if name == "artifact_mark_review":
        args = validate_tool_args(args, ARTIFACT_MARK_REVIEW_SCHEMA)
        slug = args["slug"]
        comment_id = args["comment_id"]
        d = _post(f"/api/artifacts/{slug}/comments/{comment_id}/review", {})
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Comment {comment_id} advanced to REVIEW status."

    if name == "artifact_delete_comment":
        args = validate_tool_args(args, ARTIFACT_DELETE_COMMENT_SCHEMA)
        slug = args["slug"]
        comment_id = args["comment_id"]
        reason = args["reason"]
        # Never trust LLM output — the reason lands in the activity feed, so
        # redact before sending. Route through the canonical context-aware shim
        # so a companion's extra credential patterns apply. (The SEL audit log
        # is redacted centrally in call_tool_with_logging.)
        reason = redact(reason)
        d = _delete(
            f"/api/artifacts/{slug}/comments/{comment_id}",
            {"reason": reason},
        )
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Comment {comment_id} deleted (reason recorded in activity feed)."

    if name == "artifact_list":
        args = validate_tool_args(args, ARTIFACT_LIST_SCHEMA)
        params: dict[str, str] = {}
        for k in ("tag", "kind", "q"):
            v = args.get(k)
            if v:
                params[k] = v
        path = "/api/artifacts"
        if params:
            path = f"{path}?{urlencode(params)}"
        d = _get(path)
        if d.get("error"):
            return f"Error: {d['error']}"
        items = d.get("artifacts", [])
        if not items:
            return "No artifacts saved."
        lines = []
        for a in items:
            tags = f"  [{', '.join(a.get('tags', []))}]" if a.get("tags") else ""
            lines.append(
                f"{a.get('slug', '?')}  v{a.get('version', '?')}  "
                f"{a.get('kind', '?')}{tags}  {a.get('name', '?')}"
            )
        return "\n".join(lines)

    if name == "artifact_versions":
        args = validate_tool_args(args, ARTIFACT_VERSIONS_SCHEMA)
        slug = args["slug"]
        d = _get(f"/api/artifacts/{slug}/versions")
        if d.get("error"):
            return f"Error: {d['error']}"
        versions = d.get("versions", [])
        if not versions:
            return f"No versions found for {slug}."
        return f"{slug}: versions {', '.join(f'v{v}' for v in versions)}"

    if name == "artifact_delete":
        args = validate_tool_args(args, ARTIFACT_DELETE_SCHEMA)
        slug = args["slug"]
        d = _delete(f"/api/artifacts/{slug}")
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Deleted artifact: {slug}"

    if name == "artifact_folder_list":
        validate_tool_args(args, ARTIFACT_FOLDER_LIST_SCHEMA)
        d = _get("/api/artifact-folders")
        if d.get("error"):
            return f"Error: {d['error']}"
        folder_rows = d.get("folders", [])
        if not folder_rows:
            return "No artifact folders."
        # Present as a path-sorted tree so the agent can pick an id or path.
        folder_rows.sort(key=lambda fld: str(fld.get("path") or fld.get("name", "")).lower())
        out_lines = []
        for fld in folder_rows:
            fld_path = fld.get("path") or fld.get("name", "?")
            count = fld.get("item_count", 0)
            out_lines.append(
                f"{fld.get('id', '?')}  {fld_path}  ({count} item{'' if count == 1 else 's'})"
            )
        return "\n".join(out_lines)

    if name == "artifact_folder_create":
        args = validate_tool_args(args, ARTIFACT_FOLDER_CREATE_SCHEMA)
        create_body = {"name": args["name"]}
        if args.get("parent"):
            create_body["parent"] = args["parent"]
        d = _post("/api/artifact-folders", create_body)
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Created folder `{d.get('path') or d.get('name', '?')}` (id={d.get('id', '?')})."

    if name == "artifact_folder_rename":
        args = validate_tool_args(args, ARTIFACT_FOLDER_RENAME_SCHEMA)
        fld_id, fld_err = _resolve_artifact_folder_id(args["folder"])
        if fld_err:
            return f"Error: {fld_err}"
        if not fld_id:
            return "Error: cannot rename the library root."
        d = _patch(f"/api/artifact-folders/{fld_id}", {"name": args["name"]})
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Renamed folder to `{d.get('path') or d.get('name', '?')}` (id={fld_id})."

    if name == "artifact_folder_move":
        args = validate_tool_args(args, ARTIFACT_FOLDER_MOVE_SCHEMA)
        fld_id, fld_err = _resolve_artifact_folder_id(args["folder"])
        if fld_err:
            return f"Error: {fld_err}"
        if not fld_id:
            return "Error: cannot move the library root."
        parent_fid, parent_err = _resolve_artifact_folder_id(args.get("new_parent") or "")
        if parent_err:
            return f"Error: {parent_err}"
        d = _patch(f"/api/artifact-folders/{fld_id}", {"parent_id": parent_fid})
        if d.get("error"):
            return f"Error: {d['error']}"
        move_dest = d.get("path") or "(root)"
        return f"Moved folder (id={fld_id}) to `{move_dest}`."

    if name == "artifact_folder_delete":
        args = validate_tool_args(args, ARTIFACT_FOLDER_DELETE_SCHEMA)
        fld_id, fld_err = _resolve_artifact_folder_id(args["folder"])
        if fld_err:
            return f"Error: {fld_err}"
        if not fld_id:
            return "Error: cannot delete the library root."
        cascade = bool(args.get("delete_contents"))
        del_qs = "?delete_contents=true" if cascade else ""
        d = _delete(f"/api/artifact-folders/{fld_id}{del_qs}")
        if d.get("error"):
            return f"Error: {d['error']}"
        if cascade:
            n_del = len(d.get("deleted_artifact_slugs", []))
            n_folders = len(d.get("deleted_folder_ids", []))
            return (
                f"Deleted folder (id={fld_id}) and its entire subtree "
                f"({n_folders} folders, {n_del} artifacts)."
            )
        n_kept = len(d.get("reparented_artifact_slugs", []))
        return (
            f"Deleted folder (id={fld_id}); kept {n_kept} artifact"
            f"{'' if n_kept == 1 else 's'} (re-parented to the folder's parent)."
        )

    if name == "artifact_move":
        args = validate_tool_args(args, ARTIFACT_MOVE_SCHEMA)
        slug = args["slug"]
        d = _patch(f"/api/artifacts/{slug}/folder", {"folder": args.get("folder") or ""})
        if d.get("error"):
            return f"Error: {d['error']}"
        moved_fid = d.get("folder_id", "")
        return f"Moved artifact `{slug}` to " + (
            f"folder id={moved_fid}." if moved_fid else "the library root (unfiled)."
        )
    return _UNHANDLED
