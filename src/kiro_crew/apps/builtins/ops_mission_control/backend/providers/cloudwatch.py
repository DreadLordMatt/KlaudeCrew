"""AWS CloudWatch adapters — alarms as signals, logs and metrics as evidence.

**No credential is ever stored for AWS.** This adapter uses the ambient
credential chain — the user's existing profile, assumed role, or instance role —
which is the direct application of ARCC's "IAM roles over keys" guidance. The app
does not accept, persist, or transmit an AWS access key, so there is no AWS
credential in the threat model at all.

Required read-only permissions, which the user attaches to their own principal:

    cloudwatch:DescribeAlarms, cloudwatch:GetMetricStatistics
    logs:StartQuery, logs:GetQueryResults, logs:DescribeLogGroups

No write permission is requested. Resolving a CloudWatch alarm is not something
this app does — it resolves *work items* in trackers, through ``ActionSink``.

``boto3`` is an **optional lazy import** (matching the existing STT precedent): the
module must import cleanly without it and report unconfigured, so a user who never
touches AWS pays nothing and sees no error.
"""

from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType
from typing import Any, Mapping

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    STATE_FIRING,
    Signal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    config_flag,
    config_list,
    config_value,
    provider_enabled,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    DEFAULT_POLL_LIMIT,
    Evidence,
    EvidenceBudget,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "cloudwatch"

#: Alarm state that constitutes work. ``INSUFFICIENT_DATA`` is deliberately NOT
#: included: it usually means a metric stopped reporting, which is real but
#: produces enormous noise on any account with idle resources. Users who want it
#: opt in via ``include_insufficient_data``.
_ALARM_STATE_ALARM = "ALARM"
_ALARM_STATE_INSUFFICIENT = "INSUFFICIENT_DATA"

#: How far back log evidence looks. Long enough to cover the alarm's evaluation
#: window, short enough that a Logs Insights query stays cheap.
_LOG_LOOKBACK_MINUTES = 30

#: Log lines kept per query. The evidence budget also caps total bytes; this caps
#: rows so one chatty log group cannot crowd out every other source.
_LOG_LINE_LIMIT = 40

#: Poll interval for a Logs Insights query to finish, and its ceiling.
_LOG_POLL_INTERVAL_SECS = 1.0
_LOG_MAX_WAIT_SECS = 25.0


def _boto3_client(service: str, region: str) -> Any | None:
    """Build a boto3 client, or ``None`` when boto3/credentials are unavailable.

    Lazy import by design: ``boto3`` is an optional dependency, and a user with no
    AWS setup must not see an import error — the adapter simply reports
    unconfigured.
    """
    try:
        import boto3  # noqa: PLC0415 — optional lazy import
    except ImportError:
        logger.debug("ops-mission-control: boto3 not installed; cloudwatch unavailable")
        return None
    try:
        kwargs: dict[str, Any] = {}
        if region:
            kwargs["region_name"] = region
        # Prefer the evidence namespace when it is set, so the ``profile`` field the
        # evidence adapter advertises is actually honored; otherwise fall back to the
        # signal source's, which is where a single-account install configures it.
        profile = config_value(EVIDENCE_PROVIDER_ID, "profile") or config_value(
            PROVIDER_ID, "profile"
        )
        if profile:
            session = boto3.Session(profile_name=profile, **kwargs)
            return session.client(service)
        return boto3.client(service, **kwargs)
    except Exception:  # noqa: BLE001 — missing/expired credentials, bad profile, …
        logger.exception("ops-mission-control: failed to build %s client", service)
        return None


def _severity_for(alarm: dict[str, Any]) -> str:
    """Derive severity from the alarm itself.

    CloudWatch has no severity concept, so we read one from an ``omc:severity``
    tag-style dimension if present and otherwise infer from the alarm name. This
    is a heuristic and is documented as such in the skill — a user who cares sets
    the dimension.
    """
    name = str(alarm.get("AlarmName", "")).lower()
    for dimension in alarm.get("Dimensions") or []:
        if str(dimension.get("Name", "")).lower() in {"omc:severity", "severity"}:
            return str(dimension.get("Value", ""))
    if any(token in name for token in ("critical", "sev1", "p1", "pager", "urgent")):
        return SEVERITY_CRITICAL
    return SEVERITY_WARNING


