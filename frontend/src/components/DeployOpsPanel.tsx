import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  RunRecord,
  ExternalActionContract,
  ExternalActionResponse,
  VercelStatus,
} from '../types'
import ExternalActionPanel from './ExternalActionPanel'

interface Props {
  projectId: string
  runId: string
  record: RunRecord
  onRecordChange: (rec: RunRecord) => void
  compact?: boolean
}

type Kind = 'deploy' | 'redeploy' | 'rollback'

interface DeploymentRow {
  deployment_id?: string
  url?: string | null
  ready_state?: string
  target?: string
}

/**
 * Phase 8 — Production Path controls for a run: deploy the pushed commit to
 * Vercel (gitSource), redeploy, or roll back production to a previous
 * deployment. Each is a two-phase External Action Contract (preview on
 * confirm:false, execute on confirm:true) — nothing runs on render. A deploy is
 * dispatched and finalizes off-thread; the card polls the run for the settled
 * URL via the parent's record refresh.
 */
function DeployOpsPanel({ projectId, runId, record, onRecordChange, compact }: Props) {
  const [pending, setPending] = useState<{ kind: Kind; contract: ExternalActionContract } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [vstatus, setVstatus] = useState<VercelStatus | null>(null)
  const [deployments, setDeployments] = useState<DeploymentRow[]>([])
  const [target, setTarget] = useState('')
  const [environment, setEnvironment] = useState<'preview' | 'production'>('preview')
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    let active = true
    fetch(`/api/projects/${projectId}/vercel/status`)
      .then((r) => (r.ok ? r.json() : null))
      .then((s) => {
        if (active && s) setVstatus(s)
      })
      .catch(() => {})
    return () => {
      active = false
    }
  }, [projectId, record.deployment_id, record.deploy_state])

  const loadDeployments = useCallback(async () => {
    try {
      const r = await fetch(`/api/projects/${projectId}/vercel/deployments`)
      const d = await r.json()
      if (mounted.current && Array.isArray(d.deployments)) {
        setDeployments(d.deployments)
        if (!target && d.deployments[0]?.deployment_id) setTarget(d.deployments[0].deployment_id)
      }
    } catch {
      /* ignore — the picker just stays empty */
    }
  }, [projectId, target])

  const endpointFor = (kind: Kind) =>
    `/api/projects/${projectId}/execution/runs/${runId}/vercel/${kind}`

  const post = useCallback(
    async (kind: Kind, payload: Record<string, unknown>): Promise<ExternalActionResponse> => {
      const res = await fetch(endpointFor(kind), {
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
          /* keep status */
        }
        throw new Error(detail)
      }
      return res.json()
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [projectId, runId],
  )

  const payloadFor = (kind: Kind, confirm: boolean): Record<string, unknown> => {
    if (kind === 'deploy') return { environment, confirm }
    if (kind === 'redeploy') return { deployment_id: target, environment, confirm }
    return { target_deployment_id: target, confirm } // rollback
  }

  const preview = useCallback(
    async (kind: Kind) => {
      if ((kind === 'redeploy' || kind === 'rollback')) await loadDeployments()
      setBusy(true)
      setError(null)
      try {
        const data = await post(kind, payloadFor(kind, false))
        if (!mounted.current) return
        setPending({ kind, contract: data.contract })
        if (data.run) onRecordChange(data.run)
      } catch (e) {
        if (mounted.current) setError(e instanceof Error ? e.message : 'action failed')
      } finally {
        if (mounted.current) setBusy(false)
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [post, onRecordChange, environment, target],
  )

  const confirm = useCallback(async () => {
    if (!pending) return
    setBusy(true)
    setError(null)
    try {
      const data = await post(pending.kind, payloadFor(pending.kind, true))
      if (!mounted.current) return
      if (data.run) onRecordChange(data.run)
      setPending(null)
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : 'action failed')
    } finally {
      if (mounted.current) setBusy(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending, post, onRecordChange, environment, target])

  const deployState = record.deploy_state
  const inFlight = !!deployState || busy
  const tokenOk = !!vstatus?.configured && !!vstatus?.project_id
  const canDeploy = !!(record.commit_sha || record.head_commit || record.pushed)

  const badges: string[] = []
  if (record.deployment_target) badges.push(`target ${record.deployment_target}`)
  if (record.deployment_id) badges.push(record.deployment_id)

  return (
    <div className={`gitops-panel${compact ? ' gitops-compact' : ''}`}>
      <div className="gitops-head">
        <strong>Deploy (Vercel)</strong>
        {deployState && (
          <span className="gitops-state">
            <span className="run-chat-dot" /> {deployState.replace(/_/g, ' ')}…
          </span>
        )}
      </div>

      {(record.deployment_url || badges.length > 0) && (
        <div className="gitops-status">
          {badges.map((b) => (
            <span key={b} className="gitops-badge">
              {b}
            </span>
          ))}
          {record.deployment_url && (
            <a className="gitops-prlink" href={record.deployment_url} target="_blank" rel="noreferrer">
              open ↗
            </a>
          )}
        </div>
      )}

      {!tokenOk && (
        <p className="run-chat-muted">
          {vstatus && !vstatus.configured
            ? 'No Vercel token — connect Vercel from the Runs panel.'
            : 'Link a Vercel project (set project_id in the Vercel connector).'}
        </p>
      )}

      <div className="gitops-actions">
        <label className="gitops-label" style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
          <select
            className="connector-select"
            value={environment}
            onChange={(e) => setEnvironment(e.target.value as 'preview' | 'production')}
            disabled={inFlight}
          >
            <option value="preview">preview</option>
            <option value="production">production</option>
          </select>
        </label>
        <button
          type="button"
          className="gitops-btn"
          onClick={() => preview('deploy')}
          disabled={inFlight || !canDeploy}
          title={canDeploy ? 'Deploy the pushed commit to Vercel' : 'Commit + push to GitHub first'}
        >
          Deploy…
        </button>
        <button
          type="button"
          className="gitops-btn"
          onClick={() => preview('redeploy')}
          disabled={inFlight}
          title="Redeploy an existing deployment"
        >
          Redeploy…
        </button>
        <button
          type="button"
          className="gitops-btn gitops-danger"
          onClick={() => preview('rollback')}
          disabled={inFlight}
          title="Roll back production to a previous deployment"
        >
          Roll back…
        </button>
      </div>

      {error && <div className="run-chat-error">{error}</div>}

      {pending && (
        <ExternalActionPanel
          contract={pending.contract}
          busy={busy}
          confirmLabel={
            pending.kind === 'deploy' ? 'Deploy' : pending.kind === 'redeploy' ? 'Redeploy' : 'Roll back'
          }
          blockedReason={!tokenOk ? 'Vercel not configured/linked.' : null}
          onConfirm={confirm}
          onCancel={() => setPending(null)}
        >
          {pending.kind === 'deploy' && (
            <p className="run-chat-muted">
              Deploy <code>{pending.contract.git_ref || 'main'}</code>
              {pending.contract.commit ? ` @ ${pending.contract.commit}` : ''} to{' '}
              <strong>{pending.contract.target}</strong>.
            </p>
          )}
          {(pending.kind === 'redeploy' || pending.kind === 'rollback') && (
            <>
              <label className="gitops-label">Target deployment</label>
              <select
                className="connector-select"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
              >
                {deployments.length === 0 && <option value={target}>{target || '(none found)'}</option>}
                {deployments.map((d) => (
                  <option key={d.deployment_id} value={d.deployment_id}>
                    {d.deployment_id} · {d.ready_state || '?'} {d.target ? `(${d.target})` : ''}
                  </option>
                ))}
              </select>
              {pending.kind === 'rollback' && (
                <p className="run-chat-muted">{pending.contract.summary}</p>
              )}
            </>
          )}
        </ExternalActionPanel>
      )}
    </div>
  )
}

export default DeployOpsPanel
