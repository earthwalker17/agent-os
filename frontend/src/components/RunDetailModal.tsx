import { useCallback, useEffect, useState } from 'react'
import type {
  BrowserVerificationResult,
  ExecutionPlan,
  RecoveryAssessment,
  RunEvent,
  RunRecord,
  VerificationResult,
  VisualReviewResult,
} from '../types'
import RunTimeline from './RunTimeline'
import GitOpsPanel from './GitOpsPanel'

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
    r.browser_verification_state === 'running' ||
    r.git_state != null ||
    r.integration_state != null
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
  // control surface. We reference the saved screenshot path(s) + a link to each
  // artifact rather than rendering them inline; the chat run card owns the
  // visual preview and the Run browser verification action.
  const pageUrl = (path: string) => {
    const name = path.split('/').pop() || 'browser.png'
    return `/api/projects/${projectId}/execution/runs/${runId}/screenshot?name=${encodeURIComponent(name)}`
  }
  const pages =
    v.pages && v.pages.length > 0
      ? v.pages
      : v.screenshot_path
        ? [{ path: v.screenshot_path, label: 'Home', readiness: v.readiness ?? undefined }]
        : []
  return (
    <div className="run-detail-verification">
      <div>
        <span className={`run-verify-status status-${v.status}`}>{v.status}</span>
        {v.readiness && (
          <span className="run-detail-verify-meta">readiness: {v.readiness}</span>
        )}
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
      {pages.length > 0 && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">
            {pages.length > 1 ? `Screenshots (${pages.length})` : 'Screenshot'}
          </span>
          <ul className="run-detail-shotlist">
            {pages.map((p) => (
              <li key={p.path}>
                <code>{p.label || p.path}</code>
                {p.readiness && (
                  <span className="run-detail-verify-meta">{p.readiness}</span>
                )}
                <a
                  className="run-detail-screenshot-link"
                  href={pageUrl(p.path)}
                  target="_blank"
                  rel="noreferrer"
                >
                  open
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
      {v.output_preview && (
        <pre className="run-detail-verify-output">{v.output_preview}</pre>
      )}
    </div>
  )
}

function VisualReviewBlock({
  v,
}: {
  v: VisualReviewResult | null | undefined
}): JSX.Element {
  if (!v || !v.enabled) {
    return <span className="run-detail-none">Not run</span>
  }
  if (v.status === 'skipped') {
    return (
      <div className="run-detail-verification">
        <div>
          <span className="run-verify-status status-skipped">skipped</span>
          {v.skipped_reason && (
            <span className="run-detail-verify-meta">{v.skipped_reason}</span>
          )}
        </div>
      </div>
    )
  }
  return (
    <div className="run-detail-verification">
      <div>
        <span className={`run-verify-status status-${v.status}`}>{v.status}</span>
        {v.provider && (
          <span className="run-detail-verify-meta">
            {v.provider}
            {v.model ? ` / ${v.model}` : ''}
          </span>
        )}
        {typeof v.duration_ms === 'number' && (
          <span className="run-detail-verify-meta">{v.duration_ms} ms</span>
        )}
      </div>
      {v.headline && <p className="run-detail-visual-headline">{v.headline}</p>}
      {v.reasoning && <p className="run-detail-visual-reason">{v.reasoning}</p>}
      {v.evidence && v.evidence.length > 0 && (
        <ul className="run-detail-visual-evidence">
          {v.evidence.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function RecoveryBlock({
  ra,
  recoveredBy,
}: {
  ra: RecoveryAssessment | null | undefined
  recoveredBy?: string | null
}): JSX.Element {
  if (!ra || !ra.assessed) {
    return <span className="run-detail-none">Not assessed (run is green or pre-6.1)</span>
  }
  return (
    <div className="run-detail-verification">
      <div>
        <span className={`run-verify-status status-${ra.verdict === 'ok' ? 'passed' : ra.verdict === 'exhausted' ? 'failed' : 'partial'}`}>
          {ra.verdict}
        </span>
        <span className="run-detail-verify-meta">{ra.recommended_action}</span>
        {recoveredBy && <span className="run-detail-verify-meta">recovery run dispatched</span>}
      </div>
      {ra.diagnosis && <p className="run-detail-visual-headline">{ra.diagnosis}</p>}
      {ra.rationale && <p className="run-detail-visual-reason">{ra.rationale}</p>}
      {ra.follow_up_task_card && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">Proposed fix</span>
          <pre className="run-detail-resultmd">{ra.follow_up_task_card}</pre>
        </div>
      )}
      {recoveredBy && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">Recovery run</span>
          <code>{recoveredBy}</code>
        </div>
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
        {plan.execution_mode === 'team' && (
          <span className="run-detail-verify-meta">team execution</span>
        )}
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
            {plan.execution_mode === 'team' && t.role && (
              <span className={`run-detail-task-role role-${t.role}`}>{t.role}</span>
            )}
            <span className="run-detail-task-title">{t.title}</span>
            {plan.execution_mode === 'team' && t.wave != null && (
              <span className="run-detail-verify-meta">wave {t.wave}</span>
            )}
            {plan.execution_mode === 'team' && t.workspace === 'patch' && (
              <span className="run-detail-verify-meta">patch workspace</span>
            )}
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

/** Phase 9 — the team run's integration outcome (roster + merge decisions). */
function IntegrationBlock({ record }: { record: RunRecord }): JSX.Element | null {
  const integ = record.integration
  if (!integ || !integ.enabled) return null
  const conflicts = integ.conflicts ?? []
  const applied = integ.files_applied ?? []
  return (
    <div className="run-detail-plan">
      <div>
        <span className="run-detail-verify-meta">
          {integ.waves ?? 0} wave{(integ.waves ?? 0) === 1 ? '' : 's'} integrated
        </span>
        <span className="run-detail-verify-meta">
          {applied.length} file{applied.length === 1 ? '' : 's'} applied
        </span>
        <span
          className={`run-verify-status status-${conflicts.length > 0 ? 'failed' : 'passed'}`}
        >
          {conflicts.length > 0
            ? `${conflicts.length} conflict${conflicts.length === 1 ? '' : 's'}`
            : 'no conflicts'}
        </span>
      </div>
      {applied.length > 0 && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">Files applied</span>
          {bullets(applied)}
        </div>
      )}
      {conflicts.length > 0 && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">Conflicts</span>
          <ul className="run-detail-list">
            {conflicts.map((c, i) => (
              <li key={i}>
                <code>{c.path}</code> — applied {c.applied_task}, rejected {c.rejected_task}
                {c.wave != null ? ` (wave ${c.wave})` : ''}
              </li>
            ))}
          </ul>
        </div>
      )}
      {integ.notes && <div className="run-detail-task-blockers">{integ.notes}</div>}
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
  // Bounded post-terminal settle polling (Phase 6.1) — see the poll effect.
  const [settleTicks, setSettleTicks] = useState(0)

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

  // Poll while the run is active so the timeline + status update live, plus a
  // bounded "settle" window after it goes terminal — memory reconciliation +
  // the recovery assessment are written ~2-4s AFTER the terminal status, so the
  // Recovery / Memory sections would otherwise need a manual reopen.
  const active = isActive(record)
  const terminalSettlePending =
    !!record &&
    !active &&
    record.status !== 'cancelled' &&
    record.memory_reconciliation == null &&
    settleTicks < 15
  const shouldPoll = active || terminalSettlePending
  useEffect(() => {
    if (!shouldPoll) return
    const id = window.setInterval(() => {
      refresh()
      setSettleTicks((t) => (active ? 0 : t + 1))
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [shouldPoll, refresh, active])

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

              {record.integration?.enabled && (
                <section className="run-detail-result">
                  <h4>Team Integration</h4>
                  <IntegrationBlock record={record} />
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
                <h4>Visual Review</h4>
                <VisualReviewBlock v={record.visual_review} />
                <p className="run-detail-hint">
                  AI judgment of the captured screenshots — diagnostic only; it
                  does not change the run status.
                </p>
              </section>

              {/* Phase 7 — Project Ops (Git/GitHub): diff, commit, push, PR, rollback. */}
              {record.status !== 'running' && record.status !== 'cancelled' && (
                <section className="run-detail-result">
                  <h4>Project Ops (Git / GitHub)</h4>
                  <GitOpsPanel
                    projectId={projectId}
                    runId={runId}
                    record={record}
                    onRecordChange={(rec) => {
                      setRecord(rec)
                      onRunsChanged?.()
                    }}
                  />
                  <p className="run-detail-hint">
                    Commit, push, and open a PR — each external/destructive action is
                    shown as a contract you confirm before it runs. Roll back restores
                    the pre-run checkpoint.
                  </p>
                </section>
              )}

              {/* Phase 6.1 — Main-Agent recovery assessment (non-green runs). */}
              {record.recovery_assessment && (
                <section className="run-detail-result">
                  <h4>Recovery</h4>
                  <RecoveryBlock
                    ra={record.recovery_assessment}
                    recoveredBy={record.recovered_by}
                  />
                  <p className="run-detail-hint">
                    The Main Agent's read of a non-green run — proposes a bounded
                    next step; never auto-runs unless a recovery budget was approved.
                  </p>
                </section>
              )}

              {/* Phase 6.1 — memory reconciliation outcome (audit). */}
              {record.memory_reconciliation && (
                <section className="run-detail-result">
                  <h4>Memory reconciliation</h4>
                  <div className="run-detail-verification">
                    <div>
                      <span
                        className={`run-verify-status status-${
                          record.memory_reconciled
                            ? 'passed'
                            : record.memory_reconciliation === 'error'
                              ? 'failed'
                              : 'skipped'
                        }`}
                      >
                        {record.memory_reconciled ? 'applied' : 'no update'}
                      </span>
                      <span className="run-detail-verify-meta">
                        {record.memory_reconciliation}
                      </span>
                    </div>
                    {record.memory_reconciliation_reason && (
                      <p className="run-detail-visual-reason">
                        {record.memory_reconciliation_reason}
                      </p>
                    )}
                  </div>
                </section>
              )}

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
