"""Where the scan's output lives, and the writes that put it there.

Everything this app writes is DERIVED: a scan recomputes each report from git and
from the GitHub API, so re-running it reproduces the same file rather than merging
into it. Overwrite is therefore the correct semantics, and that is why there is no
locking, no retirement lifecycle and no refuse-to-downgrade rule here -- no
downstream state is keyed to a report, so a rewrite cannot invalidate anything.
A rewrite that comes back poorer (a clone missing the commit, `gh` unauthenticated)
is visible in the file itself and is fixed by re-running in a working environment.

Writes still go through a temp file and an atomic rename, which is about torn
reads rather than concurrency: a crash partway through `json.dump` would otherwise
leave a reader looking at half a document.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

# Matches app.json's `name`.
APP_NAME = "pr-postmortem"


def data_dir() -> str:
    """Resolve the app's data directory.

    ``PRPM_DATA_DIR`` wins (tests, pods), then ``KIROCREW_HOME``, then the default.
    """
    override = os.environ.get("PRPM_DATA_DIR")
    if override:
        return override
    home = os.environ.get("KIROCREW_HOME") or os.path.join(
        os.path.expanduser("~"), ".kiro", "crew"
    )
    return os.path.join(home, "workspace", APP_NAME)


def _sub(name: str) -> str:
    path = os.path.join(data_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path


def reports_dir() -> str:
    return _sub("reports")


def analysis_dir() -> str:
    return _sub("analysis")


def bundles_dir() -> str:
    return _sub("bundles")


def prompts_dir() -> str:
    return _sub("prompts")


def state_path() -> str:
    return os.path.join(data_dir(), "state.json")


def read_json(path: str) -> dict | None:
    """Load a JSON object, or None when it is absent, unreadable or not an object."""
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: str, obj: object) -> None:
    """Write JSON via temp file + rename so a crash cannot truncate the target."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_attribution(report: dict) -> str:
    """Write one attribution report, keyed by fix PR. Returns the path written."""
    fix_pr = int(report.get("fix_pr", 0))
    path = os.path.join(reports_dir(), f"{fix_pr}.json")
    write_json(path, report)
    return path


def import_jsonl(jsonl_path: str) -> int:
    """Split a batch JSONL into per-PR report files. Returns the count written."""
    n = 0
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("fix_pr"):
                save_attribution(rec)
                n += 1
    return n


def load_state() -> dict:
    st = read_json(state_path()) or {}
    st.setdefault("schema", 1)
    st.setdefault("repos", [])
    st.setdefault("last_scan", None)
    return st


def save_state(state: dict) -> None:
    write_json(state_path(), state)


def touch_scan(summary: dict | None = None) -> dict:
    """Record when a scan last ran, so a reader can tell fresh output from stale."""
    st = load_state()
    st["last_scan"] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **(summary or {}),
    }
    write_json(state_path(), st)
    return st["last_scan"]
