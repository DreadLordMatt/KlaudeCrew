"""File I/O, outbox, upload, workspace CRUD, and file search handlers.

Split from the former single ``files.py`` module into feature-area submodules.
``upload`` is imported eagerly so its import-time OOXML ``mimetypes.add_type``
registrations still fire when this package is imported (regression guard for
.docx/.xlsx/.pptx Content-Type detection).
"""

from __future__ import annotations

from ._shared import _sel
from .browse import (
    _fuzzy_score,
    _validate_dashboard_path,
    api_browse_dirs,
    api_browse_files,
    api_file_diff,
    api_file_download,
    api_file_raw,
    api_file_read,
    api_file_search,
    api_file_watch,
    api_file_write,
)
from .outbox import (
    _INLINE_DISPOSITION_PREFIXES,
    api_outbox_download,
    api_outbox_list,
    api_outbox_notify,
    api_reveal_path,
    api_slack_upload_file,
)
from .upload import (
    _ALLOWED_DOC_EXT,
    _ALLOWED_IMAGE_EXT,
    _ALLOWED_TEXT_EXT,
    _MAGIC_PREFIXES,
    _MAX_UPLOAD_BYTES,
    _MAX_UPLOAD_FILES,
    _SCREENSHOT_DIR,
    _UPLOAD_DIR,
    _ZIP_CONTAINER_EXTS,
    _content_matches_ext,
    _write_file_restricted,
    api_screenshot,
    api_upload,
    api_upload_file,
)
from .workspaces import (
    api_dashboard_config,
    api_workspaces,
    api_workspaces_create,
    api_workspaces_delete,
    api_workspaces_update,
)

__all__ = [
    "_ALLOWED_DOC_EXT",
    "_ALLOWED_IMAGE_EXT",
    "_ALLOWED_TEXT_EXT",
    "_INLINE_DISPOSITION_PREFIXES",
    "_MAGIC_PREFIXES",
    "_MAX_UPLOAD_BYTES",
    "_MAX_UPLOAD_FILES",
    "_SCREENSHOT_DIR",
    "_UPLOAD_DIR",
    "_ZIP_CONTAINER_EXTS",
    "_content_matches_ext",
    "_fuzzy_score",
    "_sel",
    "_validate_dashboard_path",
    "_write_file_restricted",
    "api_browse_dirs",
    "api_browse_files",
    "api_dashboard_config",
    "api_file_diff",
    "api_file_download",
    "api_file_raw",
    "api_file_read",
    "api_file_search",
    "api_file_watch",
    "api_file_write",
    "api_outbox_download",
    "api_outbox_list",
    "api_outbox_notify",
    "api_reveal_path",
    "api_screenshot",
    "api_slack_upload_file",
    "api_upload",
    "api_upload_file",
    "api_workspaces",
    "api_workspaces_create",
    "api_workspaces_delete",
    "api_workspaces_update",
]
