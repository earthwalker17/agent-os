import { useCallback, useEffect, useState } from 'react'
import type { GitHubConnectorStatus, GitStatus } from '../types'

interface Props {
  projectId: string
  refreshSignal?: number
  /** Live GitHub connector status, fetched by the parent IntegrationsPanel. */
  connector: GitHubConnectorStatus | null
  /** Notify the parent (e.g. to refresh status) after a repo target is saved. */
  onSaved?: () => void
}

interface RepoInfo {
  repo: string | null
  url: string | null
}

/**
 * GitHub integration detail — the project's target GitHub repo (entered here,
 * shown as a clickable link once set / pushed) and the live working-tree
 * status. Rendered inside the Integrations panel's GitHub card; the account
 * token itself lives in backend/.env, never the UI.
 */
function GitPanel({ projectId, refreshSignal, connector, onSaved }: Props) {
  const [git, setGit] = useState<GitStatus | null>(null)
  const [repo, setRepo] = useState<RepoInfo>({ repo: null, url: null })
  const [editing, setEditing] = useState(false)
  const [repoInput, setRepoInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    const getJson = async (path: string, fallback: unknown) => {
      try {
        const r = await fetch(`/api/projects/${projectId}${path}`)
        return r.ok ? await r.json() : fallback
      } catch {
        return fallback
      }
    }
    const [g, r] = await Promise.all([
      getJson('/git/status', null),
      getJson('/github/repo', { repo: null, url: null }),
    ])
    setGit(g as GitStatus | null)
    setRepo((r as RepoInfo) || { repo: null, url: null })
  }, [projectId])

  useEffect(() => {
    load()
  }, [load, refreshSignal])

  const saveRepo = async () => {
    if (!repoInput.trim()) {
      setError('Enter a GitHub repo (owner/repo or a github.com URL)')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/github/repo`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_url: repoInput.trim() }),
      })
      const d = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(d?.detail || `HTTP ${res.status}`)
      setRepo(d)
      setEditing(false)
      setRepoInput('')
      onSaved?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to set repo')
    } finally {
      setBusy(false)
    }
  }

  const uncommitted = (git?.untracked ?? 0) + (git?.modified ?? 0) + (git?.staged ?? 0)

  return (
    <div className="git-panel-body">
      {connector?.source && connector.source !== 'none' && (
        <div className="git-panel-row">
          <span className="run-chat-muted">token from {connector.source}</span>
        </div>
      )}
      {!connector?.configured && (
        <p className="run-chat-muted">
          Set <code>GITHUB_TOKEN</code> in <code>backend/.env</code> (account-level — shared by all projects).
        </p>
      )}

      <label className="gitops-label">GitHub repository</label>
      {repo.url && !editing ? (
        <div className="git-panel-row">
          <a className="external-link" href={repo.url} target="_blank" rel="noreferrer" title={repo.url}>
            {repo.repo}
          </a>
          <button
            type="button"
            className="run-row-trace-btn"
            onClick={() => {
              setRepoInput(repo.repo || '')
              setEditing(true)
            }}
          >
            Change
          </button>
        </div>
      ) : (
        <div className="env-add">
          <input
            className="connector-input"
            placeholder="owner/repo or https://github.com/owner/repo"
            value={repoInput}
            onChange={(e) => setRepoInput(e.target.value)}
          />
          <button className="gitops-btn" onClick={saveRepo} disabled={busy}>
            {busy ? 'Saving…' : 'Save'}
          </button>
          {repo.url && (
            <button
              type="button"
              className="gitops-cancel"
              onClick={() => {
                setEditing(false)
                setError(null)
              }}
            >
              Cancel
            </button>
          )}
        </div>
      )}
      {!repo.url && !editing && (
        <p className="run-chat-muted">
          Paste the GitHub repo this project pushes to. It becomes a clickable link here after it's set.
        </p>
      )}

      <div className="git-panel-row" style={{ marginTop: 6 }}>
        {git?.is_repo ? (
          <span className="git-branch-badge" title="Current branch + working-tree state">
            ⎇ {git.branch || 'detached'}
            {git.dirty ? (
              <span className="git-dirty"> · {uncommitted} uncommitted</span>
            ) : (
              <span className="git-clean"> · clean</span>
            )}
            {git.head && <span className="run-chat-muted"> · {git.head.slice(0, 10)}</span>}
          </span>
        ) : (
          <span className="run-chat-muted">No git repo yet (created on the first coding-agent run).</span>
        )}
      </div>

      {error && <div className="run-chat-error">{error}</div>}
    </div>
  )
}

export default GitPanel
