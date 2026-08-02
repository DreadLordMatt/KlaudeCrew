/**
 * Ops Mission Control — Settings.
 *
 * Without this panel the app cannot be set up at all: providers ship disabled and
 * their credentials live in a keystone store the agent cannot reach, so a human
 * with a browser is the only thing that can turn one on.
 *
 * Two invariants the UI must not break:
 *
 * - **Secrets are write-only.** The API never returns a stored token, so a field
 *   that is already set shows a placeholder and an explicit Replace action rather
 *   than a pre-filled value. Never round-trip a secret through this form.
 * - **Autonomy is a ceiling.** The mode selector sets the app-level maximum; a
 *   per-signal rule can only narrow it. The copy says so, because "act" reads as
 *   a feature and is actually a grant of write access to production.
 *
 * KNOWN DEBT — i18n. Copy across this app's five components is inline English
 * rather than catalog keys. Per `website/AGENTS.md` it should route through
 * `i18nT`. Measured 2026-07-31 (the earlier reason recorded here was stale — it
 * cited a de/it parity failure that shipping German and Italian has since fixed;
 * all 134 i18n tests now pass):
 *
 * `node scripts/i18n-codemod.mjs --merge` extracts **93 ops keys** cleanly. The
 * blocker is that the codemod is **whole-corpus by design** — its only flags are
 * `--check`, `--dry-run`, `--merge`; there is no path scope. Re-measured on a
 * PRISTINE branch off `origin/main` (zero working-tree files, so no parallel work to
 * blame): the same run rewrote **12 files this app does not own** (ChatPage,
 * ComputerUsePanel, ChatInput, SkillsTab, …) and added **59 non-ops keys**. Those
 * strings are unextracted on `main` itself, so tree hygiene cannot separate them.
 *
 * An earlier note here blamed "parallel uncommitted work". That was wrong, and the
 * measurement above is why: the constraint is the tool's scope, not the tree's state.
 *
 * So this is a **core-i18n change, not an ops change**: extracting this app means
 * extracting the remaining corpus, and each of the 152 keys then needs a value in all
 * 10 shipped locales or `catalogParity.test.ts` fails — a real translation pass via
 * `scripts/i18n-shard.mjs split/join` (never hand-assembled; `join` is fail-closed
 * precisely to stop English shipping disguised as a translation). It belongs in its
 * own PR.
 *
 * Do NOT hand-edit `en.json` to paper over it — it is generated. Note the codemod
 * refuses a non-`--merge` run when the corpus is already converted, which is what
 * stops a rebuild from silently wiping the catalog.
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Bell,
  BellRing,
  CalendarClock,
  Clock,
  Eye,
  FolderGit2,
  GitBranch,
  Info,
  MessageSquare,
  Radio,
  UserCheck,
  Zap,
  KeyRound,
  Trash2,
  ShieldAlert,
} from 'lucide-react'
import { Badge, Btn, Card, CardTitle, Input, SendBtn, Toggle } from '../../components/ui'
import SegmentedControl from '../../components/SegmentedControl'
import {
  opsApi,
  type CompanionInfo,
  type LedgerSyncStatus,
  type NotifyOutStatus,
  type OperatingMode,
  type ProviderInfo,
  type RotationRoster,
  type SlackOutStatus,
  type SweepWindows,
} from './api'

/** Module-level frozen empty so the render-time fallback is referentially stable. */
const EMPTY_COMPANIONS: readonly CompanionInfo[] = Object.freeze([])

const MODE_SEGMENTS: { key: OperatingMode; label: string; icon: JSX.Element }[] = [
  { key: 'observe', label: 'Observe', icon: <Eye className="lucide-inline" /> },
  { key: 'propose', label: 'Propose', icon: <MessageSquare className="lucide-inline" /> },
  { key: 'act', label: 'Act', icon: <Zap className="lucide-inline" /> },
]

const MODE_HELP: Record<OperatingMode, string> = {
  observe:
    'Reads signals and investigates. Writes nothing to any provider. This is the default and the safe place to start.',
  propose:
    'Everything Observe does, plus drafts the acknowledge / resolve / comment action and asks you first. Nothing executes unapproved.',
  act: 'Executes actions for signal patterns you have explicitly allowlisted with a rule. A rule must name a source and a resource pattern — there is no "everything" rule. Every execution is audited.',
}

