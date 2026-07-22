"""Slack Socket Mode event routing.

Sets up the Socket Mode client, dispatches incoming events to the
correct handler:

- ``interactive`` → :mod:`interactions.dispatch`
- ``slash_commands`` → registry-based sub-command routing
- ``member_joined_channel`` → tracking-channel allowlist prompt
- ``app_home_opened`` → publish Home Tab view
- ``message`` / ``app_mention`` → :func:`handler.handle_message`

Also contains the bounded dedup cache (``SeenCache``) that prevents
processing the same Slack event twice.

This module was split for maintainability into focused submodules; it is now a
thin re-export shim that preserves the original public surface:

- :mod:`kiro_crew.slack.events_core` — shared state, ``SeenCache``, the
  slash-command registry, ``_safe_log`` and ``init_socket_mode``
- :mod:`kiro_crew.slack.events_slash` — ``/kirocrew`` slash-command handlers
- :mod:`kiro_crew.slack.events_hometab` — App Home tab renderer
- :mod:`kiro_crew.slack.events_message` — message routing / dedup / transcription

Importing this module (or ``events_slash``) fires the built-in slash-command
registrations.
"""

from __future__ import annotations

import logging

# Import submodules for their registration/log-config side effects and so the
# whole ``events`` surface is reachable via this shim.
from kiro_crew.slack import (  # noqa: F401
    events_core,
    events_hometab,
    events_message,
    events_slash,
)
from kiro_crew.slack.events_core import (  # noqa: F401
    _MAX_SEEN,
    SLASH_REGISTRY,
    SeenCache,
    SlashHandler,
    _background_tasks,
    _bg_tasks,
    _build_help_text,
    _get_skills_loader,
    _on_tracked_done,
    _safe_log,
    _skills_loader,
    _spawn_tracked,
    init_socket_mode,
    register_slash_command,
)
from kiro_crew.slack.events_hometab import _publish_home_tab  # noqa: F401
from kiro_crew.slack.events_message import (  # noqa: F401
    _AUDIO_MIMETYPES,
    _MAX_RECOVERED_TEXT_CHARS,
    _SLACK_BLOCK_FALLBACKS,
    _dispatch_queued,
    _extract_blocks_text,
    _extract_shared_text,
    _handle_message_deleted,
    _normalize_message_blocks,
    _render_rich_text_element,
    _resolve_approval_mode,
    _route_message,
    _transcribe_files,
    _transcribe_with_reaction,
)
from kiro_crew.slack.events_slash import (  # noqa: F401
    _get_agent_names,
    _handle_agent,
    _handle_allowlist_cmd,
    _handle_channel_cmd,
    _handle_config,
    _handle_dashboard,
    _handle_restart,
    _handle_sessions,
    _handle_slash,
    _handle_status,
    _handle_voice,
    _handle_yolo,
    _maybe_prompt_owner,
)

# Module logger (parity with the pre-split module).
logger = logging.getLogger(__name__)
