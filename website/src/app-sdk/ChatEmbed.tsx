/**
 * ChatEmbed — embeddable chat widget using KiroCrew's native rendering.
 *
 * Uses ChatMessageList (shared with ChatPage) for message rendering.
 * Manages its own state via useAppApi() + React Query. No Redux dependency.
 *
 * State management: polling via useQuery refetchInterval.
 * Poll faster during streaming (1s), slower when idle (5s).
 */
import { useState, useRef, useCallback, useEffect } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { ArrowUp, Loader2 } from 'lucide-react'
import ChatMessageList from './ChatMessageList'
import { useAppApi } from './index'
import type { ChatMessage } from '../types'

import { i18nT } from '../i18n/t'
export interface ChatEmbedProps {
  slotKey: string
  agent?: string
  placeholder?: string
}

/** Minimal shape of the chat-slot payload consumed by this embed. */
interface ChatSlotData {
  messages?: ChatMessage[]
  running?: boolean
  title?: string
}

function ChatEmbed({ slotKey, agent, placeholder }: ChatEmbedProps) {
  const api = useAppApi()
  const [input, setInput] = useState('')
  const endRef = useRef<HTMLDivElement>(null)
  const lastHashRef = useRef('')

  const { data: slotData } = useQuery({
    queryKey: ['app-sdk-embed', slotKey],
    queryFn: () => api.get<ChatSlotData>('/api/chat/slots/' + encodeURIComponent(slotKey)),
    refetchInterval: (query) => {
      const running = query.state.data?.running ?? false
      return running ? 1000 : 5000
    },
  })

  const messages = slotData?.messages ?? []
  const running = slotData?.running ?? false
  const title = slotData?.title ?? ''

  // Auto-scroll when new messages arrive
  const msgHash = messages.length + ':' + (messages[messages.length - 1]?.content?.length || 0)
  useEffect(() => {
    if (msgHash !== lastHashRef.current) {
      lastHashRef.current = msgHash
      endRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [msgHash])

  const sendMutation = useMutation({
    mutationFn: (msg: string) =>
      api.post('/api/chat', { message: msg, slot: slotKey, agent: agent || '' })
        .catch((err) => {
          // POST /api/chat returns SSE — JSON parse fails, expected.
          if (err instanceof SyntaxError) return
          throw err
        }),
  })

  const send = useCallback(() => {
    const msg = input.trim()
    if (!msg) return
    setInput('')
    sendMutation.mutate(msg)
  }, [input, sendMutation])

  /**
   * Resolve a tool-approval request from inside the embed.
   *
   * Without this the approval card RENDERS but its buttons do nothing, so an
   * embedded agent that asks permission (e.g. an ops investigation wanting to run
   * a read-only probe) stalls forever with no way to answer it — and the stall is
   * silent, because the card looks interactive.
   *
   * `trust*` decisions map to `approve`: the embed has no session-scoped trust
   * store of its own, so the honest behavior is to allow this one call rather than
   * to imply a persistent grant the embed cannot make. Anything else rejects, so an
   * unrecognized decision fails CLOSED — the safe direction when the alternative is
   * running a tool the operator did not sanction.
   *
   * Requires `/api/approvals/*` in the host app's `allowedApiPaths` — the scoped
   * API throws on an undeclared path, which would otherwise surface only here.
   * `POST /api/approvals/{id}/{action}` accepts exactly `approve` or `reject`
   * (`handlers/sessions.py::api_approval_resolve`); anything else is a 400.
   */
  const approveMutation = useMutation({
    mutationFn: ({ approvalId, decision }: { approvalId: string; decision: string }) => {
      const action =
        decision === 'approved' || decision.startsWith('trust') ? 'approve' : 'reject'
      return api
        .post(`/api/approvals/${encodeURIComponent(approvalId)}/${action}`, {})
        .catch((err) => {
          // Some resolve responses are empty/non-JSON — the same expected parse
          // failure the send path above tolerates.
          if (err instanceof SyntaxError) return
          throw err
        })
    },
  })

  const handleApprove = useCallback(
    (approvalId: string, decision: string) => {
      approveMutation.mutate({ approvalId, decision })
    },
    [approveMutation],
  )

  return (
    <div className="flex flex-col h-full min-h-0 border border-border rounded-lg overflow-hidden bg-bg">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-card shrink-0">
        <span className={`w-2 h-2 rounded-full shrink-0 ${running ? 'bg-ok animate-pulse' : 'bg-accent'}`} />
        <span className="text-[13px] font-semibold text-text-strong truncate flex-1">{title || slotKey}</span>
        {agent && <span className="text-[10px] font-mono text-muted">{agent}</span>}
        {running && <span className="text-[10px] text-ok font-mono">{i18nT('appSdk.chatEmbed.streaming')}</span>}
      </div>

      <div className="flex-1 overflow-y-auto py-4 min-h-0">
        {messages.length === 0 && !running && (
          <div className="text-center text-muted text-[13px] py-10">{i18nT('appSdk.chatEmbed.session_ready_type_a_message_to_start')}</div>
        )}
        <ChatMessageList messages={messages} running={running} onApprove={handleApprove} />
        <div ref={endRef} />
      </div>

      <div className="flex items-center gap-2 px-3 py-2 border-t border-border bg-bg-subtle shrink-0">
        <input
          type="text"
          aria-label={i18nT('appSdk.chatEmbed.chat_message')}
          className="flex-1 px-3 py-2 text-sm bg-bg-elevated border border-border rounded-md text-text outline-none focus:border-accent transition-colors"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && input.trim()) { e.preventDefault(); send() } }}
          placeholder={running ? i18nT('appSdk.chatEmbed.agent_is_working') : (placeholder || i18nT('appSdk.chatEmbed.message'))}
          disabled={sendMutation.isPending}
        />
        <button
          className="p-2 rounded-md bg-accent text-accent-fg disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-80 transition-opacity"
          onClick={send}
          disabled={sendMutation.isPending || !input.trim()}
          title={i18nT('appSdk.chatEmbed.send')}
          aria-label={i18nT('appSdk.chatEmbed.send_message')}
        >
          {sendMutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <ArrowUp size={16} />}
        </button>
      </div>
    </div>
  )
}

export default ChatEmbed
