import type { ProjectContext } from '../types'
import RunsSection from './RunsSection'
import EnvRegistryPanel from './EnvRegistryPanel'
import IntegrationsPanel from './IntegrationsPanel'
import ExternalLinksPanel from './ExternalLinksPanel'

interface Props {
  projectId: string | null
  context: ProjectContext | null
  onEditFile: (filename: string, content: string) => void
  runsRefreshSignal?: number
}

const FILE_ORDER = ['PROJECT.md', 'STATUS.md', 'DECISIONS.md', 'RESEARCH.md', 'LESSONS.md']

function ContextPanel({ projectId, context, onEditFile, runsRefreshSignal }: Props) {
  if (!projectId || !context) {
    return (
      <aside className="sidebar context-panel">
        <p className="context-empty">Select a project to view its memory and runs.</p>
      </aside>
    )
  }

  return (
    <aside className="sidebar context-panel">
      <div className="context-project-name">{projectId}</div>

      <div className="context-section-label">Project memory</div>
      <div className="context-files">
        {FILE_ORDER.map(filename => {
          const content = context[filename]
          if (content === undefined) return null

          return (
            <details key={filename}>
              <summary>{filename}</summary>
              <div className="context-read">
                <pre className="context-content">{content || '(empty)'}</pre>
                <button className="btn-edit" onClick={() => onEditFile(filename, content)}>
                  Edit
                </button>
              </div>
            </details>
          )
        })}
      </div>

      <div className="context-section-label">Integrations</div>
      <IntegrationsPanel projectId={projectId} refreshSignal={runsRefreshSignal} />

      <div className="context-section-label">Workspace</div>
      <EnvRegistryPanel projectId={projectId} />
      <ExternalLinksPanel projectId={projectId} refreshSignal={runsRefreshSignal} />

      <RunsSection projectId={projectId} refreshSignal={runsRefreshSignal} />
    </aside>
  )
}

export default ContextPanel
