"""Credential-pattern detection, batch + streaming redaction (StreamRedactor), and bare-secret heuristics.

Split out of the former monolithic ``security.py``; re-exported by
``kiro_crew.security`` for backward compatibility.
"""
from __future__ import annotations

import base64
import math
import re
import string
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


# ── Credential Output Redaction ──
# Catches raw credential patterns in LLM output / tool results,
# including base64-encoded variants.  Applied on all output paths
# alongside redact_exfiltration_urls().

_CREDENTIAL_PATTERNS = re.compile(
    r"(?:"
    # ── AWS ──
    r"(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key ID
    # key-value forms: tolerate an optional closing quote after the key name and an
    # optional opening quote before the value so JSON (`"aws_secret_access_key": "v"`)
    # is redacted, not just bare `key=v` / `key: v`. Without the `["']?` the closing
    # quote in JSON sits between the key and `:` and defeats the match → secret leaks.
    # The value class is [^\s"',}]+ (NOT \S+): \S+ is greedy and, in compact JSON
    # like {"aws_secret_access_key":"SECRET","region":"x"}, swallows everything
    # through the closing brace (`"`, `,`, `}` all match \S) — destroying adjacent
    # fields and consuming a following credential key so it's never matched/counted.
    # Stopping at JSON structural delimiters bounds the value while still matching
    # bare key=value forms.
    r'|(?:SecretAccessKey|aws_secret_access_key)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:SessionToken|aws_session_token)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:AccessKeyId|aws_access_key_id)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    # PEM private key: match the ENTIRE block (header + base64 body), not just
    # the header phrase. redact_credentials() replaces the matched SPAN, so a
    # header-only match (the original form) left the secret base64 body verbatim.
    # Two mutually exclusive tails after the header:
    #   1. Full block — ``[\s\S]*?`` (any char, incl. newlines) spans the body
    #      lazily to the first END marker. ``[\s\S]`` (not a base64 char class)
    #      is required so encrypted keys — whose ``Proc-Type:``/``DEK-Info:``
    #      headers carry ``:`` and ``,`` — are fully spanned rather than cut
    #      short at the first non-base64 char (Talos 05687e60).
    #   2. Truncated block (no END) — consume only *subsequent* PEM body lines:
    #      each continuation must start with a newline and be a base64 line or a
    #      ``Proc-Type:``/``DEK-Info:`` metadata header. This deliberately does
    #      NOT use ``$``/``\Z``: without re.MULTILINE ``$`` means end-of-STRING,
    #      so a lazy ``[\s\S]*?`` with a ``|$`` fallback swallowed everything
    #      from a header mentioned inline in prose (LLM output, docs) to the end
    #      of the string — silently deleting all trailing lines. Requiring a
    #      leading newline per line means an inline header in prose (real key
    #      material always begins on the line *after* the header) matches only
    #      the header phrase, leaving trailing content intact, while a genuine
    #      truncated key still has its body lines redacted.
    #      The final ``(?=\r?\n[A-Za-z0-9+/=])`` lookahead alternative lets the
    #      run cross a SINGLE blank line when the *next* line begins with base64
    #      material. RFC 1421 ENCRYPTED PEMs put a MANDATORY blank line between
    #      the ``DEK-Info:`` header and the base64 body; without this lookahead
    #      the per-line "every continuation must contain a base64 char" rule
    #      stopped at that blank line and leaked the whole encrypted body (for
    #      both a truncated key AND a complete encrypted key whose body exceeds
    #      the full-block cap). Because the lookahead consumes nothing, TWO+
    #      consecutive blank lines still terminate the run — trailing prose is
    #      preserved (no over-redaction). (CR-289301166.)
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"(?:"
    r"[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?:\r?\n(?:Proc-Type:[^\n]*|DEK-Info:[^\n]*|[A-Za-z0-9+/=]+(?=\r?\n|\Z)"
    r"|(?=\r?\n[A-Za-z0-9+/=])))*"
    r")"
    r"|xox[bpas]-[0-9a-zA-Z-]{10,}"  # Slack token
    # Telegram bot token: ``<bot_id>:<secret>`` — bot_id is 6+ digits, secret is
    # ~35 URL-safe base64 chars. The ``{30,}`` floor sits deliberately below the
    # real length so shortened/rotated test tokens are still caught. Analogue to
    # the Slack token above. Telegram tokens can live in ``config.json``
    # (agent-readable), so an echoed config would otherwise leak a full
    # bot-control credential unredacted. The value class ``[A-Za-z0-9_-]`` stops
    # at structural delimiters (space, quote, comma, brace), so it can't swallow
    # adjacent fields; over-redacting a rare ``digits:token`` lookalike is the
    # safe direction.
    r"|[0-9]{6,}:[A-Za-z0-9_-]{30,}"  # Telegram bot token
    # ── Third-party developer credentials (AWS-345 / AWS-59) ──
    # Distinctive, fixed-case prefixes → very low false-positive risk.  Minimum
    # lengths are kept slightly below the real token lengths so shortened test /
    # rotated variants are still redacted (over-redaction on a prefix match is the
    # safe direction).  Case-sensitive by design (these prefixes are issued in a
    # fixed case); do NOT fold — folding would broaden false positives.
    r"|gh[opsur]_[A-Za-z0-9]{30,255}"  # GitHub PAT (ghp_) + oauth/user/server/refresh
    r"|github_pat_[A-Za-z0-9_]{40,}"  # GitHub fine-grained PAT
    r"|glpat-[A-Za-z0-9_-]{16,}"  # GitLab PAT
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"  # Stripe secret / restricted keys
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"  # SendGrid API key
    r"|sk-proj-[A-Za-z0-9_-]{16,}"  # OpenAI project key
    r"|sk-ant-[A-Za-z0-9_-]{16,}"  # Anthropic API key
    r"|npm_[A-Za-z0-9]{24,}"  # npm access token
    r"|pypi-[A-Za-z0-9_-]{16,}"  # PyPI API token
    r"|do[opr]_v1_[A-Za-z0-9]{40,}"  # DigitalOcean PAT/OAuth/refresh
    r"|GOCSPX-[A-Za-z0-9_-]{20,}"  # Google OAuth client secret
    # DB connection URIs with embedded credentials — redact the
    # ``scheme://user:pass@`` prefix (the password lives here).
    r"|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|amqp(?:s)?)"
    # User portion is `*` (not `+`): empty-user connection strings (e.g. MongoDB
    # Atlas IAM `mongodb+srv://:secret@…`) still redact the password (Heimdall,
    # ported from KiroCrew CR-286281237).
    r"://[^\s:/@]*:[^\s/@]+@"
    # ── JWT / JWE / OAuth Bearer tokens (Talos cc1d6bdd; JWE hardening a8e5fe6a) ──
    # `eyJ` is the base64url encoding of every JWT header's `{"` prefix; a signed
    # JWT (JWS) is three `.`-separated base64url segments (header.payload.sig) and
    # an encrypted JWT (JWE, RFC 7516) is five (header.key.iv.ciphertext.tag), so
    # the segment quantifier accepts `(?:\.[A-Za-z0-9_-]*){2,4}` further segments
    # after the header to redact BOTH shapes as one token. Post-header segments use
    # `*` (not `+`) so an EMPTY segment still counts: a compact JWE with direct
    # (`alg:dir`) or key-agreement (`ECDH-ES`) key management has an empty Encrypted
    # Key (2nd) segment — shape `header..iv.ciphertext.tag` — which a `+` quantifier
    # would fail to match, leaking the ciphertext + tag. The `.` separators are
    # still required, so bare `eyJson`-style prose (no dots) is not over-redacted.
    # The HTTP `Authorization: Bearer <token>` header carries opaque or JWT bearer
    # creds. The JWT alternative is case-sensitive (`eyJ` is a fixed base64url
    # prefix). The header name + scheme are matched case-insensitively via scoped
    # `(?i:…)` groups because HTTP header names are case-insensitive (RFC 7230
    # §3.2), HTTP/2 mandates lowercase names, and the `Bearer` scheme is
    # case-insensitive (RFC 6750 §2.1) — so `authorization: bearer …` emitted by
    # requests / net/http / HTTP2 frame logs is redacted too. The separator is
    # JSON-aware (Talos round-2, CR-289081658): an optional quote may precede the
    # `:`/`=` and the token, so a serialized header `{"Authorization": "Bearer
    # <tok>"}` in a structured-log/JSON request dump is redacted as well. Both
    # alternatives are scoped tightly: the JWT segment class cannot cross the
    # literal `.` separators and the Bearer token class (`[A-Za-z0-9._~+/-]`, RFC
    # 6750 `b64token`) stops at whitespace/quotes, so neither over-captures. A
    # Bearer header carrying a JWT redacts as one match (the Bearer class subsumes
    # the JWT); a bare JWT is still caught independently (defense in depth).
    r"|eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){2,4}"  # JWS (3-seg) / JWE (5-seg incl. dir/ECDH-ES)
    r"|(?i:Authorization)[\"\']?\s*[:=]\s*[\"\']?(?i:Bearer)\s+[A-Za-z0-9._~+/-]+=*"  # HTTP/JSON bearer
    r")",
)


