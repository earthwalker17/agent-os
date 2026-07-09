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
  /** Public UI pass — "+" on the Conversations section: open the General
   * landing state (the conversation itself is created on the first send). */
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
  onNewGeneralConversation,
  onOpenGlobalMemory,
  onOpenAgents,
  onOpenSettings,
  collapsed,
  onToggleCollapse,
}: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

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
            title="Agents & Skills"
            aria-label="Agents and Skills"
          >
            <IconSpark />
          </button>
          <button
            className="sidebar-rail-btn"
            onClick={onOpenGlobalMemory}
            title="Global Memory"
            aria-label="Global Memory"
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
        {/* Phase 10 — Agents & Skills browser */}
        <button className="agents-btn" onClick={onOpenAgents}>
          <IconSpark size={14} />
          Agents &amp; Skills
        </button>

        {/* Global Memory button */}
        <button className="global-memory-btn" onClick={onOpenGlobalMemory}>
          <IconBook size={14} />
          Global Memory
        </button>

        {/* Conversations — the GENERAL workspace's chats, listed flat.
            "+ New conversation" opens the landing; the conversation itself is
            created when the first message is sent. */}
        <div className="sidebar-header">
          <h2>Conversations</h2>
          <button
            className="sidebar-action-btn"
            onClick={onNewGeneralConversation}
            title="New conversation"
            aria-label="New conversation"
          >
            <IconPlus />
          </button>
        </div>
        <div className="general-section">
          <div className="conv-list conv-list-flat">
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
              className={`conv-btn new-conv ${isGeneralActive && !activeConversation ? 'active' : ''}`}
              onClick={onNewGeneralConversation}
            >
              + New conversation
            </button>
          </div>
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
