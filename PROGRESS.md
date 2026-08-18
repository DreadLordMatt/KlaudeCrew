# PROGRESS

Append-only log of what the overnight orchestrator did, decided, and abandoned. Newest entries at the bottom.

## 2026-08-18 — Setup

- Seeded `TASKS.md` from the operator-provided backlog (baseline-discovery → bug-fix → tech-debt-triage → UI-polish). Commit `372560d`.
- Branch: `claude/klaudecrew-overnight-orchestrator-rgj9yp` — this is the harness/CCR-designated branch for this session, and takes precedence over the `auto/overnight-YYYYMMDD` naming convention named in the orchestrator prompt (that convention is for a different, local/unattended-CLI execution context; the substance of the loop is unaffected).
- `make backend` (venv + backend deps) and `website/`'s `npm install` both completed cleanly with no setup errors.
- Opened draft PR #19 (`DreadLordMatt/KlaudeCrew`) as the running status surface for this run; subscribed to its activity so CI failures/review comments wake this session.
- Cross-checked the repo's real open GitHub issues (#7, #8, #9, #13-#17) for context. #7 is a large multi-phase architecture mandate ("Claude Code backend feature-completeness") — explicitly not attempted unattended; noted under Suggested next / Needs a decision in the eventual morning brief. The rest may overlap with baseline-discovered bugs but `TASKS.md` (not the issue tracker) is what's driving tonight's work, per the operator's explicit backlog.

## 2026-08-18 — DEBT-2 [chore] frontend TODO/FIXME/HACK/XXX triage — DONE

- Surface: frontend. Read-only — no code changes, per the task's own instruction.
- What: grepped `website/src` for `TODO`/`FIXME`/`HACK`/`XXX` (word-boundary, exact case). 10 raw hits across 8 files; `FIXME`/`HACK`/`XXX` had zero real hits anywhere. Of the 10 `TODO` hits, only 2 are genuine first-party pending-work markers, 1 sits inside vendored third-party code (`anime.es.js`, MIT-licensed, bundled under `src/lib/`), and the remaining 7 are prose false-positives describing the shipped agent "TODO list" UI feature (types, comments, a test name) — not pending-work markers, so not catalogued as tasks.
- Result: catalogued into `TASKS.md` under a new "Tech-debt-derived tasks" section — see `FEAT-1` and `ARCH-1` below. One item flagged under Needs a decision (see that section): whether the vendored `anime.es.js` TODO is KlaudeCrew's debt to track at all, or something to leave to a future vendor bump.
- Commands run: none that mutate state (grep + read only).
- Follow-ups: none beyond the two filed tasks.

## 2026-08-18 — DEBT-1 [chore] backend TODO/FIXME/HACK/XXX triage — DONE

- Surface: backend. Read-only — no code changes.
- What: grepped `src/` (case-insensitive, all file types, excluding vendored compiled binaries). 85 raw hits/27 files. Split into 7 genuine first-party markers (6 files), 37 markers inside vendored `_vendor/llama_cpp/` (excluded per `AGENTS.md` — never touch `_vendor/`, it's checksum-pinned), and 41 false positives (the product's own "TODO list" UI feature referenced by variable/field name, placeholder text like `dashboard:xxx`, or the unrelated ML term "reward-hacking"). Zero genuine `FIXME`; zero genuine `XXX`; one genuine `HACK` (vendored).
- Result: filed `BUG-1` (force-consolidate offset-tracking fix, `eval/runner.py`/`history.py` — clean, non-core, good next pickup), `ARCH-2` and `ARCH-3` (both proposal-only — one touches core file `providers/acp.py`, the other is governance/security-adjacent and already author-deferred), `WEIXIN-1` (proposal-only — well-scoped but needs live external-service credentials to verify, so not attempted unattended), `DEBT-1b`/`DEBT-1c` (both blocked on an external dependency that doesn't exist yet — upstream kiro-cli PR #2034, and a not-yet-existing sanctioned-credential-writer registry). One item flagged under Needs a decision (`mcp_gateway/stub.py:86` — ambiguous exact field-list scope).
- Commands run: none that mutate state (grep + read only).
- Follow-ups: none beyond the filed tasks above.

## Needs a decision (running list; copied into MORNING-BRIEF.md at hand-off)

- **Vendored `anime.es.js:1296` TODO** ("naming, documentation") — upstream anime.js v3.2.2's own author-note, bundled directly under `website/src/lib/` rather than via `node_modules` or a conventionally-exempt vendor directory. Options: (a) track it as a real tech-debt item anyway since it's literally under `src/`, (b) reframe it as "upgrade vendored anime.js" rather than "fix naming/docs" (upstream's problem, not ours, to fix in place), or (c) drop it and let a future vendor-manifest bump pick up whatever upstream does with it. No action taken; not tracked as a task.
- **`src/kiro_crew/mcp_gateway/stub.py:86`** — TODO says to hash "relevant config fields (e.g. tool allowlists, hook settings)" to detect non-security config drift, but gives examples rather than an exhaustive field list. Picking the right precision (broad enough to catch real behavioral drift, narrow enough to avoid needless pool-splitting churn) is a design call for the owner, not filed as a ready task.
- **`src/kiro_crew/weixin/transport.py:13`** (`WEIXIN-1` in `TASKS.md`) — outbound Weixin media send is well-scoped and implementable, but verifying it needs live Weixin credentials/service access this environment doesn't have, and the overnight guardrails say not to wire up new external-service calls unattended. Filed as proposal-only.
- **`ARCH-2`/`ARCH-3`/`ARCH-1`/`WEIXIN-1`** (see `TASKS.md`) — all proposal-only per this repo's stop-and-ask rule (core-file rebase surface or governance/security-adjacent). None implemented; each needs an explicit human go before any code.
