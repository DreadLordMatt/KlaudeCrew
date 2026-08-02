"""Datadog adapters — monitors as signals, metrics as evidence, mute as action.

Datadog needs two secrets (an API key and an application key), which is why
``_REQUIRED_SECRETS`` has two entries and ``configured()`` demands both: a user who
enters only the API key gets "unconfigured" rather than a confusing 403 on every
poll.

The site is configurable (``datadoghq.com``, ``datadoghq.eu``, ``ddog-gov.com``,
…) because Datadog's API host is region-specific and a hardcoded US host silently
fails for every EU customer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    ACTION_COMMENT,
    ACTION_RESOLVE,
    ACTION_SILENCE,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    STATE_FIRING,
    Signal,
    resolve_silence_secs,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    config_list,
    config_value,
    provider_enabled,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    DEFAULT_POLL_LIMIT,
    ActionResult,
    Evidence,
    EvidenceBudget,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.http import (
    HttpError,
    request_json,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import (
    get_secret,
    has_secrets,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "datadog"

_SECRET_API_KEY = "api_key"
_SECRET_APP_KEY = "app_key"
_REQUIRED_SECRETS: tuple[str, ...] = (_SECRET_API_KEY, _SECRET_APP_KEY)

#: Datadog's API host is region-specific; a hardcoded US host breaks every
#: non-US customer, so the site is config with a US default.
_DEFAULT_SITE = "datadoghq.com"

#: Monitor states that constitute open work. ``No Data`` is excluded for the same
#: reason CloudWatch's INSUFFICIENT_DATA is: high volume, low signal on any
#: account with idle resources.
_OPEN_MONITOR_STATES: frozenset[str] = frozenset({"Alert", "Warn"})

_STATE_SEVERITY: dict[str, str] = {
    "Alert": SEVERITY_CRITICAL,
    "Warn": SEVERITY_WARNING,
    "No Data": SEVERITY_INFO,
}

#: Evidence window for metric queries, in seconds.
_METRIC_LOOKBACK_SECS = 3600

#: Datadog's mute endpoint takes an optional ``end`` epoch and treats its ABSENCE as
#: "mute forever" — so posting ``body={}`` (what this adapter used to do) silenced a
#: production monitor indefinitely, and the only way back was a human noticing and
#: unmuting by hand. The window is now always sent, bounded by the shared
#: ``resolve_silence_secs`` so every provider's suppression obeys one ceiling rather
#: than each adapter inventing its own.


def _api_base() -> str:
    site = config_value(PROVIDER_ID, "site") or _DEFAULT_SITE
    return f"https://api.{site}"


def _headers() -> dict[str, str]:
    return {
        "DD-API-KEY": get_secret(PROVIDER_ID, _SECRET_API_KEY),
        "DD-APPLICATION-KEY": get_secret(PROVIDER_ID, _SECRET_APP_KEY),
    }


class DatadogAdapter:
    """SignalSource + ActionSink over the Datadog monitors API."""

    id = PROVIDER_ID
    display_name = "Datadog"
    detail = "Monitors in Alert/Warn as signals; mute and comment as actions."
    config_fields: tuple[str, ...] = ("enabled", "site", "monitor_tags", "monitor_ids")
    secret_fields: tuple[str, ...] = _REQUIRED_SECRETS

    def configured(self) -> bool:
        return provider_enabled(PROVIDER_ID) and has_secrets(PROVIDER_ID, _REQUIRED_SECRETS)

    # -- SignalSource ------------------------------------------------------

    async def poll(self) -> list[Signal]:
        if not self.configured():
            return []
        return await asyncio.to_thread(self._poll_sync)

    def _poll_sync(self) -> list[Signal]:
        params: dict[str, Any] = {"page_size": DEFAULT_POLL_LIMIT}
        tags = config_list(PROVIDER_ID, "monitor_tags")
        if tags:
            params["monitor_tags"] = ",".join(tags)
        ids = config_list(PROVIDER_ID, "monitor_ids")
        if ids:
            params["id"] = ",".join(ids)

        data = request_json(f"{_api_base()}/api/v1/monitor", headers=_headers(), params=params)
        monitors = data if isinstance(data, list) else []

        signals: list[Signal] = []
        for monitor in monitors:
            if not isinstance(monitor, dict):
                continue
            state = str(monitor.get("overall_state", ""))
            if state not in _OPEN_MONITOR_STATES:
                continue
            monitor_id = str(monitor.get("id", ""))
            if not monitor_id:
                continue
            monitor_tags = monitor.get("tags")
            signals.append(
                Signal.create(
                    source=PROVIDER_ID,
                    native_id=f"monitor/{monitor_id}",
                    title=str(monitor.get("name", "") or f"monitor {monitor_id}"),
                    severity=_STATE_SEVERITY.get(state, SEVERITY_WARNING),
                    state=STATE_FIRING,
                    fired_at=str(monitor.get("overall_state_modified", "")),
                    resource=str(monitor.get("query", ""))[:200],
                    url=self._monitor_url(monitor_id),
                    # Datadog's own monitor id — the identity Datadog groups by, and
                    # already carried in labels for the action path.
                    provider_key=f"monitor/{monitor_id}" if monitor_id else "",
                    labels={
                        "dd_monitor_id": monitor_id,
                        "monitor_state": state,
                        "tags": (
                            ",".join(str(t) for t in monitor_tags)
                            if isinstance(monitor_tags, list)
                            else ""
                        ),
                    },
                )
            )
        return signals

    @staticmethod
    def _monitor_url(monitor_id: str) -> str:
        site = config_value(PROVIDER_ID, "site") or _DEFAULT_SITE
        return f"https://app.{site}/monitors/{monitor_id}"

    # -- ActionSink --------------------------------------------------------

    def supported_actions(self) -> frozenset[str]:
        # Datadog monitors are muted, not "resolved" — a monitor clears when its metric
        # recovers, so there is nothing here to close.
        #
        # ``silence`` is now the honest name for that, and it is the preferred verb.
        # ``resolve`` is kept as an alias onto the same bounded mute for the operators and
        # rules that already grant it: silently dropping it would revoke a granted
        # capability on upgrade, and refusing it would break a working allowlist. Both
        # paths now always carry an expiry.
        return frozenset({ACTION_SILENCE, ACTION_RESOLVE, ACTION_COMMENT})

    async def execute(self, signal: Signal, action: str, payload: dict[str, Any]) -> ActionResult:
        if not self.configured():
            return ActionResult(ok=False, action=action, error="datadog is not configured")
        monitor_id = signal.labels.get("dd_monitor_id", "")
        if not monitor_id:
            return ActionResult(ok=False, action=action, error="signal carries no Datadog id")
        return await asyncio.to_thread(self._execute_sync, monitor_id, action, payload)

    def _execute_sync(self, monitor_id: str, action: str, payload: dict[str, Any]) -> ActionResult:
        try:
            if action in (ACTION_SILENCE, ACTION_RESOLVE):
                # ALWAYS send an ``end``. Datadog reads a missing ``end`` as "mute
                # forever", so the previous ``body={}`` traded a firing monitor for a
                # permanently silent one — strictly worse than doing nothing, because
                # the board shows the incident resolved while the metric is still bad.
                duration = resolve_silence_secs(payload.get("duration_secs"))
                end_epoch = int(time.time()) + duration
                request_json(
                    f"{_api_base()}/api/v1/monitor/{monitor_id}/mute",
                    method="POST",
                    headers=_headers(),
                    body={"end": end_epoch},
                )
                return ActionResult(
                    ok=True,
                    action=action,
                    detail=(
                        f"datadog monitor {monitor_id} muted for {duration // 60}m "
                        f"(expires at epoch {end_epoch}; Datadog clears on recovery)"
                    ),
                )
            request_json(
                f"{_api_base()}/api/v1/events",
                method="POST",
                headers=_headers(),
                body={
                    "title": f"Ops Mission Control note — monitor {monitor_id}",
                    "text": str(payload.get("note", ""))[:2000],
                    "tags": [f"monitor_id:{monitor_id}", "source:ops-mission-control"],
                },
            )
        except HttpError as exc:
            return ActionResult(ok=False, action=action, error=str(exc))
        return ActionResult(ok=True, action=action, detail=f"datadog {action} {monitor_id}")


class DatadogEvidenceSource:
    """Recent monitor state history, as investigation evidence."""

    id = "datadog-evidence"
    display_name = "Datadog evidence"
    detail = "Monitor state transitions over the last hour."
    config_fields: tuple[str, ...] = ("enabled", "site")
    secret_fields: tuple[str, ...] = ()

    def configured(self) -> bool:
        return provider_enabled(PROVIDER_ID) and has_secrets(PROVIDER_ID, _REQUIRED_SECRETS)

    async def gather(self, signal: Signal, budget: EvidenceBudget) -> list[Evidence]:
        if not self.configured() or signal.source != PROVIDER_ID:
            return []
        return await asyncio.to_thread(self._gather_sync, signal, budget)

    def _gather_sync(self, signal: Signal, budget: EvidenceBudget) -> list[Evidence]:
        import time

        monitor_id = signal.labels.get("dd_monitor_id", "")
        if not monitor_id:
            return []
        now = int(time.time())
        try:
            data = request_json(
                f"{_api_base()}/api/v1/monitor/{monitor_id}/downtimes",
                headers=_headers(),
                params={"from_ts": now - _METRIC_LOOKBACK_SECS, "to_ts": now},
            )
        except HttpError as exc:
            logger.debug("ops-mission-control: datadog evidence failed: %s", exc)
            return []
        if not data:
            return []
        return [
            Evidence(
                source=self.id,
                kind="monitor_context",
                title=f"Datadog context — monitor {monitor_id}",
                body=str(data)[: budget.max_bytes],
                url=DatadogAdapter._monitor_url(monitor_id),
            )
        ]
