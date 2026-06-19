/**
 * Shared helpers for rendering a run's events.jsonl timeline.
 *
 * Used by both the settled milestone view (`RunTimeline`, inside the Run Detail
 * modal) and the granular chronological view (`RunTrace`, the Live Trace modal).
 */

/** Map a status-ish word onto the shared `.run-verify-status status-*` palette. */
export function kindFor(status: string | undefined): string {
  switch (status) {
    case 'completed':
    case 'passed':
    case 'running':
    case 'skipped':
    case 'pending':
    case 'failed':
    case 'cancelled':
      return status
    case 'partial':
    case 'blocked':
      return 'failed'
    default:
      return 'running'
  }
}

/** Coerce any value to a display string ('' for null/undefined). */
export function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

/** HH:MM:SS local clock for an ISO timestamp, or '' when unparseable. */
export function clockTime(iso: string | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
