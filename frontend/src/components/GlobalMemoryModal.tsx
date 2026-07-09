import { useState, useEffect } from 'react'

interface Props {
  memory: Record<string, string>
  onSave: (filename: string, content: string) => Promise<void>
  onClose: () => void
}

// SOUL.md leads: the identity anchor, now user-editable here but read-only to
// the Main Agent (excluded from every auto-writeback path).
const FILE_ORDER = ['SOUL.md', 'USER.md', 'WORKSTYLE.md', 'MEMORY.md']

function GlobalMemoryModal({ memory, onSave, onClose }: Props) {
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (editing) {
          setEditing(null)
        } else {
          onClose()
        }
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose, editing])

  const startEdit = (filename: string) => {
    setDraft(memory[filename] || '')
    setEditing(filename)
  }

  const handleSave = async () => {
    if (!editing) return
    setSaving(true)
    try {
      await onSave(editing, draft)
      setEditing(null)
    } catch (err) {
      console.error('Save error:', err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="global-memory-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Global Memory</h3>
          <button className="modal-close" onClick={onClose}>&times;</button>
        </div>
        <div className="global-memory-body">
          {FILE_ORDER.map(filename => (
            <div key={filename} className="global-memory-file">
              <div className="global-memory-file-header">
                <h4>{filename}</h4>
                {filename === 'SOUL.md' && (
                  <span className="global-memory-tag" title="The Main Agent loads SOUL.md every turn as its identity anchor but can never write it — you edit it only here.">
                    read-only to the agent
                  </span>
                )}
                {editing !== filename && (
                  <button className="btn-edit-global" onClick={() => startEdit(filename)}>
                    Edit
                  </button>
                )}
              </div>
              {editing === filename ? (
                <div className="global-memory-editor">
                  <textarea
                    className="global-memory-textarea"
                    value={draft}
                    onChange={e => setDraft(e.target.value)}
                    autoFocus
                  />
                  <div className="global-memory-editor-actions">
                    <button className="btn-save" onClick={handleSave} disabled={saving}>
                      {saving ? 'Saving...' : 'Save'}
                    </button>
                    <button className="btn-cancel" onClick={() => setEditing(null)} disabled={saving}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <pre className="global-memory-content">{memory[filename] || '(empty)'}</pre>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

export default GlobalMemoryModal
