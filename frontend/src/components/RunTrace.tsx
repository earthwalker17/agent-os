import { useCallback, useEffect, useRef, useState } from 'react'
import type { RunEvent, RunRecord } from '../types'
import { clockTime, kindFor, str } from './runEventUtils'

interface Props {
  projectId: string
  runId: string
  onClose: () => void
}

const POLL_INTERVAL_MS = 2000

function isActive(r: RunRecord | null): boolean {
  if (!r) return false
  return (
    r.status === 'running' ||
    r.verification_state === 'verifying' ||
    r.verification_state === 'repairing' ||
    r.browser_verification_state === 'running' ||
    r.integration_state != null
  )
}

interface TraceRow {
  key: string
  /** status-* palette word for the leading badge. */
  kind: string
  /** short uppercase-ish badge text (defaults to kind). */
  badge?: string
  label: string
  /** file path / command / url — rendered as inline code. */
  target?: string
  /** short bounded output / reason. */
  output?: string
  phase?: string
  /** Phase 9 — task attribution for parallel-agent events (e.g. "t2 · coder"). */
  task?: string
  time: string
}

/** A consecutive tool_call + tool_result PAIR collapsed into one row. */
function toolRow(
  call: RunEvent | null,
  result: RunEvent | null,
  key: string,
): TraceRow {
  const src = (call ?? result ?? {}) as RunEvent
  const tool = str(src.tool_name)
  const args = (call?.arguments ?? {}) as Record<string, unknown>
  const path = str(args.path)
  const query = str(args.query)
  const command = str(args.command)

  let label = tool || 'tool'
  let target = ''
  let badge = tool
  switch (tool) {
    case 'read_file':
      label = 'Read'
      target = path
      badge = 'read'
      break
    case 'write_file':
      label = 'Write'
      target = path
      badge = 'write'
      break
    case 'append_file':
      label = 'Append'
      target = path
      badge = 'append'
      break
    case 'search_files':
      label = 'Search'
      target = query ? `"${query}"${path && path !== '.' ? ` in ${path}` : ''}` : path
      badge = 'search'
      break
    case 'list_files':
      label = 'List'
      target = path || '.'
      badge = 'list'
      break
    case 'run_shell':
      label = 'Run'
      target = command ? `$ ${command}` : ''
      badge = 'shell'
      break
  }

  // Status: result decides; no result yet → still running.
  let kind = 'running'
  if (result) kind = result.success === false ? 'failed' : 'passed'

  const output = result
    ? str(result.error) || str(result.preview)
    : str((call as RunEvent | null)?.reason)

  const taskId = str(src.task_id)
  const role = str(src.role)
  return {
    key,
    kind,
    badge,
    label,
    target,
    output,
    phase: str(src.phase) || undefined,
    task: taskId ? (role ? `${taskId} · ${role}` : taskId) : undefined,
    time: clockTime((result ?? call)?.timestamp),
  }
}

