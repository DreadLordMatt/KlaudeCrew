import { BrandGlyph } from '../BrandIcon'
import kiroPowerUrl from './kiro-power.svg'

/**
 * Kiro Powers mark — Ghosty with the lightning bolt, matching the Powers icon
 * in the Kiro IDE activity bar.
 *
 * The artwork is the ghost-with-bolt brand mark (`kiro-power.svg`), which lucide
 * ships no glyph for. Rendering is delegated to the shared `BrandGlyph` span
 * (`components/BrandIcon.tsx`) — the sanctioned brand-mark path that every other
 * masked mark (GitHub, GitLab, the Kiro ghost nav mark) already routes through.
 * BrandGlyph paints the asset as a theme-aware CSS mask over `currentColor`, so
 * the mark inherits rail / hover / active colour on every theme, and owns the
 * single copy of the `url("…")` quoting fix (Vite inlines small SVGs as a
 * single-quoted `data:` URI that breaks an unquoted `url(...)` token).
 */
export default function PowerIcon({ size = 16, className = 'inline-block shrink-0' }: { size?: number; className?: string }) {
  return <BrandGlyph url={kiroPowerUrl} size={size} className={className} testId="kiro-power" />
}
