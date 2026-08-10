"""Discovering a running gateway's port, and proving the listener is ours.

Split out of :mod:`kiro_crew.cli_server` so that clients which must not import
the CLI's dependency tree can reuse the *same* ownership proof. The MCP tool
server is the motivating caller: it sends the local internal secret on every
gateway callback, so it needs the full verification chain below — not a bare
"something is listening there" check — before it may trust a discovered port.

Deliberately a leaf: stdlib plus :mod:`kiro_crew.platform_compat`,
:mod:`kiro_crew.instances.run_marker` and the stdlib-only
:mod:`kiro_crew.dashboard.urls`.
"""

from __future__ import annotations

import logging
import os
import shlex
import urllib.parse
from collections.abc import Callable

from kiro_crew import platform_compat
from kiro_crew.dashboard.urls import _DEFAULT_PORT, parse_dashboard_url
from kiro_crew.instances import run_marker

_KIROCREW_SERVER_SUBCOMMANDS = frozenset({"gateway", "dashboard", "start"})


def _basename_stem(tok: str) -> str:
    """Basename of *tok* without a Windows ``.exe`` suffix.

    Lets the venv launchers ``python.exe`` / ``kirocrew.exe`` match the same
    checks as their POSIX ``python`` / ``kirocrew`` counterparts. ``shlex.split``
    with ``posix=False`` leaves quotes on some tokens, so strip them too.

    Split on BOTH separators explicitly rather than via ``os.path.basename``:
    that is host-dependent (``posixpath`` on Linux does NOT split backslashes),
    so a Windows cmdline classified on the Linux CI fleet would keep its full
    ``D:\\...\\kirocrew.exe`` path and never match. This is host-independent — a
    basename is whatever follows the last ``/`` or ``\\``.

    Module scope rather than nested in :func:`_args_look_like_kirocrew` so
    :func:`_own_console_script` shares the one definition.
    """
    cleaned = tok.strip('"')
    base = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base


