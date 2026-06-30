import { useCallback, useEffect, useRef, useState } from 'react'
import type { RunRecord, ExternalActionContract, ExternalActionResponse } from '../types'
import ExternalActionPanel from './ExternalActionPanel'

interface Props {
  projectId: string
  runId: string
  record: RunRecord
  onRecordChange: (rec: RunRecord) => void
  compact?: boolean
}

interface SupaStatus {
  configured?: boolean
  connected?: boolean
  linked?: boolean
  project_ref?: string | null
  error?: string | null
}

/**
 * Phase 8 — Supabase migration controls for a run. "Apply migrations" is a
 * two-phase External Action Contract: the preview runs `db push --dry-run`
 * (lists the pending migrations, Docker-optional) + a best-effort `db diff`
 * (exact SQL when Docker is up); confirm applies them to the LINKED remote DB.
 * Linking is a separate confirm. Nothing runs on render; the DB password is used
 * via env only and never shown.
 */
function MigrationPanel({ projectId, runId, record, onRecordChange, compact }: Props) {
  const [status, setStatus] = useState<SupaStatus | null>(null)
  const [pending, setPending] = useState<{ kind: 'migration_apply' | 'link_project'; contract: ExternalActionContract } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [projectRef, setProjectRef] = useState('')
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    let active = true
    fetch(`/api/projects/${projectId}/supabase/status`)
      .then((r) => (r.ok ? r.json() : null))
      .then((s) => {
        if (active && s) {
          setStatus(s)
          if (s.project_ref) setProjectRef(s.project_ref)
        }
      })
      .catch(() => {})
    return () => {
      active = false
    }
  }, [projectId, record.external_state])

  const post = useCallback(
    async (path: string, payload: Record<string, unknown>): Promise<ExternalActionResponse> => {
      const res = await fetch(`/api/projects/${projectId}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const b = await res.json()
          if (b?.detail) detail = b.detail
        } catch {
          /* keep */
        }
        throw new Error(detail)
      }
      return res.json()
    },
    [projectId],
  )

  const previewMigration = async () => {
    setBusy(true)
    setError(null)
    try {
      const data = await post(`/execution/runs/${runId}/supabase/migration`, { confirm: false })
      if (mounted.current) setPending({ kind: 'migration_apply', contract: data.contract })
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : 'preview failed')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const previewLink = async () => {
    if (!projectRef.trim()) {
      setError('Enter the Supabase project ref')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const data = await post(`/supabase/link`, { project_ref: projectRef.trim(), confirm: false })
      if (mounted.current) setPending({ kind: 'link_project', contract: data.contract })
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : 'preview failed')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const confirm = async () => {
    if (!pending) return
    setBusy(true)
    setError(null)
    try {
      if (pending.kind === 'migration_apply') {
        const data = await post(`/execution/runs/${runId}/supabase/migration`, { confirm: true })
        if (mounted.current && data.run) onRecordChange(data.run)
      } else {
        await post(`/supabase/link`, { project_ref: projectRef.trim(), confirm: true })
      }
      if (mounted.current) setPending(null)
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : 'action failed')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const inFlight = record.external_state === 'migrating' || busy
  const configured = !!status?.configured
  const linked = !!status?.linked

  return (
    <div className={`gitops-panel${compact ? ' gitops-compact' : ''}`}>
      <div className="gitops-head">
        <strong>Database (Supabase)</strong>
        {record.external_state === 'migrating' && (
          <span className="gitops-state">
            <span className="run-chat-dot" /> migrating…
          </span>
        )}
      </div>

      {!configured ? (
        <p className="run-chat-muted">No Supabase token — connect Supabase from the Connectors panel.</p>
      ) : (
        <div className="gitops-status">
          <span className="gitops-badge">{linked ? `linked: ${status?.project_ref}` : 'not linked'}</span>
          {status?.error && <span className="run-chat-error">{status.error}</span>}
        </div>
      )}

      <div className="gitops-actions">
        {configured && !linked && (
          <>
            <input
              className="connector-input"
              style={{ flex: '1 1 120px' }}
              placeholder="project ref"
              value={projectRef}
              onChange={(e) => setProjectRef(e.target.value)}
            />
            <button type="button" className="gitops-btn" onClick={previewLink} disabled={inFlight}>
              Link…
            </button>
          </>
        )}
        <button
          type="button"
          className="gitops-btn gitops-danger"
          onClick={previewMigration}
          disabled={inFlight || !configured || !linked}
          title={linked ? 'Apply pending migrations to the linked DB' : 'Link a Supabase project first'}
        >
          Apply migrations…
        </button>
      </div>

      {error && <div className="run-chat-error">{error}</div>}

      {pending && (
        <ExternalActionPanel
          contract={pending.contract}
          busy={busy}
          confirmLabel={pending.kind === 'migration_apply' ? 'Apply to linked DB' : 'Link project'}
          onConfirm={confirm}
          onCancel={() => setPending(null)}
        >
          {pending.kind === 'migration_apply' ? (
            <>
              <p className="run-chat-muted">{pending.contract.summary}</p>
              {pending.contract.pending && (
                <>
                  <label className="gitops-label">Pending (dry-run)</label>
                  <pre className="gitops-diff">{pending.contract.pending}</pre>
                </>
              )}
              {pending.contract.diff_available && pending.contract.diff && (
                <>
                  <label className="gitops-label">Exact SQL diff</label>
                  <pre className="gitops-diff">{pending.contract.diff}</pre>
                </>
              )}
              {pending.contract.docker_note && (
                <p className="run-chat-muted">{pending.contract.docker_note}</p>
              )}
            </>
          ) : (
            <p className="run-chat-muted">{pending.contract.summary}</p>
          )}
        </ExternalActionPanel>
      )}
    </div>
  )
}

export default MigrationPanel
