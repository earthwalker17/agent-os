import { useState, useRef, useEffect } from 'react'
import type { Message, PendingExecution } from '../types'
import RunDetailModal from './RunDetailModal'
import RunChatCard from './RunChatCard'

interface Props {
  projectId: string | null
  conversationId: string | null
  messages: Message[]
  onSend: (message: string) => void
  loading?: boolean
  headerLabel?: string | null
  /**
   * Real project id for run-detail navigation, or null when the chat is the
   * GENERAL workspace. Distinct from `projectId` which is also used as the
   * "is the chat open" sentinel and can be the literal string "general".
   */
  runProjectId?: string | null
  /** Confirm a pending execution plan — dispatches the stored task card. */
  onConfirmPending?: (pendingExecutionId: string) => void
  /** Enter "revise mode" for a pending plan; next chat send revises it. */
  onRevisePending?: (pending: PendingExecution) => void
  /** Currently active revise-mode target (null = no pending revision). */
  revisingPendingId?: string | null
  /** Human-readable title for the plan being revised; used in the banner. */
  revisingPendingTitle?: string | null
  /** Drop out of revise mode without sending. */
  onCancelRevise?: () => void
  /** Task 06.2D — notify parent that a run/preview changed so the Runs panel refreshes. */
  onRunsChanged?: () => void
}

