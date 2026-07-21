"""deploy-web staging + local_dir validation + content-scan leaves.

Leaf module: request-input validation specs, allow-listed root resolution,
TOCTOU-safe staging copy, artifact HTML staging, and the recursive
sensitive-path / content scanners. Imports only stable kiro_crew modules
(validation, profiles specs, render, scan, security, hooks) — never another
deploy submodule — so it can be imported by config/core/pending freely.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from kiro_crew.config.paths import config_dir
from kiro_crew.deploy import profiles as profiles_mod
from kiro_crew.deploy.render import render_standalone
from kiro_crew.deploy.scan import Finding, is_credential_finding, scan_content
from kiro_crew.security import is_sensitive_path
from kiro_crew.validation import FieldSpec

# --- local_dir input validation (AutoSDE f-* security-controls) ------------
# local_dir is request-supplied and LLM-influenceable via the chat-native skill,
# and flows to the filesystem + the `aws s3 sync` subprocess. Validate it with a
# validation.py schema (type/length/charset) and confine the resolved real path
# to an allow-listed root before any filesystem/subprocess use.
_LOCAL_DIR_RE = re.compile(r"^[A-Za-z0-9 _\-./~]+$")
_LOCAL_DIR_SPEC = FieldSpec(name="local_dir", type=str, max_len=4096, pattern=_LOCAL_DIR_RE)

# profile/region are LLM-influenceable (chat-native skill) and flow into subprocess
# argv (--profile/--region) on every aws call, so they get schema validation too.
# Both allow empty (clears profile / falls back to default region); the pattern is
# only enforced on non-empty values by validate_field.
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PROFILE_SPEC = FieldSpec(name="profile", type=str, max_len=128, pattern=_PROFILE_RE)
_REGION_SPEC = profiles_mod.REGION_SPEC

# artifact_slug is LLM-influenceable (chat-native skill) and is used in a store
# lookup, so validate it like the other request inputs.
_ARTIFACT_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ARTIFACT_SLUG_SPEC = FieldSpec(name="artifact_slug", type=str, max_len=128, pattern=_ARTIFACT_SLUG_RE)

# Pre-publish scan: skip files larger than this (likely binary/media) when
# scanning a local_dir's full contents.
_SCAN_SIZE_LIMIT = 2 * 1024 * 1024  # 2 MiB


def _safe_resolve(p: Path) -> Path:
    """Resolve a path (following symlinks); fall back to the raw path on error."""
    try:
        return p.resolve()
    except OSError:
        return p


def _allowed_local_roots() -> list[Path]:
    """Resolved directories a publishable local_dir may live under.

    Aligned with the subagent_cwd_allowed_roots config and the file-explorer
    authorization boundary — only the caller's known workspace roots are
    permitted, not arbitrary filesystem locations.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    roots: list[Path] = []
    try:
        cfg = KiroCrewConfig.load()
        configured = cfg.agent.subagent_cwd_allowed_roots
    except Exception:
        configured = []
    if configured:
        for r in configured:
            expanded = Path(os.path.expanduser(r))
            try:
                if expanded.exists():
                    roots.append(expanded.resolve())
            except OSError:
                pass
    # Fallback: if no config, allow ~/workplace and ~/workspace (standard devbox)
    if not roots:
        for cand in (Path.home() / "workplace", Path.home() / "workspace",
                     Path("/local/home") / os.environ.get("USER", "") / "workplace"):
            try:
                if cand.exists():
                    roots.append(cand.resolve())
            except OSError:
                pass
    # Always allow the agent's own config-dir workspace (e.g. ~/.kirocrew/workspace).
    try:
        cdir_ws = config_dir() / "workspace"
        if cdir_ws.exists() and cdir_ws.resolve() not in roots:
            roots.append(cdir_ws.resolve())
    except OSError:
        pass
    # Also include any registered workspaces (cfg.workspaces[*].dir).
    try:
        cfg = KiroCrewConfig.load()
        for _ws_name, ws_cfg in cfg.workspaces.items():
            ws_dir = Path(os.path.expanduser(ws_cfg.dir))
            try:
                if ws_dir.exists():
                    resolved = ws_dir.resolve()
                    if resolved not in roots:
                        roots.append(resolved)
            except OSError:
                pass
    except Exception:
        pass
    # Always allow the deploy staging dir (for artifact staging, F2/F3).
    try:
        sr = _staging_root()
        roots.append(sr.resolve())
    except OSError:
        pass
    return roots


def _staging_root() -> Path:
    """Return (and validate) the private staging root under config_dir.

    Raises RuntimeError if the path is a symlink or not owned by the current user.
    """
    import stat

    sr = config_dir() / "deploy-staging"
    os.makedirs(str(sr), mode=0o700, exist_ok=True)
    st = os.lstat(str(sr))
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(
            f"deploy staging root {sr} is a symlink — refusing (possible symlink attack)"
        )
    if st.st_uid != os.getuid():
        raise RuntimeError(
            f"deploy staging root {sr} not owned by current user (uid {os.getuid()}, "
            f"owner {st.st_uid})"
        )
    return sr