def get_credential_patterns() -> list[re.Pattern[str]]:
    """Public accessor for the canonical credential regexes.

    Lets other modules (e.g. deploy-web's pre-publish content scan) reuse the
    same patterns without coupling to the private ``_CREDENTIAL_PATTERNS`` name,
    so a future rename here can't silently turn a downstream scan into a no-op.
    Returns a list so callers can iterate uniformly; the fork keeps a single
    combined compiled regex, so the list has one element.
    """
    return [_CREDENTIAL_PATTERNS]


# Base64 alphabet: at least 40 chars of [A-Za-z0-9+/] ending with optional =
_B64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


# ── Label-independent bare-secret detection (Talos bf7b1baf) ──
# A 40-char AWS *secret access key* (the value paired with an AKIA/ASIA access
# key ID) is a bare run of the base64 alphabet with NO distinctive prefix and NO
# key= label, so none of the labelled/prefixed patterns in _CREDENTIAL_PATTERNS
# catch it when it appears standalone (e.g. echoed alone, in a log line, or in a
# JSON array element). We add a conservative, entropy-gated detector for this
# shape. This is the HIGHEST false-positive-risk redaction rule in the module, so
# it is deliberately over-gated: a token must clear EVERY gate below to be
# redacted. The gates are ordered cheapest-first.
#
# AWS secret access keys are exactly 40 base64 characters. We match ANY isolated
# run of >=40 base64-alphabet chars (word-boundary look-arounds keep surrounding
# prose intact and stop a longer high-entropy blob from being split and missed),
# then require the *specific 40-char secret shape* per token.
_BARE_SECRET_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}(?![A-Za-z0-9+/])")

