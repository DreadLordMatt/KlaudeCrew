/**
 * Screenshot harness for the per-turn stats footer's new token-count segment.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with
 * every /api/** call intercepted by Playwright and answered from fixtures. No
 * gateway, no kiro-cli, no live backend — the footer reads turnStats straight
 * off the message's meta.turn_stats, which _attach_turn_stats (chat_runner.py)
 * populates, so the fixture message below mirrors that exact shape.
 *
 * Frames:
 *   01-cost-and-tokens   $ cost + token count + elapsed (the common claude_code case)
 *   02-tokens-only       token count + elapsed, nothing billed
 *   03-credits-only      unchanged pre-existing case (regression guard — no
 *                        token segment should appear when tokens is absent)
 *
 * Usage:
 *   npx vite preview --host 127.0.0.1 --port 6810 --strictPort   # in another shell
 *   node scripts/capture-turn-token-stats.mjs http://127.0.0.1:6810 ../temp-screenshots/turn-token-stats
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { json, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6810'
const OUT = process.argv[3] || '../temp-screenshots/turn-token-stats'
const SLOT = 'chat-token-stats'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const now = () => Date.now() / 1000

const slots = [{
  key: SLOT,
  title: 'Show token counts on the turn stats line',
  running: false,
  last_message: 'Show token counts on the turn stats line',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

function detailWith(turnStats) {
  return {
    running: false,
    has_more: false,
    total: 2,
    queue: [],
    project: PROJECT,
    messages: [
      { role: 'user', ts: now() - 30, content: 'Summarize the change in one line.' },
      {
        role: 'assistant',
        ts: now() - 10,
        content: 'Added a token count between the billed amount and the elapsed clock on the per-turn stats line.',
        meta: { turn_stats: turnStats },
      },
    ],
  }
}

const costAndTokens = detailWith({ elapsed_ms: 10_234, cost_usd: 0.16, tokens: 1_234 })
const tokensOnly = detailWith({ elapsed_ms: 6_800, tokens: 812 })
const creditsOnly = detailWith({ elapsed_ms: 84_000, credits: 2.5 })

const scene = { detail: costAndTokens, theme: 'dark' }

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1200, height: 500 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await stubDashboardApi(page, {
    slots,
    theme: scene.theme,
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) { json(route, scene.detail); return true }
      if (path === '/api/theme/boot') { json(route, { mode: scene.theme, theme: '' }); return true }
      if (path === '/api/recent-projects') { json(route, { dirs: [PROJECT] }); return true }
      if (path === '/api/chat/nav/resolve-links') { json(route, { summaries: [] }); return true }
      return false
    },
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  async function load(detail) {
    scene.detail = detail
    await page.addInitScript(() => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-token-stats')
    })
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-testid="turn-stats"]', { timeout: 10_000 })
    await page.waitForTimeout(300)
  }

  async function shot(name) {
    const stats = page.locator('[data-testid="turn-stats"]')
    const box = await stats.boundingBox()
    if (!box) { console.log('MISSING turn-stats element for', name); return }
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x: Math.max(0, box.x - 40), y: Math.max(0, box.y - 80), width: 520, height: box.height + 120 },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  await load(costAndTokens)
  await shot('01-cost-and-tokens')

  await load(tokensOnly)
  await shot('02-tokens-only')

  await load(creditsOnly)
  await shot('03-credits-only')

  await browser.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
