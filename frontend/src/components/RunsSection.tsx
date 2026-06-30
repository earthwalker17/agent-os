import { useCallback, useEffect, useState } from 'react'
import type { GitHubConnectorStatus, GitStatus, PreviewStatus, RunRecord } from '../types'
import RunDetailModal from './RunDetailModal'
import RunTrace from './RunTrace'
import ConnectorModal from './ConnectorModal'
import ConnectorsModal from './ConnectorsModal'

interface Props {
  projectId: string
  /**
   * Increment this from a parent to force a one-shot reload (e.g. after the
   * user sends an `@code` chat message). Re-renders that change projectId or
   * the manual refresh button already trigger their own reloads.
   */
  refreshSignal?: number
}

const POLL_INTERVAL_MS = 2000

function formatTime(iso: string | undefined | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  // Show YYYY-MM-DD HH:MM (local time)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// A run is "active" (panel should auto-refresh + look busy) while the build
// loop runs, while the automatic command verification / repair phase runs, OR
// while a user-triggered browser verification is in flight.
function isActive(run: RunRecord): boolean {
  return (
    run.status === 'running' ||
    run.verification_state === 'verifying' ||
    run.verification_state === 'repairing' ||
    run.browser_verification_state === 'running' ||
    run.git_state != null ||
    run.deploy_state != null ||
    run.external_state != null
  )
}

// Compact phase label for the run row: surfaces the in-flight sub-phase
// (cancelling / planning / executing / verifying / repairing) over the
// underlying settled status. Null once the run reaches a terminal status, so
// the row then shows that status (e.g. 'cancelled') directly.
function phaseLabel(run: RunRecord): string | null {
  // A completed/partial run that's actively being browser-verified is still
  // busy (isActive polls it); surface 'verifying' so the row badge doesn't read
  // a stale terminal status while the "N running" indicator says it's active.
  if (run.browser_verification_state === 'running') return 'verifying'
  if (run.status !== 'running') return null
  if (run.cancel_requested) return 'cancelling'
  if (run.verification_state === 'repairing') return 'repairing'
  if (run.verification_state === 'verifying') return 'verifying'
  // (browser_verification_state === 'running' is handled above the status guard,
  // since it occurs on a terminal run where run.status !== 'running'.)
  const tasks = run.plan?.tasks ?? []
  if (tasks.some((t) => t.status === 'running')) return 'executing'
  if (!run.plan) return 'planning'
  return 'executing'
}

function RunsSection({ projectId, refreshSignal }: Props) {
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [openRunId, setOpenRunId] = useState<string | null>(null)
  const [openTraceId, setOpenTraceId] = useState<string | null>(null)
  const [preview, setPreview] = useState<PreviewStatus | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  // Phase 7 — project-level Git + GitHub connector state.
  const [gitStatus, setGitStatus] = useState<GitStatus | null>(null)
  const [connector, setConnector] = useState<GitHubConnectorStatus | null>(null)
  const [showConnector, setShowConnector] = useState(false)
  const [showConnectors, setShowConnectors] = useState(false)

  const loadRuns = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/execution/runs`)
      if (!res.ok) {
        // 404 means workspace not initialized yet — treat as empty list, not error
        if (res.status === 404) {
          setRuns([])
          return
        }
        throw new Error(`HTTP ${res.status}`)
      }
      const data: RunRecord[] = await res.json()
      // Backend already returns newest-first, but sort defensively.
      data.sort((a, b) => (b.run_id || '').localeCompare(a.run_id || ''))
      setRuns(data)
    } catch (err) {
      console.error('Failed to load runs:', err)
      setError('Failed to load runs')
    } finally {
      setLoading(false)
    }
  }, [projectId])

  const loadPreview = useCallback(async () => {
    try {
      const res = await fetch(`/api/projects/${projectId}/preview/status`)
      if (!res.ok) {
        setPreview(null)
        return
      }
      setPreview(await res.json())
    } catch (err) {
      console.error('Failed to load preview status:', err)
      setPreview(null)
    }
  }, [projectId])

  const loadGit = useCallback(async () => {
    try {
      const res = await fetch(`/api/projects/${projectId}/git/status`)
      setGitStatus(res.ok ? await res.json() : null)
    } catch {
      setGitStatus(null)
    }
  }, [projectId])

  const loadConnector = useCallback(async () => {
    try {
      const res = await fetch(`/api/projects/${projectId}/github/connector`)
      setConnector(res.ok ? await res.json() : null)
    } catch {
      setConnector(null)
    }
  }, [projectId])

  // Initial load + reload on project switch + manual refresh signal.
  useEffect(() => {
    loadRuns()
    loadPreview()
    loadGit()
    loadConnector()
  }, [loadRuns, loadPreview, loadGit, loadConnector, refreshSignal])

  // Auto-poll while any run is active. The effect re-binds only when the
  // *presence* of active runs flips, not on every fetch tick, so the interval
  // keeps a steady cadence. We refresh preview status alongside so a kept-alive
  // preview (after a passing verification) surfaces promptly.
  const activeCount = runs.filter(isActive).length
  const hasActive = activeCount > 0

  useEffect(() => {
    if (!hasActive) return
    const id = window.setInterval(() => {
      loadRuns()
      loadPreview()
      loadGit()
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [hasActive, loadRuns, loadPreview, loadGit])

  const startPreview = useCallback(async () => {
    setPreviewBusy(true)
    setPreviewError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/preview/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        throw new Error(data?.detail || `HTTP ${res.status}`)
      }
      setPreview(data)
      // Open the preview in the user's browser tab once it's reachable.
      if (data?.url) window.open(data.url, '_blank', 'noopener')
    } catch (err) {
      console.error('Start preview failed:', err)
      setPreviewError(err instanceof Error ? err.message : 'Failed to start preview')
    } finally {
      setPreviewBusy(false)
    }
  }, [projectId])

  const stopPreview = useCallback(async () => {
    setPreviewBusy(true)
    setPreviewError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/preview/stop`, { method: 'POST' })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data?.detail || `HTTP ${res.status}`)
      }
      // Re-read the full status (the stop response omits has_package_json, so
      // trusting it directly would hide the control). loadPreview keeps the
      // control in place, now showing a (disabled-until-deps) Start button.
      await loadPreview()
    } catch (err) {
      console.error('Stop preview failed:', err)
      setPreviewError(err instanceof Error ? err.message : 'Failed to stop preview')
    } finally {
      setPreviewBusy(false)
    }
  }, [projectId, loadPreview])

  // The preview control is shown for any frontend app (has package.json) and
  // stays put — the button just toggles between Start and Stop. Start is only
  // clickable once dependencies are installed, and no run is currently active.
  // "Installed" is true as soon as node_modules exists on disk (deps_installed)
  // — which the 06.2E command-verification `npm install` produces — or, as a
  // fallback for older records, once a browser verification's install step
  // passed.
  const installedAtLeastOnce =
    !!preview?.deps_installed ||
    runs.some((r) => r.browser_verification?.install_status === 'passed')
  const previewRunning = !!preview?.running
  const showPreviewControl = !!preview?.has_package_json
  const startDisabled = previewBusy || hasActive || !installedAtLeastOnce
  const startTitle = !installedAtLeastOnce
    ? 'Install dependencies first — run browser verification on a completed run'
    : hasActive
    ? 'Wait for the current run to finish'
    : 'Start the dev server and open the preview'

  const runningCount = activeCount

  return (
    <details className="runs-section" open>
      <summary>
        <span className="runs-summary-left">
          <span>Runs</span>
          {hasActive && (
            <span
              className="runs-running-indicator"
              title={`${runningCount} run${runningCount === 1 ? '' : 's'} in progress — auto-refreshing every ${POLL_INTERVAL_MS / 1000}s`}
            >
              <span className="runs-running-dot" />
              {runningCount} running
            </span>
          )}
        </span>
        <button
          type="button"
          className="runs-refresh-btn"
          title="Refresh runs"
          onClick={(e) => {
            e.preventDefault()
            e.stopPropagation()
            loadRuns()
            loadPreview()
          }}
        >
          {loading ? '…' : '↻'}
        </button>
      </summary>

      {showPreviewControl && (
        <div className="preview-control">
          {previewRunning ? (
            <>
              <div className="preview-control-row">
                <span className="preview-running-badge">
                  <span className="runs-running-dot" /> Preview running
                </span>
                <button
                  type="button"
                  className="preview-stop-btn"
                  onClick={stopPreview}
                  disabled={previewBusy}
                >
                  {previewBusy ? '…' : 'Stop preview'}
                </button>
              </div>
              {preview?.url && (
                <a
                  className="preview-url-link"
                  href={preview.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {preview.url}
                </a>
              )}
            </>
          ) : (
            <div className="preview-control-row">
              <button
                type="button"
                className="preview-start-btn"
                onClick={startPreview}
                disabled={startDisabled}
                title={startTitle}
              >
                {previewBusy ? 'Starting…' : 'Start preview'}
              </button>
              {!installedAtLeastOnce && (
                <span className="preview-hint">Install deps first</span>
              )}
            </div>
          )}
          {previewError && <div className="runs-error">{previewError}</div>}
        </div>
      )}

      {/* Phase 7 — project-level Git + GitHub connector strip. */}
      {(gitStatus?.is_repo || connector) && (
        <div className="git-control">
          <div className="git-control-row">
            {gitStatus?.is_repo ? (
              <span className="git-branch-badge" title="Current branch + working-tree state">
                ⎇ {gitStatus.branch || 'detached'}
                {gitStatus.dirty ? (
                  <span className="git-dirty"> · {(gitStatus.untracked ?? 0) + (gitStatus.modified ?? 0) + (gitStatus.staged ?? 0)} uncommitted</span>
                ) : (
                  <span className="git-clean"> · clean</span>
                )}
              </span>
            ) : (
              <span className="run-chat-muted">No git repo yet (created on first run)</span>
            )}
            <button
              type="button"
              className={`git-connect-btn${connector?.connected ? ' connected' : ''}`}
              onClick={() => setShowConnector(true)}
              title="Connect a GitHub token for push + pull requests"
            >
              {connector?.connected
                ? `GitHub: ${connector.login || 'connected'}`
                : connector?.configured
                  ? 'GitHub: configured'
                  : 'Connect GitHub'}
            </button>
            <button
              type="button"
              className="git-connect-btn"
              onClick={() => setShowConnectors(true)}
              title="Connect Vercel / Supabase / Stripe (Production Path)"
            >
              Connectors
            </button>
          </div>
        </div>
      )}

      {error && <div className="runs-error">{error}</div>}

      {!error && runs.length === 0 && !loading && (
        <div className="runs-empty">
          No coding agent runs yet. Use <code>@code</code> in chat to create one.
        </div>
      )}

      {runs.length > 0 && (
        <ul className="runs-list">
          {runs.map((run) => {
            const filesCount = run.files_changed?.length ?? 0
            const cmdsCount = run.commands_run?.length ?? 0
            const time = formatTime(run.completed_at) || formatTime(run.created_at)
            const phase = phaseLabel(run)
            return (
              <li key={run.run_id} className="run-row-item">
                <button
                  type="button"
                  className="run-row"
                  onClick={() => setOpenRunId(run.run_id)}
                  title={run.run_id}
                >
                  <div className="run-row-top">
                    <span className="run-title">{run.task_title || '(untitled run)'}</span>
                    <span className={`run-status status-${phase ? 'running' : run.status}`}>
                      {phase ?? run.status}
                    </span>
                  </div>
                  <div className="run-row-meta">
                    {time && <span>{time}</span>}
                    <span>files: {filesCount}</span>
                    <span>cmds: {cmdsCount}</span>
                  </div>
                </button>
                <button
                  type="button"
                  className="run-row-trace-btn"
                  onClick={() => setOpenTraceId(run.run_id)}
                  title="Open the live execution trace"
                >
                  Trace
                </button>
              </li>
            )
          })}
        </ul>
      )}

      {openRunId && (
        <RunDetailModal
          projectId={projectId}
          runId={openRunId}
          onClose={() => setOpenRunId(null)}
          onOpenTrace={(rid) => {
            setOpenRunId(null)
            setOpenTraceId(rid)
          }}
          onRunsChanged={() => {
            loadRuns()
            loadPreview()
            loadGit()
          }}
        />
      )}

      {openTraceId && (
        <RunTrace
          projectId={projectId}
          runId={openTraceId}
          onClose={() => setOpenTraceId(null)}
        />
      )}

      {showConnector && (
        <ConnectorModal
          projectId={projectId}
          status={connector}
          onClose={() => setShowConnector(false)}
          onSaved={(s) => {
            setConnector(s)
            loadGit()
          }}
        />
      )}

      {showConnectors && (
        <ConnectorsModal projectId={projectId} onClose={() => setShowConnectors(false)} />
      )}
    </details>
  )
}

export default RunsSection
