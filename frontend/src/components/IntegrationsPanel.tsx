import { useCallback, useEffect, useState } from 'react'
import type { ConnectorProvider, ConnectorStatus, GitHubConnectorStatus } from '../types'
import ConnectorsModal from './ConnectorsModal'
import GitHubModal from './GitHubModal'
import {
  BrandGitHub,
  BrandStripe,
  BrandSupabase,
  BrandVercel,
  IconChevronRight,
} from './icons'

/** Live per-provider status — the four validated /status endpoints all return
 * `connected` + non-secret metadata on top of the presence fields. */
type LiveStatus =
  | (ConnectorStatus & {
      connected?: boolean
      linked?: boolean
      error?: string | null
      mode?: string | null
    })
  | null

interface Props {
  projectId: string
  refreshSignal?: number
}

const ORDER: ConnectorProvider[] = ['github', 'vercel', 'supabase', 'stripe']

const ENDPOINTS: Record<ConnectorProvider, string> = {
  github: '/github/connector',
  vercel: '/vercel/status',
  supabase: '/supabase/status',
  stripe: '/stripe/status',
}

const NAMES: Record<ConnectorProvider, string> = {
  github: 'GitHub',
  vercel: 'Vercel',
  supabase: 'Supabase',
  stripe: 'Stripe',
}

function markFor(p: ConnectorProvider) {
  switch (p) {
    case 'github':
      return <BrandGitHub size={15} />
    case 'vercel':
      return <BrandVercel size={14} />
    case 'supabase':
      return <BrandSupabase size={15} />
    case 'stripe':
      return <BrandStripe size={15} />
  }
}

/** Status tone + line for a card: connected (ok) > error (danger) >
 * configured-but-unvalidated (warn) > not connected (muted). */
function toneOf(s: LiveStatus): { tone: 'ok' | 'warn' | 'danger' | 'muted'; line: string } {
  if (!s) return { tone: 'muted', line: 'Not connected' }
  if (s.connected) {
    const who = s.login || s.username || s.account || s.project_ref || ''
    return { tone: 'ok', line: who ? `Connected · ${who}` : 'Connected' }
  }
  if (s.configured && s.error) return { tone: 'danger', line: 'Connection error' }
  if (s.configured) return { tone: 'warn', line: 'Configured — not validated' }
  return { tone: 'muted', line: 'Not connected' }
}

/**
 * Public UI pass — the Integrations section: one card per provider (GitHub /
 * Vercel / Supabase / Stripe) with a live connection status. Clicking a card
 * opens that provider's OWN central modal — GitHub → GitHubModal (repo +
 * working-tree status + a traceable git-history view), the Phase 8 providers →
 * ConnectorsModal locked to that single provider. Presentation only; every
 * underlying endpoint/contract is unchanged.
 */
function IntegrationsPanel({ projectId, refreshSignal }: Props) {
  const [statuses, setStatuses] = useState<Record<ConnectorProvider, LiveStatus>>({
    github: null,
    vercel: null,
    supabase: null,
    stripe: null,
  })
  const [openModal, setOpenModal] = useState<ConnectorProvider | null>(null)

  const load = useCallback(async () => {
    const fetchOne = async (p: ConnectorProvider): Promise<LiveStatus> => {
      try {
        const r = await fetch(`/api/projects/${projectId}${ENDPOINTS[p]}`)
        return r.ok ? await r.json() : null
      } catch {
        return null
      }
    }
    const [github, vercel, supabase, stripe] = await Promise.all(ORDER.map(fetchOne))
    setStatuses({ github, vercel, supabase, stripe })
  }, [projectId])

  useEffect(() => {
    load()
  }, [load, refreshSignal])

  return (
    <div className="integrations">
      {ORDER.map(p => {
        const s = statuses[p]
        const { tone, line } = toneOf(s)
        return (
          <button
            key={p}
            type="button"
            className="integration-card"
            onClick={() => setOpenModal(p)}
            title={s?.error || `Open ${NAMES[p]} settings`}
          >
            <span className={`integration-mark ${p}`}>{markFor(p)}</span>
            <span className="integration-name">
              {NAMES[p]}
              {p === 'stripe' && <span className="integration-tag">test</span>}
            </span>
            <span className={`integration-status tone-${tone}`}>{line}</span>
            <span className="integration-open-icon">
              <IconChevronRight />
            </span>
          </button>
        )
      })}

      {openModal === 'github' && (
        <GitHubModal
          projectId={projectId}
          connector={statuses.github as GitHubConnectorStatus | null}
          refreshSignal={refreshSignal}
          onClose={() => setOpenModal(null)}
          onSaved={load}
        />
      )}
      {openModal && openModal !== 'github' && (
        <ConnectorsModal
          projectId={projectId}
          singleProvider={openModal as Exclude<ConnectorProvider, 'github'>}
          onClose={() => setOpenModal(null)}
          onSaved={load}
        />
      )}
    </div>
  )
}

export default IntegrationsPanel
