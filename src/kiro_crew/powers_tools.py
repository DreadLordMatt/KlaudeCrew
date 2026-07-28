"""Agent-facing Powers tools — the read-only half of the pull activation model.

The activation design is recorded in ``docs/system-specs/modules/powers.md``
under *Deferred: activation (pull model)*.  Its shape is taken from the Kiro
IDE's own implementation: the agent **pulls** a Power's capabilities through
KiroCrew-owned tools, and nothing about a Power is pushed into agent
configuration or agent context.

This module implements the two tools that never start a process:

``power_list``
    What is installed, with each Power's declared MCP **server names**.

``power_steering``
    The contents of one ``steering/*.md`` file, on demand.

The two that DO start a process — ``power_learn`` (inventory a Power's tools)
and ``power_use`` (dispatch one call) — are deliberately not here.  They need
the consent prompt, the sandboxed spawn and the per-invocation audit described
in the spec, and shipping them alongside these would repeat the mistake that
made the original combined PR unreviewable.

WHY THESE TWO NEED NO CONSENT PROMPT
------------------------------------
Both are reads of content the user chose to install, performed only when the
agent explicitly asks for a named Power.  Nothing executes, and no third-party
text enters a session unrequested — which is precisely the property the push
design could not offer, because it injected guidance through ``SkillsLoader``
keyword triggers whether or not the agent wanted it.  Consent attaches to
execution, where the risk is.

What this module does NOT weaken from the install-is-inert contract: it never
reads a ``command`` out of ``mcp.json``.  ``power_list`` parses that file for
its server *names* only, so the boundary moves from "never parsed" to "parsed
for names, never for commands", and the argv path stays absent until the spawn
tools land.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew.config.paths import powers_dir
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.powers import (
    MCP_JSON_NAME,
    STEERING_DIR_NAME,
    PowersStore,
    is_safe_power_name,
)
from kiro_crew.powers_providers.redact import redact_external, redact_payload
from kiro_crew.sel import sel

# A steering file is markdown and nothing else.  Mirrors the IDE's own rule
# (`readPowerSteering`: `.md` only, no separators, no leading dot), reached
# independently here before that implementation was read.
_STEERING_SUFFIX = ".md"

# Upper bound on one steering file. Exceeding it is a REFUSAL, not a truncation
# (see `power_steering`).  A Power's guidance is third-party
# text on its way into a transcript, so it is bounded for the same reason the
# install path bounds `POWER.md`: an unbounded read is a context-exhaustion
# lever held by whoever wrote the bundle.
MAX_STEERING_BYTES = 128 * 1024

# Bound on `mcp.json` when read for server names.  The file is only parsed for
# its keys, but parsing happens before any size is known, so the read is capped.
_MAX_MCP_JSON_BYTES = 256 * 1024


class PowerToolError(Exception):
    """A Powers tool refused the call. Message is safe to return to the agent."""


def _store() -> PowersStore:
    return PowersStore()


def _declared_server_names(name: str) -> list[str]:
    """Return the MCP server names a Power declares, or [] when it declares none.

    Reads ``mcp.json`` for its top-level ``mcpServers`` keys and nothing else.
    The command, args and env of each server are deliberately NOT read here:
    those belong to the spawn path, and keeping them out of this module means a
    Knowledge-vs-MCP label and a server list can be produced without the process
    that would run them ever being described in memory.
    """
    root = powers_dir() / name
    path = root / MCP_JSON_NAME
    try:
        raw = safe_read_file_bytes_nolink(
            str(path), within_root=str(root), max_bytes=_MAX_MCP_JSON_BYTES
        )
    except FileTooLargeError:
        return []
    except OSError:
        return []
    if raw is None:
        # Refused by the gate — hardlinked, non-regular, escaping the Power's own
        # directory, or a sensitive path. Report no servers rather than guessing.
        return []
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A malformed bundle is not a listing failure: the Power still exists
        # and is still removable. Report no servers rather than breaking the
        # whole list, which is what an exception here would do.
        return []
    servers = parsed.get("mcpServers") if isinstance(parsed, dict) else None
    if not isinstance(servers, dict):
        return []
    return sorted(str(key) for key in servers)


def power_list(*, installed_only: bool = True) -> dict[str, Any]:
    """List installed Powers with their declared MCP server names.

    *installed_only* exists for forward compatibility with the registry-backed
    listing the dashboard already serves; only ``True`` is implemented, and any
    other value is refused rather than silently ignored.
    """
    if not installed_only:
        raise PowerToolError(
            "power_list currently reports installed Powers only; browse the "
            "registry from the dashboard's Powers surface"
        )
    store = _store()
    powers: list[dict[str, Any]] = []
    for record in store.list_powers():
        name = str(record.get("name", ""))
        powers.append(
            {
                "name": name,
                "displayName": record.get("displayName") or name,
                "description": record.get("description", ""),
                "kind": record.get("kind", ""),
                "keywords": record.get("keywords", []),
                "mcpServers": _declared_server_names(name),
                "steeringFiles": record.get("steeringFiles", []),
            }
        )
    sel().log_api_access(
        caller="agent",
        operation="power_list",
        outcome="ok",
        resources=f"count={len(powers)}",
    )
    # Redacted on the way out for the same reason the dashboard listing is: every
    # string here originates in a third-party bundle.
    return {"powers": redact_payload(powers)}


def power_steering(power: str, file: str) -> dict[str, Any]:
    """Return the contents of one ``steering/*.md`` file from an installed Power.

    Refuses anything that is not a plain markdown leaf inside that Power's
    steering directory.  The name and the filename are validated separately
    because they are separately attacker-influenced: the Power name comes from a
    bundle's own ``POWER.md`` and the filename comes from the agent, which may be
    repeating text it read from third-party content.
    """
    if not is_safe_power_name(power):
        raise PowerToolError(f"invalid power name: {power!r}")
    leaf = str(file).strip()
    if not leaf or leaf != leaf.strip("/\\"):
        raise PowerToolError(f"invalid steering file name: {file!r}")
    if "/" in leaf or "\\" in leaf or leaf.startswith("."):
        raise PowerToolError(
            f"steering file must be a plain file name, not a path: {file!r}"
        )
    if not leaf.endswith(_STEERING_SUFFIX):
        raise PowerToolError(
            f"only {_STEERING_SUFFIX} files can be read from steering/: {file!r}"
        )

    store = _store()
    if store.load_power(power) is None:
        raise PowerToolError(f"power is not installed: {power!r}")

    # `_power_path` is the single validated barrier for every name-derived path in
    # the store: it re-checks the name, refuses a symlink at the Power's root and
    # confines the result lexically. Reusing it here keeps one implementation of
    # that rule rather than a second, weaker one in this module.
    root = store._power_path(power)
    steering_root = root / STEERING_DIR_NAME
    # The DIRECTORY is a redirectable component too, and checking only the leaf
    # missed it: a symlinked `steering` sends every read inside it somewhere else,
    # and the files found there are ordinary regular files that `O_NOFOLLOW` on
    # the leaf has no objection to. The install path pins the source directory for
    # exactly this reason; that reasoning applies unchanged to reads.
    if steering_root.is_symlink():
        raise PowerToolError(
            f"steering directory is a symlink and cannot be read: {power!r}"
        )
    if not steering_root.is_dir():
        raise PowerToolError(f"power has no steering directory: {power!r}")
    target = steering_root / leaf
    if target.parent != steering_root:
        raise PowerToolError(f"steering file escapes the power directory: {file!r}")
    if target.is_symlink():
        raise PowerToolError(f"steering file is a symlink and cannot be read: {file!r}")

    # Existence is checked before the gate so "no such file" stays distinguishable
    # from "refused": the gate returns None for both, and collapsing them would tell
    # an agent that a missing file was a security refusal.
    if not target.is_file():
        raise PowerToolError(f"no such steering file: {file!r}")
    try:
        raw = safe_read_file_bytes_nolink(
            str(target), within_root=str(steering_root), max_bytes=MAX_STEERING_BYTES
        )
    except FileTooLargeError as exc:
        # Refused, not truncated: guidance cut off mid-document and presented as
        # the Power's guidance is worse than an explicit refusal.
        raise PowerToolError(
            f"steering file exceeds the {MAX_STEERING_BYTES // 1024} KiB cap: {file!r}"
        ) from exc
    except OSError as exc:
        raise PowerToolError(f"steering file is unreadable: {file!r}") from exc
    if raw is None:
        # The gate refuses hardlinked inodes, non-regular files, anything whose
        # opened descriptor resolves outside the Power's steering directory, and
        # sensitive paths — and fails closed when it cannot verify containment.
        raise PowerToolError(
            f"steering file was refused by the file-safety gate: {file!r}"
        )

    text = raw.decode("utf-8", errors="replace")
    sel().log_api_access(
        caller="agent",
        operation="power_steering",
        outcome="ok",
        resources=f"{power}/{leaf} bytes={len(raw)}",
    )
    return {
        "power": power,
        "file": leaf,
        # Third-party markdown, redacted before it reaches the transcript.
        "content": redact_external(text),
    }
