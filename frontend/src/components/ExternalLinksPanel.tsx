import { useCallback, useEffect, useState } from 'react'
import type { ConnectorStatus, RunRecord } from '../types'

interface Props {
  projectId: string
  refreshSignal?: number
}

interface LinkItem {
  label: string
  url: string
  sub?: string
}

/**
 * Surfaces the external links a project generates through successful deploys /
 * connections — the live deployed URL, the GitHub repo, the Supabase project,
 * and the latest pull request — so the user can jump straight to them.
 */
function ExternalLinksPanel({ projectId, refreshSignal }: Props) {
  const [links, setLinks] = useState<LinkItem[]>([])

  const load = useCallback(async () => {
    const getJson = async (path: string, fallback: unknown) => {
      try {
        const r = await fetch(`/api/projects/${projectId}${path}`)
        return r.ok ? await r.json() : fallback
      } catch {
        return fallback
      }
    }
    const [conns, repo, runs] = await Promise.all([
      getJson('/connectors', null),
      getJson('/github/repo', null),
      getJson('/execution/runs', []),
    ])

    const items: LinkItem[] = []
    const runList: RunRecord[] = Array.isArray(runs) ? runs : []
    const deployed = runList.find((r) => r.deployment_url)
    if (deployed?.deployment_url) {
      items.push({
        label: 'Live deployment',
        url: deployed.deployment_url,
        sub: deployed.deployment_target || undefined,
      })
    } else {
      // No run carries a deployment URL (a deploy finalizer that died mid-poll
      // loses it on old records, and runs can be pruned) — fall back to
      // Vercel's own deployment list and surface the newest READY one.
      const deps = (await getJson('/vercel/deployments', null)) as {
        deployments?: { url?: string | null; ready_state?: string | null; target?: string | null }[]
      } | null
      const ready = (deps?.deployments || []).filter((d) => d.ready_state === 'READY' && d.url)
      const best = ready.find((d) => d.target === 'production') || ready[0]
      if (best?.url) {
        items.push({
          label: 'Live deployment',
          url: best.url.startsWith('http') ? best.url : `https://${best.url}`,
          sub: best.target || undefined,
        })
      }
    }
    const pr = runList.find((r) => r.pr_url)
    if (pr?.pr_url) {
      items.push({ label: 'Pull request', url: pr.pr_url, sub: pr.pr_number ? `#${pr.pr_number}` : undefined })
    }
    const r = repo as { repo?: string | null; url?: string | null } | null
    if (r?.url) items.push({ label: 'GitHub repo', url: r.url, sub: r.repo || undefined })

    const cmap = conns as Record<string, ConnectorStatus> | null
    const supa = cmap?.supabase
    if (supa?.url) {
      items.push({ label: 'Supabase', url: supa.url, sub: supa.project_ref || undefined })
    } else if (supa?.project_ref) {
      items.push({
        label: 'Supabase dashboard',
        url: `https://supabase.com/dashboard/project/${supa.project_ref}`,
        sub: supa.project_ref,
      })
    }
    setLinks(items)
  }, [projectId])

  useEffect(() => {
    load()
  }, [load, refreshSignal])

  return (
    <details className="links-panel" open={links.length > 0}>
      <summary>Links ({links.length})</summary>
      <div className="context-read">
        {links.length === 0 ? (
          <p className="run-chat-muted">No external links yet — deploy or connect to generate them.</p>
        ) : (
          <ul className="links-list">
            {links.map((l, i) => (
              <li key={`${l.label}-${i}`} className="links-item">
                <a className="external-link" href={l.url} target="_blank" rel="noreferrer" title={l.url}>
                  {l.label}
                </a>
                {l.sub && <span className="run-chat-muted"> · {l.sub}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </details>
  )
}

export default ExternalLinksPanel
