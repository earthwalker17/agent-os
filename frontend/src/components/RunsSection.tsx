import { useCallback, useEffect, useState } from 'react'
import type { RunRecord } from '../types'
import RunDetailModal from './RunDetailModal'

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

function RunsSection({ projectId, refreshSignal }: Props) {
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [openRunId, setOpenRunId] = useState<string | null>(null)

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

  // Initial load + reload on project switch + manual refresh signal.
  useEffect(() => {
    loadRuns()
  }, [loadRuns, refreshSignal])

  // Auto-poll while any run is in `running` state. The effect re-binds only
  // when the *presence* of running runs flips, not on every fetch tick, so
  // the interval keeps a steady cadence.
  const runningCount = runs.filter((r) => r.status === 'running').length
  const hasRunning = runningCount > 0

  useEffect(() => {
    if (!hasRunning) return
    const id = window.setInterval(() => {
      loadRuns()
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [hasRunning, loadRuns])

  return (
    <details className="runs-section" open>
      <summary>
        <span className="runs-summary-left">
          <span>Runs</span>
          {hasRunning && (
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
          }}
        >
          {loading ? '…' : '↻'}
        </button>
      </summary>

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
            return (
              <li key={run.run_id}>
                <button
                  type="button"
                  className="run-row"
                  onClick={() => setOpenRunId(run.run_id)}
                  title={run.run_id}
                >
                  <div className="run-row-top">
                    <span className="run-title">{run.task_title || '(untitled run)'}</span>
                    <span className={`run-status status-${run.status}`}>{run.status}</span>
                  </div>
                  <div className="run-row-meta">
                    {time && <span>{time}</span>}
                    <span>files: {filesCount}</span>
                    <span>cmds: {cmdsCount}</span>
                  </div>
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
        />
      )}
    </details>
  )
}

export default RunsSection
