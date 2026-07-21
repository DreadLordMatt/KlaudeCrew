"""URL exfiltration detection and redaction (payload-based, host-exempt aware).

Split out of the former monolithic ``security.py``; re-exported by
``kiro_crew.security`` for backward compatibility.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs

from kiro_crew.security.credentials import _decode_b64_safe


# ── URL Exfiltration Detection ──
# Detects URLs whose path/query contain credential-like data. We flag the
# PAYLOAD, not the destination: any URL with secrets is suspicious regardless of
# host. The sole host-sensitive carve-out is a companion-supplied exact-host
# exemption (see _exfil_url_warning) that narrows ONLY the base64-blob and
# query-length heuristics for trusted tenants; the hard-credential floor and the
# heavy percent-encoding detector stay unconditional for every host.

# Host group (group 1) matches THREE host shapes so a raw-IP exfil destination
# is not silently skipped (Talos 78224f3f): a DNS name with a letter TLD, a raw
# IPv4 literal (``192.168.1.1``, incl. link-local/metadata ``169.254.169.254``),
# or a bracketed IPv6 literal (``[::1]``, ``[fd00::1]``). The prior regex required
# a ``.<letters>`` TLD, so ``http://169.254.169.254/latest/…/<secret>`` never
# matched _URL_RE and its path/query was never scanned. Group 3 stays the
# path+query so the scan/redact call sites are unchanged.
_URL_RE = re.compile(
    r"https?://"
    r"("
    r"[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}"  # DNS name with a letter TLD
    r"|\d{1,3}(?:\.\d{1,3}){3}"  # raw IPv4 literal
    r"|\[[0-9A-Fa-f:.]+\]"  # bracketed IPv6 literal (incl. IPv4-mapped ::ffff:d.d.d.d)
    # Group 3 = path AND/OR query. It must start with ``/`` (path) OR ``?``
    # (a query attached directly to the host, no path segment). The prior
    # ``/[...]*`` required a leading slash, so ``https://host?leak=<secret>``
    # yielded group(3)=None and both scan/redact bailed on ``qmark == -1``,
    # never inspecting the query — a real exfil bypass. ``[/?]`` admits both;
    # the ``path_and_query.find("?")`` split at the call sites is unchanged.
    r")(:\d+)?([/?][^\s)\"'>]*)?"
)

# Query string length threshold — normal URLs rarely exceed this
_EXFIL_QUERY_MIN_LEN = 200

# Patterns that indicate secrets or encoded data in query params
_EXFIL_PATTERNS = re.compile(
    r"(?:"
    r"[A-Za-z0-9+/=]{40,}"  # base64-like blob (40+ chars)
    r"|%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}"  # heavy URL-encoding (20+ encoded chars)
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key ID
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)

# Heavy URL-encoding detector — the same "20+ consecutive percent-encoded
# octets" branch carved out of _EXFIL_PATTERNS. This stays UNCONDITIONAL: the
# exact-host exemption below skips only the base64-blob and query-length
# heuristics (which false-positive on legitimate long base64 document
# pointers), NOT this percent-encoding detector, so an encoded exfil payload to
# a trusted-tenant host is still caught.
_EXFIL_PERCENT_RE = re.compile(
    r"%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}",
    re.IGNORECASE,
)

# S3 presigned URLs contain X-Amz-Signature (a 64-char hex string) that
# matches the base64-like blob pattern above.  These are intentional
# time-limited access tokens, not leaked credentials.  Skip the exfil
# check when ALL standard presigned-URL query params are present on an
# amazonaws.com domain.  Values are validated to prevent spoofing.
_S3_PRESIGNED_RE = re.compile(
    r"X-Amz-Algorithm=AWS4-HMAC-SHA256"
    r".*X-Amz-Credential=(?:AKIA|ASIA)[A-Z0-9]{16}(?:%2F|/)"
    r".*X-Amz-Expires=\d{1,6}"
    r".*X-Amz-Signature=[0-9a-f]{64}",
    re.IGNORECASE,
)

# Only these parameter keys are allowed in a presigned URL.  Any extra
# keys cause the fast-path to reject, falling through to normal checks.
_S3_PRESIGNED_PARAMS = frozenset(
    {
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-SignedHeaders",
        "X-Amz-Signature",
        "X-Amz-Security-Token",
    }
)


# Structural validators for presigned param values that would otherwise
# false-positive against _EXFIL_PATTERNS.  Each value is validated rather
# than exempted, so attacker-controlled data cannot be smuggled through.
_STS_TOKEN_RE = re.compile(r"^(?:FwoGZX|IQoJb3JpZ2lu)[A-Za-z0-9+/=%]{1,2000}$")
_CREDENTIAL_RE = re.compile(
    r"^(?:AKIA|ASIA)[A-Z0-9]{16}(?:%2F|/)[0-9]{8}"
    r"(?:%2F|/)[a-z0-9-]+(?:%2F|/)s3(?:%2F|/)aws4_request$"
)
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")

_STRUCTURAL_VALIDATORS = {
    "X-Amz-Credential": _CREDENTIAL_RE,
    "X-Amz-Signature": _SIGNATURE_RE,
    "X-Amz-Security-Token": _STS_TOKEN_RE,
}


def _is_safe_presigned(domain: str, query: str) -> bool:
    """Return True if the URL is a valid S3 presigned URL with no extra parameters."""
    if not domain.endswith(".amazonaws.com"):
        return False
    if not _S3_PRESIGNED_RE.search(query):
        return False
    params = parse_qs(query, keep_blank_values=True)
    if not _S3_PRESIGNED_PARAMS.issuperset(params.keys()):
        return False
    # Structurally validate params that would false-positive against
    # _EXFIL_PATTERNS.  No values are fully exempt — each is checked.
    for key, values in params.items():
        validator = _STRUCTURAL_VALIDATORS.get(key)
        if validator:
            for val in values:
                if not validator.match(val):
                    return False
        else:
            for val in values:
                if _EXFIL_PATTERNS.search(val):
                    return False
    return True


# Hard, unambiguous credential markers scanned across the FULL URL path+query
# (Talos 78224f3f) — a real AWS key / SSH-or-PEM header / Slack token in a URL is
# exfil even to an otherwise-safe host, and even with no ``?`` query (secret in
# the PATH). Distinct from the broader _EXFIL_PATTERNS base64/length heuristics,
# which stay query-only (long base64 PATH segments — CDN asset ids, git object
# hashes — are benign).
_HARD_CREDENTIAL_RE = re.compile(
    r"(?:"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key ID
    r'|(?:SecretAccessKey|aws_secret_access_key)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:SessionToken|aws_session_token)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:AccessKeyId|aws_access_key_id)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)


def _exempt_exact_hosts() -> frozenset[str]:
    """Exact-match hosts that skip ONLY the exfil base64/length heuristics.

    Sourced from the active ``PlatformContext``'s ``CredentialPolicy`` — the
    public Default returns an empty set (no exemptions), a loaded companion
    supplies its trusted-tenant host list.  NEVER read from ``config.json``: an
    agent-writable exemption would be a hole in the redaction ceiling.

    Import is FUNCTION-LOCAL (deferred, mirroring the ``sel.py`` pattern) so
    ``security`` never reaches ``kiro_crew.platform`` at module-load time — the
    CPP import-direction invariant (``platform/defaults.py`` imports ``security``
    at top level).

    Degrade semantics (INVERTED vs ``redact_via_context``'s baseline-redact
    fallback): ``PlatformCompositionError`` propagates fail-closed, but any other
    adapter failure degrades to ``frozenset()`` — the empty set means MORE
    redaction (every host runs the heuristics), the SAFE direction here.  A
    pre-method companion adapter (no ``exempt_exact_hosts``) degrades to the empty
    set via ``getattr`` rather than raising.  NO logging on the degrade path: this
    runs inside the stdio MCP servers whose stray writes corrupt the JSON-RPC
    stream.
    """
    from kiro_crew.platform.context import PlatformCompositionError, current_context

    try:
        policy = current_context().credentials
        getter = getattr(policy, "exempt_exact_hosts", None)
        if getter is None:
            return frozenset()
        raw = getter()
        # Normalize INSIDE the guarded block: a buggy companion adapter may return
        # None or a set with non-string members, and callers (_exfil_exempt_hosts)
        # iterate + .lower() the result. If that raised outside this try, it would
        # break EVERY redaction path (chat/Slack/MCP/dashboard) instead of degrading
        # to maximum redaction. Keep only str members; anything malformed degrades
        # to the empty set (the SAFE direction — more redaction).
        return frozenset(h for h in raw if isinstance(h, str))
    except PlatformCompositionError:
        raise
    except Exception:
        return frozenset()


def _exfil_exempt_hosts() -> frozenset[str]:
    """Companion exempt-host set normalized to lowercase for case-insensitive match.

    Hostnames are case-insensitive (RFC 4343); Office apps commonly emit
    mixed-case hosts (``Contoso.SharePoint.com``). _URL_RE captures the host
    verbatim, so both the captured host and the companion-supplied members must
    be lowercased before comparison or a legitimate document pointer to an
    exempted tenant is wrongly redacted. Delegates fail-closed / degrade
    semantics to _exempt_exact_hosts().
    """
    return frozenset(host.lower() for host in _exempt_exact_hosts())


def _exfil_url_warning(
    domain: str, path_and_query: str, exempt_hosts: frozenset[str]
) -> str | None:
    """Classify one matched URL — the single per-URL exfil verdict.

    Shared by scan_exfiltration_urls (which collects the warnings) and
    redact_exfiltration_urls (which redacts every URL that returns non-None), so
    the two paths can never drift — redact_ early-returns on scan_'s warnings, so
    a divergence would silently produce warnings-without-redaction. Returns the
    warning string, or None if the URL is clean/exempt.
    """
    qmark = path_and_query.find("?")
    query = path_and_query[qmark + 1 :] if qmark != -1 else ""

    # Valid S3 presigned URLs carry AKIA in X-Amz-Credential legitimately, so
    # exempt them wholesale BEFORE the hard-credential path scan below would
    # otherwise flag them.
    if query and _is_safe_presigned(domain, query):
        return None

    # Hard credential markers ANYWHERE in the path or query (Talos 78224f3f).
    # The base64/length heuristics below are query-only, so a secret embedded in
    # the URL PATH (``https://evil/AKIA…`` — no ``?``) escaped them entirely, and
    # a raw-IP host never even matched _URL_RE. These markers (AKIA/ASIA,
    # key=value creds, SSH/PEM, Slack) are unambiguous, so flag regardless of
    # domain — a real AWS key in a URL is exfil even to an otherwise-safe (or
    # exempted) host. This hard-credential floor is UNCONDITIONAL.
    if _HARD_CREDENTIAL_RE.search(path_and_query):
        return f"Suspicious URL with credential in path/query: {domain}"

    if qmark == -1:
        return None

    # UNCONDITIONAL base64 decode-and-scan: a hard credential (AWS key, SSH/PEM,
    # Slack token) that is base64-ENCODED into the query would slip past the raw
    # _HARD_CREDENTIAL_RE floor above (which matches literal markers, not encoded
    # bytes) AND, on an exempt host, past the raw base64-blob heuristic below.
    # Decode any base64 chunk and re-scan the decoded bytes for credential
    # markers; a legitimate base64 *document* decodes to non-credential text and
    # _decode_b64_safe returns "" (so it still qualifies for the exemption).
    # This runs for EVERY host, closing the encoded-credential-to-trusted-tenant
    # gap without re-flagging benign document pointers.
    if query and _decode_b64_safe(query):
        return f"Suspicious URL with encoded credential in query: {domain}"

    # Exact-host heuristic exemption (companion-supplied trusted tenants),
    # matched case-insensitively and EXACTLY (not by suffix) so a shared
    # multi-tenant domain does not exempt every tenant. The exemption skips ONLY
    # the raw base64-blob and query-length heuristics below — the ones that
    # false-positive on legitimate long base64 document pointers. Everything
    # else stays unconditional: the hard-credential floor above already ran, the
    # decode-and-scan just above catches ENCODED credentials on every host, and
    # the heavy percent-encoding detector below runs even for exempted hosts, so
    # an encoded exfil payload to a trusted tenant is still caught.
    if domain.lower() not in exempt_hosts:
        # (Valid S3 presigned URLs were already exempted at the top, so no
        # _is_safe_presigned re-check is needed here.)
        if len(query) >= _EXFIL_QUERY_MIN_LEN:
            return (
                f"Suspicious URL with long query params ({len(query)} chars): "
                f"{domain}{path_and_query[:60]}..."
            )
        if _EXFIL_PATTERNS.search(query):
            return f"Suspicious URL with credential-like query data: {domain}"

    # Heavy percent-encoding is a hard heuristic, NOT part of the exempted
    # base64/length set — it runs for every host (for non-exempt hosts it was
    # already covered by _EXFIL_PATTERNS above, so this only adds coverage on
    # exempted hosts).
    if _EXFIL_PERCENT_RE.search(query):
        return f"Suspicious URL with credential-like query data: {domain}"
    return None


def scan_exfiltration_urls(text: str) -> list[str]:
    """Scan text for URLs that may be exfiltrating data via query params.

    Flags the PAYLOAD, not the destination: the hard-credential floor and the
    base64/length heuristics inspect the URL path+query for secret patterns
    regardless of host. The one host-sensitive exception is a companion-supplied
    exact-host exemption that narrows ONLY the base64/length heuristics for
    trusted tenants (see _exfil_url_warning); the hard-credential floor and the
    percent-encoding detector stay unconditional. Returns list of warning
    strings, empty if clean.
    """
    exempt_hosts = _exfil_exempt_hosts()
    warnings: list[str] = []
    for match in _URL_RE.finditer(text):
        warning = _exfil_url_warning(match.group(1), match.group(3) or "", exempt_hosts)
        if warning:
            warnings.append(warning)
    return warnings


def redact_exfiltration_urls(text: str) -> tuple[str, list[str]]:
    """Scan and redact suspicious exfiltration URLs from text.

    Returns (cleaned_text, list_of_warnings).
    """
    warnings = scan_exfiltration_urls(text)
    if not warnings:
        return text, []

    exempt_hosts = _exfil_exempt_hosts()
    result = text
    for match in _URL_RE.finditer(text):
        domain = match.group(1)
        if _exfil_url_warning(domain, match.group(3) or "", exempt_hosts):
            result = result.replace(match.group(0), f"[REDACTED: suspicious URL to {domain}]")
    return result, warnings
