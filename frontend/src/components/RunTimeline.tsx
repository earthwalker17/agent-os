import type { RunEvent } from '../types'
import { clockTime, kindFor, str } from './runEventUtils'

interface Props {
  events: RunEvent[]
  /**
   * Whether the run is still active. When false (terminal run), any milestone
   * that never received its settling event is coerced from 'running' to
   * 'skipped' so nothing keeps spinning after the run ends.
   */
  runActive?: boolean
}

interface Milestone {
  key: string
  label: string
  detail: string
  kind: string
  time: string
}

/**
 * Settled milestone view of a run's timeline.
 *
 * Unlike the raw event log (which the Live Trace renders), this collapses each
 * logical step's start/settle event PAIR into a single row keyed by that step
 * (plan, each task, verification, each repair attempt, browser). A finished step
 * therefore shows its terminal status (completed / failed / skipped) instead of
 * leaving a stale "Task started [running]" row behind — the historical record
 * stays intact in events.jsonl + the Live Trace; this view just settles.
 */
function buildMilestones(events: RunEvent[]): Milestone[] {
  const order: string[] = []
  const byKey = new Map<string, Milestone>()
  let discreteSeq = 0

  const upsert = (key: string, fields: Partial<Milestone>) => {
    const prev = byKey.get(key)
    if (!prev) order.push(key)
    byKey.set(key, {
      key,
      label: fields.label ?? prev?.label ?? '',
      detail: fields.detail ?? prev?.detail ?? '',
      kind: fields.kind ?? prev?.kind ?? 'running',
      time: fields.time ?? prev?.time ?? '',
    })
  }

  for (const e of events) {
    const time = clockTime(e.timestamp)
    switch (e.type) {
      case 'run_dispatched':
      case 'run_started':
        upsert('run', { label: 'Run started', detail: str(e.title), kind: 'info', time })
        break

      case 'plan_started':
        upsert('plan', { label: 'Planning', detail: 'analyzing the task', kind: 'running', time })
        break
      case 'plan_ready': {
        const n = typeof e.task_count === 'number' ? e.task_count : undefined
        const mode = str(e.mode)
        const detail = [n != null ? `${n} task${n === 1 ? '' : 's'}` : '', mode && `(${mode})`]
          .filter(Boolean)
          .join(' ')
        upsert('plan', { label: 'Plan ready', detail: detail || str(e.goal), kind: 'completed', time })
        break
      }
      case 'plan_failed':
        upsert('plan', {
          label: 'Planning fell back to a single task',
          detail: str(e.error),
          kind: 'skipped',
          time,
        })
        break

      case 'task_started': {
        const id = str(e.task_id)
        const role = str(e.role)
        const wave = typeof e.wave === 'number' ? `wave ${e.wave}` : ''
        const tags = [role && role !== 'coder' ? role : '', wave, e.parallel === true ? 'parallel' : '']
          .filter(Boolean)
          .join(' · ')
        upsert(`task:${id}`, {
          label: str(e.title) || `Task ${id}`,
          detail: tags ? `${tags} — in progress…` : 'in progress…',
          kind: 'running',
          time,
        })
        break
      }
      case 'task_status': {
        const id = str(e.task_id)
        const status = str(e.status)
        const detail = str(e.summary) || str(e.reason) || ''
        upsert(`task:${id}`, {
          label: byKey.get(`task:${id}`)?.label || `Task ${id}`,
          detail,
          kind: kindFor(status),
          time,
        })
        break
      }

      // Phase 9 — team execution milestones.
      case 'team_execution_started': {
        const waves = Array.isArray(e.waves) ? e.waves.length : undefined
        upsert(`evt:${discreteSeq++}`, {
          label: 'Team execution',
          detail: waves != null ? `${waves} wave${waves === 1 ? '' : 's'}` : '',
          kind: 'info',
          time,
        })
        break
      }
      case 'wave_started': {
        const n = typeof e.wave === 'number' ? e.wave : '?'
        const par = Array.isArray(e.parallel) ? e.parallel.length : 0
        upsert(`evt:${discreteSeq++}`, {
          label: `Wave ${n} started`,
          detail: par > 0 ? `${par} task${par === 1 ? '' : 's'} in parallel` : '',
          kind: 'info',
          time,
        })
        break
      }
      case 'integration_started': {
        const n = typeof e.wave === 'number' ? e.wave : '?'
        const tasks = Array.isArray(e.tasks) ? e.tasks.join(', ') : ''
        upsert(`integration:${n}`, {
          label: `Integrating wave ${n}`,
          detail: tasks,
          kind: 'running',
          time,
        })
        break
      }
      case 'integration_completed': {
        const n = typeof e.wave === 'number' ? e.wave : '?'
        const applied = typeof e.applied === 'number' ? e.applied : 0
        const conflicts = typeof e.conflicts === 'number' ? e.conflicts : 0
        upsert(`integration:${n}`, {
          label: `Wave ${n} integrated`,
          detail: `${applied} file${applied === 1 ? '' : 's'} applied${
            conflicts > 0 ? `, ${conflicts} conflict${conflicts === 1 ? '' : 's'}` : ''
          }`,
          kind: conflicts > 0 ? 'warning' : 'completed',
          time,
        })
        break
      }
      case 'integration_conflict':
        upsert(`evt:${discreteSeq++}`, {
          label: 'Integration conflict',
          detail: `${str(e.path)} — kept ${str(e.applied_task)}, rejected ${str(e.rejected_task)}`,
          kind: 'failed',
          time,
        })
        break

      case 'verification_started': {
        const n = typeof e.commands === 'number' ? e.commands : undefined
        const detail = [str(e.mode), n != null ? `${n} command${n === 1 ? '' : 's'}` : '']
          .filter(Boolean)
          .join(', ')
        upsert('verification', { label: 'Verification', detail, kind: 'running', time })
        break
      }
      case 'verification': {
        if (e.enabled === false) {
          upsert('verification', { label: 'Verification skipped', detail: '', kind: 'skipped', time })
        } else {
          upsert('verification', {
            label: `Verification ${str(e.status)}`,
            detail: str(e.command),
            kind: kindFor(str(e.status)),
            time,
          })
        }
        break
      }

      case 'verification_repair_started': {
        const attempt = typeof e.attempt === 'number' ? e.attempt : discreteSeq
        upsert(`repair:${attempt}`, {
          label: `Repair pass ${attempt || ''}`.trim(),
          detail: str(e.command),
          kind: 'running',
          time,
        })
        break
      }
      case 'verification_reverified': {
        const attempt = typeof e.attempt === 'number' ? e.attempt : discreteSeq
        upsert(`repair:${attempt}`, {
          label: `Repair pass ${attempt || ''}`.trim(),
          detail: `re-verified: ${str(e.status)}`,
          kind: kindFor(str(e.status)),
          time,
        })
        break
      }
      case 'verification_repair_failed': {
        const attempt = typeof e.attempt === 'number' ? e.attempt : discreteSeq
        upsert(`repair:${attempt}`, {
          label: `Repair pass ${attempt || ''}`.trim(),
          detail: str(e.error),
          kind: 'failed',
          time,
        })
        break
      }

      case 'browser_verification_started':
        upsert('browser', { label: 'Browser verification', detail: '', kind: 'running', time })
        break
      case 'browser_verification':
      case 'browser_verification_ui': {
        if (e.enabled === false) break
        upsert('browser', {
          label: `Browser verification ${str(e.status)}`,
          detail: str(e.url),
          kind: kindFor(str(e.status)),
          time,
        })
        break
      }
      case 'visual_review':
        upsert('visual_review', {
          label: `Visual review: ${str(e.status)}`,
          detail: str(e.headline),
          kind: kindFor(str(e.status)),
          time,
        })
        break

      // Discrete lifecycle facts — each gets a unique key so they all show.
      case 'run_completed':
        upsert(`evt:${discreteSeq++}`, {
          label: `Agent finished (${str(e.status)})`,
          detail: '',
          kind: kindFor(str(e.status)),
          time,
        })
        break
      case 'run_failed':
        upsert(`evt:${discreteSeq++}`, { label: 'Run failed', detail: str(e.error), kind: 'failed', time })
        break
      case 'run_cancel_requested':
        upsert(`evt:${discreteSeq++}`, { label: 'Cancellation requested', detail: '', kind: 'skipped', time })
        break
      case 'run_cancelled':
        upsert(`evt:${discreteSeq++}`, { label: 'Run cancelled', detail: str(e.reason), kind: 'cancelled', time })
        break
      case 'run_retried':
        upsert(`evt:${discreteSeq++}`, { label: 'Run retried', detail: str(e.new_run_id), kind: 'skipped', time })
        break
      case 'run_interrupted':
        upsert(`evt:${discreteSeq++}`, { label: 'Run interrupted', detail: str(e.reason), kind: 'failed', time })
        break

      default:
        break
    }
  }

  return order.map((k) => byKey.get(k)!)
}

function RunTimeline({ events, runActive = false }: Props) {
  const milestones = buildMilestones(events)

  if (milestones.length === 0) {
    return <div className="run-detail-none">No events yet.</div>
  }

  return (
    <ul className="run-timeline">
      {milestones.map((m) => {
        // A terminal run should never leave a milestone spinning — settle any
        // dangling 'running' to 'skipped' (it never reached its completion event).
        const kind = !runActive && m.kind === 'running' ? 'skipped' : m.kind
        return (
          <li key={m.key} className="run-timeline-event">
            <span className={`run-verify-status status-${kind}`}>{kind}</span>
            <span className="run-timeline-label">{m.label}</span>
            {m.detail && <code className="run-timeline-detail">{m.detail}</code>}
            {m.time && <span className="run-timeline-time">{m.time}</span>}
          </li>
        )
      })}
    </ul>
  )
}

export default RunTimeline