class CloudWatchSignalSource:
    """CloudWatch alarms in ALARM state, as signals."""

    id = PROVIDER_ID
    display_name = "AWS CloudWatch"
    detail = (
        "Alarms in ALARM state. Uses your ambient AWS credentials — no key is stored. "
        "Set include_insufficient_data to also catch alarms whose metric STOPPED "
        "reporting (a pipeline that silently stopped running looks healthy otherwise) "
        "— off by default because it is noisy on accounts with idle resources."
    )
    config_fields: tuple[str, ...] = (
        "enabled",
        "region",
        "profile",
        "alarm_name_prefix",
        "alarm_names",
        "include_insufficient_data",
    )
    secret_fields: tuple[str, ...] = ()

    def configured(self) -> bool:
        return provider_enabled(PROVIDER_ID)

    async def poll(self) -> list[Signal]:
        if not self.configured():
            return []
        return await asyncio.to_thread(self._poll_sync)

    def _poll_sync(self) -> list[Signal]:
        region = config_value(PROVIDER_ID, "region")
        client = _boto3_client("cloudwatch", region)
        if client is None:
            return []

        states = [_ALARM_STATE_ALARM]
        if config_flag(PROVIDER_ID, "include_insufficient_data"):
            states.append(_ALARM_STATE_INSUFFICIENT)

        signals: list[Signal] = []
        for state in states:
            kwargs: dict[str, Any] = {"StateValue": state, "MaxRecords": DEFAULT_POLL_LIMIT}
            prefix = config_value(PROVIDER_ID, "alarm_name_prefix")
            if prefix:
                kwargs["AlarmNamePrefix"] = prefix
            names = config_list(PROVIDER_ID, "alarm_names")
            if names:
                kwargs["AlarmNames"] = names[:100]
                kwargs.pop("AlarmNamePrefix", None)
            try:
                response = client.describe_alarms(**kwargs)
            except Exception:  # noqa: BLE001 — surfaced as per-source health
                logger.exception("ops-mission-control: describe_alarms failed")
                return signals

            for alarm in response.get("MetricAlarms", []):
                name = str(alarm.get("AlarmName", ""))
                if not name:
                    continue
                namespace = str(alarm.get("Namespace", ""))
                metric = str(alarm.get("MetricName", ""))
                resource = f"{namespace}/{metric}" if namespace or metric else name
                updated = alarm.get("StateUpdatedTimestamp")
                signals.append(
                    Signal.create(
                        source=PROVIDER_ID,
                        native_id=f"alarm/{name}",
                        title=str(alarm.get("AlarmDescription") or name),
                        severity=_severity_for(alarm),
                        state=STATE_FIRING,
                        fired_at=updated.strftime("%Y-%m-%dT%H:%M:%SZ") if updated else "",
                        resource=resource,
                        url=self._console_url(region, name),
                        labels={
                            "alarm_name": name,
                            "namespace": namespace,
                            "metric": metric,
                            "region": region,
                            "state": state,
                        },
                    )
                )
        return signals

    @staticmethod
    def _console_url(region: str, alarm_name: str) -> str:
        if not region:
            return ""
        from urllib.parse import quote

        return (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#alarmsV2:alarm/{quote(alarm_name)}"
        )


#: The evidence adapter's own config namespace. It advertises ``config_fields``, so
#: the Settings UI writes to ``providers["cloudwatch-evidence"]`` — but the gather
#: code read ``providers["cloudwatch"]``, so ``log_groups`` (which exists ONLY on this
#: adapter) could never be set through the UI at all: whatever the operator typed
#: landed where nothing looked for it, and log evidence was silently always empty.
EVIDENCE_PROVIDER_ID = "cloudwatch-evidence"


def _evidence_value(field: str) -> str:
    """Read a field from the evidence namespace, falling back to the signal one.

    The fallback keeps ``region`` / ``profile`` working for an install that already
    configured them on ``cloudwatch`` (the common case — one AWS account serves both
    adapters), while ``log_groups`` now resolves where the UI actually writes it.
    """
    value = config_value(EVIDENCE_PROVIDER_ID, field)
    return value if value else config_value(PROVIDER_ID, field)


def _evidence_list(field: str) -> list[str]:
    values = config_list(EVIDENCE_PROVIDER_ID, field)
    return values if values else config_list(PROVIDER_ID, field)


