import { useState, useMemo, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, RefreshCw, AlertTriangle, Info, ExternalLink, Plug, BookOpen, Check, X } from 'lucide-react'
import PowerIcon from '../../components/icons/PowerIcon'
import { api } from '../../api/client'
import { safeHttpUrl } from '../../lib/safeUrl'
import { Card, Btn, Badge, SearchInput, ContentSkeleton, EmptyState } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import type { Power, RegistryPower } from '../../types'

/** Fixed scope filter chips, mirroring kiro.dev/powers. */
const SCOPES: { key: string; label: string }[] = [
  { key: '', label: 'All' },
  { key: 'official', label: 'Official' },
  { key: 'aws', label: 'AWS' },
  { key: 'community', label: 'Community' },
]

const scopeLabel = (scope: RegistryPower['scope']): string =>
  scope === 'aws' ? 'AWS' : scope.charAt(0).toUpperCase() + scope.slice(1)

/** Read an ApiError-shaped status without importing ApiError (keeps the tab
 *  decoupled from the api module's class so test mocks stay simple). */
const errStatus = (e: unknown): number | undefined =>
  (e as { status?: number } | null | undefined)?.status

/**
 * Publisher icon for a registry card, falling back to the Kiro Powers mark.
 *
 * The URL is host-validated server-side (`marketplace.valid_icon_url`), so this
 * component's job is only presentational robustness: the asset host is a
 * third-party origin that can 404 or be unreachable offline, and a broken-image
 * glyph in every card is worse than no icon. `onError` therefore swaps in the
 * same `PowerIcon` used by the empty states, so the grid keeps one consistent
 * placeholder instead of a mix of broken and missing.
 *
 * Decorative: the accessible name is already the adjacent card title, so the
 * image carries an empty alt and is hidden from the accessibility tree rather
 * than repeating the name to screen readers.
 */
function PowerCardIcon({ url, name }: { url?: string; name: string }) {
  const [failed, setFailed] = useState(false)
  if (!url || failed) {
    return (
      <span
        className="shrink-0 w-8 h-8 rounded-md bg-bg-hover border border-border flex items-center justify-center text-muted"
        aria-hidden="true"
        data-testid="power-icon-fallback"
      >
        <PowerIcon size={16} />
      </span>
    )
  }
  return (
    <img
      src={url}
      alt=""
      aria-hidden="true"
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
      title={name}
      data-testid="power-icon"
      className="shrink-0 w-8 h-8 rounded-md object-contain bg-bg-hover border border-border"
    />
  )
}

