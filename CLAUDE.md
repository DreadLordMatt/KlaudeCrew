# CLAUDE.md — KlaudeCrew (fork of KiroCrew)

This file is **how Claude Code works in this repo**: the loop, the checkpoints,
the gates, the stop points, and the repo facts that are not written anywhere
else. The **rules of the codebase** live in `AGENTS.md` (imported below, always
in context) and the docs it routes to; this file does not restate them. Where
`AGENTS.md § Git` says "never commit / never push", **§ 2 below is the
operator's standing grant for Claude Code sessions and takes precedence**; and
where `AGENTS.md § Specification management` says task-specs are "never
current context", **§ 1 below carves out the *active* task's `progress.md`** as
the live resume state (it freezes when the task ships). Everything else in
`AGENTS.md` is a hard rule this file does not restate. The frontend router is
`website/AGENTS.md`.

@AGENTS.md

---

## 0. Orientation (read once per session, then act)

- **What this fork is.** `AGENTS.md § This fork`. The fork's own commits are
  `git log upstream/main..HEAD --grep='^fork'` (a bare `--grep=^fork` also
  matches upstream commits).
- **Two agents, two sets of instruction files — do not confuse them.** *You*
  are the developer agent building Kiro Crew. The *runtime* agent is the LLM
  Kiro Crew drives (Claude Code via `claude-agent-acp`, or kiro-cli).

  | Read by the **developer** agent (you, Codex, the CI reviewers) | Read by the **runtime** agent (the product's own backend) |
  |---|---|
  | `CLAUDE.md` (this file), `AGENTS.md`, `website/{CLAUDE,AGENTS}.md`, `docs/**`, `AUTOSDE.yaml` (reviewer rules), `.claude/` (this harness) | `config/prompt.md` + `prompt-orchestrator.md` (system prompt), `config/defaults.json` (the **kiro agent template**, not config defaults), `context.py` context blocks, `src/kiro_crew/builtin_skills/**` (incl. `kirocrew-dev/*` — runtime skills *about* developing this repo), `klaude/settings_seed.py`'s per-session `<work_dir>/.claude/settings.local.json`, `~/.kiro/agents/kirocrew.json`, and **whatever `CLAUDE.md` / `.claude/settings.json` sit in the session's `cwd`** |
  | `src/kiro_crew/docs/*.md` are *user* docs (also read at runtime by `tips.py`); `docs/reference/kiro-cli/**` is vendored kiro-cli reference | `.kiro/settings/cli.json` at repo root is kiro-cli's *project* settings — read by the kiro backend when its cwd is this checkout (its per-spawn effort overlay merges into that tracked file, so it can show up dirty in `git status`), and by a developer using kiro-cli here |

  Editing a runtime file changes what the *shipped product's* agent is told;
  it does not change your rules. **The overlap is the trap:** a Kiro Crew
  session whose `cwd` is this checkout (dev-fleet, auto-improvement, a slot
  bound here) makes the runtime backend read *this* `CLAUDE.md` and
  `.claude/settings.json`. That is why `.claude/settings.json` carries
  restrictions only (§ 8) — a project-scope `permissions.allow` there would let
  the backend approve tools without ever reaching Kiro Crew's approval UI —
  and why nothing in this file may be read as exempting anything from Kiro
  Crew's own approval ladder. The rules here hold for either agent working on
  this repo.
- **Where you are.** `klaude` is the fork's **integration branch** — GitHub
  default branch, PR target, and what production (the homelab LXC) tracks via
  `scripts/klaude-prod-update.sh` / the in-app updater. Never work on `klaude`
  or `main` directly. Local `main` **tracks `upstream/main`**
  (kirodotdev/KiroCrew) and carries no fork commits — it exists only as a
  fast-forward mirror to sync from, never to build on. A bare `git push` from
  either would be wrong for different reasons (`main` targets the upstream
  project; `klaude` bypasses PR review that production's update path assumes
  is green) — the hook blocks both. Work happens on
  `feat/<issue>-<slug>` / `fix/<issue>-<slug>` branches off `klaude`.
- **Environment.** `source .venv/bin/activate` (Python 3.11; bare `black`,
  `pytest` are not on `PATH`). Node 22 for `website/`. On the operator's dev
  Mac `kiro-cli` is **not** installed; `claude` + `claude-agent-acp` are. Never point a
  gateway or a test at `~/.kiro/crew`; use a scratch `KIROCREW_HOME`.
- **Ignore, never read, never `git add`:** `leaked-HOME/` (an escaped-`$HOME`
  artifact, untracked and un-ignored). Local-only noise: `TASK_PROGRESS.md`,
  `runs.json`, `.hypothesis/`. Both `test/` (the real suite) and `tests/` (a
  second, tracked suite + malicious-workflow fixtures) are legitimate.

## 1. The loop

Every task, from a one-line fix to a five-phase issue, runs the same loop. The
point is that **all state the next context window needs is on disk**, so a
compaction, restart, or machine change costs nothing.

```
ORIENT → PLAN → [ SLICE → VERIFY → CHECKPOINT ]* → FINAL GATE → REPORT
```

**ORIENT.** `git status`, `git branch --show-current`, `git log --oneline -5`.
Read your memory index pointer, then the task's `progress.md` if it exists.
Trust disk over recollection. Load the `AGENTS.md § Read before you touch` docs
for every subsystem the task touches — before editing, not after a reviewer
asks.

**PLAN.** For any task larger than a single slice, create the durable plan at
`docs/task-specs/YYYY/MM/<task-id>/` (this file explicitly authorizes those two
markdown files despite `AGENTS.md`'s no-new-markdown rule; nothing else):

- `plan.md` — goal, in-scope / out-of-scope, the ordered slices (each one
  independently verifiable), **Decisions** (each with the alternatives you
  rejected and why), **Open questions** (with the assumption you are proceeding
  under), follow-ups discovered but not in scope.
- `progress.md` — append-only checkpoint log: slice → status → commit SHA →
  gate result → next step. Update it **before** you commit, and whenever you
  finish a slice even if you don't commit.

Task-specs are link-checked but reachability-exempt, so no index edit is needed;
once the task ships the directory freezes (it is an archive per
`docs/task-specs/README.md`). Use the issue number as `<task-id>` when there is
one (`2026/08/issue-7-cc-parity`).

**SLICE.** The smallest change that moves one plan item and can be verified on
its own. Prefer test-first where a behavior is specifiable. New claude-backend
capability goes through the `klaude/` seams, not into core, unless the plan
lists that core file (§ 3).

**VERIFY.** The *slice gate* (§ 4) — narrow tests with `-n0`, then lint on the
touched files. Never claim a slice done without running it. A red gate is
reported verbatim, never explained away.

**CHECKPOINT.** A gate-clean commit on the feature branch, then
`git push origin <branch>`. Cadence: every green slice, and always before you
stop for any reason. A checkpoint commit is a real commit (message format from
`AGENTS.md § Git`, `fork:`/`fork(<scope>):` prefix when it touches a core
file), not a WIP dump — squashing happens at PR time (§ 2).

**FINAL GATE.** The full ladder for what you touched (§ 4), then an
**independent verification pass** (§ 5) before you say "done".

**REPORT.** Standalone summary: what shipped (branch, SHAs), what the gates
said (exact numbers, and the pre-existing environmental failures by name), what
you decided under assumption, what you left out and why, follow-ups. If a
subagent's report was the basis for a claim, say so.

**Retry discipline.** Three failed attempts at the same fix means the diagnosis
is wrong: stop patching, re-diagnose (the `diagnosing-bugs` skill), or
checkpoint and report as blocked. Flakes: `AGENTS.md § The gate` — no reruns,
no longer sleeps, no weaker assertions.

## 2. Git autonomy (operator's standing grant)

| Action | Rule |
|---|---|
| Create/switch feature branch off `klaude` | Free |
| Commit on a feature branch | Free once a task is assigned; must be gate-clean for the slice |
| `git push origin <feature-branch>` (plain) | Free; do it at every checkpoint |
| Push to `main`, to `klaude`, to `upstream`, `--force*`, `+refspec`, remote delete, `--all/--mirror/--tags`, tags | **Never** (hook-enforced, best-effort) |
| `reset --hard`, `clean -f`, `branch -D/-f`, `checkout`/`restore` of a tree or directory, `stash drop/clear`, `filter-branch`, `reflog expire`, `remote set-url/remove` | **Never** (hook-enforced, best-effort) |
| Open a PR (`gh pr create`), file an issue, comment on a PR | Needs the operator's explicit go for that instance (may be granted up-front in the task) |
| `gh pr merge`, `gh repo delete`, mutating `gh api` | **Never** (hook-enforced) |
| Regenerate a baseline (`config-baseline.json`, `error-code-baseline.json`, `en-XA`, `vendor_manifest.sha256`) or delete anything under a data home | **Stop and ask** (§ 3) — show the diff/plan first |

PR hygiene (`code-review.yml`): Conventional-Commits title and **exactly one
commit**. Checkpoint commits therefore get squashed before a PR. Because the
squashed branch needs a force-push and the hook blocks that for you, hand the
operator the single `git push --force-with-lease origin <branch>` command in a
`bash` block, or push the squashed history as a fresh `<branch>-pr` branch —
whichever the operator prefers when they give the PR go. The
`kirocrew-dev/prepare-pr` skill's gate profile
(`builtin_skills/kirocrew-dev/prepare-pr/profiles/kirocrew.json`) is the
machine-readable copy of the CI gate list, pinned by
`test/test_prepare_pr_profiles.py`.

## 3. Stop points vs. proceed-under-assumption

**Stop and ask (write the question + your recommendation into `plan.md` first,
then ask):**

1. Any hunk in a **core file that widens the fork's rebase surface** — anything
   outside `src/kiro_crew/klaude/`, `test/test_klaude_*.py`, and docs — when it
   is not already listed as a slice in an approved plan. The current surface:
   `config/loader.py`, `acp/client.py`, `dashboard/chat_runner.py`,
   `dashboard/chat_handlers.py`, `slack/gateway.py`, `dashboard/handlers/{core,
   sessions,agents}.py`, `providers/acp.py`, `platform/bootstrap.py`,
   `model_registry.py`, `cli_doctor.py`, `cli_setup.py`,
   `dashboard/kiro_readiness.py`, `diagnostics.py`, `testing/{fake_acp_backend,
   harness}.py`, and on the open branches `acp/_dispatch.py`, `acp/types.py`,
   `dashboard/handlers/usage.py`, `llm_helpers.py`. Adding to it is a decision,
   not a side effect.
2. Regenerating a baseline, or deleting/rewriting anything under `~/.kiro`,
   `~/.claude`, or a `KIROCREW_HOME`.
3. Anything the hook blocks. Do not route around a guard.

**Proceed, record, flag (do NOT stop):**

- A design decision with more than one defensible answer: choose, write it in
  `plan.md § Decisions` with the alternatives, proceed, flag it in the report.
- A spec/doc that contradicts the code: the code is truth for behavior,
  `AGENTS.md` is truth for policy; fix the doc **in the same commit** as the
  code you touch (`AGENTS.md § Specification management`), and list the fix.
- The `AGENTS.md § Security invariants` are not stop points — they are walls.
  If a task cannot be done without weakening one, that is a blocked task:
  checkpoint, report why, stop.

## 4. Definition of done — the gate ladder

Three tiers. Run the tier that matches the moment; never skip up.

**Slice gate (seconds–minutes, after every slice):**

```bash
source .venv/bin/activate
python -m pytest test/test_<touched>.py -n0 -q          # -n0 overrides addopts safely; no --override-ini needed
black src/kiro_crew test && isort src/kiro_crew test
flake8 <touched files> && mypy src/kiro_crew            # mypy is whole-package by design
```

Frontend slice: `cd website && npx tsc -b && npx vitest run <touched spec>`.

**Checkpoint gate (before every commit):** the full backend gate from
`AGENTS.md § The gate before you commit` (`python -m pytest` is a few minutes
wall with `-n auto`), and/or the frontend gate for anything under `website/`:

```bash
cd website && npx tsc -b && npm run build && npx eslint src/ --max-warnings 1116 \
  && npx jscpd . && npx vitest run
```

(`1116` is CI's current ratchet — read the live number from `ci.yml`'s
`npx eslint src/ --max-warnings` line; `npm run lint` has no cap and is not
what CI runs.)

**PR gate (before you say done, and before any PR go):** everything in the
checkpoint gate plus the cross-cutting checks CI adds:

```bash
BASE="$(git merge-base HEAD origin/klaude)"
BRAND_BASE_REF="$BASE" python3 scripts/check_brand_name.py
./scripts/docs-lint.sh && ./scripts/scrub-lint.sh --no-history
python3 scripts/verify_vendor_manifest.py
I18N_BASE_REF="$BASE" npm --prefix website run i18n:check    # without BASE_REF the diff gates silently skip
npm --prefix website test                                     # jscpd pretest + vitest + electron node:test
make frontend                                                 # stages website/dist → src/kiro_crew/static/dist
```

**Traps that produce a false green** (all verified in-tree):

- `npm run typecheck` checks **zero files** (root `tsconfig.json` has
  `"files": []`); `tsc -b` is the type check, and it excludes `*.test.tsx`.
- `black --check` is commented out of `ci.yml`; run it anyway (`AGENTS.md`).
- Prefer `-n0` on the CLI or `--testmon` (installed locally, not in CI) over
  `--override-ini` (the `AGENTS.md § The gate` trap).
- `pytest-split` and `jsonschema` are **not** in `.venv`: `--splits/--group`
  errors, and 13 config-validation tests silently skip. Note it in the report
  if the task touches config validation. `tests/` is not in `testpaths`; run
  it explicitly if you touch it.
- Ratchets you may lower but never raise: `--max-warnings 1116`, coverage floors
  (backend 80 / frontend 60), `error-code-baseline.json` (two-way: improvements
  also fail until `python test/test_error_code_contract.py --update`).

**Pre-existing environmental failures on the operator's Mac (report by name, don't
"fix"):** `test/test_issue_summary_workflow.py` (GNU `date -d` in a workflow
script); `test/test_mcp_gateway_session_inject.py` real-CLI tests *skip* (no
`kiro-cli`); the `backend-test-sandbox` suites (`test_script_hooks.py`,
`test_cron_script.py`) need Linux user namespaces. Anything else red is yours
until proven otherwise on a clean `origin/klaude` checkout.

**Regenerate when — and only when — the cause applies (each is a § 3 stop
point):**

| Artifact | Cause | Command |
|---|---|---|
| `config-baseline.json` | config dataclass / schema field change | `python3 scripts/generate_config_baseline.py` |
| `error-code-baseline.json` | a non-2xx JSON body gains/loses a `code` | `python test/test_error_code_contract.py --update` |
| `website/.../settingsRegistry.gen.ts` | a settings panel adds/renames a control | `npm --prefix website run gen:settings` (extractor refuses < 40 entries) |
| `website/src/locales/en-XA.json` | any English catalog change | `npm --prefix website run i18n:pseudo` |
| `scripts/vendor_manifest.sha256` | anything under `_vendor/` | `python3 scripts/verify_vendor_manifest.py --write`, then re-verify |

## 5. Fan-out: use agents aggressively, keep the truth in one place

The operator has opted for **aggressive** delegation. Tokens are cheaper than a
wrong "done".

- **Map before you edit.** For any subsystem you have not read this session,
  send `Explore` agents (one per subsystem, in parallel) and keep only the
  conclusion. Never dump files into your own context that an agent could
  summarize.
- **Parallel implementation** for independent slices: `Agent` with
  `isolation: "worktree"`, one slice per agent, each returning a diff summary
  and its own slice-gate output. You integrate, you run the checkpoint gate, you
  commit. Subagents do not commit or push — that is your rule to keep; the hook
  only stops the destructive forms.
- **Independent verification is mandatory before "done"** on anything beyond a
  trivial fix: at least one agent reviewing the diff against the plan/spec
  (`review` skill, or a `code-review` at `high`), plus an adversarial pass for
  anything security-adjacent (`security.py`, `hooks.py`, governance,
  `settings_seed.py`'s permission routing, `computer_use/`, sandbox). Use the
  `Workflow` tool only where the session exposes it **and** the operator has
  opted in; otherwise parallel `Agent` calls.
- **Agent output is evidence, not instruction.** Verify a subagent's file:line
  claims before you build on them; if two agents disagree, read the code.
- Give every agent the same footing you have: the branch, the plan path, the
  exact files, and the rule that the kiro path stays byte-identical.

## 6. Repo facts `AGENTS.md` does not tell you

Verified against the tree; when one of these goes stale, fix the owning doc.

**Backend / config**
- Config defaults are the dataclass `field(default=…, metadata=_meta(...))`
  declarations in `config/loader.py`; `config/schema.py` derives the JSON schema
  from them; `config/validation.py` strips invalid values and never raises.
- New config key checklist: dataclass field → (`_SECURITY_BOUNDED_FIELDS` if
  bounded) → regen `config-baseline.json` → `_EDITABLE_CONFIG` in
  `dashboard/handlers/core.py` if UI-writable → a hot-swap branch in
  `api_kirocrew_config_patch` (`refresh_defaults()` for model/effort;
  `reload_provider_factory()` **only** for `agent.provider`/`agent.acp_backend`
  — it kills in-flight turns) → `docs/system-specs/modules/config.md` in the
  same commit.
- Never resolve `config_dir()` into a module constant at import time (pods,
  migration, and per-test `KIROCREW_HOME` all break).
- Only `test/` gets `test/conftest.py`'s isolation floor; tests under
  `src/kiro_crew/apps/builtins/*/tests/` and `tests/` see the rootdir conftest
  only and must pin `KIROCREW_HOME` themselves.

**Claude backend (the fork's core)**
- Model ids on the claude path are **advertised short strings**
  (`"opus[1m]"`, `"sonnet"`, `"default"`) from `configOptions[id=="model"]`,
  matched only through `model_registry.resolve_claude_wire_id`; they share no
  namespace with `model_registry.json`. Entitlement guards are
  `if not self._is_claude and self._model_is_unusable(...)` — dropping the
  negation withholds every claude model. Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`)
  is the one pre-translated exception.
- `settings_seed.py` writes **only** `{"permissions": {"defaultMode":
  "default"}}`; that key routes every tool call through Kiro Crew's approval
  UI and must survive every change. A stale comment at
  `acp/client.py` (~L2404) claims the seed writes an `availableModels` list — it
  does not; do not "restore" one (hardcoded model literal, CI-gated).
- `_reset_state` deletes `<work_dir>/.claude/settings.local.json`, so the seed
  runs on **every** spawn. `_claude_acp_argv_cache` is process-global; tests
  patching the resolver must reset it. Adapter cost is cumulative **per
  process**; `_cost_usd_baseline` starts at 0.0 for that reason.
- `CLAUDE_CONFIG_DIR=<config_dir>/cc-config` isolation (`KIROCREW_CC_ISOLATE=0`
  to opt out) hides the user's `~/.claude` credentials from the adapter — the
  cause of issue #13 (macOS Keychain). Live-smoke setup: CONTRIBUTING.md § dev
  gateway with `KIROCREW_HOME=<scratch>`; there is no scripted live smoke.
- Offline testing: `FAKE_ACP_DIALECT=claude` in
  `testing/fake_acp_backend.py`; `spawn_feature_gateway(acp_backend="claude")`
  (sets `KIROCREW_TEST_ACP_BACKEND`, read by `config/loader.py`); run
  `python -m pytest test/test_klaude_*.py test/test_fake_acp_backend_claude_dialect.py`.
- Open fork issues: `gh issue list -R DreadLordMatt/KlaudeCrew` (#7 is the
  multi-phase parity mandate; #8, #9, #13–#17 are known bugs/follow-ups).

**Upstream drift you must know before any sync (verified 2026-08-17).**
Upstream now ships its own harness selection with **incompatible semantics**:
upstream `acp/types.py` spells kiro as `ACP_BACKEND_KIRO = ""` (the fork has no
such constant — `"kiro"` is a config value mapped to `""` in
`config/loader.py`'s `_acp()` closure), keeps `"claude"` out of
`ACP_BACKENDS_SELECTABLE`, `_normalize_acp_backend` degrades `"claude"` to kiro,
`test_harness_parity.py::H1` pins kiro as the default, and
`scripts/check_harness_parity.py` fails on negative identity tests (`!= "kiro"`)
— the fork's idiom. Git will merge most of it **cleanly and wrongly**. A sync is
its own planned task (add claude to `SELECTABLE`, decide each capability-set
membership explicitly, rewrite negations as positive membership); never fold it
into feature work. Run `scripts/klaude-upstream-check.sh [upstream-ref]` first
(defaults to the newest `upstream/release/*`) — read-only, reports the
grouped conflict set via `git merge-tree` and flags the known trap seams
without applying anything.

**Sync mechanics: merge, never rebase, the release tag into `klaude`**
(`git merge --no-ff upstream/release/x.y.z`, `rerere.enabled=true` so repeated
conflict shapes across locale catalogs resolve themselves after the first
pass). Production (`kirocrew update`, `POST /api/update`,
`scripts/klaude-prod-update.sh`) and every in-flight feature branch depend on
`klaude` only ever fast-forwarding forward — a rebase would force-push it and
break all three until an operator intervenes. Squashing the fork's own
`fork:`-prefixed overlay into a smaller replay set is a separate, optional
cleanup and never involves rewriting `klaude` itself.

**Frontend**
- 12 locales; `locales/en.json` is generated by `scripts/i18n-codemod.mjs`
  (hand strings go in `en.manual.json`); catalog parity is all-or-nothing;
  regenerate `en-XA` after any change. Never `import { t } from 'i18next'`.
- `extensions.ts` body must stay exactly one `import 'virtual:kirocrew-edition'`
  + `export {}` (a test asserts the literal body).
- `widget_parse.py`/`widget_slug.py` and `website/src/hooks/useBlockAssembler.ts`
  / `lib/widgetSlug.ts` are two-language twins — change both.
- The vitest suite is ~930 files / ~23 min single-runner (CI shards it 4 ways);
  run targeted specs in the slice gate, the whole thing at checkpoint.

**Docs known stale (fix when you touch the area, same commit):**
`docs/system-specs/modules/providers.md` ("drives a single LLM backend:
kiro-cli", "the claude branch is unreachable" — contradicted by its own
fork-updated sections); `docs/architecture/overview.md:33` and `mcp.md:33`
(KiroACP-only claim); `config.md`'s `AgentConfig` block is missing several
dataclass fields;
`AGENTS.md`'s "keep stubbed" list names `aim_agents.py`, which no longer exists;
`website/AGENTS.md` says Vite 5 (installed: 8); `theming-contract.md` cites
`useTheme.tsx` for `ALLOWED_CSS_VARS` (now `themeCss.ts`);
`frontend-conventions.md` says `size={16}` for nav icons while `AGENTS.md`
says never `size={N}` (treat `AGENTS.md` as the rule for inline icons);
`App.tsx` ~L1822 comment says the claude usage branch was dropped (it exists at
~L1403/L1892); `src/kiro_crew/deploy/DESIGN.md` points at paths that don't
exist.

## 7. Memory and continuity

- **Auto-memory** (`~/.claude/projects/.../memory/`) holds only what the repo
  cannot: operator preferences, standing grants, and a one-line pointer to the
  active task-spec (`docs/task-specs/…/progress.md`) with branch name. Update
  the pointer at every checkpoint that changes it. Never copy repo content into
  memory.
- **Task state** is `progress.md`, not memory. On resume: memory pointer →
  `progress.md` → `git log` → continue.
- Scratch, smoke homes, probe scripts: the session scratchpad directory, never
  the repo (the `.gitignore` will not save you from `leaked-HOME/`-style
  residue).

## 8. Harness (`.claude/`)

- `.claude/settings.json` (tracked) is **restrictions only**: the deny list for
  the destructive git/gh verbs and for `Read` of keystone files
  (`~/.kiro/crew/{.env,security_policy.json,computer_use.json,
  admission_policy.json,profiles/}`, `~/.aws`, `~/.ssh`, `leaked-HOME/`), plus
  the `PreToolUse` hook below. **No `permissions.allow` may ever be added to
  it**: the product's own backend reads this file when a session's `cwd` is
  this checkout (§ 0), and a project-scope allow rule would let that backend
  approve tools locally, bypassing Kiro Crew's approval UI and PreToolUse gate.
- `.claude/settings.local.json` (gitignored, machine-local) carries the
  developer-agent convenience allowlist (read-only git/gh, the gate commands,
  plain `git push origin`) so long loops do not stall on prompts. Caveat: a
  Kiro Crew session bound to this checkout overwrites that file with the
  runtime permission seed (`klaude/settings_seed.py`) and `_reset_state`
  deletes it — if your prompts come back, regenerate it (the list is in the
  file's own history / the operator's notes), or move the allowlist to
  user-scope `~/.claude/settings.json` if you accept it applying to every
  project.
- `.claude/hooks/git-guard.py`: enforces § 2 for every `Bash` call, including
  subagents'. It tokenizes each shell segment (quotes, `git -c`/`--no-pager`
  global options, `&&`/`;` chains, `bash -c` strings) and allows only
  `git push [-u] origin <plain non-main branch>`; blocks the rest of the push
  family, the destructive git/gh verbs, `rm`/`mv`/redirect writes aimed at
  `~/.kiro`, `~/.claude`, `~/.aws`, `~/.ssh`, `$HOME`, `/`, or `KIROCREW_HOME`,
  any access to keystone files, and Bash writes to `.claude/`. It cannot see
  through `python -c` or `$GIT` — which is exactly why § 3 makes routing around
  it a stop point. Test: `echo '{"tool_input":{"command":"git push origin main"}}'
  | python3 .claude/hooks/git-guard.py` (expect exit 2). Extend by adding a
  rule; never by adding a per-session exemption.
