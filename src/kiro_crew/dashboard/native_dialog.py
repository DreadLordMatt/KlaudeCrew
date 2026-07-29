"""Host-side native folder dialog.

The dashboard renders in a browser (or an Electron webview), and the web
platform deliberately withholds absolute filesystem paths from a page:
``showDirectoryPicker()`` hands back a ``FileSystemDirectoryHandle`` whose only
identifying field is the leaf name, and ``<input webkitdirectory>`` reports
paths relative to the picked root. A project directory must be an absolute path
the *gateway* can resolve, so neither is usable.

The gateway itself runs on the machine whose directories the user is choosing,
so it can ask the OS for its own folder dialog and read back a real path. That
is what this module does: build an argv for the platform's native chooser, run
it, and return the selected directory.

Same-machine assumption: the caller (``handlers.files``) gates this on a direct
local request, and the gateway already drives host GUI actions this way in
``api_reveal_path`` (Finder reveal). When the gateway is genuinely remote — an
``ssh -L`` forward, a headless host — the dialog either cannot start or opens on
a screen nobody is watching, so every entry point here is timeout-bounded and
the dashboard falls back to its server-side directory browser.

Prompts are fixed literals rather than caller-supplied text: nothing user
controlled is interpolated into an AppleScript or PowerShell program, and every
subprocess runs argv-style with ``shell=False``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

#: Wall-clock ceiling for a dialog. Generous — the user is browsing their disk
#: by hand — but bounded, so a dialog opened on an unwatched screen (remote
#: gateway) eventually releases the single-flight slot instead of wedging it.
DIALOG_TIMEOUT_SEC = 300

#: Prompts, owned here rather than accepted from a request. Each is passed to
#: the chooser as *data* (an argv element or env value), never spliced into
#: program text, so the AppleScript/PowerShell bodies stay fixed literals.
PROMPT_PROJECT = "Choose a project folder"
PROMPT_KNOWLEDGE = "Select a folder to add to your knowledge base"

#: Backend identifiers returned by :func:`detect_backend`.
BACKEND_OSASCRIPT = "osascript"
BACKEND_POWERSHELL = "powershell"
BACKEND_ZENITY = "zenity"
BACKEND_KDIALOG = "kdialog"

#: Env vars carrying the prompt and default location into the PowerShell
#: chooser. Passing them through the environment instead of the command text
#: keeps the script a fixed literal (no quoting or injection surface).
_WIN_DEFAULT_ENV = "KIROCREW_DIALOG_DEFAULT"
_WIN_PROMPT_ENV = "KIROCREW_DIALOG_PROMPT"

# AppleScript, one -e line per element. Prompt and default location arrive as
# argv so the program text stays constant.
#
# `activate me` rather than `tell application "System Events" to activate`: the
# panel still needs to come forward (otherwise it can open behind the browser),
# but System Events is a faceless background app, so that Apple Event LAUNCHES
# it and brings it frontmost before `choose folder` runs — routinely most of a
# second on a cold call, which is felt as a slow-opening Finder panel.
# `activate me` raises osascript's own process instead: same effect on window
# order, no second process to start.
_OSASCRIPT_LINES = (
    "on run argv",
    "set promptText to item 1 of argv",
    "set loc to item 2 of argv",
    "activate me",
    "if loc is \"\" then",
    "set f to choose folder with prompt promptText",
    "else",
    "set f to choose folder with prompt promptText default location (POSIX file loc)",
    "end if",
    "return POSIX path of f",
    "end run",
)

# WinForms folder browser. -STA is mandatory: the dialog needs a
# single-threaded apartment and silently fails without it.
_POWERSHELL_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
    f"$d.Description = $env:{_WIN_PROMPT_ENV}; "
    "$d.ShowNewFolderButton = $true; "
    f"if ($env:{_WIN_DEFAULT_ENV}) {{ $d.SelectedPath = $env:{_WIN_DEFAULT_ENV} }}; "
    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
    "{ [Console]::Out.Write($d.SelectedPath) }"
)


class DialogUnavailable(RuntimeError):
    """No native chooser can run on this host (no binary, or no GUI session)."""


def _powershell() -> str | None:
    """Absolute path to powershell.exe, or ``None`` when it isn't usable."""
    system_root = os.environ.get("SystemRoot") or "C:\\Windows"
    candidate = os.path.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )
    if platform_compat.is_executable_file(candidate):
        return candidate
    return shutil.which("powershell")


