import { useCallback, useEffect, useRef, useState } from 'react'
import type { EnvVarEntry, ExternalActionContract, ExternalActionResponse } from '../types'

interface Props {
  projectId: string
}

/**
 * Phase 8 — project-scoped app-env registry (the BUILT app's env vars, e.g.
 * DATABASE_URL / STRIPE_SECRET_KEY). Presence-only: values are write-only and
 * never displayed. "Push to Vercel" drives the two-phase env-set contract.
 */
function EnvRegistryPanel({ projectId }: Props) {
  const [vars, setVars] = useState<EnvVarEntry[]>([])
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const [secret, setSecret] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<{ key: string; contract: ExternalActionContract } | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/projects/${projectId}/env`)
      const d = await r.json()
      if (mounted.current && Array.isArray(d.vars)) setVars(d.vars)
    } catch {
      /* ignore */
    }
  }, [projectId])

  useEffect(() => {
    load()
  }, [load])

  const add = async () => {
    if (!key.trim() || !value.trim()) {
      setError('Key and value are required')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/env`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: key.trim(), value, secret }),
      })
      const d = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(d?.detail || `HTTP ${res.status}`)
      if (mounted.current) {
        setKey('')
        setValue('')
        load()
      }
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : 'Failed to add')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const del = async (k: string) => {
    try {
      await fetch(`/api/projects/${projectId}/env/${encodeURIComponent(k)}`, { method: 'DELETE' })
      load()
    } catch {
      setError('Failed to delete')
    }
  }

  const pushEnv = useCallback(
    async (k: string, confirm: boolean): Promise<ExternalActionResponse> => {
      const res = await fetch(`/api/projects/${projectId}/vercel/env/set`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: k, confirm }),
      })
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const b = await res.json()
          if (b?.detail) detail = b.detail
        } catch {
          /* keep */
        }
        throw new Error(detail)
      }
      return res.json()
    },
    [projectId],
  )

  const previewPush = async (k: string) => {
    setError(null)
    try {
      const data = await pushEnv(k, false)
      if (mounted.current) setPending({ key: k, contract: data.contract })
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : 'push failed')
    }
  }

  const confirmPush = async () => {
    if (!pending) return
    setBusy(true)
    try {
      await pushEnv(pending.key, true)
      if (mounted.current) setPending(null)
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : 'push failed')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  return (
    <details className="env-registry">
      <summary>Environment ({vars.length})</summary>
      <div className="context-read">
        <ul className="runs-list">
          {vars.map((v) => (
            <li key={v.key} className="run-row-item">
              <span className="run-title" style={{ flex: 1 }}>
                {v.key}
                <span className="run-chat-muted">
                  {' '}
                  · {v.secret ? 'secret' : 'public'} · {v.targets.join('/')}
                  {v.is_set ? ' · set' : ''}
                </span>
              </span>
              <button type="button" className="gitops-btn" onClick={() => previewPush(v.key)} title="Push to Vercel">
                ↑ Vercel
              </button>
              <button type="button" className="run-row-trace-btn" onClick={() => del(v.key)}>
                ✕
              </button>
            </li>
          ))}
          {vars.length === 0 && <li className="run-chat-muted">No env vars yet.</li>}
        </ul>

        <div className="env-add">
          <input
            className="connector-input"
            placeholder="KEY"
            value={key}
            onChange={(e) => setKey(e.target.value)}
          />
          <input
            className="connector-input"
            type="password"
            placeholder="value (write-only)"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoComplete="off"
          />
          <label className="run-chat-muted" style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
            <input type="checkbox" checked={secret} onChange={(e) => setSecret(e.target.checked)} /> secret
          </label>
          <button className="gitops-btn" onClick={add} disabled={busy}>
            Add
          </button>
        </div>

        {error && <div className="run-chat-error">{error}</div>}

        {pending && (
          <div className="gitops-contract">
            <div className="gitops-contract-head">
              <strong>{pending.contract.title}</strong>
              <span className="gitops-tag external">external</span>
            </div>
            <p className="run-chat-muted">
              Push key <code>{pending.contract.key}</code> ({pending.contract.type}) to targets{' '}
              {(pending.contract.targets || []).join(', ')}. The value is never shown.
            </p>
            <div className="gitops-contract-actions">
              <button className="gitops-confirm" onClick={confirmPush} disabled={busy}>
                {busy ? 'Pushing…' : 'Push to Vercel'}
              </button>
              <button className="gitops-cancel" onClick={() => setPending(null)} disabled={busy}>
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </details>
  )
}

export default EnvRegistryPanel
