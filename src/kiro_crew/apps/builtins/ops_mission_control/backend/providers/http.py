"""Shared HTTP helper for REST-based providers.

Uses ``urllib.request`` on a worker thread rather than adding an HTTP client
dependency — KiroCrew's convention is to prefer stdlib, and these adapters make a
handful of small JSON calls on a 2-minute cadence, which does not justify a new
third-party dep.

Two properties every caller depends on:

**Errors never carry the credential.** The auth header is built by the caller and
the raised message is scrubbed here, so a 401 body echoing back a token cannot
land in a log, a transcript, or a Slack message.

**Bodies are size-capped.** A provider returning a huge payload must not be able
to blow out memory or a model's context.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.providers import HTTP_TIMEOUT_SECS
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import redact_tokens

logger = logging.getLogger(__name__)

#: Hard cap on a provider response body. Generous for a JSON list of incidents,
#: small enough to bound memory and downstream context cost.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Only https is accepted. Provider tokens travel in these headers; permitting
#: http would let a mistyped or attacker-supplied config exfiltrate them in
#: cleartext.
_REQUIRED_SCHEME = "https"


class HttpError(RuntimeError):
    """A provider HTTP call failed. The message is always token-scrubbed."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(redact_tokens(message))
        self.status = status


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT_SECS,
) -> Any:
    """Perform a JSON HTTP request synchronously. Call via ``asyncio.to_thread``.

    Raises ``HttpError`` on a non-2xx response or a transport failure, with the
    message scrubbed of anything token-shaped.
    """
    if params:
        # doseq so list-valued params (PagerDuty's ``statuses[]``) serialize right.
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params, doseq=True)}"

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != _REQUIRED_SCHEME:
        raise HttpError(0, f"refusing non-https provider URL: {parsed.scheme or 'none'}")

    payload = json.dumps(body).encode("utf-8") if body is not None else None
    all_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        all_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=payload, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            detail = ""
        raise HttpError(exc.code, f"HTTP {exc.code}: {detail[:400]}") from None
    except urllib.error.URLError as exc:
        raise HttpError(0, f"transport error: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise HttpError(0, f"transport error: {exc}") from None

    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HttpError(0, f"malformed JSON response: {exc}") from None
