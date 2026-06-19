import { Task } from '../types';

interface DependencyMapProps {
  tasks: Task[];
}

function DependencyMap({ tasks }: DependencyMapProps) {
  // Group tasks by workstream for better visualization
  const tasksWithDeps = tasks.filter(t => t.dependencies && t.dependencies.length > 0);
  
  return (
    <div className="bg-gradient-to-br from-gray-800/30 to-gray-900/30 backdrop-blur-sm border border-cyan-500/20 rounded-lg p-4 sm:p-6 min-h-[400px]">
      <div className="space-y-4">
        {tasksWithDeps.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-gray-500">
            <svg className="w-16 h-16 mb-4 opacity-50" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <p className="text-sm">No task dependencies defined</p>
          </div>
        ) : (
          <div className="space-y-3">
            {tasksWithDeps.map((task) => (
              <div
                key={task.id}
                className="bg-black/30 border border-cyan-500/20 rounded-lg p-4 hover:border-cyan-500/40 transition-all duration-300 group"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-xs font-mono text-cyan-400">{task.id}</span>
                      <span
                        className={`px-2 py-0.5 text-xs rounded-full ${
                          task.status === 'completed'
                            ? 'bg-green-500/20 text-green-400'
                            : task.status === 'in-progress'
                            ? 'bg-blue-500/20 text-blue-400'
                            : task.status === 'blocked'
                            ? 'bg-red-500/20 text-red-400'
                            : 'bg-gray-500/20 text-gray-400'
                        }`}
                      >
                        {task.status}
                      </span>
                    </div>
                    <h4 className="text-sm font-medium text-gray-200 mb-2 truncate">{task.title}</h4>
                    <div className="flex flex-wrap gap-2">
                      {task.dependencies?.map((depId) => {
                        const depTask = tasks.find(t => t.id === depId);
                        return (
                          <div
                            key={depId}
                            className="flex items-center gap-1.5 px-2 py-1 bg-purple-500/10 border border-purple-500/30 rounded text-xs group-hover:bg-purple-500/20 transition-colors"
                          >
                            <svg className="w-3 h-3 text-purple-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7l5 5m0 0l-5 5m5-5H6" />
                            </svg>
                            <span className="text-purple-300 font-mono">{depId}</span>
                            {depTask && (
                              <span className="text-gray-500">• {depTask.title.slice(0, 20)}{depTask.title.length > 20 ? '...' : ''}</span>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      
      {/* Summary Stats */}
      <div className="mt-6 pt-4 border-t border-cyan-500/20 grid grid-cols-2 sm:grid-cols-3 gap-4">
        <div className="text-center">
          <div className="text-2xl font-bold text-cyan-400">{tasksWithDeps.length}</div>
          <div className="text-xs text-gray-500 font-mono">Dependent Tasks</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-purple-400">
            {tasksWithDeps.reduce((sum, t) => sum + (t.dependencies?.length || 0), 0)}
          </div>
          <div className="text-xs text-gray-500 font-mono">Total Links</div>
        </div>
        <div className="text-center col-span-2 sm:col-span-1">
          <div className="text-2xl font-bold text-green-400">
            {tasksWithDeps.filter(t => t.status === 'completed').length}
          </div>
          <div className="text-xs text-gray-500 font-mono">Resolved</div>
        </div>
      </div>
    </div>
  );
}

export default DependencyMap;
