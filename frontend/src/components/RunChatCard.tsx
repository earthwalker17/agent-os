import { useCallback, useEffect, useRef, useState } from 'react'
import type { RunRecord } from '../types'
import GitOpsPanel from './GitOpsPanel'
import DeployOpsPanel from './DeployOpsPanel'

interface Props {
  projectId: string
  runId: string
  /** Conversation to attach a proposed recovery plan to (Phase 6). */
  conversationId?: string | null
  /** Open the detailed RunDetailModal for this run. */
  onOpenRun: (runId: string) => void
  /** Open the lightweight Live Trace modal for this run. */
  onOpenTrace?: (runId: string) => void
  /** Notify the parent that run state changed so the Runs panel can refresh. */
  onRunsChanged?: () => void
  /** Re-fetch chat messages after a recovery plan is proposed (Phase 6). */
  onMessagesChanged?: () => void
  /**
   * Provider Registry 2.0 — the user's selected chat provider/model, forwarded
   * to browser verification so the diagnostic AI visual judgment prefers a
   * vision-capable selection (and skips gracefully when none is available).
   */
  provider?: string
  model?: string
}

const POLL_INTERVAL_MS = 2000

const TERMINAL = new Set(['completed', 'partial', 'blocked', 'failed', 'cancelled'])

// Statuses a terminal run can be retried from (everything except a clean pass).
const RETRYABLE = new Set(['partial', 'blocked', 'failed', 'cancelled'])

/**
 * Derive the current execution phase from run.json alone (no extra fetch) so
 * the card can show a live phase badge: planning → executing → verifying /
 * repairing → browser verification, plus a transient "cancelling".
 */
function derivePhase(r: RunRecord): string | null {
  if (r.status !== 'running') return null
  if (r.cancel_requested) return 'cancelling'
  if (r.verification_state === 'repairing') return 'repairing'
  if (r.verification_state === 'verifying') return 'verifying'
  if (r.browser_verification_state === 'running') return 'browser verification'
  const tasks = r.plan?.tasks ?? []
  if (tasks.some((t) => t.status === 'running')) return 'executing'
  if (!r.plan) return 'planning'
  return 'executing'
}

