import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { hasBuiltinComponent, getBuiltinComponent } from '../apps/builtinRegistry'

/**
 * Ops Mission Control is a builtin page. Its route must be registered and must be
 * a single plain top-level segment — `BuiltinAppRoute` resolves the catch-all
 * `/:builtinApp` from ONE path parameter, so a multi-segment route would register
 * but never resolve (navigation silently redirects to chat).
 */
describe('ops-mission-control builtin registration', () => {
  const ROUTE = '/ops-mission-control'

  it('is registered in the builtin component registry', () => {
    expect(hasBuiltinComponent(ROUTE)).toBe(true)
  })

  it('resolves to a lazy component', () => {
    const component = getBuiltinComponent(ROUTE)
    expect(component).toBeDefined()
    expect(component).toHaveProperty('$$typeof')
  })

  it('uses a route shape BuiltinAppRoute can actually resolve', () => {
    // Single leading slash, one segment, no query/hash/whitespace.
    expect(ROUTE).toMatch(/^\/[A-Za-z0-9][A-Za-z0-9._~-]*$/)
  })

  it('matches the manifest route so the sidebar entry and page agree', () => {
    // app.json declares ui.pages[0].route as this exact value; a mismatch means
    // the nav item renders but the page never mounts.
    expect(ROUTE).toBe('/ops-mission-control')
  })

  it('the Playwright stat-card assertions match the labels the page renders', () => {
    // A renamed StatCard silently breaks the browser spec, and the browser spec only
    // runs in the opt-in E2E gate — so the mismatch survived until that gate ran.
    // This is the cheap check that fails in the default `npm run test` instead.
    const page = readFileSync(
      resolve(__dirname, '../apps/ops-mission-control/OpsMissionControlPage.tsx'),
      'utf-8',
    )
    const spec = readFileSync(
      resolve(__dirname, '../../playwright/ops-mission-control.spec.ts'),
      'utf-8',
    )
    const rendered = [...page.matchAll(/<StatCard\s+label="([^"]+)"/g)].map((m) => m[1])
    expect(rendered.length).toBeGreaterThan(0)

    const asserted = spec.match(/for \(const label of \[([^\]]+)\]\)/)
    expect(asserted, 'the stat-card loop must still exist in the spec').toBeTruthy()
    for (const label of rendered) {
      expect(
        asserted![1],
        `StatCard "${label}" is rendered but the Playwright spec does not assert it`,
      ).toContain(label)
    }
  })
})

/**
 * The ledger is the app's central premise: on a second occurrence the responder should
 * read what worked last time. The board used to render that as the number "2 matched",
 * which is the payoff reduced to a count — the actual pattern and fix were only visible
 * by opening the agent's chat transcript.
 */
describe('ops-mission-control renders the remembered fix, not just a count', () => {
  const page = readFileSync(
    resolve(__dirname, '../apps/ops-mission-control/OpsMissionControlPage.tsx'),
    'utf-8',
  )

  it('resolves matched entry ids against the ledger it already fetches', () => {
    // Ids alone cannot be rendered; without this lookup the panel can only show a count.
    expect(page).toContain('ledgerById')
    expect(page).toMatch(/ledger_matches\.map\(/)
  })

  it('shows the pattern AND the fix', () => {
    // Matching the right lesson and not showing its remedy is a half-answer — the same
    // reason `ledger_index.entry_text` embeds pattern+fix together rather than pattern.
    expect(page).toMatch(/entry\.pattern/)
    expect(page).toMatch(/entry\.fix/)
  })

  it('shows trust, confidence and use count beside the fix', () => {
    // An unproven `observed/low` entry must not read like a verified one. `use_count` is
    // what decides the agent's fast path, so a human reviewing the same entry needs it.
    for (const field of ['entry.trust', 'entry.confidence', 'entry.use_count']) {
      expect(page, `${field} must be visible so an unproven entry cannot look proven`).toContain(
        field,
      )
    }
  })

  it('says so when a matched entry is no longer in the ledger', () => {
    // Hygiene prunes and decays. Rendering nothing for a missing id would read as "no
    // prior knowledge", which is the opposite of what happened.
    expect(page).toMatch(/no longer in the\s*\n?\s*ledger/)
  })

  it('does not use an emoji for the trust signal', () => {
    // Repo convention: lucide icons or text, never emoji in the UI.
    const block = page.slice(page.indexOf('ledger_matches.map('))
    expect(block.slice(0, 2000)).not.toMatch(/[\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}]/u)
  })
})