function ProviderRow({ provider }: { provider: ProviderInfo }) {
  const queryClient = useQueryClient()
  const [secretDrafts, setSecretDrafts] = useState<Record<string, string>>({})
  const [configDrafts, setConfigDrafts] = useState<Record<string, string>>({})

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'providers'] })
    queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
  }

  const configMutation = useMutation({
    mutationFn: (updates: Record<string, unknown>) =>
      opsApi.putProviderConfig(provider.id, updates),
    onSuccess: invalidate,
  })

  const secretMutation = useMutation({
    mutationFn: ({ field, value }: { field: string; value: string }) =>
      opsApi.putSecret(provider.id, field, value),
    onSuccess: (_data, variables) => {
      // Drop the draft immediately so a token never lingers in component state
      // longer than the request that stored it.
      setSecretDrafts((prev) => ({ ...prev, [variables.field]: '' }))
      invalidate()
    },
  })

  const revokeMutation = useMutation({
    mutationFn: () => opsApi.deleteSecret(provider.id),
    onSuccess: invalidate,
  })

  const enabled = Boolean(provider.config?.enabled)
  // Fields other than the enable flag, which has its own toggle.
  const editableFields = provider.config_fields.filter((f) => f !== 'enabled')

  // Not every adapter HAS an enable flag, and painting a toggle for one that does not
  // was a dead control. `_handle_put_provider_config` 400s any key the adapter did not
  // declare, and the rotation adapters declare none: "Schedule file (git)" declares only
  // `github_login`, "Observe only" and "Always on shift" declare nothing. So their
  // toggles rejected every click — and because the error line lived INSIDE the block the
  // toggle gates, the rejection was invisible: the operator clicked, nothing moved, and
  // no message appeared. `github_login` was therefore unreachable through the UI at all,
  // which is precisely the field an instance needs to recognise itself in rotation.yaml.
  //
  // The fix is here and not in the adapter: `schedule_file.configured()` deliberately
  // keys on the schedule FILE existing, and rotation sources are selected by
  // `configured()`, never by `provider_enabled` (registry.resolve_shift) — so adding an
  // `enabled` field would invent a flag that gates nothing.
  const hasEnableFlag = provider.config_fields.includes('enabled')
  const fieldsVisible = enabled || !hasEnableFlag
  const writeError = configMutation.isError || secretMutation.isError

  return (
    <div className="border-t border-border py-3">
      <div className="flex items-center gap-2 mb-1">
        {hasEnableFlag ? (
          <Toggle
            checked={enabled}
            onChange={(v) => configMutation.mutate({ enabled: v })}
            label={`Enable ${provider.display_name}`}
          />
        ) : null}
        <span className="text-sm font-medium">{provider.display_name}</span>
        <Badge variant={provider.configured ? 'ok' : 'muted'}>
          {provider.configured ? 'ready' : 'not set up'}
        </Badge>
        <span className="text-[12px] text-muted ml-auto">{provider.roles.join(' · ')}</span>
      </div>
      <p className="text-[12px] text-muted mb-2">{provider.detail}</p>

      {fieldsVisible && (editableFields.length > 0 || provider.secret_fields.length > 0) ? (
        <div className={`flex flex-col gap-2 ${hasEnableFlag ? 'pl-11' : ''}`}>
          {editableFields.map((field) => {
            const inputId = `omc-${provider.id}-${field}`
            // The input is BOTH nested in the label and bound by htmlFor/id:
            // jsx-a11y/label-has-for requires both forms.
            return (
              <label
                key={field}
                htmlFor={inputId}
                className="flex items-center gap-2 text-[13px]"
              >
                <span className="w-44 shrink-0 text-muted">{field}</span>
                <Input
                  id={inputId}
                  value={configDrafts[field] ?? String(provider.config?.[field] ?? '')}
                  onChange={(e) =>
                    setConfigDrafts((prev) => ({ ...prev, [field]: e.target.value }))
                  }
                  onBlur={() => {
                    const draft = configDrafts[field]
                    if (draft !== undefined && draft !== String(provider.config?.[field] ?? '')) {
                      configMutation.mutate({ [field]: draft })
                    }
                  }}
                  placeholder="—"
                />
              </label>
            )
          })}

          {provider.secret_fields.map((field) => {
            const isSet = Boolean(provider.secrets?.[field])
            const secretId = `omc-${provider.id}-secret-${field}`
            return (
              <label
                key={field}
                htmlFor={secretId}
                className="flex items-center gap-2 text-[13px]"
              >
                <span className="w-44 shrink-0 text-muted flex items-center gap-1">
                  <KeyRound className="lucide-inline" /> {field}
                </span>
                <Input
                  id={secretId}
                  type="password"
                  autoComplete="off"
                  value={secretDrafts[field] ?? ''}
                  onChange={(e) =>
                    setSecretDrafts((prev) => ({ ...prev, [field]: e.target.value }))
                  }
                  // Never pre-fill: the API cannot return a stored secret, and a
                  // placeholder that looked like a value would invite a re-save
                  // of the placeholder itself.
                  placeholder={isSet ? 'stored — enter a new value to replace' : 'not set'}
                />
                <SendBtn
                  disabled={!secretDrafts[field] || secretMutation.isPending}
                  onClick={() =>
                    secretMutation.mutate({ field, value: secretDrafts[field] ?? '' })
                  }
                >
                  {isSet ? 'Replace' : 'Save'}
                </SendBtn>
              </label>
            )
          })}

          {provider.secret_fields.length > 0 && Object.keys(provider.secrets ?? {}).some(
            (k) => provider.secrets?.[k],
          ) ? (
            <div>
              <Btn danger disabled={revokeMutation.isPending} onClick={() => revokeMutation.mutate()}>
                <Trash2 className="lucide-inline" /> Revoke stored credentials
              </Btn>
              {/* Disclose the retention boundary HERE, next to the only control that
                  changes it. Credentials live in a keystone file at the crew-home root
                  (they must, for the sensitive-path floor), NOT under the app
                  directory — so uninstalling the app cannot remove them, and nothing
                  else tells the user that. Revoking before uninstall is the only way
                  to be sure a token is gone. */}
              <p className="text-[12px] text-muted mt-1.5">
                Stored outside the app&apos;s folder so the agent cannot read them —
                which also means <strong>uninstalling this app does not delete
                them</strong>. Revoke here first if you want the credential gone.
              </p>
            </div>
          ) : null}
        </div>
      ) : null}

      {/* OUTSIDE the block the enable toggle gates. It used to be inside, which meant the
          one write most likely to be rejected — the toggle itself, on an adapter with no
          `enabled` field — failed with no visible message at all. A rejected write must
          always be able to say so. */}
      {writeError ? (
        <p className={`text-[12px] text-danger ${hasEnableFlag ? 'pl-11' : ''}`}>
          {((configMutation.error ?? secretMutation.error) as Error)?.message}
        </p>
      ) : null}
    </div>
  )
}

/**
 * Slack output channel — the pin board.
 *
 * Deliberately has NO token field. KiroCrew already holds a Slack bot token for
 * its own gateway and this app reuses that client, so there is no second
 * credential to enter, store, or rotate. The consequence is a real dependency
 * rather than a hidden one: when KiroCrew's Slack is not connected, this card says
 * so and points at the fix instead of silently doing nothing.
 */
