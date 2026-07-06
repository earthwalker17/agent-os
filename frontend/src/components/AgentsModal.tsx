import { useState, useEffect } from 'react'
import type { AgentInfo, AgentCapabilities } from '../types'

interface Props {
  agents: AgentInfo[]
  onClose: () => void
}

/** Capability flags -> short badge labels (order = display order). */
export function capabilityBadges(caps: AgentCapabilities): string[] {
  const badges: string[] = []
  if (caps.read_only) badges.push('read-only')
  if (caps.writes_memory) badges.push('memory')
  if (caps.writes_repo) badges.push('repo')
  if (caps.searches_web) badges.push('web')
  if (caps.deploys) badges.push('deploy')
  if (caps.dispatches_runs) badges.push('runs')
  if (caps.requires_confirmation) badges.push('asks first')
  return badges
}

interface OpenSkill {
  skillId: string
  title: string
  content: string
}

/**
 * Phase 10 — the Agents browser: a two-pane modal listing every system agent
 * from GET /api/agents (agents_registry-backed). The detail pane shows the
 * agent's contract (introduction, use cases, responsibilities, tools,
 * approval boundary) plus its built-in skills, each readable and manually
 * editable (the skill-update endpoint is the only write path).
 */
function AgentsModal({ agents, onClose }: Props) {
  const [selectedId, setSelectedId] = useState<string>(agents[0]?.id ?? '')
  const [openSkill, setOpenSkill] = useState<OpenSkill | null>(null)
  const [skillLoading, setSkillLoading] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const agent = agents.find(a => a.id === selectedId) ?? agents[0]

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (editing) {
        setEditing(false)
      } else if (openSkill) {
        setOpenSkill(null)
      } else {
        onClose()
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose, editing, openSkill])

  const selectAgent = (id: string) => {
    setSelectedId(id)
    setOpenSkill(null)
    setEditing(false)
    setError('')
  }

  const loadSkill = async (skillId: string) => {
    if (!agent) return
    if (openSkill?.skillId === skillId) {
      setOpenSkill(null)
      setEditing(false)
      return
    }
    setSkillLoading(true)
    setError('')
    setEditing(false)
    try {
      const res = await fetch(`/api/agents/${agent.id}/skills/${skillId}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setOpenSkill({ skillId, title: data.title, content: data.content || '(empty)' })
    } catch (err) {
      console.error('Skill load error:', err)
      setError('Could not load this skill.')
    } finally {
      setSkillLoading(false)
    }
  }

  const startEdit = () => {
    if (!openSkill) return
    setDraft(openSkill.content === '(empty)' ? '' : openSkill.content)
    setEditing(true)
    setError('')
  }

  const saveSkill = async () => {
    if (!agent || !openSkill) return
    setSaving(true)
    setError('')
    try {
      const res = await fetch(`/api/agents/${agent.id}/skills/${openSkill.skillId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: draft }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `HTTP ${res.status}`)
      }
      setOpenSkill({ ...openSkill, content: draft })
      setEditing(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed.')
    } finally {
      setSaving(false)
    }
  }

  if (!agent) return null

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="agents-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Agents</h3>
          <button className="modal-close" onClick={onClose}>&times;</button>
        </div>
        <div className="agents-modal-body">
          <ul className="agents-list">
            {agents.map(a => (
              <li key={a.id}>
                <button
                  className={
                    `agents-list-item${a.id === agent.id ? ' active' : ''}` +
                    `${a.status === 'planned' ? ' planned' : ''}`
                  }
                  onClick={() => selectAgent(a.id)}
                >
                  <span className="agents-list-name">{a.name}</span>
                  {a.command ? (
                    <code className="agent-command-chip">{a.command}</code>
                  ) : a.status === 'planned' ? (
                    <span className="agent-planned-tag">planned</span>
                  ) : (
                    <span className="agent-planned-tag">contracts</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
          <div className="agent-detail">
            <div className="agent-detail-header">
              <h4>{agent.name}</h4>
              {agent.command && (
                <code className="agent-command-chip">
                  {agent.command}
                  {agent.aliases.length > 0 && ` (${agent.aliases.join(', ')})`}
                </code>
              )}
              {agent.status === 'planned' && (
                <span className="agent-planned-tag">planned</span>
              )}
            </div>
            <div className="agent-badges">
              {capabilityBadges(agent.capabilities).map(b => (
                <span key={b} className={`agent-badge badge-${b.replace(/\s+/g, '-')}`}>{b}</span>
              ))}
            </div>
            <p className="agent-introduction">{agent.introduction}</p>
            {agent.use_cases.length > 0 && (
              <div className="agent-section">
                <h5>Use it for</h5>
                <ul>{agent.use_cases.map(u => <li key={u}>{u}</li>)}</ul>
              </div>
            )}
            {agent.responsibilities.length > 0 && (
              <div className="agent-section">
                <h5>Responsibilities</h5>
                <ul>{agent.responsibilities.map(r => <li key={r}>{r}</li>)}</ul>
              </div>
            )}
            {agent.tool_categories.length > 0 && (
              <div className="agent-section">
                <h5>Tools</h5>
                <ul>{agent.tool_categories.map(t => <li key={t}>{t}</li>)}</ul>
              </div>
            )}
            {agent.approval_boundary && (
              <div className="agent-section agent-boundary">
                <h5>Approval boundary</h5>
                <p>{agent.approval_boundary}</p>
              </div>
            )}
            {agent.skills.length > 0 && (
              <div className="agent-section">
                <h5>Built-in skills</h5>
                <ul className="agent-skills">
                  {agent.skills.map(s => (
                    <li key={s.id} className="skill-item">
                      <button
                        className={`skill-toggle${openSkill?.skillId === s.id ? ' open' : ''}`}
                        onClick={() => void loadSkill(s.id)}
                        disabled={skillLoading}
                      >
                        <span className="skill-title">{s.title}</span>
                        <span className="skill-description">{s.description}</span>
                      </button>
                      {openSkill?.skillId === s.id && (
                        <div className="skill-view">
                          {editing ? (
                            <div className="skill-editor">
                              <textarea
                                className="skill-textarea"
                                value={draft}
                                onChange={e => setDraft(e.target.value)}
                                autoFocus
                              />
                              <div className="global-memory-editor-actions">
                                <button className="btn-save" onClick={() => void saveSkill()} disabled={saving}>
                                  {saving ? 'Saving...' : 'Save'}
                                </button>
                                <button className="btn-cancel" onClick={() => setEditing(false)} disabled={saving}>
                                  Cancel
                                </button>
                              </div>
                            </div>
                          ) : (
                            <>
                              <pre className="skill-content">{openSkill.content}</pre>
                              <button className="btn-edit-global" onClick={startEdit}>
                                Edit
                              </button>
                            </>
                          )}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {error && <div className="agent-detail-error">{error}</div>}
          </div>
        </div>
      </div>
    </div>
  )
}

export default AgentsModal
