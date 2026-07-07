import { useState } from 'react'
import type { Conversation } from '../types'
import {
  IconBook,
  IconChevronRight,
  IconPanelLeft,
  IconPencil,
  IconPlus,
  IconSettings,
  IconSpark,
  IconX,
} from './icons'

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
  onOpenAgents: () => void
  /** UI polish pass — open the global Settings modal (sidebar footer). */
  onOpenSettings: () => void
  /** UI polish pass — collapsed-rail state, owned by App. */
  collapsed: boolean
  onToggleCollapse: () => void
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
  onOpenAgents,
  onOpenSettings,
  collapsed,
  onToggleCollapse,
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

  // Collapsed rail — same component stays mounted, so expand/selection state
  // survives collapse; every rail button reuses the existing handlers.
  if (collapsed) {
    return (
      <aside className="sidebar project-list collapsed">
        <div className="sidebar-rail">
          <button
            className="sidebar-rail-btn"
            onClick={onToggleCollapse}
            title="Expand sidebar"
            aria-label="Expand sidebar"
          >
            <IconPanelLeft />
          </button>
          <div className="sidebar-rail-divider" />
          <button
            className="sidebar-rail-btn"
            onClick={onOpenAgents}
            title="Agents"
            aria-label="Agents"
          >
            <IconSpark />
          </button>
          <button
            className="sidebar-rail-btn"
            onClick={onOpenGlobalMemory}
            title="Global memory"
            aria-label="Global memory"
          >
            <IconBook />
          </button>
          <div className="sidebar-rail-spacer" />
          <button
            className="sidebar-rail-btn"
            onClick={onOpenSettings}
            title="Settings"
            aria-label="Settings"
          >
            <IconSettings />
          </button>
        </div>
      </aside>
    )
  }

  return (
    <aside className="sidebar project-list">
      <div className="sidebar-top">
        <span className="sidebar-brand">Agent OS</span>
        <button
          className="sidebar-collapse-btn"
          onClick={onToggleCollapse}
          title="Collapse sidebar"
          aria-label="Collapse sidebar"
        >
          <IconPanelLeft />
        </button>
      </div>

      <div className="sidebar-body">
        {/* Phase 10 — Agents browser */}
        <button className="agents-btn" onClick={onOpenAgents}>
          <IconSpark size={14} />
          Agents
        </button>

        {/* Global memory button */}
        <button className="global-memory-btn" onClick={onOpenGlobalMemory}>
          <IconBook size={14} />
          Global memory
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
              <span className={`expand-icon ${isGeneralOpen ? 'open' : ''}`}>
                <IconChevronRight />
              </span>
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
                    <IconX size={12} />
                  </button>
                </div>
              ))}
              <button
                className="conv-btn new-conv"
                onClick={onNewGeneralConversation}
              >
                + New conversation
              </button>
            </div>
          )}
        </div>

        {/* Divider */}
        <div className="sidebar-divider" />

        {/* PROJECTS section */}
        <div className="sidebar-header">
          <h2>Projects</h2>
          <button className="sidebar-action-btn" onClick={onNewProject} title="New project">
            <IconPlus />
          </button>
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
                    <span className={`expand-icon ${open ? 'open' : ''}`}>
                      <IconChevronRight />
                    </span>
                    {id}
                  </button>
                  {id === activeProject && (
                    <div className="project-actions">
                      <button
                        className="project-action-btn"
                        onClick={e => { e.stopPropagation(); onRenameProject(id) }}
                        title="Rename project"
                      >
                        <IconPencil />
                      </button>
                      <button
                        className="project-action-btn delete"
                        onClick={e => { e.stopPropagation(); onDeleteProject(id) }}
                        title="Delete project"
                      >
                        <IconX />
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
                          <IconX size={12} />
                        </button>
                      </div>
                    ))}
                    <button
                      className="conv-btn new-conv"
                      onClick={() => onNewConversation(id)}
                    >
                      + New conversation
                    </button>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      </div>

      <div className="sidebar-footer">
        <button className="sidebar-settings-btn" onClick={onOpenSettings}>
          <IconSettings size={15} />
          Settings
        </button>
      </div>
    </aside>
  )
}

export default ProjectList