function SlackOutCard({
  status,
  onSave,
  saving,
}: {
  status?: SlackOutStatus
  onSave: (updates: Record<string, unknown>) => void
  saving: boolean
}) {
  // Local draft so the field is editable without a save per keystroke; seeded
  // from the server value and re-seeded when it changes underneath us.
  const [channel, setChannel] = useState(status?.channel ?? '')
  const [touched, setTouched] = useState(false)
  const serverChannel = status?.channel ?? ''
  useEffect(() => {
    if (!touched) setChannel(serverChannel)
  }, [serverChannel, touched])

  const enabled = Boolean(status?.enabled)
  const dirty = touched && channel.trim() !== serverChannel

  return (
    <Card>
      <CardTitle>
        <MessageSquare className="lucide-inline" /> Slack
      </CardTitle>
      <p className="text-[13px] text-muted mb-3">
        Mirror incidents to a channel as a live board: one message per incident whose
        state updates in place, with the diagnosis in its thread. Uses the Slack
        connection KiroCrew already has — this app stores no Slack token of its own.
      </p>

      <div className="flex items-center gap-2 text-[13px]">
        <Toggle
          checked={enabled}
          onChange={(v) => onSave({ slack_enabled: v })}
          label="Mirror incidents to Slack"
        />
        <span>Mirror incidents to Slack</span>
        {status ? (
          <Badge variant={status.ready ? 'ok' : enabled ? 'warn' : 'muted'}>
            {status.ready ? 'active' : enabled ? 'needs setup' : 'off'}
          </Badge>
        ) : null}
      </div>

      {enabled ? (
        <div className="mt-3 flex flex-col gap-2">
          {/* Input BOTH nested and bound by htmlFor/id — jsx-a11y/label-has-for
              requires both forms, matching the provider rows above. */}
          <label
            className="flex items-center gap-2 text-[13px]"
            htmlFor="omc-slack-channel"
          >
            <span className="w-44 shrink-0 text-muted">Channel ID</span>
            <Input
              id="omc-slack-channel"
              value={channel}
              placeholder="C0123456789"
              onChange={(e) => {
                setTouched(true)
                setChannel(e.target.value)
              }}
            />
            <SendBtn
              disabled={!dirty || saving}
              onClick={() => {
                onSave({ slack_channel: channel.trim() })
                setTouched(false)
              }}
            >
              Save
            </SendBtn>
          </label>
          <p className="text-[12px] text-muted">
            Find it at the bottom of the channel&apos;s detail dialog in Slack. Invite the
            KiroCrew bot to the channel first, or posting will fail.
          </p>
        </div>
      ) : null}

      {/* The backend already distinguishes the three failure modes and names the
          fix for each; rendering its sentence beats re-deriving that here. */}
      {status && !status.ready && enabled ? (
        <p className="text-[13px] text-warn mt-3 flex items-start gap-1.5">
          <AlertTriangle className="lucide-inline" />
          <span>{status.detail}</span>
        </p>
      ) : null}
    </Card>
  )
}

/** Lucide component per channel icon name declared in `app.json`. */
const CHANNEL_ICONS: Record<string, JSX.Element> = {
  UserCheck: <UserCheck className="lucide-inline" />,
  Radio: <Radio className="lucide-inline" />,
  Clock: <Clock className="lucide-inline" />,
}

/**
 * What each declared channel actually fires on.
 *
 * Keyed by the manifest id, so a channel the backend declares and this map does not know
 * still renders (with its name and priority) rather than disappearing — an unexplained
 * channel is better than a hidden one. Deliberately phrased as the EDGE condition,
 * because that is the contract: the backend pushes on a state change and never on a tick,
 * and copy that said "when a source is unhealthy" would read as a recurring alert.
 */
const CHANNEL_WHEN: Record<string, string> = {
  'waiting-on-you': 'the moment an incident starts waiting on a person',
  'source-health': 'the poll where a source stops answering — not again while it stays down',
  'incident-released': 'when an idle investigation is released for re-pickup',
}

/**
 * Local desktop notifications.
 *
 * This card exists because the app declared the `notification` permission from day one
 * and never produced one, so the only push channel that needs NO credential and no
 * inbound URL was inert — every fact this app computed required an open dashboard tab or
 * a Slack workspace it holds no token for.
 *
 * Deliberately has NO per-channel mute control. KiroCrew renders that centrally at
 * Settings → Notifications (one row per channel with a mute switch and a priority
 * override), and a second copy here would be two controls that can disagree about the
 * same stored setting. What this card owns instead is the app-level on/off and the
 * DECLARATION of which channels exist — which the central rail cannot show for a freshly
 * installed app, because it lists channels only once they have been registered and
 * registration happens on a channel's first push.
 */
