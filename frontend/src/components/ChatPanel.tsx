import { useState, useRef, useEffect } from 'react'
import type { Message } from '../types'

interface Props {
  projectId: string | null
  conversationId: string | null
  messages: Message[]
  onSend: (message: string) => void
  loading?: boolean
  headerLabel?: string | null
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

function ChatPanel({ projectId, conversationId, messages, onSend, loading, headerLabel }: Props) {
  const [input, setInput] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

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
        {messages.map((msg, i) => (
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
          </div>
        ))}
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
    </main>
  )
}

export default ChatPanel
