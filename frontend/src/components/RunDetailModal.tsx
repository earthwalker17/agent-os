import { useCallback, useEffect, useState } from 'react'
import type {
  BrowserVerificationResult,
  ExecutionPlan,
  RunEvent,
  RunRecord,
  VerificationResult,
} from '../types'
import RunTimeline from './RunTimeline'

interface Props {
  projectId: string
  runId: string
  onClose: () => void
  /** Run control — let the parent refresh its run list after a cancel/retry. */
  onRunsChanged?: () => void
  /** Open the lightweight Live Trace modal for this run. */
  onOpenTrace?: (runId: string) => void
}

const POLL_INTERVAL_MS = 2000

function isActive(r: RunRecord | null): boolean {
  if (!r) return false
  return (
    r.status === 'running' ||
    r.verification_state === 'verifying' ||
    r.verification_state === 'repairing' ||
    r.browser_verification_state === 'running'
  )
}

function bullets(items: string[] | undefined | null): JSX.Element {
  const list = items ?? []
  if (list.length === 0) return <span className="run-detail-none">None</span>
  return (
    <ul className="run-detail-list">
      {list.map((x, i) => (
        <li key={i}>{x}</li>
      ))}
    </ul>
  )
}

function VerificationBlock({ v }: { v: VerificationResult | null | undefined }): JSX.Element {
  if (!v) {
    return <span className="run-detail-none">Not run</span>
  }
  if (!v.enabled) {
    return (
      <div className="run-detail-verification">
        <div>
          <span className={`run-verify-status status-skipped`}>skipped</span>
          <span className="run-detail-verify-meta">
            {v.mode === 'skipped'
              ? 'no safe verify command could be inferred'
              : 'no verify command configured'}
          </span>
        </div>
      </div>
    )
  }
  const commands = v.commands ?? []
  return (
    <div className="run-detail-verification">
      <div>
        <span className={`run-verify-status status-${v.status}`}>{v.status}</span>
        {v.mode && <span className="run-detail-verify-meta">{v.mode}</span>}
        {(v.repair_attempts ?? 0) > 0 && (
          <span className="run-detail-verify-meta">
            {v.repair_attempts} repair pass{(v.repair_attempts ?? 0) === 1 ? '' : 'es'}
          </span>
        )}
        {typeof v.duration_ms === 'number' && (
          <span className="run-detail-verify-meta">{v.duration_ms} ms</span>
        )}
      </div>
      {commands.length > 0 ? (
        <ul className="run-detail-verify-cmd-list">
          {commands.map((c, i) => (
            <li key={i}>
              <span className={`run-verify-status status-${c.status}`}>{c.status}</span>
              <span className="run-detail-verify-meta">{c.kind}</span>
              <code>{c.command}</code>
              {typeof c.exit_code === 'number' && (
                <span className="run-detail-verify-meta">exit {c.exit_code}</span>
              )}
              {c.status === 'failed' && c.output_preview && (
                <pre className="run-detail-verify-output">{c.output_preview}</pre>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <>
          {v.command && (
            <div className="run-detail-verify-cmd">
              <span className="run-detail-label">Command</span>
              <code>{v.command}</code>
            </div>
          )}
          {v.output_preview && (
            <pre className="run-detail-verify-output">{v.output_preview}</pre>
          )}
        </>
      )}
    </div>
  )
}

function BrowserVerificationBlock({
  projectId,
  runId,
  v,
}: {
  projectId: string
  runId: string
  v: BrowserVerificationResult | null | undefined
}): JSX.Element {
  if (!v) {
    return <span className="run-detail-none">Not run</span>
  }
  if (!v.enabled) {
    return (
      <div className="run-detail-verification">
        <div>
          <span className={`run-verify-status status-skipped`}>skipped</span>
          <span className="run-detail-verify-meta">no browser verification configured</span>
        </div>
      </div>
    )
  }
  // Task 06.2D — the modal is the detailed inspection view, not the primary
  // control surface. We reference the saved screenshot path + a link to the
  // artifact rather than rendering it inline; the chat run card owns the
  // visual preview and the Run browser verification action.
  const screenshotUrl = v.screenshot_path
    ? `/api/projects/${projectId}/execution/runs/${runId}/screenshot`
    : null
  return (
    <div className="run-detail-verification">
      <div>
        <span className={`run-verify-status status-${v.status}`}>{v.status}</span>
        {typeof v.duration_ms === 'number' && (
          <span className="run-detail-verify-meta">{v.duration_ms} ms</span>
        )}
      </div>
      {v.install_status && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">Install</span>
          <span className={`run-verify-status status-${v.install_status}`}>
            {v.install_status}
          </span>
          {v.install_command && <code>{v.install_command}</code>}
        </div>
      )}
      {v.install_output_preview && (
        <pre className="run-detail-verify-output">{v.install_output_preview}</pre>
      )}
      {v.command && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">Command</span>
          <code>{v.command}</code>
        </div>
      )}
      {v.url && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">URL</span>
          <code>{v.url}</code>
        </div>
      )}
      {v.screenshot_path && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">Screenshot</span>
          <code>{v.screenshot_path}</code>
          {screenshotUrl && (
            <a
              className="run-detail-screenshot-link"
              href={screenshotUrl}
              target="_blank"
              rel="noreferrer"
            >
              open artifact
            </a>
          )}
        </div>
      )}
      {v.output_preview && (
        <pre className="run-detail-verify-output">{v.output_preview}</pre>
      )}
    </div>
  )
}

function PlanBlock({ plan }: { plan: ExecutionPlan | null | undefined }): JSX.Element | null {
  const tasks = plan?.tasks ?? []
  if (!plan || tasks.length === 0) return null
  return (
    <div className="run-detail-plan">
      {plan.goal && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">Goal</span>
          <span>{plan.goal}</span>
        </div>
      )}
      <div>
        {plan.mode && <span className="run-detail-verify-meta">{plan.mode}</span>}
        <span className="run-detail-verify-meta">
          {tasks.length} task{tasks.length === 1 ? '' : 's'}
        </span>
      </div>
      {plan.risks && plan.risks.length > 0 && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">Risks</span>
          {bullets(plan.risks)}
        </div>
      )}
      <ol className="run-detail-task-list">
        {tasks.map((t) => (
          <li key={t.id}>
            <span className={`run-verify-status status-${t.status}`}>{t.status}</span>
            <span className="run-detail-task-title">{t.title}</span>
            {t.depends_on && t.depends_on.length > 0 && (
              <span className="run-detail-verify-meta">after {t.depends_on.join(', ')}</span>
            )}
            {t.summary && <div className="run-detail-task-summary">{t.summary}</div>}
            {t.blockers && t.blockers.length > 0 && (
              <div className="run-detail-task-blockers">{t.blockers.join('; ')}</div>
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}

function RunDetailModal({ projectId, runId, onClose, onRunsChanged, onOpenTrace }: Props) {
  const [record, setRecord] = useState<RunRecord | null>(null)
  const [resultMd, setResultMd] = useState<string>('')
  const [events, setEvents] = useState<RunEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Run control (cancel / retry).
  const [controlBusy, setControlBusy] = useState(false)
  const [controlError, setControlError] = useState<string | null>(null)
  const [retriedRunId, setRetriedRunId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [recRes, resRes, evRes] = await Promise.all([
        fetch(`/api/projects/${projectId}/execution/runs/${runId}`),
        fetch(`/api/projects/${projectId}/execution/runs/${runId}/result`),
        fetch(`/api/projects/${projectId}/execution/runs/${runId}/events`),
      ])
      if (!recRes.ok) throw new Error(`run record HTTP ${recRes.status}`)
      const rec: RunRecord = await recRes.json()
      setRecord(rec)
      if (resRes.ok) {
        const data = await resRes.json()
        setResultMd(typeof data?.content === 'string' ? data.content : '')
      } else {
        setResultMd('')
      }
      if (evRes.ok) {
        const data = await evRes.json()
        setEvents(Array.isArray(data?.events) ? data.events : [])
      }
    } catch (err) {
      console.error('Failed to load run detail:', err)
      setError(err instanceof Error ? err.message : 'Failed to load run')
    } finally {
      setLoading(false)
    }
  }, [projectId, runId])

  // Silent refresh (no loading flash) for polling an active run.
  const refresh = useCallback(async () => {
    try {
      const [recRes, evRes] = await Promise.all([
        fetch(`/api/projects/${projectId}/execution/runs/${runId}`),
        fetch(`/api/projects/${projectId}/execution/runs/${runId}/events`),
      ])
      if (recRes.ok) setRecord(await recRes.json())
      if (evRes.ok) {
        const data = await evRes.json()
        setEvents(Array.isArray(data?.events) ? data.events : [])
      }
    } catch (err) {
      console.error('Run detail refresh failed:', err)
    }
  }, [projectId, runId])

  useEffect(() => {
    load()
  }, [load])

  // Poll while the run is active so the timeline + status update live.
  useEffect(() => {
    if (!isActive(record)) return
    const id = window.setInterval(refresh, POLL_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [record, refresh])

  const cancelRun = useCallback(async () => {
    setControlBusy(true)
    setControlError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/execution/runs/${runId}/cancel`, {
        method: 'POST',
      })
      if (!res.ok) {
        const b = await res.json().catch(() => ({}))
        throw new Error(b?.detail || `HTTP ${res.status}`)
      }
      setRecord(await res.json())
    } catch (err) {
      setControlError(err instanceof Error ? err.message : 'Cancel failed')
    } finally {
      setControlBusy(false)
      onRunsChanged?.()
    }
  }, [projectId, runId, onRunsChanged])

  const retryRun = useCallback(async () => {
    setControlBusy(true)
    setControlError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/execution/runs/${runId}/retry`, {
        method: 'POST',
      })
      if (!res.ok) {
        const b = await res.json().catch(() => ({}))
        throw new Error(b?.detail || `HTTP ${res.status}`)
      }
      const rec: RunRecord = await res.json()
      setRetriedRunId(rec.run_id)
    } catch (err) {
      setControlError(err instanceof Error ? err.message : 'Retry failed')
    } finally {
      setControlBusy(false)
      onRunsChanged?.()
    }
  }, [projectId, runId, onRunsChanged])

  // Close on Escape, matching EditModal's pattern
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-content run-detail-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h3>Coding Agent Run</h3>
          <button className="modal-close" onClick={onClose} title="Close">
            ×
          </button>
        </div>

        <div className="run-detail-body">
          {loading && <div className="run-detail-loading">Loading…</div>}
          {error && <div className="runs-error">{error}</div>}

          {record && !loading && (
            <>
              <div className="run-detail-meta">
                <div>
                  <span className="run-detail-label">Run ID</span>
                  <code className="run-detail-runid">{record.run_id}</code>
                </div>
                <div>
                  <span className="run-detail-label">Status</span>
                  <span className={`run-status status-${record.status}`}>
                    {record.status}
                  </span>
                </div>
                <div>
                  <span className="run-detail-label">Task</span>
                  <span>{record.task_title || '(untitled)'}</span>
                </div>
              </div>

              {/* --- run control (cancel / retry) --- */}
              <div className="run-detail-actions">
                {onOpenTrace && (
                  <button
                    type="button"
                    className="run-chat-trace-btn"
                    onClick={() => onOpenTrace(runId)}
                    title="Open the live execution trace — a faster, chronological activity thread"
                  >
                    Open live trace
                  </button>
                )}
                {record.status === 'running' && (
                  <button
                    type="button"
                    className="run-chat-cancel-btn"
                    onClick={cancelRun}
                    disabled={controlBusy || !!record.cancel_requested}
                    title="Stop this run at its next step boundary"
                  >
                    {record.cancel_requested ? 'Cancelling…' : 'Cancel run'}
                  </button>
                )}
                {['partial', 'blocked', 'failed', 'cancelled'].includes(record.status) &&
                  !retriedRunId && (
                    <button
                      type="button"
                      className="run-chat-retry-btn"
                      onClick={retryRun}
                      disabled={controlBusy}
                      title="Dispatch a new run from the same task card"
                    >
                      {controlBusy ? 'Retrying…' : 'Retry'}
                    </button>
                  )}
                {retriedRunId && (
                  <span className="run-chat-muted">
                    Retried as new run <code>{retriedRunId}</code> — see the Runs list.
                  </span>
                )}
                {controlError && <span className="runs-error">{controlError}</span>}
              </div>

              {record.plan && (record.plan.tasks?.length ?? 0) > 1 && (
                <section className="run-detail-result">
                  <h4>Plan &amp; Tasks</h4>
                  <PlanBlock plan={record.plan} />
                </section>
              )}

              <section className="run-detail-result">
                <h4>Timeline</h4>
                <RunTimeline events={events} runActive={isActive(record)} />
              </section>

              <div className="run-detail-grid">
                <section>
                  <h4>Files Changed</h4>
                  {bullets(record.files_changed)}
                </section>
                <section>
                  <h4>Commands Run</h4>
                  {bullets(record.commands_run)}
                </section>
                <section>
                  <h4>Blockers</h4>
                  {bullets(record.blockers)}
                </section>
              </div>

              <section className="run-detail-result">
                <h4>Verification</h4>
                <VerificationBlock v={record.verification} />
              </section>

              <section className="run-detail-result">
                <h4>Browser Verification</h4>
                <BrowserVerificationBlock
                  projectId={projectId}
                  runId={runId}
                  v={record.browser_verification}
                />
                <p className="run-detail-hint">
                  Run or re-run browser verification from the run's message in
                  the chat thread.
                </p>
              </section>

              <section className="run-detail-result">
                <h4>result.md</h4>
                {resultMd ? (
                  <pre className="run-detail-resultmd">{resultMd}</pre>
                ) : (
                  <div className="run-detail-none">_(no result.md)_</div>
                )}
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

export default RunDetailModal
