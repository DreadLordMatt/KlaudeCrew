import { test, expect, type APIRequestContext, type Page } from '@playwright/test'

/**
 * Ops Mission Control e2e — browser-side verification that the app EXISTS,
 * LOADS, and every mode WORKS.
 *
 * Why this exists: the app is `defaultEnabled: false`, so it is invisible in the
 * nav until enabled, and the whole surface (board, settings, dispatch) sits behind
 * a `_require_enabled` gate that 403s while disabled. Both are correct behavior
 * and both look identical to "the app is missing" from the outside — the failure
 * this suite is here to tell apart.
 *
 * These tests MUTATE gateway state (they enable the app and write provider
 * config). That is deliberate: enabling is the thing under test. They are written
 * to be idempotent so a re-run against an already-enabled gateway still passes.
 *
 * Run against a live gateway:
 *   PLAYWRIGHT_BASE_URL=http://localhost:6777 \
 *   PLAYWRIGHT_TOKEN=$(kctoken-2 | grep ^KC_URL= | sed "s/.*token=//; s/'$//") \
 *   npx playwright test playwright/ops-mission-control.spec.ts --project=chromium
 */

const APP = 'ops-mission-control'
const ROUTE = `/${APP}`
const API = `/api/apps/${APP}`

/**
 * Serial for the WHOLE FILE, not just per-describe.
 *
 * These tests share one mutable resource: the app's enabled flag on the live
 * gateway. `describe.serial` only orders tests *inside* its own block, so a
 * parallel worker running the enable-gate suite would flip the flag underneath
 * the "appears in Discover while disabled" test — which is exactly the flake
 * observed on the second run. File-level serial is the honest fix; the suite is
 * ~15s, so there is nothing to gain from parallelism here.
 */
test.describe.configure({ mode: 'serial' })

type AppEntry = {
  name: string
  displayName: string
  enabled: boolean
  origin?: string
  manifest?: {
    displayName?: string
    iconUrl?: string
    crons?: unknown[]
    ui?: { pages?: { route: string; label?: string }[] }
  }
}

async function listApps(request: APIRequestContext): Promise<AppEntry[]> {
  const res = await request.get('/api/apps')
  expect(res.ok()).toBeTruthy()
  const body = await res.json()
  return Array.isArray(body) ? body : (body.apps ?? [])
}

async function findApp(request: APIRequestContext): Promise<AppEntry> {
  const app = (await listApps(request)).find((a) => a.name === APP)
  expect(app, `${APP} must be present in /api/apps`).toBeTruthy()
  return app as AppEntry
}

/** Enable the app if it is not already. Idempotent. */
async function ensureEnabled(request: APIRequestContext): Promise<void> {
  const app = await findApp(request)
  if (app.enabled) return
  const res = await request.post(`/api/apps/${APP}/enable`)
  expect(res.ok(), 'enable must succeed').toBeTruthy()
}

/** Disable the app if it is enabled. Idempotent. */
async function ensureDisabled(request: APIRequestContext): Promise<void> {
  const app = await findApp(request)
  if (!app.enabled) return
  const res = await request.post(`/api/apps/${APP}/disable`)
  expect(res.ok(), 'disable must succeed').toBeTruthy()
}

/**
 * Console noise the dashboard emits on EVERY page, verified against the core
 * /settings page: the gateway's CSP lists `[::1]` sources Chromium rejects, the
 * Google-Fonts stylesheet is CSP-blocked, and some background fetches 403 on a
 * token-auth session. None originates in this app, so asserting `[]` would make
 * this suite fail for reasons it is not testing. Anything OUTSIDE this set is a
 * real regression.
 */
const BASELINE_CONSOLE_NOISE =
  /Content Security Policy|fonts\.googleapis\.com|Failed to load resource|favicon|ResizeObserver/i