# Exactly-40 is the AWS secret-key length. Keeping the shape check length-exact
# (rather than ">=40") is what lets the structural gates below cleanly separate
# real keys from 64-char sha256 hex, base64 document blobs, etc.
_SECRET_KEY_LEN = 40

# Shannon-entropy floor (bits/char). A uniformly-random 40-char base64 string
# averages ~4.78 bits/char and empirically almost never drops below ~4.4;
# English-word identifiers, hex digests, and repeated/low-alphabet runs sit
# below this. 4.3 is a conservative floor that admits real keys (the canonical
# AWS example scores 4.66) while rejecting camelCase code identifiers and file
# paths, which cluster around 4.0-4.3.
_SECRET_ENTROPY_MIN = 4.3

# Even after the entropy floor, camelCase / PascalCase code identifiers and
# slash-delimited file paths (e.g. src/main/java/com/Example/FooBarBazClas1) can
# survive on entropy ALONE. Two structural signals separate a random secret from
# a word-based identifier or path: (a) a random key almost never contains a long
# unbroken lowercase run, whereas identifiers/paths are built from dictionary
# words that do; (b) a random key has a low vowel ratio, whereas English words
# do not. NOTE: unlike a naive design we deliberately do NOT treat the presence
# of '/' or '+' as a free pass to redact — 40-char mixed-case file paths contain
# '/' yet are benign, so a '/' token must still clear both structural gates.
# Thresholds are chosen from measured distributions (see test_security.py) with a
# wide margin toward NOT redacting.
_SECRET_MAX_LOWER_RUN = 5
_SECRET_MAX_VOWEL_RATIO = 0.30

