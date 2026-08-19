#!/bin/bash
# klaude-prod-update.sh — the one command that updates a production KlaudeCrew
# checkout to the latest pushed integration branch and restarts the service.
#
# Intended use: `ssh <host> <checkout>/scripts/klaude-prod-update.sh` from the
# laptop, after a PR has landed on the integration branch (default: klaude).
#
# Deliberately mirrors the primitives `kirocrew update` already uses (git
# reset --hard against origin/<branch>, frontend rebuild, editable pip
# install, `setup --agent-only`) rather than inventing a new update path, so
# behavior stays consistent with the in-app updater. Restart is service-aware
# via `kirocrew restart` (systemd on Linux) so a `kirocrew service install`
# production box restarts through systemd, not a bare respawn.
#
# Safety: refuses on a dirty working tree (production never carries local
# edits — dogfooding happens in worktrees, see the kirocrew-worktree-dev
# skill) and on a branch mismatch (won't silently jump branches).
set -euo pipefail

BRANCH="klaude"
UPDATE_ADAPTER=false
CHECK_ONLY=false
NO_RESTART=false
REPO_DIR=""

usage() {
  cat <<'EOF'
Usage: klaude-prod-update.sh [OPTIONS]

Update a production KlaudeCrew checkout to the latest pushed commit on the
integration branch, rebuild, and restart the service.

Options:
  --branch NAME       Integration branch to track (default: klaude)
  --repo DIR          Checkout to update (default: $KIROCREW_PROJECT_DIR, else
                       the directory this script lives in)
  --check             Only report whether an update is available (exit 10 if
                       so, 0 if already up to date); makes no changes
  --update-adapter    Also refresh the global claude-agent-acp npm package
  --no-restart        Skip the service restart step after updating
  -h, --help          Show this help

Examples:
  klaude-prod-update.sh --check
  klaude-prod-update.sh
  klaude-prod-update.sh --branch feat/7-capability-matrix --no-restart
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --branch) BRANCH="$2"; shift 2 ;;
    --repo) REPO_DIR="$2"; shift 2 ;;
    --check) CHECK_ONLY=true; shift ;;
    --update-adapter) UPDATE_ADAPTER=true; shift ;;
    --no-restart) NO_RESTART=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

log() { printf '[klaude-prod-update] %s\n' "$1"; }
die() { printf '[klaude-prod-update] ERROR: %s\n' "$1" >&2; exit 1; }

# --- Locate the checkout ---
if [ -z "$REPO_DIR" ]; then
  if [ -n "${KIROCREW_PROJECT_DIR:-}" ] && [ -d "${KIROCREW_PROJECT_DIR}/.git" ]; then
    REPO_DIR="$KIROCREW_PROJECT_DIR"
  else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  fi
fi
[ -d "$REPO_DIR/.git" ] || die "'$REPO_DIR' is not a git checkout"
cd "$REPO_DIR"

KIROCREW_BIN="$REPO_DIR/.venv/bin/kirocrew"
[ -x "$KIROCREW_BIN" ] || die "no .venv/bin/kirocrew in $REPO_DIR — run minimal_install.sh first"

# --- Preflight: clean tree, correct branch ---
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$CURRENT_BRANCH" = "$BRANCH" ] || die \
  "checked out branch is '$CURRENT_BRANCH', not '$BRANCH' (pass --branch to override)"

if [ -n "$(git status --porcelain)" ]; then
  die "working tree is dirty — production must stay clean; dogfooding sessions" \
      $'\n  belong in a separate git worktree, never in this checkout'
fi

log "fetching origin/$BRANCH..."
git fetch --quiet origin "$BRANCH"

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "origin/$BRANCH")"

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
  log "already up to date at $(git rev-parse --short HEAD) ($CURRENT_BRANCH)"
  exit 0
fi

log "update available: $(git rev-parse --short "$LOCAL_SHA") -> $(git rev-parse --short "$REMOTE_SHA")"
git log --oneline "$LOCAL_SHA..$REMOTE_SHA" | sed 's/^/  /'

if $CHECK_ONLY; then
  exit 10
fi

# --- Apply: same primitive kirocrew update uses, so a rewritten branch tip
# (force-pushed PR merge, squash) never leaves prod wedged on a diverged ref ---
log "resetting to origin/$BRANCH..."
git reset --hard "origin/$BRANCH"

# --- Rebuild: mirror minimal_install.sh's order ---
log "building frontend..."
(
  cd website
  if [ -f package-lock.json ]; then npm ci --silent; else npm install --silent; fi
  npm run build --silent
)
rm -rf src/kiro_crew/static/dist
mkdir -p src/kiro_crew/static
cp -R website/dist src/kiro_crew/static/dist

log "installing backend (editable)..."
KIROCREW_SKIP_FRONTEND=1 "$REPO_DIR/.venv/bin/pip" install --prefer-binary -e . --quiet

if $UPDATE_ADAPTER; then
  log "updating claude-agent-acp..."
  npm install -g @agentclientprotocol/claude-agent-acp@latest --silent
fi

log "refreshing agent config..."
"$KIROCREW_BIN" setup --agent-only

if $NO_RESTART; then
  log "update complete (--no-restart); restart manually to run the new code"
  exit 0
fi

log "restarting..."
"$KIROCREW_BIN" restart

log "waiting for readiness..."
READY=false
for _ in $(seq 1 30); do
  if "$KIROCREW_BIN" status >/dev/null 2>&1; then
    READY=true
    break
  fi
  sleep 1
done
$READY || log "WARNING: service did not report ready within 30s; check 'kirocrew logs'"

log "now running $(git rev-parse --short HEAD) ($CURRENT_BRANCH)"
