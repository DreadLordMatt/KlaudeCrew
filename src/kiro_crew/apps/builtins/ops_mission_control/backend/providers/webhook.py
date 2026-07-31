"""Inbound webhook signal source — the escape hatch for everything else.

Any system that can POST JSON becomes a signal source: Grafana, Prometheus
Alertmanager, Sentry, a cron job, a custom script. This is what keeps the app from
being limited to the four providers we happened to implement.

Security shape, which is the interesting part:

- Deliveries land on the **authenticated gateway surface**
  (``/api/apps/ops-mission-control/webhook``). We ship no public ingress and no
  tunnel; exposing the gateway is the operator's decision, documented as such.
- Every delivery must carry an HMAC-SHA256 signature over the raw body, keyed by a
  secret held in the keystone store and compared with ``hmac.compare_digest``. No
  secret configured means the endpoint refuses everything — fail-closed, so
  enabling the adapter cannot accidentally open an unauthenticated write path into
  the incident board.
- Accepted deliveries are spooled to a bounded queue that the heartbeat drains, so
  a delivery burst cannot outrun the dispatch loop or grow without limit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections import deque
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    STATE_FIRING,
    Signal,
    normalize_severity,
    utc_now_iso,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import provider_enabled
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import (
    get_secret,
    has_secrets,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "webhook"

_SECRET_SIGNING_KEY = "signing_secret"
_REQUIRED_SECRETS: tuple[str, ...] = (_SECRET_SIGNING_KEY,)

#: Header carrying the hex HMAC-SHA256 of the raw request body.
SIGNATURE_HEADER = "X-OMC-Signature"

#: Bounded spool. A delivery burst must not grow memory without limit; oldest
#: entries are dropped, which is the right trade because the providers that fan
#: out fastest are also the ones that re-deliver.
MAX_QUEUED_SIGNALS = 200

#: Cap on an accepted body. Anything larger is refused before parsing.
MAX_BODY_BYTES = 256 * 1024

_queue: deque[Signal] = deque(maxlen=MAX_QUEUED_SIGNALS)


def verify_signature(raw_body: bytes, provided: str) -> bool:
    """Constant-time HMAC check over the raw body.

    Fail-closed: no configured secret, or no provided signature, means reject. An
    unauthenticated path that can inject incidents would let anyone who can reach
    the port manufacture work and drive the agent's attention.
    """
    secret = get_secret(PROVIDER_ID, _SECRET_SIGNING_KEY)
    if not secret or not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip().lower())


#: Cap on label pairs kept from one delivery. Labels reach the model's context and
#: the fingerprint, so an unbounded map is both a token cost and a way to bloat the
#: dispatch index from outside.
MAX_LABELS = 50


def _normalize_labels(raw: Any) -> dict[str, str]:
    """Coerce a payload's ``labels`` to ``dict[str, str]``, or ``{}``.

    Guards the type BEFORE calling ``.items()``. The previous version put the
    ``isinstance`` check in a comprehension's ``if`` clause — which is evaluated per
    item, after ``.items()`` had already been called on the raw value — so
    ``{"labels": "text"}`` raised ``AttributeError``. That escaped ``enqueue``'s
    ``except`` (which only covers JSON/Unicode errors) and 500-ed the ingress, so a
    correctly-signed sender could crash the endpoint with one malformed field.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if len(out) >= MAX_LABELS:
            break
        out[str(key)[:100]] = str(value)[:200]
    return out


def signal_from_payload(payload: dict[str, Any]) -> Signal | None:
    """Map a webhook body onto a Signal.

    Accepts a small, documented envelope and is deliberately strict about it: a
    body with no title has nothing a human could act on, so it is refused rather
    than turned into an unreadable board row.
    """
    title = str(payload.get("title") or payload.get("summary") or "").strip()
    if not title:
        return None
    native_id = str(payload.get("id") or payload.get("fingerprint") or title)[:200]
    return Signal.create(
        source=PROVIDER_ID,
        native_id=native_id,
        title=title,
        severity=normalize_severity(str(payload.get("severity", ""))),
        state=STATE_FIRING,
        fired_at=str(payload.get("fired_at") or utc_now_iso()),
        resource=str(payload.get("resource", ""))[:200],
        url=str(payload.get("url", ""))[:500],
        labels=_normalize_labels(payload.get("labels")),
    )


def enqueue(raw_body: bytes, signature: str) -> tuple[bool, str]:
    """Verify and queue a delivery. Returns ``(accepted, detail)``."""
    if not provider_enabled(PROVIDER_ID):
        return False, "webhook source is not enabled"
    if not has_secrets(PROVIDER_ID, _REQUIRED_SECRETS):
        return False, "no signing secret configured"
    if len(raw_body) > MAX_BODY_BYTES:
        return False, "body too large"
    if not verify_signature(raw_body, signature):
        return False, "signature mismatch"
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, "malformed JSON"
    if not isinstance(payload, dict):
        return False, "payload must be a JSON object"
    signal = signal_from_payload(payload)
    if signal is None:
        return False, "payload has no title"
    _queue.append(signal)
    return True, signal.id


def drain() -> list[Signal]:
    """Remove and return every queued signal (called by the heartbeat)."""
    drained = list(_queue)
    _queue.clear()
    return drained


def queue_depth() -> int:
    return len(_queue)


class WebhookSignalSource:
    """Drains the webhook spool into the dispatch loop."""

    id = PROVIDER_ID
    display_name = "Inbound webhook"
    detail = "Any system that can POST signed JSON — Grafana, Alertmanager, Sentry, scripts."
    config_fields: tuple[str, ...] = ("enabled",)
    secret_fields: tuple[str, ...] = _REQUIRED_SECRETS

    def configured(self) -> bool:
        return provider_enabled(PROVIDER_ID) and has_secrets(PROVIDER_ID, _REQUIRED_SECRETS)

    async def poll(self) -> list[Signal]:
        if not self.configured():
            return []
        return drain()