def _args_look_like_kirocrew(args: str) -> bool:
    """Return ``True`` if a process command-line *args* string is a Kiro Crew server.

    This gates the SIGTERM sent by :func:`_stop`, so it must be
    **precise** (never match an unrelated process that merely mentions
    "kirocrew") while still recognising *every* way the gateway can be spawned.

    Instead of enumerating brittle substring variants (``kiro_crew.gateway`` vs
    ``kiro_crew gateway`` vs ``kirocrew gateway`` …), we parse the command line
    *structurally* and key on the real module/binary name plus a known server
    subcommand (:data:`_KIROCREW_SERVER_SUBCOMMANDS`). This is deterministic and
    robust to interpreter path, Python version suffix, and whitespace. Two spawn
    shapes are recognised:

    * **Module invocation** — ``<python> -m kiro_crew <subcmd>`` (the form used by
      a service install and the launchd/systemd service), plus the legacy dotted
      form ``<python> -m kiro_crew.<subcmd>``. A Python interpreter must precede
      ``-m`` so we don't misread some other tool's ``-m`` flag (e.g. ``grep -m``).
    * **Console script** — ``/path/to/kirocrew <subcmd>`` (used when the
      ``kirocrew`` wrapper resolves on ``PATH``).

    Examples::

        >>> _args_look_like_kirocrew("/x/python3.10 -m kiro_crew gateway")
        True
        >>> _args_look_like_kirocrew("python3 -m kiro_crew.dashboard")
        True
        >>> _args_look_like_kirocrew("~/.local/bin/kirocrew start")
        True
        >>> _args_look_like_kirocrew("python -m kiro_crew run /tmp/spec.md")  # task runner
        False
        >>> _args_look_like_kirocrew("vim /tmp/kirocrew-notes.txt")
        False
    """
    # ``ps -o args=`` (POSIX) / Win32_Process.CommandLine (Windows WMI) return a
    # shell-style string; tokenize it the way the host shell would. On Windows
    # use posix=False so backslash path separators survive (default posix=True
    # eats them: ``C:\Py\python.exe`` -> ``C:Pypython.exe``, breaking the
    # interpreter/basename checks below). Fall back to a naive split on a
    # malformed string (e.g. an odd quote) so this best-effort check never raises.
    try:
        tokens = shlex.split(args, posix=not platform_compat.IS_WINDOWS)
    except ValueError:
        tokens = args.split()

    for index, token in enumerate(tokens):
        # --- Module form: "<python> -m kiro_crew <subcmd>" / "-m kiro_crew.<subcmd>"
        if token == "-m" and index + 1 < len(tokens):
            # Only treat "-m" as Python's module flag when a Python interpreter
            # precedes it; otherwise an unrelated tool's "-m" option could be
            # misread (e.g. "grep -m kiro_crew gateway file"). The match is
            # case-insensitive: the macOS framework build's interpreter basename
            # is "Python" (capital P), which a case-sensitive startswith would
            # miss, leaving stop/restart unable to find the gateway.
            interpreter_seen = any(
                _basename_stem(t).lower().startswith("python") for t in tokens[:index]
            )
            if interpreter_seen:
                # "kiro_crew.gateway" -> ("kiro_crew", "gateway"); a bare
                # "kiro_crew" -> ("kiro_crew", "").
                package, _, dotted_subcmd = tokens[index + 1].partition(".")
                if package == "kiro_crew":
                    # Dotted submodule form: ``-m kiro_crew.gateway``.
                    if dotted_subcmd in _KIROCREW_SERVER_SUBCOMMANDS:
                        return True
                    # Subcommand-as-argument form: ``-m kiro_crew gateway``. The
                    # subcommand is argparse's first positional after the module,
                    # i.e. always at index+2. Check only that slot so a later
                    # positional/flag value cannot match — e.g.
                    # ``-m kiro_crew run gateway`` ("gateway" is a file argument
                    # to the task runner) must NOT be treated as a server.
                    if (
                        index + 2 < len(tokens)
                        and tokens[index + 2] in _KIROCREW_SERVER_SUBCOMMANDS
                    ):
                        return True

        # --- Console-script form: ".../kirocrew <subcmd>" (or kirocrew.exe on Win)
        if (
            _basename_stem(token) == "kirocrew"
            and index + 1 < len(tokens)
            and tokens[index + 1] in _KIROCREW_SERVER_SUBCOMMANDS
        ):
            return True

    return False


def _is_kirocrew_process(pid: int) -> bool:
    """Return ``True`` if *pid* looks like a Kiro Crew gateway process.

    Resolves the process command line cross-platform via
    :func:`platform_compat.process_command_line` (Linux ``/proc``, macOS ``ps``,
    Windows ``Win32_Process`` WMI — the venv ``kirocrew.exe`` re-execs
    ``python.exe`` so the image name alone is ambiguous there) and defers
    classification to :func:`_args_look_like_kirocrew`.

    ``process_command_line`` returns ``""`` on any failure (dead PID, missing
    ``ps``, WMI error), which classifies as "not a match" — _stop()'s separate
    ``listening_pid_tool_available()`` check already surfaces the tool-absent
    case, so this never needs to raise.
    """
    out = platform_compat.process_command_line(pid)
    if not out:
        return False
    return _args_look_like_kirocrew(out)