# A token that base64-decodes to >=85% printable ASCII is encoded *text*, not a
# random key (random 40-char keys decode to mostly non-printable bytes). Such a
# token is left to the existing base64 decode-and-scan path in redact_credentials
# so we do not double-count or mis-classify it here.
_SECRET_PRINTABLE_DECODE_RATIO = 0.85

_VOWELS: frozenset[str] = frozenset("aeiouAEIOU")

# All-hex runs are git SHAs (40 hex), sha256 (64 hex), md5 (32 hex), etc. — never
# an AWS secret key (which uses the full base64 alphabet). Reject them outright.
_HEX_ONLY_RE = re.compile(r"\A[0-9a-fA-F]+\Z")


def _shannon_entropy(token: str) -> float:
    """Return the Shannon entropy of *token* in bits per character."""
    if not token:
        return 0.0
    counts = Counter(token)
    length = len(token)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _decodes_to_printable_text(token: str) -> bool:
    """Return True if *token* base64-decodes to mostly-printable ASCII.

    Encoded human-readable text (a base64 document blob) decodes to printable
    bytes; a random 40-char secret key decodes to mostly non-printable bytes. We
    use this to exclude encoded-text blobs from the bare-secret heuristic (they
    are handled by the existing decode-and-scan pass instead).
    """
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False)
    except Exception:
        return False
    if not raw:
        return False
    printable = sum(1 for b in raw if 0x20 <= b <= 0x7E or b in (0x09, 0x0A, 0x0D))
    return printable / len(raw) >= _SECRET_PRINTABLE_DECODE_RATIO


def _longest_lowercase_run(token: str) -> int:
    """Return the length of the longest run of consecutive lowercase letters.

    Dictionary-word identifiers and file-path segments contain long lowercase
    word runs; a uniformly random base64 secret almost never does. This is the
    primary discriminator that keeps camelCase identifiers and mixed-case file
    paths out of the bare-secret heuristic.
    """
    best = current = 0
    for ch in token:
        if ch.islower():
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _vowel_ratio(token: str) -> float:
    """Return the fraction of alphabetic characters in *token* that are vowels."""
    letters = [ch for ch in token if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch in _VOWELS) / len(letters)


def _looks_like_secret_key(token: str) -> bool:
    """Return True if *token* has the shape of a bare AWS secret access key.

    Conservative, multi-gate classifier for a label-less 40-char base64 secret
    (Talos bf7b1baf). Every gate must pass; the design bias is toward NOT
    redacting (a false negative merely reverts to today's behavior, a false
    positive corrupts benign output). Gates, cheapest-first:

    1. Length is EXACTLY 40 (AWS secret-key length).
    2. Contains all three of lower + upper + digit (rejects all-lower prose runs,
       all-upper CONSTANT_NAMES, base32, digit strings).
    3. Not an all-hex run (rejects git SHAs, sha256/md5 digests).
    4. Shannon entropy >= _SECRET_ENTROPY_MIN (rejects low-entropy repeats/prose
       and most code identifiers, which cluster below 4.3).
    5. Does not base64-decode to printable text (rejects encoded-text blobs).
    6. Structural randomness: longest lowercase run <= _SECRET_MAX_LOWER_RUN AND
       vowel ratio <= _SECRET_MAX_VOWEL_RATIO. These separate a random key from
       word-based identifiers and slash-delimited file paths that survive the
       entropy floor. Both gates apply to EVERY token (a '/' or '+' does not
       exempt a token, so 40-char mixed-case file paths stay intact).

    BOUNDARY ASSUMPTION: this classifier deliberately evaluates an EXACTLY-40-char
    window (gate 1). It does NOT itself scan longer runs — a real key glued to an
    adjacent base64 char with no delimiter (e.g. ``X`` + key, key + ``A``,
    ``SECRET=`` + key + ``ABC``, key + ``X`` + key) forms a 41+ char run that would
    fail the exact-40 gate and leak verbatim. Callers that receive raw ``{40,}``
    runs MUST use :func:`_contains_bare_secret`, which slides a 40-char window
    across the run so a glued secret is still caught. Keep the exact-40 shape here:
    it is what lets the structural gates cleanly separate real keys from 64-char
    sha256 hex, base64 document blobs, etc.
    """
    if len(token) != _SECRET_KEY_LEN:
        return False
    has_lower = any(ch.islower() for ch in token)
    has_upper = any(ch.isupper() for ch in token)
    has_digit = any(ch.isdigit() for ch in token)
    if not (has_lower and has_upper and has_digit):
        return False
    if _HEX_ONLY_RE.match(token):
        return False
    if _shannon_entropy(token) < _SECRET_ENTROPY_MIN:
        return False
    if _decodes_to_printable_text(token):
        return False
    return (
        _longest_lowercase_run(token) <= _SECRET_MAX_LOWER_RUN
        and _vowel_ratio(token) <= _SECRET_MAX_VOWEL_RATIO
    )


