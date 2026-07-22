"""HTTP transport to the local gateway for kirocrew-core MCP tools.

Owns the gateway base URL (``_API`` is resolved once here, at import, and
imported by name elsewhere so ``KiroCrewConfig.load()`` runs in exactly one
place) plus the internal-secret / user-token GET/POST/PATCH/DELETE helpers.
Imports the ``identity`` and ``governance`` leaves."""

from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlencode

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.mcp_core.governance import _resolve_session_key
from kiro_crew.mcp_core.identity import (
    _internal_secret,
    _local_user_token,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def _resolve_api_base() -> str:
    """Resolve the gateway API base URL from ``dashboard.url`` config."""
    cfg = KiroCrewConfig.load()
    _host, port = parse_dashboard_url(cfg.dashboard.url)
    return f"http://localhost:{port}"


_API = _resolve_api_base()


def _session_key_header_error(sk: str) -> str | None:
    """Return an actionable error if the session key cannot go in an HTTP header.

    http.client encodes header values as latin-1, so a non-latin-1 char in the
    session key (e.g. an em-dash from a tab title) raises UnicodeEncodeError
    before the request is sent. Detect it up front and tell the user to rename
    the tab, rather than surfacing the raw codec error (Mesh-2241).
    """
    try:
        sk.encode("latin-1")
        return None
    except UnicodeEncodeError:
        return (
            "session key contains a character invalid in HTTP headers "
            "(non-latin-1, e.g. an em-dash or emoji in the tab title) — "
            "rename the chat tab to use ASCII characters and retry"
        )


def _post(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    headers = {"Content-Type": "application/json", "X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # urlopen raises HTTPError on 4xx/5xx; str(e) is only "HTTP Error 400:
        # Bad Request" — the structured {"error": ...} body lives in e.read().
        # Surface it so callers can act on the backend's actual error (e.g.
        # the learn_add "unknown session" mapping) instead of an opaque code.
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _http_error_body(exc: urllib.error.HTTPError) -> dict:
    """Decode the JSON body of an ``HTTPError`` into the standard error dict.

    Prefers the structured ``{"error": ...}`` JSON body (so callers can match
    on the backend's actual message), then the raw body text, then
    ``str(exc)`` — so a non-JSON or empty error response still yields a usable
    ``{"error": ...}`` payload instead of an opaque ``"HTTP Error 400"``.

    An HTTP response body is content originating outside KiroCrew, so the
    decoded message is redacted (``redact_exfiltration_urls`` +
    ``redact_credentials``) before it is handed back to a caller that may echo
    it to the LLM / dashboard / Slack. Redaction leaves plain markers like
    ``"unknown session"`` intact, so downstream matching is unaffected.
    """
    try:
        raw = exc.read().decode("utf-8", "replace").strip()
    except Exception:
        raw = ""
    message = raw or str(exc)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "error" in parsed:
                message = str(parsed["error"])
        except Exception:
            pass
    message, _ = redact_exfiltration_urls(message)
    message, _ = redact_credentials(message)
    return {"error": message}


def _get(path: str) -> dict:
    headers = {"X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    req = urllib.request.Request(
        f"{_API}{path}",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _patch(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    headers = {"Content-Type": "application/json", "X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        headers=headers,
        method="PATCH",
    )
    try:
        # _API is the hardcoded loopback dashboard base and `path` is a code
        # literal — never attacker-controlled, so no file:// scheme risk.
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosemgrep  # noqa: E501
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _delete(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode() if body else None
    headers = {"X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        headers=headers,
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _with_token(path: str, token: str) -> str:
    """Append ``?token=`` (or ``&token=``) to *path* for user-token routes."""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{urlencode({'token': token})}"


def _get_user(path: str) -> dict:
    """GET a user-token-gated route (e.g. ``/api/autonudge*``).

    These routes reject ``X-Internal-Secret``; authenticate with a
    bootstrapped user token passed as the ``?token=`` query param instead.
    """
    token = _local_user_token()
    if not token:
        return {"error": "could not obtain local user token"}
    req = urllib.request.Request(f"{_API}{_with_token(path, token)}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _delete_user(path: str) -> dict:
    """DELETE a user-token-gated route (e.g. ``/api/autonudge/{id}``)."""
    token = _local_user_token()
    if not token:
        return {"error": "could not obtain local user token"}
    req = urllib.request.Request(
        f"{_API}{_with_token(path, token)}",
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}