def _stage_tree_safe(source: Path, staging_root: Path) -> Path:
    """Hook-gated staging copy with hardlink rejection (R19 F2).

    Replaces shutil.copytree: walks the source tree, reads each file through
    hooks.safe_read_file_bytes_nolink (O_NOFOLLOW + fstat nlink + is_sensitive_path gate), and
    rejects files with st_nlink > 1 (hardlinks that could stage sensitive
    content bypassing the symlink check). Dirs are recreated with 0o700.

    Returns the path to the staged tree root. Raises RuntimeError on rejection.
    """
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    dst = staging_root / "tree"
    os.makedirs(str(dst), mode=0o700, exist_ok=True)

    for dirpath, dirnames, filenames in os.walk(str(source), followlinks=False):
        # R23 F3: a symlinked DIRECTORY appears in dirnames but is not
        # descended (followlinks=False) and carries no file entries — it would
        # silently vanish from the snapshot, deploying something different
        # from the approved tree. Reject explicitly instead.
        for dname in dirnames:
            dpath = Path(dirpath) / dname
            if dpath.is_symlink():
                raise RuntimeError(
                    f"symlink-in-tree: symlinked directory at {dpath} — deploy blocked"
                )
        rel_dir = os.path.relpath(dirpath, str(source))
        target_dir = dst / rel_dir if rel_dir != "." else dst
        os.makedirs(str(target_dir), mode=0o700, exist_ok=True)

        for fname in filenames:
            src_file = Path(dirpath) / fname
            # Reject symlinks (already handled upstream, defense in depth)
            if src_file.is_symlink():
                raise RuntimeError(
                    f"symlink-in-tree: symlink found at {src_file} — deploy blocked"
                )
            # R19 F2: reject hardlinks (st_nlink > 1)
            try:
                st = os.lstat(str(src_file))
            except OSError:
                raise RuntimeError(
                    f"hardlink-in-tree: cannot stat {src_file} — deploy blocked"
                )
            if st.st_nlink > 1:
                raise RuntimeError(
                    f"hardlink-in-tree: {src_file} has nlink={st.st_nlink} "
                    f"(hardlinked file) — deploy blocked"
                )
            # Read through the hook gate (sensitive-path + O_NOFOLLOW).
            # R30 F1: the nolink variant fstat()s the OPENED descriptor and
            # rejects st_nlink > 1 / non-regular inodes — the lstat() above is
            # only a fast-path pre-check; the authoritative hardlink rejection
            # is pinned to the same inode that gets read (no lstat->open race).
            # R33 F1: within_root pins containment to the OPENED fd — a nested
            # dir swapped for a symlink after the walk cannot smuggle files
            # from outside the approved tree into the public deployment.
            try:
                data = safe_read_file_bytes_nolink(
                    str(src_file), within_root=str(source)
                )
            except FileTooLargeError as e:
                # R33 F2: tree cap (200 MiB) > per-file cap — surface as a
                # structured staging rejection (409), not an escaping 500.
                raise RuntimeError(
                    f"file-too-large: {src_file} exceeds the per-file read cap "
                    f"({e}) — deploy blocked"
                ) from e
            if data is None:
                raise RuntimeError(
                    f"staging-read-blocked: {src_file} rejected by safe_read_file_bytes_nolink "
                    f"— deploy blocked"
                )
            target_file = target_dir / fname
            target_file.write_bytes(data)

    return dst


def _stage_artifact_html(kind: str, content: str, name: str) -> tuple[list, str, int]:
    """Render + scan + write an artifact to a temp dir. Blocking (filesystem +
    CPU) -- must be called via asyncio.to_thread, never directly on the loop.

    Raises ValueError for webapp-kind artifacts (not deployable HTML).
    """
    if kind == "webapp":
        raise ValueError(
            "webapp artifacts contain an app summary, not deployable HTML; "
            "deploy the app's built directory via local_dir instead"
        )
    html = render_standalone(kind, content, title=name)
    findings = scan_content(html)
    tmp_dir = tempfile.mkdtemp(prefix="deploy-web-")
    Path(tmp_dir, "index.html").write_text(html, encoding="utf-8")
    return findings, tmp_dir, len(html.encode("utf-8"))


def _dir_contains_sensitive(src: Path, resolved: Path) -> bool:
    """Recursive sensitive-path walk. Blocking -- call via asyncio.to_thread."""
    if is_sensitive_path(str(src)) or is_sensitive_path(str(resolved)):
        return True
    for p in resolved.rglob("*"):
        resolved_child = _safe_resolve(p)
        # A symlink pointing outside the tree is itself suspicious --
        # treat it as sensitive (fail closed, block the deploy).
        if not _is_within(resolved_child, resolved):
            return True
        if is_sensitive_path(str(resolved_child)):
            return True
    return False