/** Map a non-tool event to a trace row (or null to drop it). */
function describeEvent(e: RunEvent, key: string): TraceRow | null {
  const time = clockTime(e.timestamp)
  const phase = str(e.phase) || undefined
  const base = { key, time, phase }
  switch (e.type) {
    case 'run_dispatched':
    case 'run_started':
      return { ...base, kind: 'info', badge: 'start', label: 'Run started', target: str(e.title) }
    case 'plan_started':
      return { ...base, kind: 'running', badge: 'plan', label: 'Planning started' }
    case 'plan_ready': {
      const n = typeof e.task_count === 'number' ? e.task_count : undefined
      const mode = str(e.mode)
      const target = [n != null ? `${n} task${n === 1 ? '' : 's'}` : '', mode && `(${mode})`]
        .filter(Boolean)
        .join(' ')
      return { ...base, kind: 'completed', badge: 'plan', label: 'Plan ready', target, output: str(e.goal) }
    }
    case 'plan_failed':
      return { ...base, kind: 'skipped', badge: 'plan', label: 'Planning fell back to a single task', output: str(e.error) }
    case 'task_started': {
      const role = str(e.role)
      const tags = [
        role && role !== 'coder' ? role : '',
        typeof e.wave === 'number' ? `wave ${e.wave}` : '',
        e.parallel === true ? 'parallel' : '',
        str(e.workspace) === 'patch' ? 'patch workspace' : '',
      ]
        .filter(Boolean)
        .join(' · ')
      return {
        ...base,
        kind: 'running',
        badge: 'task',
        label: `Task ${str(e.task_id)} started`,
        target: str(e.title),
        output: tags || undefined,
      }
    }
    case 'task_status': {
      const status = str(e.status)
      return {
        ...base,
        kind: kindFor(status),
        badge: 'task',
        label: `Task ${str(e.task_id)} ${status}`,
        output: str(e.summary) || str(e.reason),
      }
    }
    // Phase 9 — team execution events.
    case 'team_execution_started': {
      const waves = Array.isArray(e.waves) ? e.waves.length : undefined
      return {
        ...base,
        kind: 'info',
        badge: 'team',
        label: 'Team execution started',
        target: waves != null ? `${waves} wave${waves === 1 ? '' : 's'}` : '',
      }
    }
    case 'wave_started': {
      const par = Array.isArray(e.parallel) ? (e.parallel as unknown[]).length : 0
      return {
        ...base,
        kind: 'info',
        badge: 'wave',
        label: `Wave ${str(e.wave)} started`,
        target: par > 0 ? `${par} task${par === 1 ? '' : 's'} in parallel` : '',
      }
    }
    case 'integration_started':
      return {
        ...base,
        kind: 'running',
        badge: 'merge',
        label: `Integrating wave ${str(e.wave)}`,
        target: Array.isArray(e.tasks) ? (e.tasks as unknown[]).join(', ') : '',
      }
    case 'integration_conflict':
      return {
        ...base,
        kind: 'failed',
        badge: 'merge',
        label: 'Integration conflict',
        target: str(e.path),
        output: `kept ${str(e.applied_task)}, rejected ${str(e.rejected_task)}`,
      }
    case 'integration_completed': {
      const applied = typeof e.applied === 'number' ? e.applied : 0
      const conflicts = typeof e.conflicts === 'number' ? e.conflicts : 0
      return {
        ...base,
        kind: conflicts > 0 ? 'warning' : 'completed',
        badge: 'merge',
        label: `Wave ${str(e.wave)} integrated`,
        target: `${applied} file${applied === 1 ? '' : 's'} applied${
          conflicts > 0 ? `, ${conflicts} conflict${conflicts === 1 ? '' : 's'}` : ''
        }`,
      }
    }
    case 'task_continued':
      return { ...base, kind: 'info', badge: 'task', label: 'Task budget extended', target: `+${str(e.granted_steps)} steps` }
    case 'verification_started': {
      const n = typeof e.commands === 'number' ? e.commands : undefined
      const target = [str(e.mode), n != null ? `${n} command${n === 1 ? '' : 's'}` : '']
        .filter(Boolean)
        .join(', ')
      return { ...base, kind: 'running', badge: 'verify', label: 'Verification started', target }
    }
    case 'verification': {
      if (e.enabled === false) {
        return { ...base, kind: 'skipped', badge: 'verify', label: 'Verification skipped' }
      }
      return { ...base, kind: kindFor(str(e.status)), badge: 'verify', label: `Verification ${str(e.status)}`, target: str(e.command) }
    }
    case 'verification_repair_started':
      return { ...base, kind: 'running', badge: 'repair', label: `Repair pass ${str(e.attempt)}`.trim(), target: str(e.command) }
    case 'verification_reverified':
      return { ...base, kind: kindFor(str(e.status)), badge: 'repair', label: `Re-verified (${str(e.status)})`, target: str(e.command) }
    case 'verification_repair_failed':
      return { ...base, kind: 'failed', badge: 'repair', label: 'Repair pass failed', output: str(e.error) }
    case 'browser_verification_started':
      return { ...base, kind: 'running', badge: 'browser', label: 'Browser verification started' }
    case 'browser_verification':
    case 'browser_verification_ui':
      if (e.enabled === false) return null
      return { ...base, kind: kindFor(str(e.status)), badge: 'browser', label: `Browser verification ${str(e.status)}`, target: str(e.url) }
    case 'visual_review':
      return { ...base, kind: kindFor(str(e.status)), badge: 'visual', label: `Visual review: ${str(e.status)}`, output: str(e.headline), target: str(e.url) }
    case 'run_completed':
      return { ...base, kind: kindFor(str(e.status)), badge: 'done', label: `Agent finished (${str(e.status)})` }
    case 'run_failed':
      return { ...base, kind: 'failed', badge: 'done', label: 'Run failed', output: str(e.error) }
    case 'run_cancel_requested':
      return { ...base, kind: 'skipped', badge: 'cancel', label: 'Cancellation requested' }
    case 'run_cancelled':
      return { ...base, kind: 'cancelled', badge: 'cancel', label: 'Run cancelled', output: str(e.reason) }
    case 'run_retried':
      return { ...base, kind: 'skipped', badge: 'retry', label: 'Run retried', target: str(e.new_run_id) }
    case 'run_interrupted':
      return { ...base, kind: 'failed', badge: 'done', label: 'Run interrupted', output: str(e.reason) }
    case 'memory_reconciled':
      return { ...base, kind: 'info', badge: 'memory', label: 'Memory reconciled', output: str(e.reason) }
    // 'llm_response' is intentionally dropped — it carries the model's raw
    // output (possible chain-of-thought). Intent is shown only via the
    // structured tool_call reason + plan goal/analysis.
    default:
      return null
  }
}

