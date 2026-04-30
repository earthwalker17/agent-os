import { useState, useEffect, useRef } from 'react'

interface Props {
  filename: string
  content: string
  onSave: (filename: string, content: string) => Promise<void>
  onClose: () => void
}

function EditModal({ filename, content, onSave, onClose }: Props) {
  const [draft, setDraft] = useState(content)
  const [saving, setSaving] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    textareaRef.current?.focus()
  }, [])

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  const handleSave = async () => {
    setSaving(true)
    try {
      await onSave(filename, draft)
      onClose()
    } catch (err) {
      console.error('Save error:', err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>{filename}</h3>
          <button className="modal-close" onClick={onClose}>&times;</button>
        </div>
        <textarea
          ref={textareaRef}
          className="modal-textarea"
          value={draft}
          onChange={e => setDraft(e.target.value)}
        />
        <div className="modal-actions">
          <button className="btn-save" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save'}
          </button>
          <button className="btn-cancel" onClick={onClose} disabled={saving}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}

export default EditModal