def _contains_bare_secret(run: str) -> bool:
    """Return True if any 40-char window of *run* looks like a bare secret key.

    :func:`_looks_like_secret_key` only accepts an EXACTLY-40-char token, but the
    ``_BARE_SECRET_RUN_RE`` boundary look-arounds capture the longest possible run
    of base64-alphabet chars. A genuine 40-char secret glued to an adjacent
    base64 char with no delimiter (``X`` + key, key + ``A``, ``SECRET=`` + key +
    ``ABC``, key + ``X`` + key) produces a 41+ char run that would fail the
    exact-40 gate and leak verbatim. We slide a 40-char window across the run and
    report a hit if ANY window clears every gate. This stays linear in the run
    length (the regex yields disjoint spans), so cost is bounded overall.

    ENCODED-TEXT-BLOB EXCLUSION: if the WHOLE run base64-decodes to printable
    text it is a cohesive encoded blob (e.g. an OAuth/PKCE ``code_challenge``,
    which is ``base64(sha256-hex)``), not a bare secret — those are handled by
    the decode-and-scan pass instead. We must skip it here because sliding a
    40-char window byte-by-byte across such a blob creates base64-*misaligned*
    sub-windows whose garbage decode looks high-entropy and would clear every
    per-window gate, wrongly redacting a legitimate sign-in URL (regression
    guarded by the OAuth-URL corpus). This is the same bias-toward-not-redacting
    that :func:`_looks_like_secret_key` already applies per-window (gate 5),
    lifted to run granularity so a misaligned window cannot defeat it. A genuine
    glued secret (``X`` + key, key + ``ABC``, key + ``X`` + key) does NOT decode
    cleanly as a whole run, so it still reaches the sliding window below.
    """
    if len(run) < _SECRET_KEY_LEN:
        return False
    if _decodes_to_printable_text(run):
        return False
    for start in range(len(run) - _SECRET_KEY_LEN + 1):
        if _looks_like_secret_key(run[start : start + _SECRET_KEY_LEN]):
            return True
    return False


def _decode_b64_safe(text: str) -> str:
    """Try to base64-decode chunks in text; return decoded content or ''."""
    for m in _B64_CHUNK_RE.finditer(text):
        try:
            decoded = base64.b64decode(m.group(), validate=True).decode("utf-8", errors="ignore")
            if _CREDENTIAL_PATTERNS.search(decoded):
                return decoded
        except Exception:
            continue
    return ""


# Standard replacement tag for a redacted credential. Shared between the batch
# redactor (`redact_credentials`) and the streaming fail-closed path
# (`StreamRedactor.feed`) so the on-the-wire marker is identical everywhere.
_REDACTED_CREDENTIAL_TAG = "[REDACTED: credential]"


