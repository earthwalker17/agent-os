import { useState } from 'react'
import type { GitHubConnectorStatus } from '../types'

interface Props {
  projectId: string
  status: GitHubConnectorStatus | null
  onClose: () => void
  onSaved: (status: GitHubConnectorStatus) => void
}

/**
 * Phase 7 — GitHub connector setup. The token is sent to the backend credential
 * store (gitignored) and never returned, logged, or shown again; the UI only
 * ever displays presence + login.
 */
function ConnectorModal({ projectId, status, onClose, onSaved }: Props) {
  const [token, setToken] = useState('')
  const [scope, setScope] = useState<'project' | 'global'>('project')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    if (!token.trim()) {
      setError('Paste a token first')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/credentials/github`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token.trim(), scope }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
      onSaved(data)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save token')
    } finally {
      setBusy(false)
    }
  }

  const disconnect = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(
        `/api/projects/${projectId}/credentials/github?scope=${scope}`,
        { method: 'DELETE' },
      )
      const data = await res.json().catch(() => ({}))
      onSaved(data)
      onClose()
    } catch {
      setError('Failed to disconnect')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="connector-modal" onClick={(e) => e.stopPropagation()}>
        <h3>Connect GitHub</h3>
        <p className="run-chat-muted">
          Paste a GitHub Personal Access Token (classic <code>repo</code> scope, or a
          fine-grained token with Contents + Pull requests read/write). It is stored in
          the gitignored credential store and never appears in prompts, logs, commits, or the UI.
        </p>
        {status?.configured && (
          <p className="run-chat-muted">
            Currently:{' '}
            {status.connected ? `connected as ${status.login || '—'}` : 'configured (not validated)'}{' '}
            · scope {status.scope} · source {status.source}
          </p>
        )}
        <label className="gitops-label">Token</label>
        <input
          type="password"
          className="connector-input"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="ghp_…"
          autoFocus
        />
        <label className="gitops-label">Scope</label>
        <select
          className="connector-select"
          value={scope}
          onChange={(e) => setScope(e.target.value as 'project' | 'global')}
        >
          <option value="project">This project</option>
          <option value="global">Global (all projects)</option>
        </select>
        {error && <div className="run-chat-error">{error}</div>}
        <div className="modal-actions">
          <button className="gitops-confirm" onClick={save} disabled={busy}>
            {busy ? 'Saving…' : 'Save & connect'}
          </button>
          {status?.configured && (
            <button className="btn-cancel" onClick={disconnect} disabled={busy}>
              Disconnect
            </button>
          )}
          <button className="btn-cancel" onClick={onClose} disabled={busy}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}

export default ConnectorModal
