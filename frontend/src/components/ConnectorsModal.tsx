import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ConnectorProvider, ConnectorStatus } from '../types'

interface Props {
  projectId: string
  onClose: () => void
  onSaved?: () => void
}

interface FieldDef {
  key: string
  label: string
  placeholder?: string
}

interface ProviderDef {
  label: string
  secrets: FieldDef[]
  meta: FieldDef[]
  note?: string
}

// github keeps its own dedicated connector modal (it validates + captures login);
// this modal covers the Phase 8 providers via the generic /credentials/{provider}.
const CONFIG: Record<Exclude<ConnectorProvider, 'github'>, ProviderDef> = {
  vercel: {
    label: 'Vercel',
    secrets: [{ key: 'token', label: 'Access token', placeholder: 'from vercel.com/account/tokens' }],
    meta: [
      { key: 'org_id', label: 'Team / Org ID (optional)' },
      { key: 'project_id', label: 'Project ID (link the Vercel project)' },
    ],
  },
  supabase: {
    label: 'Supabase',
    secrets: [
      { key: 'access_token', label: 'Access token (sbp_…)' },
      { key: 'db_password', label: 'Database password' },
      { key: 'service_role', label: 'service_role key (optional, secret)' },
    ],
    meta: [
      { key: 'project_ref', label: 'Project ref' },
      { key: 'url', label: 'Project URL' },
      { key: 'anon_key', label: 'Anon key (public)' },
    ],
  },
  stripe: {
    label: 'Stripe (TEST)',
    secrets: [
      { key: 'secret_key', label: 'Secret key (sk_test_…)' },
      { key: 'webhook_secret', label: 'Webhook signing secret (whsec_…)' },
    ],
    meta: [{ key: 'publishable_key', label: 'Publishable key (pk_test_…, public)' }],
    note: 'Test mode only — a live key is refused unless you explicitly allow it.',
  },
}

const PROVIDERS = Object.keys(CONFIG) as Exclude<ConnectorProvider, 'github'>[]

/**
 * Phase 8 — multi-provider connector setup (Vercel / Supabase / Stripe). Secrets
 * are written to the gitignored credential store and never returned; the UI only
 * ever shows presence. Mirrors the GitHub ConnectorModal but generalized.
 */
function ConnectorsModal({ projectId, onClose, onSaved }: Props) {
  const [provider, setProvider] = useState<Exclude<ConnectorProvider, 'github'>>('vercel')
  const [fields, setFields] = useState<Record<string, string>>({})
  const [scope, setScope] = useState<'project' | 'global'>('project')
  const [allowLive, setAllowLive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [statuses, setStatuses] = useState<Record<string, ConnectorStatus>>({})

  const def = CONFIG[provider]
  const current = statuses[provider]

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/projects/${projectId}/connectors`)
      if (r.ok) setStatuses(await r.json())
    } catch {
      /* ignore */
    }
  }, [projectId])

  useEffect(() => {
    load()
  }, [load])

  // reset the form when switching providers
  useEffect(() => {
    setFields({})
    setError(null)
    setAllowLive(false)
  }, [provider])

  const setField = (k: string, v: string) => setFields((f) => ({ ...f, [k]: v }))

  const hasInput = useMemo(() => Object.values(fields).some((v) => v.trim()), [fields])

  const save = async () => {
    if (!hasInput) {
      setError('Enter at least one value')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const clean: Record<string, string> = {}
      for (const [k, v] of Object.entries(fields)) if (v.trim()) clean[k] = v.trim()
      const res = await fetch(`/api/projects/${projectId}/credentials/${provider}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fields: clean, scope, allow_live: allowLive }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
      await load()
      setFields({})
      onSaved?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save')
    } finally {
      setBusy(false)
    }
  }

  const disconnect = async () => {
    setBusy(true)
    setError(null)
    try {
      await fetch(`/api/projects/${projectId}/credentials/${provider}?scope=${scope}`, { method: 'DELETE' })
      await load()
      onSaved?.()
    } catch {
      setError('Failed to disconnect')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="connector-modal" onClick={(e) => e.stopPropagation()}>
        <h3>Connectors</h3>
        <div className="gitops-actions" style={{ marginBottom: 8 }}>
          {PROVIDERS.map((p) => (
            <button
              key={p}
              type="button"
              className={`gitops-btn${provider === p ? ' connected' : ''}`}
              onClick={() => setProvider(p)}
            >
              {CONFIG[p].label}
              {statuses[p]?.configured ? ' ✓' : ''}
            </button>
          ))}
        </div>

        <p className="run-chat-muted">
          {def.note ||
            'Stored in the gitignored credential store. Never appears in prompts, logs, commits, memory, or the UI.'}
        </p>
        {current?.configured && (
          <p className="run-chat-muted">
            Currently configured · scope {current.scope} · source {current.source}
          </p>
        )}

        {def.secrets.map((f) => (
          <div key={f.key}>
            <label className="gitops-label">
              {f.label}
              {current?.secret_fields?.[f.key] ? ' (set)' : ''}
            </label>
            <input
              type="password"
              className="connector-input"
              value={fields[f.key] || ''}
              onChange={(e) => setField(f.key, e.target.value)}
              placeholder={f.placeholder || '••••••'}
              autoComplete="off"
            />
          </div>
        ))}
        {def.meta.map((f) => (
          <div key={f.key}>
            <label className="gitops-label">{f.label}</label>
            <input
              type="text"
              className="connector-input"
              value={fields[f.key] || ''}
              onChange={(e) => setField(f.key, e.target.value)}
              autoComplete="off"
            />
          </div>
        ))}

        <label className="gitops-label">Scope</label>
        <select
          className="connector-select"
          value={scope}
          onChange={(e) => setScope(e.target.value as 'project' | 'global')}
        >
          <option value="project">This project</option>
          <option value="global">Global (all projects)</option>
        </select>

        {provider === 'stripe' && (
          <label className="run-chat-muted" style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 6 }}>
            <input type="checkbox" checked={allowLive} onChange={(e) => setAllowLive(e.target.checked)} />
            Allow a live (non-test) key — discouraged
          </label>
        )}

        {error && <div className="run-chat-error">{error}</div>}
        <div className="modal-actions">
          <button className="gitops-confirm" onClick={save} disabled={busy}>
            {busy ? 'Saving…' : 'Save'}
          </button>
          {current?.configured && (
            <button className="btn-cancel" onClick={disconnect} disabled={busy}>
              Disconnect
            </button>
          )}
          <button className="btn-cancel" onClick={onClose} disabled={busy}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}

export default ConnectorsModal
