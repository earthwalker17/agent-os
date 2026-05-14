import { useState, useEffect, useCallback } from 'react'
import ProjectList from './components/ProjectList'
import ChatPanel from './components/ChatPanel'
import ContextPanel from './components/ContextPanel'
import EditModal from './components/EditModal'
import ConfirmDialog from './components/ConfirmDialog'
import GlobalMemoryModal from './components/GlobalMemoryModal'
import type { Message, Conversation, ProjectContext } from './types'

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

  // Modal state
  const [editModal, setEditModal] = useState<{ filename: string; content: string } | null>(null)
  const [confirmDialog, setConfirmDialog] = useState<{ message: string; onConfirm: () => void } | null>(null)
  const [globalMemoryModal, setGlobalMemoryModal] = useState(false)
  const [globalMemory, setGlobalMemory] = useState<Record<string, string>>({})

  // Runs panel refresh trigger — bumped after the user sends an @code message
  // so the panel reloads without waiting for the next poll tick.
  const [runsRefreshKey, setRunsRefreshKey] = useState(0)

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

  useEffect(() => {
    if (!activeConversation) { setMessages([]); return }
    fetch(`/api/conversations/${activeConversation}/messages`)
      .then(res => res.json())
      .then(setMessages)
      .catch(console.error)
  }, [activeConversation])

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

  const handleDeleteProject = useCallback((projectId: string) => {
    setConfirmDialog({
      message: `Delete project "${projectId}" and all its conversations? This cannot be undone.`,
      onConfirm: () => {
        setConfirmDialog(null)
        fetch(`/api/projects/${projectId}`, { method: 'DELETE' })
          .then(async res => {
            if (!res.ok) {
              const err = await res.json()
              alert(err.detail || 'Failed to delete project')
              return
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
            if (!res.ok) return
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
          .catch(err => console.error('Delete conversation error:', err))
      },
    })
  }, [activeConversation])

  // --- Chat ---

  const handleSend = useCallback(async (message: string) => {
    if (!activeConversation) return

    const userMsg: Message = {
      role: 'user',
      content: message,
      timestamp: new Date().toISOString(),
    }
    setMessages(prev => [...prev, userMsg])
    setLoading(true)

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: activeConversation, message }),
      })
      const data = await res.json()
      setMessages(prev => [...prev, { role: data.role, content: data.content, timestamp: data.timestamp }])

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
    } finally {
      setLoading(false)
    }
  }, [activeConversation, activeProject, isGeneralActive, loadConversations, loadGeneralConversations, refreshContext, loadGlobalMemory])

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
