"""Backwards-compatible shim: interactions.py was split into slack submodules.

Implementation now lives in interactions_core / interactions_config /
interactions_options / interactions_sessions / interactions_review. This module
re-exports the original public surface so existing import sites and monkeypatch
targets keep resolving.
"""

from __future__ import annotations

import logging

from kiro_crew.slack.interactions_config import (  # noqa: F401
    _handle_agent_select,
    _handle_allowlist,
    _handle_allowlist_remove,
    _handle_ch_activation,
    _handle_ch_add,
    _handle_ch_agent,
    _handle_ch_remove,
    _handle_channel_remove,
    _handle_channels_select,
    _handle_config_submission,
    _handle_message_shortcut,
    _handle_shortcut_submission,
    _handle_track_channel,
    _handle_users_select,
    _handle_voice_config_submission,
    _refresh_channels_modal,
)
from kiro_crew.slack.interactions_core import (  # noqa: F401
    _ACTION_PAYLOAD_CAP,
    _FENCE_MARKER_RE,
    VIEW_REGISTRY,
    ViewHandler,
    _extract_selected_value,
    _get_forward_callback,
    _import_thread_to_slot,
    _mark_button_clicked,
    _neutralize_fence_markers,
    _orch,
    _route_action_to_session,
    ack_button,
    dispatch,
    handle_view_closed,
    handle_view_submission,
    init,
    register_view_handler,
)
from kiro_crew.slack.interactions_options import (  # noqa: F401
    _ACTION_PREFIX,
    _handle_cron_ack,
    _handle_options,
    _handle_options_submit,
    _handle_subagent_ack,
    _handle_tool_approval,
    _replace_options_blocks,
)
from kiro_crew.slack.interactions_review import (  # noqa: F401
    _REVIEW_AUTH_DENIED_MSG,
    _can_act_on_review_draft,
    _delete_review_placeholder,
    _handle_review_approve,
    _handle_review_cancel,
    _handle_review_edit,
    _handle_review_edit_submit,
    _handle_review_revise,
    _handle_review_revise_submit,
    _parse_draft_key,
    _post_review_auth_error,
)
from kiro_crew.slack.interactions_sessions import (  # noqa: F401
    _handle_inline_stop,
    _handle_resume_choice,
    _handle_session_end,
    _handle_session_new,
    _handle_session_resume,
    _handle_stop_cancel,
    _handle_stop_confirm,
    _handle_stop_kill_now,
    _resume_locks,
)

# Preserve the original module-level logger name for parity (some tests/log
# filters reference "kiro_crew.slack.interactions").
logger = logging.getLogger("kiro_crew.slack.interactions")
