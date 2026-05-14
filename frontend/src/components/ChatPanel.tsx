import { useState, useRef, useEffect } from 'react'
import type { Message } from '../types'
import RunDetailModal from './RunDetailModal'

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
}: Props) {
  const [input, setInput] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const [openRunId, setOpenRunId] = useState<string | null>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // If the user navigates away from a project that had a modal open, clear it.
  useEffect(() => {
    if (!runProjectId) setOpenRunId(null)
  }, [runProjectId])

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
          const runId =
            msg.role === 'assistant' && runProjectId ? extractRunId(msg.content) : null
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
      <form className="chat-input" onSubmit={handleSubmit}>
        <input
          type="text"
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Type a message..."
          autoFocus
          disabled={loading}
        />
        <button type="submit" disabled={loading}>Send</button>
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
