import { useState, useRef, useEffect } from 'react'
import type { ModelInfo } from '../types'

interface Props {
  /** The current provider's selectable models (capability-tagged). */
  models: ModelInfo[]
  /** Currently selected model id. */
  selectedModel: string
  /** Select a model id. */
  onSelect: (modelId: string) => void
  disabled?: boolean
}

/**
 * Provider Registry 2.0 — a compact, upward-opening model picker that sits on
 * the right of the composer. The trigger shows the selected model's label and a
 * small vision/text capability cue; clicking opens a popover *above* it listing
 * the provider's models, each tagged with its image-input capability. Closes on
 * outside click, Escape, or selection. Renders nothing when the provider has no
 * models (e.g. an unknown provider).
 */
function ModelPicker({ models, selectedModel, onSelect, disabled }: Props) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!models || models.length === 0) return null

  const current = models.find(m => m.id === selectedModel)
  const label = current?.label || selectedModel || 'Model'

  return (
    <div className="model-picker" ref={ref}>
      <button
        type="button"
        className="model-picker-trigger"
        onClick={() => setOpen(o => !o)}
        disabled={disabled}
        title={`Model: ${label}${current && !current.vision ? ' (text only — no image input)' : ''}`}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        {current && (
          <span
            className={`model-picker-cap ${current.vision ? 'vision' : 'text'}`}
            aria-hidden="true"
          >
            {current.vision ? '👁' : 'T'}
          </span>
        )}
        {/* Compact: a fixed "Model" label, not the (long) model name. The
            selected model is in the tooltip + highlighted in the dropdown. */}
        <span className="model-picker-label">Model</span>
        <span className="model-picker-caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <ul className="model-picker-menu" role="listbox">
          {models.map(m => (
            <li key={m.id} role="option" aria-selected={m.id === selectedModel}>
              <button
                type="button"
                className={`model-picker-option${m.id === selectedModel ? ' selected' : ''}`}
                onClick={() => {
                  onSelect(m.id)
                  setOpen(false)
                }}
              >
                <span className="model-picker-option-label">{m.label}</span>
                <span className={`model-picker-vision ${m.vision ? 'yes' : 'no'}`}>
                  {m.vision ? '👁 vision' : 'text'}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default ModelPicker