/** Collapse the raw event stream into chronological trace rows.
 *
 * Phase 9: a team run's parallel units interleave their events in one
 * events.jsonl, so a tool_call's result is not necessarily the next event —
 * pairing scans forward (bounded) for the first unconsumed tool_result with
 * the same step / phase / tool AND task attribution. Sequential runs (no
 * task_id on tool events) pair exactly as before.
 */
function buildRows(events: RunEvent[]): TraceRow[] {
  const rows: TraceRow[] = []
  const consumed = new Set<number>()
  for (let i = 0; i < events.length; i++) {
    if (consumed.has(i)) continue
    const e = events[i]
    if (e.type === 'llm_response') continue
    if (e.type === 'tool_call') {
      let match = -1
      for (let j = i + 1; j < events.length && j <= i + 200; j++) {
        if (consumed.has(j)) continue
        const n = events[j]
        if (
          n.type === 'tool_result' &&
          n.step === e.step &&
          n.phase === e.phase &&
          n.tool_name === e.tool_name &&
          n.task_id === e.task_id
        ) {
          match = j
          break
        }
      }
      if (match >= 0) {
        consumed.add(match)
        rows.push(toolRow(e, events[match], `r${i}`))
      } else {
        rows.push(toolRow(e, null, `r${i}`))
      }
      continue
    }
    if (e.type === 'tool_result') {
      // Orphan result (planning read-only rejection / repair run_shell block).
      rows.push(toolRow(null, e, `r${i}`))
      continue
    }
    const row = describeEvent(e, `r${i}`)
    if (row) rows.push(row)
  }
  return rows
}

/**
 * The Live Trace modal — a focused, fast, vertical chronological thread of a
 * run's activity (planning, every file read/write/append/search, shell command,
 * task start/finish, verification, repair, browser verification, cancel/retry).
 *
 * Lighter than the full RunDetailModal: it reads `…/runs/{id}/events` (with the
 * `since` cursor so polling appends rather than re-fetching the whole log) plus
 * run.json for the live header counts, and auto-scrolls while the run is active.
 * After the run finishes it's a complete, replayable record reconstructed from
 * the persisted events.
 */
