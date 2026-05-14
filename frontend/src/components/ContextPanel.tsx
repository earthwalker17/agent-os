import type { ProjectContext } from '../types'
import RunsSection from './RunsSection'

interface Props {
  projectId: string | null
  context: ProjectContext | null
  onEditFile: (filename: string, content: string) => void
  runsRefreshSignal?: number
}

const FILE_ORDER = ['PROJECT.md', 'STATUS.md', 'TASK_QUEUE.md', 'DECISIONS.md', 'RESEARCH.md']

function ContextPanel({ projectId, context, onEditFile, runsRefreshSignal }: Props) {
  if (!projectId || !context) {
    return (
      <aside className="sidebar context-panel">
        <h2>Context</h2>
        <p className="context-empty">Select a project to view context</p>
      </aside>
    )
  }

  return (
    <aside className="sidebar context-panel">
      <h2>Context: {projectId}</h2>
      <div className="context-files">
        {FILE_ORDER.map(filename => {
          const content = context[filename]
          if (content === undefined) return null

          return (
            <details key={filename} open={filename === 'STATUS.md'}>
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

      <RunsSection projectId={projectId} refreshSignal={runsRefreshSignal} />
    </aside>
  )
}

export default ContextPanel
