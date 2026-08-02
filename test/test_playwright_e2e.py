"""Dashboard Playwright E2E suite, folded into ``test_e2e`` (E2eTestCommand).

Boots a real gateway via the same ``spawn_feature_gateway`` harness the smoke
suite uses, then runs the credential-less, crash-free Playwright spec set
(``website/playwright``) against it. Uses Playwright's own bundled Chromium
(``playwright install`` at website-setup time); this OSS fork does not vend a
browser binary.

Gating:
  * ``KIROCREW_E2E`` (set by E2eTestCommand) lifts the skipif, same as the
    smoke suite.
  * Skips gracefully when the in-tree ``website`` dir or its Playwright CLI
    can't be resolved (e.g. a python-only checkout without the built frontend
    dependency).
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NoReturn

import pytest

# Gate the browser suite behind KIROCREW_E2E so it never runs in the default
# unit-test pass. Applied as a decorator rather than a module-level pytestmark so
# the floor helper's own tests below DO run in the default pass -- an unverified
# guard against silent darkening is no guard.
_requires_e2e = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="E2E Playwright suite. Set KIROCREW_E2E=1 to run.",
)

_WEBSITE = "website"

# Executed-spec floor.
#
# This suite has been silently darkened before: 36 of 103 authored specs were
# excluded by `grepInvert` in playwright.config.ts, so they were never collected
# and never reported as skips. The gate stayed green while a third of the suite
# did not run. An exit code alone cannot catch that, so assert the count.
#
# Every dark spec that was later un-darkened had ALSO rotted -- stale selectors
# for UI that had moved -- because nothing exercised them. That is the argument
# for a floor rather than a periodic audit.
#
# RAISE this when you add specs. Only LOWER it with a written reason in the
# commit body: a drop means specs stopped running.
#
# 208 -> 230. Two separate corrections:
#
# 1. The ops-mission-control suite added 24 specs and the floor was never raised, so it
#    carried ~11 specs of slack — most of that new suite could have been deleted or
#    darkened with the gate still green, exactly what this constant exists to prevent.
#
# 2. Measured against a real run rather than `--list`: the collected count (220) is NOT
#    the executed count (230). File-level `serial` describes and per-project setup
#    specs change what actually runs, and one failing spec in a serial file previously
#    aborted 14 others — the run reported executed=215/skipped=14 while `--list` still
#    said 220. So this floor is set from an observed green-path run, not from
#    collection.
#
# 230 -> 232. The ops-mission-control suite added one spec (the incident panel's ledger
# detail) and, as in correction 1 above, the floor was not raised with it — the same slack
# reappearing immediately after being removed, which is the argument for treating "raise
# the floor" as part of adding a spec rather than a follow-up.
#
# 232 is measured, and cross-checked two ways so it is not an inference from a passing
# run: a mid-investigation run reported `expected=229, flaky=1, skipped=1, unexpected=1`
# (= 232 total), and `grep -c '^\s*test('` across `playwright/*.spec.ts` independently
# counts 232.
# 232 -> 234. One spec added: the on-call team panel renders (owner-requested display,
# and under strict gating the only thing separating "a teammate has it" from "this instance
# silently stopped working"). Raised WITH the spec, per the note above.
#
# 234 is MEASURED, not derived: I first wrote 233 by adding one to the previous floor, and
# a real run reported `executed=234, skipped=0, unexpected=0` — the arithmetic was wrong
# because the previous floor already trailed the suite. `grep -c '^\s*test('` across
# playwright/*.spec.ts independently agrees at 234. Take the measurement over the sum.
#
# 234 -> 235 on merging `main`. Both sides of the merge had moved this constant — this
# branch to 234 (its own specs) and `main` to 210 — so neither side's number describes the
# MERGED suite, and taking either would have re-introduced exactly the slack the floor
# exists to remove. Re-counted on the merged tree instead of picking a side.
MIN_EXECUTED_SPECS = 235

# Skips are silent passes. A spec should seed its preconditions rather than skip
# when they are absent, so the intended steady state is zero. Specs excluded by
# tag are never collected, so they do not count here.
MAX_SKIPPED_SPECS = 0


def _assert_suite_not_darkened(report: Path) -> None:
    """Fail if the run executed fewer specs than the floor, or skipped any.

    Reads Playwright's JSON report rather than scraping stdout. `expected` and
    `flaky` both mean "ran and ultimately passed"; counting only `expected` would
    trip the floor whenever CI's retries absorb a flake.
    """
    try:
        stats = json.loads(report.read_text()).get("stats") or {}
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
        pytest.fail(f"could not read Playwright JSON report at {report}: {exc}")

    executed = int(stats.get("expected", 0)) + int(stats.get("flaky", 0))
    skipped = int(stats.get("skipped", 0))
    print(
        f"[test:e2e:playwright] executed={executed} skipped={skipped} stats={stats}",
        flush=True,
    )

    assert executed >= MIN_EXECUTED_SPECS, (
        f"only {executed} specs executed, floor is {MIN_EXECUTED_SPECS}. "
        "Specs stopped running rather than failing. Check grepInvert in "
        "website/playwright.config.ts for a newly excluded tag, and check that no "
        "spec file was renamed out of the testDir. If the drop is intended, lower "
        "MIN_EXECUTED_SPECS and say why in the commit body."
    )
    assert skipped <= MAX_SKIPPED_SPECS, (
        f"{skipped} spec(s) skipped, ceiling is {MAX_SKIPPED_SPECS}. A skip reports "
        "green while verifying nothing. Seed the precondition in a fixture instead "
        "of skipping on its absence."
    )


def _newest_mtime(root: Path, patterns: tuple[str, ...]) -> float:
    """Newest mtime among ``patterns`` under ``root``, or 0.0 if none match."""
    newest = 0.0
    for pattern in patterns:
        for path in root.rglob(pattern):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:  # pragma: no cover - racing a rebuild
                continue
    return newest


def _assert_served_bundle_is_current(website: Path) -> None:
    """Fail when the gateway would serve a bundle older than the frontend source.

    The gateway serves ``src/kiro_crew/static/dist`` (``server.py`` ``_DIST_DIR``) and
    this gate does NOT build. ``npm run build`` writes ``website/dist`` and does **not**
    stage; the staging step is a separate ``cp -R`` documented in AGENTS.md. So a run
    that skips staging silently exercises whatever bundle happens to be on disk.

    That is not hypothetical: both repos were serving bundles ~2 and ~10 hours older
    than the source, so every browser assertion about freshly-changed UI was verifying
    the PREVIOUS build. The spec under test even rendered correctly in the DOM while its
    ``data-testid`` was absent from the served JS — a failure that reads as a UI bug and
    is actually a build-staging gap.

    Compares newest source mtime against newest served-asset mtime. A skew this coarse
    cannot catch every case (an edit inside the same second, a touched-but-unchanged
    file), but it catches the multi-hour staleness that actually happens, and it fails
    LOUD with the exact command to fix it.
    """
    dist = Path(__file__).resolve().parents[1] / "src/kiro_crew/static/dist"
    if not dist.is_dir():
        pytest.fail(
            f"no staged frontend bundle at {dist}. The gateway serves this directory and "
            "this gate does not build. Run:\n"
            "  cd website && npm run build && "
            "cp -R dist ../src/kiro_crew/static/dist"
        )

    src_mtime = _newest_mtime(website / "src", ("*.ts", "*.tsx", "*.css"))
    dist_mtime = _newest_mtime(dist, ("*.js", "*.css"))
    if src_mtime and dist_mtime and src_mtime > dist_mtime:
        stale_secs = int(src_mtime - dist_mtime)
        pytest.fail(
            f"the served bundle is {stale_secs}s older than website/src — every browser "
            "spec would verify the PREVIOUS build, which is how a rendered-but-untestable "
            "element looks like a UI bug. `npm run build` alone is not enough; it writes "
            "website/dist and does not stage. Run:\n"
            "  cd website && npm run build && "
            "cp -R dist ../src/kiro_crew/static/dist"
        )


def _node_major(node_bin: str) -> int | None:
    """Return the major version of ``node_bin``, or None if it can't run."""
    try:
        out = subprocess.check_output(
            [node_bin, "--version"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.match(r"v(\d+)\.", out)
    return int(m.group(1)) if m else None


def _resolve_node18_dir() -> str | None:
    """Return a bin dir holding a real node>=18 binary, or None.

    Playwright 1.58 requires Node>=18. We cannot rely on the ambient ``node``:
    the website dir often pins an older Node via mise (its shim is cwd-sensitive
    and resolves to the pinned version when Playwright runs there), and the
    build env may not expose Node on PATH at all. So scan a prioritized list of
    *concrete* node binaries (never a mise shim, which is cwd-sensitive) and
    return the first dir whose node is >=18; prepending it to PATH makes the
    Playwright shebang resolve it regardless of mise.
    """
    candidates: list[str] = []
    # mise-managed concrete installs (local dev) — not the shim.
    candidates += sorted(
        glob.glob(os.path.expanduser("~/.local/share/mise/installs/node/*/bin/node")),
        reverse=True,
    )
    # Ambient node last; skip mise shims (cwd-sensitive, unreliable here).
    onpath = shutil.which("node")
    if onpath and "/shims/" not in onpath:
        candidates.append(onpath)
    for c in candidates:
        maj = _node_major(c)
        if maj is not None and maj >= 18:
            return str(Path(c).resolve().parent)
    return None


def _resolve_website_dir() -> Path | None:
    """Locate the in-tree ``website`` root (with ``playwright/`` + ``node_modules``).

    Mirrors ``kiro_crew.frontend`` dist resolution: the canonical frontend lives
    in-tree at ``<repo-root>/website``. ``test/`` sits at the repo root, so the
    website is a sibling of this file's parent directory.
    """
    repo_root = Path(__file__).resolve().parent.parent  # KiroCrew repo root
    in_tree = repo_root / _WEBSITE
    return in_tree if (in_tree / "playwright").is_dir() else None


@_requires_e2e
def test_dashboard_playwright_suite() -> None:
    """Boot a gateway and run the credential-less Playwright spec set against it."""

    def _unresolved(msg: str) -> NoReturn:
        # On the required PR gate (KIROCREW_E2E_REQUIRE, set by that step) an
        # environment-resolution miss is a HARD failure: pytest counts a skip as
        # a pass, so the gate would go green having run zero browser specs -- the
        # exact "dead suite, silent UI drift" rot this fold exists to catch. Keep
        # the graceful skip for ad-hoc local/dev runs (marker unset).
        if os.environ.get("KIROCREW_E2E_REQUIRE"):
            pytest.fail(msg)
        pytest.skip(msg)

    website = _resolve_website_dir()
    if website is None:
        _unresolved("website dir not resolvable (no playwright/ dir)")

    pw_bin = website / "node_modules" / ".bin" / "playwright"
    if not pw_bin.exists():
        _unresolved(f"Playwright CLI not found at {pw_bin}")

    node_dir = _resolve_node18_dir()
    if node_dir is None:
        _unresolved("No Node.js >=18 found; Playwright 1.58 requires it")

    _assert_served_bundle_is_current(website)

    # Point the gateway's ACP client at the packaged fake backend so the
    # agent-driven specs (chat, fork) run deterministic, credential-less turns
    # instead of needing real model access. acp/client.py reads
    # KIROCREW_KIRO_BIN and, when set, spawns it as the agent binary; the
    # harness gateway inherits os.environ at spawn time.
    from kiro_crew.testing import fake_acp_backend
    from kiro_crew.testing.harness import spawn_feature_gateway

    prev_kiro_bin = os.environ.get("KIROCREW_KIRO_BIN")
    os.environ["KIROCREW_KIRO_BIN"] = str(fake_acp_backend.__file__)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "playwright-results.json"
            with spawn_feature_gateway(fixture="minimal", approval="reads") as gw:
                env = dict(os.environ)
                env.update(
                    {
                        # Prepend a node>=18 bin dir so the playwright shebang
                        # resolves it ahead of any cwd-pinned mise shim.
                        "PATH": node_dir + os.pathsep + env.get("PATH", ""),
                        "PLAYWRIGHT_BASE_URL": f"http://localhost:{gw.port}",
                        "PLAYWRIGHT_TOKEN": gw.token,
                        # Fake ACP backend is wired, so the agent specs (chat/fork)
                        # can run headlessly -- opt them back in.
                        "PLAYWRIGHT_RUN_AGENT_SPECS": "1",
                        # Explicit ephemeral-harness marker: this gateway runs on an
                        # isolated tmp KIROCREW_HOME (spawn_feature_gateway --test-mode),
                        # so its slots are disposable.
                        "KIROCREW_E2E_EPHEMERAL": "1",
                        # CI mode: serial workers + retries:2 (absorbs gateway-load
                        # timeout flakes) + html reporter, per playwright.config.ts.
                        "CI": "1",
                        # Machine-readable counts for the darkening floor below.
                        "PLAYWRIGHT_JSON_OUTPUT_NAME": str(report),
                    }
                )
                print(
                    f"[test:e2e:playwright] base={env['PLAYWRIGHT_BASE_URL']} "
                    f"node_dir={node_dir} fake_acp={fake_acp_backend.__file__} cwd={website}",
                    flush=True,
                )
                # html keeps the CI artifact the config asks for; json adds the
                # counts. A CLI --reporter replaces the config value, so name both.
                rc = subprocess.call(
                    [str(pw_bin), "test", "--reporter=html,json"],
                    cwd=str(website),
                    env=env,
                )
            # Assert the counts even on failure: a red run plus a collapsed spec
            # count points at darkening rather than at the reported failure.
            _assert_suite_not_darkened(report)
            assert rc == 0, f"playwright test exited {rc}"
    finally:
        if prev_kiro_bin is None:
            os.environ.pop("KIROCREW_KIRO_BIN", None)
        else:
            os.environ["KIROCREW_KIRO_BIN"] = prev_kiro_bin


# --------------------------------------------------------------------------- #
# Floor-helper unit tests. Ungated on purpose: these need no gateway and no
# browser, and a silently broken floor would never fire when it matters.
# --------------------------------------------------------------------------- #


def _write_report(tmp_path: Path, **stats: int) -> Path:
    report = tmp_path / "results.json"
    report.write_text(json.dumps({"stats": stats}))
    return report


def test_floor_accepts_a_run_at_the_floor(tmp_path: Path) -> None:
    report = _write_report(tmp_path, expected=MIN_EXECUTED_SPECS, flaky=0, skipped=0)
    _assert_suite_not_darkened(report)  # must not raise


def test_floor_counts_flaky_as_executed(tmp_path: Path) -> None:
    """CI runs retries:2, so a flake absorbed by a retry still ran."""
    report = _write_report(
        tmp_path, expected=MIN_EXECUTED_SPECS - 1, flaky=1, skipped=0
    )
    _assert_suite_not_darkened(report)  # must not raise


def test_floor_rejects_a_collapsed_spec_count(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path, expected=MIN_EXECUTED_SPECS - 1, flaky=0, skipped=0
    )
    with pytest.raises(AssertionError, match="specs executed, floor is"):
        _assert_suite_not_darkened(report)


def test_floor_rejects_any_skip(tmp_path: Path) -> None:
    report = _write_report(tmp_path, expected=MIN_EXECUTED_SPECS, flaky=0, skipped=1)
    with pytest.raises(AssertionError, match="skipped, ceiling is"):
        _assert_suite_not_darkened(report)


def test_floor_fails_when_the_report_is_missing(tmp_path: Path) -> None:
    """A missing report must fail loudly, not pass for lack of evidence."""
    # pytest.fail raises Failed, which derives from BaseException, so a plain
    # `pytest.raises(Exception)` would not catch it.
    with pytest.raises(
        pytest.fail.Exception, match="could not read Playwright JSON report"
    ):
        _assert_suite_not_darkened(tmp_path / "absent.json")