function fileSummary(files: string[] | undefined): string {
  const list = files ?? []
  if (list.length === 0) return ''
  const names = list.map((f) => f.replace(/^repo\//, ''))
  const shown = names.slice(0, 4)
  const tail = names.length > shown.length ? `, and ${names.length - shown.length} more` : ''
  return `${shown.map((n) => `\`${n}\``).join(', ')}${tail}`
}

/**
 * Task 06.2D — the chat-first run follow-up card.
 *
 * Attached to any assistant message carrying a `run_id` in its metadata. It
 * owns the in-chat run lifecycle: a live "running" note while the first build
 * pass executes, a natural completion summary, and the primary browser
 * verification control + result (preview URL + screenshot) — all without the
 * user needing to open the RunDetailModal. The modal remains available via the
 * "Details" link for exact logs and artifacts.
 */
function RunChatCard({ projectId, runId, conversationId, onOpenRun, onOpenTrace, onRunsChanged, onMessagesChanged, provider, model }: Props) {
  const [record, setRecord] = useState<RunRecord | null>(null)
  const [verifying, setVerifying] = useState(false)
  const [verifyError, setVerifyError] = useState<string | null>(null)
  // Index of the captured page shown in the fullscreen lightbox (null = closed).
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)
  // Run control (cancel / retry).
  const [controlBusy, setControlBusy] = useState(false)
  const [controlError, setControlError] = useState<string | null>(null)
  const [retriedRunId, setRetriedRunId] = useState<string | null>(null)
  // Phase 6 — bounded post-terminal "settle" polling: memory reconciliation +
  // the recovery assessment are written ~2-4s AFTER the run goes terminal, so we
  // keep polling briefly until they land instead of freezing on a stale view.
  const [settleTicks, setSettleTicks] = useState(0)
  // Phase 6 — confirmable recovery handoff.
  const [recoveryBusy, setRecoveryBusy] = useState(false)
  const [recoveryProposed, setRecoveryProposed] = useState(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/projects/${projectId}/execution/runs/${runId}`)
      if (!res.ok) return
      const rec: RunRecord = await res.json()
      if (mounted.current) setRecord(rec)
    } catch (err) {
      console.error('RunChatCard load failed:', err)
    }
  }, [projectId, runId])

  useEffect(() => {
    load()
  }, [load])

  // Poll while the build run is in flight, while the automatic command
  // verification / repair phase runs (verification_state), or while a browser
  // verification is running (the backend writes 'running' sub-statuses during
  // those windows).
  const isRunning = record?.status === 'running'
  const commandVerifyingState =
    record?.verification_state === 'verifying' || record?.verification_state === 'repairing'
  const isVerifyingState = record?.browser_verification_state === 'running'
  // A run that just went terminal whose post-run reconciliation hasn't landed
  // yet (cancelled runs skip reconciliation, so never settle-poll them). Capped
  // so an old pre-reconciliation run can't poll forever.
  const isTerminalNow = record ? TERMINAL.has(record.status) : false
  const needsSettle =
    isTerminalNow &&
    record?.status !== 'cancelled' &&
    record?.memory_reconciliation == null &&
    settleTicks < 15
  // Phase 7 — a Git action (commit/push/PR/rollback) sets a transient git_state;
  // keep polling so the card reflects the settled delivery state.
  const gitInFlight = record?.git_state != null
  // Phase 8 — a deploy/redeploy finalizes off-thread; keep polling so the card
  // reflects the settled deployment URL when it lands.
  const deployInFlight = record?.deploy_state != null || record?.external_state != null
  const shouldPoll =
    isRunning ||
    commandVerifyingState ||
    isVerifyingState ||
    verifying ||
    needsSettle ||
    gitInFlight ||
    deployInFlight
  useEffect(() => {
    if (!shouldPoll) return
    const id = window.setInterval(() => {
      load()
      setSettleTicks((t) => (isRunning ? 0 : t + 1))
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [shouldPoll, load, isRunning])

  const runBrowserVerification = useCallback(async () => {
    setVerifying(true)
    setVerifyError(null)
    onRunsChanged?.()
    try {
      const res = await fetch(
        `/api/projects/${projectId}/execution/runs/${runId}/browser-verify`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: provider ?? null, model: model ?? null }),
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
      const rec: RunRecord = await res.json()
      if (mounted.current) setRecord(rec)
    } catch (err) {
      console.error('Browser verification failed:', err)
      if (mounted.current) {
        setVerifyError(err instanceof Error ? err.message : 'Browser verification failed')
      }
    } finally {
      if (mounted.current) setVerifying(false)
      onRunsChanged?.()
    }
  }, [projectId, runId, onRunsChanged, provider, model])

  const cancelRun = useCallback(async () => {
    setControlBusy(true)
    setControlError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/execution/runs/${runId}/cancel`, {
        method: 'POST',
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
      const rec: RunRecord = await res.json()
      if (mounted.current) setRecord(rec)
    } catch (err) {
      console.error('Cancel run failed:', err)
      if (mounted.current) {
        setControlError(err instanceof Error ? err.message : 'Cancel failed')
      }
    } finally {
      if (mounted.current) setControlBusy(false)
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
        let detail = `HTTP ${res.status}`
        try {
          const b = await res.json()
          if (b?.detail) detail = b.detail
        } catch {
          /* keep status */
        }
        throw new Error(detail)
      }
      const rec: RunRecord = await res.json()
      if (mounted.current) setRetriedRunId(rec.run_id)
    } catch (err) {
      console.error('Retry run failed:', err)
      if (mounted.current) {
        setControlError(err instanceof Error ? err.message : 'Retry failed')
      }
    } finally {
      if (mounted.current) setControlBusy(false)
      onRunsChanged?.()
    }
  }, [projectId, runId, onRunsChanged])

  // Phase 6 — turn the Main Agent's recovery assessment into a confirmable
  // pending plan in the conversation (the user still clicks "OK, run this" to
  // dispatch — no auto-run).
  const proposeRecovery = useCallback(async () => {
    if (!conversationId) return
    setRecoveryBusy(true)
    setControlError(null)
    try {
      const res = await fetch(
        `/api/projects/${projectId}/execution/runs/${runId}/propose-recovery`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversation_id: conversationId }),
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
      if (mounted.current) setRecoveryProposed(true)
      onMessagesChanged?.()
    } catch (err) {
      console.error('Propose recovery failed:', err)
      if (mounted.current) {
        setControlError(err instanceof Error ? err.message : 'Could not propose a fix')
      }
    } finally {
      if (mounted.current) setRecoveryBusy(false)
    }
  }, [projectId, runId, conversationId, onMessagesChanged])

  if (!record) return null

  const status = record.status
  const phase = derivePhase(record)
  const tasks = record.plan?.tasks ?? []
  const isMultiTask = tasks.length > 1
  const bv = record.browser_verification
  const cv = record.verification
  const verifyingNow = verifying || record.browser_verification_state === 'running'
  // Task 06.2E — the automatic command-verification phase still in flight.
  const commandVerifying =
    record.verification_state === 'verifying' || record.verification_state === 'repairing'
  const commandVerifyFailed = !!cv && cv.enabled && cv.status === 'failed'
  const commandVerifyPassed = !!cv && cv.enabled && cv.status === 'passed'
  const isTerminal = TERMINAL.has(status)
  // Browser verification is only offered once command verification is clean
  // (passed or safely skipped) — never while it's still running or has failed.
  const canVerify =
    (status === 'completed' || status === 'partial') &&
    !commandVerifying &&
    !commandVerifyFailed
  const hadVerifyAttempt = !!bv && bv.enabled && (bv.status === 'passed' || bv.status === 'failed')
  const pageUrl = (path: string) => {
    const name = path.split('/').pop() || 'browser.png'
    return `/api/projects/${projectId}/execution/runs/${runId}/screenshot?name=${encodeURIComponent(name)}`
  }
  // Captured pages (multi-page). Falls back to the single primary screenshot for
  // runs verified before the multi-page upgrade.
  const galleryPages =
    bv?.pages && bv.pages.length > 0
      ? bv.pages.map((p) => ({ path: p.path, label: p.label || p.path, readiness: p.readiness }))
      : bv?.screenshot_path
        ? [{ path: bv.screenshot_path, label: 'Home', readiness: bv.readiness ?? undefined }]
        : []
  const vr = record.visual_review
  // Phase 6 — memory reconciliation + recovery assessment surfaces.
  const memTag = record.memory_reconciliation
  const ra = record.recovery_assessment
  // A recovery run already exists for this run (auto or confirmed) → don't offer
  // the manual "Run suggested fix" path again (one recovery per parent).
  const alreadyRecovered = !!record.recovered_by
  const recoveryActionable =
    !!ra && ra.assessed && ra.verdict === 'needs_recovery' && !!ra.follow_up_task_card &&
    !alreadyRecovered

  // Thumbnail gallery — distinct from the "captured" vs "judged" signals below.
  const gallery =
    galleryPages.length > 0 ? (
      <div className="run-chat-gallery">
        {galleryPages.map((p, i) => (
          <button
            key={p.path}
            type="button"
            className="run-chat-shot"
            onClick={() => setLightboxIndex(i)}
            title={`Open ${p.label}`}
          >
            <img src={pageUrl(p.path)} alt={p.label} className="run-chat-screenshot-thumb" />
            {galleryPages.length > 1 && <span className="run-chat-shot-label">{p.label}</span>}
          </button>
        ))}
      </div>
    ) : null

  // AI visual judgment — the third distinct signal (server reachable → page
  // captured → visually judged). Diagnostic only; never affects run status.
  const visualReviewBlock =
    vr && vr.enabled ? (
      vr.status === 'skipped' ? (
        <p className="run-chat-muted">
          AI visual judgment skipped{vr.skipped_reason ? `: ${vr.skipped_reason}` : ''}.
        </p>
      ) : (
        <div className={`run-chat-visual visual-${vr.status}`}>
          <div className="run-chat-visual-head">
            <span className={`run-verify-status status-${vr.status}`}>{vr.status}</span>
            <strong>AI visual judgment</strong>
          </div>
          {vr.headline && <p className="run-chat-visual-headline">{vr.headline}</p>}
          {vr.reasoning && <p className="run-chat-visual-reason">{vr.reasoning}</p>}
          {vr.evidence && vr.evidence.length > 0 && (
            <ul className="run-chat-visual-evidence">
              {vr.evidence.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          )}
          {vr.provider && (
            <p className="run-chat-muted run-chat-visual-by">
              Reviewed by {vr.provider}
              {vr.model ? ` / ${vr.model}` : ''}
            </p>
          )}
        </div>
      )
    ) : null

  return (
    <div className="run-chat-card">
      {/* --- live phase badge (run control / timeline) --- */}
      {phase && (
        <div className="run-chat-phase-row">
          <span className="run-chat-dot" />
          <span className={`run-chat-phase phase-${phase.replace(/\s+/g, '-')}`}>{phase}</span>
        </div>
      )}

      {/* --- build-run lifecycle line --- */}
      {status === 'running' && !phase && (
        <div className="run-chat-status-line">
          <span className="run-chat-dot" />
          Coding Agent is working on the first build pass…
        </div>
      )}

      {/* --- task checklist for multi-task runs (live, from run.json) --- */}
      {isMultiTask && (
        <ul className="run-chat-tasklist">
          {tasks.map((t) => (
            <li key={t.id} className="run-chat-task">
              <span className={`run-verify-status status-${t.status}`}>{t.status}</span>
              <span className="run-chat-task-title">{t.title}</span>
            </li>
          ))}
        </ul>
      )}

      {isTerminal && (
        <div className="run-chat-summary">
          {status === 'completed' || status === 'partial' ? (
            <p>
              <strong>The first build pass is complete.</strong>{' '}
              {record.summary?.trim() ||
                'The Coding Agent finished its initial pass.'}
            </p>
          ) : (
            <p>
              <strong>The run did not complete (status: {status}).</strong>{' '}
              {record.summary?.trim() || ''}
            </p>
          )}
          {fileSummary(record.files_changed) && (
            <p className="run-chat-files">
              Files changed:{' '}
              <span dangerouslySetInnerHTML={{ __html: codeify(fileSummary(record.files_changed)) }} />
            </p>
          )}
          {(record.blockers?.length ?? 0) > 0 && (
            <ul className="run-chat-blockers">
              {record.blockers!.map((b, i) => (
                <li key={i}>{b}</li>
              ))}
            </ul>
          )}
          {canVerify && !verifyingNow && !hadVerifyAttempt && (
            <p className="run-chat-muted">Browser verification has not been run yet.</p>
          )}
          {/* Phase 6 — Main Agent memory reconciliation outcome. */}
          {memTag && (
            <p className="run-chat-muted run-chat-memory">
              {record.memory_reconciled
                ? '🧠 Project memory updated to reflect this run.'
                : memTag === 'error'
                  ? '🧠 Memory reconciliation hit an error (run is unaffected).'
                  : '🧠 Memory left unchanged (nothing new to record).'}
            </p>
          )}
        </div>
      )}

      {/* --- command verification: in-progress phase (Task 06.2E) --- */}
      {commandVerifying && (
        <div className="run-chat-verify-progress">
          <span className="run-chat-dot" />
          {record.verification_state === 'repairing'
            ? 'Command verification found an issue — the Coding Agent is making a repair pass.'
            : 'Command verification is running (build / tests).'}
        </div>
      )}

      {/* --- command verification: settled result (Task 06.2E) --- */}
      {!commandVerifying && commandVerifyPassed && (
        <p className="run-chat-muted">
          Command verification passed
          {cv && (cv.repair_attempts ?? 0) > 0 ? ' after a repair pass' : ''}.
        </p>
      )}
      {!commandVerifying && commandVerifyFailed && (
        <p className="run-chat-muted">
          Command verification failed
          {cv && (cv.repair_attempts ?? 0) > 0
            ? ' even after a repair pass'
            : ''}
          {cv?.command ? ': ' : '.'}
          {cv?.command ? <code>{cv.command}</code> : null} See Details for the
          command output.
        </p>
      )}

      {/* --- browser verification: progress --- */}
      {verifyingNow && (
        <div className="run-chat-verify-progress">
          <span className="run-chat-dot" />
          Browser verification is running. Installing dependencies, starting the
          dev server on port 5174, and capturing a screenshot.
        </div>
      )}

      {/* --- browser verification: passed --- */}
      {!verifyingNow && bv?.enabled && bv.status === 'passed' && (
        <div className="run-chat-verify-result passed">
          <p>
            <strong>Browser verification passed.</strong>{' '}
            {bv.install_status === 'passed'
              ? 'Dependencies were installed successfully, '
              : ''}
            the dev server started{' '}
            {bv.url ? `on ${stripScheme(bv.url)}` : ''} and the page rendered
            {galleryPages.length > 1 ? ` (${galleryPages.length} views captured).` : '.'}
          </p>
          {bv.readiness === 'unconfirmed' && (
            <p className="run-chat-muted">
              Note: render readiness could not be confirmed before capture — the
              app may still have been settling.
            </p>
          )}
          {bv.url && (
            <p className="run-chat-preview-url">
              Preview:{' '}
              <a href={bv.url} target="_blank" rel="noreferrer">
                {bv.url}
              </a>
            </p>
          )}
          {gallery}
          {visualReviewBlock}
        </div>
      )}

      {/* --- browser verification: failed --- */}
      {!verifyingNow && bv?.enabled && bv.status === 'failed' && (
        <div className="run-chat-verify-result failed">
          <p>
            <strong>Browser verification failed.</strong>
          </p>
          {bv.install_status && (
            <p className="run-chat-muted">Dependency install: {bv.install_status}</p>
          )}
          {bv.command && (
            <p className="run-chat-muted">
              Command: <code>{bv.command}</code>
            </p>
          )}
          {bv.output_preview && (
            <pre className="run-chat-verify-output">{bv.output_preview}</pre>
          )}
          {gallery}
        </div>
      )}

      {/* --- Phase 6: Main-Agent recovery assessment (next steps) --- */}
      {ra && ra.assessed && (ra.diagnosis || ra.verdict !== 'ok') && (
        <div className={`run-chat-recovery recovery-${ra.verdict}`}>
          <div className="run-chat-recovery-head">
            <span className="run-chat-recovery-icon">🛠️</span>
            <strong>Next steps</strong>
            <span className="run-chat-recovery-action">{ra.recommended_action}</span>
          </div>
          {ra.diagnosis && <p className="run-chat-recovery-diagnosis">{ra.diagnosis}</p>}
          {ra.rationale && <p className="run-chat-muted">{ra.rationale}</p>}
          {recoveryActionable && !recoveryProposed && (
            <button
              type="button"
              className="run-chat-recovery-btn"
              onClick={proposeRecovery}
              disabled={recoveryBusy || !conversationId}
              title="Draft a confirmable recovery plan you can run with one click"
            >
              {recoveryBusy ? 'Preparing…' : 'Run suggested fix'}
            </button>
          )}
          {recoveryProposed && (
            <p className="run-chat-muted">
              A recovery plan was added below — confirm it to run the fix.
            </p>
          )}
          {alreadyRecovered && (
            <p className="run-chat-muted">
              {record.recovery_of ? 'Recovery' : 'A recovery run was dispatched'}
              {' '}
              <button
                type="button"
                className="run-chat-link-btn"
                onClick={() => onOpenRun(record.recovered_by as string)}
                title={`Open recovery run ${record.recovered_by}`}
              >
                view recovery run
              </button>
            </p>
          )}
          {!recoveryActionable && !alreadyRecovered && ra.verdict === 'exhausted' && (
            <p className="run-chat-muted">
              Automatic recovery looks exhausted — this one likely needs a manual look.
            </p>
          )}
        </div>
      )}

      {/* --- auto-recovery lineage badge --- */}
      {record.recovery_of && (
        <p className="run-chat-muted run-chat-recovery-lineage">
          ↻ Auto-recovery of run{' '}
          <button
            type="button"
            className="run-chat-link-btn"
            onClick={() => onOpenRun(record.recovery_of as string)}
            title={`Open original run ${record.recovery_of}`}
          >
            {record.recovery_of}
          </button>
          {typeof record.recovery_budget === 'number' && record.recovery_budget > 0 && (
            <> · {record.recovery_budget} recovery pass{record.recovery_budget === 1 ? '' : 'es'} left</>
          )}
        </p>
      )}

      {/* --- Phase 7: Project Ops (commit / push / PR / rollback) --- */}
      {isTerminal && status !== 'cancelled' && (
        <GitOpsPanel
          projectId={projectId}
          runId={runId}
          record={record}
          compact
          onRecordChange={(rec) => {
            if (mounted.current) setRecord(rec)
            onRunsChanged?.()
          }}
        />
      )}

      {/* --- Phase 8: Production Path (deploy / redeploy / rollback) --- */}
      {isTerminal && status !== 'cancelled' && (
        <DeployOpsPanel
          projectId={projectId}
          runId={runId}
          record={record}
          compact
          onRecordChange={(rec) => {
            if (mounted.current) setRecord(rec)
            onRunsChanged?.()
          }}
        />
      )}

      {verifyError && <div className="run-chat-error">{verifyError}</div>}
      {controlError && <div className="run-chat-error">{controlError}</div>}

      {/* --- retry follow-up affordance --- */}
      {retriedRunId && (
        <div className="run-chat-muted">
          Retried as a new run.{' '}
          <button
            type="button"
            className="run-chat-link-btn"
            onClick={() => {
              onRunsChanged?.()
              onOpenRun(retriedRunId)
            }}
          >
            View new run
          </button>
        </div>
      )}

      {/* --- action row --- */}
      <div className="run-chat-actions">
        {status === 'running' && (
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
        {isTerminal && RETRYABLE.has(status) && !retriedRunId && (
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
        {canVerify && !verifyingNow && (
          <button
            type="button"
            className="run-chat-verify-btn"
            onClick={runBrowserVerification}
            title="Install dependencies, start the dev server on port 5174, and capture a screenshot"
          >
            {hadVerifyAttempt ? 'Re-run browser verification' : 'Run browser verification'}
          </button>
        )}
        {onOpenTrace && (
          <button
            type="button"
            className="run-chat-trace-btn"
            onClick={() => onOpenTrace(runId)}
            title="Open the live execution trace — a chronological thread of what the Coding Agent is doing"
          >
            Live trace
          </button>
        )}
        <button
          type="button"
          className="run-chat-details-btn"
          onClick={() => onOpenRun(runId)}
          title="Open detailed logs and artifacts"
        >
          Details
        </button>
      </div>

      {lightboxIndex !== null && galleryPages[lightboxIndex] && (() => {
        const idx = lightboxIndex
        const page = galleryPages[idx]
        const count = galleryPages.length
        return (
          <div className="run-chat-lightbox" onClick={() => setLightboxIndex(null)}>
            <div className="run-chat-lightbox-inner" onClick={(e) => e.stopPropagation()}>
              <img src={pageUrl(page.path)} alt={`${page.label} (full)`} />
              <div className="run-chat-lightbox-bar">
                {count > 1 && (
                  <button
                    type="button"
                    onClick={() => setLightboxIndex((idx - 1 + count) % count)}
                  >
                    ‹ Prev
                  </button>
                )}
                <span>
                  {page.label} ({idx + 1}/{count})
                </span>
                {count > 1 && (
                  <button type="button" onClick={() => setLightboxIndex((idx + 1) % count)}>
                    Next ›
                  </button>
                )}
                <button type="button" onClick={() => setLightboxIndex(null)}>
                  Close
                </button>
              </div>
            </div>
          </div>
        )
      })()}
    </div>
  )
}

function stripScheme(url: string): string {
  return url.replace(/^https?:\/\//, '')
}

// Wrap `backtick` spans into <code> for the inline file list. Input is built
// internally (file names), so there's no untrusted HTML here.
function codeify(text: string): string {
  return text.replace(/`([^`]+)`/g, '<code>$1</code>')
}

export default RunChatCard
