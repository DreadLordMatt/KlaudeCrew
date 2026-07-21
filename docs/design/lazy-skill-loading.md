# Lazy Skill Loading: Usage-Ranked Top-K Injection with Per-Section Context Budgets

Status: Shipped in MeshClaw (opt-in, `skills.lazy_load`). This doc describes the
design so it can be evaluated for KiroCrew.

## Problem

Session-start context injection dumped a summary line for **every** installed
on-demand skill, uncapped. With many skill packages installed this block alone
dominated the single shared context budget (a flat ~165k chars, ~55k tokens).
Worse, the budget was enforced by **position-based hard truncation**: whatever
happened to be assembled last was silently dropped. Skills are assembled early,
so a bloated skill list starved higher-value tail content -- learned lessons and
provenance -- without any signal to the user or the model.

Two distinct failure modes, one root cause:

1. Skill catalog size scales with installed packages, not with usefulness.
   Most skills are never used in a given workspace, yet all paid full price
   every turn.
2. All context sections competed for one pool, so any one section's bloat was
   another section's data loss.

## Goals

- Keep the skill catalog discoverable without paying for the full catalog
  every turn.
- No section can starve another section. Truncation must be deterministic and
  local to the offending section.
- Zero behavior change unless explicitly enabled (safe default; byte-for-byte
  legacy output when off).

## Design

### 1. Opt-in flag, zero-impact default

A single config switch (`skills.lazy_load`, default **off**). Off preserves the
legacy behavior exactly: one flat budget, full unranked skill dump. On enables
the three mechanisms below. This mirrors the pattern used for MCP prewarm
(`prewarm_count=0` default) -- upgrades never silently change context shape.

### 2. Usage-ranked top-K skill injection

`skills.get_context(budget)` gains a budget parameter:

- `budget=None` -> legacy full dump (the off path).
- Integer budget -> two tiers:
  - **Pinned skills** (`always: true` in skill frontmatter) are injected in
    full, unconditionally. These are the operator's "must always be visible"
    set.
  - **On-demand skills** are ranked by a persistent usage score and injected
    hottest-first until the budget is exhausted (top-K where K is whatever
    fits, not a fixed constant).
- The tail (everything that did not fit) is summarized as a count plus a
  pointer to the discovery mechanisms: a `skill_search` tool, `$skillname`
  inline tokens, and trigger-phrase auto-loading. Nothing becomes
  unreachable -- it just stops being pre-paid.

### 3. Persistent usage ledger

A `SkillUsageLedger` records a hit every time a skill is actually loaded at a
real injection point (trigger match, `$skillname` resolution), **regardless of
whether lazy load is enabled**. This means the ranking is warm-started with
real usage data by the time a user opts in.

Ledger properties:

- Per-skill hit tally with TTL so stale usage decays.
- Sort key is `(hits, max(last_seen, recency_boost))` -- primary by hit count,
  tie-broken by recency. The recency boost uses the skill's install/update
  time so a freshly added skill is not starved behind old favorites before it
  has had a chance to accumulate hits.
- Debounced atomic flush to disk (write-behind), so hot paths never block on
  I/O and a crash cannot corrupt the ledger.

### 4. Tail discovery: `skill_search`

A new built-in tool that greps the full skill catalog by keyword (name and
description first, body as fallback) and returns matching skills with their
file paths. The model is told in the injected block that the tail exists and
how to find it. This converts the catalog from push (pay every turn) to pull
(pay on demand), the same shape as MCP tool-search for tool specs.

### 5. Per-section context budgets

The core structural change (design credit: this section split was proposed
during review). Instead of all sections sharing one pool with positional
truncation, each section gets an **independent cap defined as a percentage of
a fixed base** (165k chars ~= 55k tokens):

| Section              | Share of base |
|----------------------|---------------|
| Compressed history   | 27%           |
| Lessons              | 22.6%         |
| Thread history       | 21%           |
| Daily history        | 16%           |
| Skills (top-K block) | 15%           |
| Steering resources   | 10%           |
| Semantic memory      | 7.7%          |
| Episodic memory      | 7.7%          |
| Projects             | 3.9%          |
| Preamble headroom    | 3%            |
| Preferences          | 2.6%          |

Invariants:

- Each section is truncated to **its own cap**. A bloated skill catalog can
  only truncate the skills block, never lessons or memory.
- The global ceiling is the **sum of the section caps** (~1.15x base), and
  global truncation remains only as a last-resort backstop. The
  `global == sum(sections)` invariant is enforced by a unit test so a new
  section cannot be added without accounting for it.
- Off path keeps the flat legacy ceiling.

### 6. Follow-up layer: window-proportional scaling (shipped separately)

The percentages above were originally computed against a base tuned for a 1M
token window, so the same absolute char counts consumed ~5x the proportional
share on a 200K model. A follow-up change scales the base linearly with the
active model's context window (`base(window) = base * window / 1M`, floored at
20% of base so a pathologically small window cannot zero out injection). With
both layers, every section occupies the same fraction of the window on any
model.

Known gap: when the model id cannot be resolved to a window (e.g. `auto` or an
unrecognized id), the budget falls back to the 1M reference intentionally --
an unknown model never has its budget silently shrunk. On a backend that
actually serves a 200K model this fallback overshoots; fixing it requires
resolving the window from the live session rather than the model id.

## What this does and does not solve

This design governs the **injected context** (memory, lessons, history,
skills, steering). It does not govern:

- **MCP tool specs**: sent by the CLI runtime in the `tools` parameter of
  every request, outside any injection budget. Deferred tool loading
  (tool-search) is the corresponding fix at that layer.
- **Steering semantics**: steering files are capped (10%) but not filtered by
  their `inclusion:` frontmatter; that is a separate follow-up.

## Test coverage

- Ledger: hit tally, TTL decay, recency boost, atomic flush.
- Budget: `global ceiling == sum(section caps)` invariant, per-section
  truncation isolation.
- Skills: lazy top-K path, legacy full-dump path byte-equivalence, search
  fallback.