/** Collect console errors so a silently-broken page cannot pass. */
function captureConsoleErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text())
  })
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`))
  return errors
}

// ── 1. The app EXISTS ──────────────────────────────────────────────────────

test.describe('Ops Mission Control — existence', () => {
  test('is registered as a builtin app with a complete manifest', async ({ request }) => {
    const app = await findApp(request)
    expect(app.origin).toBe('builtin')
    expect(app.manifest?.displayName).toBe('Ops Mission Control')
    // The nav entry and the page route must agree, or the sidebar item renders
    // and the page never mounts.
    expect(app.manifest?.ui?.pages?.[0]?.route).toBe(ROUTE)
    // All four SOP crons must ship in the manifest, or the app is inert.
    expect(app.manifest?.crons?.length).toBe(4)
  })

  test('its icon asset is actually served', async ({ request }) => {
    const res = await request.get(`/app-assets/${APP}/icon.svg`)
    expect(res.status(), 'icon must be served, not 404').toBe(200)
  })

  /**
   * The storefront's Discover catalog is built from builtins that are DISABLED
   * and not hidden (AppsPage.tsx `browseApps`); an ENABLED one correctly moves to
   * the Library tab. Both halves are asserted, and each sets the enabled flag it
   * needs rather than inheriting whatever ran before it — see the file-level
   * serial note above for why inheritance is not safe here.
   */
  test('appears in the Discover catalog while disabled', async ({ page, request }) => {
    await ensureDisabled(request)
    await page.goto('/apps', { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Ops Mission Control').first()).toBeVisible({ timeout: 20000 })
  })

  test('moves to the Library tab once enabled', async ({ page, request }) => {
    await ensureEnabled(request)
    await page.goto('/apps', { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: /Library/i }).first().click()
    await expect(page.getByText('Ops Mission Control').first()).toBeVisible({ timeout: 20000 })
  })
})

// ── 2. The enabled-gate is real ────────────────────────────────────────────

test.describe('Ops Mission Control — enable gate', () => {
  test('enabling registers all four crons into the live scheduler', async ({ request }) => {
    await ensureEnabled(request)
    const res = await request.get('/api/crons')
    expect(res.ok()).toBeTruthy()
    const body = await res.json()
    const jobs = Array.isArray(body) ? body : (body.jobs ?? body.crons ?? [])
    const mine = jobs.filter((j: { name?: string }) => (j.name ?? '').startsWith(`${APP}/`))
    expect(mine.length, 'all 4 SOP crons must register').toBe(4)
    // A cron may register PAUSED only if some tier will actually resume it, and the
    // rotation-check SOP resumes ONLY the `on_shift` tier — it is explicitly forbidden
    // from touching `always` and `primary`. Nothing else flips a manifest
    // `enabled: false`, so a paused cron on any other tier never runs at all.
    //
    // This assertion previously read "everything except rotation-check registers paused",
    // which is the same enumerate-instead-of-state bug that let `ledger-hygiene` and
    // `reconcile` ship dead in the manifest. Read the tier map from the app itself rather
    // than restating it here, so the spec cannot drift from the backend's own answer.
    const rotationRes = await request.get(`${API}/rotation`)
    expect(rotationRes.ok()).toBeTruthy()
    const tierCrons = (await rotationRes.json()).tier_crons ?? {}
    const onShift: string[] = tierCrons.on_shift ?? []
    expect(onShift.length, 'the on_shift tier must name at least one cron').toBeGreaterThan(0)

    for (const job of mine) {
      const name = job.name ?? ''
      if (onShift.includes(name)) {
        // May ship either way — rotation-check arms it from the live rotation state.
        continue
      }
      expect(
        job.enabled,
        `${name} is not on the on_shift tier, so nothing will ever resume it — ` +
          'registering it paused means it never runs',
      ).toBeTruthy()
    }
  })

  test('board API answers once enabled', async ({ request }) => {
    await ensureEnabled(request)
    const res = await request.get(`${API}/state`)
    expect(res.status()).toBe(200)
    const state = await res.json()
    // Shape contract the board UI depends on.
    expect(Array.isArray(state.incidents)).toBeTruthy()
    expect(Array.isArray(state.providers)).toBeTruthy()
    expect(state.rotation?.mode).toBeTruthy()
    expect(state.ledger).toBeTruthy()
  })
})

// ── 3. The page LOADS ──────────────────────────────────────────────────────

test.describe('Ops Mission Control — page load', () => {
  test.beforeEach(async ({ request }) => {
    await ensureEnabled(request)
  })

  test('renders the board without console errors', async ({ page }) => {
    const errors = captureConsoleErrors(page)
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveURL(new RegExp(ROUTE))

    // Page header — proves the lazy chunk resolved and mounted.
    await expect(page.getByText('Mission Control').first()).toBeVisible({ timeout: 20000 })
    await expect(
      page.getByText('Autonomous first responder', { exact: false }),
    ).toBeVisible({ timeout: 20000 })

    // A failed lazy import surfaces as this, not as a thrown error.
    await expect(page.getByText(/Failed to load/i)).toHaveCount(0)
    expect(errors.filter((e) => !BASELINE_CONSOLE_NOISE.test(e))).toEqual([])
  })

  test('shows the four stat cards', async ({ page }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    // 'Waiting on you', not 'Needs human': the card was renamed when blocked_reason
    // landed, because "Needs human" reads the same whether the agent wants one click
    // of approval or has run out of ideas. This assertion kept the old name and only
    // the packaged E2E gate caught it — my hand-run suite passed against a stale
    // bundle that still had the old label.
    for (const label of ['Active', 'Waiting on you', 'Sources wired', 'Patterns known']) {
      await expect(page.getByText(label, { exact: true }).first()).toBeVisible({ timeout: 20000 })
    }
  })

  test('the on-call team panel names every member and explains the gating', async ({
    page,
    request,
  }) => {
    // The owner asked for "clear display of the team composition", and under strict
    // gating this panel is the ONLY thing that distinguishes "a teammate holds the pager"
    // from "the schedule is broken and this instance has silently stopped working". A
    // source grep cannot prove it renders, so assert it in the browser.
    //
    // The schedule is SEEDED through the API rather than assumed: a fixture with no
    // rotation.yaml renders no panel at all (correct for a solo install), which would
    // make this spec pass vacuously.
    await ensureEnabled(request)
    const seeded = await request.put(`${API}/providers/schedule-file/config`, {
      data: { github_login: 'carol' },
    })
    expect(seeded.ok(), 'seeding the operator login must succeed').toBeTruthy()

    const state = await request.get(`${API}/state`)
    expect(state.ok()).toBeTruthy()
    const roster = (await state.json()).rotation?.roster
    if (!roster?.members?.length) {
      // No committed schedule on this fixture: the panel is correctly absent, and the
      // roster's own behavior is covered by tests/test_schedule_file.py::TestRoster.
      // Assert the ABSENCE rather than skipping — the E2E gate forbids skips because
      // they report green while verifying nothing.
      await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
      await expect(page.getByText('Board', { exact: true }).first()).toBeVisible({
        timeout: 20000,
      })
      await expect(page.getByText('On-call team')).toHaveCount(0)
      return
    }

    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('On-call team').first()).toBeVisible({ timeout: 20000 })
    for (const member of roster.members) {
      await expect(
        page.getByText(member.login, { exact: false }).first(),
        `every member must be listed, missing ${member.login}`,
      ).toBeVisible()
    }
    // The gating note is what tells an operator WHY this instance may be idle.
    await expect(page.getByText('only the on-call instance picks up work')).toBeVisible()
  })

  test('expanding an incident shows the remembered fix, not just a count', async ({
    page,
    request,
  }) => {
    // The ledger's whole payoff is that a recurrence reads what worked last time. The
    // panel used to show only "N matched" — the pattern and fix were reachable only by
    // opening the agent's chat. Asserted in the browser because the vitest checks for
    // this are source greps, which cannot prove the element renders.
    //
    // The precondition is SEEDED unconditionally via `/incident/claim`, which takes a
    // signal object and needs no HMAC. An earlier version of this spec POSTed to
    // `/webhook` UNSIGNED, so `seeded.ok()` was ALWAYS false and it always fell to a
    // branch asserting only that "Board" is visible — which is always true. It passed
    // unconditionally while testing nothing: the same green-tick-claiming-coverage
    // failure as the skip the E2E gate had already rejected here, just wearing a
    // conditional instead of a skip. No conditional now: seed, expand, assert.
    await ensureEnabled(request)
    const claimed = await request.post(`${API}/incident/claim`, {
      data: {
        signal: {
          id: 'e2e:ledger-panel',
          source: 'cloudwatch',
          title: 'E2E seeded signal for the ledger panel',
          resource: 'e2e/resource',
          severity: 'warning',
          state: 'firing',
        },
      },
    })
    // 200 = claimed, 409 = a previous run already owns it. Both leave a row on the
    // board, which is all this spec needs; anything else is a real failure.
    expect(
      [200, 409],
      `seeding an incident must succeed (got ${claimed.status()}: ${await claimed.text()})`,
    ).toContain(claimed.status())

    // Confirm the seed is visible in the API before asking the browser for it, so a
    // failure here distinguishes "the board did not render it" from "it was never
    // claimed" — two very different bugs that look identical from a missing locator.
    const state = await request.get(`${API}/state`)
    expect(state.ok()).toBeTruthy()
    const open = (await state.json()).incidents ?? []
    expect(open.length, 'the seeded incident must be open in /state').toBeGreaterThan(0)

    // Navigate AFTER seeding. The board polls `/state` on an interval, so loading first
    // and waiting would race the poll; a fresh load reads the seeded state immediately.
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Board', { exact: true }).first()).toBeVisible({ timeout: 20000 })

    const row = page.locator('[data-testid="omc-incident-row"]').first()
    await expect(row, 'the seeded incident must appear on the board').toBeVisible({
      timeout: 20000,
    })
    await row.click()

    // The panel must state the ledger outcome in WORDS. A seeded signal matches no
    // prior pattern, so "none matched" is the honest answer here; a real recurrence
    // renders "Fix:" with the remembered remedy. What must never appear is a bare
    // count with the knowledge hidden behind it.
    await expect(page.getByText(/Fix:|none matched/).first()).toBeVisible({ timeout: 10000 })
  })

  test('Board shows the board and ledger, and all three tabs', async ({ page }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Board', { exact: true }).first()).toBeVisible({ timeout: 20000 })
    await expect(page.getByText('Knowledge ledger').first()).toBeVisible()

    // Assert the tab CONTROLS, not the word "Signals" — that word is now the tab
    // label, so a bare text assertion would pass even if the panel were broken.
    for (const tab of ['Board', 'Signals', 'Handover', 'Settings']) {
      await expect(page.getByTitle(tab, { exact: true }).first()).toBeVisible()
    }

    // Source health has moved off the Board into its own tab, which is what gives
    // the incident rows their full width back.
    await expect(page.getByText('Signal sources')).toHaveCount(0)
  })

  test('advertises read-only mode in the header', async ({ page }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    // Observe is the safe default; if this badge says Act on a fresh install the
    // autonomy default has regressed.
    await expect(page.getByText('Observe').first()).toBeVisible({ timeout: 20000 })
  })
})

// ── 4. Every MODE works ────────────────────────────────────────────────────

test.describe('Ops Mission Control — modes', () => {
  test.beforeEach(async ({ request }) => {
    await ensureEnabled(request)
  })

  test('Board / Signals / Settings view switching', async ({ page }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('Knowledge ledger').first()).toBeVisible({ timeout: 20000 })

    // Into Settings.
    await page.getByTitle('Settings', { exact: true }).first().click()
    await expect(page.getByText('Autonomy').first()).toBeVisible({ timeout: 15000 })
    await expect(page.getByText('Providers', { exact: true }).first()).toBeVisible()
    // Board-only content must be gone — proves a real view swap, not an overlay.
    await expect(page.getByText('Knowledge ledger')).toHaveCount(0)

    // Into Signals — its own tab, not the old 280px rail beside the Board.
    await page.getByTitle('Signals', { exact: true }).first().click()
    await expect(page.getByText('Signal sources').first()).toBeVisible({ timeout: 15000 })
    await expect(page.getByText('Firing, not yet claimed').first()).toBeVisible()
    await expect(page.getByText('Autonomy')).toHaveCount(0)

    // Back to Board.
    await page.getByTitle('Board', { exact: true }).first().click()
    await expect(page.getByText('Knowledge ledger').first()).toBeVisible({ timeout: 15000 })
    await expect(page.getByText('Signal sources')).toHaveCount(0)
  })

  test('Signals tab polls sources on demand and reports per-source outcome', async ({
    page,
  }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await page.getByTitle('Signals', { exact: true }).first().click()
    await expect(page.getByText('Signal sources').first()).toBeVisible({ timeout: 20000 })

    // Every signal-role adapter must be listed, configured or not — an
    // unconfigured source that is simply absent is indistinguishable from one
    // that does not exist.
    for (const name of ['AWS CloudWatch', 'PagerDuty', 'Datadog', 'GitHub Issues']) {
      await expect(page.getByText(name, { exact: true }).first()).toBeVisible({ timeout: 15000 })
    }

    // Polling is an explicit action (it hits paid provider APIs), so nothing is
    // fetched until asked.
    const poll = page.waitForResponse(
      (r) => r.url().includes(`${API}/signals`) && r.request().method() === 'GET',
      { timeout: 60000 },
    )
    await page.getByRole('button', { name: /Poll now/i }).click()
    const res = await poll
    expect(res.status()).toBe(200)

    const body = await res.json()
    expect(Array.isArray(body.signals)).toBeTruthy()
    expect(Array.isArray(body.unclaimed)).toBeTruthy()
    // The per-source error map is what tells "ready but credentials expired" apart
    // from "genuinely healthy".
    expect(body).toHaveProperty('errors')

    // Summary line appears once a poll has happened.
    await expect(page.getByText(/not yet claimed/).first()).toBeVisible({ timeout: 20000 })
  })

  test('Handover digest renders and leads with a headline', async ({ page, request }) => {
    // The digest is a read-only projection, so assert the CONTRACT the panel needs
    // rather than a particular board state (which other tests mutate).
    const res = await request.get(`${API}/handover`)
    expect(res.status()).toBe(200)
    const body = await res.json()
    expect(typeof body.headline).toBe('string')
    expect(body.headline.length).toBeGreaterThan(0)
    expect(Array.isArray(body.recurring_patterns)).toBeTruthy()
    expect(Array.isArray(body.open_work?.waiting_on_you)).toBeTruthy()
    // Coverage must name blind spots, not just count what is on: an unconfigured
    // source is silence that looks like health.
    expect(Array.isArray(body.coverage?.not_configured)).toBeTruthy()
    // The pre-rendered text exists so a Slack paste and the UI cannot disagree.
    expect(String(body.text)).toContain('Shift handover')

    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await page.getByTitle('Handover', { exact: true }).first().click()
    await expect(page.getByText('Coverage', { exact: true }).first()).toBeVisible({
      timeout: 20000,
    })
    await expect(page.getByText(body.headline, { exact: false }).first()).toBeVisible()
    await expect(page.getByRole('button', { name: /Copy as text/i })).toBeVisible()
    // Board-only content must be gone — proves a real view swap.
    await expect(page.getByText('Knowledge ledger')).toHaveCount(0)
  })

  test('Settings lists every provider adapter', async ({ page }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await page.getByTitle('Settings', { exact: true }).first().click()
    await expect(page.getByText('Autonomy').first()).toBeVisible({ timeout: 15000 })

    for (const name of ['AWS CloudWatch', 'PagerDuty', 'Datadog', 'GitHub Issues']) {
      await expect(page.getByText(name, { exact: true }).first()).toBeVisible({ timeout: 15000 })
    }
  })

  test('Slack output: toggle reveals a channel field and NO token field', async ({
    page,
    request,
  }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await page.getByTitle('Settings', { exact: true }).first().click()
    await expect(page.getByText('Autonomy').first()).toBeVisible({ timeout: 15000 })

    const toggle = page.getByRole('switch', { name: /Mirror incidents to Slack/i }).first()
    await expect(toggle).toBeVisible({ timeout: 15000 })
    if ((await toggle.getAttribute('aria-checked')) !== 'true') {
      await toggle.click()
    }

    // The channel field appears. Assert it is NOT masked rather than that
    // type === "text": the shared Input sets no explicit type, so the attribute is
    // absent (defaulting to text) and asserting the literal would fail for a
    // reason that has nothing to do with this feature.
    const channel = page.locator('#omc-slack-channel')
    await expect(channel).toBeVisible({ timeout: 15000 })
    await expect(channel).not.toHaveAttribute('type', 'password')

    // ...and there is NO password/secret input anywhere in this card, because the
    // app reuses KiroCrew's Slack client and stores no token of its own. If a
    // future change adds one, this fails — which is the point.
    const card = page.locator('div').filter({ hasText: 'Mirror incidents to Slack' }).last()
    await expect(card.locator('input[type="password"]')).toHaveCount(0)

    await expect
      .poll(async () => (await (await request.get(`${API}/state`)).json()).slack?.enabled, {
        timeout: 15000,
      })
      .toBe(true)

    // Leave it off: this spec must not arm an output channel on a real gateway.
    await request.put(`${API}/settings`, { data: { slack_enabled: false } })
  })

  test('Slack status names the missing piece rather than failing silently', async ({
    page,
    request,
  }) => {
    // Enabled with no channel is the half-configured state a user lands in.
    await request.put(`${API}/settings`, {
      data: { slack_enabled: true, slack_channel: '' },
    })
    const state = await (await request.get(`${API}/state`)).json()
    expect(state.slack?.ready).toBe(false)
    // The detail string must be actionable, not just "not ready".
    expect(String(state.slack?.detail ?? '')).toMatch(/channel|Slack/i)

    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await page.getByTitle('Settings', { exact: true }).first().click()
    await expect(page.getByText(/needs setup/i).first()).toBeVisible({ timeout: 15000 })

    await request.put(`${API}/settings`, { data: { slack_enabled: false } })
  })

  test('autonomy mode: observe → propose → act, each persisted', async ({ page, request }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await page.getByTitle('Settings', { exact: true }).first().click()
    await expect(page.getByText('Autonomy').first()).toBeVisible({ timeout: 15000 })

    // Propose.
    await page.getByTitle('Propose', { exact: true }).first().click()
    await expect(page.getByText('drafts the acknowledge', { exact: false })).toBeVisible({
      timeout: 15000,
    })
    await expect
      .poll(async () => (await (await request.get(`${API}/rotation`)).json()).mode, {
        timeout: 15000,
      })
      .toBe('propose')

    // Act — must surface the warning that it does nothing without a rule.
    await page.getByTitle('Act', { exact: true }).first().click()
    await expect(page.getByText('matched by a rule you have written', { exact: false })).toBeVisible(
      { timeout: 15000 },
    )
    await expect
      .poll(async () => (await (await request.get(`${API}/rotation`)).json()).mode, {
        timeout: 15000,
      })
      .toBe('act')

    // Back to the safe default, so this spec leaves no live write authority.
    await page.getByTitle('Observe', { exact: true }).first().click()
    await expect
      .poll(async () => (await (await request.get(`${API}/rotation`)).json()).mode, {
        timeout: 15000,
      })
      .toBe('observe')
  })

  test('enabling a provider reveals its config fields and persists', async ({ page, request }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await page.getByTitle('Settings', { exact: true }).first().click()
    await expect(page.getByText('AWS CloudWatch').first()).toBeVisible({ timeout: 15000 })

    // The CloudWatch toggle is labelled for a11y — use that, not nth-child.
    const toggle = page.getByRole('switch', { name: /Enable AWS CloudWatch/i }).first()
    if ((await toggle.getAttribute('aria-checked')) !== 'true') {
      await toggle.click()
    }
    // Config fields appear only when enabled.
    await expect(page.getByText('region', { exact: true }).first()).toBeVisible({ timeout: 15000 })

    await expect
      .poll(
        async () => {
          const providers = (await (await request.get(`${API}/providers`)).json()).providers ?? []
          return providers.find((p: { id: string }) => p.id === 'cloudwatch')?.config?.enabled
        },
        { timeout: 15000 },
      )
      .toBeTruthy()
  })

  test('secret fields are write-only: never pre-filled with a stored value', async ({ page }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await page.getByTitle('Settings', { exact: true }).first().click()
    await expect(page.getByText('PagerDuty').first()).toBeVisible({ timeout: 15000 })

    const toggle = page.getByRole('switch', { name: /Enable PagerDuty/i }).first()
    if ((await toggle.getAttribute('aria-checked')) !== 'true') {
      await toggle.click()
    }

    // The token input must be a password field and must be EMPTY — the API
    // cannot return a stored secret, so a pre-filled value would mean the UI
    // round-tripped one.
    const secret = page.locator(`#omc-pagerduty-secret-api_token`)
    await expect(secret).toBeVisible({ timeout: 15000 })
    await expect(secret).toHaveAttribute('type', 'password')
    await expect(secret).toHaveValue('')
  })

  test('Check now runs a dispatch cycle and reports the outcome', async ({ page }) => {
    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('button', { name: /Check now/i })).toBeVisible({ timeout: 20000 })

    const dispatch = page.waitForResponse(
      (r) => r.url().includes(`${API}/dispatch`) && r.request().method() === 'POST',
      { timeout: 60000 },
    )
    await page.getByRole('button', { name: /Check now/i }).click()
    const res = await dispatch
    expect(res.status(), 'dispatch must not 403/500').toBe(200)

    const body = await res.json()
    // Contract the cron depends on to decide whether to stay silent.
    expect(body).toHaveProperty('changed')
    expect(body).toHaveProperty('claimed')
    expect(body).toHaveProperty('polled')
  })

  test('primary-instance toggle persists', async ({ page, request }) => {
    // Pin the starting state via the API instead of reading whatever a previous
    // test left behind: deriving the expectation from `aria-checked` raced the
    // in-flight mutation and made this flake in both directions.
    const put = await request.put(`${API}/settings`, { data: { primary_instance: true } })
    expect(put.ok()).toBeTruthy()

    await page.goto(ROUTE, { waitUntil: 'domcontentloaded' })
    await page.getByTitle('Settings', { exact: true }).first().click()
    await expect(page.getByText('Instance', { exact: true }).first()).toBeVisible({ timeout: 15000 })

    const toggle = page.getByRole('switch', { name: /nightly ledger maintenance/i }).first()
    // The UI must reflect the state we just set — this is the read half.
    await expect(toggle).toHaveAttribute('aria-checked', 'true', { timeout: 15000 })

    // ...and a click must persist the write half.
    await toggle.click()
    await expect
      .poll(async () => (await (await request.get(`${API}/rotation`)).json()).primary, {
        timeout: 15000,
      })
      .toBe(false)

    // Restore the default (a single-instance install should be primary).
    await request.put(`${API}/settings`, { data: { primary_instance: true } })
  })
})

// ── 5. Security surfaces hold in the browser ───────────────────────────────

test.describe('Ops Mission Control — security', () => {
  test('the providers API never returns a stored token', async ({ request }) => {
    await ensureEnabled(request)
    const res = await request.get(`${API}/providers`)
    expect(res.ok()).toBeTruthy()
    const raw = await res.text()
    // A real token shape must never appear in a read response.
    expect(raw).not.toMatch(/u\+[A-Za-z0-9_-]{18,}/)
  })

  test('the config route refuses a secret field', async ({ request }) => {
    await ensureEnabled(request)
    // data/config.json is served WITHOUT session auth, so a token written there
    // would sit behind nothing but the port.
    const res = await request.put(`${API}/providers/pagerduty/config`, {
      data: { api_token: 'u+ShouldNeverBeAccepted' },
    })
    expect(res.status(), 'secret on the config route must be refused').toBe(400)
    expect(await res.text()).toContain('secret field')
  })
})