def _gateway_owns_port(port: int) -> bool:
    """True only when *this user's* gateway process is listening on *port*.

    Reachability is not enough to trust a discovered port. Client commands hand
    the local secret to whatever answers (``_token`` and ``_logout`` send
    ``X-Local-Secret``), and ``clear_marker`` runs only on graceful shutdown —
    so a crashed gateway leaves a marker naming a port some unrelated process
    may since have bound. A bare TCP connect would walk the secret straight into
    that process, which could then mint owner tokens against the real gateway.

    A command-line check is not enough either: argv is attacker-chosen, so a
    listener launched as ``/tmp/kirocrew gateway`` would pass it. The proof used
    here is an identity the attacker cannot forge, in three parts:

    1. **Recorded pid** — ``run_marker.read_pid(port)`` reads the sidecar the
       gateway wrote at ``0600`` inside the ``0700`` ``run/`` dir. Another local
       user cannot write it, so they cannot nominate a process of theirs.
    2. **Holds the port** — that pid must be among
       ``platform_compat.find_listening_pids(port)``. This is what makes a stale
       recorded pid harmless: it has to actually hold the port we are about to
       send the secret to.
    3. **Owned by us, and ours** — the pid's uid must equal the caller's
       (``process_owner_uid``), and its argv must look like a gateway. The uid
       check is what closes pid *recycling* into a foreign user's process; argv
       remains only as defense in depth, never as the sole proof.

    A same-user attacker is out of scope by construction: they can already read
    ``.local_secret`` (mode ``0600``, their own uid), so nothing here can be an
    escalation for them. The boundary this closes is a *different* local user.

    **Fails closed** at every step: no sidecar, no recorded pid, a pid that does
    not hold the port, an unresolvable uid, a missing lookup tool
    (``find_listening_pids`` folds that into an empty list) or a throwing one —
    all deny, and discovery is skipped in favour of the documented default.
    ``--port`` and ``KIROCREW_PORT`` remain available on such hosts.

    **Non-POSIX denies outright.** ``process_owner_uid`` cannot report an owner
    on Windows, and a home that is writable by another user (a shared or
    misconfigured ``KIROCREW_HOME``) would let them replace both the marker and
    the sidecar with a forged listener — the file-permission argument that
    carries step 1 is exactly what stops holding there. Rather than trust
    steps 1-2 alone, discovery is skipped: Windows users keep ``--port`` /
    ``KIROCREW_PORT``, which is precisely where they were before this fallback
    existed, so nothing regresses. This is the one place the feature is
    deliberately unavailable rather than approximated.
    """
    if not platform_compat.IS_POSIX:
        return False
    recorded = run_marker.read_pid(port)
    if recorded is None:
        return False
    try:
        pids = platform_compat.find_listening_pids(port)
    except Exception:
        return False
    if recorded not in pids:
        return False
    owner = platform_compat.process_owner_uid(recorded)
    if owner is None or owner != platform_compat.local_user_id():
        return False
    return _is_kirocrew_process(recorded)


def _marker_port(*, on_ambiguous: Callable[[list[int]], None] | None = None) -> int | None:
    """Port of the sole gateway-owned run-marker, or ``None``.

    Zero-configuration discovery for the common single-gateway box: the gateway
    already advertises itself by writing ``<data-home>/run/gateway-<port>.bin``
    (see :mod:`kiro_crew.instances.run_marker`), so a client with no ``--port``,
    no ``KIROCREW_PORT`` and no port in ``dashboard.url`` can read that instead
    of assuming 5476 and connecting to a dead port.

    Two guards keep this from being a guess:

    * **Ownership.** Only ports where a verified Kiro Crew gateway process is
      listening count (:func:`_gateway_owns_port`); a stale marker, or one whose
      port has been taken over by an unrelated process, is discarded.
    * **Ambiguity.** With several gateways up there is no basis to pick one, so
      this refuses (returns ``None``, landing on the documented default) and
      tells the user on stderr which ports it saw and how to name one.
    """
    try:
        candidates = run_marker.marker_ports()
    except Exception:
        return None
    if not candidates:
        return None
    owned = [p for p in candidates if _gateway_owns_port(p)]
    if len(owned) == 1:
        return owned[0]
    if len(owned) > 1 and on_ambiguous is not None:
        on_ambiguous(owned)
    return None