def redact_credentials(text: str) -> tuple[str, list[str]]:
    """Redact raw credential patterns from text, including base64-encoded.

    Returns (cleaned_text, list_of_warnings).
    """
    warnings: list[str] = []
    result = text

    # 1. Redact plaintext credential patterns
    for m in _CREDENTIAL_PATTERNS.finditer(result):
        matched = m.group()
        tag = _REDACTED_CREDENTIAL_TAG
        result = result.replace(matched, tag, 1)
        warnings.append(f"Redacted credential pattern: {matched[:20]}...")

    # 2. Detect and redact base64-encoded credentials
    for m in _B64_CHUNK_RE.finditer(text):
        chunk = m.group()
        decoded = _decode_b64_safe(chunk)
        if decoded:
            result = result.replace(chunk, "[REDACTED: encoded credential]", 1)
            warnings.append(f"Redacted base64-encoded credential ({len(chunk)} chars)")

    # 3. Detect and redact BARE 40-char AWS secret keys with no label/prefix
    # (Talos bf7b1baf). These carry no distinctive marker for _CREDENTIAL_PATTERNS
    # to anchor on, so an entropy + structural heuristic is the only way to catch
    # a standalone secret value. Scan the ORIGINAL text (not the already-mutated
    # result) so match offsets are stable; skip any run whose text has already
    # been redacted away by an earlier pass.
    for m in _BARE_SECRET_RUN_RE.finditer(text):
        run = m.group()
        # Slide a 40-char window across the run rather than gating the whole run
        # on len == 40: a real secret glued to an adjacent base64 char (no
        # delimiter) yields a 41+ char run that the exact-40 shape check would
        # miss, leaking the key verbatim. Redact the whole run if ANY window is a
        # secret.
        if not _contains_bare_secret(run):
            continue
        if run not in result:
            # Already redacted by pass 1/2 (e.g. it was a labelled value or an
            # encoded-credential chunk) — nothing left to replace.
            continue
        result = result.replace(run, _REDACTED_CREDENTIAL_TAG, 1)
        warnings.append(f"Redacted bare secret key ({len(run)} chars)")

    return result, warnings


def redact(text: str) -> str:
    """Apply all redaction passes (exfiltration URLs + credentials)."""
    # Deferred import breaks the credentials<->exfiltration module cycle: the
    # exfiltration URL scanner calls _decode_b64_safe from THIS module, so
    # exfiltration imports credentials at load time; credentials therefore must
    # not import exfiltration at load time. Mirrors the module's existing
    # deferred-import pattern (scan_memory, _exempt_exact_hosts).
    from kiro_crew.security.exfiltration import redact_exfiltration_urls

    text = redact_exfiltration_urls(text)[0]
    text = redact_credentials(text)[0]
    return text


# ── Streaming redaction (pentest issue 3) ──
# Per-chunk redaction misses a credential split across token/streaming
# boundaries: a chunk ending ``...AKIA`` and the next starting ``IOSFODNN7...``
# each individually escape redact_credentials(), so the raw fragments reach
# WebSocket/SSE consumers even though the final assembled message is redacted.
# StreamRedactor withholds the trailing run of "credential-class" characters
# (which could be the start of a not-yet-complete credential) until a
# terminator arrives or the stream ends, redacting only the confirmed-safe
# prefix before it is emitted on the wire.

# Characters that can appear inside a credential token/pattern. A credential is
# a contiguous run of these; any byte OUTSIDE this set terminates an in-progress
# match, so text up to (and including) such a terminator is safe to redact and
# emit. Includes URL / base64 / connection-string punctuation so exfil URLs and
# DB URIs are also held intact across chunk boundaries — plus quotes and URL
# query delimiters (``"`` ``'`` ``?&#``) so a JSON key/value or query-string
# secret is not committed piecemeal across a chunk edge. (The private-key HEADER
# phrase contains spaces and is the one pattern that can split on a terminator;
# it is a non-secret header string and the final full-text pass still redacts
# the persisted/displayed copy.)
_CRED_CLASS: frozenset[str] = frozenset(
    string.ascii_letters + string.digits + "_-+/=.:@%~" + '"' + "'" + "?&#"
)

