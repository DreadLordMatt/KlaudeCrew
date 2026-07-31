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
 * `node scripts/i18n-codemod.mjs --merge` extracts **88 ops keys** cleanly. The
 * blocker is that it is a whole-corpus pass: the same run rewrote **14 files this
 * app does not own** (ChatPage, ComputerUsePanel, ChatInput, SkillsTab, …) and added
 * **58 keys belonging to parallel in-flight work**. Extracting this app therefore
 * cannot be separated from someone else's uncommitted changes, and each of the 88
 * keys then needs a value in all 10 shipped locales or `catalogParity.test.ts`
 * fails — so it also needs a real translation pass, via
 * `scripts/i18n-shard.mjs split/join` (never hand-assembled; `join` is fail-closed
 * precisely to stop English shipping disguised as a translation).
 *
 * Do this as its own change, on a quiet tree, not folded into an app feature. Do
 * NOT hand-edit `en.json` to paper over it — it is generated. Note the codemod
 * refuses a non-`--merge` run when the corpus is already converted, which is what
 * stops a rebuild from silently wiping 3906 keys.
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Eye,
  MessageSquare,
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
  type OperatingMode,
  type ProviderInfo,
  type SlackOutStatus,
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

  return (
    <div className="border-t border-border py-3">
      <div className="flex items-center gap-2 mb-1">
        <Toggle
          checked={enabled}
          onChange={(v) => configMutation.mutate({ enabled: v })}
          label={`Enable ${provider.display_name}`}
        />
        <span className="text-sm font-medium">{provider.display_name}</span>
        <Badge variant={provider.configured ? 'ok' : 'muted'}>
          {provider.configured ? 'ready' : 'not set up'}
        </Badge>
        <span className="text-[12px] text-muted ml-auto">{provider.roles.join(' · ')}</span>
      </div>
      <p className="text-[12px] text-muted mb-2">{provider.detail}</p>

      {enabled ? (
        <div className="flex flex-col gap-2 pl-11">
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

          {configMutation.isError || secretMutation.isError ? (
            <p className="text-[12px] text-danger">
              {((configMutation.error ?? secretMutation.error) as Error)?.message}
            </p>
          ) : null}
        </div>
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
    </div>
  )
}