def _scan_tree(src: Path) -> tuple[list, int]:
    """Recursive content scan + size sum. Blocking -- call via asyncio.to_thread."""
    from kiro_crew.hooks import safe_read_file_bytes

    findings: list = []
    resolved_root = _safe_resolve(src)
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        # Symlink containment: resolve the target and ensure it stays within
        # the source tree. A symlink pointing outside -> fail closed (finding
        # surfaced, target content NEVER read). This blocks attacks where an
        # LLM-supplied dir contains `link -> /etc/passwd` or similar.
        resolved_file = _safe_resolve(f)
        if not _is_within(resolved_file, resolved_root):
            findings.append(Finding(
                kind="symlink-escape",
                snippet=f"{f.name} -> {resolved_file} escapes source tree",
                line=0,
                severity="credential",
            ))
            continue
        try:
            file_size = f.stat().st_size
        except OSError:
            findings.append(Finding(
                kind="unreadable-file",
                snippet=f"{f.name} — cannot stat (permission denied or I/O error)",
                line=0,
                severity="credential",
            ))
            continue
        if file_size >= _SCAN_SIZE_LIMIT:
            # Fail closed: a file too large to scan must not slip through to a
            # public upload unexamined -- surface it as a finding instead.
            findings.append(Finding(
                kind="unscanned-large-file",
                snippet=f"{f.name} ({file_size} bytes) exceeds the scan size limit",
                line=0,
                severity="credential",
            ))
            continue
        # F2: Read as BYTES first to handle binary files (images, fonts, etc.)
        # without crashing on UnicodeDecodeError. Binary files are identified by
        # null bytes in the first 8KiB — they skip content scanning but are NOT
        # exempt from sensitive-path rejection (already checked via safe_read_file_bytes).
        raw_bytes = safe_read_file_bytes(str(resolved_file))
        if raw_bytes is None:
            # safe_read_file_bytes returns None when is_sensitive_path rejects
            # or file is unreadable — treat as credential finding (fail closed).
            findings.append(Finding(
                kind="hook-denied",
                snippet=f"{f.name} — denied by file-read security gate",
                line=0,
                severity="credential",
            ))
            continue
        # F1 (R11): Credential patterns MUST be evaluated on every file regardless
        # of binary classification. A NUL-prepended HTML/JS file is still parseable
        # by browsers and can contain secrets. Decode with errors="replace" so NUL
        # bytes become U+FFFD but AKIA patterns remain intact.
        content = raw_bytes.decode("utf-8", errors="replace")
        cred_findings = [f for f in scan_content(content) if is_credential_finding(f)]
        findings.extend(cred_findings)

        # Binary detection: null byte in first 8KiB -> skip non-credential
        # content scanning (internal-host, ARN, account-id heuristics produce
        # noise on binary files). Credential findings above are kept regardless.
        if b"\x00" in raw_bytes[:8192]:
            continue
        # Full content scan (non-credential findings) for text files only.
        non_cred_findings = [f for f in scan_content(content) if not is_credential_finding(f)]
        findings.extend(non_cred_findings)
    byte_size = 0
    for p in src.rglob("*"):
        if p.is_file():
            try:
                byte_size += p.stat().st_size
            except OSError:
                pass  # already recorded as unreadable-file finding above
    return findings, byte_size


def _compute_content_digest(src_dir: str) -> str:
    """Compute a deterministic content digest for a staged/source directory.

    Produces a sha256 hash of a sorted manifest of (relative_path, size, content_hash)
    per file. Used at preview time and re-verified at confirm time to detect
    content changes between preview and confirmation (F1 R8).
    """
    import hashlib

    root = Path(src_dir)
    entries: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                rel = str(p.relative_to(root))
                content = p.read_bytes()
                file_hash = hashlib.sha256(content).hexdigest()[:16]
                entries.append(f"{rel}:{len(content)}:{file_hash}")
            except OSError:
                pass
    manifest = "\n".join(entries)
    return hashlib.sha256(manifest.encode()).hexdigest()


def _compute_tree_size_global(root: Path) -> int:
    """Total bytes of all regular files in a directory tree. Blocking."""
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _is_within(path: Path, root: Path) -> bool:
    # normpath + prefix check is the canonical containment barrier (recognized
    # by static analyzers); equivalent to relative_to for absolute resolved paths.
    p = os.path.normpath(str(path))
    r = os.path.normpath(str(root))
    return p == r or p.startswith(r + os.sep)


# Re-exported for callers that reference the type name (parity with the pre-split
# handlers module, where Finding was a module global).
__all__ = [
    "Finding",
    "_LOCAL_DIR_RE", "_LOCAL_DIR_SPEC", "_PROFILE_RE", "_PROFILE_SPEC",
    "_REGION_SPEC", "_ARTIFACT_SLUG_RE", "_ARTIFACT_SLUG_SPEC", "_SCAN_SIZE_LIMIT",
    "_safe_resolve", "_allowed_local_roots", "_staging_root", "_stage_tree_safe",
    "_stage_artifact_html", "_dir_contains_sensitive", "_scan_tree",
    "_compute_content_digest", "_compute_tree_size_global", "_is_within",
]