# Upper bound on withheld trailing characters. Larger than the longest
# fixed-format credential so a split token is always rejoined before emission;
# bounds latency/memory for a pathologically long unbroken run (only affects a
# single >512-char secret with no delimiter, which no supported provider issues).
_STREAM_HOLDBACK_MAX = 512

# PEM header hold-back: matches an in-progress "BEGIN [type] PRIVATE KEY"
# phrase in the tail of the commit buffer.  When found, we refuse to commit
# at the whitespace boundary so the full multi-word marker stays inside one
# redaction pass (Heimdall, ported from KiroCrew CR-286281237).
_PEM_HOLD_RE = re.compile(
    r"BEGIN[\s](?:RSA[\s]?|DSA[\s]?|EC[\s]?|OPENSSH[\s]?)?(?:PRIVATE)?[\s]?$",
    re.IGNORECASE,
)

# JWTs (esp. RS256/ES256 with embedded claims) routinely exceed the 512-char DoS
# floor, so a terminal JWT longer than _STREAM_HOLDBACK_MAX would be bisected by
# the default cap and emitted half-redacted. When the withheld tail *looks like*
# the start of a JWT, we raise the cap to this larger ceiling so the whole token
# is rejoined before emission while still keeping the buffer bounded (Talos
# round-2 follow-up to CR-289081658).
_STREAM_HOLDBACK_JWT_MAX = 4096

# The withheld tail is a partial JWT/JWE when it ends with the `eyJ` base64url
# header prefix optionally followed by up to FOUR `.`-separated base64url segments
# (the final segment may be empty mid-stream). Three segments = a JWS/JWT
# (header.payload.sig); five = a compact JWE (header.key.iv.ciphertext.tag), so the
# `{0,4}` trailing quantifier admits the full JWE shape too — matching the batch
# `_CREDENTIAL_PATTERNS` JWE ceiling — instead of bisecting a >512-char JWE at the
# 512 floor. Anchored to the buffer end (`\Z`).
_PARTIAL_JWT_TAIL_RE = re.compile(r"eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){0,4}\Z")

# Trailing (possibly incomplete) `Authorization: Bearer <token>` anchor at the end
# of the stream buffer. Unlike a bare credential run, this anchor embeds WHITESPACE
# (`Authorization: Bearer `) which is NOT in `_CRED_CLASS`, so the maximal-trailing-
# cred-run holdback in `StreamRedactor.feed` would commit the `Authorization:` /
# `Bearer ` prefix in one chunk and the opaque token in the next — redacting
# neither, since the batch `Authorization:\s*Bearer` pattern only fires when the
# whole anchor is present in a single `redact()` call. We therefore withhold from
# the START of any such trailing anchor so the anchor and its token stay joined
# until a terminator (or stream end) arrives.
#
# `\Z` pins the match to the buffer tail so only a genuinely in-progress anchor is
# held. The `Bearer` word is matched by any of its prefixes (`B`…`Bearer`) so a
# split mid-word (`Authorization: Bear` | `er opaque…`) still holds; a completed
# anchor followed by a token then whitespace no longer matches (`\s+` after the
# token cannot reach `\Z`), so it is committed and redacted whole. Requiring the
# `Bearer` prefix bounds over-holding: ordinary prose like `Authorization: granted`
# fails the match and is released immediately. Case-INSENSITIVE and JSON-aware to
# mirror the batch pattern: HTTP/2 lower-cases header names (`authorization:` /
# `bearer`) and JSON shapes the header as `{"Authorization": "Bearer <tok>"}` (a
# quote before the `:` and before the token), so the anchor tolerates an optional
# quote around `[:=]` and folds the `Authorization`/`Bearer` words — otherwise a
# lowercase or JSON-shaped anchor split across chunks would not be held and its
# token would leak. Opaque OAuth/refresh/SSO Bearer tokens carry no `eyJ` header,
# so without this anchor a >512-char opaque bearer tail would stay on the 512 floor
# and stream its raw tail.
_BEARER_ANCHOR_PARTIAL_RE = re.compile(
    r"""Authorization["']?\s*[:=]\s*["']?"""
    r"(?:Bearer(?:\s+[A-Za-z0-9._~+/=-]*)?|Beare|Bear|Bea|Be|B)?\Z",
    re.IGNORECASE,
)


