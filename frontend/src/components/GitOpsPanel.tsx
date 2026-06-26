import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  RunRecord,
  GitActionContract,
  GitActionResponse,
  GitHubConnectorStatus,
} from '../types'

interface Props {
  projectId: string
  runId: string
  record: RunRecord
  /** Push the updated run record back to the parent (card/modal) after an action. */
  onRecordChange: (rec: RunRecord) => void
  /** Compact layout for the chat card; full layout for the detail modal. */
  compact?: boolean
}

type ActionKind = 'commit' | 'push' | 'pr' | 'rollback'

const ENDPOINT: Record<ActionKind, string> = {
  commit: 'git/commit',
  push: 'git/push',
  pr: 'github/pr',
  rollback: 'git/rollback',
}

/**
 * Phase 7 — Project Ops controls for a terminal run: review the diff, commit,
 * push, open a PR, and roll back. Each external/destructive action is a two-phase
 * External Action Contract — clicking an action fetches a preview (confirm:false)
 * and renders it for explicit confirmation; only "Confirm" executes
 * (confirm:true). Nothing here runs on render.
 */
function GitOpsPanel({ projectId, runId, record, onRecordChange, compact }: Props) {
  const [pending, setPending] = useState<{ kind: ActionKind; contract: GitActionContract } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [diff, setDiff] = useState<string | null>(null)
  const [showDiff, setShowDiff] = useState(false)
  const [message, setMessage] = useState('')
  const [connector, setConnector] = useState<GitHubConnectorStatus | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    let active = true
    fetch(`/api/projects/${projectId}/github/connector`)
      .then((r) => (r.ok ? r.json() : null))
      .then((s) => {
        if (active && s) setConnector(s)
      })
      .catch(() => {})
    return () => {
      active = false
    }
  }, [projectId])

  const post = useCallback(
    async (kind: ActionKind, payload: Record<string, unknown>): Promise<GitActionResponse> => {
      const res = await fetch(
        `/api/projects/${projectId}/execution/runs/${runId}/${ENDPOINT[kind]}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        },
      )
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const b = await res.json()
          if (b?.detail) detail = b.detail
        } catch {
          /* keep status */
        }
        throw new Error(detail)
      }
      return res.json()
    },
    [projectId, runId],
  )

  const preview = useCallback(
    async (kind: ActionKind) => {
      setBusy(true)
      setError(null)
      try {
        const data = await post(kind, { confirm: false })
        if (!mounted.current) return
        setPending({ kind, contract: data.contract })
        if (kind === 'commit') setMessage(data.contract.message || '')
        onRecordChange(data.run)
      } catch (e) {
        if (mounted.current) setError(e instanceof Error ? e.message : 'action failed')
      } finally {
        if (mounted.current) setBusy(false)
      }
    },
    [post, onRecordChange],
  )

  const confirm = useCallback(async () => {
    if (!pending) return
    setBusy(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = { confirm: true }
      if (pending.kind === 'commit') payload.message = message
      const data = await post(pending.kind, payload)
      if (!mounted.current) return
      onRecordChange(data.run)
      setPending(null)
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : 'action failed')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }, [pending, message, post, onRecordChange])

  const toggleDiff = useCallback(async () => {
    if (showDiff) {
      setShowDiff(false)
      return
    }
    try {
      const res = await fetch(`/api/projects/${projectId}/execution/runs/${runId}/diff`)
      const data = await res.json()
      if (mounted.current) {
        setDiff(data.available ? data.diff || '(empty diff)' : '(no diff captured for this run)')
        setShowDiff(true)
      }
    } catch {
      if (mounted.current) setError('could not load diff')
    }
  }, [projectId, runId, showDiff])

  const gitState = record.git_state
  const inFlight = !!gitState || busy
  const connected = !!connector?.connected
  const tokenConfigured = !!connector?.configured

  // delivery-state line
  const badges: string[] = []
  if (record.branch) badges.push(`branch ${record.branch}`)
  if (record.commit_sha) badges.push(`commit ${record.commit_sha.slice(0, 7)}`)
  if (record.pushed) badges.push('pushed')

  return (
    <div className={`gitops-panel${compact ? ' gitops-compact' : ''}`}>
      <div className="gitops-head">
        <strong>Project Ops</strong>
        {gitState && (
          <span className="gitops-state">
            <span className="run-chat-dot" /> {gitState.replace(/_/g, ' ')}…
          </span>
        )}
      </div>

      {(record.diff_stat || badges.length > 0 || record.pr_url) && (
        <div className="gitops-status">
          {record.diff_stat && <span className="gitops-stat">{record.diff_stat}</span>}
          {badges.map((b) => (
            <span key={b} className="gitops-badge">
              {b}
            </span>
          ))}
          {record.pr_url && (
            <a className="gitops-prlink" href={record.pr_url} target="_blank" rel="noreferrer">
              PR{record.pr_number ? ` #${record.pr_number}` : ''} ↗
            </a>
          )}
        </div>
      )}

      <div className="gitops-actions">
        <button type="button" className="gitops-btn" onClick={toggleDiff} disabled={inFlight}>
          {showDiff ? 'Hide diff' : 'Review diff'}
        </button>
        <button type="button" className="gitops-btn" onClick={() => preview('commit')} disabled={inFlight}>
          Commit…
        </button>
        <button
          type="button"
          className="gitops-btn"
          onClick={() => preview('push')}
          disabled={inFlight || !record.commit_sha}
          title={record.commit_sha ? 'Push the branch to GitHub' : 'Commit first'}
        >
          Push…
        </button>
        <button
          type="button"
          className="gitops-btn"
          onClick={() => preview('pr')}
          disabled={inFlight || !record.pushed}
          title={record.pushed ? 'Open a pull request' : 'Push the branch first'}
        >
          Create PR…
        </button>
        {record.base_commit && (
          <button
            type="button"
            className="gitops-btn gitops-danger"
            onClick={() => preview('rollback')}
            disabled={inFlight}
            title="Discard this run's changes and restore the pre-run state"
          >
            Roll back…
          </button>
        )}
      </div>

      {showDiff && <pre className="gitops-diff">{diff}</pre>}

      {error && <div className="run-chat-error">{error}</div>}

      {/* --- contract preview / confirmation --- */}
      {pending && (
        <div className={`gitops-contract${pending.contract.destructive ? ' destructive' : ''}`}>
          <div className="gitops-contract-head">
            <strong>{pending.contract.title}</strong>
            {pending.contract.external && <span className="gitops-tag external">external</span>}
            {pending.contract.destructive && <span className="gitops-tag danger">destructive</span>}
          </div>

          {pending.kind === 'commit' && (
            <>
              <label className="gitops-label">Commit message</label>
              <textarea
                className="gitops-message"
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                rows={3}
              />
              {pending.contract.files && pending.contract.files.length > 0 && (
                <p className="run-chat-muted">
                  Files: {pending.contract.files.slice(0, 8).join(', ')}
                  {pending.contract.files.length > 8 ? ` +${pending.contract.files.length - 8} more` : ''}
                </p>
              )}
              {pending.contract.refused && pending.contract.refused.length > 0 && (
                <p className="run-chat-muted gitops-refused">
                  Refused (secret-looking, will NOT be committed): {pending.contract.refused.join(', ')}
                </p>
              )}
            </>
          )}

          {pending.kind === 'push' && (
            <>
              <p className="run-chat-muted">
                Push <code>{pending.contract.branch}</code> to{' '}
                <strong>{pending.contract.target || 'the configured remote'}</strong>.
              </p>
              {!tokenConfigured && (
                <p className="run-chat-error">
                  No GitHub token configured. Connect GitHub from the Runs panel first.
                </p>
              )}
            </>
          )}

          {pending.kind === 'pr' && (
            <>
              <p className="run-chat-muted">
                Open a PR <code>{pending.contract.head}</code> → <code>{pending.contract.base}</code>
                {pending.contract.target ? ` on ${pending.contract.target}` : ''}.
              </p>
              <p className="gitops-prtitle">{pending.contract.pr_title}</p>
              {!pending.contract.pushed && (
                <p className="run-chat-muted">Note: the branch must be pushed first.</p>
              )}
            </>
          )}

          {pending.kind === 'rollback' && (
            <p className="run-chat-muted">
              {pending.contract.summary} Restores to <code>{pending.contract.target}</code>.
            </p>
          )}

          <div className="gitops-contract-actions">
            <button
              type="button"
              className={`gitops-confirm${pending.contract.destructive ? ' gitops-danger' : ''}`}
              onClick={confirm}
              disabled={busy || (pending.kind === 'push' && !tokenConfigured)}
            >
              {busy
                ? 'Working…'
                : pending.kind === 'commit'
                  ? 'Create commit'
                  : pending.kind === 'push'
                    ? 'Push to GitHub'
                    : pending.kind === 'pr'
                      ? 'Open pull request'
                      : 'Roll back'}
            </button>
            <button type="button" className="gitops-cancel" onClick={() => setPending(null)} disabled={busy}>
              Cancel
            </button>
            {!compact && pending.kind !== 'commit' && (
              <span className="run-chat-muted">
                {connected ? `Connected as ${connector?.login || '—'}` : 'GitHub not connected'}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default GitOpsPanel
