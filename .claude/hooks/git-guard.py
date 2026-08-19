#!/usr/bin/env python3
"""PreToolUse guard for Bash tool calls made by Claude Code in this repo.

Encodes the git/data contract from CLAUDE.md § 2 mechanically. Best-effort by
design: it tokenizes each shell segment (split on && || ; | and newlines,
recursing into `bash -c "..."`/`sh -c`/`eval`/`xargs` strings) so quoting,
global git options (`-c k=v`, `--no-pager`, `-C dir`), and chaining do not
defeat it. It cannot see through arbitrary interpreters (`python -c`, `$GIT`),
which is why CLAUDE.md § 3 also makes "route around the guard" a stop point.

  ALLOWED  git push [-u|--set-upstream] [-q|-v|-n|--dry-run|--no-verify]
             origin <feature-branch>          (plain, non-force, non-integration)
  BLOCKED  push to any other remote, to main/master/HEAD/klaude, force/lease/`+`,
           deletes, --all/--mirror/--tags, quoted/expanded/refs/heads refspecs;
           reset --hard/--merge/--keep, clean with force, branch -D/-f,
           checkout/restore of a whole tree or directory, stash drop/clear,
           filter-branch/filter-repo, reflog expire/delete, prune, gc --prune,
           update-ref -d, remote remove/rm/set-url/rename, worktree remove -f;
           gh pr merge, gh repo delete, gh release delete, mutating gh api;
           rm/mv/find -delete/rsync --delete/shred/truncate/redirect-writes
           aimed at ~/.kiro, ~/.claude, ~/.aws, ~/.ssh, $HOME itself, /,
           or $KIROCREW_HOME; any read/write of the keystone files
           (~/.kiro/crew/{.env,security_policy.json,computer_use.json,
           admission_policy.json,denied_commands.json,profiles/}); and any
           write/rm/chmod aimed at .claude/settings.json, .claude/hooks/.

Why main/upstream are special: local `main` TRACKS upstream/main
(kirodotdev/KiroCrew); a bare `git push` from main targets the upstream project.

Why `klaude` is also protected: it is the fork's integration branch (GitHub
default branch, production checks it out, the in-app updater and
`scripts/klaude-prod-update.sh` fast-forward/pull it). It lands via squash-merge
PR only, same as `main` -- a direct push would bypass review and could hand
production a broken build.

Exit 2 = block (stderr shown to the model). Exit 0 = allow. Fails OPEN on
malformed input so a guard bug never bricks a session.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

HOME = os.path.expanduser("~")
KIROCREW_HOME = os.environ.get("KIROCREW_HOME", "")
PROJECT = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

PUSH_OK_FLAGS = {"-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "-n", "--dry-run",
                 "--no-verify", "--porcelain", "--progress", "--no-progress"}
PUSH_BAD_FLAGS = {"-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror", "--all",
                  "--tags", "--delete", "-d", "--prune", "--follow-tags", "--atomic"}
FORBIDDEN_DST = {"main", "master", "HEAD", "klaude"}
REFSPEC_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# Directories/files under the operator's home that no Bash call may target.
PROTECTED_HOME_DIRS = (".kiro", ".claude", ".aws", ".ssh", ".gnupg")
KEYSTONE_LEAVES = ("security_policy.json", "computer_use.json", "admission_policy.json",
                   "denied_commands.json", "oauth_endpoints.json", "token_signing.key",
                   ".local_secret", ".env")
KEYSTONE_DIRS = ("profiles", "trust")
SELF_PROTECT = (".claude/settings.json", ".claude/hooks")

DESTROY_VERBS = {"rm", "shred", "truncate", "unlink", "rmdir"}
MOVE_VERBS = {"mv", "cp", "rsync", "tee", "dd", "sed", "perl", "chmod", "chown", "ln", "install"}
WRITE_VERBS = DESTROY_VERBS | {"mv", "cp", "scp", "rsync", "tee", "dd", "sed", "perl", "chmod",
                               "chown", "ln", "install", "touch", "patch"}


def block(reason: str, command: str) -> None:
    sys.stderr.write(
        f"BLOCKED by .claude/hooks/git-guard.py: {reason}\n"
        f"Command: {command}\n"
        "This needs the operator's explicit per-instance go (CLAUDE.md § 2/§ 3). "
        "Stop and ask; do not work around the guard.\n"
    )
    sys.exit(2)


# ---------- path helpers ----------

def _norm(tok: str) -> str:
    """Expand ~, $HOME, ${HOME}, quotes, and collapse ./ segments for path checks."""
    t = tok.strip("'\"")
    t = t.replace("${HOME}", HOME).replace("$HOME", HOME)
    if t.startswith("~"):
        t = HOME + t[1:]
    if KIROCREW_HOME:
        t = t.replace("${KIROCREW_HOME}", KIROCREW_HOME).replace("$KIROCREW_HOME", KIROCREW_HOME)
    return os.path.normpath(t) if t else t


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _protected_home_target(tok: str) -> str | None:
    """Return a reason if tok resolves onto a protected home dir/file (or home/root itself)."""
    if "$" in tok and "$HOME" not in tok and "${HOME}" not in tok and "KIROCREW_HOME" not in tok:
        # Unresolvable expansion aimed at a path-looking arg: refuse for rm-class verbs only.
        return None
    p = _norm(tok)
    if not p or not (p.startswith("/") or p.startswith("~")):
        return None
    if p in ("/", HOME) or p == HOME + "/*":
        return f"target '{tok}' is the operator's home or filesystem root"
    for d in PROTECTED_HOME_DIRS:
        if _under(p, os.path.join(HOME, d)):
            return f"target '{tok}' is under ~/{d}"
    if KIROCREW_HOME and _under(p, os.path.normpath(KIROCREW_HOME)):
        return f"target '{tok}' is under KIROCREW_HOME"
    return None


def _keystone_target(tok: str) -> str | None:
    p = _norm(tok)
    if not p.startswith("/"):
        return None
    for base in (os.path.join(HOME, ".kiro", "crew"), os.path.join(HOME, ".kirocrew"),
                 os.path.normpath(KIROCREW_HOME) if KIROCREW_HOME else ""):
        if not base:
            continue
        for leaf in KEYSTONE_LEAVES:
            if p == os.path.join(base, leaf):
                return f"'{tok}' is a keystone/secret file"
        for d in KEYSTONE_DIRS:
            if _under(p, os.path.join(base, d)):
                return f"'{tok}' is under a keystone directory"
    for d in (".aws", ".ssh", ".gnupg"):
        if _under(p, os.path.join(HOME, d)):
            return f"'{tok}' is under ~/{d}"
    return None


def _self_target(tok: str) -> bool:
    p = _norm(tok)
    if not p.startswith("/"):
        p = os.path.normpath(os.path.join(PROJECT, p))
    for rel in SELF_PROTECT:
        if _under(p, os.path.join(PROJECT, rel)):
            return True
    return False


# ---------- git ----------

def check_git(argv: list[str], command: str) -> None:
    i = 1
    # Skip global options: -C dir, -c k=v, --git-dir=..., --work-tree=..., --no-pager, -p, etc.
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
            i += 2
        else:
            i += 1
    if i >= len(argv):
        return
    verb, args = argv[i], argv[i + 1:]
    positional = [a for a in args if not a.startswith("-")]
    flags = [a for a in args if a.startswith("-")]
    joined_flags = " ".join(flags)

    if verb == "push":
        remote = refspec = None
        for a in args:
            if a in PUSH_OK_FLAGS:
                continue
            if a in PUSH_BAD_FLAGS or a.startswith("--force") or a.startswith("--push-option") \
                    or a.startswith("-o") or a.startswith("--repo"):
                block(f"git push option '{a}' is not permitted", command)
            if a.startswith("-"):
                block(f"unrecognized git push option '{a}'", command)
            if remote is None:
                remote = a
            elif refspec is None:
                refspec = a
            else:
                block("git push with more than one refspec is not permitted", command)
        if remote is None:
            block("bare 'git push' is not permitted (local main tracks upstream/main); "
                  "write 'git push origin <branch>'", command)
        if remote != "origin":
            block(f"pushing to remote '{remote}' is not permitted (only 'origin')", command)
        if refspec is None:
            block("'git push origin' without a branch is not permitted", command)
        if refspec.startswith("+"):
            block(f"forced refspec '{refspec}' is not permitted", command)
        if ":" in refspec:
            src, dst = refspec.split(":", 1)
            if not src:
                block(f"remote branch deletion '{refspec}' is not permitted", command)
        else:
            src = dst = refspec
        for part in (src, dst):
            if not REFSPEC_OK.match(part) or "heads/" in part or "tags/" in part or "refs/" in part:
                block(f"refspec '{refspec}' is not a plain branch name", command)
        if dst in FORBIDDEN_DST or dst.startswith("v") and re.match(r"^v\d", dst):
            block(f"pushing to '{dst}' is not permitted; only feature branches", command)
        return

    if verb == "reset" and any(f in ("--hard", "--merge", "--keep") for f in flags):
        block("git reset --hard/--merge/--keep is not permitted", command)
    if verb == "clean":
        if "--force" in flags or any(re.match(r"^-[a-zA-Z]*[fxX]", f) for f in flags):
            block("git clean with force/-x is not permitted", command)
    if verb == "branch":
        if "-D" in flags or "-f" in flags or "--force" in flags or \
                any(re.match(r"^-[a-zA-Z]*[Df]", f) for f in flags) or \
                ("--delete" in flags and ("--force" in flags or "-f" in flags)):
            block("git branch -D / -f is not permitted", command)
    if verb in ("checkout", "restore", "switch"):
        if verb == "restore" and "--staged" in flags and "--worktree" not in flags and "-W" not in flags:
            return  # unstaging is harmless
        if verb == "checkout" and ("-f" in flags or "--force" in flags):
            block("git checkout -f is not permitted", command)
        if verb == "switch" and ("-f" in flags or "--force" in flags or "--discard-changes" in flags):
            block("git switch --discard-changes is not permitted", command)
        # Whole-tree / directory discards.
        for a in positional:
            n = a.strip("'\"")
            if n in (".", "./", ":/", "*", "$PWD", "${PWD}") or n.endswith("/") or n.endswith("/."):
                block(f"git {verb} of a whole tree/directory ('{a}') is not permitted; "
                      "name individual files", command)
        if verb == "checkout" and "--" in args:
            after = args[args.index("--") + 1:]
            if not after:
                block("git checkout -- with no path is not permitted", command)
    if verb == "stash" and positional and positional[0] in ("drop", "clear"):
        block("git stash drop/clear is not permitted", command)
    if verb in ("filter-branch", "filter-repo"):
        block(f"git {verb} is not permitted", command)
    if verb == "reflog" and positional and positional[0] in ("expire", "delete"):
        block("git reflog expire/delete is not permitted", command)
    if verb == "prune" or (verb == "gc" and any(f.startswith("--prune") for f in flags)):
        block("git prune / gc --prune is not permitted", command)
    if verb == "update-ref" and ("-d" in flags or any(p.startswith("refs/heads/") for p in positional)):
        block("git update-ref on branch heads is not permitted", command)
    if verb == "remote" and positional and positional[0] in ("remove", "rm", "set-url", "rename", "prune"):
        block(f"git remote {positional[0]} is not permitted", command)
    if verb == "worktree" and positional and positional[0] == "remove" and \
            ("-f" in flags or "--force" in flags):
        block("git worktree remove --force is not permitted", command)
    if verb == "submodule" and positional and positional[0] == "deinit":
        block("git submodule deinit is not permitted", command)
    _ = joined_flags


# ---------- gh ----------

def check_gh(argv: list[str], command: str) -> None:
    a = argv[1:]
    if len(a) >= 2 and (a[0], a[1]) in {("pr", "merge"), ("repo", "delete"), ("release", "delete"),
                                        ("repo", "archive"), ("secret", "set"), ("secret", "delete")}:
        block(f"gh {a[0]} {a[1]} is not permitted", command)
    if a and a[0] == "api":
        method = None
        mutating_field = False
        for i, t in enumerate(a):
            if t in ("-X", "--method") and i + 1 < len(a):
                method = a[i + 1].upper()
            elif t.startswith("--method="):
                method = t.split("=", 1)[1].upper()
            elif t in ("-f", "-F", "--field", "--raw-field", "--input") or \
                    t.startswith(("--field=", "--raw-field=", "--input=")):
                mutating_field = True
        if method in ("PUT", "PATCH", "DELETE", "POST") or (mutating_field and method != "GET"):
            block("mutating gh api call is not permitted", command)
        if any("/merge" in t for t in a):
            block("gh api targeting a merge endpoint is not permitted", command)


# ---------- filesystem ----------

def check_fs(argv: list[str], command: str) -> None:
    verb = os.path.basename(argv[0])
    args = argv[1:]
    if verb in DESTROY_VERBS or verb in MOVE_VERBS or verb == "find":
        for t in args:
            why = _protected_home_target(t)
            if why and (verb in DESTROY_VERBS or verb in ("mv", "rsync", "shred", "truncate", "dd",
                                                          "chmod", "chown", "sed", "perl", "tee",
                                                          "install", "ln", "find")):
                # cp/scp INTO a protected dir is a write too, but reading FROM ~/.claude is common
                # for the operator's own tooling; only writes/deletes are blocked here.
                if verb == "find" or verb in ("sed", "perl"):
                    if any(x in args for x in ("-delete", "-exec", "-execdir", "-i")) or \
                            any(x.startswith("-i") for x in args):
                        block(f"{verb} mutating a protected path: {why}", command)
                    continue
                if verb == "rsync" and "--delete" not in args:
                    continue
                block(f"{verb} on a protected path: {why}", command)
        if verb in ("rm", "shred", "truncate", "unlink"):
            for t in args:
                if t.startswith("$") and "HOME" not in t and "KIROCREW_HOME" not in t and \
                        t not in ("$PWD", "${PWD}"):
                    # e.g. rm -rf $DIR — cannot resolve; allow (scratch dirs are common), but not
                    # if it is followed by a protected suffix.
                    if any(t.endswith(sfx) for sfx in ("/.kiro", "/.claude", "/.aws", "/.ssh")):
                        block(f"rm of an expansion ending in a protected dir '{t}'", command)
    # Keystone files: neither read nor written by any verb (Read-tool deny does not cover
    # grep/rg/jq/python).
    for t in args:
        why = _keystone_target(t)
        if why:
            block(f"access to a keystone/secret path is not permitted: {why}", command)
    # Self-protection: no Bash write/rm/chmod to the guard or the shared settings.
    if verb in WRITE_VERBS:
        for t in args:
            if _self_target(t):
                block(f"'{verb}' aimed at .claude harness file '{t}' is not permitted", command)


REDIR = re.compile(r"(?:(?<=\s)|^)(\d*)(>>|>&|>|<&|<<<|<<|<)\s*(\S*)")


def strip_redirects(line: str, command: str) -> str:
    """Check `> path` / `>> path` targets against protected/self paths, then remove every
    redirection from the line so the lexer sees only argv words."""
    for m in REDIR.finditer(line):
        op, target = m.group(2), m.group(3)
        if not op.startswith(">") or not target or target.startswith("&") or target == "/dev/null":
            continue
        why = _protected_home_target(target) or _keystone_target(target)
        if why:
            block(f"redirect write to a protected path: {why}", command)
        if _self_target(target):
            block(f"redirect write to harness file '{target}' is not permitted", command)
    return REDIR.sub(" ", line)


# ---------- driver ----------

WRAPPERS = {"env", "sudo", "nohup", "time", "command", "builtin", "exec", "nice", "ionice",
            "timeout", "caffeinate", "stdbuf"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}


def segments(command: str) -> list[list[str]]:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        # Unbalanced quotes: fall back to a crude split so we still see verbs.
        toks = command.replace("&&", " ; ").replace("||", " ; ").replace("|", " ; ").split()
        toks = [t for chunk in " ".join(toks).split(";") for t in (chunk.split() + [";"])]
    segs: list[list[str]] = []
    cur: list[str] = []
    for t in toks:
        if t in ("&&", "||", ";", "|", "&", ";;", "\n") or t.endswith("\n"):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    # Newlines survive shlex as part of tokens? No — shlex treats them as whitespace; so also
    # split the raw command on newlines and re-lex each line if the whole thing had them.
    return segs


def check_segment(seg: list[str], command: str, depth: int = 0) -> None:
    if not seg or depth > 3:
        return
    argv = list(seg)
    # Strip leading VAR=value assignments and wrappers.
    while argv and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]) or
                    os.path.basename(argv[0]) in WRAPPERS):
        if os.path.basename(argv[0]) in ("timeout", "nice", "ionice") and len(argv) > 2 and \
                not argv[1].startswith("-"):
            argv = argv[2:]
        elif os.path.basename(argv[0]) == "env":
            argv = argv[1:]
            while argv and (argv[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0])):
                argv = argv[1:]
        else:
            argv = argv[1:]
    if not argv:
        return
    prog = os.path.basename(argv[0])
    if prog in SHELLS and "-c" in argv:
        inner = argv[argv.index("-c") + 1] if argv.index("-c") + 1 < len(argv) else ""
        for s in segments(strip_redirects(inner, command)):
            check_segment(s, command, depth + 1)
        return
    if prog == "eval":
        for s in segments(strip_redirects(" ".join(argv[1:]), command)):
            check_segment(s, command, depth + 1)
        return
    if prog == "xargs":
        rest = [a for a in argv[1:] if not a.startswith("-")]
        if rest:
            check_segment(rest, command, depth + 1)
        return
    if prog == "git":
        check_git(argv, command)
    elif prog == "gh":
        check_gh(argv, command)
    check_fs(argv, command)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    command = ""
    if isinstance(payload, dict):
        ti = payload.get("tool_input") or {}
        if isinstance(ti, dict):
            command = str(ti.get("command") or "")
    if not command.strip():
        sys.exit(0)
    for line in command.splitlines():
        for seg in segments(strip_redirects(line, command)):
            check_segment(seg, command)
    sys.exit(0)


if __name__ == "__main__":
    main()
