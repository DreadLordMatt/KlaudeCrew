/**
 * Powers surface registration.
 *
 * Powers moved out of the Agent Capabilities tab strip and into the left
 * rail's Apps group (the app-grid section), so the registration itself is now
 * the contract: navId, route, group, and app-only placement. Pinning it here
 * catches an accidental regroup or route rename that would silently drop the
 * surface out of the rail.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import '../surfaces/builtins'
import { getBuiltinSurface, getBuiltinSurfaces } from '../surfaces/registry'
import PowerIcon from '../components/icons/PowerIcon'
import { BrandGlyph } from '../components/BrandIcon'

describe('Powers surface', () => {
  it('is registered in the Apps group at /powers', () => {
    const surface = getBuiltinSurface('powers')
    expect(surface).toBeDefined()
    expect(surface?.route).toBe('/powers')
    expect(surface?.label).toBe('Powers')
    expect(surface?.group).toBe('Apps')
  })

  it('reaches the rail: included in getBuiltinSurfaces (not appOnly/hidden)', () => {
    // App.tsx builds NAV_ITEMS from getBuiltinSurfaces() and then filters
    // group === 'Apps'. Since that accessor drops `appOnly` and
    // `hiddenFromNav`, either flag would silently remove the row from the rail
    // — which is exactly the regression this pins.
    const surface = getBuiltinSurface('powers')
    expect(surface?.appOnly).toBeFalsy()
    expect(surface?.hiddenFromNav).toBeFalsy()
    const railApps = getBuiltinSurfaces().filter(s => s.group === 'Apps')
    expect(railApps.map(s => s.navId)).toContain('powers')
  })

  it('uses the upstream Kiro Powers bolt mark', () => {
    const surface = getBuiltinSurface('powers')
    // Guards against a silent swap back to a generic lucide glyph.
    expect(surface?.icon?.type).toBe(PowerIcon)
  })

  it('is no longer a Capabilities tab', async () => {
    const mod = await import('../pages/CapabilitiesPage')
    expect(mod.default).toBeDefined()
    const src = await import('../pages/CapabilitiesPage?raw')
    expect(String(src.default)).not.toContain("'powers'")
  })
})

describe('PowerIcon', () => {
  // React's own serialization: jsdom's cssstyle drops the `mask-*` longhands, so
  // assert the mask contract against renderToStaticMarkup (see KiroGhostMark test).
  const staticMarkup = (el: Parameters<typeof renderToStaticMarkup>[0]) => renderToStaticMarkup(el)

  it('renders through the shared BrandGlyph path (not a hand-rolled mask)', () => {
    // BrandGlyph is the ONLY masked-glyph span that stamps a data-testid; the
    // previous hand-rolled implementation set none. Its presence proves the
    // icon now routes through the sanctioned helper.
    const { getByTestId } = render(<PowerIcon />)
    const el = getByTestId('kiro-power')
    expect(el.getAttribute('aria-hidden')).toBe('true')
    // currentColor tint is BrandGlyph's theme-aware contract.
    expect(el.style.backgroundColor).toBe('currentcolor')
  })

  it('paints the ghost+bolt kiro-power asset as a quoted CSS mask', () => {
    const markup = staticMarkup(<PowerIcon />)
    const style = /style="([^"]*)"/.exec(markup)?.[1] ?? ''
    // Artwork preserved: the mask source is the ghost-with-bolt kiro-power svg,
    // NOT a lucide glyph substitution.
    expect(style).toContain('kiro-power')
    expect(style).toContain('mask-size:contain')
    // BrandGlyph's URL-quoting fix (unquoted url() breaks on Vite data: URIs).
    expect(style).toMatch(/mask-image:url\(&quot;/)
  })

  it('emits the same markup as BrandGlyph for the same asset (delegation, not duplication)', () => {
    // The kiro-power asset URL is resolved inside PowerIcon; recover it from the
    // rendered mask and feed it back through BrandGlyph — identical output proves
    // PowerIcon is a thin delegate, so any future BrandGlyph fix reaches it too.
    const markup = staticMarkup(<PowerIcon size={22} />)
    const url = /mask-image:url\(&quot;([^&]*)&quot;\)/.exec(markup)?.[1] ?? ''
    expect(url).not.toBe('')
    const viaHelper = staticMarkup(<BrandGlyph url={url} size={22} className="inline-block shrink-0" testId="kiro-power" />)
    expect(markup).toBe(viaHelper)
  })
})