function RunTrace({ projectId, runId, onClose }: Props) {
  const [record, setRecord] = useState<RunRecord | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [cursor, setCursor] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const cursorRef = useRef(0)

  // Initial load: full event list + record.
  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [recRes, evRes] = await Promise.all([
        fetch(`/api/projects/${projectId}/execution/runs/${runId}`),
        fetch(`/api/projects/${projectId}/execution/runs/${runId}/events`),
      ])
      if (!recRes.ok) throw new Error(`run record HTTP ${recRes.status}`)
      setRecord(await recRes.json())
      if (evRes.ok) {
        const data = await evRes.json()
        const evs: RunEvent[] = Array.isArray(data?.events) ? data.events : []
        setEvents(evs)
        const total = typeof data?.total === 'number' ? data.total : evs.length
        cursorRef.current = total
        setCursor(total)
      }
    } catch (err) {
      console.error('Failed to load run trace:', err)
      setError(err instanceof Error ? err.message : 'Failed to load trace')
    } finally {
      setLoading(false)
    }
  }, [projectId, runId])

  // Incremental poll: fetch only events past the cursor, append, update record.
  const refresh = useCallback(async () => {
    try {
      const [recRes, evRes] = await Promise.all([
        fetch(`/api/projects/${projectId}/execution/runs/${runId}`),
        fetch(`/api/projects/${projectId}/execution/runs/${runId}/events?since=${cursorRef.current}`),
      ])
      if (recRes.ok) setRecord(await recRes.json())
      if (evRes.ok) {
        const data = await evRes.json()
        const fresh: RunEvent[] = Array.isArray(data?.events) ? data.events : []
        if (fresh.length > 0) setEvents((prev) => [...prev, ...fresh])
        const total = typeof data?.total === 'number' ? data.total : cursorRef.current + fresh.length
        cursorRef.current = total
        setCursor(total)
      }
    } catch (err) {
      console.error('Run trace refresh failed:', err)
    }
  }, [projectId, runId])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (!isActive(record)) return
    const id = window.setInterval(refresh, POLL_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [record, refresh])

  // Auto-scroll to newest while the run is active.
  useEffect(() => {
    if (!isActive(record)) return
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [cursor, record])

  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const rows = buildRows(events)
  const active = isActive(record)
  const filesCount = record?.files_changed?.length ?? 0
  const cmdsCount = record?.commands_run?.length ?? 0
  const tasks = record?.plan?.tasks ?? []
  const totalTasks = tasks.length
  const doneTasks = tasks.filter((t) =>
    ['completed', 'failed', 'skipped'].includes(t.status),
  ).length

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content run-trace-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Live Trace</h3>
          <button className="modal-close" onClick={onClose} title="Close">
            ×
          </button>
        </div>

        {record && (
          <div className="run-trace-header">
            <span className={`run-status status-${record.status}`}>
              {active && <span className="run-chat-dot" />}
              {record.status}
            </span>
            <span className="run-trace-metric">{filesCount} file{filesCount === 1 ? '' : 's'}</span>
            <span className="run-trace-metric">{cmdsCount} cmd{cmdsCount === 1 ? '' : 's'}</span>
            {totalTasks > 1 && (
              <span className="run-trace-tasks">
                <span className="run-trace-tasks-bar">
                  <span
                    className="run-trace-tasks-fill"
                    style={{ width: `${totalTasks ? (doneTasks / totalTasks) * 100 : 0}%` }}
                  />
                </span>
                tasks {doneTasks}/{totalTasks}
              </span>
            )}
          </div>
        )}

        <div className="run-trace-body" ref={bodyRef}>
          {loading && <div className="run-detail-loading">Loading…</div>}
          {error && <div className="runs-error">{error}</div>}
          {!loading && !error && rows.length === 0 && (
            <div className="run-detail-none">No activity yet.</div>
          )}
          {rows.length > 0 && (
            <ul className="run-trace-list">
              {rows.map((r) => (
                <li key={r.key} className="run-trace-row">
                  <span className="run-trace-time">{r.time}</span>
                  <span className={`run-verify-status status-${r.kind}`}>{r.badge || r.kind}</span>
                  <span className="run-trace-main">
                    <span className="run-trace-label">{r.label}</span>
                    {r.target && <code className="run-trace-target">{r.target}</code>}
                    {r.output && <span className="run-trace-output">{r.output}</span>}
                  </span>
                  {r.task && <span className="run-trace-task-chip">{r.task}</span>}
                  {r.phase && r.phase !== 'execution' && (
                    <span className="run-trace-phase">{r.phase}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}

export default RunTrace
