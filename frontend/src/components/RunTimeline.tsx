import type { RunEvent } from '../types'

interface Props {
  events: RunEvent[]
}

/** Map a status-ish word onto the shared `.run-verify-status status-*` palette. */
function kindFor(status: string | undefined): string {
  switch (status) {
    case 'completed':
    case 'passed':
    case 'running':
    case 'skipped':
    case 'pending':
    case 'failed':
      return status
    case 'partial':
    case 'blocked':
      return 'failed'
    default:
      return 'running'
  }
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

interface Row {
  label: string
  detail: string
  kind: string
}

/**
 * Translate one factual run event into a timeline row, or null to drop it.
 * We deliberately surface only meaningful lifecycle/plan/task/verification
 * events and shell commands — read-only file/list/search tool calls are noise.
 */
function describe(e: RunEvent): Row | null {
  switch (e.type) {
    case 'run_dispatched':
    case 'run_started':
      return { label: 'Run started', detail: str(e.title), kind: 'running' }
    case 'plan_started':
      return { label: 'Planning started', detail: '', kind: 'running' }
    case 'plan_ready': {
      const n = typeof e.task_count === 'number' ? e.task_count : undefined
      const mode = str(e.mode)
      const detail = [n != null ? `${n} task${n === 1 ? '' : 's'}` : '', mode && `(${mode})`]
        .filter(Boolean)
        .join(' ')
      return { label: 'Plan ready', detail: detail || str(e.goal), kind: 'completed' }
    }
    case 'plan_failed':
      return { label: 'Planning fell back to a single task', detail: str(e.error), kind: 'skipped' }
    case 'task_started':
      return { label: 'Task started', detail: str(e.title) || str(e.task_id), kind: 'running' }
    case 'task_status': {
      const status = str(e.status)
      const detail = [str(e.task_id), str(e.summary) || str(e.reason)].filter(Boolean).join(' — ')
      return { label: `Task ${status}`, detail, kind: kindFor(status) }
    }
    case 'tool_call': {
      if (str(e.tool_name) !== 'run_shell') return null
      const args = (e.arguments ?? {}) as Record<string, unknown>
      return { label: 'Command', detail: str(args.command), kind: 'running' }
    }
    case 'verification_started': {
      const n = typeof e.commands === 'number' ? e.commands : undefined
      const detail = [str(e.mode), n != null ? `${n} command${n === 1 ? '' : 's'}` : '']
        .filter(Boolean)
        .join(', ')
      return { label: 'Verification started', detail, kind: 'running' }
    }
    case 'verification_repair_started':
      return { label: 'Repair pass started', detail: str(e.command), kind: 'running' }
    case 'verification_repair_failed':
      return { label: 'Repair pass failed', detail: str(e.error), kind: 'failed' }
    case 'verification_reverified':
      return { label: `Re-verified: ${str(e.status)}`, detail: str(e.command), kind: kindFor(str(e.status)) }
    case 'verification': {
      if (e.enabled === false) return { label: 'Verification skipped', detail: '', kind: 'skipped' }
      return { label: `Verification ${str(e.status)}`, detail: str(e.command), kind: kindFor(str(e.status)) }
    }
    case 'browser_verification_started':
      return { label: 'Browser verification started', detail: '', kind: 'running' }
    case 'browser_verification':
    case 'browser_verification_ui': {
      if (e.enabled === false) return null
      return {
        label: `Browser verification ${str(e.status)}`,
        detail: str(e.url),
        kind: kindFor(str(e.status)),
      }
    }
    case 'run_completed':
      return { label: `Agent finished (${str(e.status)})`, detail: '', kind: kindFor(str(e.status)) }
    case 'run_failed':
      return { label: 'Run failed', detail: str(e.error), kind: 'failed' }
    case 'run_cancel_requested':
      return { label: 'Cancellation requested', detail: '', kind: 'skipped' }
    case 'run_cancelled':
      return { label: 'Run cancelled', detail: str(e.reason), kind: 'skipped' }
    case 'run_retried':
      return { label: 'Run retried', detail: str(e.new_run_id), kind: 'skipped' }
    case 'run_interrupted':
      return { label: 'Run interrupted', detail: str(e.reason), kind: 'failed' }
    default:
      return null
  }
}

function clockTime(iso: string | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/**
 * Read-only factual timeline of a run's events. Driven by the
 * `…/runs/{id}/events` endpoint (events.jsonl). Used in the Run Detail modal
 * and polled while the run is active.
 */
function RunTimeline({ events }: Props) {
  const rows = events
    .map((e) => ({ row: describe(e), time: clockTime(e.timestamp) }))
    .filter((x): x is { row: Row; time: string } => x.row !== null)

  if (rows.length === 0) {
    return <div className="run-detail-none">No events yet.</div>
  }

  return (
    <ul className="run-timeline">
      {rows.map(({ row, time }, i) => (
        <li key={i} className="run-timeline-event">
          <span className={`run-verify-status status-${row.kind}`}>{row.kind}</span>
          <span className="run-timeline-label">{row.label}</span>
          {row.detail && <code className="run-timeline-detail">{row.detail}</code>}
          {time && <span className="run-timeline-time">{time}</span>}
        </li>
      ))}
    </ul>
  )
}

export default RunTimeline