/** Minimal markdown-to-HTML for orchestration responses. */
function renderMarkdown(text: string): string {
  return text
    // headings
    .replace(/^### (.+)$/gm, '<h4 class="orch-h3">$1</h4>')
    .replace(/^## (.+)$/gm, '<h3 class="orch-h2">$1</h3>')
    // bold
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    // unordered list items
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    // ordered list items
    .replace(/^(\d+)\. (.+)$/gm, '<li>$2</li>')
    // blockquote
    .replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>')
    // paragraphs: double newline
    .replace(/\n\n/g, '<br/><br/>')
    // single newlines within content
    .replace(/\n/g, '<br/>')
}

// Backend `run_store.new_run_id()` format: YYYYMMDD-HHMMSS-XXXXXXXX (8 hex
// chars). We pin to exactly this shape so the affordance only ever appears
// for genuine Coding Agent placeholders, not for arbitrary backticked text
// in normal orchestrator replies.
const RUN_ID_RE = /\*\*Run ID:\*\*\s*`(\d{8}-\d{6}-[0-9a-f]{8})`/i

function extractRunId(content: string): string | null {
  const m = content.match(RUN_ID_RE)
  return m ? m[1] : null
}

function ChatPanel({
  projectId,
  conversationId,
  messages,
  onSend,
  loading,
  headerLabel,
  runProjectId,
  onConfirmPending,
  onRevisePending,
  revisingPendingId,
  revisingPendingTitle,
  onCancelRevise,
  onRunsChanged,
}: Props) {
  const [input, setInput] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [openRunId, setOpenRunId] = useState<string | null>(null)
  const [showTaskCardFor, setShowTaskCardFor] = useState<string | null>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // If the user navigates away from a project that had a modal open, clear it.
  useEffect(() => {
    if (!runProjectId) setOpenRunId(null)
  }, [runProjectId])

  // When the user enters revise mode, focus the input so they can start
  // typing the revision instructions immediately.
  useEffect(() => {
    if (revisingPendingId) inputRef.current?.focus()
  }, [revisingPendingId])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = input.trim()
    if (!trimmed || !conversationId || loading) return
    onSend(trimmed)
    setInput('')
  }

  const label = headerLabel || projectId

  if (!projectId) {
    return (
      <main className="chat-panel">
        <div className="chat-empty">Select a workspace to start chatting</div>
      </main>
    )
  }

  if (!conversationId) {
    return (
      <main className="chat-panel">
        <div className="chat-header">
          <h2>{label}</h2>
        </div>
        <div className="chat-empty">Select or create a conversation to start chatting</div>
      </main>
    )
  }

  return (
    <main className="chat-panel">
      <div className="chat-header">
        <h2>{label}</h2>
      </div>
      <div className="chat-messages">
        {messages.length === 0 && (
          <div className="chat-empty">No messages yet. Start the conversation.</div>
        )}
        {messages.map((msg, i) => {
          // Task 06.2D — a run id in message metadata drives the chat-first run
          // follow-up card (live status + browser verification controls). Older
          // messages without metadata fall back to the legacy regex affordance.
          const runIdFromMeta =
            msg.role === 'assistant'
              ? (msg.metadata as { run_id?: string } | null | undefined)?.run_id
              : undefined
          const runId =
            msg.role === 'assistant' && runProjectId && !runIdFromMeta
              ? extractRunId(msg.content)
              : null
          const pending = msg.role === 'assistant' ? msg.pending_execution ?? null : null
          const isPendingActionable =
            pending && pending.status === 'pending' && !!onConfirmPending && !!onRevisePending
          const isPendingDispatched = pending && pending.status === 'dispatched'
          const isBeingRevised = pending && pending.pending_execution_id === revisingPendingId
          const taskCardKey = pending?.pending_execution_id ?? null
          const taskCardOpen = taskCardKey !== null && showTaskCardFor === taskCardKey
          return (
            <div key={msg.id || i} className={`chat-message ${msg.role}`}>
              <div className="chat-message-role">{msg.role === 'user' ? 'You' : 'Agent OS'}</div>
              {msg.role === 'assistant' ? (
                <div
                  className="chat-message-content orch-response"
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content) }}
                />
              ) : (
                <div className="chat-message-content">{msg.content}</div>
              )}
              {runIdFromMeta && runProjectId && (
                <RunChatCard
                  projectId={runProjectId}
                  runId={runIdFromMeta}
                  onOpenRun={(rid) => setOpenRunId(rid)}
                  onRunsChanged={onRunsChanged}
                />
              )}
              {runId && (
                <div className="chat-run-affordance-row">
                  <button
                    type="button"
                    className="chat-run-affordance"
                    onClick={() => setOpenRunId(runId)}
                    title={`Open run ${runId}`}
                  >
                    View Run
                  </button>
                </div>
              )}
              {pending && isPendingActionable && !isBeingRevised && (
                <div className="pending-execution-actions">
                  <button
                    type="button"
                    className="pending-confirm-btn"
                    onClick={() => onConfirmPending && onConfirmPending(pending.pending_execution_id)}
                    disabled={loading}
                  >
                    OK, run this
                  </button>
                  <button
                    type="button"
                    className="pending-revise-btn"
                    onClick={() => onRevisePending && onRevisePending(pending)}
                    disabled={loading}
                  >
                    Revise plan
                  </button>
                  <button
                    type="button"
                    className="pending-inspect-btn"
                    onClick={() =>
                      setShowTaskCardFor(taskCardOpen ? null : pending.pending_execution_id)
                    }
                    title="Show the full task card the Coding Agent will receive"
                  >
                    {taskCardOpen ? 'Hide task card' : 'Show task card'}
                  </button>
                </div>
              )}
              {pending && isBeingRevised && (
                <div className="pending-revising-hint">
                  Revising this plan — type your changes in the input below.
                </div>
              )}
              {isPendingDispatched && pending && pending.run_id && !runIdFromMeta && (
                <div className="chat-run-affordance-row">
                  <button
                    type="button"
                    className="chat-run-affordance"
                    onClick={() => setOpenRunId(pending.run_id || null)}
                    title={`Open run ${pending.run_id}`}
                  >
                    View Run
                  </button>
                </div>
              )}
              {pending && taskCardOpen && (
                <pre className="pending-task-card">
                  {pending.task_card}
                </pre>
              )}
            </div>
          )
        })}
        {loading && (
          <div className="chat-message assistant">
            <div className="chat-message-role">Agent OS</div>
            <div className="chat-message-content chat-thinking">Thinking...</div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>
      {revisingPendingId && (
        <div className="revise-mode-banner">
          <span>
            Revising plan: <strong>{revisingPendingTitle || revisingPendingId}</strong>
            . Type your revision instructions and send.
          </span>
          <button
            type="button"
            className="revise-mode-cancel"
            onClick={() => onCancelRevise && onCancelRevise()}
            disabled={loading}
          >
            Cancel
          </button>
        </div>
      )}
      <form className="chat-input" onSubmit={handleSubmit}>
        <input
          ref={inputRef}
          type="text"
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder={revisingPendingId ? 'Describe what to change about the plan...' : 'Type a message...'}
          autoFocus
          disabled={loading}
        />
        <button type="submit" disabled={loading}>
          {revisingPendingId ? 'Send revision' : 'Send'}
        </button>
      </form>
      {openRunId && runProjectId && (
        <RunDetailModal
          projectId={runProjectId}
          runId={openRunId}
          onClose={() => setOpenRunId(null)}
        />
      )}
    </main>
  )
}

export default ChatPanel
