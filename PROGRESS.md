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

## Needs a decision (running list; copied into MORNING-BRIEF.md at hand-off)

- **Vendored `anime.es.js:1296` TODO** ("naming, documentation") — upstream anime.js v3.2.2's own author-note, bundled directly under `website/src/lib/` rather than via `node_modules` or a conventionally-exempt vendor directory. Options: (a) track it as a real tech-debt item anyway since it's literally under `src/`, (b) reframe it as "upgrade vendored anime.js" rather than "fix naming/docs" (upstream's problem, not ours, to fix in place), or (c) drop it and let a future vendor-manifest bump pick up whatever upstream does with it. No action taken; not tracked as a task.
