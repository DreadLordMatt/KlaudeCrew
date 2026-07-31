"""On-call rotation from a committed schedule file — no rotation service required.

A team that has PagerDuty already has a rotation API. This adapter is for everyone
else: the owner's ask was "assuming we do not have oncall service we just can keep
oncall schedule in that repository and for auth we can use github logins". So the
schedule is a YAML file that lives in the SAME git repo the knowledge ledger syncs
through (``ledger_sync``), and identity is the operator's GitHub login.

Why a file in git rather than a service:

- **It is already synced.** The ledger repo is the team's shared memory; a rotation
  is the same kind of small, slow-changing, human-edited fact. Reusing that transport
  means no second integration, no second credential, and the schedule arrives on the
  same pull that brings teammates' lessons.
- **It is reviewable.** A shift swap is a diff with an author and a timestamp. That is
  a better audit trail than most rotation UIs give you, and it is the same reason the
  ledger is JSONL in git rather than rows in a private DB.
- **It degrades correctly.** Every failure — missing file, malformed YAML, unresolvable
  login, clock outside every window — resolves to ``unknown=True``, which the tier gate
  treats as ARMED. See ``ShiftStatus.unknown``: wrongly disarming a team costs missed
  incidents, while wrongly arming costs API polls in a tier that only *observes*.

**This adapter never decides authority, only tier arming.** Being on shift arms the
``on_shift`` cron tier; it does not raise the autonomy mode. ``effective = min(app_mode,
rule_mode)`` still governs every action, so a schedule file — which any teammate can
edit and push — cannot escalate what the agent may DO. That separation is deliberate:
the schedule is shared, mutable, and only as trustworthy as the repo's write access, so
it is wired to the cheap decision (when to look) and not the expensive one (what to do).

Schedule format (``rotation.yaml`` at the repo root)::

    timezone: America/Los_Angeles     # optional; UTC when absent
    shifts:
      - from: 2026-08-01
        to: 2026-08-08
        who: octocat                  # a GitHub login
      - from: 2026-08-08T09:00
        to: 2026-08-15T09:00
        who: [octocat, hubot]         # co-primary is allowed

See ``docs/system-specs/modules/ops-mission-control.md`` § Rotation.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    config_value,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import ShiftStatus
from kiro_crew.sandbox import resource_limit_preexec, sandboxed_spawn_argv

logger = logging.getLogger(__name__)

PROVIDER_ID = "schedule-file"

#: Filename inside the synced ledger repo. Fixed rather than configurable: the whole
#: point is that every teammate's install reads the SAME file, and a per-install
#: filename would let two members disagree about where the rotation lives.
SCHEDULE_FILENAME = "rotation.yaml"

#: Config key holding this operator's GitHub login. Optional — when absent we resolve
#: it from the local ``gh`` CLI, which is where the owner wanted identity to come from.
_CONFIG_LOGIN = "github_login"

#: Cap on the schedule file we will parse. A rotation is tens of lines; anything
#: megabyte-scale is a mistake or a hostile push, and YAML parsing is not free.
MAX_SCHEDULE_BYTES = 256 * 1024

#: Cap on shift entries considered. A year of daily shifts is 365; 5000 is far above
#: any real rotation and bounds the scan a pushed file can cost us.
MAX_SHIFTS = 5000

_GH_TIMEOUT_SECS = 10.0

#: Cached GitHub login. Resolving shells out to ``gh``, and ``on_shift`` runs on every
#: rotation-check cron tick — an unbounded re-shell per tick is a needless process
#: spawn. The login does not change within a gateway lifetime in any realistic flow.
_login_cache: str | None = None


def schedule_path() -> Path:
    """Where the schedule lives: beside the ledger, inside the synced repo."""
    return ledger.ledger_path().parent / SCHEDULE_FILENAME


def _resolve_login_sync() -> str:
    """This operator's GitHub login, from config or the local ``gh`` CLI.

    Routed through ``sandboxed_spawn_argv`` + ``resource_limit_preexec`` like every
    other agent-reachable spawn in this app — the ``test/test_spawn_audit.py`` gate
    requires that chokepoint, and a rotation check is reachable from a cron an agent
    can trigger. Returns "" on any failure; the caller turns that into ``unknown``.
    """
    configured = (config_value(PROVIDER_ID, _CONFIG_LOGIN) or "").strip()
    if configured:
        return configured

    global _login_cache
    if _login_cache is not None:
        return _login_cache

    argv, env, cleanup = sandboxed_spawn_argv(["gh", "api", "user", "--jq", ".login"])
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, sandbox-routed
            argv,
            capture_output=True,
            timeout=_GH_TIMEOUT_SECS,
            env=env,
            preexec_fn=resource_limit_preexec(),
        )
        login = proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        logger.debug("ops-mission-control: could not resolve a GitHub login via gh", exc_info=True)
        login = ""
    finally:
        # A temp-profile PATH, not a callable — the same shape ledger_sync documents.
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)

    # Cache the miss too: a machine with no gh must not re-shell every tick.
    _login_cache = login
    return login


def reset_login_cache() -> None:
    """Forget the resolved login. For tests and for an operator who just ran ``gh auth``."""
    global _login_cache
    _login_cache = None


def _tzinfo(name: str) -> timezone | Any:
    """Resolve an IANA name, falling back to UTC.

    A bad or unavailable timezone must not fail the whole rotation check — UTC gives a
    defined answer, and the shift windows are usually day-granular anyway. Windows ships
    no system tz database, which is why ``tzdata`` is a declared Windows dependency;
    this still degrades rather than raising if the lookup fails.
    """
    if not name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — unknown zone, missing tzdata, bad type
        logger.debug("ops-mission-control: unknown rotation timezone %r; using UTC", name)
        return timezone.utc


def _parse_moment(raw: Any, tz: Any, *, end: bool) -> datetime | None:
    """Parse a ``from``/``to`` value into an aware datetime.

    Accepts ``YYYY-MM-DD`` and ``YYYY-MM-DDTHH:MM``. A DATE-only ``to`` is treated as
    the END of that day, not midnight at its start: a human writing ``to: 2026-08-08``
    means "through the 8th", and reading it as 00:00 would silently drop the last day of
    every shift written that way. That off-by-one-day is the single most likely way this
    file gets misread, so it is handled here rather than left to the operator.
    """
    if isinstance(raw, datetime):
        moment = raw
    elif isinstance(raw, str):
        text = raw.strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            logger.debug("ops-mission-control: unparseable rotation moment %r", raw)
            return None
        if end and len(text) == 10:  # bare YYYY-MM-DD
            moment = moment + timedelta(days=1)
    else:
        # PyYAML parses a bare date into datetime.date; accept it via isoformat.
        iso = getattr(raw, "isoformat", None)
        if not callable(iso):
            return None
        return _parse_moment(iso(), tz, end=end)

    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment


def _whos(entry: dict[str, Any]) -> list[str]:
    """The logins on a shift. Accepts a scalar or a list (co-primary is legitimate)."""
    raw = entry.get("who", entry.get("login", entry.get("logins")))
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def read_schedule() -> tuple[list[dict[str, Any]], str, str]:
    """Parse the schedule file. Returns ``(shifts, timezone_name, error)``.

    Never raises: a malformed schedule that a teammate pushed must degrade to
    ``unknown`` (tier armed), not crash the rotation-check cron for everyone.
    """
    path = schedule_path()
    try:
        if not path.exists():
            return [], "", f"no {SCHEDULE_FILENAME} in the synced ledger repo"
        if path.stat().st_size > MAX_SCHEDULE_BYTES:
            return [], "", f"{SCHEDULE_FILENAME} exceeds {MAX_SCHEDULE_BYTES} bytes"
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], "", f"could not read {SCHEDULE_FILENAME}: {exc}"

    try:
        import yaml  # type: ignore[import-untyped]

        # safe_load, never load: this file arrives over `git pull` from a shared repo,
        # so it is untrusted input by construction and must not be able to construct
        # arbitrary Python objects.
        data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 — any YAML fault degrades to unknown
        return [], "", f"{SCHEDULE_FILENAME} is not valid YAML: {str(exc)[:200]}"

    if not isinstance(data, dict):
        return [], "", f"{SCHEDULE_FILENAME} must be a YAML mapping"

    tz_name = str(data.get("timezone", "") or "")
    shifts = data.get("shifts")
    if not isinstance(shifts, list):
        return [], tz_name, f"{SCHEDULE_FILENAME} has no 'shifts' list"

    entries = [s for s in shifts[:MAX_SHIFTS] if isinstance(s, dict)]
    if len(shifts) > MAX_SHIFTS:
        # Say so rather than silently scanning a prefix — a truncated rotation would
        # look like "nobody is on shift" for everyone past the cut.
        logger.warning(
            "ops-mission-control: %s lists %d shifts; only the first %d are considered",
            SCHEDULE_FILENAME,
            len(shifts),
            MAX_SHIFTS,
        )
    return entries, tz_name, ""


def resolve_now(now: datetime | None = None) -> ShiftStatus:
    """Who is on shift right now, per the committed schedule.

    Synchronous and injectable (``now``) so the window arithmetic is testable without
    freezing the clock globally.
    """
    shifts, tz_name, error = read_schedule()
    if error:
        # Unknown, not off-shift: see the module docstring on fail-open.
        return ShiftStatus(on_shift=True, unknown=True)

    tz = _tzinfo(tz_name)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    me = _resolve_login_sync()
    if not me:
        logger.debug("ops-mission-control: no GitHub login resolved; rotation is unknown")
        return ShiftStatus(on_shift=True, unknown=True)

    for entry in shifts:
        start = _parse_moment(entry.get("from"), tz, end=False)
        end = _parse_moment(entry.get("to"), tz, end=True)
        if start is None or end is None or end <= start:
            continue
        if not (start <= moment < end):
            continue
        logins = _whos(entry)
        # Case-insensitive: GitHub logins are case-insensitive, and a schedule written
        # "Octocat" against a `gh` login of "octocat" must not read as off-shift.
        if any(login.lower() == me.lower() for login in logins):
            return ShiftStatus(on_shift=True, who=me, until=end.isoformat())
        # A window that covers now and names someone ELSE is a definitive answer: this
        # operator is off shift. Returning here rather than continuing is what makes
        # the file able to say "not you" at all.
        if logins:
            return ShiftStatus(on_shift=False, who=", ".join(logins), until=end.isoformat())

    # No window covers now. That is a real gap in the schedule (a rotation that ran
    # out, or a not-yet-filled week) — report unknown so the tier stays armed rather
    # than letting an expired file quietly disable everyone's response.
    return ShiftStatus(on_shift=True, unknown=True)


class ScheduleFileRotationSource:
    """``RotationSource`` backed by ``rotation.yaml`` in the synced ledger repo."""

    id = PROVIDER_ID
    display_name = "Schedule file (git)"
    detail = (
        "Reads rotation.yaml from the synced knowledge repo and matches your GitHub "
        "login. No rotation service required."
    )
    config_fields: tuple[str, ...] = (_CONFIG_LOGIN,)
    secret_fields: tuple[str, ...] = ()

    def configured(self) -> bool:
        # Configured means "there is a schedule to read". The login is optional (we
        # fall back to `gh`), so requiring it here would make the common case — a
        # committed file plus an already-authenticated gh — look unconfigured.
        return schedule_path().exists()

    async def on_shift(self) -> ShiftStatus:
        if not self.configured():
            return ShiftStatus(on_shift=True, unknown=True)
        # File read, YAML parse, and a possible `gh` spawn are all synchronous.
        return await asyncio.to_thread(resolve_now)


def status() -> dict[str, Any]:
    """Settings-panel status: is there a schedule, and what does it currently say?"""
    shifts, tz_name, error = read_schedule()
    shift = resolve_now()
    return {
        "provider": PROVIDER_ID,
        "path": str(schedule_path()),
        "present": schedule_path().exists(),
        "shifts": len(shifts),
        "timezone": tz_name or "UTC",
        "login": _resolve_login_sync(),
        "on_shift": shift.on_shift,
        "who": shift.who,
        "until": shift.until,
        "unknown": shift.unknown,
        "detail": error or ("on shift" if shift.on_shift and not shift.unknown else ""),
    }
