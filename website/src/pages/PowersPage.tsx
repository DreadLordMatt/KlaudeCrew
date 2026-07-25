import { PageHeader } from '../components/ui'
import PowersTab from './overview/PowersTab'

/**
 * Powers app surface (`/powers`, Apps group in the left rail).
 *
 * Powers live under Apps rather than as an Agent Capabilities tab because a
 * Power is an installable unit with its own browsable catalog — closer to the
 * app grid than to the per-agent configuration surfaces (Skills, Hooks, MCP)
 * that Capabilities hosts. The body is the same component either way.
 */
export default function PowersPage() {
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title="Powers"
        subtitle="Installable capability bundles — browse the registry, install to disk, remove when done"
      />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <PowersTab />
      </div>
    </div>
  )
}
