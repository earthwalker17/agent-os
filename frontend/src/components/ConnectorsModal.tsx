import { useCallback, useEffect, useState } from 'react'
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
  /** The account-level access token now lives in backend/.env, not the UI. */
  envHint: { field: string; env: string }
  secrets: FieldDef[]
  meta: FieldDef[]
  note?: string
}

// github keeps its own dedicated Git panel (token → .env, repo URL project-level);
// this modal covers the Phase 8 providers via the generic /credentials/{provider}.
//
// Account-level access TOKENS are entered once in backend/.env (VERCEL_TOKEN,
// SUPABASE_ACCESS_TOKEN, STRIPE_SECRET_KEY) — the connectors read them from
// there. Only PROJECT-specific fields are entered here.
const CONFIG: Record<Exclude<ConnectorProvider, 'github'>, ProviderDef> = {
  vercel: {
    label: 'Vercel',
    envHint: { field: 'Access token', env: 'VERCEL_TOKEN' },
    secrets: [],
    meta: [
      { key: 'project_id', label: 'Project ID (link the Vercel project)' },
      { key: 'org_id', label: 'Team / Org ID (optional)' },
    ],
  },
  supabase: {
    label: 'Supabase',
    envHint: { field: 'Access token (sbp_…)', env: 'SUPABASE_ACCESS_TOKEN' },
    secrets: [
      { key: 'db_password', label: 'Database password (project-specific)' },
      { key: 'service_role', label: 'service_role key (optional, project-specific)' },
    ],
    meta: [
      { key: 'project_ref', label: 'Project ref' },
      { key: 'url', label: 'Project URL' },
      { key: 'anon_key', label: 'Anon key (public)' },
    ],
  },
  stripe: {
    label: 'Stripe (TEST)',
    envHint: { field: 'Secret key (sk_test_…)', env: 'STRIPE_SECRET_KEY' },
    secrets: [
      { key: 'webhook_secret', label: 'Webhook signing secret (whsec_…, usually auto-set on register)' },
    ],
    meta: [{ key: 'publishable_key', label: 'Publishable key (pk_test_…, public)' }],
    note: 'Test mode only — a live key is refused unless you explicitly allow it.',
  },
}

const PROVIDERS = Object.keys(CONFIG) as Exclude<ConnectorProvider, 'github'>[]

/**
 * Phase 8 — multi-provider connector setup (Vercel / Supabase / Stripe). Account
 * tokens are set in backend/.env; only project-specific fields are entered here.
 * Secrets are write-only (never returned); saved non-secret metadata stays
 * visible in the inputs so the user can see + edit what's set. Closing with
 * unsaved edits prompts to save first — only Save persists.
 */
function ConnectorsModal({ projectId, onClose, onSaved }: Props) {
  const [provider, setProvider] = useState<Exclude<ConnectorProvider, 'github'>>('vercel')
  const [fields, setFields] = useState<Record<string, string>>({})
  const [scope, setScope] = useState<'project' | 'global'>('project')
  const [allowLive, setAllowLive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [statuses, setStatuses] = useState<Record<string, ConnectorStatus>>({})
  const [dirty, setDirty] = useState(false)

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

  // Prefill the (non-secret) metadata inputs from the saved status so the user
  // always sees what's set — and re-runs after a save/provider-switch. Secret
  // inputs stay blank (write-only) but their label shows "(set)".
  useEffect(() => {
    const st = statuses[provider] as unknown as Record<string, unknown> | undefined
    const init: Record<string, string> = {}
    for (const f of CONFIG[provider].meta) {
      const v = st?.[f.key]
      if (v != null && String(v).trim()) init[f.key] = String(v)
    }
    setFields(init)
    setDirty(false)
    setError(null)
    setAllowLive(false)
  }, [provider, statuses])

  const setField = (k: string, v: string) => {
    setFields((f) => ({ ...f, [k]: v }))
    setDirty(true)
  }

  const save = async (): Promise<boolean> => {
    const clean: Record<string, string> = {}
    for (const [k, v] of Object.entries(fields)) if (v.trim()) clean[k] = v.trim()
    if (Object.keys(clean).length === 0) {
      setError('Enter at least one project value')
      return false
    }
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`/api/projects/${projectId}/credentials/${provider}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fields: clean, scope, allow_live: allowLive }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
      await load() // re-prefill from the refreshed status (keeps saved values visible)
      onSaved?.()
      setDirty(false)
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save')
      return false
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
      setDirty(false)
    } catch {
      setError('Failed to disconnect')
    } finally {
      setBusy(false)
    }
  }

  // Closing with unsaved edits prompts to save (only Save persists).
  const attemptClose = useCallback(async () => {
    if (dirty && !busy) {
      const doSave = window.confirm(
        'You have unsaved changes.\n\nOK — Save & close\nCancel — Discard changes & close',
      )
      if (doSave) {
        const ok = await save()
        if (!ok) return // save failed — stay open so the user can fix it
      }
    }
    onClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, busy])

  return (
    <div className="modal-overlay" onClick={attemptClose}>
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
            'Only project-specific fields are entered here — saved values stay visible. Secrets are write-only and never shown again.'}
        </p>

        {/* Account-level token lives in .env, not the UI. */}
        <div className="connector-env-hint">
          {def.envHint.field}: set <code>{def.envHint.env}</code> in <code>backend/.env</code>{' '}
          (account-level — shared by all projects).
        </div>

        {current?.configured && (
          <p className="run-chat-muted">
            Token configured · scope {current.scope} · source {current.source}
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
              placeholder={current?.secret_fields?.[f.key] ? '•••••• (saved — leave blank to keep)' : f.placeholder || '••••••'}
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
              placeholder={f.placeholder || ''}
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
            <input type="checkbox" checked={allowLive} onChange={(e) => { setAllowLive(e.target.checked); setDirty(true) }} />
            Allow a live (non-test) key — discouraged
          </label>
        )}

        {error && <div className="run-chat-error">{error}</div>}
        <div className="modal-actions">
          <button className="gitops-confirm" onClick={save} disabled={busy || !dirty}>
            {busy ? 'Saving…' : 'Save'}
          </button>
          {current?.configured && (
            <button className="btn-cancel" onClick={disconnect} disabled={busy}>
              Disconnect
            </button>
          )}
          <button className="btn-cancel" onClick={attemptClose} disabled={busy}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}

export default ConnectorsModal
