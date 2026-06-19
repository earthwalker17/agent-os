import { useState } from 'react';
import { Task, Workstream } from '../types';

interface WorkstreamBoardProps {
  tasks: Task[];
  workstreams: Workstream[];
}

function WorkstreamBoard({ tasks, workstreams }: WorkstreamBoardProps) {
  const [selectedWorkstream, setSelectedWorkstream] = useState<string>('all');
  const [selectedStatus, setSelectedStatus] = useState<string>('all');

  const filteredTasks = tasks.filter((task) => {
    if (selectedWorkstream !== 'all' && task.workstream !== selectedWorkstream) return false;
    if (selectedStatus !== 'all' && task.status !== selectedStatus) return false;
    return true;
  });

  const statusColors: Record<string, string> = {
    'todo': 'bg-gray-500/20 text-gray-400 border-gray-500/30',
    'in-progress': 'bg-blue-500/20 text-blue-400 border-blue-500/30',
    'completed': 'bg-green-500/20 text-green-400 border-green-500/30',
    'blocked': 'bg-red-500/20 text-red-400 border-red-500/30',
  };

  const priorityColors: Record<string, string> = {
    'critical': 'text-red-400',
    'high': 'text-orange-400',
    'medium': 'text-yellow-400',
    'low': 'text-green-400',
  };

  return (
    <div className="space-y-6">
      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="flex-1">
          <label className="block text-xs text-gray-400 mb-2 font-mono">WORKSTREAM</label>
          <select
            value={selectedWorkstream}
            onChange={(e) => setSelectedWorkstream(e.target.value)}
            className="w-full px-4 py-2 bg-black/50 border border-cyan-500/30 rounded-lg text-white text-sm focus:outline-none focus:border-cyan-500/60 hover:border-cyan-500/50 transition-colors"
          >
            <option value="all">All Workstreams</option>
            {workstreams.map((ws) => (
              <option key={ws.id} value={ws.id}>
                {ws.name}
              </option>
            ))}
          </select>
        </div>
        <div className="flex-1">
          <label className="block text-xs text-gray-400 mb-2 font-mono">STATUS</label>
          <select
            value={selectedStatus}
            onChange={(e) => setSelectedStatus(e.target.value)}
            className="w-full px-4 py-2 bg-black/50 border border-cyan-500/30 rounded-lg text-white text-sm focus:outline-none focus:border-cyan-500/60 hover:border-cyan-500/50 transition-colors"
          >
            <option value="all">All Statuses</option>
            <option value="todo">To Do</option>
            <option value="in-progress">In Progress</option>
            <option value="completed">Completed</option>
            <option value="blocked">Blocked</option>
          </select>
        </div>
      </div>

      {/* Task Count */}
      <div className="flex items-center justify-between px-4 py-2 bg-cyan-500/10 border border-cyan-500/20 rounded-lg">
        <span className="text-sm text-gray-400 font-mono">
          Showing {filteredTasks.length} of {tasks.length} tasks
        </span>
        <div className="flex items-center gap-2">
          {['todo', 'in-progress', 'completed', 'blocked'].map((status) => {
            const count = filteredTasks.filter((t) => t.status === status).length;
            return (
              <div key={status} className="flex items-center gap-1 text-xs">
                <div className={`w-2 h-2 rounded-full ${
                  status === 'completed' ? 'bg-green-400' :
                  status === 'in-progress' ? 'bg-blue-400' :
                  status === 'blocked' ? 'bg-red-400' : 'bg-gray-400'
                }`}></div>
                <span className="text-gray-500">{count}</span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Task Cards */}
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
        {filteredTasks.length === 0 ? (
          <div className="col-span-full flex flex-col items-center justify-center py-12 text-gray-500">
            <svg className="w-16 h-16 mb-4 opacity-50" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
            </svg>
            <p className="text-lg font-semibold mb-1">No tasks found</p>
            <p className="text-sm">Try adjusting your filters</p>
          </div>
        ) : (
          filteredTasks.map((task) => {
            const workstream = workstreams.find((ws) => ws.id === task.workstream);
            const statusClass = statusColors[task.status] || statusColors['todo'];
            const priorityClass = priorityColors[task.priority] || priorityColors['medium'];

            return (
              <div
                key={task.id}
                className="bg-slate-800/50 rounded-lg p-4 border border-slate-700 hover:border-cyan-500/50 transition-all hover:shadow-lg hover:shadow-cyan-500/10"
              >
                {/* Header */}
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-start space-x-2 flex-1">
                    {workstream && (
                      <div
                        className="w-1 h-full rounded-full flex-shrink-0 mt-1"
                        style={{ backgroundColor: workstream.color }}
                      ></div>
                    )}
                    <div className="flex-1 min-w-0">
                      <h4 className="text-white font-semibold text-sm mb-1 truncate">
                        {task.title}
                      </h4>
                      <p className="text-xs text-gray-400 line-clamp-2">
                        {task.description}
                      </p>
                    </div>
                  </div>
                  <div className="flex-shrink-0 ml-2">
                    <span className={`text-xs font-bold ${priorityClass}`}>!</span>
                  </div>
                </div>

                {/* Status Badge */}
                <div className="mb-3">
                  <span className={`inline-block px-2 py-1 rounded text-xs font-medium border ${statusClass}`}>
                    {task.status.toUpperCase().replace('-', ' ')}
                  </span>
                </div>

                {/* Meta Info */}
                <div className="space-y-2 text-xs">
                  <div className="flex items-center justify-between text-gray-400">
                    <span className="flex items-center space-x-1">
                      <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                      </svg>
                      <span className="truncate">{task.assignee}</span>
                    </span>
                    {task.estimatedHours && (
                      <span className="flex items-center space-x-1">
                        <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                        </svg>
                        <span>{task.estimatedHours}h</span>
                      </span>
                    )}
                  </div>

                  <div className="flex items-center space-x-1 text-gray-400">
                    <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                    </svg>
                    <span>
                      {new Date(task.dueDate).toLocaleDateString('en-US', {
                        month: 'short',
                        day: 'numeric',
                      })}
                    </span>
                  </div>

                  {task.dependencies.length > 0 && (
                    <div className="flex items-center space-x-1 text-yellow-400">
                      <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
                      </svg>
                      <span>{task.dependencies.length} dependencies</span>
                    </div>
                  )}

                  {task.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {task.tags.slice(0, 3).map((tag, index) => (
                        <span
                          key={index}
                          className="px-2 py-0.5 bg-cyan-500/20 text-cyan-400 rounded text-xs border border-cyan-500/30"
                        >
                          {tag}
                        </span>
                      ))}
                      {task.tags.length > 3 && (
                        <span className="px-2 py-0.5 bg-slate-700 text-gray-400 rounded text-xs">
                          +{task.tags.length - 3}
                        </span>
                      )}
                    </div>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

export default WorkstreamBoard;
