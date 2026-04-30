import { useState } from 'react'
import type { Conversation } from '../types'

interface Props {
  projects: string[]
  activeProject: string | null
  activeConversation: string | null
  conversations: Conversation[]
  isGeneralActive: boolean
  generalConversations: Conversation[]
  onSelectProject: (id: string) => void
  onSelectConversation: (conv: Conversation) => void
  onNewConversation: (projectId: string) => void
  onNewProject: () => void
  onRenameProject: (projectId: string) => void
  onDeleteProject: (projectId: string) => void
  onDeleteConversation: (conv: Conversation) => void
  onSelectGeneral: () => void
  onNewGeneralConversation: () => void
  onOpenGlobalMemory: () => void
}

function ProjectList({
  projects,
  activeProject,
  activeConversation,
  conversations,
  isGeneralActive,
  generalConversations,
  onSelectProject,
  onSelectConversation,
  onNewConversation,
  onNewProject,
  onRenameProject,
  onDeleteProject,
  onDeleteConversation,
  onSelectGeneral,
  onNewGeneralConversation,
  onOpenGlobalMemory,
}: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [generalExpanded, setGeneralExpanded] = useState(false)

  const toggle = (projectId: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(projectId)) {
        next.delete(projectId)
      } else {
        next.add(projectId)
      }
      return next
    })
    onSelectProject(projectId)
  }

  const isExpanded = (id: string) => expanded.has(id) || id === activeProject

  const isGeneralOpen = generalExpanded || isGeneralActive

  return (
    <aside className="sidebar project-list">
      {/* Global memory button */}
      <button className="global-memory-btn" onClick={onOpenGlobalMemory}>
        View Global Memories
      </button>

      {/* GENERAL workspace section */}
      <div className="sidebar-header">
        <h2>General</h2>
      </div>
      <div className="general-section">
        <div className="project-row">
          <button
            className={`project-btn ${isGeneralActive ? 'active' : ''}`}
            onClick={() => { setGeneralExpanded(prev => !prev); onSelectGeneral() }}
          >
            <span className={`expand-icon ${isGeneralOpen ? 'open' : ''}`}>&#9656;</span>
            General
          </button>
        </div>
        {isGeneralOpen && (
          <div className="conv-list">
            {generalConversations.map(conv => (
              <div key={conv.id} className="conv-row">
                <button
                  className={`conv-btn ${conv.id === activeConversation ? 'active' : ''}`}
                  onClick={() => onSelectConversation(conv)}
                  title={conv.title}
                >
                  {conv.title}
                </button>
                <button
                  className="conv-delete-btn"
                  onClick={e => { e.stopPropagation(); onDeleteConversation(conv) }}
                  title="Delete conversation"
                >
                  &times;
                </button>
              </div>
            ))}
            <button
              className="conv-btn new-conv"
              onClick={onNewGeneralConversation}
            >
              + New Conversation
            </button>
          </div>
        )}
      </div>

      {/* Divider */}
      <div className="sidebar-divider" />

      {/* PROJECTS section */}
      <div className="sidebar-header">
        <h2>Projects</h2>
        <button className="sidebar-action-btn" onClick={onNewProject} title="New Project">+</button>
      </div>
      <ul>
        {projects.map(id => {
          const projectConvs = conversations.filter(c => c.project_id === id)
          const open = isExpanded(id)

          return (
            <li key={id} className="project-item">
              <div className="project-row">
                <button
                  className={`project-btn ${id === activeProject ? 'active' : ''}`}
                  onClick={() => toggle(id)}
                >
                  <span className={`expand-icon ${open ? 'open' : ''}`}>&#9656;</span>
                  {id}
                </button>
                {id === activeProject && (
                  <div className="project-actions">
                    <button
                      className="project-action-btn"
                      onClick={e => { e.stopPropagation(); onRenameProject(id) }}
                      title="Rename project"
                    >
                      &#9998;
                    </button>
                    <button
                      className="project-action-btn delete"
                      onClick={e => { e.stopPropagation(); onDeleteProject(id) }}
                      title="Delete project"
                    >
                      &times;
                    </button>
                  </div>
                )}
              </div>
              {open && (
                <div className="conv-list">
                  {projectConvs.map(conv => (
                    <div key={conv.id} className="conv-row">
                      <button
                        className={`conv-btn ${conv.id === activeConversation ? 'active' : ''}`}
                        onClick={() => onSelectConversation(conv)}
                        title={conv.title}
                      >
                        {conv.title}
                      </button>
                      <button
                        className="conv-delete-btn"
                        onClick={e => { e.stopPropagation(); onDeleteConversation(conv) }}
                        title="Delete conversation"
                      >
                        &times;
                      </button>
                    </div>
                  ))}
                  <button
                    className="conv-btn new-conv"
                    onClick={() => onNewConversation(id)}
                  >
                    + New Conversation
                  </button>
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </aside>
  )
}

export default ProjectList
