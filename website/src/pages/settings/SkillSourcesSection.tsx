import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

import { SettingsSection, SettingsCard, SettingsInput } from '../../components/settings'
import { api } from '../../api/client'
import type { SkillSource } from '../../types'

/**
 * Settings → Skills → Linked skill repos.
 *
 * Links a git repo whose SKILL.md trees are mirrored into
 * $KIROCREW_HOME/skill-sources/<name>/ and mounted read-only, so a team shares
 * skills through a repo instead of copying files between machines.
 *
 * Two behaviours the UI has to communicate, because both are load-bearing:
 * mirrored skills are LOWER precedence than local ones (a same-named local
 * skill wins, so a sync can never overwrite your own work), and a failed sync
 * leaves the last good mirror mounted rather than blanking the skill set — so a
 * failed row still reports the SHA it is serving.
 */
export function SkillSourcesSection() {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [repo, setRepo] = useState('')
  const [branch, setBranch] = useState('main')
  const [subdir, setSubdir] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [confirmRemove, setConfirmRemove] = useState('')

  const sourcesQ = useQuery({
    queryKey: ['skillSources'],
    // Defensive optional call: several test suites mock ../../api/client
    // partially, leaving newly added methods undefined on mount.
    queryFn: () => Promise.resolve(api.skillSources?.()).then((r) => r ?? { sources: [] }),
  })
  const sources: SkillSource[] = sourcesQ.data?.sources ?? []

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['skillSources'] })
    // Mirrored skills participate in the skills list, so it is now stale.
    qc.invalidateQueries({ queryKey: ['skills'] })
  }

  const addMut = useMutation({
    mutationFn: () => api.addSkillSource({ name: name.trim(), repo: repo.trim(), branch: branch.trim(), subdir: subdir.trim() }),
    onSuccess: (res) => {
      setError('')
      setNotice(`Linked ${res.source.name} — ${res.source.skill_count} skill(s) available`)
      setName('')
      setRepo('')
      setBranch('main')
      setSubdir('')
      invalidate()
    },
    onError: (e: unknown) => {
      setNotice('')
      setError(e instanceof Error ? e.message : 'Could not link that repo')
    },
  })

  const syncMut = useMutation({
    mutationFn: (sourceName: string) => api.syncSkillSource(sourceName),
    onSuccess: (res) => {
      if (res.ok) {
        setError('')
        setNotice(res.message || res.result?.message || 'Synced')
      } else {
        setNotice('')
        setError(res.message || res.result?.message || 'Sync failed')
      }
      invalidate()
    },
    onError: (e: unknown) => {
      setNotice('')
      setError(e instanceof Error ? e.message : 'Sync failed')
      // Refetch on failure too: the row's last_ok / last_error changed server-side
      // even though the sync did not succeed, so skipping this leaves the row
      // claiming the previous outcome.
      invalidate()
    },
  })

  const removeMut = useMutation({
    mutationFn: (sourceName: string) => api.deleteSkillSource(sourceName),
    onSuccess: () => {
      setError('')
      setNotice('')
      setConfirmRemove('')
      invalidate()
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : 'Could not unlink that repo'),
  })

  const busy = addMut.isPending || syncMut.isPending || removeMut.isPending
  const canAdd = name.trim().length > 0 && repo.trim().length > 0 && !busy

  return (
    <SettingsSection title="Linked skill repos">
      <SettingsCard>
        <p className="text-[12px] text-muted mb-3">
          Mirror a git repo's skills into this instance. Everyone who links the same repo gets the
          same skills, and a sync pulls updates and removals. Mirrored skills are read-only and
          never override a skill of the same name in your own skills directory.
        </p>

        {sources.length === 0 && !sourcesQ.isLoading && (
          <p className="text-[12px] text-muted mb-3">No repos linked yet.</p>
        )}

        {sources.length > 0 && (
          <ul className="mb-4 flex flex-col gap-2">
            {sources.map((s) => (
              <li
                key={s.name}
                className="border border-border rounded px-3 py-2 flex items-start justify-between gap-3"
              >
                <div className="min-w-0">
                  <div className="text-[13px] font-medium truncate">
                    {s.name}
                    <span className="text-muted font-normal">
                      {' · '}
                      {s.skill_count} skill{s.skill_count === 1 ? '' : 's'}
                    </span>
                  </div>
                  <div className="text-[11px] text-muted truncate">
                    {s.repo}
                    {' · '}
                    {s.branch}
                    {s.subdir ? ` · ${s.subdir}/` : ''}
                    {s.head ? ` · ${s.head.slice(0, 7)}` : ''}
                  </div>
                  {s.last_ok === false && (
                    <div className="text-[11px] text-danger mt-0.5">
                      Last sync failed{s.last_error ? ` (${s.last_error})` : ''}
                      {s.head ? ' — still serving the previous commit' : ''}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    type="button"
                    className="text-[12px] px-2 py-1 border border-border rounded hover:bg-hover disabled:opacity-50"
                    onClick={() => syncMut.mutate(s.name)}
                    disabled={busy}
                  >
                    {syncMut.isPending && syncMut.variables === s.name ? 'Syncing' : 'Sync'}
                  </button>
                  {confirmRemove === s.name ? (
                    <>
                      <button
                        type="button"
                        className="text-[12px] px-2 py-1 border border-danger text-danger rounded hover:bg-hover disabled:opacity-50"
                        onClick={() => removeMut.mutate(s.name)}
                        disabled={busy}
                      >
                        Confirm
                      </button>
                      <button
                        type="button"
                        className="text-[12px] px-2 py-1 border border-border rounded hover:bg-hover"
                        onClick={() => setConfirmRemove('')}
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="text-[12px] px-2 py-1 border border-border rounded hover:bg-hover disabled:opacity-50"
                      onClick={() => setConfirmRemove(s.name)}
                      disabled={busy}
                    >
                      Unlink
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}

        <SettingsInput
          label="Repository URL"
          aria-label="Repository URL"
          description="HTTPS or SSH git URL on a public forge (github.com, gitlab.com, ...) or a host you configured as an app registry."
          value={repo}
          onChange={setRepo}
          placeholder="https://github.com/your-org/team-skills.git"
          disabled={busy}
        />
        <SettingsInput
          label="Name"
          aria-label="Name"
          description="Lowercase kebab-case. Also the mirror directory name."
          value={name}
          onChange={setName}
          placeholder="team-skills"
          disabled={busy}
        />
        <SettingsInput
          label="Branch"
          aria-label="Branch"
          value={branch}
          onChange={setBranch}
          placeholder="main"
          disabled={busy}
        />
        <SettingsInput
          label="Subdirectory"
          aria-label="Subdirectory"
          description="Optional. Path inside the repo that holds the skills — leave empty if they are at the repo root."
          value={subdir}
          onChange={setSubdir}
          placeholder="skills"
          disabled={busy}
        />
        <div className="mt-3">
          <button
            type="button"
            className="text-[12px] px-3 py-1.5 border border-border rounded hover:bg-hover disabled:opacity-50"
            onClick={() => addMut.mutate()}
            disabled={!canAdd}
          >
            {addMut.isPending ? 'Linking and syncing' : 'Link repo'}
          </button>
        </div>

        {error && <p className="text-[12px] text-danger mt-2">{error}</p>}
        {notice && !error && <p className="text-[12px] text-muted mt-2">{notice}</p>}
      </SettingsCard>
    </SettingsSection>
  )
}
