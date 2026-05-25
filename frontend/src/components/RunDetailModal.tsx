import { useCallback, useEffect, useState } from 'react'
import type { RunRecord, VerificationResult } from '../types'

interface Props {
  projectId: string
  runId: string
  onClose: () => void
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
          <span className="run-detail-verify-meta">no verify command configured</span>
        </div>
      </div>
    )
  }
  return (
    <div className="run-detail-verification">
      <div>
        <span className={`run-verify-status status-${v.status}`}>{v.status}</span>
        {typeof v.exit_code === 'number' && (
          <span className="run-detail-verify-meta">exit {v.exit_code}</span>
        )}
        {typeof v.duration_ms === 'number' && (
          <span className="run-detail-verify-meta">{v.duration_ms} ms</span>
        )}
      </div>
      {v.command && (
        <div className="run-detail-verify-cmd">
          <span className="run-detail-label">Command</span>
          <code>{v.command}</code>
        </div>
      )}
      {v.output_preview && (
        <pre className="run-detail-verify-output">{v.output_preview}</pre>
      )}
    </div>
  )
}

function RunDetailModal({ projectId, runId, onClose }: Props) {
  const [record, setRecord] = useState<RunRecord | null>(null)
  const [resultMd, setResultMd] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [recRes, resRes] = await Promise.all([
        fetch(`/api/projects/${projectId}/execution/runs/${runId}`),
        fetch(`/api/projects/${projectId}/execution/runs/${runId}/result`),
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
    } catch (err) {
      console.error('Failed to load run detail:', err)
      setError(err instanceof Error ? err.message : 'Failed to load run')
    } finally {
      setLoading(false)
    }
  }, [projectId, runId])

  useEffect(() => {
    load()
  }, [load])

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
