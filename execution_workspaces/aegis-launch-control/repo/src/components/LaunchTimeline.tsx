import { Milestone } from '../types';

interface LaunchTimelineProps {
  milestones: Milestone[];
}

const LaunchTimeline = ({ milestones }: LaunchTimelineProps) => {
  const getStatusColor = (status: Milestone['status']) => {
    switch (status) {
      case 'completed':
        return 'bg-green-500 text-green-100 border-green-400';
      case 'at-risk':
        return 'bg-yellow-500 text-yellow-100 border-yellow-400';
      case 'missed':
        return 'bg-red-500 text-red-100 border-red-400';
      default:
        return 'bg-blue-500 text-blue-100 border-blue-400';
    }
  };

  const getStatusIcon = (status: Milestone['status']) => {
    switch (status) {
      case 'completed':
        return (
          <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
          </svg>
        );
      case 'at-risk':
        return (
          <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
          </svg>
        );
      case 'missed':
        return (
          <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
          </svg>
        );
      default:
        return (
          <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clipRule="evenodd" />
          </svg>
        );
    }
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric'
    });
  };

  // Sort milestones by date
  const sortedMilestones = [...milestones].sort(
    (a, b) => new Date(a.date).getTime() - new Date(b.date).getTime()
  );

  return (
    <div className="bg-gray-800/30 rounded-lg border border-gray-700 p-6">
      <div className="space-y-4">
        {sortedMilestones.map((milestone, index) => {
          const isLast = index === sortedMilestones.length - 1;
          const statusColor = getStatusColor(milestone.status);

          return (
            <div key={milestone.id} className="relative">
              {/* Timeline connector line */}
              {!isLast && (
                <div className="absolute left-6 top-12 bottom-0 w-0.5 bg-gradient-to-b from-cyan-500/50 to-transparent" />
              )}

              <div className="flex items-start space-x-4">
                {/* Status Icon */}
                <div
                  className={`flex-shrink-0 w-12 h-12 rounded-full flex items-center justify-center border-2 ${statusColor} ${milestone.criticalPath ? 'ring-2 ring-cyan-400 ring-offset-2 ring-offset-gray-900' : ''}`}
                >
                  {getStatusIcon(milestone.status)}
                </div>

                {/* Milestone Content */}
                <div className="flex-1 bg-gray-900/50 rounded-lg border border-gray-700 p-4 hover:border-cyan-500/50 transition-colors">
                  <div className="flex items-start justify-between mb-2">
                    <div className="flex-1">
                      <div className="flex items-center space-x-2 mb-1">
                        <h4 className="text-lg font-semibold text-white">
                          {milestone.name}
                        </h4>
                        {milestone.criticalPath && (
                          <span className="px-2 py-0.5 bg-cyan-500/20 border border-cyan-500/50 rounded text-xs font-mono text-cyan-400">
                            CRITICAL PATH
                          </span>
                        )}
                      </div>
                      <p className="text-sm text-gray-400">
                        {milestone.description}
                      </p>
                    </div>
                    <div className="text-right ml-4">
                      <div className="text-sm font-mono text-cyan-400">
                        {formatDate(milestone.date)}
                      </div>
                      <div className={`text-xs font-semibold uppercase mt-1 ${statusColor.split(' ')[1]}`}>
                        {milestone.status.replace('-', ' ')}
                      </div>
                    </div>
                  </div>

                  {/* Workstreams involved */}
                  {milestone.workstreams.length > 0 && (
                    <div className="flex items-center space-x-2 mt-3 pt-3 border-t border-gray-700">
                      <span className="text-xs text-gray-500 font-mono">WORKSTREAMS:</span>
                      <div className="flex flex-wrap gap-1">
                        {milestone.workstreams.map((ws) => (
                          <span
                            key={ws}
                            className="px-2 py-0.5 bg-gray-800 rounded text-xs text-gray-300 font-mono"
                          >
                            {ws}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Progress Summary */}
      <div className="mt-6 pt-6 border-t border-gray-700">
        <div className="grid grid-cols-4 gap-4 text-center">
          <div>
            <div className="text-2xl font-bold text-green-400">
              {sortedMilestones.filter(m => m.status === 'completed').length}
            </div>
            <div className="text-xs text-gray-500 font-mono mt-1">COMPLETED</div>
          </div>
          <div>
            <div className="text-2xl font-bold text-blue-400">
              {sortedMilestones.filter(m => m.status === 'upcoming').length}
            </div>
            <div className="text-xs text-gray-500 font-mono mt-1">UPCOMING</div>
          </div>
          <div>
            <div className="text-2xl font-bold text-yellow-400">
              {sortedMilestones.filter(m => m.status === 'at-risk').length}
            </div>
            <div className="text-xs text-gray-500 font-mono mt-1">AT RISK</div>
          </div>
          <div>
            <div className="text-2xl font-bold text-cyan-400">
              {sortedMilestones.filter(m => m.criticalPath).length}
            </div>
            <div className="text-xs text-gray-500 font-mono mt-1">CRITICAL</div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default LaunchTimeline;
