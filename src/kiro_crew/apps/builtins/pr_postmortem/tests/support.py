"""Shared helpers for building synthetic git history in these tests.

There is ONE git runner here rather than a copy per test module, because the
isolation below is the kind of thing that gets fixed in one copy and forgotten in
the other.

Isolation matters for two separate reasons:

* **Hooks and templates.** A developer's global config can set `core.hooksPath`
  or `init.templateDir`, which would make `git commit` in these tests run that
  person's hooks -- arbitrary code, triggered by running the suite. Pointing
  `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` at the null device removes that
  config from the picture, and a fresh `git init` has no local hooks of its own.
* **Determinism.** Identity, and anything else the host might configure (a
  default branch name, a commit template, gpg signing), must come from here so
  the assertions do not depend on who is running them.

``os.devnull`` rather than a hardcoded path: the POSIX spelling does not exist on
Windows, and these tests run on the Windows shards too.
"""

from __future__ import annotations

import os
import subprocess


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "author@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    })
    return env


def git(args: list[str], cwd: str, check: bool = True) -> str:
    """Run one git command in ``cwd`` with the host's configuration excluded."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=git_env(),
        capture_output=True,
        text=True,
        check=check,
    )
    return proc.stdout


def write(root: str, rel: str, body: str) -> str:
    """Write ``body`` to ``rel`` under ``root``, creating parents. Returns the path."""
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def commit(root: str, message: str) -> str:
    """Stage everything and commit. Returns the new commit's sha."""
    git(["add", "-A"], root)
    git(["commit", "-q", "-m", message], root)
    return git(["rev-parse", "HEAD"], root).strip()
