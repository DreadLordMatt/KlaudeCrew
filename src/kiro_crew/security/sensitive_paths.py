"""Sensitive credential/trust-root path gating (read + write) and sensitive-bash-command detection.

Split out of the former monolithic ``security.py``; re-exported by
``kiro_crew.security`` for backward compatibility.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from kiro_crew.security.shell import (
    _check_env_credential_access,
    _check_imds_access,
    _is_path_like,
    normalize_shell_command,
)


# ── Sensitive Paths ──
# Directories and files that must never be read by the agent.
# Patterns are resolved relative to $HOME at check time.

_SENSITIVE_HOME_DIRS: list[str] = [
    ".aws",
    ".ssh",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker/config.json",
    ".kube/config",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    # kiro-cli / amazon-q auth stores hold the live SSO bearer token, read by
    # the dashboard credit pill via the audited kiro_usage_api._token_from_sqlite
    # helper. Classify the WHOLE data directories (not just data.sqlite3) so the
    # WAL/SHM/journal sidecars — which can hold the same credential bytes — are
    # covered too. Agent file tools must not read them through the shared gate.
    # The internal reader opens the DB read-only + SEL-audited (NOT via
    # is_sensitive_path), so it still works; the sandbox bind-mount list
    # (sandbox.py) is SEPARATE, so kiro-cli's own auth is unaffected.
    ".local/share/kiro-cli",
    ".local/share/amazon-q",
    "Library/Application Support/kiro-cli",
    "Library/Application Support/amazon-q",
    ".kirocrew/.env",
    # Security Event Log trust root (Talos finding cdf82704). The SEL is a
    # tamper-evident, HMAC-SHA256-chained audit trail (``sel.py``): each entry
    # signs over the previous entry's hash using the key in ``sel_hmac.key``.
    # An audited agent that could fs_read the key could forge/rewrite the entire
    # chain and erase evidence of its own actions, and one that could read/rewrite
    # ``security_events.jsonl`` could tamper with the log directly. Both are the
    # governed subject's own audit root, so — like ``.kirocrew/.env`` — they must
    # never be readable OR writable through the shared file gate
    # (``is_sensitive_path``). The gateway's own SEL writer/reader (``sel.py``,
    # ``session_health.py``) opens these files directly and does NOT route through
    # this gate, so legitimate audit logging still works. (Deeper hardening — an
    # out-of-process signer that never exposes the key to the agent's namespace —
    # is documented future work; this closes the read/rewrite path.)
    ".kirocrew/sel_hmac.key",
    ".kirocrew/security_events.jsonl",
    # App admission trust root (keystone invariant). ``apps/admission.py`` loads
    # ``config_dir()/app_admission.json`` as the SOLE fleet-controlled App Kit
    # admission ceiling (kill-switch / allowlist / require_signature) — the one
    # thing an app can never source itself. An absent file falls open (admit), so
    # a governed agent that could fs_write/tee/rm this file could delete or neuter
    # its own admission ceiling and re-admit a banned/unsigned app. Like the SEL
    # trust roots above, it is the governed subject's own ceiling and must never be
    # readable OR writable through the shared file gate (``is_sensitive_path``).
    # The gateway's own loader (``apps.admission.load_app_admission_policy``) opens
    # it directly via ``path.read_text`` and does NOT route through this gate, so
    # legitimate policy loading still works.
    ".kirocrew/app_admission.json",
    # Governance trust-root files (KEYSTONE of the two-level governance model).
    # Under "secure by default, not by mandate" the ONLY thing preventing a
    # prompt-injected agent from rewriting its own ceiling is that it cannot
    # WRITE these files — and is_sensitive_path() is the shared read+write gate
    # (hooks.on_tool_call, validate_file_path, artifacts) across every surface.
    # The agent (the governed subject) is blocked; the operator (trust root)
    # edits them out-of-band.  admission_policy.json is the existing plugin
    # trust root; security_policy.json + profiles/ are the new governance ones.
    ".kirocrew/security_policy.json",
    ".kirocrew/profiles",
    ".kirocrew/admission_policy.json",
    # KiroCrew's own dashboard-auth secrets (Mesh-2369). ``token_signing.key``
    # (dashboard/token_secret.py) signs every access + refresh token;
    # ``refresh_chains.json`` (dashboard/refresh_tokens.py) stores refresh-token
    # chain state; ``.local_secret`` (server.py / cli_commands.py / mcp_core.py /
    # cron_script.py / mcp_shared.py) is the shared internal-auth secret used to
    # authenticate MCP/cron/hook callbacks back into the gateway. These are this
    # host's own crown-jewel credentials: like the SEL trust root (sel_hmac.key),
    # the app-admission root, and the governance security_policy.json above, an
    # agent that could fs_read them could forge dashboard auth tokens or
    # impersonate internal callers. All legitimate readers (token_secret.py,
    # refresh_tokens.py, cli_commands.py, mcp_core.py, cron_script.py,
    # mcp_shared.py, mcp_playwright_proxy.py, cli_server.py, mcp_cron.py) open
    # these files directly via ``Path.read_text()``/``open()`` and do NOT route
    # through this gate, so legitimate token minting/verification still works.
    ".kirocrew/token_signing.key",
    ".kirocrew/refresh_chains.json",
    ".kirocrew/.local_secret",
]

# ── Write-protected paths (block modification, allow reads) ──
# Runtime config files carry security-relevant resource ceilings (concurrent
# subagents, per-agent turn budget, warm-pool size). A prompt-injected agent
# with file-write access must not be able to rewrite these to inflate its own
# limits and drive host resource exhaustion (pentest — config-loader bound
# bypass, recommendation: block agent tools from modifying config files).
#
# They are DELIBERATELY NOT in ``_SENSITIVE_HOME_DIRS`` above: that list is the
# shared read+write gate, and reading config.json is routine and intended (the
# dashboard file viewer, ``cat``, and knowledge indexing all read it). We
# instead block only WRITES, at the agent file-edit tool gate
# (hooks.on_tool_call), via ``is_sensitive_write_path``. This is defense in
# depth on top of the loader's load-time clamp, which already neutralizes any
# inflated on-disk value no matter how it was written. The operator edits config
# out-of-band (dashboard config API / CLI), which do NOT route through this
# gate, so legitimate config changes still work.
_WRITE_PROTECTED_HOME_PATHS: list[str] = [
    ".kirocrew/config.json",
    ".kirocrew/config.local.json",
]

# Regex for bash commands that read sensitive paths.
# Matches: cat, head, tail, less, more, strings, xxd, base64, cp, scp, open,
# awk, od, nl, sed, perl (read verbs that can access file contents via path args)
# followed by a path containing any sensitive dir.
_READ_CMDS = r"(?:cat|head|tail|less|more|strings|xxd|base64|cp|scp|open|vi|vim|nano|code|awk|od|nl|sed|perl)\s"

# Regex for bash commands that WRITE/MODIFY a path argument.  Reads alone were
# not enough: a prompt-injected agent could rewrite the governance trust-root
# (or plant a credential) with a write verb that carries no redirect char and
# is not a read verb — e.g. ``tee ~/.kirocrew/security_policy.json``,
# ``mv evil ~/.kirocrew/profiles/x.json``, ``sed -i ... ~/.aws/credentials``,
# ``dd of=...``, ``truncate``, ``ln -sf``, ``install``, plus archive-extraction
# and VCS-checkout verbs that materialise a file at a destination
# (``tar -xf … -C``, ``unzip -d``, ``git checkout/restore -- <path>``).  This
# list is defense-in-depth; the verb-independent catch-all below is the real
# backstop, so a write verb we forgot is still caught when it names a
# sensitive path as an argument.
# NOTE: ``git`` is narrowed to the verbs that actually MATERIALISE a file —
# a bare ``git`` would over-block read-only inspection (``git log/status/diff/
# show/blame/grep -- <sensitive path>``) that operators run during incident
# triage. The verb-independent catch-all still flags a sensitive-path token
# regardless of git verb, so this only trims false positives (CR-284272012).
_WRITE_CMDS = (
    r"(?:tee|mv|dd|truncate|ln|install|sed|chmod|chown|rm|rmdir|touch|mkdir|rsync"
    r"|tar|unzip|gunzip|gzip|cpio|patch"
    r"|git\s+(?:checkout|restore|reset|apply|clean|rm|mv|stash))\s"
)

# Matches python/ruby/perl one-liners that open sensitive paths
_SCRIPT_OPEN = r"(?:python|ruby|perl)\S*\s.*open\s*\("


def _build_sensitive_regex() -> re.Pattern[str]:
    """Build a compiled regex matching bash reads OR writes of sensitive paths.

    Three matching strategies, OR'd:
      1. a READ verb / WRITE verb / script-open / shell-redirect followed by a
         sensitive path (the original verb-anchored form);
      2. a verb-INDEPENDENT catch-all: a sensitive path appearing ANYWHERE in
         the command as an argument token.  This is the real backstop — a write
         verb the allowlist forgot (or a novel one) is still blocked because the
         destination path is sensitive.  Reading a sensitive path is itself
         already blocked by is_sensitive_path on the file-read title, so flagging
         any command that *names* the trust-root/credential path is correct and
         fail-safe.
    The home anchor accepts ``~`` / ``$HOME`` / the literal ``Path.home()`` AND a
    generic ``/home/<user>`` / ``/Users/<user>`` literal so an unexpanded
    ``/home/$USER/...`` or another user's literal path is still caught.
    """
    home = re.escape(str(Path.home()))
    tilde = re.escape("~")
    home_var = re.escape("$HOME")
    # Generic home roots so a literal "/home/<user>" or "/Users/<user>" token
    # (not just the running user's resolved home) is anchored too.
    generic_home = r"/home/[^/\s]+|/Users/[^/\s]+"
    home_alts = f"(?:{home}|{tilde}|{home_var}|{generic_home})"
    escaped_dirs = [re.escape(d) for d in _SENSITIVE_HOME_DIRS]
    dirs_pattern = "|".join(escaped_dirs)
    sensitive_path = rf"{home_alts}/(?:{dirs_pattern})(?:/|\s|$|['\"])"
    return re.compile(
        # (1) verb/redirect-anchored, OR (2) verb-independent: the sensitive path
        # appears anywhere as a token.  The token anchor accepts start-of-string
        # plus the separators that precede a path argument: whitespace, quote,
        # ``=`` (VAR=path), AND ``:``/``,``/``;`` (option:path, PATH-style
        # colon lists, comma/semicolon-joined args) — without the latter a
        # ``FOO=bar:~/.aws/credentials`` or ``PATH=/x:~/.ssh/id_rsa`` token slips
        # past the backstop while no verb branch fires either (CR-284272012).
        rf"(?:(?:{_READ_CMDS}.*|{_WRITE_CMDS}.*|{_SCRIPT_OPEN}.*|.*[<>|]\s*)"
        rf"{sensitive_path}"
        rf"|(?:^|.*[\s'\"=:,;]){sensitive_path})",
        re.IGNORECASE,
    )


_SENSITIVE_RE: re.Pattern[str] | None = None


def _get_sensitive_re() -> re.Pattern[str]:
    global _SENSITIVE_RE
    if _SENSITIVE_RE is None:
        _SENSITIVE_RE = _build_sensitive_regex()
    return _SENSITIVE_RE


def _path_in_home_dirs(path_str: str, home_dirs: list[str], base_dir: str | None = None) -> bool:
    """Return True if *path_str* resolves under any of *home_dirs* (``$HOME``-relative).

    Shared matching core for :func:`is_sensitive_path` (read+write gate,
    ``_SENSITIVE_HOME_DIRS``) and :func:`is_sensitive_write_path` (write-only
    gate, the read+write set PLUS ``_WRITE_PROTECTED_HOME_PATHS``). Keeping one
    implementation means the symlink/casefold hardening below cannot drift
    between the two gates.

    ── Symlink robustness (pentest AWS-345 / AWS-62) ──
    A workspace symlink pointing at ``~/.aws/credentials`` (absolute OR relative
    ``../../.aws/credentials`` traversal) must NOT be readable through the link.
    We therefore check MULTIPLE candidate forms of the input and return True if
    ANY of them lands in a matched location:

      1. the fully symlink-RESOLVED canonical target (``realpath`` /
         ``Path.resolve`` — follows every symlink in the chain, including
         intermediate directories and the final component).  This is what
         defeats the symlink bypass: the resolved target of the link is
         ``~/.aws/credentials`` even though the link's own name is benign.
      2. the LEXICALLY-normalized path (no symlink following) and the raw
         expanded string — so a path that *textually* names a matched dir is
         still caught when resolution fails (dangling link, permission error).

    ``base_dir`` anchors a *relative* input against the caller's known working
    directory (e.g. the agent's workspace cwd) so a relative title like
    ``sub/cfg.ini`` resolves against the real directory rather than whatever CWD
    the gateway process happens to have.  Absolute inputs are unaffected;
    ``base_dir=None`` preserves the historical CWD-relative behavior.
    """
    if not path_str:
        return False

    # Expand ~ and $HOME
    expanded = os.path.expanduser(os.path.expandvars(path_str))

    # Anchor a relative input against the supplied workspace dir so it resolves
    # to the real file rather than the gateway's CWD.  Absolutize base_dir
    # itself first — if a caller passes a relative base_dir, os.path.join would
    # re-anchor against the process CWD (the very thing the parameter exists to
    # avoid), giving zero protection when CWD is unrelated to the workspace.
    if base_dir and not os.path.isabs(expanded):
        expanded = os.path.join(os.path.abspath(base_dir), expanded)

    # Build the candidate forms.  Symlink-resolved forms defeat a link bypass;
    # the lexical forms are the fail-safe fallback when resolution cannot
    # complete (over-matching a sensitive-looking path is the safe direction).
    candidates: set[str] = set()
    try:
        candidates.add(os.path.realpath(expanded))
    except (OSError, ValueError):
        pass
    try:
        # Guarded false-positive: this resolve() is INSIDE is_sensitive_path — the
        # sanitizer itself — building candidate forms to CHECK a path against the
        # sensitive denylist. It performs no read/write. CodeQL surfaces
        # py/path-injection here only because a new caller (artifact relocate)
        # reaches it with user input; the function's whole purpose is to vet that
        # input, so suppress the alert on the resolution step.
        candidates.add(str(Path(expanded).resolve()))  # lgtm[py/path-injection]
    except (OSError, ValueError, RuntimeError):
        pass
    candidates.add(os.path.normpath(expanded))
    candidates.add(expanded)

    try:
        home = str(Path.home().resolve())
    except (OSError, ValueError):
        home = str(Path.home())
    # Compare against the sensitive dirs anchored at BOTH the logical home and
    # its realpath.  On macOS the per-user temp/home prefix can itself be
    # reached via OS symlinks (``/var`` → ``/private/var``); folding both roots
    # in means a resolved candidate under either spelling is still matched.
    sensitive_targets: set[str] = {os.path.join(home, d).casefold() for d in home_dirs}
    home_real = os.path.realpath(home)
    if home_real.casefold() != home.casefold():
        sensitive_targets |= {os.path.join(home_real, d).casefold() for d in home_dirs}
    # When KIROCREW_HOME points to a non-default path, the keystone secrets
    # (token_signing.key, refresh_chains.json, .local_secret, sel_hmac.key,
    # security_policy.json etc.) live there — NOT under ~/.kirocrew. Without
    # this expansion any ".kirocrew/X" entry in the home_dirs list would miss
    # the real file location, letting the agent read/write its own signing key
    # or governance ceiling via the custom KIROCREW_HOME. Expand each entry
    # prefixed with ".kirocrew" using the env-override root ADDITIONALLY (the
    # ~/~ root stays so both locations are always covered).
    kiro_home_env = os.environ.get("KIROCREW_HOME")
    if kiro_home_env:
        try:
            kiro_home = str(Path(kiro_home_env).expanduser().resolve())
        except (OSError, ValueError):
            kiro_home = os.path.abspath(os.path.expanduser(kiro_home_env))
        _kiro_prefix = ".kirocrew" + os.sep
        for d in home_dirs:
            if d.startswith(_kiro_prefix) or d == ".kirocrew":
                tail = d[len(".kirocrew") :]  # includes leading / or is ""
                full = os.path.join(kiro_home, tail.lstrip(os.sep)) if tail else kiro_home
                sensitive_targets.add(full.casefold())
                # Also add the resolved form in case the env value itself has
                # symlinks (matches the home/home_real duality above).
                try:
                    sensitive_targets.add(os.path.realpath(full).casefold())
                except (OSError, ValueError):
                    pass

    # Case-fold both sides for the membership test.  On a case-insensitive
    # filesystem (macOS APFS/HFS+ default — a supported platform) the OS opens
    # ``~/.kirocrew/Security_Policy.json`` and ``~/.kirocrew/security_policy.json``
    # as the SAME file, so a byte-exact comparison would let the agent write its
    # own governance ceiling via an alternate-case path. Folding is strictly more
    # protective (it can only ever over-match an alternate-case variant of an
    # already-sensitive path, which is itself suspicious), so it is safe on
    # case-sensitive Linux too — matching the IGNORECASE bash-read matcher.
    for cand in candidates:
        cand_cf = cand.casefold()
        for sensitive_path in sensitive_targets:
            if cand_cf == sensitive_path or cand_cf.startswith(sensitive_path + os.sep):
                return True
    return False


def is_sensitive_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path points to a read+write-sensitive location.

    Used across every file-access surface (hooks.on_tool_call, validate_file_path,
    artifacts, dashboard file I/O, knowledge indexing) to block BOTH reads and
    writes of credential files and the governance trust-root
    (:data:`_SENSITIVE_HOME_DIRS`). See :func:`_path_in_home_dirs` for the
    symlink/casefold matching contract.
    """
    return _path_in_home_dirs(path_str, _SENSITIVE_HOME_DIRS, base_dir)


def is_sensitive_write_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path must not be MODIFIED by an agent tool.

    Superset of :func:`is_sensitive_path`: everything that is read+write blocked
    PLUS the write-only-protected runtime config files
    (:data:`_WRITE_PROTECTED_HOME_PATHS`), which stay readable but must not be
    written by the agent. Enforced at the file-edit tool gate
    (``hooks.on_tool_call`` on the ACP ``edit`` kind) — see
    :data:`_WRITE_PROTECTED_HOME_PATHS` for the rationale.
    """
    return _path_in_home_dirs(
        path_str, _SENSITIVE_HOME_DIRS + _WRITE_PROTECTED_HOME_PATHS, base_dir
    )


# Archive/extraction destination flags (tar -C, unzip -d, rsync dest) pointing
# INTO the governance trust-root parent ``~/.kirocrew`` — an extraction there can
# drop/overwrite ``security_policy.json`` or a ``profiles/`` entry even though the
# bare ``~/.kirocrew`` dir is not itself a sensitive-path entry.  Match the
# destination-dir form specifically so normal ``~/.kirocrew`` access (sessions.db,
# config.json) is not over-blocked.
_EXTRACT_INTO_TRUST_ROOT_RE = re.compile(
    r"-(?:C|d)\s+(?:~|\$HOME|/home/[^/\s]+|/Users/[^/\s]+|"
    + re.escape(str(Path.home()))
    + r")/\.kirocrew(?:/[^\s]*)?(?:\s|$|['\"])",
    re.IGNORECASE,
)

# ── Symlink-staging to a sensitive target via RELATIVE traversal ──
# The home-anchored ~/$HOME/absolute forms of ``ln -sf ~/.aws/credentials link``
# are already caught by _build_sensitive_regex (the sensitive path appears as an
# argument token).  What that matcher CANNOT see is a sensitive dir named through
# pure relative traversal — ``ln -sf ../../../.aws/credentials link`` — because
# it has no home anchor.  Creating such a symlink is the staging step of the
# pentest attack chain (AWS-345 / AWS-62, recommendation item 3): a pre-existing
# link to a credential file lets a later in-workspace read follow it.  We block
# the CREATION verbs (``ln``, ``cp -s``/``--symbolic-link``) when any token
# names a sensitive dir via dot-slash traversal.
_SENSITIVE_SEGMENT_ALT = "|".join(re.escape(d) for d in _SENSITIVE_HOME_DIRS)
_RELATIVE_SENSITIVE_RE = re.compile(
    rf"(?:^|[\s'\"=:,;])(?:\.\.?/)+(?:{_SENSITIVE_SEGMENT_ALT})(?:/|\s|$|['\"])",
    re.IGNORECASE,
)


# ── Read verbs for normalizer second-pass ──
# Programs that can read file contents. Used to detect path-based credential
# access via the normalizer when the regex first-pass misses obfuscated forms.
_NORMALIZER_READ_VERBS: frozenset[str] = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "strings",
        "xxd",
        "base64",
        "cp",
        "scp",
        "open",
        "vi",
        "vim",
        "nano",
        "code",
        # Extended coverage for relative-traversal attacks (pentest finding):
        "awk",
        "od",
        "nl",
        "sed",
        "perl",
        "grep",
        "egrep",
        "fgrep",
        "sort",
        "uniq",
        "wc",
        "cut",
        "paste",
        "diff",
        "tee",
        "xargs",
        "file",
        "stat",
        "md5sum",
        "sha256sum",
        "python",
        "python3",
        "ruby",
        "node",
    }
)


def is_sensitive_bash_command(command: str) -> str | None:
    """Check if a bash command reads sensitive paths, accesses IMDS, or leaks env creds.

    Uses a two-pass approach:
    1. **Regex first-pass (fast):** Pattern match against known read-verb + sensitive
       path combinations. Catches unobfuscated commands instantly.
    2. **Normalizer second-pass:** Tokenizes the command via
       ``normalize_shell_command`` (strips shell quoting, expands $HOME/~, resolves
       relative paths), then routes each path-like token through
       ``is_sensitive_path()`` to catch obfuscation (e.g. ``ca""t ~/.aws/credentials``,
       ``awk '{print}' $HOME/.ssh/id_rsa``, ``sed -n p ~/../../etc/shadow``).

    Returns denial reason string, or None if clean.
    """
    # ── Pass 1: regex fast-path ──
    if _get_sensitive_re().search(command):
        return "Blocked: command accesses sensitive credential path"
    if _EXTRACT_INTO_TRUST_ROOT_RE.search(command):
        return "Blocked: command extracts into the governance trust-root directory"
    # Block ANY command referencing a sensitive path via relative traversal,
    # regardless of verb.  The home-anchored/absolute forms are already caught
    # by the matcher above; this covers the relative-traversal forms that escape
    # it (was gated on ln/cp only, so dd/base64/xxd/head/tail slipped past).
    if _RELATIVE_SENSITIVE_RE.search(command):
        return "Blocked: command references a sensitive credential path via relative traversal"

    # ── Pass 2: normalizer-based sensitive path detection ──
    normalizer_result = _check_sensitive_via_normalizer(command)
    if normalizer_result:
        return normalizer_result

    # IMDS access via any IP encoding (decimal, hex, octal, IPv6-mapped)
    imds_result = _check_imds_access(command)
    if imds_result:
        return imds_result
    # Environment credential exfiltration (declare -p, env|grep, printenv, etc.)
    env_result = _check_env_credential_access(command)
    if env_result:
        return env_result
    return None


def _check_sensitive_via_normalizer(command: str) -> str | None:
    """Normalizer second-pass: tokenize command and route paths through is_sensitive_path.

    Catches obfuscation the regex first-pass cannot:
    - Quoted command names: ``ca""t ~/.aws/credentials``
    - Variable expansion: ``$HOME/.ssh/id_rsa``
    - Relative traversal: ``awk '{print}' ~/../../.aws/credentials``
    - Mixed evasion: ``"cat" ~/.aws/credentials``

    Only triggers when a recognized read verb is present in the resolved tokens
    (avoids false positives on write/create commands).

    Returns denial reason string, or None if clean.
    """
    try:
        tokens = normalize_shell_command(command)
    except Exception:
        return None

    if not tokens:
        return None

    # Check if any token resolves to a known read verb (by basename, so
    # /usr/bin/cat is recognized as "cat").
    has_read_verb = False
    for token in tokens:
        if not token:
            continue
        basename = os.path.basename(token).lower()
        if basename in _NORMALIZER_READ_VERBS:
            has_read_verb = True
            break

    if not has_read_verb:
        return None

    # Route each path-like token through is_sensitive_path()
    for token in tokens:
        if not token:
            continue
        # Skip flags
        if token.startswith("-"):
            continue
        # Skip tokens that ARE the read verb itself
        basename = os.path.basename(token).lower()
        if basename in _NORMALIZER_READ_VERBS:
            continue
        # Only check tokens that look like filesystem paths
        if not _is_path_like(token):
            continue
        # is_sensitive_path handles symlink resolution, traversal, ~ expansion,
        # $HOME expansion, and all sensitive directory checks
        if is_sensitive_path(token):
            return (
                "Blocked: command accesses sensitive credential path "
                f"(resolved via normalizer: {token[:80]})"
            )
    return None

# ── Binary File MIME Allowlist ──
# Files whose UTF-8 decode fails are accepted by file_send / outbox only when
# their guessed MIME type is in this allowlist. Deny-by-default; expand only
# when a use case is reviewed for safe rendering on the dashboard origin.
# SVG is intentionally excluded (can carry inline scripts); served as
# attachment by the download handler for defense-in-depth.
BINARY_MIME_ALLOWLIST: frozenset[str] = frozenset(
    {
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
        "audio/ogg",
        "audio/flac",
        "audio/aac",
        "audio/mp4",
        "audio/webm",
        "audio/opus",
        "video/mp4",
        "video/webm",
        "video/ogg",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "application/pdf",
    }
)
