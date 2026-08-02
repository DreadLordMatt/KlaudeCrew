import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'

/**
 * Regression: a pending approval must be answerable in BOTH states.
 *
 * The approval buttons used to render only when the group was collapsed
 * (`!expanded`). But a group with a live pending approval AUTO-EXPANDS while the
 * turn is running, so the one turn that was waiting on the user was the one turn
 * they could not answer — the agent sat parked with no visible way to unblock it,
 * and nothing failed. Found while watching a real ops investigation stall on a
 * read-only AWS probe inside the embedded incident chat.
 *
 * `ChatMessageList.test.tsx` cannot catch this: it mocks CollapsibleToolGroup out
 * entirely, so the expanded/collapsed branch is never exercised there.
 */
describe('CollapsibleToolGroup — pending approval', () => {
  const renderGroup = (props: Record<string, unknown> = {}) =>
    render(
      <CollapsibleToolGroup
        count={2}
        hasPermission
        isRunning
        pendingPermCount={1}
        permissionMeta={{ approval_id: 'abc-123', tool_input: '{"command":"aws sts get-caller-identity"}' }}
        onApprove={vi.fn()}
        {...props}
      >
        <div>tool call child</div>
      </CollapsibleToolGroup>,
    )

  it('offers Approve / Trust / Reject when auto-expanded during a running turn', () => {
    // autoExpand is what a live pending-approval group actually does.
    renderGroup({ autoExpand: true })
    expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /trust/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /reject/i })).toBeTruthy()
  })

  it('still offers them when collapsed', () => {
    renderGroup({ autoExpand: false })
    expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /reject/i })).toBeTruthy()
  })

  it('hides Trust when the host cannot persist a trust grant', () => {
    // `ChatEmbed` maps every `trust*` decision onto a one-shot `approve` because it has
    // no session-scoped trust store. Showing the button there means the operator grants
    // "trust", is re-prompted on the very next identical call, and has no way to tell
    // that the grant never existed — a label promising something the host cannot do.
    // Approve and Reject must survive: the point is to remove the dishonest affordance,
    // not the ability to answer.
    renderGroup({ autoExpand: true, canPersistTrust: false })
    expect(screen.queryByRole('button', { name: /trust/i })).toBeNull()
    expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /reject/i })).toBeTruthy()
  })

  it('shows Trust by default, so the main chat is unaffected', () => {
    // The prop defaults to true precisely so this change cannot silently remove the
    // button from the dashboard chat, which DOES have a trust store.
    renderGroup({ autoExpand: true })
    expect(screen.getByRole('button', { name: /trust/i })).toBeTruthy()
  })

  it('resets the card when the approval POST fails, instead of showing a false Approved', async () => {
    /**
     * `submitDecision` optimistically marks the card approved and hides the buttons, then
     * relies on the promise `onApprove` returns to reject so it can undo that. A handler that
     * returns void (react-query's `mutate()`, which is what `ChatEmbed` used) swallows the
     * rejection: the card said "Approved", the buttons vanished, and the agent stayed parked
     * on a decision that never reached it — silent, and with no way to retry. Found in review.
     *
     * This asserts the CONTRACT the fix depends on, which is why it lives here rather than in
     * ChatEmbed's suite: that suite mocks ChatMessageList out entirely, so no real approval
     * button is ever rendered there.
     */
    const onApprove = vi.fn().mockRejectedValue(new Error('gateway said no'))
    // The component logs the failure on purpose; keep the test output clean.
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      renderGroup({ autoExpand: true, onApprove })
      fireEvent.click(screen.getByRole('button', { name: /approve/i }))

      await waitFor(() => expect(onApprove).toHaveBeenCalledWith('approved'))
      // Answerable again: the optimistic state was rolled back.
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /approve/i })).not.toBeDisabled(),
      )
      expect(screen.getByRole('button', { name: /reject/i })).toBeTruthy()
    } finally {
      consoleError.mockRestore()
    }
  })

  it('keeps the card resolved when the approval succeeds', async () => {
    // The converse, so the rollback cannot be "always reset" and still pass.
    const onApprove = vi.fn().mockResolvedValue(undefined)
    renderGroup({ autoExpand: true, onApprove })
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))

    await waitFor(() => expect(onApprove).toHaveBeenCalledWith('approved'))
    // No rollback on the happy path — the buttons stay gone.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /reject/i })).toBeNull(),
    )
  })

  it('renders no approval buttons when there is nothing pending', () => {
    renderGroup({ hasPermission: false, pendingPermCount: 0, autoExpand: true })
    expect(screen.queryByRole('button', { name: /^approve$/i })).toBeNull()
  })

  it('renders no approval buttons without an onApprove handler', () => {
    // A host that cannot resolve approvals must not show buttons that do nothing.
    renderGroup({ onApprove: undefined, autoExpand: true })
    expect(screen.queryByRole('button', { name: /^approve$/i })).toBeNull()
  })
})
