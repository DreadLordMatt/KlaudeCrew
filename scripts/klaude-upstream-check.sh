#!/bin/bash
# klaude-upstream-check.sh — read-only report on how far the fork's
# integration branch (klaude) has drifted from upstream (kirodotdev/KiroCrew),
# and what a merge/rebase onto a given upstream ref would conflict on.
#
# Never applies anything: it fetches upstream, runs `git merge-tree` against
# the requested ref, and reports the file-level conflict set grouped the way
# AGENTS.md's "This fork" section describes the rebase surface (backend core /
# tracked artifacts / tests / frontend / locales). It also flags the seams
# CLAUDE.md's drift note warns merge CLEANLY but WRONGLY (upstream's
# acp/types.py inverts the fork's ACP_BACKEND_CLAUDE selection semantics) so
# that trap surfaces on every run, not just the ones with a text conflict.
#
# A sync is its own planned task per CLAUDE.md/AGENTS.md — this script is the
# thing you run before starting that task, and periodically to know how big
# the task has grown.
set -euo pipefail

INTEGRATION_BRANCH="klaude"
REF="${1:-}"

log() { printf '[klaude-upstream-check] %s\n' "$1"; }
die() { printf '[klaude-upstream-check] ERROR: %s\n' "$1" >&2; exit 2; }

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not inside a git repo"
git remote get-url upstream >/dev/null 2>&1 || die \
  "no 'upstream' remote configured (expected kirodotdev/KiroCrew)"

log "fetching upstream..."
git fetch --quiet upstream

if [ -z "$REF" ]; then
  # Newest upstream/release/* branch by committer date, else upstream/main.
  REF="$(git for-each-ref --sort=-committerdate --format='%(refname:short)' \
        'refs/remotes/upstream/release/*' | head -1)"
  REF="${REF:-upstream/main}"
fi
git rev-parse --verify --quiet "$REF^{commit}" >/dev/null || die "unknown ref '$REF'"

INTEGRATION_SHA="$(git rev-parse "$INTEGRATION_BRANCH" 2>/dev/null)" || \
  die "no local branch '$INTEGRATION_BRANCH' (checkout or fetch it first)"
REF_SHA="$(git rev-parse "$REF")"
BASE_SHA="$(git merge-base "$INTEGRATION_BRANCH" "$REF")"

AHEAD="$(git rev-list --count "$BASE_SHA..$INTEGRATION_BRANCH")"
BEHIND="$(git rev-list --count "$BASE_SHA..$REF")"

echo "== klaude-upstream-check =="
echo "integration branch : $INTEGRATION_BRANCH ($(git rev-parse --short "$INTEGRATION_SHA"))"
echo "upstream ref        : $REF ($(git rev-parse --short "$REF_SHA"))"
echo "merge-base           : $(git rev-parse --short "$BASE_SHA")"
echo "fork-only commits    : $AHEAD"
echo "upstream-only commits: $BEHIND"
echo

log "running merge-tree dry run (no changes made)..."
TREE_OUT="$(git merge-tree --write-tree --name-only "$INTEGRATION_BRANCH" "$REF" 2>&1)" || true

# git merge-tree --name-only prints, all on stdout: the written tree oid on
# line 1, then one path per line for every conflicted entry, then a BLANK
# line, then free-form "Auto-merging <path>" / "CONFLICT (content): ..."
# progress messages for every touched file (clean or not) -- those messages
# are not part of the --name-only contract and must not be read as paths, so
# stop at the first blank line rather than taking every remaining line.
CONFLICTS="$(printf '%s\n' "$TREE_OUT" | tail -n +2 | sed '/^$/q' | grep -v '^$' || true)"

if [ -z "$CONFLICTS" ]; then
  echo "Result: CLEAN — $REF would merge into $INTEGRATION_BRANCH with no textual conflicts."
  CLEAN=true
else
  echo "Result: CONFLICTS — the following files would need manual resolution:"
  echo

  group() {
    local title="$1"; shift
    local pattern="$1"; shift
    local matches
    matches="$(printf '%s\n' "$CONFLICTS" | grep -E "$pattern" || true)"
    if [ -n "$matches" ]; then
      echo "  $title:"
      printf '%s\n' "$matches" | sed 's/^/    /'
    fi
  }

  BACKEND_RE='^src/kiro_crew/(acp|config|dashboard|providers|platform|slack|testing)/|^src/kiro_crew/(cli_doctor|cli_setup|diagnostics|model_registry|session|history|llm_helpers|eval)'
  ARTIFACTS_RE='^(config-baseline\.json|AGENTS\.md|README\.md|\.gitignore)$'
  TESTS_RE='^test/'
  LOCALES_RE='^website/src/i18n/locales/'
  # Frontend = under website/src/ but not a locale catalog; BSD grep has no
  # lookahead, so exclude the locale set with a second pass instead.
  FRONTEND_MATCHES="$(printf '%s\n' "$CONFLICTS" | grep -E '^website/src/' | grep -vE "$LOCALES_RE" || true)"

  group "Backend core"      "$BACKEND_RE"
  group "Tracked artifacts" "$ARTIFACTS_RE"
  group "Tests"             "$TESTS_RE"
  if [ -n "$FRONTEND_MATCHES" ]; then
    echo "  Frontend:"
    printf '%s\n' "$FRONTEND_MATCHES" | sed 's/^/    /'
  fi
  group "Locale catalogs"   "$LOCALES_RE"
  OTHER="$(printf '%s\n' "$CONFLICTS" | grep -vE \
    "$BACKEND_RE|$ARTIFACTS_RE|$TESTS_RE|^website/src/" \
    || true)"
  if [ -n "$OTHER" ]; then
    echo "  Other:"
    printf '%s\n' "$OTHER" | sed 's/^/    /'
  fi
  CLEAN=false
fi
echo

log "checking for seams that merge cleanly but with inverted semantics..."
FLAG_FOUND=false
if git show "$REF:src/kiro_crew/acp/types.py" > /tmp/klaude-upstream-check-types.py 2>/dev/null; then
  if grep -q 'ACP_BACKEND_KIRO' /tmp/klaude-upstream-check-types.py; then
    echo "  WARNING: upstream $REF still spells kiro as ACP_BACKEND_KIRO=\"\" in acp/types.py —"
    echo "           the fork has no such constant. A clean auto-merge here likely means the"
    echo "           fork's ACP_BACKEND_CLAUDE selection did not survive; verify by hand."
    FLAG_FOUND=true
  fi
  if grep -q 'ACP_BACKENDS_SELECTABLE' /tmp/klaude-upstream-check-types.py && \
     ! grep -q '"claude"' /tmp/klaude-upstream-check-types.py; then
    echo "  WARNING: upstream $REF's ACP_BACKENDS_SELECTABLE does not mention \"claude\" —"
    echo "           confirm the merged result still selects the claude backend by default."
    FLAG_FOUND=true
  fi
  rm -f /tmp/klaude-upstream-check-types.py
fi
if git show "$REF:src/kiro_crew/acp/client.py" 2>/dev/null | grep -q '_normalize_acp_backend'; then
  echo "  WARNING: upstream $REF has its own _normalize_acp_backend — check it does not"
  echo "           degrade \"claude\" back to kiro after a merge."
  FLAG_FOUND=true
fi
$FLAG_FOUND || echo "  none of the known trap seams detected in this ref (does not guarantee safety)"
echo

if $CLEAN; then
  exit 0
else
  exit 1
fi
