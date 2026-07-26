import { describe, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import DiffView from '../components/UnifiedDiffView'

describe('UnifiedDiffView', () => {
  it('renders parsed rows with line numbers after the deferred mount', async () => {
    render(<DiffView patch={'@@ -1,2 +1,2 @@\n context\n-old\n+new'} path="a.ts" />)
    await waitFor(() => expect(screen.getByText('new')).toBeInTheDocument())
    expect(screen.getByText('old')).toBeInTheDocument()
    expect(screen.getByText('context')).toBeInTheDocument()
  })

  it('renders the preamble for hunk-less patches (binary/mode-only)', async () => {
    render(<DiffView patch={'Binary files a/img.png and b/img.png differ'} path="img.png" />)
    await waitFor(() => expect(screen.getByText(/Binary files .* differ/)).toBeInTheDocument())
  })

  it('renders an enormous single line without highlighting it', async () => {
    // The ROW cap does not bound line LENGTH; hljs on a minified megabyte-long
    // line would block the main thread. Over the bound the text still renders.
    const huge = 'a'.repeat(50_000)
    render(<DiffView patch={`@@ -0,0 +1 @@\n+${huge}`} path="bundle.js" />)
    await waitFor(() => expect(screen.getByText(huge)).toBeInTheDocument())
    // Plain text, not an hljs-highlighted span.
    expect(screen.getByText(huge).className).not.toContain('hljs')
  })

  it('caps rendered rows and says the remainder was cut', async () => {
    const adds = Array.from({ length: 2500 }, (_, i) => `+line ${i}`).join('\n')
    render(<DiffView patch={`@@ -0,0 +1,2500 @@\n${adds}`} path="big.txt" />)
    await waitFor(() => expect(screen.getByText(/Diff display capped at 2,000 lines/)).toBeInTheDocument())
    expect(screen.getByText('line 0')).toBeInTheDocument()
    expect(screen.queryByText('line 2400')).toBeNull()
  })
})
