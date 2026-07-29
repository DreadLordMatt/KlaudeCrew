import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker, { __resetNativeProbeForTests } from '../components/ProjectPicker'
import { api } from '../api/client'
import { useRef } from 'react'

type BrowseDirsResult = Awaited<ReturnType<typeof api.browseDirs>>

const mockBrowseDirs = (path = '/home/u', dirs: { name: string; path: string }[] = []): BrowseDirsResult =>
  ({ path, parent: '/home', dirs })

beforeEach(() => {
  // The probe answer is cached for the page session in production; drop it so
  // each case starts from an unknown host.
  __resetNativeProbeForTests()
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/projA', '/home/u/projB'] })
  vi.spyOn(api, 'browseDirs').mockResolvedValue(mockBrowseDirs())
  // Default to "no native dialog on this host" so the server-side browser is the
  // surface under test; the native-dialog suite below opts in explicitly.
  vi.spyOn(api, 'nativeDirDialogAvailable').mockResolvedValue({ available: false })
})

afterEach(() => {
  vi.restoreAllMocks()
})

// Helper: build a DOMRect-shaped object (jsdom doesn't expose DOMRect directly).
const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height,
  bottom: top + height,
  right: left + width,
  x: left, y: top,
  toJSON: () => ({}),
} as DOMRect)

