import { useCallback, useEffect, useState } from 'react'
import type { GitCommit, GitHubConnectorStatus } from '../types'
import GitPanel from './GitPanel'

interface Props {
  projectId: string
  connector: GitHubConnectorStatus | null
  refreshSignal?: number
  onClose: () => void
  onSaved?: () => void
}

/**
 * Public UI pass — the GitHub connector opened as a central modal: the
 * connection line + repo target + working-tree status (the GitPanel body),
 * plus an expandable, traceable git-record view (all commits newest-first from
 * the read-only /git/log endpoint). Read-only history; no new mutation path.
 */
function GitHubModal({ projectId, connector, refreshSignal, onClose, onSaved }: Props) {
  const [showHistory, setShowHistory] = useState(false)
  const [commits, setCommits] = useState<GitCommit[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const loadHistory = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/git/log?limit=100`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setCommits(Array.isArray(data.commits) ? data.commits : [])
    } catch {
      setError('Could not load git history.')
      setCommits([])
    } finally {
      setLoading(false)
    }
  }, [projectId])

  const toggleHistory = () => {
    const next = !showHistory
    setShowHistory(next)
    if (next && commits === null) void loadHistory()
  }

  const connLine = connector?.connected
    ? `Connected as ${connector.login || '—'}`
    : connector?.configured
      ? 'Token configured (not validated)'
      : 'No token'

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="github-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>GitHub connector</h3>
          <button className="modal-close" onClick={onClose}>&times;</button>
        </div>
        <div className="github-modal-body">
          <div className={`git-conn-badge${connector?.connected ? ' connected' : ''}`}>
            {connLine}
          </div>

          <GitPanel projectId={projectId} refreshSignal={refreshSignal} connector={connector} onSaved={onSaved} />

          <div className="git-history">
            <button
              type="button"
              className={`git-history-toggle${showHistory ? ' open' : ''}`}
              onClick={toggleHistory}
              aria-expanded={showHistory}
            >
              {showHistory ? '▾' : '▸'} Git history
              <span className="run-chat-muted"> · all commits, newest first</span>
            </button>
            {showHistory && (
              <div className="git-history-body">
                {loading && <div className="run-chat-muted">Loading history…</div>}
                {error && <div className="run-chat-error">{error}</div>}
                {!loading && !error && commits && commits.length === 0 && (
                  <div className="run-chat-muted">No commits yet.</div>
                )}
                {commits && commits.length > 0 && (
                  <ul className="git-history-list">
                    {commits.map((c) => (
                      <li key={c.hash} className="git-commit-row">
                        <code className="git-commit-hash">{c.short}</code>
                        <div className="git-commit-main">
                          <span className="git-commit-subject" title={c.subject}>{c.subject}</span>
                          <span className="git-commit-meta">
                            {c.author} · {c.date}
                          </span>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default GitHubModal