def url_port(url: object) -> int | None:
    """Port explicitly written in a ``dashboard.url`` value, or ``None``.

    ``parse_dashboard_url`` substitutes the default when a URL names no port, so
    its return value alone cannot distinguish "the user chose 5476" from "nothing
    was configured" — which is the difference that decides whether marker
    discovery may run. The value is user-editable JSON and core installs may lack
    jsonschema, so a non-string (``"url": 123``) must not raise: urlparse raises
    TypeError on one, which is not a ValueError.
    """
    if not isinstance(url, str):
        if url is not None:
            logging.getLogger(__name__).warning(
                "Ignoring non-string dashboard.url of type %s", type(url).__name__
            )
        return None
    if not url:
        return None
    try:
        _host, port = parse_dashboard_url(url)
        explicit = urllib.parse.urlsplit(url if "://" in url else f"http://{url}").port
    except (TypeError, ValueError):
        return None
    return port if explicit is not None else None


def env_port() -> int | None:
    """``KIROCREW_PORT`` when it holds a valid integer, else ``None``."""
    raw = os.environ.get("KIROCREW_PORT")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def resolve_port(url: object, *, on_ambiguous: Callable[[list[int]], None] | None = None) -> int:
    """The dashboard port to talk to: explicit choice, then discovery, then default.

    Single owner of the precedence chain so a tool call and a CLI command cannot
    drift apart: ``KIROCREW_PORT``, then a port spelled in ``dashboard.url``, then
    the sole gateway-owned run marker, then :data:`_DEFAULT_PORT`. Callers that
    also honour a ``--port`` flag apply it before calling this.
    """
    chosen = env_port()
    if chosen is not None:
        return chosen
    chosen = url_port(url)
    if chosen is not None:
        return chosen
    discovered = _marker_port(on_ambiguous=on_ambiguous)
    if discovered is not None:
        return discovered
    return _DEFAULT_PORT


def _dial_host(port: int) -> str | None:
    """Host literal to dial for a *discovered* port, or ``None`` when unknown.

    Ownership is proven against a port NUMBER — ``find_listening_pids`` is
    family-blind — so the address family is a separate fact that has to be read off
    the verified process. Dialing a hard-coded family instead reaches whatever else
    holds that port on the other one: with the gateway on ``[::1]:7788`` an IPv4
    dial lands on another user's listener, and pinning IPv6 merely inverts the hole.

    A wildcard bind answers on both, so IPv4 loopback is used for it. Returns
    ``None`` when the address cannot be established (no lsof, Windows, a race that
    dropped the socket), and callers must then refuse the discovered port rather
    than guess — the documented default is the safe landing.
    """
    pid = run_marker.read_pid(port)
    if pid is None:
        return None
    hosts = platform_compat.listening_host_literals(pid, port)
    if not hosts:
        return None
    # A wildcard bind arrives already resolved to the loopback of its own family,
    # so IPv4 is preferred only when the gateway actually holds that side.
    if "127.0.0.1" in hosts:
        return "127.0.0.1"
    if "::1" in hosts:
        return "[::1]"
    return None


def resolve_dial_target(
    url: object, *, on_ambiguous: Callable[[list[int]], None] | None = None
) -> tuple[str | None, int]:
    """``(host, port)`` for a loopback client, where ``host`` may be ``None``.

    ``None`` means "nothing was discovered, and nothing was configured either" —
    the caller keeps whatever host it used before (``localhost``), because with no
    ownership proof there is no verified address to prefer. A discovered port always
    comes with the address its verified owner is bound to, or is discarded.
    """
    chosen = env_port()
    if chosen is not None:
        return None, chosen
    chosen = url_port(url)
    if chosen is not None:
        return None, chosen
    discovered = _marker_port(on_ambiguous=on_ambiguous)
    if discovered is not None:
        host = _dial_host(discovered)
        if host is not None:
            return host, discovered
    return None, _DEFAULT_PORT