class CloudWatchEvidenceSource:
    """Alarm history and recent log lines, as investigation evidence."""

    id = EVIDENCE_PROVIDER_ID
    display_name = "AWS CloudWatch evidence"
    detail = "Alarm history plus recent matching log lines."
    config_fields: tuple[str, ...] = ("enabled", "region", "profile", "log_groups")

    #: What this adapter needs, clamped by the operator's ceiling in
    #: ``EvidenceBudget.for_source``. Logs Insights is submit-then-poll rather than a
    #: single request, so it wants longer than a plain REST adapter — the reason
    #: ``_LOG_MAX_WAIT_SECS`` existed at all. It can still only ever NARROW the
    #: operator's value, never raise it.
    #: A MappingProxy, not a dict: a mutable class attribute shared across every
    #: instance is one accidental ``hint["timeout_secs"] = 300`` away from an adapter
    #: rewriting its own ceiling at runtime.
    evidence_budget_hint: Mapping[str, float] = MappingProxyType(
        {"timeout_secs": _LOG_MAX_WAIT_SECS}
    )
    secret_fields: tuple[str, ...] = ()

    def configured(self) -> bool:
        # Either namespace enabling it is enough: the signal source and this adapter
        # share one AWS account, and requiring a second toggle for the same account
        # would make evidence silently absent for anyone who enabled only CloudWatch.
        return provider_enabled(EVIDENCE_PROVIDER_ID) or provider_enabled(PROVIDER_ID)

    async def gather(self, signal: Signal, budget: EvidenceBudget) -> list[Evidence]:
        if not self.configured() or signal.source != PROVIDER_ID:
            return []
        return await asyncio.to_thread(self._gather_sync, signal, budget)

    def _gather_sync(self, signal: Signal, budget: EvidenceBudget) -> list[Evidence]:
        region = _evidence_value("region")
        out: list[Evidence] = []
        calls = 0

        alarm_name = signal.labels.get("alarm_name", "")
        if alarm_name and calls < budget.max_calls:
            client = _boto3_client("cloudwatch", region)
            calls += 1
            if client is not None:
                try:
                    history = client.describe_alarm_history(AlarmName=alarm_name, MaxRecords=10)
                    lines = [
                        f"{item.get('Timestamp')} {item.get('HistorySummary', '')}"
                        for item in history.get("AlarmHistoryItems", [])
                    ]
                    if lines:
                        out.append(
                            Evidence(
                                source=self.id,
                                kind="alarm_history",
                                title=f"Alarm history — {alarm_name}",
                                body="\n".join(lines),
                            )
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("ops-mission-control: alarm history failed")

        log_groups = _evidence_list("log_groups")
        for group in log_groups:
            if calls >= budget.max_calls:
                break
            calls += 1
            body = self._query_logs(region, group, budget)
            if body:
                out.append(
                    Evidence(
                        source=self.id,
                        kind="logs",
                        title=f"Recent errors — {group}",
                        body=body,
                    )
                )
        return out

    def _query_logs(self, region: str, log_group: str, budget: EvidenceBudget) -> str:
        """Run a bounded Logs Insights query for recent error-ish lines."""
        import time

        client = _boto3_client("logs", region)
        if client is None:
            return ""
        try:
            now = int(time.time())
            start = client.start_query(
                logGroupName=log_group,
                startTime=now - _LOG_LOOKBACK_MINUTES * 60,
                endTime=now,
                queryString=(
                    "fields @timestamp, @message "
                    "| filter @message like /(?i)(error|exception|timeout|fail)/ "
                    "| sort @timestamp desc "
                    f"| limit {_LOG_LINE_LIMIT}"
                ),
            )
            query_id = start.get("queryId")
            if not query_id:
                return ""
            waited = 0.0
            budget_wait = min(_LOG_MAX_WAIT_SECS, budget.timeout_secs)
            while waited < budget_wait:
                result = client.get_query_results(queryId=query_id)
                status = str(result.get("status", ""))
                if status == "Complete":
                    rows = result.get("results", [])
                    lines = [
                        " ".join(str(f.get("value", "")) for f in row if f.get("field") != "@ptr")
                        for row in rows
                    ]
                    return "\n".join(lines)[: budget.max_bytes]
                if status in {"Failed", "Cancelled", "Timeout"}:
                    return ""
                time.sleep(_LOG_POLL_INTERVAL_SECS)
                waited += _LOG_POLL_INTERVAL_SECS
            # Out of budget: stop the query rather than leaving it running and
            # billable after we have stopped caring about the answer.
            try:
                client.stop_query(queryId=query_id)
            except Exception:  # noqa: BLE001
                logger.debug("ops-mission-control: stop_query failed", exc_info=True)
            return ""
        except Exception:  # noqa: BLE001
            logger.exception("ops-mission-control: logs query failed for %r", log_group)
            return ""