export default function PowersTab() {
  const queryClient = useQueryClient()
  const [view, setView] = useState<'installed' | 'browse'>('installed')
  const pickedInitial = useRef(false)
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('')
  const [scope, setScope] = useState('')

  const installedQuery = useQuery({
    queryKey: ['powers'],
    queryFn: () => api.powers().then(r => r.installed),
  })
  const installed = installedQuery.data ?? []

  // Default to Installed only when something is installed; otherwise open on
  // Browse. Gated on isSuccess, NOT merely "not loading": a FAILED installed
  // query also stops loading, and treating that as a successful empty result
  // switched to Browse and offered already-installed Powers as installable.
  // On failure the view stays put and the error is surfaced instead.
  useEffect(() => {
    if (pickedInitial.current || !installedQuery.isSuccess) return
    pickedInitial.current = true
    setView(installed.length > 0 ? 'installed' : 'browse')
  }, [installedQuery.isSuccess, installed.length])

  // Registry is fetched lazily (only on the Browse view) and never retried so a
  // 503 surfaces the inline unavailable state immediately.
  const registryQuery = useQuery({
    queryKey: ['powers-registry'],
    queryFn: () => api.powersRegistry({ limit: 200 }),
    enabled: view === 'browse',
    retry: false,
  })
  const items = registryQuery.data?.items ?? []
  const providers = registryQuery.data?.providers ?? []
  const stale = registryQuery.data?.stale

  const categories = useMemo(
    () => Array.from(new Set(items.map(i => i.category).filter(Boolean))).sort(),
    [items]
  )

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return items.filter(p => {
      if (scope && p.scope !== scope) return false
      if (category && p.category !== category) return false
      if (q && !(`${p.displayName} ${p.description} ${p.author || ''} ${p.keywords.join(' ')}`.toLowerCase().includes(q))) return false
      return true
    })
  }, [items, search, category, scope])

  // Mark registry cards already installed. The backend records the RESOLVED
  // source repository as provenance, so the repo URL comparison is the reliable
  // one and applies to every provider. The bare-slug name check is a fallback
  // that is only sound for the OFFICIAL provider, where POWER.md `name` equals
  // the slug; applying it to marketplace cards would let a marketplace Power
  // named X falsely mark a same-named official Power installed (and vice versa),
  // since slugs are not unique across providers.
  const installedRefs = useMemo(() => ({
    names: new Set(installed.map(p => p.name)),
    urls: new Set(installed.map(p => (p.source?.ref || '').toLowerCase())),
  }), [installed])
  const isInstalled = (p: RegistryPower) =>
    installedRefs.urls.has((p.githubUrl || '').toLowerCase()) ||
    (p.provider === 'official' && installedRefs.names.has(p.id))

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['powers'] })
  const install = useMutation({
    mutationFn: (p: RegistryPower) => api.installPower({ kind: 'registry', ref: p.id, provider: p.provider }),
    onSuccess: () => { invalidate(); setView('installed') },
  })
  const remove = useMutation({ mutationFn: (name: string) => api.removePower(name), onSuccess: invalidate })

  /* ── one installed power row ──
     No enable/disable or trust control: an installed bundle is inert, so there
     is nothing here to switch on yet. Showing a disabled toggle would imply an
     activation path that does not exist; the note below says so plainly instead
     of leaving the absence unexplained. */
  const renderInstalled = (p: Power) => {
    return (
      <div key={p.name} className="border border-border bg-bg-elevated rounded-lg p-3.5 mb-2.5 transition-colors">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[14px] font-semibold text-text truncate">{p.displayName}</span>
              <Badge variant={p.kind === 'mcp' ? 'aim' : 'muted'}>
                {p.kind === 'mcp' ? <><Plug className="lucide-inline" /> MCP</> : <><BookOpen className="lucide-inline" /> Knowledge</>}
              </Badge>
              <Badge variant="muted">Inactive</Badge>
            </div>
            {p.author && <div className="text-[11px] text-muted mt-0.5">by {p.author}</div>}
            <p className="text-[12px] text-muted mt-1">{p.description}</p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Btn danger onClick={() => remove.mutate(p.name)} disabled={remove.isPending} aria-label={`Remove ${p.displayName}`}>Remove</Btn>
          </div>
        </div>

        {/* keywords */}
        {p.keywords.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2">
            {p.keywords.map(k => (
              <span key={k} className="text-[10px] px-1.5 py-[1px] rounded-full bg-bg-hover text-muted border border-border">{k}</span>
            ))}
          </div>
        )}

        <div className="mt-3 flex items-start gap-2 rounded-md border border-border bg-bg-hover p-2.5">
          <Info size={13} className="mt-[2px] shrink-0 text-muted" />
          <span className="text-[12px] text-muted">
            Downloaded from{' '}
            <a href={/^https?:\/\//.test(p.source.ref) ? p.source.ref : undefined} target="_blank" rel="noopener noreferrer" className="text-accent hover:text-accent-hover font-mono break-all">{p.source.ref}</a>
            {' '}and stored on disk. It is not active: no MCP server is registered and none of its
            guidance is loaded into agent context. Activation arrives in a later release.
          </span>
        </div>
      </div>
    )
  }

  /* ── one registry (browse) card ── */
  const renderCard = (p: RegistryPower) => {
    const busy = install.isPending && install.variables?.id === p.id
    return (
      <div key={`${p.provider}:${p.id}`} className="border border-border bg-bg-elevated rounded-lg p-3 flex flex-col gap-2 hover:bg-bg-hover hover:border-border-strong transition-colors">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-start gap-2.5 min-w-0">
            <PowerCardIcon url={p.iconUrl} name={p.displayName} />
            <div className="min-w-0">
              <div className="text-[13px] font-semibold text-text truncate">{p.displayName}</div>
              {p.author && <div className="text-[11px] text-muted truncate">by {p.author}</div>}
            </div>
          </div>
          <span className="text-[10px] px-1.5 py-[1px] rounded-full bg-bg-hover text-muted border border-border shrink-0">{scopeLabel(p.scope)}</span>
        </div>
        <p className="text-[12px] text-muted flex-1 overflow-hidden">{p.description}</p>
        <div className="flex items-center gap-1.5 flex-wrap">
          {p.category && <Badge variant="muted">{p.category}</Badge>}
        </div>
        <div className="flex items-center justify-between gap-2 mt-1">
          {/* Routed through `safeHttpUrl` and OMITTED when rejected, the same
              treatment `McpBrowserModal` gives a registry `repo_url`. The value
              originates in a scraped third-party page and is validated
              server-side, but it also survives in a disk cache, so a
              `javascript:` payload here would be click-to-execute. A missing
              link is the correct degradation — better than a link that runs. */}
          {safeHttpUrl(p.githubUrl) ? (
            <a
              href={safeHttpUrl(p.githubUrl) as string}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[12px] text-accent hover:text-accent-hover inline-flex items-center gap-1 focus-ring rounded"
              aria-label={`View ${p.displayName} source on GitHub`}
            >
              <ExternalLink size={12} /> Source
            </a>
          ) : (
            <span className="text-[12px] text-muted/70 italic">Source unavailable</span>
          )}
          {isInstalled(p)
            ? <Badge variant="ok"><Check className="lucide-inline" /> Installed</Badge>
            : <Btn primary onClick={() => install.mutate(p)} disabled={install.isPending} aria-label={`Install ${p.displayName}`}>
                <Download size={13} /> {busy ? 'Installing…' : 'Install'}
              </Btn>}
        </div>
      </div>
    )
  }

  /* ── browse view body ── */
  const renderBrowse = () => {
    if (registryQuery.isLoading) return <ContentSkeleton rows={6} />
    if (registryQuery.error) {
      const is503 = errStatus(registryQuery.error) === 503
      return (
        <div className="rounded-md border border-border bg-bg-elevated p-6 text-center" role="status">
          <AlertTriangle size={22} className="mx-auto text-[var(--warn)] mb-2" />
          <div className="text-sm font-medium text-text">Powers registry unavailable</div>
          <div className="text-[13px] text-muted mt-1 max-w-md mx-auto">
            {is503
              ? 'The registry is still warming up (HTTP 503). It becomes available once the provider layer is live — try again shortly.'
              : (registryQuery.error as Error).message}
          </div>
          <div className="mt-3">
            <Btn onClick={() => registryQuery.refetch()} disabled={registryQuery.isFetching}>
              <RefreshCw size={14} className={registryQuery.isFetching ? 'animate-spin' : ''} /> Retry
            </Btn>
          </div>
        </div>
      )
    }
    return (
      <>
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <div className="relative max-w-[420px] flex-1 min-w-[200px]">
            <SearchInput placeholder="Search powers…" value={search} onChange={e => setSearch(e.target.value)} aria-label="Search powers" />
            {search && <button className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text transition-colors cursor-pointer" onClick={() => setSearch('')} aria-label="Clear search"><X size={14} /></button>}
          </div>
          <div className="flex items-center gap-1" role="group" aria-label="Filter by scope">
            {SCOPES.map(s => (
              <button
                key={s.key || 'all'}
                type="button"
                onClick={() => setScope(s.key)}
                aria-pressed={scope === s.key}
                className={`px-2 py-1 rounded-md text-[12px] border transition-colors cursor-pointer focus-ring ${
                  scope === s.key
                    ? 'bg-accent-subtle text-accent border-accent/40'
                    : 'bg-bg-elevated text-muted border-border hover:text-text hover:border-border-strong'
                }`}
              >
                {s.label}
              </button>
            ))}
          </div>
          {categories.length > 0 && (
            <select
              value={category}
              onChange={e => setCategory(e.target.value)}
              aria-label="Filter by category"
              className="text-[12px] rounded-md border border-border bg-bg-elevated text-text px-2 py-1 outline-none focus-ring cursor-pointer"
            >
              <option value="">All categories</option>
              {categories.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          )}
          <Btn onClick={() => registryQuery.refetch()} disabled={registryQuery.isFetching} aria-label="Refresh registry">
            <RefreshCw size={14} className={registryQuery.isFetching ? 'animate-spin' : ''} />
          </Btn>
        </div>

        {/* `stale` means "at least one provider failed" — the backend sets it
            whether or not a cache was available, so it cannot promise cached
            content. When the list is empty there is nothing cached to show, and
            claiming otherwise made an offline failure look like stale data. */}
        {(stale || providers.some(pr => !pr.available)) && (
          <div className="mb-3 text-[12px] text-[var(--warn)] inline-flex items-center gap-1.5">
            <AlertTriangle size={13} />
            {items.length > 0
              ? 'Some providers are unavailable — this list may be incomplete or out of date.'
              : 'No results: every registry provider is currently unavailable.'}
          </div>
        )}
        {install.error && <div className="mb-3 text-[13px] text-danger">{(install.error as Error).message}</div>}

        {items.length === 0 ? (
          <EmptyState icon={<PowerIcon size={22} />} title="No powers in the registry" subtitle="The marketplace mirror returned no entries." />
        ) : filtered.length === 0 ? (
          <div className="text-muted/70 text-[13px] italic py-6 text-center">No powers match your filters.</div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {filtered.map(renderCard)}
          </div>
        )}
      </>
    )
  }

  /* ── installed view body ── */
  const renderInstalledView = () => {
    if (installedQuery.isLoading) return <ContentSkeleton rows={4} />
    if (installedQuery.error) {
      return (
        <div className="rounded-md border border-border bg-bg-elevated p-6 text-center" role="status">
          <AlertTriangle size={22} className="mx-auto text-danger mb-2" />
          <div className="text-sm font-medium text-text">Couldn’t load installed powers</div>
          <div className="text-[13px] text-muted mt-1">{(installedQuery.error as Error).message}</div>
          <div className="mt-3"><Btn onClick={() => installedQuery.refetch()}><RefreshCw size={14} /> Retry</Btn></div>
        </div>
      )
    }
    if (installed.length === 0) {
      return (
        <EmptyState
          icon={<PowerIcon size={22} />}
          title="No powers installed"
          subtitle="Browse the registry to add MCP tools and expert guidance to your agent."
        />
      )
    }
    return (
      <>
        {remove.error && <div className="mb-3 text-[13px] text-danger">{(remove.error as Error).message}</div>}
        {installed.map(renderInstalled)}
      </>
    )
  }

  return (<>
    <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2 flex items-center gap-2">
      Powers ({installed.length})
      <InfoTip text="Powers are installable capability bundles that package MCP tools and on-demand guidance. This release browses, installs and removes them. An installed power is stored on disk and left inert — no MCP server is registered and no guidance enters agent context — so nothing third-party runs until activation ships." />
      <span className="ml-auto flex items-center gap-1" role="tablist" aria-label="Powers view">
        <button
          type="button"
          role="tab"
          aria-selected={view === 'installed'}
          onClick={() => setView('installed')}
          className={`px-2.5 py-1 rounded-md text-[13px] border transition-colors cursor-pointer focus-ring ${
            view === 'installed' ? 'bg-accent-subtle text-accent border-accent/40' : 'bg-transparent text-muted border-border hover:text-text hover:border-border-strong'
          }`}
        >
          Installed
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === 'browse'}
          onClick={() => setView('browse')}
          className={`px-2.5 py-1 rounded-md text-[13px] border transition-colors cursor-pointer focus-ring inline-flex items-center gap-1 ${
            view === 'browse' ? 'bg-accent-subtle text-accent border-accent/40' : 'bg-transparent text-muted border-border hover:text-text hover:border-border-strong'
          }`}
        >
          <Download size={13} /> Browse
        </button>
      </span>
    </h4>
    <Card>
      {view === 'installed' ? renderInstalledView() : renderBrowse()}
    </Card>
  </>)
}
