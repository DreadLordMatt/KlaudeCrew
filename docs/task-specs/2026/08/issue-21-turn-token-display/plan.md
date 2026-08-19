# Show token counts on the per-turn stats line — plan

Issue: [#21](https://github.com/DreadLordMatt/KlaudeCrew/issues/21)

## Goal

The per-message turn-stats line under an assistant reply (the "$0.16 · 10s"
text) shows cost and elapsed time but not token counts, even though the
backend already has the data. Extend that line to also show tokens, so the
number is useful even on subscription/OAuth billing where `cost_usd` is an
API-equivalent estimate rather than a real charge.

## In scope

1. Thread `TurnUsage`'s existing `input_tokens` / `output_tokens` /
   `cache_creation_tokens` / `cache_read_tokens` fields through
   `_attach_turn_stats()` into the per-message `meta.turn_stats` dict.
2. Extend the frontend `TurnStats` type and the `AssistantMessage.tsx` render
   logic to display a token count alongside cost/elapsed.
3. New i18n strings across all 12 locale catalogs.
4. Backend + frontend tests extending the existing coverage.

## Out of scope

- Changing how tokens are counted or parsed at the ACP dispatch layer
  (`src/kiro_crew/acp/_dispatch.py`) — that's already correct and unrelated to
  this display gap.
- The aggregate usage pill / `GET /api/sessions/usage`
  (`src/kiro_crew/dashboard/handlers/usage.py`) or the overview `TokenDailyChart` —
  this is specifically the per-message line in the chat transcript.
- Any new cost-estimation logic — `cost_usd` is untouched.

## Current state (source-verified 2026-08-19)

- `TurnUsage` (`src/kiro_crew/acp/types.py:241`) already carries all four
  token fields; each provider fills what it bills in and leaves the rest at 0
  ("claude_code/bedrock fill token counts + cost_usd, kiro fills credits").
- `_attach_turn_stats()` (`src/kiro_crew/dashboard/chat_runner.py:954`) is the
  only place that builds the `stats` dict attached to
  `meta.turn_stats` on the last assistant message. It currently takes
  `elapsed_ms`, `credits`, `cost_usd` — no token params — and follows an
  "omit zero fields so the frontend renders only what the provider actually
  bills in" convention (`if credits > 0: stats["credits"] = ...`, same for
  `cost_usd`).
- The call site is around `chat_runner.py:6073`, which already has the
  `TurnUsage` instance in scope as `_u` (`_turn_cost_usd = float(_u.cost_usd or 0.0)`
  at line 5623) — the token fields are sitting right there unused.
- Backend test coverage: `test/test_turn_stats.py` — a focused unit-test file
  for exactly this function (`test_attaches_elapsed_and_credits`,
  `test_zero_credits_key_omitted`, `test_zero_cost_key_omitted`,
  `test_boundary_scopes_to_current_turn_assistant`, etc.). Follow its existing
  `_ChatSlot` fixture pattern for the new cases.
- Frontend: `TurnStats` interface (`website/src/pages/chat/AssistantMessage.tsx:27`):
  `{ elapsed_ms: number; credits?: number; cost_usd?: number }`. Render logic
  ~lines 226–278 builds the displayed string, already following the same
  "only show what's present" convention (`hasCredits`, `hasCost` booleans).
- Frontend test coverage: `website/src/test/AssistantMessage.test.tsx` already
  renders `<AssistantMessage ... turnStats={{ elapsed_ms, credits/cost_usd }} />`
  per case — extend with `input_tokens`/`output_tokens` cases the same way.
- Number formatting precedent: `website/src/pages/overview/TokenDailyChart.tsx`
  has a local, unexported `fmtNum()` (1.2M/1.2K compact notation) used for the
  aggregate daily chart. Cost formatting has a canonical shared helper,
  `website/src/utils/formatCost.ts`'s `formatCost()`, explicitly documented as
  "every cost surface ... formats the same concept the same way" — but
  `AssistantMessage.tsx` currently does NOT use it; it calls the lower-level
  `fmtCurrency(cost_usd, 'USD', { maximumFractionDigits: 4, minimumFractionDigits: 4 })`
  directly, which is a pre-existing inconsistency (see Decisions).

## Slices

1. **Backend: thread the fields through.** Extend `_attach_turn_stats()`'s
   signature to accept the four token counts (or the whole `TurnUsage`
   subset), add them to `stats` following the existing zero-omission
   convention, update the call site to pass `_u.input_tokens` etc. Extend
   `test/test_turn_stats.py` with cases mirroring the existing credits/cost
   tests (attaches when present, omitted when zero, doesn't disturb the
   boundary-scoping behavior).
2. **Frontend: extend the type and render logic.** Add the token fields to
   `TurnStats`, extend the render function to show a token count when
   present. Extract a shared compact-number formatter (promote
   `TokenDailyChart.tsx`'s `fmtNum` to a small shared utility, e.g.
   `website/src/utils/formatTokens.ts`) so the daily chart and the new
   per-turn line share one formatter — consistent with the `formatCost`
   precedent of one canonical formatter per concept. Extend
   `AssistantMessage.test.tsx` with matching cases.
3. **i18n.** Add the new string(s) to `en.manual.json`, regenerate
   `en.json`, translate into the other 11 locales, regenerate `en-XA.json`
   (`npm --prefix website run i18n:pseudo`) — per `website/docs/i18n-catalog.md`.
4. **Verify.** Slice gate (`test/test_turn_stats.py -n0`, targeted vitest),
   then the full checkpoint gate before PR.

## Decisions

- **What to display:** total tokens (input + output), not a full
  input/output/cache-read/cache-write breakdown, in the compact line —
  matching the existing line's terseness ("$0.16 · 10s" stays a glance-able
  summary, not a stats table). Cache tokens are real but would be misleading
  summed in without distinction (cache reads are far cheaper per real API
  pricing than fresh input), so they're left out of the headline number.
  Rejected alternative: show a full breakdown inline — too noisy for a
  footer line that's meant to be skimmed; a future tooltip/expansion could
  surface the full `TurnUsage` breakdown without crowding the default view,
  but that's not in scope here.
- **Formatter reuse:** promote `fmtNum` out of `TokenDailyChart.tsx` into a
  shared utility rather than duplicating a compact-number formatter in
  `AssistantMessage.tsx`. Rejected alternative: leave it duplicated — cheap
  now, but this codebase's own `formatCost.ts` precedent argues for one
  formatter per concept.
- **Not fixing the `formatCost` inconsistency in this task.** Noted above:
  `AssistantMessage.tsx` calls `fmtCurrency` directly instead of the shared
  `formatCost()` helper other cost surfaces use. Worth fixing since this
  slice already touches that exact code, but it's a separate, small
  pre-existing issue — call it out in the PR description as a follow-up
  rather than silently bundling an unrelated fix into a feature PR.

## Open questions

- Exact label copy for the new i18n string(s) (e.g. "1.2K tok" vs "1.2K
  tokens" vs an icon+number with no unit). Proceeding under the assumption
  that a short "N tok"-style suffix matches the existing terse style ("59s",
  "2.5 credits"); a human should sanity-check the final copy before this
  ships, not block the implementation on it.

## Follow-ups (not in scope)

- A tooltip/expansion showing the full `TurnUsage` breakdown (cache
  read/write split) for turns that lean heavily on prompt caching.
- Fixing `AssistantMessage.tsx`'s direct `fmtCurrency` call to route through
  `formatCost()` instead, for consistency with other cost surfaces.
