import { useState, useEffect, useCallback } from 'react'
import ProjectList from './components/ProjectList'
import ChatPanel from './components/ChatPanel'
import ContextPanel from './components/ContextPanel'
import EditModal from './components/EditModal'
import ConfirmDialog from './components/ConfirmDialog'
import GlobalMemoryModal from './components/GlobalMemoryModal'
import type { Message, Conversation, ProjectContext, PendingExecution, ChatAttachment, ProviderInfo } from './types'

const GENERAL_PROJECT_ID = '__GENERAL__'

// Mirrors backend execution.chat_delegation.is_code_delegation — we use this
// in the frontend purely to decide whether to ping the Runs panel for a fast
// refresh after a chat send. The backend is the source of truth for actual
// delegation routing.
function isCodeDelegation(message: string): boolean {
  const stripped = message.replace(/^\s+/, '')
  if (!stripped.toLowerCase().startsWith('@code')) return false
  const rest = stripped.slice(5)
  return rest === '' || /^[\s:\-,]/.test(rest)
}

function App() {
  const [projects, setProjects] = useState<string[]>([])
  const [activeProject, setActiveProject] = useState<string | null>(null)
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeConversation, setActiveConversation] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [context, setContext] = useState<ProjectContext | null>(null)
  const [loading, setLoading] = useState(false)

  // GENERAL workspace state
  const [isGeneralActive, setIsGeneralActive] = useState(false)
  const [generalConversations, setGeneralConversations] = useState<Conversation[]>([])

  // Task 07.1 — model provider selection.
  const [providersList, setProvidersList] = useState<ProviderInfo[]>([])
  const [selectedProvider, setSelectedProvider] = useState<string>('claude')

  // Task 07.2 — color theme (dark default), persisted across reloads.
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    const saved = localStorage.getItem('agentos-theme')
    return saved === 'light' ? 'light' : 'dark'
  })

  // Modal state
  const [editModal, setEditModal] = useState<{ filename: string; content: string } | null>(null)
  const [confirmDialog, setConfirmDialog] = useState<{ message: string; onConfirm: (checked: boolean) => void; checkboxLabel?: string } | null>(null)
  const [globalMemoryModal, setGlobalMemoryModal] = useState(false)
  const [globalMemory, setGlobalMemory] = useState<Record<string, string>>({})

  // Runs panel refresh trigger — bumped after the user sends an @code message
  // so the panel reloads without waiting for the next poll tick.
  const [runsRefreshKey, setRunsRefreshKey] = useState(0)

  // Task 05.9.5: pending-execution revise mode. When the user clicks
  // "Revise plan" on a confirmable plan, we remember which pending id the
  // next chat send is revising. Cleared after the next send completes or
  // when the user cancels.
  const [revisingPendingId, setRevisingPendingId] = useState<string | null>(null)
  const [revisingPendingTitle, setRevisingPendingTitle] = useState<string | null>(null)

  // --- Data loading ---

  const loadProjects = useCallback(async () => {
    try {
      const res = await fetch('/api/projects')
      const data: string[] = await res.json()
      setProjects(data)
      return data
    } catch (err) {
      console.error(err)
      return []
    }
  }, [])

  useEffect(() => {
    loadProjects().then(data => {
      if (data.length > 0 && !activeProject && !isGeneralActive) {
        setActiveProject(data[0])
      }
    })
  }, [])

  const loadConversations = useCallback(async (projectId: string) => {
    try {
      const res = await fetch(`/api/projects/${projectId}/conversations`)
      const data: Conversation[] = await res.json()
      setConversations(prev => {
        const others = prev.filter(c => c.project_id !== projectId)
        return [...others, ...data]
      })
    } catch (err) {
      console.error(err)
    }
  }, [])

  const loadGeneralConversations = useCallback(async () => {
    try {
      const res = await fetch('/api/general/conversations')
      const data: Conversation[] = await res.json()
      setGeneralConversations(data)
    } catch (err) {
      console.error(err)
    }
  }, [])

  useEffect(() => {
    loadGeneralConversations()
  }, [loadGeneralConversations])

  // Task 07.2 — apply the theme to <html> (CSS variables cascade from there to
  // everything, modals included) and remember the choice.
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('agentos-theme', theme)
  }, [theme])

  // Task 07.1 — load provider availability once and pre-select the default
  // (Claude when available, otherwise the first available provider).
  useEffect(() => {
    fetch('/api/providers')
      .then(res => res.json())
      .then((data: { providers: ProviderInfo[]; default: string }) => {
        const list = data.providers ?? []
        setProvidersList(list)
        const fallback = list.find(p => p.available)?.id
        const def = list.find(p => p.id === data.default && p.available)?.id
        if (def || fallback) setSelectedProvider(def || fallback!)
      })
      .catch(console.error)
  }, [])

  useEffect(() => {
    if (activeProject) loadConversations(activeProject)
  }, [activeProject, loadConversations])

  const refreshContext = useCallback(async () => {
    if (!activeProject) return
    try {
      const res = await fetch(`/api/projects/${activeProject}/context`)
      setContext(await res.json())
    } catch (err) {
      console.error(err)
    }
  }, [activeProject])

  useEffect(() => {
    if (activeProject) refreshContext()
    else setContext(null)
  }, [activeProject, refreshContext])

  // Switching conversations clears any in-progress revise mode — the
  // pending plan it was targeting belongs to a different conversation.
  useEffect(() => {
    setRevisingPendingId(null)
    setRevisingPendingTitle(null)
  }, [activeConversation])

  // Hydrate pending-execution state for messages that carry a
  // pending_execution_id in their metadata (Task 05.9.5). On page reload
  // the message list returns metadata only, so we fetch the live state of
  // each referenced pending row in parallel and attach it for rendering.
  const hydratePendingForMessages = useCallback(
    async (msgs: Message[], projectIdForFetch: string | null): Promise<Message[]> => {
      if (!projectIdForFetch || projectIdForFetch === 'general') return msgs
      const targets = msgs
        .map((m, idx) => ({ idx, pid: (m.metadata as { pending_execution_id?: string } | null | undefined)?.pending_execution_id }))
        .filter((t): t is { idx: number; pid: string } => typeof t.pid === 'string')
      if (targets.length === 0) return msgs
      const results = await Promise.all(
        targets.map(async (t) => {
          try {
            const res = await fetch(
              `/api/projects/${projectIdForFetch}/execution/pending/${t.pid}`,
            )
            if (!res.ok) return { idx: t.idx, plan: null as PendingExecution | null }
            const plan: PendingExecution = await res.json()
            return { idx: t.idx, plan }
          } catch {
            return { idx: t.idx, plan: null }
          }
        }),
      )
      const hydrated = msgs.slice()
      for (const { idx, plan } of results) {
        hydrated[idx] = { ...hydrated[idx], pending_execution: plan }
      }
      return hydrated
    },
    [],
  )

  useEffect(() => {
    if (!activeConversation) { setMessages([]); return }
    let cancelled = false
    fetch(`/api/conversations/${activeConversation}/messages`)
      .then(res => res.json())
      .then(async (msgs: Message[]) => {
        if (cancelled) return
        // Pending plans only exist in real projects, never GENERAL. Use the
        // owning project id from the active project state so the fetch
        // routes to the right namespace.
        const projectIdForFetch = isGeneralActive ? null : activeProject
        const hydrated = await hydratePendingForMessages(msgs, projectIdForFetch)
        if (!cancelled) setMessages(hydrated)
      })
      .catch(console.error)
    return () => { cancelled = true }
  }, [activeConversation, activeProject, isGeneralActive, hydratePendingForMessages])

  const loadGlobalMemory = useCallback(async () => {
    try {
      const res = await fetch('/api/global-memory')
      const data = await res.json()
      setGlobalMemory(data)
      return data
    } catch (err) {
      console.error(err)
      return {}
    }
  }, [])

  // --- Project actions ---

  const handleSelectProject = useCallback((projectId: string) => {
    setIsGeneralActive(false)
    setActiveProject(projectId)
    setActiveConversation(null)
    setMessages([])
  }, [])

  const handleSelectGeneral = useCallback(() => {
    setIsGeneralActive(true)
    setActiveProject(null)
    setActiveConversation(null)
    setMessages([])
    setContext(null)
  }, [])

  const handleNewProject = useCallback(() => {
    const name = prompt('Enter new project name:')
    if (!name?.trim()) return

    fetch('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim() }),
    })
      .then(async res => {
        if (!res.ok) {
          const err = await res.json()
          alert(err.detail || 'Failed to create project')
          return
        }
        const data = await res.json()
        await loadProjects()
        setIsGeneralActive(false)
        setActiveProject(data.project_id)
        setActiveConversation(null)
        setMessages([])
      })
      .catch(err => console.error('Create project error:', err))
  }, [loadProjects])

  const handleRenameProject = useCallback((projectId: string) => {
    const newName = prompt('Enter new project name:', projectId)
    if (!newName?.trim() || newName.trim() === projectId) return

    fetch(`/api/projects/${projectId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_name: newName.trim() }),
    })
      .then(async res => {
        if (!res.ok) {
          const err = await res.json()
          alert(err.detail || 'Failed to rename project')
          return
        }
        const data = await res.json()
        await loadProjects()
        if (activeProject === projectId) {
          setActiveProject(data.project_id)
          await loadConversations(data.project_id)
        }
      })
      .catch(err => console.error('Rename project error:', err))
  }, [activeProject, loadProjects, loadConversations])

  const handleDeleteProject = useCallback(async (projectId: string) => {
    // Only offer the "delete workspace too" checkbox when a workspace
    // actually exists on disk for this project.
    let workspaceExists = false
    try {
      const res = await fetch(`/api/projects/${projectId}/workspace-status`)
      if (res.ok) {
        const data = await res.json()
        workspaceExists = !!data.exists
      }
    } catch (err) {
      console.error('Workspace status check failed:', err)
    }

    setConfirmDialog({
      message: workspaceExists
        ? `Delete project "${projectId}" and all its conversations? This cannot be undone. ` +
          `Its execution workspace is kept unless you tick the box below.`
        : `Delete project "${projectId}" and all its conversations? This cannot be undone.`,
      checkboxLabel: workspaceExists ? 'Delete its execution workspace too' : undefined,
      onConfirm: (deleteWorkspace: boolean) => {
        setConfirmDialog(null)
        const url = `/api/projects/${projectId}${deleteWorkspace ? '?delete_workspace=true' : ''}`
        fetch(url, { method: 'DELETE' })
          .then(async res => {
            if (!res.ok) {
              const err = await res.json()
              alert(err.detail || 'Failed to delete project')
              return
            }
            const data = await res.json().catch(() => ({}))
            if (data.status === 'partial' && data.warning) {
              alert(data.warning)
            }
            setConversations(prev => prev.filter(c => c.project_id !== projectId))
            if (activeProject === projectId) {
              setActiveProject(null)
              setActiveConversation(null)
              setMessages([])
              setContext(null)
            }
            const remaining = await loadProjects()
            if (remaining.length > 0 && activeProject === projectId) {
              setActiveProject(remaining[0])
            }
          })
          .catch(err => console.error('Delete project error:', err))
      },
    })
  }, [activeProject, loadProjects])

  // --- Conversation actions ---

  const handleSelectConversation = useCallback((conv: Conversation) => {
    if (conv.project_id === GENERAL_PROJECT_ID) {
      setIsGeneralActive(true)
      setActiveProject(null)
      setContext(null)
    } else {
      setIsGeneralActive(false)
      setActiveProject(conv.project_id)
    }
    setActiveConversation(conv.id)
  }, [])

  const handleNewConversation = useCallback(async (projectId: string) => {
    try {
      const res = await fetch(`/api/projects/${projectId}/conversations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: '' }),
      })
      const conv: Conversation = await res.json()
      setConversations(prev => [conv, ...prev])
      setIsGeneralActive(false)
      setActiveProject(projectId)
      setActiveConversation(conv.id)
      setMessages([])
    } catch (err) {
      console.error('Failed to create conversation:', err)
    }
  }, [])

  const handleNewGeneralConversation = useCallback(async () => {
    try {
      const res = await fetch('/api/general/conversations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: '' }),
      })
      const conv: Conversation = await res.json()
      setGeneralConversations(prev => [conv, ...prev])
      setIsGeneralActive(true)
      setActiveProject(null)
      setContext(null)
      setActiveConversation(conv.id)
      setMessages([])
    } catch (err) {
      console.error('Failed to create general conversation:', err)
    }
  }, [])

  const handleDeleteConversation = useCallback((conv: Conversation) => {
    setConfirmDialog({
      message: `Delete conversation "${conv.title}"? This cannot be undone.`,
      onConfirm: () => {
        setConfirmDialog(null)
        fetch(`/api/conversations/${conv.id}`, { method: 'DELETE' })
          .then(async res => {
            if (!res.ok) {
              let detail = `HTTP ${res.status}`
              try {
                const err = await res.json()
                detail = err.detail || detail
              } catch {
                // body wasn't JSON — fall through to the HTTP status
              }
              alert(`Failed to delete conversation: ${detail}`)
              return
            }
            if (conv.project_id === GENERAL_PROJECT_ID) {
              setGeneralConversations(prev => prev.filter(c => c.id !== conv.id))
            } else {
              setConversations(prev => prev.filter(c => c.id !== conv.id))
            }
            if (activeConversation === conv.id) {
              setActiveConversation(null)
              setMessages([])
            }
          })
          .catch(err => {
            console.error('Delete conversation error:', err)
            alert(`Failed to delete conversation: ${err}`)
          })
      },
    })
  }, [activeConversation])

  // --- Chat ---

  const handleSend = useCallback(async (message: string, attachments?: ChatAttachment[]) => {
    if (!activeConversation) return

    // Snapshot revise-mode at the start of the send; clear it eagerly so the
    // banner disappears immediately on submit. We restore on error so the
    // user can retry without re-clicking "Revise plan".
    const revisingId = revisingPendingId
    if (revisingId) {
      setRevisingPendingId(null)
      setRevisingPendingTitle(null)
    }

    const userMsg: Message = {
      role: 'user',
      content: message,
      timestamp: new Date().toISOString(),
      // Task 07.0 — show the just-uploaded attachments on the optimistic
      // bubble; they re-hydrate from server metadata on the next load.
      metadata: attachments && attachments.length ? { attachments } : null,
    }
    setMessages(prev => [...prev, userMsg])
    setLoading(true)

    try {
      const body: {
        conversation_id: string
        message: string
        revise_pending_id?: string
        attachments?: ChatAttachment[]
        provider?: string
      } = {
        conversation_id: activeConversation,
        message,
        provider: selectedProvider,
      }
      if (revisingId) body.revise_pending_id = revisingId
      if (attachments && attachments.length) body.attachments = attachments

      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        // Restore revise mode so the user can edit and retry.
        if (revisingId) {
          setRevisingPendingId(revisingId)
          setRevisingPendingTitle(revisingPendingTitle)
        }
        alert(err.detail || 'Chat request failed')
        return
      }
      const data = await res.json()
      // Task 06.2D — a dispatched run id (from an @code message) is echoed on
      // the response; carry it in metadata so the chat-first run follow-up card
      // attaches to this message (and survives a reload via persisted metadata).
      const newMsgMeta: Record<string, unknown> | null = data.run_id
        ? { run_id: data.run_id }
        : data.pending_execution
        ? { pending_execution_id: data.pending_execution.pending_execution_id }
        : null
      const newMsg: Message = {
        id: data.message_id ?? undefined,
        role: data.role,
        content: data.content,
        timestamp: data.timestamp,
        pending_execution: data.pending_execution ?? null,
        metadata: newMsgMeta,
      }
      setMessages(prev => [...prev, newMsg])

      // Refresh conversation list for title update
      if (isGeneralActive) {
        await loadGeneralConversations()
      } else if (activeProject) {
        await loadConversations(activeProject)
      }

      // Refresh context panel if memory files were updated
      if (data.memory_updated) {
        if (isGeneralActive) {
          // Global memory was updated — refresh global memory if modal is open
          await loadGlobalMemory()
        } else {
          await refreshContext()
        }
      }

      // If this was an @code dispatch inside a project, nudge the Runs panel
      // to refresh immediately so the new run shows up without waiting for
      // the polling tick.
      if (!isGeneralActive && activeProject && isCodeDelegation(message)) {
        setRunsRefreshKey(k => k + 1)
      }
    } catch (err) {
      console.error('Chat error:', err)
      if (revisingId) {
        setRevisingPendingId(revisingId)
        setRevisingPendingTitle(revisingPendingTitle)
      }
    } finally {
      setLoading(false)
    }
  }, [activeConversation, activeProject, isGeneralActive, loadConversations, loadGeneralConversations, refreshContext, loadGlobalMemory, revisingPendingId, revisingPendingTitle, selectedProvider])

  // Task 05.9.5: confirm a pending execution plan. Dispatches the stored
  // task card through the same path as `@code`, marks the pending row as
  // dispatched, and appends a confirmation chat message. We re-load the
  // conversation messages so the new assistant message shows up, and
  // re-hydrate the existing pending bubble so its buttons disappear.
  const handleConfirmPending = useCallback(async (pendingId: string) => {
    if (!activeProject || isGeneralActive) return
    try {
      const res = await fetch(
        `/api/projects/${activeProject}/execution/pending/${pendingId}/confirm`,
        { method: 'POST' },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        alert(err.detail || 'Failed to dispatch the run.')
        return
      }
      // Refresh messages (new confirmation bubble + updated pending status)
      if (activeConversation) {
        const msgsRes = await fetch(`/api/conversations/${activeConversation}/messages`)
        const msgs: Message[] = await msgsRes.json()
        const hydrated = await hydratePendingForMessages(msgs, activeProject)
        setMessages(hydrated)
      }
      // Refresh Runs panel immediately.
      setRunsRefreshKey(k => k + 1)
    } catch (err) {
      console.error('Confirm pending plan error:', err)
      alert('Failed to dispatch the run — see console for details.')
    }
  }, [activeProject, activeConversation, isGeneralActive, hydratePendingForMessages])

  const handleRevisePending = useCallback((pending: PendingExecution) => {
    setRevisingPendingId(pending.pending_execution_id)
    setRevisingPendingTitle(pending.title || 'Pending plan')
  }, [])

  const handleCancelRevise = useCallback(() => {
    setRevisingPendingId(null)
    setRevisingPendingTitle(null)
  }, [])

  // --- Memory file editing ---

  const handleEditFile = useCallback((filename: string, content: string) => {
    setEditModal({ filename, content })
  }, [])

  const handleSaveFile = useCallback(async (filename: string, content: string) => {
    if (!activeProject) return
    const res = await fetch(`/api/projects/${activeProject}/update-file`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, content }),
    })
    if (!res.ok) throw new Error('Save failed')
    await refreshContext()
  }, [activeProject, refreshContext])

  // --- Global memory modal ---

  const handleOpenGlobalMemory = useCallback(async () => {
    await loadGlobalMemory()
    setGlobalMemoryModal(true)
  }, [loadGlobalMemory])

  const handleSaveGlobalFile = useCallback(async (filename: string, content: string) => {
    const res = await fetch('/api/global-memory/update-file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, content }),
    })
    if (!res.ok) throw new Error('Save failed')
    // Refresh the global memory data
    await loadGlobalMemory()
  }, [loadGlobalMemory])

  // Determine chat header label
  const chatLabel = isGeneralActive ? 'General' : activeProject

  return (
    <div className="app-layout">
      <ProjectList
        projects={projects}
        activeProject={activeProject}
        activeConversation={activeConversation}
        conversations={conversations}
        isGeneralActive={isGeneralActive}
        generalConversations={generalConversations}
        onSelectProject={handleSelectProject}
        onSelectConversation={handleSelectConversation}
        onNewConversation={handleNewConversation}
        onNewProject={handleNewProject}
        onRenameProject={handleRenameProject}
        onDeleteProject={handleDeleteProject}
        onDeleteConversation={handleDeleteConversation}
        onSelectGeneral={handleSelectGeneral}
        onNewGeneralConversation={handleNewGeneralConversation}
        onOpenGlobalMemory={handleOpenGlobalMemory}
      />
      <ChatPanel
        projectId={isGeneralActive ? 'general' : activeProject}
        conversationId={activeConversation}
        messages={messages}
        onSend={handleSend}
        loading={loading}
        headerLabel={chatLabel}
        runProjectId={isGeneralActive ? null : activeProject}
        onConfirmPending={handleConfirmPending}
        onRevisePending={handleRevisePending}
        revisingPendingId={revisingPendingId}
        revisingPendingTitle={revisingPendingTitle}
        onCancelRevise={handleCancelRevise}
        onRunsChanged={() => setRunsRefreshKey(k => k + 1)}
        providers={providersList}
        selectedProvider={selectedProvider}
        onSelectProvider={setSelectedProvider}
        theme={theme}
        onSelectTheme={setTheme}
      />
      <ContextPanel
        projectId={isGeneralActive ? null : activeProject}
        context={isGeneralActive ? null : context}
        onEditFile={handleEditFile}
        runsRefreshSignal={runsRefreshKey}
      />
      {editModal && (
        <EditModal
          filename={editModal.filename}
          content={editModal.content}
          onSave={handleSaveFile}
          onClose={() => setEditModal(null)}
        />
      )}
      {confirmDialog && (
        <ConfirmDialog
          message={confirmDialog.message}
          onConfirm={confirmDialog.onConfirm}
          onCancel={() => setConfirmDialog(null)}
          checkboxLabel={confirmDialog.checkboxLabel}
        />
      )}
      {globalMemoryModal && (
        <GlobalMemoryModal
          memory={globalMemory}
          onSave={handleSaveGlobalFile}
          onClose={() => setGlobalMemoryModal(false)}
        />
      )}
    </div>
  )
}

export default App
