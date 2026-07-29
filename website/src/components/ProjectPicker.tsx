import { useState, useEffect, useRef, useCallback, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { FolderOpen, ChevronRight, ChevronLeft, Clock, Search } from 'lucide-react'
import { api } from '../api/client'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'

import { i18nT } from '../i18n/t'

/**
 * Whether the host can open a native folder dialog, resolved once per page.
 *
 * The answer is a property of the machine running the gateway, not of this
 * panel, so re-asking on every open only buys a round-trip — which is felt as
 * lag when the dashboard is reached over a tunnel or a slow link.
 */
let nativeProbe: Promise<boolean> | null = null
function probeNativeDialog(): Promise<boolean> {
  if (!nativeProbe) {
    // Defensive on two axes: the method may be absent (test fixtures mock the
    // api module partially) and the call may throw synchronously rather than
    // reject. Either way the answer is "no native dialog", which is the safe
    // default — the server-side browser still works.
    try {
      nativeProbe = Promise.resolve(api.nativeDirDialogAvailable?.())
        .then(d => !!d?.available)
        .catch(() => false)
    } catch {
      nativeProbe = Promise.resolve(false)
    }
  }
  return nativeProbe
}
/** Test seam: drop the cached answer so each case probes fresh. */
export function __resetNativeProbeForTests(): void {
  nativeProbe = null
  warmedRecents = null
  warmedBrowse = null
  warmScheduled = false
}

/**
 * Recents and the first directory listing, warmed after boot.
 *
 * The panel itself paints in ~40ms, but on the FIRST open its lists were empty
 * until a round-trip completed — ~220ms at 80ms RTT, ~830ms at 400ms, and over
 * a second on a loaded tunnel. Later opens were instant because the data stayed
 * in component state, so the cost fell entirely on the first use.
 *
 * Warming happens on an idle callback AFTER mount rather than during boot: the
 * boot path is already round-trip bound, and this data is not needed for first
 * paint. By the time the user reaches for the button it is in hand, and if they
 * click sooner the open still works — it just falls back to fetching, exactly as
 * before.
 */
type WarmDirs = Awaited<ReturnType<typeof api.browseDirs>>
let warmedRecents: Promise<string[]> | null = null
let warmedBrowse: Promise<WarmDirs | null> | null = null
let warmScheduled = false

function warmRecents(): Promise<string[]> {
  warmedRecents ??= Promise.resolve(api.recentProjects?.())
    .then(d => d?.dirs || [])
    .catch(() => [])
  return warmedRecents
}

function warmBrowse(): Promise<WarmDirs | null> {
  warmedBrowse ??= Promise.resolve(api.browseDirs?.())
    .then(d => d ?? null)
    .catch(() => null)
  return warmedBrowse
}

/** Schedule the warm once per page, off the boot critical path. */
function scheduleWarm(): void {
  if (warmScheduled) return
  warmScheduled = true
  const run = () => {
    void warmRecents()
    // Only the server-side browser needs a directory listing; a host with a
    // native dialog never renders the tree.
    void probeNativeDialog().then(ok => { if (!ok) void warmBrowse() })
  }
  const idle = (globalThis as { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => void })
    .requestIdleCallback
  if (typeof idle === 'function') idle(run, { timeout: 3000 })
  else setTimeout(run, 1500)
}

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  anchorRef?: RefObject<HTMLElement | null>
  anchorRect?: DOMRect | null
  onSelect: (path: string) => void
}

