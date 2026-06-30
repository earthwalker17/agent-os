import type { ReactNode } from 'react'
import type { ExternalActionContract } from '../types'

interface Props {
  contract: ExternalActionContract
  busy: boolean
  confirmLabel?: string
  /** Disable confirm (e.g. a precondition like "no token") with a reason note. */
  blockedReason?: string | null
  onConfirm: () => void
  onCancel: () => void
  children?: ReactNode
}

/**
 * Phase 8 — the generic two-phase External Action Contract preview block.
 * Renders the contract envelope (external / destructive / TEST tags) + a confirm
 * gate; the action-specific body is passed as children. Shared by the Deploy /
 * Migration / Checkout / Webhook panels so every external action confirms the
 * same way (mirrors the Phase 7 GitOps contract block).
 */
function ExternalActionPanel({
  contract,
  busy,
  confirmLabel,
  blockedReason,
  onConfirm,
  onCancel,
  children,
}: Props) {
  return (
    <div className={`gitops-contract${contract.destructive ? ' destructive' : ''}`}>
      <div className="gitops-contract-head">
        <strong>{contract.title}</strong>
        {contract.external && <span className="gitops-tag external">external</span>}
        {contract.destructive && <span className="gitops-tag danger">destructive</span>}
        {contract.mode === 'test' && <span className="gitops-tag">TEST</span>}
        {contract.mode === 'live' && <span className="gitops-tag danger">LIVE</span>}
      </div>

      {children}

      {blockedReason && <p className="run-chat-error">{blockedReason}</p>}

      <div className="gitops-contract-actions">
        <button
          type="button"
          className={`gitops-confirm${contract.destructive ? ' gitops-danger' : ''}`}
          onClick={onConfirm}
          disabled={busy || !!blockedReason}
        >
          {busy ? 'Working…' : confirmLabel || 'Confirm'}
        </button>
        <button type="button" className="gitops-cancel" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  )
}

export default ExternalActionPanel
