"""Shell-command normalization, path resolution, IP canonicalization, and IMDS / environment-credential access detection.

Split out of the former monolithic ``security.py``; re-exported by
``kiro_crew.security`` for backward compatibility.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shlex
import socket


# ── Shell-aware command normalizer ──
# Strips shell quoting tricks, expands tilde/HOME, and resolves paths so that
# obfuscated commands (e.g. ca""t ~/.aws/credentials, $HOME/.ssh/id_rsa) are
# reduced to their canonical form before deny-list matching.

# Regex to strip empty-string concatenation: paired quotes ('' or "") that
# vanish (e.g. g""it -> git, ca''t -> cat).
_EMPTY_QUOTE_RE = re.compile(r'""|\'\'')

# Regex for $HOME or ${HOME} variable expansion.
_HOME_VAR_RE = re.compile(r"\$\{HOME\}|\$HOME", re.IGNORECASE)


def normalize_shell_command(cmd: str) -> list[str]:
    """Normalize a shell command string into a resolved token list.

    Handles:
    - Shell quoting via shlex.split(posix=True)
    - Empty-string concatenation (g""it -> git, ca''t -> cat)
    - Tilde expansion (~/... -> /home/user/...)
    - $HOME / ${HOME} expansion to actual home directory
    - Backslash stripping (handled by shlex POSIX mode)

    Returns a list of resolved tokens.  On parse failure (unmatched quotes)
    falls back to basic whitespace splitting with quote/backslash stripping.
    """
    if not cmd or not cmd.strip():
        return []

    # Pre-process: expand $HOME/${HOME} BEFORE shlex splitting so that
    # expansion happens even inside quoted strings that shlex won't expand.
    home = os.path.expanduser("~")
    preprocessed = _HOME_VAR_RE.sub(home, cmd)

    # Tokenize using POSIX shlex — handles quoting, escaping, etc.
    try:
        tokens = shlex.split(preprocessed, posix=True)
    except ValueError:
        # Unbalanced quotes or other parse errors — fall back to basic split.
        tokens = preprocessed.split()
        tokens = [t.strip("\"'\\") for t in tokens]

    resolved: list[str] = []
    for token in tokens:
        # Strip empty-string concatenation artifacts: ca""t -> cat, g''it -> git
        token = _EMPTY_QUOTE_RE.sub("", token)

        # Expand tilde (shlex doesn't do tilde expansion)
        if token.startswith("~"):
            token = os.path.expanduser(token)

        resolved.append(token)

    return resolved


def resolve_command_paths(tokens: list[str]) -> list[str]:
    """Resolve path-like tokens to their canonical absolute form.

    Runs os.path.realpath() on tokens that look like filesystem paths
    (start with /, ~, ./, or ../) to resolve symlinks and directory traversal.
    Non-path tokens are returned unchanged.

    Args:
        tokens: List of shell tokens (typically from normalize_shell_command).

    Returns:
        New list with path-like tokens resolved to their realpath.
    """
    resolved: list[str] = []
    for token in tokens:
        if _is_path_like(token):
            resolved.append(os.path.realpath(token))
        else:
            resolved.append(token)
    return resolved


def _is_path_like(token: str) -> bool:
    """Heuristic: does this token look like a filesystem path?"""
    if not token:
        return False
    # Absolute path
    if token.startswith("/"):
        return True
    # Home-relative (already expanded, but handle edge cases)
    if token.startswith("~"):
        return True
    # Relative with explicit directory prefix
    if token.startswith("./") or token.startswith("../"):
        return True
    # Contains path separator and has directory component (not a flag)
    if "/" in token and not token.startswith("-"):
        # Exclude URLs (http://, https://, etc.)
        if "://" in token:
            return False
        return True
    return False


# ── IP Canonicalization (IMDS bypass prevention) ──
# Attackers bypass IMDS checks by encoding 169.254.169.254 in alternate forms:
#   - Decimal:   2852039166 (single 32-bit integer)
#   - Hex:       0xa9fea9fe or 0xa9.0xfe.0xa9.0xfe
#   - Octal:     0251.0376.0251.0376
#   - IPv6-mapped: ::ffff:169.254.169.254 or ::ffff:a9fe:a9fe
#   - Mixed:     169.254.0xa9.0376
# canonicalize_ip converts ALL these to dotted-quad for uniform matching.


def canonicalize_ip(s: str) -> str:
    """Convert an IP address in any encoding to dotted-quad (a.b.c.d).

    Handles:
    - Standard dotted-quad (passthrough)
    - Single decimal integer (e.g. 2852039166)
    - Hex integer (e.g. 0xa9fea9fe)
    - Octal/hex per-octet (e.g. 0251.0376.0251.0376 or 0xa9.0xfe.0xa9.0xfe)
    - IPv6-mapped IPv4 (e.g. ::ffff:169.254.169.254 or ::ffff:a9fe:a9fe)

    Returns the dotted-quad string on success, or the original string unchanged
    if it cannot be parsed as an IP address.
    """
    s = s.strip()
    if not s:
        return s

    # Try IPv6-mapped IPv4: ::ffff:... forms
    if s.startswith("::ffff:") or s.startswith("::FFFF:"):
        try:
            addr = ipaddress.ip_address(s)
            if hasattr(addr, "ipv4_mapped") and addr.ipv4_mapped:
                return str(addr.ipv4_mapped)
            if isinstance(addr, ipaddress.IPv6Address):
                mapped = addr.ipv4_mapped
                if mapped:
                    return str(mapped)
        except (ValueError, AttributeError):
            pass

    # Try standard dotted-quad with possible hex/octal octets
    parts = s.split(".")
    if 1 <= len(parts) <= 4:
        octets: list[int] = []
        valid = True
        for part in parts:
            try:
                # Handle C-style octal (0NNN without 'o' prefix) which Python 3
                # int(x, 0) doesn't recognize. Must check before int(x, 0).
                if len(part) > 1 and part[0] == "0" and part[1:].isdigit():
                    # Could be octal (0251) or just "00" etc.
                    if all(c in "01234567" for c in part[1:]):
                        val = int(part, 8)
                    else:
                        # Has 8 or 9 -- not valid octal, treat as decimal
                        val = int(part)
                else:
                    # int() with base=0 handles: decimal, 0x hex
                    val = int(part, 0)
                octets.append(val)
            except (ValueError, OverflowError):
                valid = False
                break

        if valid:
            if len(octets) == 1:
                # Single integer: 2852039166 -> 4 octets
                val = octets[0]
                if 0 <= val <= 0xFFFFFFFF:
                    return str(ipaddress.IPv4Address(val))
            elif len(octets) == 4:
                # Four octets (each 0-255)
                if all(0 <= o <= 255 for o in octets):
                    return f"{octets[0]}.{octets[1]}.{octets[2]}.{octets[3]}"
            elif len(octets) in (2, 3):
                # inet_aton "short" forms the OS resolver / curl accept but which
                # neither ipaddress nor the 1-/4-octet branches above canonicalize:
                #   a.b     -> a.(b as 24-bit)     e.g. 169.16689662  -> 169.254.169.254
                #   a.b.c   -> a.b.(c as 16-bit)   e.g. 169.254.43518 -> 169.254.169.254
                # Resolve them exactly as the OS does via inet_aton (which also
                # rejects out-of-range forms like 169.254.11207422), so an IMDS
                # SSRF cannot slip through in a 2-/3-part encoding. The last octet
                # carries the remaining low-order bytes, so a decimal/hex value up
                # to 0xFFFFFF (3-part) / 0xFFFFFFFF (2-part) is legal — validate the
                # leading octets are single bytes, then defer to inet_aton.
                if all(0 <= o <= 255 for o in octets[:-1]):
                    try:
                        return socket.inet_ntoa(socket.inet_aton(s))
                    except OSError:
                        pass

    # Try parsing as a plain integer (no dots) -- decimal or hex
    try:
        val = int(s, 0)
        if 0 <= val <= 0xFFFFFFFF:
            return str(ipaddress.IPv4Address(val))
    except (ValueError, OverflowError):
        pass

    # Try full ipaddress parsing as fallback
    try:
        addr = ipaddress.ip_address(s)
        if isinstance(addr, ipaddress.IPv4Address):
            return str(addr)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
    except ValueError:
        pass

    return s


# ── IMDS Access Detection ──
# The AWS Instance Metadata Service at 169.254.169.254 (link-local) exposes
# IAM role credentials via /latest/meta-data/iam/security-credentials/.
# Any HTTP client (not just curl/wget) hitting this IP must be blocked.

# Regex to extract potential IP addresses from a command string.
# Captures dotted-quad, hex/octal per-octet, bare integers, IPv6-mapped forms.
_IP_CANDIDATE_RE = re.compile(
    r"(?:"
    r"::ffff:[0-9a-fA-Fx.:]+|"  # IPv6-mapped
    r"[0-9a-fA-F]{1,4}:[0-9a-fA-F:]{2,}|"  # native IPv6 literal (colon run, e.g. fd00:ec2::254)
    r"0[xX][0-9a-fA-F]+(?:\.[0-9a-fA-Fx]+)*|"  # Hex (with possible dotted)
    # inet_aton "short" forms the OS resolver / curl accept (a.b.c and a.b),
    # where the trailing component packs the remaining low-order bytes. These
    # must be captured WHOLE (not just the tail) so canonicalize_ip can resolve
    # them and catch an IMDS SSRF hidden in a 2-/3-part encoding. Listed before
    # the bare-integer / dotted-quad alternatives so the full token wins.
    r"\d{1,3}\.\d{1,3}\.(?:0[xX][0-9a-fA-F]+|\d{4,10})|"  # 3-part: a.b.c
    r"\d{1,3}\.(?:0[xX][0-9a-fA-F]+|\d{5,10})|"  # 2-part: a.b
    r"\d{7,10}|"  # Large decimal (single integer IP)
    r"(?:0[0-7]+\.){3}0[0-7]+|"  # Octal dotted
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"  # Standard dotted-quad
    r")"
)

_IMDS_IP = "169.254.169.254"
# Native IPv6 IMDS endpoint (dual-stack EC2). The IPv4 gate above misses this
# because canonicalize_ip returns native IPv6 unchanged; mirrors embeddings.py's
# SSRF gate which also blocks it (CWE-918 dual-stack parity).
_IMDS_IPV6 = "fd00:ec2::254"

# HTTP tools that can fetch IMDS -- broader than just curl/wget
_HTTP_TOOLS_RE = re.compile(
    r"(?:curl|wget|http|https|fetch|lwp-request|lynx|links|"
    r"python|ruby|perl|node|nc|ncat|socat|telnet|"
    r"Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b",
    re.IGNORECASE,
)


def _check_imds_access(command: str) -> str | None:
    """Detect attempts to access the IMDS endpoint via any encoding.

    Returns denial reason if IMDS access detected, None otherwise.
    """
    # Quick reject: no IP-like candidate in command
    candidates = _IP_CANDIDATE_RE.findall(command)
    if not candidates:
        return None

    try:
        imds_v6: ipaddress.IPv6Address | None = ipaddress.ip_address(_IMDS_IPV6)  # type: ignore[assignment]
    except ValueError:  # pragma: no cover - constant is a valid literal
        imds_v6 = None
    for candidate in candidates:
        canonical = canonicalize_ip(candidate)
        if canonical == _IMDS_IP:
            # Found IMDS IP -- block regardless of tool since even echo
            # piped into nc could exfil credentials from the metadata service
            return (
                f"Blocked: command accesses IMDS endpoint "
                f"(169.254.169.254 via encoding '{candidate}')"
            )
        # Native IPv6 IMDS endpoint (fd00:ec2::254) — reachable over IPv6 on
        # dual-stack hosts; the IPv4 canonicalization above never matches it.
        # ipaddress equality normalizes compressed/expanded forms.
        if imds_v6 is not None:
            try:
                if ipaddress.ip_address(candidate.strip("[]")) == imds_v6:
                    return (
                        f"Blocked: command accesses IMDS endpoint "
                        f"(fd00:ec2::254 via '{candidate}')"
                    )
            except ValueError:
                pass
    return None


# ── Environment Credential Exfiltration Detection ──
# Attackers can read AWS credentials from environment variables without
# touching the filesystem, bypassing is_sensitive_path/bash checks.
# Block: declare -p AWS_SECRET*, env | grep AWS_, printenv AWS_,
#         awk 'ENVIRON["AWS_*"]', export -p | grep AWS_

_ENV_CRED_PATTERNS: list[re.Pattern[str]] = [
    # declare -p AWS_SECRET_ACCESS_KEY / declare -p AWS_SESSION_TOKEN
    re.compile(
        r"declare\s+(?:-[a-zA-Z]+\s+)*-?p\s+AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
    # env / printenv / export -p piped through grep for AWS_ vars
    re.compile(
        r"(?:env|printenv|export\s+-p|set)\s*(?:\|.*)?(?:grep|awk|sed)\s+.*AWS_",
        re.IGNORECASE,
    ),
    # Direct printenv of sensitive vars
    re.compile(
        r"printenv\s+AWS_(?:SECRET_ACCESS_KEY|SESSION_TOKEN|SECURITY_TOKEN)",
        re.IGNORECASE,
    ),
    # echo $AWS_SECRET* / echo ${AWS_SECRET*}
    re.compile(
        r"(?:echo|printf|cat)\s+.*\$\{?AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
    # awk ENVIRON["AWS_SECRET*"] / awk ENVIRON["AWS_SESSION*"]
    re.compile(
        r"awk\s+.*ENVIRON\s*\[\s*[\"']AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
    # python/ruby/node reading os.environ for AWS secrets
    re.compile(
        r"(?:python|ruby|node|perl)\S*\s+.*(?:os\.environ|ENV|process\.env)"
        r".*AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
]


def _check_env_credential_access(command: str) -> str | None:
    """Detect attempts to read AWS credentials from environment variables.

    Returns denial reason if env credential access detected, None otherwise.
    """
    for pattern in _ENV_CRED_PATTERNS:
        if pattern.search(command):
            return "Blocked: command reads AWS credentials from environment variables"
    return None