export default function ProjectPicker({ open, onOpenChange, anchorRef, anchorRect, onSelect }: Props) {
  const [tab, setTab] = useState<'recent' | 'browse'>('recent')
  const [input, setInput] = useState('')
  const [browsePath, setBrowsePath] = useState('')
  const [browseParent, setBrowseParent] = useState('')
  const [browseDirs, setBrowseDirs] = useState<{ name: string; path: string }[]>([])
  const [recentDirs, setRecentDirs] = useState<string[]>([])
  const [recentQuery, setRecentQuery] = useState('')
  const [browseSel, setBrowseSel] = useState(0)
  // Native folder dialog: `null` while the probe is in flight, then whether the
  // host can open one. `nativeError` is set when a dialog that was supposed to
  // work fails anyway (no GUI session behind an ssh -L forward, for instance) —
  // that re-exposes the server-side Browse tab as the fallback.
  const [nativeOk, setNativeOk] = useState<boolean | null>(null)
  const [nativePending, setNativePending] = useState(false)
  const [nativeError, setNativeError] = useState('')
  const btnRef = anchorRef
  const dropRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const recentSearchRef = useRef<HTMLInputElement>(null)
  const browseItemRefs = useRef<(HTMLElement | null)[]>([])
  const anchorRectRef = useRef<DOMRect | null>(anchorRect ?? null)
  anchorRectRef.current = anchorRect ?? null
  const getAnchorRect = useCallback((): DOMRect | null => {
    if (btnRef?.current && typeof btnRef.current.getBoundingClientRect === 'function') {
      return btnRef.current.getBoundingClientRect()
    }
    return anchorRectRef.current
  }, [btnRef])

  // Applying a listing is shared by the network path and the warmed cache, so
  // both land the panel in exactly the same state.
  const applyBrowse = useCallback((d: WarmDirs, preserveInput = false) => {
    setBrowsePath(d.path); setBrowseParent(d.parent); setBrowseDirs(d.dirs); setBrowseSel(0)
    if (!preserveInput) setInput(d.path)
    // Keep the combobox input focused so arrow/Enter nav continues after a drill.
    requestAnimationFrame(() => inputRef.current?.focus())
  }, [])

  const browse = useCallback((path?: string, preserveInput = false) => {
    api.browseDirs(path).then(d => applyBrowse(d, preserveInput)).catch(() => {})
  }, [applyBrowse])

  // Probe on MOUNT, not on open: the round-trip then overlaps with the user
  // deciding to click, so the first open already knows which chrome to draw
  // instead of rendering tabs and dropping them a moment later. The data warm
  // rides along, so the first open's lists are populated too.
  useEffect(() => {
    probeNativeDialog().then(setNativeOk)
    scheduleWarm()
  }, [])

  useEffect(() => {
    if (!open) return
    setRecentQuery('')
    setNativeError('')
    setNativePending(false)
    // Warmed values resolve immediately when the idle callback already ran, so
    // the list is populated in the same frame the panel paints.
    warmRecents().then(dirs => {
      setRecentDirs(dirs)
      setTab(dirs.length ? 'recent' : 'browse')
    })
    probeNativeDialog().then(ok => {
      setNativeOk(ok)
      // The server-side tree is only shown when there is no native dialog, so
      // listing it up front would be a round-trip nobody reads.
      if (!ok) warmBrowse().then(d => { if (d) applyBrowse(d) })
    })
  }, [open, applyBrowse])

  useEffect(() => {
    if (!open) return
    let cleanup = () => {}
    const timer = setTimeout(() => {
      const handler = (e: MouseEvent) => {
        if (dropRef.current && dropRef.current.contains(e.target as Node)) return
        const target = e.target as Node | null
        const live = btnRef?.current
        if (live && typeof (live as Element).contains === 'function' && (live as Element).contains(target)) return
        const r = getAnchorRect()
        if (r && e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return
        onOpenChange(false)
      }
      document.addEventListener('mousedown', handler)
      cleanup = () => document.removeEventListener('mousedown', handler)
    }, 0)
    return () => { clearTimeout(timer); cleanup() }
  }, [open, onOpenChange, btnRef, getAnchorRect])

  const select = (path: string) => { onSelect(path); onOpenChange(false) }

  // Ask the gateway to open the host's own folder dialog. The panel stays
  // mounted while the OS dialog is up (the answer arrives on this request), and
  // a failure degrades to the server-side browser rather than a dead end. Where
  // the dialog opens is the gateway's decision, not ours.
  const openNative = useCallback(() => {
    setNativeError('')
    setNativePending(true)
    api.openNativeDirDialog()
      .then(d => {
        setNativePending(false)
        if (d.path) select(d.path)
      })
      .catch(() => {
        setNativePending(false)
        setNativeOk(false)
        setNativeError(i18nT('components.projectPicker.native_dialog_failed'))
        setTab('browse')
        // The tree was never listed (native mode skips it), so fetch it now
        // that it is about to become the only way to pick a folder.
        browse()
      })
    // `select` closes over onSelect/onOpenChange, both stable for a mounted panel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [browse])

  const rq = recentQuery.trim().toLowerCase()
  const filteredRecent = rq ? recentDirs.filter(d => d.toLowerCase().includes(rq)) : recentDirs

  // Recent tab uses the shared selected-index keyboard nav (same model as the
  // Skill/File pickers). The Browse tab has its own combobox input handler
  // below, so the hook is only armed on Recent to avoid double-handling keys.
  const recentNav = useListKeyboardNav({
    open: open && tab === 'recent',
    count: filteredRecent.length,
    onChoose: i => { const d = filteredRecent[i]; if (d) select(d) },
    onClose: () => onOpenChange(false),
  })

  // Reset the Recent highlight whenever the filtered list changes.
  useEffect(() => { recentNav.setSelected(0) }, [recentQuery]) // eslint-disable-line react-hooks/exhaustive-deps

  // Reset the Browse highlight whenever the visible list changes (tab switch,
  // drill into a new dir, or filter edit).
  useEffect(() => { setBrowseSel(0) }, [tab, input, browsePath])

  // Auto-drill on a typed trailing slash. Without this, typing "/foo/bar/" only
  // filters the *current* directory's children by the last segment — the list
  // never descends into the typed subdirectory. When the input ends with "/"
  // (and differs from the dir we've already loaded), browse into it. Debounced
  // so intermediate keystrokes before the slash don't each fire a request.
  useEffect(() => {
    if (!open || tab !== 'browse') return
    const trimmed = input.trim()
    if (!trimmed.endsWith('/') || trimmed.length <= 1) return
    // Strip the trailing slash to get the target dir; skip if it's already loaded.
    const target = trimmed.replace(/\/+$/, '') || '/'
    if (target === browsePath) return
    const t = setTimeout(() => browse(target, true), 250)
    return () => clearTimeout(t)
  }, [input, open, tab, browsePath, browse])

  // Keep the highlighted Browse subdir scrolled into view.
  useEffect(() => {
    if (!open || tab !== 'browse') return
    const el = browseItemRefs.current[browseSel]
    if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' })
  }, [browseSel, open, tab])

  const anchorR = getAnchorRect()
  if (!open || !anchorR) return null

  const q = input.toLowerCase()
  const filteredBrowse = q && q !== browsePath.toLowerCase() ? browseDirs.filter(d => d.name.toLowerCase().includes(q.split('/').pop() || '') || d.path.toLowerCase().includes(q)) : browseDirs
  // With a native dialog on hand there is only one way to browse, so the tab
  // strip goes away and the panel is just: recents, plus "Choose folder…".
  const showTabs = nativeOk !== true
  const view = showTabs ? tab : 'recent'

  return createPortal(
    <div ref={dropRef} className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl w-[400px] flex flex-col overflow-hidden animate-slide-up" style={(() => {
      const dropMinH = 200
      const spaceBelow = window.innerHeight - anchorR.bottom - 8
      const flipUp = spaceBelow < dropMinH || anchorR.bottom > window.innerHeight / 2
      const left = Math.max(8, Math.min(anchorR.right - 400, window.innerWidth - 408))
      // With a native dialog and no recents there is nothing to scroll, so the
      // panel sizes to its content instead of reserving a tall empty box.
      const compact = nativeOk === true && recentDirs.length === 0
      if (flipUp) {
        const spaceAbove = anchorR.top - 8
        const h = Math.min(460, Math.max(200, spaceAbove))
        return compact
          ? { bottom: window.innerHeight - anchorR.top + 4, left, maxHeight: h }
          : { bottom: window.innerHeight - anchorR.top + 4, left, height: h }
      }
      const h = Math.min(460, Math.max(200, spaceBelow))
      return compact
        ? { top: anchorR.bottom + 4, left, maxHeight: h }
        : { top: anchorR.bottom + 4, left, height: h }
    })()}>
      {/* Tabs — only when the server-side browser is the browsing surface. */}
      {showTabs && (
      <div className="flex border-b border-border">
        <button className={`flex-1 px-3 py-2 text-[12px] font-medium flex items-center justify-center gap-1.5 transition-colors ${tab === 'recent' ? 'text-accent border-b-2 border-accent' : 'text-muted hover:text-text'}`} onMouseDown={e => { e.preventDefault(); setTab('recent') }}>
          <Clock size={12} /> {i18nT('components.projectPicker.recent')}
        </button>
        <button className={`flex-1 px-3 py-2 text-[12px] font-medium flex items-center justify-center gap-1.5 transition-colors ${tab === 'browse' ? 'text-accent border-b-2 border-accent' : 'text-muted hover:text-text'}`} onMouseDown={e => { e.preventDefault(); setTab('browse') }}>
          <FolderOpen size={12} /> {i18nT('components.projectPicker.browse')}
        </button>
      </div>
      )}
      {/* Native mode has no tab strip, so a heading takes its place: without it
          the panel is two bare file paths and a button with no stated purpose. */}
      {nativeOk === true && (
        <div className="px-3 py-2 border-b border-border text-[12px] font-medium text-text">
          {i18nT('components.projectPicker.open_a_project_folder')}
        </div>
      )}
      {nativeError && (
        <div className="px-3 py-2 text-[11px] text-warn border-b border-border">{nativeError}</div>
      )}

      {view === 'recent' ? (
        <>
          {recentDirs.length > 0 && (
            <div className="p-2 border-b border-border">
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted pointer-events-none" />
                <input
                  ref={recentSearchRef}
                  autoFocus
                  type="text"
                  aria-label={i18nT('components.projectPicker.search_recent_projects')}
                  aria-controls="pp-recent-list"
                  placeholder={i18nT('components.projectPicker.search_recent_projects_2')}
                  value={recentQuery}
                  onChange={e => setRecentQuery(e.target.value)}
                  className="w-full bg-bg-elevated border border-border rounded pl-7 pr-3 py-1.5 text-[13px] text-text placeholder:text-muted focus:outline-none focus:border-accent"
                />
              </div>
            </div>
          )}
          <div id="pp-recent-list" role="listbox" aria-label={i18nT('components.projectPicker.recent_projects')} className="overflow-y-auto flex-1 min-h-0">
            {recentDirs.length === 0 ? (
              <div className="px-3 py-6 text-[12px] text-muted text-center">
                {nativeOk === true
                  ? i18nT('components.projectPicker.no_recent_projects_native')
                  : i18nT('components.projectPicker.no_recent_projects')}
              </div>
            ) : filteredRecent.length === 0 ? (
              <div className="px-3 py-6 text-[12px] text-muted text-center">{i18nT('components.projectPicker.no_matching_projects')}</div>
            ) : filteredRecent.map((d, i) => (
              <button
                key={d}
                role="option"
                aria-selected={i === recentNav.selected}
                id={`pp-recent-${i}`}
                tabIndex={-1}
                ref={el => { recentNav.itemRefs.current[i] = el }}
                className={`w-full text-left px-3 py-2 flex items-center gap-2 cursor-pointer transition-colors ${i === recentNav.selected ? 'bg-bg-hover' : 'hover:bg-bg-hover'}`}
                onMouseEnter={() => recentNav.setSelected(i)}
                onMouseDown={e => { e.preventDefault(); select(d) }}
              >
                <FolderOpen size={12} className="text-accent shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] font-mono font-semibold text-text truncate">{d.split('/').pop()}</div>
                  <div className="text-[11px] text-muted truncate">{d}</div>
                </div>
              </button>
            ))}
          </div>
        </>
      ) : (
        <>
          <div className="p-2 border-b border-border flex gap-1 items-center">
            {browseParent && browseParent !== browsePath && (
              <button aria-label={i18nT('components.projectPicker.back')} onClick={() => browse(browseParent)} className="p-1 text-muted hover:text-text rounded hover:bg-bg-hover shrink-0" title={i18nT('components.projectPicker.back')}><ChevronLeft size={16} /></button>
            )}
            <input
              ref={inputRef}
              autoFocus
              type="text"
              role="combobox"
              aria-expanded={true}
              aria-label={i18nT('components.projectPicker.project_directory_path')}
              aria-controls="pp-browse-list"
              aria-activedescendant={filteredBrowse.length ? `pp-dir-${browseSel}` : undefined}
              placeholder={i18nT('components.projectPicker.path_to_project')}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => {
                const n = filteredBrowse.length
                const commit = () => { const p = input.trim() || browsePath; if (p) select(p) }
                if (e.key === 'ArrowDown') { e.preventDefault(); setBrowseSel(s => (n ? Math.min(s + 1, n - 1) : 0)) }
                else if (e.key === 'ArrowUp') { e.preventDefault(); setBrowseSel(s => Math.max(s - 1, 0)) }
                else if (e.key === 'Enter') {
                  e.preventDefault()
                  if (e.metaKey || e.ctrlKey) commit()                               // ⌘/Ctrl+Enter commits the current dir
                  else if (n > 0 && filteredBrowse[browseSel]) browse(filteredBrowse[browseSel].path)  // Enter drills into the highlighted folder
                  else commit()                                                       // nothing to drill into -> commit typed path
                }
                else if (e.key === 'ArrowLeft' && e.currentTarget.selectionStart === 0 && e.currentTarget.selectionEnd === 0 && browseParent && browseParent !== browsePath) {
                  e.preventDefault(); browse(browseParent)                            // caret at start -> go to parent
                }
                else if (e.key === 'Escape' || e.key === 'Tab') { e.preventDefault(); onOpenChange(false); btnRef?.current?.focus() }
              }}
              className="flex-1 bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-none focus:border-accent"
            />
            <button disabled={!input.trim() && !browsePath} onMouseDown={e => { e.preventDefault(); select(input.trim() || browsePath) }} className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 disabled:opacity-40 disabled:cursor-not-allowed shrink-0">{i18nT('components.projectPicker.select')}</button>
          </div>
          <div id="pp-browse-list" role="listbox" aria-label={i18nT('components.projectPicker.subdirectories')} className="overflow-y-auto flex-1 min-h-0">
            {filteredBrowse.length === 0 && <div className="px-3 py-4 text-[12px] text-muted text-center">{i18nT('components.projectPicker.no_subdirectories')}</div>}
            {filteredBrowse.map((d, i) => (
              <button
                key={d.path}
                role="option"
                aria-selected={i === browseSel}
                id={`pp-dir-${i}`}
                tabIndex={-1}
                ref={el => { browseItemRefs.current[i] = el }}
                className={`w-full text-left px-3 py-1.5 flex items-center gap-2 cursor-pointer transition-colors ${i === browseSel ? 'bg-bg-hover' : 'hover:bg-bg-hover'}`}
                onMouseEnter={() => setBrowseSel(i)}
                onClick={() => browse(d.path)}
              >
                <FolderOpen size={12} className="text-accent shrink-0" />
                <span className="text-[13px] font-mono text-text truncate">{d.name}</span>
                <ChevronRight size={12} className="text-muted ml-auto shrink-0" />
              </button>
            ))}
          </div>
        </>
      )}

      {nativeOk === true && (
        <div className="border-t border-border p-2 shrink-0">
          <button
            type="button"
            disabled={nativePending}
            onClick={openNative}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded text-[12px] font-medium bg-accent/20 text-accent hover:bg-accent/30 disabled:opacity-60 disabled:cursor-wait transition-colors"
          >
            <FolderOpen size={13} className="shrink-0" />
            {nativePending
              ? i18nT('components.projectPicker.waiting_for_dialog')
              : i18nT('components.projectPicker.choose_folder')}
          </button>
        </div>
      )}
    </div>,
    document.body
  )
}
