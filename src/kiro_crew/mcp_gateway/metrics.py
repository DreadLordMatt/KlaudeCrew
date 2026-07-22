"""OTEL metric emitters for the MCP gateway backend-acquire / lazy-load paths.

Split out of :mod:`kiro_crew.mcp_gateway.gatewayd` (LOC refactor). The metric
names / attrs live here in production code — shared by the ensure_backend +
lazy-spawn paths and their unit tests so the tests drive real production code
instead of duplicating the metric shape.
"""

from __future__ import annotations

import logging

from kiro_crew.metrics.provider import get_recorder

logger = logging.getLogger(__name__)


def _emit_backend_acquire_metric(acquire_ms: float, *, warm: bool) -> None:
    """Emit kirocrew.mcp.backend.acquire.duration (best-effort).

    Shared by the ensure_backend + lazy-spawn paths and their unit tests so the
    metric name / attrs live in production, not duplicated in the test
    (tests must drive real production code).
    """
    try:
        get_recorder().histogram(
            "kirocrew.mcp.backend.acquire.duration",
            acquire_ms,
            unit="ms",
            attrs={"warm": warm},
        )
    except Exception:  # telemetry must never break the gateway hot path
        logger.debug("backend.acquire metric emit failed", exc_info=True)


def _emit_lazy_load_metrics(elapsed_ms: float, *, warm: bool) -> None:
    """Emit MCP lazy-load count + duration (+ backend.acquire), best-effort.

    Shared by the lazy-spawn path and its unit test.
    """
    try:
        rec = get_recorder()
        rec.counter("kirocrew.mcp.lazy_load.count", attrs={"transport": "stdio"})
        rec.histogram(
            "kirocrew.mcp.lazy_load.duration",
            elapsed_ms,
            unit="ms",
            attrs={"transport": "stdio"},
        )
    except Exception:  # telemetry must never break the gateway hot path
        logger.debug("lazy_load metric emit failed", exc_info=True)
    _emit_backend_acquire_metric(elapsed_ms, warm=warm)