class StreamRedactor:
    """Rolling-buffer redactor for streamed LLM output.

    Feed raw chunks in order; ``feed`` returns the redacted, safe-to-broadcast
    prefix (possibly empty while a partial credential is buffered). Call
    ``flush`` when the stream/segment ends to redact and return the remainder.
    Adds at most one chunk of latency. A credential is never split across a
    commit boundary because commits only ever end at a non-credential-class
    character, while a credential is a contiguous credential-class run.
    """

    __slots__ = ("_buf", "_redact")

    def __init__(self, redactor: "Callable[[str], str] | None" = None) -> None:
        self._buf = ""
        # Resolve at call time so module-load order is irrelevant.
        self._redact = redactor or redact

    def feed(self, chunk: str) -> str:
        """Accept a chunk; return the redacted prefix that is safe to emit now."""
        if not chunk:
            return ""
        self._buf += chunk
        # Start of the maximal trailing credential-class run.
        i = len(self._buf)
        while i > 0 and self._buf[i - 1] in _CRED_CLASS:
            i -= 1
        # PEM header hold-back (Heimdall, ported from KiroCrew CR-286281237): the
        # multi-word phrase "BEGIN RSA PRIVATE KEY" splits on whitespace.  If the
        # tail of the commit window contains an in-progress PEM header prefix,
        # refuse to commit at this boundary.
        if i > 0 and _PEM_HOLD_RE.search(self._buf[max(0, i - 50) : i]):
            i = 0
        # Also withhold from the start of any trailing (possibly incomplete)
        # `Authorization: Bearer <token>` anchor. Its embedded whitespace is not in
        # _CRED_CLASS, so the run scan above would otherwise commit the anchor
        # prefix and the opaque token in separate chunks — leaking the token, since
        # the batch Bearer pattern only fires on the joined anchor.
        anchor = _BEARER_ANCHOR_PARTIAL_RE.search(self._buf)
        if anchor is not None:
            i = min(i, anchor.start())
        # Escalate the holdback cap to the JWT ceiling when the withheld tail is
        # (the start of) a credential that legitimately exceeds the 512-char DoS
        # floor: a partial JWT/JWE (`eyJ…`) OR a trailing `Authorization: Bearer`
        # anchor. Bearer must be included alongside JWT — an opaque OAuth/refresh/
        # SSO Bearer token > 512 chars has no `eyJ` prefix, so keying escalation on
        # `_PARTIAL_JWT_TAIL_RE` alone left its 512-char tail streaming raw. Still
        # bounded: a run with no credential anchor stays on the 512 floor.
        cred_anchored = _PARTIAL_JWT_TAIL_RE.search(self._buf) is not None or anchor is not None
        cap = _STREAM_HOLDBACK_MAX
        if len(self._buf) - i > cap and cred_anchored:
            cap = _STREAM_HOLDBACK_JWT_MAX
        if len(self._buf) - i > cap:
            if cred_anchored:
                # Fail closed: a credential-anchored tail (JWT/JWE/Bearer) has blown
                # past the 4096 ceiling. Bisecting here would emit the token's head
                # raw, so instead redact+emit the safe prefix, append the tag, and
                # DROP the oversized tail. A plain cred-class run with no credential
                # anchor falls through to the bisect below and is committed
                # (bisecting an opaque non-credential run cannot leak a structured
                # secret and preserves the DoS bound with no data loss).
                commit, self._buf = self._buf[:i], ""
                out = self._redact(commit) if commit else ""
                return out + _REDACTED_CREDENTIAL_TAG
            i = len(self._buf) - cap
        if i <= 0:
            return ""  # whole buffer is a (possibly partial) credential run — hold
        commit, self._buf = self._buf[:i], self._buf[i:]
        return self._redact(commit)

    def flush(self) -> str:
        """Redact and return the buffered remainder; clears the buffer."""
        out = self._redact(self._buf) if self._buf else ""
        self._buf = ""
        return out

    def reset(self) -> None:
        """Discard the buffer without emitting (segment abandoned/cleared)."""
        self._buf = ""
