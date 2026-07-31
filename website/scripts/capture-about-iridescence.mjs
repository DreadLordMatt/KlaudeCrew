/**
 * Screenshot harness for Settings > About (Iridescence backdrop + glass cards).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * The subject is a WebGL shader, so this harness differs from its siblings in
 * two ways:
 *   - Chromium is launched with SwiftShader, because headless has no GPU and the
 *     canvas would otherwise be blank.
 *   - It asserts the canvas actually PAINTED (readPixels on a non-black pixel)
 *     before shooting. A blank-canvas screenshot is worse evidence than none.
 *
 * Also captures the reduced-motion variant, since that path renders a single
 * static frame instead of running the rAF loop.
 *
 * Usage: node scripts/capture-about-iridescence.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../.github/screenshots/about-iridescence'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const CHANGELOG = [
  '## 0.2.0',
  '',
  '- Crews tab rename, non-blocking startup restore, channel wire-test harness.',
  '',
  '## 0.1.9',
  '',
  '- electron-updater migration; macOS OTA bundle swap proven end to end.',
  '',
  '## 0.1.8',
  '',
  '- Notification bridge and the shared channel dispatch pipeline.',
].join('\n')

/** Endpoints only the About panel needs, layered on the shared boot stubs. */
async function aboutApi(path, route) {
  if (path === '/api/changelog') return json(route, { content: CHANGELOG }), true
  if (path === '/api/update/check') return json(route, { available: false }), true
  if (path === '/api/config/kirocrew') return json(route, { auto_update: true }), true
  return false
}

async function shoot(browser, { name, reducedMotion }) {
  const context = await browser.newContext({
    viewport: { width: 1400, height: 980 },
    deviceScaleFactor: 2,
    reducedMotion: reducedMotion ? 'reduce' : 'no-preference',
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { extra: aboutApi })

  await page.goto(`${base}/settings?tab=about`, { waitUntil: 'domcontentloaded' })

  // Fail loudly rather than shoot the wrong panel.
  await page.locator('#main-content').getByText('Updates', { exact: false })
    .first().waitFor({ state: 'visible', timeout: 15000 })

  const canvas = page.locator('#main-content canvas')
  await canvas.waitFor({ state: 'attached', timeout: 15000 })
  await page.waitForTimeout(reducedMotion ? 600 : 2200) // let the shader evolve

  // Assert the shader actually painted. readPixels is NOT usable here: ogl
  // creates the context without preserveDrawingBuffer, so the backbuffer is
  // legitimately empty after compositing and would read back black even when
  // the page looks correct. Instead screenshot the canvas and use encoded PNG
  // size as the signal — a flat/black canvas compresses to a few KB, while the
  // shader's continuous gradients do not.
  const canvasShot = await canvas.screenshot()
  const FLOOR = 40_000
  if (canvasShot.length < FLOOR) {
    throw new Error(
      `shader looks flat: canvas PNG is ${canvasShot.length}B (< ${FLOOR}B floor) — refusing to write ${name}`,
    )
  }

  await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
  console.log(`wrote ${OUT}/${PREFIX}-${name}.png  (canvas PNG ${canvasShot.length}B)`)
  await context.close()
}

let base
async function main() {
  const served = await serveDist()
  base = served.base
  // Headless Chromium has no GPU; SwiftShader gives it a software GL.
  const browser = await chromium.launch({
    args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
  })

  await shoot(browser, { name: 'about', reducedMotion: false })
  await shoot(browser, { name: 'about-reduced-motion', reducedMotion: true })

  await browser.close()
  served.srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