function NotifyOutCard({
  status,
  onSave,
}: {
  status?: NotifyOutStatus
  onSave: (updates: Record<string, unknown>) => void
}) {
  const enabled = Boolean(status?.enabled)
  const channels = status?.channels ?? []

  return (
    <Card>
      <CardTitle>
        <BellRing className="lucide-inline" /> Desktop notifications
      </CardTitle>
      <p className="text-[13px] text-muted mb-3">
        Get a notification when something changes that needs you — no Slack workspace and
        no credential of any kind. It goes to KiroCrew&apos;s notification centre and to
        your desktop.
      </p>

      <div className="flex items-center gap-2 text-[13px]">
        <Toggle
          checked={enabled}
          onChange={(v) => onSave({ notify_enabled: v })}
          label="Notify me on state changes"
        />
        <span>Notify me on state changes</span>
        {/* Three states, not two, and `bus_available` is what separates the middle one.
            "needs setup" was rendered for every not-ready case — including the one where
            there is no setup to do: the bus lives on the running gateway, so its absence is
            not something a toggle, a field or a credential fixes. Telling an operator to
            set something up when nothing they can reach would help is advice that cannot
            work, and they would go looking for the missing field. Unlike the Slack card,
            where the not-ready half genuinely IS the operator's (connect KiroCrew's Slack),
            so "needs setup" is honest there. */}
        {status ? (
          <Badge
            variant={status.ready ? 'ok' : !enabled ? 'muted' : status.bus_available ? 'warn' : 'err'}
            title={status.detail}
          >
            {status.ready
              ? 'active'
              : !enabled
                ? 'off'
                : status.bus_available
                  ? 'needs setup'
                  : 'unavailable here'}
          </Badge>
        ) : null}
      </div>

      {/* The declaration, not the bus registry — see api.ts. Shown whether or not the
          channel is on, because "which channels could speak to me" is the question an
          operator has BEFORE deciding to enable this. */}
      {channels.length > 0 ? (
        <dl className="mt-3 flex flex-col gap-2">
          {channels.map((ch) => (
            <div key={ch.id} className="flex items-start gap-2 text-[13px]">
              <dt className="flex items-center gap-1.5 w-44 shrink-0">
                {CHANNEL_ICONS[ch.icon] ?? <Bell className="lucide-inline" />}
                <span>{ch.name}</span>
              </dt>
              <dd className="text-muted">
                {CHANNEL_WHEN[ch.id] ?? 'on a state change'}
                {ch.default_priority === 'critical' ? (
                  <span className="text-warn"> · interrupts by default</span>
                ) : ch.default_priority === 'passive' ? (
                  <span> · quiet, and expires on its own</span>
                ) : null}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      <p className="text-[12px] text-muted mt-3">
        One notification per state change, never per heartbeat: a source that stays down
        is reported once, not every two minutes. Repeats for the same incident stack into
        one row.
      </p>

      {/* Mute lives centrally. Say where, rather than adding a control that would fight
          the one KiroCrew already stores. */}
      {enabled ? (
        <p className="text-[12px] text-muted mt-1 flex items-start gap-1.5">
          <Info className="lucide-inline" />
          <span>
            To silence one of these without turning the rest off — or to change how loudly
            it interrupts — use Settings → Notifications, where every app channel is
            listed. A channel appears there after it first fires.
          </span>
        </p>
      ) : null}

      {/* The backend distinguishes off from no-bus and names the fix for each; render its
          sentence rather than re-deriving that here. */}
      {status && !status.ready && enabled ? (
        <p className="text-[13px] text-warn mt-3 flex items-start gap-1.5">
          <AlertTriangle className="lucide-inline" />
          <span>{status.detail}</span>
        </p>
      ) : null}
    </Card>
  )
}

/**
 * Hide a `userinfo@` component before a remote URL is painted.
 *
 * `https://user:ghp_xxx@github.com/org/repo.git` → `https://github.com/org/repo.git`.
 *
 * Not theatre, and not a claim that the value was cleaned: `config.json` is served
 * UNAUTHENTICATED (see `providers.write_config`), the write path only length-caps the
 * remote, and `secrets.redact_tokens` has no pattern for a PAT embedded in a URL. So if an
 * operator pastes a token-bearing remote it IS stored in a world-readable file, and this
 * function changes nothing about that. What it does is stop this panel from being a second
 * place the token is displayed — a screenshot or a screen-share of Settings should not leak
 * a credential the operator has already been told not to enter here.
 */
export function displayRemote(url: string): string {
  const trimmed = url.trim()
  // Scheme-relative and scp-style (`git@host:org/repo`) remotes have no userinfo to strip;
  // the `//` requirement is what distinguishes them from a URL that does.
  const marker = trimmed.indexOf('://')
  if (marker < 0) return trimmed
  const rest = trimmed.slice(marker + 3)
  const at = rest.indexOf('@')
  const slash = rest.indexOf('/')
  if (at < 0 || (slash >= 0 && at > slash)) return trimmed
  return `${trimmed.slice(0, marker + 3)}${rest.slice(at + 1)}`
}

/**
 * Shared team memory — the git repo the knowledge ledger syncs through.
 *
 * The backend has accepted these three settings all along and nothing ever sent them, so
 * the app's headline team feature was reachable only by hand-editing `data/config.json`.
 * The owner's report was exactly that: "I do not see where we can specify memory exchange
 * / SOP / on-call schedule repository."
 *
 * Placed with the Slack card because both answer "where does this instance talk to the
 * outside world", and BEFORE the Instance card because that card's nightly-maintenance
 * copy only makes sense to someone who already knows a shared ledger exists.
 */
function SharedMemoryCard({
  status,
  onSave,
  saving,
}: {
  status?: LedgerSyncStatus
  onSave: (updates: Record<string, unknown>) => void
  saving: boolean
}) {
  // Local drafts, seeded from the server and re-seeded while untouched — the same shape
  // as the Slack channel field, so a background /state refresh cannot clobber typing.
  const serverRemote = status?.remote ?? ''
  const serverBranch = status?.branch ?? ''
  const [remote, setRemote] = useState(serverRemote)
  const [branch, setBranch] = useState(serverBranch)
  const [remoteTouched, setRemoteTouched] = useState(false)
  const [branchTouched, setBranchTouched] = useState(false)
  useEffect(() => {
    if (!remoteTouched) setRemote(serverRemote)
  }, [serverRemote, remoteTouched])
  useEffect(() => {
    if (!branchTouched) setBranch(serverBranch)
  }, [serverBranch, branchTouched])

  const enabled = Boolean(status?.enabled)
  const remoteDirty = remoteTouched && remote.trim() !== serverRemote
  // A blank branch is never sent: the backend applies `main` only when the key is absent,
  // so posting "" would persist an empty branch and no default would rescue it.
  const branchDirty = branchTouched && branch.trim() !== '' && branch.trim() !== serverBranch

  const saveRemote = () => {
    if (!remoteDirty || saving) return
    onSave({ ledger_sync_remote: remote.trim() })
    setRemoteTouched(false)
  }
  const saveBranch = () => {
    if (!branchDirty || saving) return
    onSave({ ledger_sync_branch: branch.trim() })
    setBranchTouched(false)
  }

  // A branch problem only matters once the repo exists AND sync is on: `branch_matches` is
  // true for an uninitialized repo by design, so this is the "sync is live and the local
  // repo drifted" case. `detached` narrows it to the half the operator has to fix by hand.
  const branchDrifted = Boolean(status?.ready && status.initialized && !status.branch_matches)

  // Driven ONLY by fields the backend reports. Worst state first, because a schedule
  // conflict makes every push fail and must not be outranked by "syncing".
  //
  // A drifted branch ranks BELOW both conflicts and above "syncing": publishing genuinely
  // still works (the refspecs are explicit), so calling it an error would overstate it —
  // but "syncing" alone is what hid it for the whole life of the feature.
  const badge = status?.schedule_conflict
    ? { variant: 'err' as const, label: 'schedule conflict' }
    : status?.conflict
      ? { variant: 'warn' as const, label: 'ledger conflict' }
      : branchDrifted
        ? {
            variant: 'warn' as const,
            label: status?.detached ? 'detached HEAD' : 'wrong local branch',
          }
        : status?.ready && status.initialized
          ? { variant: 'ok' as const, label: 'syncing' }
          : status?.ready
            ? { variant: 'ok' as const, label: 'ready' }
            : enabled
              ? { variant: 'warn' as const, label: 'needs setup' }
              : { variant: 'muted' as const, label: 'off' }

  const troubled = Boolean(
    status && (!status.ready || status.conflict || status.schedule_conflict || branchDrifted),
  )

  return (
    <Card>
      <CardTitle>
        <FolderGit2 className="lucide-inline" /> Shared team memory
      </CardTitle>
      <p className="text-[13px] text-muted mb-3">
        Your team&apos;s knowledge ledger, exchanged through an ordinary git repo:
        <span className="font-mono"> ledger.jsonl</span> is pulled and pushed by the nightly
        maintenance pass, so a fix a teammate recorded reaches your investigations from the
        next pass onward. The on-call schedule rides the same repo — see below.
      </p>
      {/* The CADENCE, stated because it is not the one the design intended and an operator
          plans around it. `POST /ledger/hygiene` is the only caller of the git transport
          (`grep sync_safely backend/` → routes.py twice, dispatch.py zero), and that route
          runs on the daily `primary`-tier cron — so an instance that is not primary has no
          code path that pulls at all.

          This paragraph replaced "pulled before every match and pushed after every lesson",
          which is what `ledger_sync`'s module docstring still aspires to and what
          `sync_safely`'s own docstring wrongly claims ("the dispatch cycle and the daily
          hygiene pass call this"). Believing it costs a team correctness, not just latency:
          `rotation.yaml` travels in the same repo, so a non-primary instance keeps arming
          off a schedule it may never fetch again — which is the double-claim strict gating
          exists to prevent. Saying the real cadence is the honest half of the fix; moving
          the pull onto an always-tier cron is the other half and is backend work. */}
      <p className="text-[12px] text-muted mb-3 flex items-start gap-1.5">
        <Info className="lucide-inline" />
        <span>
          Exchange happens on the nightly maintenance pass only — not per incident. It runs
          on whichever instance has &quot;nightly ledger maintenance&quot; on (see Instance,
          below), so an instance with that off never pulls: it works from the copy it already
          has, including the copy of <span className="font-mono">rotation.yaml</span> it last
          saw.
        </span>
      </p>

      <div className="flex items-center gap-2 text-[13px]">
        <Toggle
          checked={enabled}
          onChange={(v) => onSave({ ledger_sync_enabled: v })}
          label="Share the knowledge ledger with my team"
        />
        <span>Share the knowledge ledger with my team</span>
        {status ? <Badge variant={badge.variant}>{badge.label}</Badge> : null}
      </div>

      <div className="mt-3 flex flex-col gap-2">
        {/* Input BOTH nested and bound by htmlFor/id — jsx-a11y/label-has-for wants both,
            matching every other field in this file. */}
        <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-sync-remote">
          <span className="w-44 shrink-0 text-muted">Repository</span>
          <Input
            id="omc-sync-remote"
            value={remote}
            placeholder="git@github.com:your-org/ops-memory.git"
            onChange={(e) => {
              setRemoteTouched(true)
              setRemote(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') saveRemote()
            }}
          />
          {/* An explicit Save, not a commit on blur. Tabbing out of a half-pasted URL
              would repoint the whole team's repo, and the backend's branch/length
              refusals need a moment the operator can attribute to their own click. */}
          <SendBtn disabled={!remoteDirty || saving} onClick={saveRemote}>
            Save
          </SendBtn>
        </label>

        <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-sync-branch">
          <span className="w-44 shrink-0 text-muted flex items-center gap-1">
            <GitBranch className="lucide-inline" /> Branch
          </span>
          <Input
            id="omc-sync-branch"
            value={branch}
            placeholder="main"
            onChange={(e) => {
              setBranchTouched(true)
              setBranch(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') saveBranch()
            }}
          />
          <SendBtn disabled={!branchDirty || saving} onClick={saveBranch}>
            Save
          </SendBtn>
        </label>

        {/* The honest boundary. There is genuinely no credential to enter — sync shells out
            to git, which uses whatever the operator already has — and saying so is the only
            thing that stops someone reaching for a token-bearing HTTPS URL, since this
            config file is served without auth. */}
        <p className="text-[12px] text-muted">
          No credential to enter: sync runs <span className="font-mono">git</span> as you,
          with your own SSH key or credential helper. Prefer an SSH remote
          (<span className="font-mono">git@github.com:org/repo.git</span>) — never paste a
          URL with a token in it, because this app&apos;s settings file is readable without
          signing in. Leave the branch blank for <span className="font-mono">main</span>.
        </p>
      </div>

      {status ? (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px] mt-3">
          <dt className="text-muted">Remote</dt>
          <dd className="font-mono truncate">{displayRemote(status.remote) || '—'}</dd>
          <dt className="text-muted">Branch</dt>
          <dd className="font-mono">{status.branch || 'main'}</dd>
          {/* The local branch is shown ONLY when it disagrees, and labelled as a separate
              fact rather than folded into the row above. Two rows that usually say the same
              thing invite exactly the conflation that caused the bug; one row that appears
              only on disagreement makes the disagreement the point. */}
          {branchDrifted ? (
            <>
              <dt className="text-warn">This repo is on</dt>
              <dd className="font-mono text-warn">
                {status.detached ? 'no branch (detached HEAD)' : status.local_branch || '—'}
              </dd>
            </>
          ) : null}
        </dl>
      ) : null}

      {/* The backend already distinguishes off / no remote / not yet created / conflicted
          and names the fix for each, so its sentence is rendered verbatim — the same
          reasoning as the Slack card above. */}
      {troubled && status ? (
        <p
          className={`text-[13px] mt-3 flex items-start gap-1.5 ${
            status.schedule_conflict ? 'text-danger' : 'text-warn'
          }`}
        >
          <AlertTriangle className="lucide-inline" />
          <span>{status.detail}</span>
        </p>
      ) : null}
      {status?.schedule_conflict ? (
        <p className="text-[12px] text-muted mt-1">
          Pushes stay refused until <span className="font-mono">rotation.yaml</span> is
          resolved by hand in the repo — publishing a schedule full of conflict markers
          would leave every teammate unable to parse who is on call.
        </p>
      ) : null}
      {/* Says what the drift costs, and does NOT overstate it: the exchange keeps working,
          because sync names the branch explicitly on every fetch, merge and push. What
          breaks is the operator's own git — which is the thing they need when a conflicted
          schedule sends them into this directory to fix it by hand. */}
      {branchDrifted && status ? (
        <p className="text-[12px] text-muted mt-1">
          Your team&apos;s ledger is still being exchanged — sync names{' '}
          <span className="font-mono">{status.branch || 'main'}</span> explicitly every time.
          What does not work is <span className="font-mono">git pull</span> or{' '}
          <span className="font-mono">git push</span> run by hand in the ledger directory,
          because this branch has no upstream.{' '}
          {status.detached
            ? 'A detached HEAD is left alone on purpose: finish or abort the merge or rebase in progress first, since moving refs under one can lose that work.'
            : 'The next sync moves it across by itself, unless a different branch of that name already exists — then it refuses rather than choose which history to keep, and the message above names the merge to run.'}
        </p>
      ) : null}
    </Card>
  )
}

/**
 * On-call schedule — the second file in that same repo.
 *
 * Exists because the format was documented ONLY in a Python docstring, the module spec and
 * one SOP, so an operator standing in Settings had no way to learn that the schedule is a
 * file at all, let alone which file. It is deliberately not a path they choose: every
 * teammate must read the same one, so `schedule_file.schedule_path()` fixes it beside
 * `ledger.jsonl`.
 *
 * Renders `github_login` here rather than only in the generic Providers row above because
 * this is where someone goes looking for it, and it answers the roster warnings directly
 * beneath. Both controls post the same key through the same mutation, so they cannot
 * disagree on the server.
 */
function OnCallScheduleCard({
  provider,
  roster,
  syncReady,
}: {
  provider?: ProviderInfo
  roster?: RotationRoster
  syncReady: boolean
}) {
  const queryClient = useQueryClient()
  const serverLogin = String(provider?.config?.github_login ?? '')
  const [login, setLogin] = useState(serverLogin)
  const [touched, setTouched] = useState(false)
  useEffect(() => {
    if (!touched) setLogin(serverLogin)
  }, [serverLogin, touched])

  const loginMutation = useMutation({
    mutationFn: (value: string) => opsApi.putProviderConfig('schedule-file', {
      github_login: value,
    }),
    onSuccess: () => {
      setTouched(false)
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'providers'] })
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'rotation'] })
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
    },
  })

  const dirty = touched && login.trim() !== serverLogin
  const commit = () => {
    if (!dirty || loginMutation.isPending) return
    loginMutation.mutate(login.trim())
  }

  return (
    <Card>
      <CardTitle>
        <CalendarClock className="lucide-inline" /> On-call schedule
      </CardTitle>
      <p className="text-[13px] text-muted mb-3">
        There is no path to pick here. The schedule is a file called
        <span className="font-mono"> rotation.yaml</span>, committed to the repo above beside
        <span className="font-mono"> ledger.jsonl</span> — the location is fixed on purpose,
        because a rotation only works if every teammate&apos;s instance reads the same one. Edit
        it in the repo; it arrives on the same pull that brings teammates&apos; lessons, and a
        shift swap becomes a reviewable diff.
      </p>

      {!syncReady ? (
        <p className="text-[12px] text-warn mb-3 flex items-start gap-1.5">
          <AlertTriangle className="lucide-inline" />
          <span>
            Sharing is not set up above, so the schedule is whatever exists on this machine
            and reaches nobody else.
          </span>
        </p>
      ) : null}

      <p className="text-[12px] text-muted flex items-start gap-1.5">
        <Info className="lucide-inline" />
        <span>Expected shape:</span>
      </p>
      <pre className="font-mono text-[12px] bg-bg-elevated border border-border rounded-md p-2.5 mt-1 overflow-x-auto">
        {`leader: octocat                 # optional; runs nightly ledger hygiene alone
timezone: America/Los_Angeles   # optional; UTC when absent
shifts:
  - from: 2026-08-01
    to: 2026-08-08              # a date-only 'to' means THROUGH that whole day
    who: octocat                # a GitHub login
  - from: 2026-08-08T09:00
    to: 2026-08-15T09:00
    who: [octocat, hubot]       # a list is allowed — co-primary`}
      </pre>

      {/* `schedule-file` is registered unconditionally (registry.build_default_registry), so
          an absent provider here means the catalog has not arrived yet, not that the adapter
          is missing — hence a loading line rather than an explanation of a fault. */}
      {!provider ? <p className="text-[12px] text-muted mt-3">Loading…</p> : null}

      {provider ? (
        <div className="mt-3 flex flex-col gap-2">
          <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-schedule-login">
            <span className="w-44 shrink-0 text-muted">Your GitHub login</span>
            <Input
              id="omc-schedule-login"
              value={login}
              placeholder="resolved from the gh CLI when blank"
              onChange={(e) => {
                setTouched(true)
                setLogin(e.target.value)
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commit()
              }}
            />
            <SendBtn disabled={!dirty || loginMutation.isPending} onClick={commit}>
              Save
            </SendBtn>
          </label>
          <p className="text-[12px] text-muted">
            How this instance recognises itself in <span className="font-mono">who:</span>.
            Leave it blank to use the login the local <span className="font-mono">gh</span> CLI
            is authenticated as.
          </p>
          {loginMutation.isError ? (
            <p className="text-[12px] text-danger">
              {(loginMutation.error as Error).message}
            </p>
          ) : null}
        </div>
      ) : null}

      {/* The two setup mistakes the app ALREADY detects and, until now, could only report
          on the Board — which is the wrong place, because the Board answers "who has the
          pager" and this answers "why is my setup wrong". Under strict gating both leave
          this instance permanently idle while looking configured. */}
      {roster ? (
        <div className="mt-3 flex flex-col gap-1.5">
          {!roster.me ? (
            <p className="text-[13px] text-warn flex items-start gap-1.5">
              <AlertTriangle className="lucide-inline" />
              <span>
                No GitHub login resolved for this instance, so it cannot tell whether it is
                on call. Enter one above, or authenticate the{' '}
                <span className="font-mono">gh</span> CLI.
              </span>
            </p>
          ) : null}
          {/* Conditioned on `strict_gating`, because the consequence inverts with it and
              only one half is a fault. Strict on: an unnamed instance is disarmed and idle.
              Strict off: `schedule_file._indeterminate` returns `on_shift=True`, so it arms
              anyway — and so does every other unnamed instance, which is the duplicate-claim
              shape the shared schedule exists to prevent. Both are worth saying; neither is
              the other. */}
          {roster.me && !roster.me_on_roster && roster.strict_gating ? (
            <p className="text-[13px] text-warn flex items-start gap-1.5">
              <AlertTriangle className="lucide-inline" />
              <span>
                <span className="font-mono">{roster.me}</span> is not named in any shift, so
                under strict gating this instance will never pick up work. Add it to a{' '}
                <span className="font-mono">who:</span> list, or correct the login above.
              </span>
            </p>
          ) : null}
          {roster.me && !roster.me_on_roster && !roster.strict_gating ? (
            <p className="text-[13px] text-muted flex items-start gap-1.5">
              <Info className="lucide-inline" />
              <span>
                <span className="font-mono">{roster.me}</span> is not named in any shift, and
                strict gating is off — so this instance picks up work regardless of the
                schedule, and does not defer to whoever is on call. Add it to a{' '}
                <span className="font-mono">who:</span> list if the rotation is meant to
                decide who responds.
              </span>
            </p>
          ) : null}
          {roster.error ? (
            <p className="text-[13px] text-danger flex items-start gap-1.5">
              <AlertTriangle className="lucide-inline" />
              <span>{roster.error}</span>
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  )
}

/**
 * Seconds as something an operator reads at 3am without doing arithmetic.
 *
 * Exported for the unit test: the whole point of this card is that the stored unit
 * (seconds) is not the unit a human reasons about, and 43200 silently misread as minutes
 * is exactly the confusion the card exists to remove.
 */
export function humanizeSecs(secs: number): string {
  if (!Number.isFinite(secs) || secs <= 0) return '—'
  if (secs < 60) return `${secs}s`
  if (secs < 3600) {
    const mins = secs / 60
    return `${Number.isInteger(mins) ? mins : mins.toFixed(1)} min`
  }
  const hours = secs / 3600
  return `${Number.isInteger(hours) ? hours : hours.toFixed(1)} h`
}

/**
 * Heartbeat pacing — the claim ceiling and the two release windows.
 *
 * `PUT /settings` has accepted all three for as long as they existed and no read path
 * returned any of them, so an operator who changed how long a dead investigation pins a
 * signal got no confirmation and no way to look the value up again. Worse for the
 * untouched case: the defaults governing every install were invisible, so "how long
 * before this gets picked up again" had no answer short of reading the source.
 *
 * Last in the panel deliberately. These are tuning knobs with correct defaults — an
 * operator setting the app up needs providers and autonomy, not this — and putting them
 * above the setup cards would imply they need attention.
 */
function HeartbeatCard({
  sweep,
  onSave,
  saving,
}: {
  sweep?: SweepWindows
  onSave: (updates: Record<string, unknown>) => void
  saving: boolean
}) {
  // Drafts in MINUTES, because the backend's seconds are a storage unit and nobody tunes a
  // 12-hour window by typing 43200. Re-seeded from the server while untouched, the same
  // shape as every other field here, so a background /state refresh cannot clobber typing.
  const serverStale = sweep?.stale_after_secs ?? 0
  const serverNeedsHuman = sweep?.needs_human_stale_after_secs ?? 0
  const [stale, setStale] = useState('')
  const [needsHuman, setNeedsHuman] = useState('')
  const [staleTouched, setStaleTouched] = useState(false)
  const [needsHumanTouched, setNeedsHumanTouched] = useState(false)
  useEffect(() => {
    if (!staleTouched) setStale(serverStale ? String(Math.round(serverStale / 60)) : '')
  }, [serverStale, staleTouched])
  useEffect(() => {
    if (!needsHumanTouched) {
      setNeedsHuman(serverNeedsHuman ? String(Math.round(serverNeedsHuman / 60)) : '')
    }
  }, [serverNeedsHuman, needsHumanTouched])

  // The backend refuses anything non-integer or <= 0, so the button is disabled rather
  // than letting the operator earn a 400 they have to interpret.
  const parseMins = (raw: string): number | null => {
    const mins = Number(raw.trim())
    if (!raw.trim() || !Number.isInteger(mins) || mins <= 0) return null
    return mins * 60
  }
  const staleSecs = parseMins(stale)
  const needsHumanSecs = parseMins(needsHuman)
  const staleDirty = staleTouched && staleSecs !== null && staleSecs !== serverStale
  const needsHumanDirty =
    needsHumanTouched && needsHumanSecs !== null && needsHumanSecs !== serverNeedsHuman

  const save = (key: string, secs: number | null, clear: () => void) => {
    if (secs === null || saving) return
    onSave({ [key]: secs })
    clear()
  }

  if (!sweep) {
    // Never substitute the defaults here. This gateway did not report them, and printing
    // 2 h against an instance that might be running 30 m would be a confident lie.
    return (
      <Card>
        <CardTitle>
          <Clock className="lucide-inline" /> Heartbeat pacing
        </CardTitle>
        <p className="text-[13px] text-muted">
          Not reported by this gateway, so the values in force cannot be shown.
        </p>
      </Card>
    )
  }

  return (
    <Card>
      <CardTitle>
        <Clock className="lucide-inline" /> Heartbeat pacing
      </CardTitle>
      <p className="text-[13px] text-muted mb-3">
        How much the heartbeat picks up at once, and how long it waits before handing work
        back. Releasing an incident does not resolve it — the signal becomes claimable
        again, so an investigation whose agent died gets retried instead of pinning the
        alarm forever.
      </p>

      <div className="flex flex-col gap-2">
        <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-stale-after">
          <span className="w-56 shrink-0 text-muted">Release an investigation after</span>
          <Input
            id="omc-stale-after"
            value={stale}
            inputMode="numeric"
            placeholder="120"
            onChange={(e) => {
              setStaleTouched(true)
              setStale(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                save('stale_after_secs', staleSecs, () => setStaleTouched(false))
              }
            }}
          />
          <span className="text-muted shrink-0">min</span>
          <SendBtn
            disabled={!staleDirty || saving}
            onClick={() => save('stale_after_secs', staleSecs, () => setStaleTouched(false))}
          >
            Save
          </SendBtn>
        </label>

        <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-needs-human-after">
          <span className="w-56 shrink-0 text-muted">Release a question after</span>
          <Input
            id="omc-needs-human-after"
            value={needsHuman}
            inputMode="numeric"
            placeholder="720"
            onChange={(e) => {
              setNeedsHumanTouched(true)
              setNeedsHuman(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                save('needs_human_stale_after_secs', needsHumanSecs, () =>
                  setNeedsHumanTouched(false),
                )
              }
            }}
          />
          <span className="text-muted shrink-0">min</span>
          <SendBtn
            disabled={!needsHumanDirty || saving}
            onClick={() =>
              save('needs_human_stale_after_secs', needsHumanSecs, () =>
                setNeedsHumanTouched(false),
              )
            }
          >
            Save
          </SendBtn>
        </label>
      </div>

      <p className="text-[12px] text-muted mt-2">
        An incident waiting on you gets the longer window — being asleep is not the same as
        an agent crashing.{' '}
        {sweep.needs_human_derived ? (
          <>
            Currently <span className="font-mono">{humanizeSecs(serverNeedsHuman)}</span>,
            derived from the window above, so it moves when that does. Setting it here pins
            it.
          </>
        ) : (
          <>
            Pinned at <span className="font-mono">{humanizeSecs(serverNeedsHuman)}</span>,
            independent of the window above.
          </>
        )}
      </p>

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px] mt-3">
        <dt className="text-muted">In force now</dt>
        <dd className="font-mono">
          {humanizeSecs(serverStale)} / {humanizeSecs(serverNeedsHuman)}
        </dd>
        <dt className="text-muted">New claims per heartbeat</dt>
        <dd className="font-mono">{sweep.max_claims_per_cycle}</dd>
      </dl>
    </Card>
  )
}

export default function SettingsPanel() {
  const queryClient = useQueryClient()

  const providersQuery = useQuery({
    queryKey: ['ops-mission-control', 'providers'],
    queryFn: () => opsApi.providers(),
  })

  const rotationQuery = useQuery({
    queryKey: ['ops-mission-control', 'rotation'],
    queryFn: () => opsApi.rotation(),
  })

  // Slack status rides on /state (it depends on live gateway state, not config
  // alone), so this reuses the board's query rather than adding an endpoint.
  const stateQuery = useQuery({
    queryKey: ['ops-mission-control', 'state'],
    queryFn: () => opsApi.state(),
  })

  const settingsMutation = useMutation({
    mutationFn: (updates: Record<string, unknown>) => opsApi.putSettings(updates),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'rotation'] })
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
    },
  })

  const providers = providersQuery.data?.providers ?? []
  const companions = stateQuery.data?.companions ?? EMPTY_COMPANIONS
  const mode: OperatingMode = rotationQuery.data?.mode ?? 'observe'

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardTitle>Autonomy</CardTitle>
        <p className="text-[13px] text-muted mb-3">
          The maximum this instance may do. A per-signal rule can narrow this, never widen
          it — so leaving it on Observe keeps the app read-only no matter what rules exist.
        </p>
        {/* collapse={false}: inside a Card, `> * { z-index: 1 }` traps the
            collapsed dropdown overlay beneath the rows below it. */}
        <SegmentedControl
          segments={MODE_SEGMENTS}
          value={mode}
          onChange={(value) => settingsMutation.mutate({ mode: value })}
          layoutId="omc-mode"
          collapse={false}
        />
        <p className="text-[13px] text-muted mt-3">{MODE_HELP[mode]}</p>
        {mode === 'act' ? (
          <p className="text-[13px] text-warn mt-2 flex items-start gap-1.5">
            <ShieldAlert className="lucide-inline" />
            <span>
              Act only takes effect for signals matched by a rule you have written. With no
              rules, this behaves like Propose.
            </span>
          </p>
        ) : null}
        {rotationQuery.data && rotationQuery.data.rules === 0 && mode === 'act' ? (
          <p className="text-[12px] text-muted mt-1">No rules defined yet.</p>
        ) : null}
        {settingsMutation.isError ? (
          <p className="text-[12px] text-danger mt-2">
            {(settingsMutation.error as Error).message}
          </p>
        ) : null}
      </Card>

      <Card>
        <CardTitle>Providers</CardTitle>
        <p className="text-[13px] text-muted">
          Turn on the systems you want watched. AWS uses your existing credentials — no key
          is stored. Tokens for other providers are kept where the agent cannot read them.
        </p>
        {providersQuery.isLoading ? (
          <p className="text-sm text-muted mt-2">Loading…</p>
        ) : (
          providers.map((p) => <ProviderRow key={p.id} provider={p} />)
        )}

        {/* Shown only when a companion IS installed. A public install has none and
            should not be told about an extension point it is not using. When one is
            installed but its adapters are absent above, that gap is the signal that
            it was rejected at admission — which is why this is reported at all. */}
        {companions.length > 0 ? (
          <div className="border-t border-border pt-3 mt-3">
            <p className="text-[12px] text-muted">
              Adapter package{companions.length === 1 ? '' : 's'} installed:{' '}
              {companions.map((c) => c.name).join(', ')}. Their sources appear in the list
              above once admitted; if they do not, the fleet admission policy rejected
              them — check the gateway log.
            </p>
          </div>
        ) : null}
      </Card>

      <SlackOutCard
        status={stateQuery.data?.slack}
        onSave={(updates) => settingsMutation.mutate(updates)}
        saving={settingsMutation.isPending}
      />

      {/* Immediately after Slack, because they are the two output channels and the
          difference between them is the point: this one needs no workspace and no token,
          so it works on an install where the Slack card cannot. */}
      <NotifyOutCard
        status={stateQuery.data?.notify}
        onSave={(updates) => settingsMutation.mutate(updates)}
      />

      {/* Sync status rides on /state for the same reason Slack's does — it reflects live
          repo state (is there a .git yet, does a tracked file hold conflict markers), not
          config alone — so this reuses the board's query rather than adding an endpoint. */}
      <SharedMemoryCard
        status={stateQuery.data?.ledger_sync}
        onSave={(updates) => settingsMutation.mutate(updates)}
        saving={settingsMutation.isPending}
      />

      {/* Immediately after, because the schedule lives INSIDE that repo and only reads
          correctly as a consequence of it. */}
      <OnCallScheduleCard
        provider={providers.find((p) => p.id === 'schedule-file')}
        roster={rotationQuery.data?.roster}
        syncReady={Boolean(stateQuery.data?.ledger_sync?.ready)}
      />

      <Card>
        <CardTitle>Instance</CardTitle>
        {/* Not a <label>: Toggle renders a role="switch" div, not a form
            control, so a label has nothing to associate with. The switch
            carries its own accessible name via `label`. */}
        <div className="flex items-center gap-2 text-[13px]">
          <Toggle
            checked={Boolean(rotationQuery.data?.primary)}
            onChange={(v) => settingsMutation.mutate({ primary_instance: v })}
            label="Run nightly ledger maintenance on this instance"
          />
          <span>Run nightly ledger maintenance on this instance</span>
        </div>
        <p className="text-[12px] text-muted mt-2">
          Leave this on if you are the only one running Ops Mission Control. On a team
          sharing one ledger, exactly one instance should be primary so the maintenance pass
          does not run once per person.
        </p>
      </Card>

      {/* Last: correct out of the box, so it must not compete with the cards an operator
          actually has to touch to get the app watching anything. */}
      <HeartbeatCard
        sweep={rotationQuery.data?.sweep}
        onSave={(updates) => settingsMutation.mutate(updates)}
        saving={settingsMutation.isPending}
      />
    </div>
  )
}
