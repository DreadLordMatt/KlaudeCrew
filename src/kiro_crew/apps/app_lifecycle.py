"""App lifecycle helpers — version check, lifecycle scripts, builtin service sync.

Extracted from ``routes.py`` (LOC split). Imported by ``crud`` and re-exported
from ``routes`` so historical ``from kiro_crew.apps.routes import X`` keeps working.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.apps.manager import apps_dir
from kiro_crew.apps.registry import minimal_env
from kiro_crew.apps.version import check_min_version as _check_min_version_str
from kiro_crew.config.loader import config_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Version compatibility check
# ---------------------------------------------------------------------------


def _check_min_version(manifest_data: dict[str, Any]) -> str | None:
    """Check if the app requires a newer KiroCrew version.

    Returns an error message if the current version is too old, or None if OK.
    """
    return _check_min_version_str(manifest_data.get("minKiroCrewVersion", ""))


# ---------------------------------------------------------------------------
# Lifecycle script helper
# ---------------------------------------------------------------------------

async def _run_lifecycle_script(
    app_name: str,
    script: str,
    *,
    timeout: int = 30,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a lifecycle script (onEnable/onDisable/onUpdate/onUninstall) in the app directory.

    Returns dict with ``output`` (str) and ``failed`` (bool).
    """
    app_root = apps_dir() / app_name
    if not app_root.is_dir():
        return {"output": f"app directory not found: {app_root}", "failed": True}

    safe_script = f"set -euo pipefail\n{script}"
    from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv
    base_cmd = ["/bin/bash", "-c", safe_script]
    sandboxed_cmd, cleanup = wrap_argv(base_cmd, mode="standard")
    sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling (Talos bdf0d7e5)
    env = minimal_env(NONINTERACTIVE="1")
    if extra_env:
        env.update(extra_env)
    try:
        # Process-group isolation for timeout tree-kill. Pass both flags explicitly
        # (NOT **dict unpack — breaks mypy's Popen overload resolution on the build
        # fleet): start_new_session=True is a no-op on Windows, creationflags is 0
        # (no-op) on POSIX. (App lifecycle scripts are bash; on Windows without bash
        # they fail gracefully rather than crash here.)
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_cmd,
            cwd=str(app_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            preexec_fn=resource_limit_preexec(),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = (stdout or b"").decode(errors="replace").strip()
            lines = output.split("\n")
            output = "\n".join(lines[-20:])  # last 20 lines
        except asyncio.TimeoutError:
            try:
                # killpg on POSIX, taskkill /T on Windows — via platform_compat.
                platform_compat.kill_process_tree(proc.pid, platform_compat.SIGTERM)
            except OSError:
                proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return {"output": f"script timed out after {timeout}s", "failed": True}
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass

    failed = bool(proc.returncode and proc.returncode != 0)
    return {"output": output, "failed": failed}


# ---------------------------------------------------------------------------
# Builtin app helpers — sync config.json and stop/start live services
# ---------------------------------------------------------------------------


def _redact_warning(msg: str) -> str:
    """Redact credentials and exfiltration URLs from warning strings."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls
    msg, _ = redact_credentials(msg)
    msg, _ = redact_exfiltration_urls(msg)
    return msg


# Maps builtin app names to their config.json key and dashboard state
# restart callback attribute.  Only apps with a live gateway service
# (not just metadata) need entries here.  Empty in the open-source build —
# no bundled builtin ships a live gateway service.
_BUILTIN_SERVICE_APPS: dict[str, tuple[str, str]] = {}


def _sync_builtin_config(name: str, *, enabled: bool) -> None:
    """Update config.json for a builtin app so gateway reads the right state on restart."""
    cfg_key, _ = _BUILTIN_SERVICE_APPS.get(name, (None, None))
    if cfg_key is None:
        return
    path = config_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError) as exc:
        raise OSError(f"Could not read config.json: {exc}") from exc
    section = data.setdefault(cfg_key, {})
    section["enabled"] = enabled
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.chmod(0o600)
        tmp.rename(path)
    except OSError:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    logger.info("Synced config.json %s.enabled = %s", cfg_key, enabled)


async def _notify_builtin_service(request: web.Request, name: str) -> str | None:
    """Stop/start a builtin service via its dashboard restart callback.

    Returns None on success, or a warning string on failure.
    The restart callback re-reads config.json, so calling _sync_builtin_config
    first ensures the service picks up the new enabled state.
    """
    _, restart_attr = _BUILTIN_SERVICE_APPS.get(name, (None, None))
    if restart_attr is None:
        return None
    state = request.app.get("state")
    if state is None:
        return "no gateway state available — restart gateway to apply"
    restart_fn = getattr(state, restart_attr, None)
    if restart_fn is None:
        return "no restart callback available — restart gateway to apply"
    try:
        result = await restart_fn()
        if result == "ok" or result == "init returned without service":
            return None
        return f"restart returned: {result}"
    except Exception as exc:
        logger.warning("Builtin service restart failed for %s: %s", name, exc)
        return f"restart failed: {exc}"
