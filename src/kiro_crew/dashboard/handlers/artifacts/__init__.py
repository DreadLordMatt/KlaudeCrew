"""Artifact HTTP handlers (``/api/artifacts`` and friends).

This package was split out of the former single ``handlers/artifacts.py`` module.
It re-exports every public handler and internal helper the module previously
exposed, so ``from kiro_crew.dashboard.handlers.artifacts import <name>`` keeps
working unchanged (``dashboard/server.py`` imports ~30 handlers by name, and
``deploy/teardown.py`` imports ``_serialize``).

Submodules (a load-time DAG, leaf first):
  - ``redaction``  — serialization + redaction + snippet helpers (leaf).
  - ``core``       — request/response/audit primitives, ``sel()`` seam, the shared
                     ``_run_off_loop`` wrapper, and the list/create/read/update/
                     delete/versions/events/pin CRUD handlers.
  - ``docs``       — session-doc scan + /materialize + the content cache
                     (single owner of ``_content_cache``; scanned by ``core``).
  - ``folders``    — folder handlers + folder-reference resolvers + relocate.
  - ``publishing`` — publish/share/sync handlers + ``_publish_governance_denied``.
  - ``comments``   — comment CRUD handlers.
  - ``remote``     — provider-routed browse / clone / fork handlers.

The submodules are imported eagerly below so the by-name imports resolve at load.
``core`` reaches ``docs`` / ``folders`` / ``publishing`` only through lazy
(function-local) imports inside three handlers, which is what keeps the package a
DAG despite create/update/list touching those clusters.
"""

from __future__ import annotations

from .comments import (  # noqa: F401
    api_artifact_comments,
    api_artifact_delete_comment,
    api_artifact_edit_comment,
    api_artifact_mark_review,
    api_artifact_post_comment,
    api_artifact_reopen_comment,
    api_artifact_reply_comment,
    api_artifact_resolve_comment,
)
from .core import (  # noqa: F401
    _MAX_BODY_BYTES,
    _SESSION_KEY_RE,
    _artifact_source_for_request,
    _audit,
    _clean_origin_session_key,
    _err,
    _event_session_id,
    _json_response,
    _notify_artifact_update,
    _read_json_body,
    _run_off_loop,
    _session_key,
    _set_pinned_and_reload,
    api_artifact_delete,
    api_artifact_detail,
    api_artifact_events,
    api_artifact_record_event,
    api_artifact_set_pinned,
    api_artifact_update,
    api_artifact_version_detail,
    api_artifact_versions,
    api_artifacts_create,
    api_artifacts_list,
    logger,
    sel,
)
from .docs import (  # noqa: F401
    _CONTENT_CACHE_MAX_ITEM_BYTES,
    _CONTENT_CACHE_MAX_TOTAL_BYTES,
    _cache_entry_bytes,
    _content_cache,
    _content_cache_bytes,
    _content_cache_lock,
    _load_content,
    _materialize_and_pin,
    _recorded_doc_identities,
    _scan_artifacts,
    _scan_session_docs,
    api_artifact_materialize,
    api_artifact_session_docs,
)
from .folders import (  # noqa: F401
    _ARTIFACT_FOLDER_ICON_TASKS,
    _resolve_folder_ref,
    _resolve_folder_ref_off_loop,
    _serialize_folder,
    _set_folder_and_reload,
    _spawn_artifact_folder_icon_task,
    api_artifact_folder_create,
    api_artifact_folder_delete,
    api_artifact_folder_update,
    api_artifact_folders,
    api_artifact_relocate,
    api_artifact_set_folder,
)
from .publishing import (  # noqa: F401
    _ARTIFACT_PROVIDER_RE,
    _VALID_VISIBILITY,
    _publish_governance_denied,
    _sharing_model_dict,
    _sync_error_response,
    _validate_sharing_body,
    api_artifact_overwrite_remote,
    api_artifact_publish,
    api_artifact_publish_providers,
    api_artifact_pull_latest,
    api_artifact_refresh_sharing,
    api_artifact_unpublish,
    api_artifact_update_sharing,
    api_artifact_upstream_status,
)
from .redaction import (  # noqa: F401
    _CONTEXT_LINE_LEN,
    _CONTEXT_MAX_LINES,
    _DELETED_SESSION_LABEL,
    _MAX_REDACT_DEPTH,
    _MD_BLOCKQUOTE_RE,
    _MD_EMPHASIS_RE,
    _MD_FENCE_RE,
    _MD_HEADING_RE,
    _MD_LINK_RE,
    _MD_LIST_RE,
    _REMOTE_ID_CRED_TAG,
    _REMOTE_ID_KEYS,
    _SEARCH_QUERY_MAX_CHARS,
    _SERIALIZE_REDACTED_KEYS,
    _SNIPPET_MAX_LEN,
    _STRIP_TAGS_RE,
    _clean_markdown,
    _context_snippet,
    _id_embeds_hard_credential,
    _redact_audit_metadata,
    _redact_remote_response,
    _redact_text,
    _redact_webapp_metadata,
    _resolve_session_title,
    _serialize,
    _snippet_from,
    _strip_content,
    _validate_inbound_webapp_metadata,
)
from .remote import (  # noqa: F401
    _annotate_local_slugs,
    api_remote_artifacts_browse,
    api_remote_artifacts_clone,
    api_remote_artifacts_fork,
)
