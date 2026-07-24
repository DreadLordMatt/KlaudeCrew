#!/bin/bash
# Run the KiroCrew gateway from LIVE source. Uses the venv Python (which has
# all deps) but PYTHONPATH=src so code changes are picked up immediately on
# restart.
#
# Usage: ./dev-backend.sh
#   - Runs on a DEDICATED dev port (default 6777), INDEPENDENT of the operator's
#     production KIROCREW_PORT (which may be customized, e.g. 9999). Override the
#     dev port with KIROCREW_DEV_PORT.
#   - Uses .kirocrew-dev/ as data directory (isolated from ~/.kirocrew/); a custom
#     production KIROCREW_HOME is likewise NOT inherited — override with
#     KIROCREW_DEV_HOME.
#   - KIROCREW_DEV_DRYRUN=1 prints the resolved port/home and exits (no launch).
#   - Ctrl+C to stop, re-run to pick up changes.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export PYTHONPATH="$SCRIPT_DIR/src"
export KIROCREW_PROJECT_DIR="$SCRIPT_DIR"

# --- Isolated dev data dir ----------------------------------------------------
# Do NOT inherit a custom production KIROCREW_HOME, or the throwaway dev gateway
# would run against real ~/.kirocrew data. Resolve a DEDICATED dev home (override
# with KIROCREW_DEV_HOME); default .kirocrew-dev.
KIROCREW_HOME="${KIROCREW_DEV_HOME:-.kirocrew-dev}"
# Absolutize: config_dir() resolves this against each process's CWD, and MCP
# subprocesses (mcp-core/mcp-cron) are spawned with session-workspace CWDs —
# a relative HOME makes them create empty config dirs with no .local_secret,
# so their gateway IPC calls fail with 403 Forbidden.
case "$KIROCREW_HOME" in
    /*) ;;
    *) KIROCREW_HOME="$SCRIPT_DIR/$KIROCREW_HOME" ;;
esac
export KIROCREW_HOME

# --- Dedicated dev port (independent of a custom production KIROCREW_PORT) -----
# The gateway binds $KIROCREW_PORT. Do NOT inherit the operator's production
# KIROCREW_PORT — it is commonly customized (e.g. 9999) and reusing it makes the
# dev instance contend for the LIVE gateway's socket. Resolve an INDEPENDENT dev
# port instead (override with KIROCREW_DEV_PORT; default 6777). If that port is
# itself busy (a stale dev run, or a production port equal to the dev default),
# the gateway reports the conflict on bind — set KIROCREW_DEV_PORT to move it.
KIROCREW_PORT="${KIROCREW_DEV_PORT:-6777}"
case "$KIROCREW_PORT" in
    ''|*[!0-9]*) echo "ERROR: KIROCREW_DEV_PORT must be numeric, got '$KIROCREW_PORT'." >&2; exit 1 ;;
esac
export KIROCREW_PORT

# KIROCREW_DEV_DRYRUN=1 -> print the resolved config and exit without launching
# (confirm which port/home dev-backend would use; needs no venv).
if [ -n "${KIROCREW_DEV_DRYRUN:-}" ]; then
    echo "resolved dev config: port=$KIROCREW_PORT home=$KIROCREW_HOME"
    exit 0
fi

# --- Runtime interpreter ------------------------------------------------------
# Find the Python with deps installed: prefer the venv created by install.sh /
# setup.sh / minimal_install.sh. Override with RUNTIME_PYTHON if needed.
RUNTIME_PYTHON="${RUNTIME_PYTHON:-}"
if [ -z "$RUNTIME_PYTHON" ] || [ ! -x "$RUNTIME_PYTHON" ]; then
    if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        RUNTIME_PYTHON="$SCRIPT_DIR/.venv/bin/python"
    fi
fi
if [ ! -x "$RUNTIME_PYTHON" ]; then
    echo "ERROR: Cannot find the KiroCrew venv Python at $SCRIPT_DIR/.venv/bin/python."
    echo "Run 'bash minimal_install.sh' once to set up the venv, then try again."
    exit 1
fi

echo "👻 Dev backend starting (live source, port $KIROCREW_PORT)"
echo "   Python: $RUNTIME_PYTHON"
echo "   Source: $PYTHONPATH"
echo "   Data:   $KIROCREW_HOME"
echo ""

exec "$RUNTIME_PYTHON" -m kiro_crew gateway
