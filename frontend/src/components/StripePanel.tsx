import { useCallback, useEffect, useRef, useState } from 'react'
import type { RunRecord, ExternalActionContract, ExternalActionResponse } from '../types'
import ExternalActionPanel from './ExternalActionPanel'

interface Props {
  projectId: string
  record: RunRecord
  compact?: boolean
}

interface StripeStatus {
  configured?: boolean
  connected?: boolean
  mode?: string
  has_webhook_secret?: boolean
  error?: string | null
}

type Kind = 'checkout_test' | 'webhook_register'

/**
 * Phase 8 — Stripe TEST-mode controls. Provision a test Product+Price (returns a
 * price_id for the app's env) and register the deployed webhook endpoint (the
 * returned signing secret is stored, never shown). Both are two-phase contracts.
 * Everything is test-mode; a "Local test" helper surfaces the `stripe listen`
 * command for local webhook forwarding. The per-purchase checkout + signature
 * verification live in the built app, not here.
 */
function StripePanel({ projectId, record, compact }: Props) {
  const [status, setStatus] = useState<StripeStatus | null>(null)
  const [pending, setPending] = useState<{ kind: Kind; contract: ExternalActionContract } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)
  const [localCmd, setLocalCmd] = useState<string | null>(null)
  const [hookUrl, setHookUrl] = useState('')
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    let active = true
    fetch(`/api/projects/${projectId}/stripe/status`)
      .then((r) => (r.ok ? r.json() : null))
      .then((s) => {
        if (active && s) setStatus(s)
      })
      .catch(() => {})
    return () => {
      active = false
    }
  }, [projectId])

  useEffect(() => {
    if (record.deployment_url && !hookUrl) setHookUrl(`${record.deployment_url}/api/stripe/webhook`)
  }, [record.deployment_url, hookUrl])

  const post = useCallback(
    async (path: string, payload: Record<string, unknown>): Promise<ExternalActionResponse> => {
      const res = await fetch(`/api/projects/${projectId}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
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

  const pathFor = (kind: Kind) => (kind === 'checkout_test' ? '/stripe/checkout-test' : '/stripe/webhook/register')
  const payloadFor = (kind: Kind, confirm: boolean): Record<string, unknown> =>
    kind === 'checkout_test' ? { confirm } : { url: hookUrl, confirm }

  const preview = async (kind: Kind) => {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const data = await post(pathFor(kind), payloadFor(kind, false))
      if (mounted.current) setPending({ kind, contract: data.contract })
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : 'preview failed')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const confirm = async () => {
    if (!pending) return
    setBusy(true)
    setError(null)
    try {
      const data = await post(pathFor(pending.kind), payloadFor(pending.kind, true))
      if (mounted.current) {
        const c = data.contract
        if (pending.kind === 'checkout_test') setResult(`price_id: ${c.price_id} (add to env)`)
        else setResult(`webhook ${c.endpoint_id ?? ''} registered (signing secret stored)`)
        setPending(null)
      }
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : 'action failed')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const showLocalCmd = async () => {
    try {
      const r = await fetch(`/api/projects/${projectId}/stripe/webhook/local-command`)
      const d = await r.json()
      if (mounted.current) setLocalCmd(`${d.listen}\n${d.trigger}`)
    } catch {
      setError('could not load local command')
    }
  }

  const configured = !!status?.configured

  return (
    <div className={`gitops-panel${compact ? ' gitops-compact' : ''}`}>
      <div className="gitops-head">
        <strong>Payments (Stripe)</strong>
        <span className="gitops-tag">TEST</span>
      </div>

      {!configured ? (
        <p className="run-chat-muted">No Stripe test key — connect Stripe from the Connectors panel.</p>
      ) : (
        <div className="gitops-status">
          <span className="gitops-badge">{status?.connected ? 'connected' : 'configured'}</span>
          {status?.has_webhook_secret && <span className="gitops-badge">webhook secret stored</span>}
          {status?.error && <span className="run-chat-error">{status.error}</span>}
        </div>
      )}

      <div className="gitops-actions">
        <button type="button" className="gitops-btn" onClick={() => preview('checkout_test')} disabled={busy || !configured}>
          Provision test price…
        </button>
        <button type="button" className="gitops-btn" onClick={() => preview('webhook_register')} disabled={busy || !configured}>
          Register webhook…
        </button>
        <button type="button" className="gitops-btn" onClick={showLocalCmd} disabled={busy}>
          Local test cmd
        </button>
      </div>

      {result && <p className="run-chat-muted">✓ {result}</p>}
      {localCmd && <pre className="gitops-diff">{localCmd}</pre>}
      {error && <div className="run-chat-error">{error}</div>}

      {pending && (
        <ExternalActionPanel
          contract={pending.contract}
          busy={busy}
          confirmLabel={pending.kind === 'checkout_test' ? 'Provision' : 'Register'}
          onConfirm={confirm}
          onCancel={() => setPending(null)}
        >
          {pending.kind === 'checkout_test' ? (
            <p className="run-chat-muted">
              Create a TEST product <code>{pending.contract.name}</code> at{' '}
              {((pending.contract.amount ?? 0) / 100).toFixed(2)} {pending.contract.currency?.toUpperCase()} (
              {pending.contract.checkout_mode}). Returns a price_id for the app env.
            </p>
          ) : (
            <>
              <label className="gitops-label">Endpoint URL</label>
              <input className="connector-input" value={hookUrl} onChange={(e) => setHookUrl(e.target.value)} />
              <p className="run-chat-muted">
                Events: {(pending.contract.enabled_events || []).join(', ')}. The signing secret is stored, never shown.
              </p>
            </>
          )}
        </ExternalActionPanel>
      )}
    </div>
  )
}

export default StripePanel
