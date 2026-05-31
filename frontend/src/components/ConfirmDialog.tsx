import { useEffect, useState } from 'react'

interface Props {
  message: string
  onConfirm: (checkboxChecked: boolean) => void
  onCancel: () => void
  /** When provided, renders a checkbox above the actions. Its checked value
   *  is passed back through onConfirm. */
  checkboxLabel?: string
}

function ConfirmDialog({ message, onConfirm, onCancel, checkboxLabel }: Props) {
  const [checked, setChecked] = useState(false)

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel()
      if (e.key === 'Enter') onConfirm(checked)
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onConfirm, onCancel, checked])

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="confirm-dialog" onClick={e => e.stopPropagation()}>
        <p className="confirm-message">{message}</p>
        {checkboxLabel && (
          <label className="confirm-checkbox">
            <input
              type="checkbox"
              checked={checked}
              onChange={e => setChecked(e.target.checked)}
            />
            {checkboxLabel}
          </label>
        )}
        <div className="modal-actions">
          <button className="btn-danger" onClick={() => onConfirm(checked)}>Delete</button>
          <button className="btn-cancel" onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  )
}

export default ConfirmDialog