describe('ProjectPicker', () => {
  describe('visibility', () => {
    it('renders nothing when open is false', () => {
      const { container } = renderWithProviders(
        <ProjectPicker open={false} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(container.textContent).toBe('')
      expect(screen.queryByText('Recent')).not.toBeInTheDocument()
    })

    it('renders nothing when open but no anchor (rect or ref) is provided', () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} onSelect={vi.fn()} />
      )
      expect(screen.queryByText('Recent')).not.toBeInTheDocument()
    })

    it('renders tabs and Recent panel when open with anchorRect', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Recent')).toBeInTheDocument()
      expect(screen.getByText('Browse')).toBeInTheDocument()
    })
  })

  describe('anchorRect positioning', () => {
    it('positions below the anchor when in upper viewport half (no flip)', async () => {
      // Anchor near top of a 768-tall viewport; bottom = 124 < 384 (half)
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 200)} onSelect={vi.fn()} />
      )
      const drop = (await screen.findByText('Recent')).closest('div.fixed') as HTMLElement
      expect(drop).toBeTruthy()
      // top = anchorR.bottom (124) + 4 = 128
      expect(drop.style.top).toBe('128px')
      expect(drop.style.bottom).toBe('')
    })

    it('flips upward when anchor is in lower viewport half', async () => {
      // Viewport 768 tall, anchor at top=600 → bottom=624 > 384 → flipUp
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(600, 200)} onSelect={vi.fn()} />
      )
      const drop = (await screen.findByText('Recent')).closest('div.fixed') as HTMLElement
      expect(drop).toBeTruthy()
      // bottom = innerHeight - anchorR.top + 4 = 768 - 600 + 4 = 172
      expect(drop.style.bottom).toBe('172px')
      expect(drop.style.top).toBe('')
    })

    it('clamps left position to keep dropdown inside viewport', async () => {
      // Anchor at right edge: innerWidth=1280, anchorR.right=1278 → left = min(1278-400, 1280-408) = 872
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(50, 1198, 80, 24)} onSelect={vi.fn()} />
      )
      const drop = (await screen.findByText('Recent')).closest('div.fixed') as HTMLElement
      expect(parseInt(drop.style.left)).toBeLessThanOrEqual(872)
      expect(parseInt(drop.style.left)).toBeGreaterThanOrEqual(8)
    })

    it('clamps left position to minimum 8px when anchor is far left', async () => {
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(50, 0, 20)} onSelect={vi.fn()} />
      )
      const drop = (await screen.findByText('Recent')).closest('div.fixed') as HTMLElement
      // anchorR.right = 20 → 20 - 400 = -380 → Math.max(8, ...) = 8
      expect(drop.style.left).toBe('8px')
    })
  })

  describe('anchorRef fallback', () => {
    function PickerWithRef({ onSelect = vi.fn() }: { onSelect?: (p: string) => void }) {
      const ref = useRef<HTMLButtonElement>(null)
      return (
        <>
          <button ref={ref} data-testid="anchor-btn">Anchor</button>
          <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRef={ref} onSelect={onSelect} />
        </>
      )
    }

    it('falls back to anchorRef.getBoundingClientRect when anchorRect is absent', async () => {
      renderWithProviders(<PickerWithRef />)
      // jsdom returns a 0,0,0,0 rect by default but it's still a valid DOMRect → component renders
      expect(await screen.findByText('Recent')).toBeInTheDocument()
    })

    it('prefers live anchorRef.getBoundingClientRect over anchorRect when both are provided', async () => {
      function Both() {
        const ref = useRef<HTMLButtonElement>(null)
        return (
          <>
            <button ref={ref}>Anchor</button>
            <ProjectPicker
              open={true}
              onOpenChange={vi.fn()}
              anchorRef={ref}
              anchorRect={rect(100, 200)}
              onSelect={vi.fn()}
            />
          </>
        )
      }
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      renderWithProviders(<Both />)
      await screen.findByText('Recent')
      // Live ref measurement wins so layout shifts (scroll/resize/keyboard) stay accurate.
      // jsdom returns a 0,0,0,0 rect for the button → bottom=0 → top = 0 + 4 = 4,
      // NOT the captured anchorRect's 124 + 4 = 128. The ref attaches after the
      // first paint, so wait for the post-mount re-render to settle the value.
      await waitFor(() => {
        const drop = screen.getByText('Recent').closest('div.fixed') as HTMLElement
        expect(drop.style.top).toBe('4px')
      })
    })
  })

  describe('outside-click behavior', () => {
    it('closes when mousedown lands outside both dropdown and anchor', async () => {
      const onOpenChange = vi.fn()
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 200)} onSelect={vi.fn()} />
      )
      await screen.findByText('Recent')
      // Tick the timer so the listener is registered
      await act(async () => { await Promise.resolve() })
      // Click well outside (clientX=0, clientY=0 is not inside anchorRect or dropdown)
      const evt = new MouseEvent('mousedown', { clientX: 0, clientY: 0, bubbles: true })
      document.dispatchEvent(evt)
      await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    })

    it('does NOT close when mousedown is inside the anchor rect (rect hit-test)', async () => {
      const onOpenChange = vi.fn()
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 200, 80, 24)} onSelect={vi.fn()} />
      )
      await screen.findByText('Recent')
      await act(async () => { await Promise.resolve() })
      // Click inside anchor rect: x in [200,280], y in [100,124]
      const evt = new MouseEvent('mousedown', { clientX: 240, clientY: 110, bubbles: true })
      document.dispatchEvent(evt)
      // Give it a moment to (not) fire
      await act(async () => { await Promise.resolve() })
      expect(onOpenChange).not.toHaveBeenCalled()
    })

    it('does NOT close when mousedown is inside the dropdown panel itself', async () => {
      const onOpenChange = vi.fn()
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 200)} onSelect={vi.fn()} />
      )
      const recentTab = await screen.findByText('Recent')
      await act(async () => { await Promise.resolve() })
      fireEvent.mouseDown(recentTab)
      expect(onOpenChange).not.toHaveBeenCalled()
    })
  })

  describe('selection', () => {
    it('renders recent projects from api.recentProjects', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('projA')).toBeInTheDocument()
      expect(screen.getByText('projB')).toBeInTheDocument()
    })

    it('calls onSelect and onOpenChange(false) when clicking a recent entry', async () => {
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      const item = await screen.findByText('projA')
      fireEvent.mouseDown(item)
      expect(onSelect).toHaveBeenCalledWith('/home/u/projA')
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })

    it('shows "No recent projects" when user switches to Recent tab with empty list', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // Empty list auto-switches to Browse, so click Recent to land on the empty state
      const recentTab = await screen.findByText('Recent')
      fireEvent.mouseDown(recentTab)
      expect(await screen.findByText('No recent projects')).toBeInTheDocument()
    })

    it('switches to Browse tab when no recent projects exist', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', [
        { name: 'workplace', path: '/home/u/workplace' },
      ]))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // Browse panel shows the directory listing
      expect(await screen.findByText('workplace')).toBeInTheDocument()
    })

    it('selects typed path on Enter in Browse tab', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      const input = await screen.findByPlaceholderText('/path/to/project')
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(onSelect).toHaveBeenCalledWith('/home/u/typed')
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })

    it('closes on Escape in Browse tab without calling onSelect', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      const input = await screen.findByPlaceholderText('/path/to/project')
      fireEvent.keyDown(input, { key: 'Escape' })
      expect(onSelect).not.toHaveBeenCalled()
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
  })

  describe('keyboard navigation', () => {
    it('Recent tab: ArrowDown moves the highlight and Enter selects', async () => {
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      await screen.findByText('projA')
      const optA = screen.getByText('projA').closest('[role="option"]') as HTMLElement
      const optB = screen.getByText('projB').closest('[role="option"]') as HTMLElement
      // First option highlighted by default.
      expect(optA).toHaveAttribute('aria-selected', 'true')
      // The Recent tab listens at the document level (no input to focus).
      fireEvent.keyDown(document, { key: 'ArrowDown' })
      await waitFor(() => expect(optB).toHaveAttribute('aria-selected', 'true'))
      fireEvent.keyDown(document, { key: 'Enter' })
      expect(onSelect).toHaveBeenCalledWith('/home/u/projB')
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })

    it('Browse tab: ArrowDown highlights a subdir and Enter drills into it', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      const browseSpy = vi.mocked(api.browseDirs)
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u', [
        { name: 'alpha', path: '/home/u/alpha' },
        { name: 'beta', path: '/home/u/beta' },
      ]))
      const onSelect = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      const input = await screen.findByPlaceholderText('/path/to/project')
      await screen.findByText('beta')
      browseSpy.mockClear()
      fireEvent.keyDown(input, { key: 'ArrowDown' }) // highlight index 1 (beta)
      fireEvent.keyDown(input, { key: 'Enter' })     // Enter drills into the highlighted folder
      await waitFor(() => expect(browseSpy).toHaveBeenCalledWith('/home/u/beta'))
      expect(onSelect).not.toHaveBeenCalled()         // drilling, not committing
    })

    it('Browse tab: Cmd+Enter commits the current directory', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', [
        { name: 'alpha', path: '/home/u/alpha' },
      ]))
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      const input = await screen.findByPlaceholderText('/path/to/project')
      await screen.findByText('alpha')
      fireEvent.keyDown(input, { key: 'Enter', metaKey: true }) // commit current dir, no drill
      expect(onSelect).toHaveBeenCalledWith('/home/u')
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
  })

  describe('Recent tab search', () => {
    it('renders a search box only when there are recent projects', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // Recent projects exist (projA/projB from the default beforeEach mock).
      expect(await screen.findByPlaceholderText('Search recent projects…')).toBeInTheDocument()
    })

    it('does NOT render the search box when there are no recent projects', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // Empty list lands on Browse; switch to Recent and confirm no search box.
      const recentTab = await screen.findByText('Recent')
      fireEvent.mouseDown(recentTab)
      await screen.findByText('No recent projects')
      expect(screen.queryByPlaceholderText('Search recent projects…')).not.toBeInTheDocument()
    })

    it('filters the recent list by case-insensitive substring on the full path', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await screen.findByText('projA')
      const searchBox = screen.getByPlaceholderText('Search recent projects…')
      // 'proja' (lowercase) matches '/home/u/projA' but not '/home/u/projB'.
      fireEvent.change(searchBox, { target: { value: 'proja' } })
      await waitFor(() => expect(screen.queryByText('projB')).not.toBeInTheDocument())
      expect(screen.getByText('projA')).toBeInTheDocument()
    })

    it('shows "No matching projects" when the query matches nothing', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await screen.findByText('projA')
      const searchBox = screen.getByPlaceholderText('Search recent projects…')
      fireEvent.change(searchBox, { target: { value: 'zzz-no-match' } })
      expect(await screen.findByText('No matching projects')).toBeInTheDocument()
    })

    it('keyboard nav + Enter selects from the filtered list, not the full list', async () => {
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      await screen.findByText('projA')
      const searchBox = screen.getByPlaceholderText('Search recent projects…')
      // Narrow to just projB. The document-level nav hook now sees count=1.
      fireEvent.change(searchBox, { target: { value: 'projb' } })
      await waitFor(() => expect(screen.queryByText('projA')).not.toBeInTheDocument())
      // Index 0 of the filtered list is projB; Enter selects it.
      fireEvent.keyDown(document, { key: 'Enter' })
      expect(onSelect).toHaveBeenCalledWith('/home/u/projB')
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
  })

  describe('Browse tab trailing-slash auto-drill', () => {
    beforeEach(() => {
      vi.useFakeTimers()
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
    })
    afterEach(() => {
      vi.runOnlyPendingTimers()
      vi.useRealTimers()
    })

    it('drills into the typed directory when the input ends with a slash', async () => {
      const browseSpy = vi.mocked(api.browseDirs)
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // Drain the initial browse() + recentProjects() promises.
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const input = screen.getByPlaceholderText('/path/to/project')
      browseSpy.mockClear()
      fireEvent.change(input, { target: { value: '/home/u/workplace/' } })
      // Debounce is 250ms; nothing should fire before it elapses.
      expect(browseSpy).not.toHaveBeenCalled()
      await act(async () => { await vi.advanceTimersByTimeAsync(250) })
      // Trailing slash is stripped to the target dir for the API call. The
      // preserveInput flag is internal to browse() and is NOT forwarded to
      // api.browseDirs (a network call that only takes a path), so the spy
      // sees just the path. Slash preservation is asserted in the next test.
      expect(browseSpy).toHaveBeenCalledWith('/home/u/workplace')
    })

    it('preserves the typed trailing slash in the input after the drill resolves', async () => {
      const browseSpy = vi.mocked(api.browseDirs)
      // Initial mount resolves to /home/u so the drill target (/home/u/workplace)
      // differs from browsePath — otherwise the `target === browsePath` guard
      // early-returns and the drill never fires (making the assertion trivial).
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const input = screen.getByPlaceholderText('/path/to/project') as HTMLInputElement
      // The drill response resolves with a canonical path WITHOUT the trailing slash.
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u/workplace', []))
      fireEvent.change(input, { target: { value: '/home/u/workplace/' } })
      await act(async () => { await vi.advanceTimersByTimeAsync(250) })
      // The drill fired (target differed from browsePath)...
      expect(browseSpy).toHaveBeenCalledWith('/home/u/workplace')
      // ...but preserveInput=true means setInput is NOT called, so the user's
      // text (including the trailing slash they just typed) is retained.
      expect(input.value).toBe('/home/u/workplace/')
    })

    it('does NOT auto-drill for a non-slash-terminated path', async () => {
      const browseSpy = vi.mocked(api.browseDirs)
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const input = screen.getByPlaceholderText('/path/to/project')
      browseSpy.mockClear()
      fireEvent.change(input, { target: { value: '/home/u/workpla' } })
      await act(async () => { await vi.advanceTimersByTimeAsync(300) })
      expect(browseSpy).not.toHaveBeenCalled()
    })

    it('does NOT re-drill when the slash target equals the already-loaded dir', async () => {
      const browseSpy = vi.mocked(api.browseDirs)
      // browsePath is '/home/u' after the initial load.
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const input = screen.getByPlaceholderText('/path/to/project')
      browseSpy.mockClear()
      // Typing '/home/u/' strips to '/home/u' which equals browsePath → no-op.
      fireEvent.change(input, { target: { value: '/home/u/' } })
      await act(async () => { await vi.advanceTimersByTimeAsync(300) })
      expect(browseSpy).not.toHaveBeenCalled()
    })
  })

  describe('native folder dialog', () => {
    const nativeAvailable = () => {
      vi.mocked(api.nativeDirDialogAvailable).mockResolvedValue({ available: true })
    }

    it('replaces the Browse tab with a Choose folder action when the host can open one', async () => {
      nativeAvailable()
      const openSpy = vi.spyOn(api, 'openNativeDirDialog').mockResolvedValue({ cancelled: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Choose folder…')).toBeInTheDocument()
      // One browsing surface only — the tab strip is gone.
      expect(screen.queryByText('Browse')).not.toBeInTheDocument()
      expect(screen.queryByText('Recent')).not.toBeInTheDocument()
      // Recents remain reachable: they are what a native dialog cannot offer.
      expect(screen.getByText('/home/u/projA')).toBeInTheDocument()
      expect(openSpy).not.toHaveBeenCalled()
    })

    it('keeps the server-side browser when the host cannot open a dialog', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Browse')).toBeInTheDocument()
      expect(screen.queryByText('Choose folder…')).not.toBeInTheDocument()
    })

    it('selects the picked directory and closes', async () => {
      nativeAvailable()
      vi.spyOn(api, 'openNativeDirDialog').mockResolvedValue({ path: '/home/u/picked' })
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      fireEvent.click(await screen.findByText('Choose folder…'))
      await waitFor(() => expect(onSelect).toHaveBeenCalledWith('/home/u/picked'))
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })

    it('sends no path — the gateway decides where the dialog opens', async () => {
      nativeAvailable()
      const openSpy = vi.spyOn(api, 'openNativeDirDialog').mockResolvedValue({ cancelled: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      fireEvent.click(await screen.findByText('Choose folder…'))
      await waitFor(() => expect(openSpy).toHaveBeenCalledWith())
    })

    it('shows a waiting state while the OS dialog is up', async () => {
      nativeAvailable()
      let resolve: (v: { cancelled: boolean }) => void = () => {}
      vi.spyOn(api, 'openNativeDirDialog').mockReturnValue(
        new Promise(r => { resolve = r as (v: { cancelled: boolean }) => void })
      )
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      fireEvent.click(await screen.findByText('Choose folder…'))
      const waiting = await screen.findByText('A folder window opened — choose a folder there')
      expect(waiting.closest('button')).toBeDisabled()
      await act(async () => { resolve({ cancelled: true }); await Promise.resolve() })
      expect(await screen.findByText('Choose folder…')).toBeInTheDocument()
    })

    it('stays open on cancel without selecting anything', async () => {
      nativeAvailable()
      vi.spyOn(api, 'openNativeDirDialog').mockResolvedValue({ cancelled: true })
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      fireEvent.click(await screen.findByText('Choose folder…'))
      await waitFor(() => expect(screen.getByText('Choose folder…')).toBeInTheDocument())
      expect(onSelect).not.toHaveBeenCalled()
      expect(onOpenChange).not.toHaveBeenCalled()
    })

    it('falls back to the server-side browser when the dialog fails', async () => {
      // A host that advertised a dialog can still fail to draw one (no GUI
      // session behind an ssh -L forward) — the user must not be left stranded.
      nativeAvailable()
      vi.spyOn(api, 'openNativeDirDialog').mockRejectedValue(new Error('503'))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      fireEvent.click(await screen.findByText('Choose folder…'))
      expect(await screen.findByText(/Couldn't open the folder window/)).toBeInTheDocument()
      expect(screen.getByText('Browse')).toBeInTheDocument()
      expect(screen.getByPlaceholderText('/path/to/project')).toBeInTheDocument()
      expect(screen.queryByText('Choose folder…')).not.toBeInTheDocument()
    })

    it('falls back when the availability probe itself fails', async () => {
      vi.mocked(api.nativeDirDialogAvailable).mockRejectedValue(new Error('offline'))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Browse')).toBeInTheDocument()
      expect(screen.queryByText('Choose folder…')).not.toBeInTheDocument()
    })

    it('probes the host once per page, not once per open', async () => {
      // Host capability cannot change under us; re-asking only costs a
      // round-trip, which is what makes the panel feel slow over a tunnel.
      nativeAvailable()
      const probeSpy = vi.mocked(api.nativeDirDialogAvailable)
      const { rerender } = renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await screen.findByText('Choose folder…')
      rerender(<ProjectPicker open={false} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />)
      rerender(<ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />)
      await screen.findByText('Choose folder…')
      expect(probeSpy).toHaveBeenCalledTimes(1)
    })

    it('does not list the server-side tree that native mode never shows', async () => {
      nativeAvailable()
      const browseSpy = vi.mocked(api.browseDirs)
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await screen.findByText('Choose folder…')
      expect(browseSpy).not.toHaveBeenCalled()
    })

    it('lists the tree lazily once the dialog fails and the fallback appears', async () => {
      nativeAvailable()
      vi.spyOn(api, 'openNativeDirDialog').mockRejectedValue(new Error('503'))
      const browseSpy = vi.mocked(api.browseDirs)
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      fireEvent.click(await screen.findByText('Choose folder…'))
      await waitFor(() => expect(browseSpy).toHaveBeenCalled())
      expect(await screen.findByPlaceholderText('/path/to/project')).toBeInTheDocument()
    })
  })

  describe('warmed data', () => {
    it('fetches recents and the tree once across repeated opens', async () => {
      // The panel paints in ~40ms but its lists used to wait a full round-trip
      // on the FIRST open — ~830ms at 400ms RTT. Warming once per page removes
      // that from the perceived open latency.
      const recentSpy = vi.mocked(api.recentProjects)
      const browseSpy = vi.mocked(api.browseDirs)
      const { rerender } = renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await screen.findByText('/home/u/projA')
      for (const open of [false, true, false, true]) {
        rerender(<ProjectPicker open={open} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />)
        await act(async () => { await Promise.resolve() })
      }
      await screen.findByText('/home/u/projA')
      expect(recentSpy).toHaveBeenCalledTimes(1)
      expect(browseSpy).toHaveBeenCalledTimes(1)
    })

    it('still populates when the warm has not run yet', async () => {
      // Clicking before the idle callback fires must not show an empty panel.
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('/home/u/projA')).toBeInTheDocument()
    })

    it('survives a failed warm without breaking the panel', async () => {
      vi.mocked(api.recentProjects).mockRejectedValue(new Error('offline'))
      vi.mocked(api.browseDirs).mockRejectedValue(new Error('offline'))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Browse')).toBeInTheDocument()
      // Empty recents means the panel opens on Browse, which still works.
      expect(screen.getByPlaceholderText('/path/to/project')).toBeInTheDocument()
    })
  })
})