def _has_display() -> bool:
    """Return ``True`` when an X11/Wayland session is reachable.

    Linux only. A gateway on a headless server has the chooser binaries
    installed often enough that presence alone is a bad signal.
    """
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def detect_backend() -> str | None:
    """Return the native-chooser backend for this host, or ``None``.

    ``None`` means the dashboard should keep using its server-side directory
    browser: there is no chooser to run, or no session to draw it in.
    """
    if sys.platform == "darwin":
        return BACKEND_OSASCRIPT if shutil.which("osascript") else None
    if sys.platform == "win32":
        return BACKEND_POWERSHELL if _powershell() else None
    if not _has_display():
        return None
    for name, backend in (("zenity", BACKEND_ZENITY), ("kdialog", BACKEND_KDIALOG)):
        if shutil.which(name):
            return backend
    return None


def is_available() -> bool:
    """Return ``True`` when a native folder dialog can be opened here."""
    return detect_backend() is not None


def build_command(
    backend: str, default_path: str = "", prompt: str = PROMPT_PROJECT,
) -> tuple[list[str], dict[str, str]]:
    """Return ``(argv, extra_env)`` for *backend*.

    *default_path* is the directory the chooser opens in; empty means "let the
    OS decide". Neither it nor *prompt* is ever spliced into program text — each
    travels as a separate argv element or env value.
    """
    if backend == BACKEND_OSASCRIPT:
        argv = ["osascript"]
        for line in _OSASCRIPT_LINES:
            argv += ["-e", line]
        # The `--` separator keeps a path that starts with "-" out of
        # osascript's own option parsing.
        return argv + ["--", prompt, default_path], {}
    if backend == BACKEND_POWERSHELL:
        exe = _powershell() or "powershell"
        argv = [exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", _POWERSHELL_SCRIPT]
        env = {_WIN_PROMPT_ENV: prompt}
        if default_path:
            env[_WIN_DEFAULT_ENV] = default_path
        return argv, env
    if backend == BACKEND_ZENITY:
        argv = ["zenity", "--file-selection", "--directory", f"--title={prompt}"]
        if default_path:
            # zenity treats a trailing separator as "start inside this dir".
            argv.append(f"--filename={default_path.rstrip(os.sep)}{os.sep}")
        return argv, {}
    if backend == BACKEND_KDIALOG:
        return ["kdialog", "--title", prompt,
                "--getexistingdirectory", default_path or os.path.expanduser("~")], {}
    raise DialogUnavailable(f"unknown dialog backend: {backend}")


def _is_cancellation(backend: str, returncode: int, stderr: str) -> bool:
    """Return ``True`` when a non-zero exit means "the user clicked Cancel".

    Cancel and failure share an exit status on most of these tools, so each
    backend needs its own reading. Guessing wrong in the cancel direction would
    hide real breakage; guessing wrong the other way would show the user an
    error for a normal dismissal.
    """
    if backend == BACKEND_OSASCRIPT:
        # AppleScript reports a user cancel as error -128, printed as
        # "execution error: User canceled. (-128)". Match the parenthesised
        # token, not a bare "-128" substring, so a different negative code that
        # merely contains those digits is not read as a dismissal. The numeric
        # check carries this on non-English systems, where the message won't.
        return "(-128)" in stderr or "user canceled" in stderr.lower()
    if backend in (BACKEND_ZENITY, BACKEND_KDIALOG):
        # Both exit 1 on dismissal and 255/-1 on real errors.
        return returncode == 1
    # PowerShell exits 0 whether or not the user picked something; a cancel is
    # an empty stdout, handled by the caller.
    return False


def choose_directory(default_path: str = "", prompt: str = PROMPT_PROJECT) -> str | None:
    """Open the host's native folder dialog. Blocking — call in a thread.

    Returns the chosen absolute path, or ``None`` if the user cancelled.

    Raises:
        DialogUnavailable: no chooser on this host, the chooser could not
            start, it failed, or it outran :data:`DIALOG_TIMEOUT_SEC`.
    """
    backend = detect_backend()
    if backend is None:
        raise DialogUnavailable("no native folder dialog on this host")
    argv, extra_env = build_command(backend, default_path, prompt)
    env = {**os.environ, **extra_env}
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=DIALOG_TIMEOUT_SEC, env=env, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DialogUnavailable("the folder dialog timed out") from exc
    except OSError as exc:
        raise DialogUnavailable(f"could not start the folder dialog: {exc}") from exc
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        if _is_cancellation(backend, proc.returncode, stderr):
            return None
        logger.debug("native folder dialog failed (%s): rc=%s %s", backend, proc.returncode, stderr)
        raise DialogUnavailable("the folder dialog could not be opened")
    if not stdout:
        return None  # dismissed without a selection
    # osascript's `POSIX path of` appends a separator to directories; the
    # dashboard stores project paths without one.
    return stdout.rstrip("/") or "/"
