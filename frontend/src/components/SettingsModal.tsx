import { useEffect, useRef } from 'react'
import type { ProviderInfo, ModelInfo } from '../types'

interface Props {
  /** Task 07.1 — all providers; unavailable ones render disabled ("no key"). */
  providers: ProviderInfo[]
  selectedProvider: string
  onSelectProvider: (providerId: string) => void
  /** The selected provider's model options (same list the composer picker uses). */
  models: ModelInfo[]
  selectedModel: string
  onSelectModel: (modelId: string) => void
  /** Task 07.2 — active color theme; persisted by App on change. */
  theme: 'dark' | 'light'
  onSelectTheme: (theme: 'dark' | 'light') => void
  onClose: () => void
}

/**
 * UI polish pass — global settings, opened from the sidebar footer. Hosts the
 * model/provider and theme controls that used to sit in the chat header.
 * Every control is live: it writes through the same App-level handlers the
 * header dropdowns used, so selection + persistence behavior is unchanged.
 */
function SettingsModal({
  providers,
  selectedProvider,
  onSelectProvider,
  models,
  selectedModel,
  onSelectModel,
  theme,
  onSelectTheme,
  onClose,
}: Props) {
  const dialogRef = useRef<HTMLDivElement>(null)

  // Focus the dialog on open; hand focus back to the opener on close, so
  // keyboard users land where they left off (the sidebar Settings button).
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    dialogRef.current?.focus()
    return () => opener?.focus()
  }, [])

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // Already consumed by a nested widget, or aimed at a native <select>
        // popup — let the select close itself instead of the whole modal.
        if (e.defaultPrevented) return
        if ((e.target as HTMLElement | null)?.tagName === 'SELECT') return
        onClose()
        return
      }
      // aria-modal hides the background from assistive tech, so keep real
      // focus inside the dialog too: wrap Tab at the edges.
      if (e.key === 'Tab' && dialogRef.current) {
        const focusables = dialogRef.current.querySelectorAll<HTMLElement>(
          'button, select, [tabindex]:not([tabindex="-1"])',
        )
        if (focusables.length === 0) return
        const first = focusables[0]
        const last = focusables[focusables.length - 1]
        const active = document.activeElement
        if (active && !dialogRef.current.contains(active)) {
          e.preventDefault()
          first.focus()
        } else if (e.shiftKey && (active === first || active === dialogRef.current)) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && active === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="settings-modal"
        onClick={e => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
      >
        <div className="modal-header">
          <h3>Settings</h3>
          <button className="modal-close" onClick={onClose} aria-label="Close settings">
            &times;
          </button>
        </div>

        <div className="settings-body">
          <section className="settings-section">
            <h4 className="settings-section-title">Model</h4>
            <label className="settings-field">
              <span className="settings-label">Provider</span>
              <select
                className="settings-select"
                value={selectedProvider}
                onChange={e => onSelectProvider(e.target.value)}
                aria-label="Model provider"
              >
                {providers.map(p => (
                  <option key={p.id} value={p.id} disabled={!p.available}>
                    {p.label}
                    {p.available ? '' : ' — no key'}
                  </option>
                ))}
              </select>
            </label>
            {models.length > 0 && (
              <label className="settings-field">
                <span className="settings-label">Model</span>
                <select
                  className="settings-select"
                  value={selectedModel}
                  onChange={e => onSelectModel(e.target.value)}
                  aria-label="Model"
                >
                  {models.map(m => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                      {m.vision ? '' : ' — text only'}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <p className="settings-hint">
              Applies to every project and conversation. Providers without a
              configured key are listed but can't be selected.
            </p>
          </section>

          <section className="settings-section">
            <h4 className="settings-section-title">Appearance</h4>
            <div className="settings-field">
              <span className="settings-label">Theme</span>
              <div className="settings-theme-toggle" role="group" aria-label="Color theme">
                <button
                  type="button"
                  className={`settings-theme-option${theme === 'dark' ? ' active' : ''}`}
                  onClick={() => onSelectTheme('dark')}
                  aria-pressed={theme === 'dark'}
                >
                  Dark
                </button>
                <button
                  type="button"
                  className={`settings-theme-option${theme === 'light' ? ' active' : ''}`}
                  onClick={() => onSelectTheme('light')}
                  aria-pressed={theme === 'light'}
                >
                  Light
                </button>
              </div>
            </div>
            <p className="settings-hint">Remembered on this device.</p>
          </section>
        </div>

        <div className="modal-actions">
          <button className="btn-save" onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </div>
  )
}

export default SettingsModal
