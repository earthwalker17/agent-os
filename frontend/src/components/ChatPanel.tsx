import { useState, useRef, useEffect, useCallback } from 'react'
import type { Message, PendingExecution, ChatAttachment, ProviderInfo, ModelInfo } from '../types'
import RunDetailModal from './RunDetailModal'
import RunChatCard from './RunChatCard'
import RunTrace from './RunTrace'
import ModelPicker from './ModelPicker'

interface Props {
  projectId: string | null
  conversationId: string | null
  messages: Message[]
  /** Send a message with optional uploaded attachments (Task 07.0). */
  onSend: (message: string, attachments?: ChatAttachment[]) => void
  loading?: boolean
  headerLabel?: string | null
  /**
   * Real project id for run-detail navigation, or null when the chat is the
   * GENERAL workspace. Distinct from `projectId` which is also used as the
   * "is the chat open" sentinel and can be the literal string "general".
   * Also gates the "Add to workspace too" upload option — only real projects
   * have an execution workspace to copy into.
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
  /** Phase 6 — re-fetch the conversation messages (e.g. after a recovery plan is proposed). */
  onMessagesChanged?: () => void
  /** Task 07.1 — model providers + current selection for the header dropdown. */
  providers?: ProviderInfo[]
  selectedProvider?: string
  onSelectProvider?: (providerId: string) => void
  /**
   * Provider Registry 2.0 — the selected provider's capability-tagged models +
   * current model selection for the composer-side model picker. Drives the
   * per-model image-upload gating (a text-only model can't attach images).
   */
  models?: ModelInfo[]
  selectedModel?: string
  onSelectModel?: (modelId: string) => void
  /** Task 07.2 — active color theme + setter for the top-right theme dropdown. */
  theme?: 'dark' | 'light'
  onSelectTheme?: (theme: 'dark' | 'light') => void
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

// Max composer height before it scrolls instead of growing further.
const MAX_TEXTAREA_HEIGHT = 200

// Accepted upload types, mirroring the backend allow-list (uploads.py).
const ACCEPT_TYPES = 'image/*,.txt,.md,.pdf,.doc,.docx'
// Provider Registry 2.0 — when the selected model can't read images, restrict
// the file picker to non-image document types so images can't be attached.
const ACCEPT_TYPES_NO_IMAGE = '.txt,.md,.pdf,.doc,.docx'

function isImage(mime: string | undefined): boolean {
  return !!mime && mime.startsWith('image/')
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// Web Speech API is non-standard and unprefixed only in some browsers; reach
// for either spelling and treat its absence as "voice unsupported".
function getSpeechRecognition(): (new () => SpeechRecognition) | null {
  const w = window as unknown as {
    SpeechRecognition?: new () => SpeechRecognition
    webkitSpeechRecognition?: new () => SpeechRecognition
  }
  return w.SpeechRecognition || w.webkitSpeechRecognition || null
}

// Chrome streams audio to a remote speech service, so a flaky connection throws
// a transient "network" error. Auto-retry a couple times before giving up.
const MAX_VOICE_NETWORK_RETRIES = 2

// Append dictated text to whatever is already there, with a single separating
// space (and never a leading space on the addition).
function joinTranscript(base: string, addition: string): string {
  const add = addition.replace(/^\s+/, '')
  if (!add) return base
  if (!base) return add
  return /\s$/.test(base) ? base + add : `${base} ${add}`
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
  onMessagesChanged,
  providers,
  selectedProvider,
  onSelectProvider,
  models,
  selectedModel,
  onSelectModel,
  theme,
  onSelectTheme,
}: Props) {
  const [input, setInput] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [openRunId, setOpenRunId] = useState<string | null>(null)
  const [openTraceId, setOpenTraceId] = useState<string | null>(null)
  const [showTaskCardFor, setShowTaskCardFor] = useState<string | null>(null)

  // Task 07.0 — composer attachment + voice state.
  const [files, setFiles] = useState<File[]>([])
  const [addToWorkspace, setAddToWorkspace] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  // Provider Registry 2.0 — transient note shown when image attachments are
  // blocked/removed because the selected model can't process images.
  const [imageBlockedNote, setImageBlockedNote] = useState<string | null>(null)
  const [recording, setRecording] = useState(false)
  const [voiceError, setVoiceError] = useState<string | null>(null)
  const recognitionRef = useRef<SpeechRecognition | null>(null)
  // Refs mirror live recording state for use inside recognition callbacks
  // (which capture a stale closure), and carry the dictation session's text.
  const recordingRef = useRef(false)
  const manualStopRef = useRef(false)
  const baseTextRef = useRef('')         // input text when this session started
  const finalTextRef = useRef('')        // finalized transcript so far this session
  const networkErrorRef = useRef(false)  // a "network" error fired this session
  const networkRetryRef = useRef(0)

  // Real project conversations get the "add to workspace" option; GENERAL does not.
  const isProjectConversation = !!runProjectId
  const voiceSupported = getSpeechRecognition() !== null

  // Provider Registry 2.0 — does the selected model accept image input? Defaults
  // to allowed when the capability is not yet known (providers still loading) so
  // existing behavior is preserved until the model list arrives.
  const selectedModelInfo = models?.find(m => m.id === selectedModel)
  const visionEnabled = selectedModelInfo ? selectedModelInfo.vision : true

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // If the user navigates away from a project that had a modal open, clear it.
  useEffect(() => {
    if (!runProjectId) {
      setOpenRunId(null)
      setOpenTraceId(null)
    }
  }, [runProjectId])

  // When the user enters revise mode, focus the input so they can start
  // typing the revision instructions immediately.
  useEffect(() => {
    if (revisingPendingId) inputRef.current?.focus()
  }, [revisingPendingId])

  // Auto-grow the textarea to fit its content, up to a cap.
  const resizeInput = useCallback(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`
  }, [])

  useEffect(() => {
    resizeInput()
  }, [input, resizeInput])

  // Switching conversations clears the composer's pending attachments + voice
  // state so files don't bleed across threads.
  useEffect(() => {
    setFiles([])
    setUploadError(null)
    setVoiceError(null)
    setImageBlockedNote(null)
    manualStopRef.current = true
    if (recognitionRef.current) {
      try { recognitionRef.current.abort() } catch { /* already stopped */ }
    }
    recordingRef.current = false
    setRecording(false)
  }, [conversationId])

  // The "add to workspace" toggle is meaningless outside a project; keep it off.
  useEffect(() => {
    if (!isProjectConversation) setAddToWorkspace(false)
  }, [isProjectConversation])

  // Provider Registry 2.0 — if the user switches to a text-only model while
  // image files are staged, drop the images (a text-only model can't read them)
  // and note it. Non-image attachments are untouched.
  useEffect(() => {
    if (visionEnabled) {
      // Back on a vision model — clear any lingering "images blocked" note.
      setImageBlockedNote(null)
      return
    }
    if (files.some(f => f.type.startsWith('image/'))) {
      setFiles(prev => prev.filter(f => !f.type.startsWith('image/')))
      setImageBlockedNote(
        "Removed attached image(s) — the selected model can't read images.",
      )
    }
  }, [visionEnabled, files])

  const handleFilesPicked = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files ? Array.from(e.target.files) : []
    if (picked.length) {
      // Gate images out when the selected model is text-only.
      const allowed = visionEnabled
        ? picked
        : picked.filter(f => !f.type.startsWith('image/'))
      if (allowed.length < picked.length) {
        setImageBlockedNote(
          "Images can't be attached — the selected model doesn't read images.",
        )
      } else {
        setImageBlockedNote(null)
      }
      if (allowed.length) {
        setFiles(prev => [...prev, ...allowed])
        setUploadError(null)
      }
    }
    // Reset so picking the same file again re-fires onChange.
    e.target.value = ''
  }

  const removeFile = (index: number) => {
    setFiles(prev => prev.filter((_, i) => i !== index))
  }

  const setRecordingState = useCallback((val: boolean) => {
    recordingRef.current = val
    setRecording(val)
  }, [])

  // Spin up one recognition session. We run in continuous + interim mode so
  // text appears live and the engine keeps listening through natural pauses;
  // when a session ends on its own (silence/timeout) we transparently restart
  // it, so the only thing that actually stops recording is the user (or a
  // hard error). Returns false if the session couldn't be created.
  const startVoiceSession = useCallback((): boolean => {
    const SR = getSpeechRecognition()
    if (!SR) {
      setVoiceError('Voice input is not supported in this browser.')
      return false
    }
    const recognition = new SR()
    recognition.lang = 'en-US'
    recognition.continuous = true
    recognition.interimResults = true

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      // `results` is cumulative for the session: rebuild final + interim each
      // time so the textarea reflects the live transcript as it's spoken.
      let finalText = ''
      let interimText = ''
      for (let i = 0; i < event.results.length; i++) {
        const result = event.results[i]
        if (result.isFinal) finalText += result[0].transcript
        else interimText += result[0].transcript
      }
      finalTextRef.current = finalText
      setInput(joinTranscript(baseTextRef.current, finalText + interimText))
    }

    recognition.onerror = (event: SpeechRecognitionErrorEvent) => {
      // 'no-speech' (a quiet pause) and 'aborted' (our own stop) are benign —
      // let onend decide whether to keep listening.
      if (event.error === 'no-speech' || event.error === 'aborted') return
      if (event.error === 'network') {
        // Transient: Chrome lost its connection to the speech service. Flag it
        // so onend can retry; only surface a message once retries are spent.
        networkErrorRef.current = true
        return
      }
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        manualStopRef.current = true // a permissions failure won't self-heal
        setVoiceError('Microphone access was blocked. Allow mic access and try again.')
        return
      }
      setVoiceError(`Voice input error: ${event.error}`)
    }

    recognition.onend = () => {
      // Commit the finalized text (drop any trailing interim) into the base so
      // a restart continues appending from the right place.
      const committed = joinTranscript(baseTextRef.current, finalTextRef.current)
      baseTextRef.current = committed
      finalTextRef.current = ''
      setInput(committed)

      // Keep listening unless the user stopped us.
      if (!manualStopRef.current && recordingRef.current) {
        if (networkErrorRef.current) {
          networkErrorRef.current = false
          if (networkRetryRef.current < MAX_VOICE_NETWORK_RETRIES) {
            networkRetryRef.current += 1
            try { recognition.start(); return } catch { /* fall through */ }
          } else {
            setVoiceError(
              'Voice recognition keeps losing its connection. Chrome sends ' +
              'audio to an online speech service, so this needs a stable ' +
              'internet connection — please try again.',
            )
          }
        } else {
          // Natural end (pause/timeout) → seamlessly resume.
          try { recognition.start(); return } catch { /* fall through */ }
        }
      }

      recognitionRef.current = null
      setRecordingState(false)
      inputRef.current?.focus()
    }

    recognitionRef.current = recognition
    try {
      recognition.start()
      return true
    } catch {
      setVoiceError('Could not start voice input.')
      recognitionRef.current = null
      return false
    }
  }, [setRecordingState])

  const toggleVoice = () => {
    setVoiceError(null)
    if (!getSpeechRecognition()) {
      setVoiceError('Voice input is not supported in this browser.')
      return
    }
    // Already listening → stop immediately. Flip the UI now (optimistic) and
    // abort the engine so re-click always ends recording without waiting.
    if (recordingRef.current) {
      manualStopRef.current = true
      setRecordingState(false)
      const rec = recognitionRef.current
      if (rec) {
        try { rec.stop() } catch { try { rec.abort() } catch { /* noop */ } }
      }
      return
    }
    // Start a fresh session, appending to the current input.
    manualStopRef.current = false
    networkErrorRef.current = false
    networkRetryRef.current = 0
    baseTextRef.current = input
    finalTextRef.current = ''
    if (startVoiceSession()) setRecordingState(true)
  }

  // Stop any in-flight recognition on unmount.
  useEffect(() => {
    return () => {
      manualStopRef.current = true
      if (recognitionRef.current) {
        try { recognitionRef.current.abort() } catch { /* noop */ }
      }
    }
  }, [])

  const doSend = async () => {
    const trimmed = input.trim()
    if (!conversationId || loading || uploading) return
    // Provider Registry 2.0 — never upload images for a text-only model, even if
    // some slipped into the staged set (e.g. a late model switch).
    const filesToSend = visionEnabled
      ? files
      : files.filter(f => !f.type.startsWith('image/'))
    // Nothing to send: no text and no files.
    if (!trimmed && filesToSend.length === 0) return

    let attachments: ChatAttachment[] | undefined
    if (filesToSend.length > 0) {
      setUploading(true)
      setUploadError(null)
      try {
        const form = new FormData()
        form.append('conversation_id', conversationId)
        form.append('add_to_workspace', String(addToWorkspace && isProjectConversation))
        for (const f of filesToSend) form.append('files', f)
        const res = await fetch('/api/chat/upload', { method: 'POST', body: form })
        if (!res.ok) {
          const err = await res.json().catch(() => ({}))
          setUploadError(err.detail || 'File upload failed')
          return
        }
        const data = await res.json()
        attachments = data.attachments as ChatAttachment[]
      } catch (err) {
        console.error('Upload error:', err)
        setUploadError('File upload failed — see console for details.')
        return
      } finally {
        setUploading(false)
      }
    }

    onSend(trimmed, attachments)
    setInput('')
    setFiles([])
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    void doSend()
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Ctrl/Cmd + Enter sends; plain Enter inserts a newline (default behavior).
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      void doSend()
    }
  }

  const label = headerLabel || projectId

  // Task 07.1 — provider dropdown shown top-left in the chat header. All four
  // providers are listed; ones without a configured key render disabled.
  const providerSelector =
    providers && providers.length > 0 ? (
      <select
        className="provider-select"
        value={selectedProvider ?? ''}
        onChange={e => onSelectProvider && onSelectProvider(e.target.value)}
        title="Model provider"
        aria-label="Model provider"
      >
        {providers.map(p => (
          <option key={p.id} value={p.id} disabled={!p.available}>
            {p.label}
            {p.available ? '' : ' — no key'}
          </option>
        ))}
      </select>
    ) : null

  // Task 07.2 — light/dark theme dropdown, shown top-right in the chat header.
  const themeSelector = onSelectTheme ? (
    <select
      className="theme-select"
      value={theme ?? 'dark'}
      onChange={e => onSelectTheme(e.target.value as 'dark' | 'light')}
      title="Color theme"
      aria-label="Color theme"
    >
      <option value="dark">Dark</option>
      <option value="light">Light</option>
    </select>
  ) : null

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

  const sendDisabled = loading || uploading || (!input.trim() && files.length === 0)

  return (
    <main className="chat-panel">
      <div className="chat-header">
        <div className="chat-header-row">
          {providerSelector}
          <h2>{label}</h2>
        </div>
        {themeSelector}
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
          // Task 07.0 — attachments attached to a (user) message.
          const attachments =
            (msg.metadata as { attachments?: ChatAttachment[] } | null | undefined)?.attachments ?? []
          // Phase 6 — intent badge + memory chip from the assistant message metadata.
          const metaIntent =
            msg.role === 'assistant'
              ? (msg.metadata as { intent?: string } | null | undefined)?.intent
              : undefined
          const metaMemoryReason =
            msg.role === 'assistant'
              ? (msg.metadata as { memory_reason?: string } | null | undefined)?.memory_reason
              : undefined
          return (
            <div key={msg.id || i} className={`chat-message ${msg.role}`}>
              <div className="chat-message-role">{msg.role === 'user' ? 'You' : 'Agent OS'}</div>
              {msg.role === 'assistant' ? (
                <div
                  className="chat-message-content orch-response"
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content) }}
                />
              ) : (
                msg.content && <div className="chat-message-content">{msg.content}</div>
              )}
              {(metaIntent || metaMemoryReason) && (
                <div className="chat-meta-row">
                  {metaIntent && (
                    <span className={`chat-intent-badge intent-${metaIntent}`}>{metaIntent}</span>
                  )}
                  {metaMemoryReason && (
                    <span className="chat-memory-chip" title="Project memory was updated this turn">
                      🧠 {metaMemoryReason}
                    </span>
                  )}
                </div>
              )}
              {attachments.length > 0 && (
                <div className="chat-attachments">
                  {attachments.map((att, ai) => (
                    <div className="chat-attachment" key={ai} title={att.original_filename}>
                      {isImage(att.mime_type) && conversationId ? (
                        <img
                          className="chat-attachment-thumb"
                          src={`/api/conversations/${conversationId}/attachments/${encodeURIComponent(att.stored_filename)}`}
                          alt={att.original_filename}
                        />
                      ) : (
                        <span className="chat-attachment-icon">📄</span>
                      )}
                      <span className="chat-attachment-name">{att.original_filename}</span>
                      <span
                        className={`chat-attachment-scope ${att.added_to_workspace ? 'workspace' : 'chat'}`}
                      >
                        {att.added_to_workspace ? 'chat + workspace' : 'chat only'}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {runIdFromMeta && runProjectId && (
                <RunChatCard
                  projectId={runProjectId}
                  runId={runIdFromMeta}
                  conversationId={conversationId}
                  onOpenRun={(rid) => setOpenRunId(rid)}
                  onOpenTrace={(rid) => setOpenTraceId(rid)}
                  onRunsChanged={onRunsChanged}
                  onMessagesChanged={onMessagesChanged}
                  provider={selectedProvider}
                  model={selectedModel}
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
      {/* Task 07.0 — selected file chips + "add to workspace" toggle, shown
          above the composer before sending. */}
      {(files.length > 0 || uploadError || voiceError || imageBlockedNote) && (
        <div className="composer-tray">
          {voiceError && <div className="composer-voice-error">{voiceError}</div>}
          {imageBlockedNote && (
            <div className="composer-image-blocked">{imageBlockedNote}</div>
          )}
          {uploadError && <div className="composer-upload-error">{uploadError}</div>}
          {files.length > 0 && (
            <>
              <div className="composer-chips">
                {files.map((f, i) => (
                  <div className="composer-chip" key={i} title={f.name}>
                    <span className="composer-chip-name">{f.name}</span>
                    <span className="composer-chip-size">{formatSize(f.size)}</span>
                    <button
                      type="button"
                      className="composer-chip-remove"
                      onClick={() => removeFile(i)}
                      disabled={uploading}
                      aria-label={`Remove ${f.name}`}
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
              {isProjectConversation && (
                <label className="composer-workspace-toggle">
                  <input
                    type="checkbox"
                    checked={addToWorkspace}
                    onChange={e => setAddToWorkspace(e.target.checked)}
                    disabled={uploading}
                  />
                  Add to workspace too
                </label>
              )}
            </>
          )}
        </div>
      )}
      {models && models.length > 0 && (
        <div className="composer-modelbar">
          <ModelPicker
            models={models}
            selectedModel={selectedModel ?? ''}
            onSelect={(m) => onSelectModel && onSelectModel(m)}
            disabled={loading || uploading}
          />
        </div>
      )}
      <form className="chat-input composer" onSubmit={handleSubmit}>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={visionEnabled ? ACCEPT_TYPES : ACCEPT_TYPES_NO_IMAGE}
          style={{ display: 'none' }}
          onChange={handleFilesPicked}
        />
        <button
          type="button"
          className={`composer-icon-btn composer-attach-btn${visionEnabled ? '' : ' no-image'}`}
          onClick={() => fileInputRef.current?.click()}
          disabled={loading || uploading}
          title={
            visionEnabled
              ? 'Attach files'
              : 'Attach files — images unavailable for the selected model'
          }
          aria-label="Attach files"
        >
          +
        </button>
        <textarea
          ref={inputRef}
          className="composer-textarea"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            revisingPendingId
              ? 'Describe what to change about the plan...  (Ctrl+Enter to send)'
              : 'Type a message...  (Enter for newline, Ctrl+Enter to send)'
          }
          rows={1}
          autoFocus
          disabled={loading}
        />
        <button
          type="button"
          className={`composer-icon-btn composer-voice-btn${recording ? ' recording' : ''}`}
          onClick={toggleVoice}
          disabled={loading || uploading || !voiceSupported}
          title={
            voiceSupported
              ? recording
                ? 'Stop listening'
                : 'Start voice input'
              : 'Voice input not supported in this browser'
          }
          aria-label="Voice input"
        >
          {recording ? '■' : '🎤'}
        </button>
        <button type="submit" className="composer-send-btn" disabled={sendDisabled}>
          {uploading ? 'Uploading...' : revisingPendingId ? 'Send revision' : 'Send'}
        </button>
      </form>
      {openRunId && runProjectId && (
        <RunDetailModal
          projectId={runProjectId}
          runId={openRunId}
          onClose={() => setOpenRunId(null)}
          onOpenTrace={(rid) => {
            setOpenRunId(null)
            setOpenTraceId(rid)
          }}
          onRunsChanged={onRunsChanged}
        />
      )}
      {openTraceId && runProjectId && (
        <RunTrace
          projectId={runProjectId}
          runId={openTraceId}
          onClose={() => setOpenTraceId(null)}
        />
      )}
    </main>
  )
}

export default ChatPanel
