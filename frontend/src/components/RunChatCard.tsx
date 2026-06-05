import { useCallback, useEffect, useRef, useState } from 'react'
import type { RunRecord } from '../types'

interface Props {
  projectId: string
  runId: string
  /** Open the detailed RunDetailModal for this run. */
  onOpenRun: (runId: string) => void
  /** Notify the parent that run state changed so the Runs panel can refresh. */
  onRunsChanged?: () => void
}

const POLL_INTERVAL_MS = 2000

const TERMINAL = new Set(['completed', 'partial', 'blocked', 'failed'])

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
function RunChatCard({ projectId, runId, onOpenRun, onRunsChanged }: Props) {
  const [record, setRecord] = useState<RunRecord | null>(null)
  const [verifying, setVerifying] = useState(false)
  const [verifyError, setVerifyError] = useState<string | null>(null)
  const [screenshotOpen, setScreenshotOpen] = useState(false)
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
  const shouldPoll = isRunning || commandVerifyingState || isVerifyingState || verifying
  useEffect(() => {
    if (!shouldPoll) return
    const id = window.setInterval(load, POLL_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [shouldPoll, load])

  const runBrowserVerification = useCallback(async () => {
    setVerifying(true)
    setVerifyError(null)
    onRunsChanged?.()
    try {
      const res = await fetch(
        `/api/projects/${projectId}/execution/runs/${runId}/browser-verify`,
        { method: 'POST' },
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
  }, [projectId, runId, onRunsChanged])

  if (!record) return null

  const status = record.status
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
  const screenshotUrl = bv?.screenshot_path
    ? `/api/projects/${projectId}/execution/runs/${runId}/screenshot`
    : null

  return (
    <div className="run-chat-card">
      {/* --- build-run lifecycle line --- */}
      {status === 'running' && (
        <div className="run-chat-status-line">
          <span className="run-chat-dot" />
          Coding Agent is working on the first build pass…
        </div>
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
            {bv.url ? `on ${stripScheme(bv.url)}` : ''} and the page rendered.
          </p>
          {bv.url && (
            <p className="run-chat-preview-url">
              Preview:{' '}
              <a href={bv.url} target="_blank" rel="noreferrer">
                {bv.url}
              </a>
            </p>
          )}
          {screenshotUrl && (
            <button
              type="button"
              className="run-chat-screenshot-btn"
              onClick={() => setScreenshotOpen(true)}
              title="Open the captured screenshot"
            >
              <img
                src={screenshotUrl}
                alt="browser verification screenshot"
                className="run-chat-screenshot-thumb"
              />
            </button>
          )}
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
          {screenshotUrl && (
            <button
              type="button"
              className="run-chat-screenshot-btn"
              onClick={() => setScreenshotOpen(true)}
            >
              <img
                src={screenshotUrl}
                alt="browser verification screenshot"
                className="run-chat-screenshot-thumb"
              />
            </button>
          )}
        </div>
      )}

      {verifyError && <div className="run-chat-error">{verifyError}</div>}

      {/* --- action row --- */}
      <div className="run-chat-actions">
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
        <button
          type="button"
          className="run-chat-details-btn"
          onClick={() => onOpenRun(runId)}
          title="Open detailed logs and artifacts"
        >
          Details
        </button>
      </div>

      {screenshotOpen && screenshotUrl && (
        <div className="run-chat-lightbox" onClick={() => setScreenshotOpen(false)}>
          <img src={screenshotUrl} alt="browser verification screenshot (full)" />
        </div>
      )}
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
