import { useState, useEffect } from 'react';
import { LaunchMission, Task, Risk, Workstream } from '../types';

interface MissionOverviewProps {
  mission: LaunchMission;
  tasks: Task[];
  risks: Risk[];
  workstreams: Workstream[];
}

export default function MissionOverview({ mission, tasks, risks, workstreams }: MissionOverviewProps) {
  const [timeRemaining, setTimeRemaining] = useState<string>('');
  const [animatedReadiness, setAnimatedReadiness] = useState(0);

  // Calculate countdown
  useEffect(() => {
    const updateCountdown = () => {
      const now = new Date().getTime();
      const launchTime = new Date(mission.launchDate).getTime();
      const diff = launchTime - now;

      if (diff <= 0) {
        setTimeRemaining('T-MINUS 00:00:00:00');
        return;
      }

      const days = Math.floor(diff / (1000 * 60 * 60 * 24));
      const hours = Math.floor((diff % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
      const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
      const seconds = Math.floor((diff % (1000 * 60)) / 1000);

      setTimeRemaining(
        `T-MINUS ${String(days).padStart(2, '0')}:${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
      );
    };

    updateCountdown();
    const interval = setInterval(updateCountdown, 1000);
    return () => clearInterval(interval);
  }, [mission.launchDate]);

  // Animate readiness score on mount
  useEffect(() => {
    const targetReadiness = calculateReadiness();
    let current = 0;
    const increment = targetReadiness / 50; // 50 steps
    const interval = setInterval(() => {
      current += increment;
      if (current >= targetReadiness) {
        setAnimatedReadiness(targetReadiness);
        clearInterval(interval);
      } else {
        setAnimatedReadiness(Math.floor(current));
      }
    }, 20);
    return () => clearInterval(interval);
  }, []);

  // Calculate readiness score (0-100%)
  const calculateReadiness = (): number => {
    const completedTasks = tasks.filter(t => t.status === 'completed').length;
    const totalTasks = tasks.length;
    const taskProgress = (completedTasks / totalTasks) * 40; // 40% weight

    const criticalRisks = risks.filter(r => r.severity === 'critical' && r.status === 'identified').length;
    const riskPenalty = Math.min(criticalRisks * 5, 30); // Max 30% penalty

    const budgetHealth = (mission.budget - mission.budgetUsed) / mission.budget;
    const budgetScore = budgetHealth > 0.1 ? 30 : budgetHealth * 300; // 30% weight

    const overallProgress = mission.progress * 0.3; // 30% weight from overall progress

    return Math.max(0, Math.min(100, Math.floor(taskProgress + budgetScore + overallProgress - riskPenalty)));
  };

  // Calculate active blockers
  const activeBlockers = tasks.filter(t => t.status === 'blocked').length;

  // Calculate velocity (tasks completed per week)
  const calculateVelocity = (): number => {
    const completedTasks = tasks.filter(t => t.status === 'completed' && t.completedDate);
    if (completedTasks.length === 0) return 0;

    const now = new Date();
    const oneWeekAgo = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
    const recentCompletions = completedTasks.filter(t => {
      const completedDate = new Date(t.completedDate!);
      return completedDate >= oneWeekAgo;
    });

    return recentCompletions.length;
  };

  const velocity = calculateVelocity();

  // Determine deployment status
  const getDeploymentStatus = (): { label: string; color: string; pulse: boolean } => {
    if (mission.progress >= 90) {
      return { label: 'GO FOR LAUNCH', color: 'text-green-400 border-green-400', pulse: true };
    } else if (mission.progress >= 70) {
      return { label: 'ON TRACK', color: 'text-blue-400 border-blue-400', pulse: false };
    } else if (mission.progress >= 50) {
      return { label: 'CAUTION', color: 'text-yellow-400 border-yellow-400', pulse: false };
    } else {
      return { label: 'HOLD', color: 'text-red-400 border-red-400', pulse: false };
    }
  };

  const deploymentStatus = getDeploymentStatus();

  // Get risk summary
  const riskSummary = {
    critical: risks.filter(r => r.severity === 'critical' && r.status === 'identified').length,
    high: risks.filter(r => r.severity === 'high' && r.status === 'identified').length,
    total: risks.filter(r => r.status === 'identified').length,
    mitigated: risks.filter(r => r.status === 'mitigated').length,
  };

  return (
    <div className="space-y-6">
      {/* Hero Status Card */}
      <div className="bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 rounded-xl p-6 sm:p-8 border border-cyan-500/30 shadow-2xl shadow-cyan-500/20">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Launch Countdown */}
          <div className="lg:col-span-2">
            <div className="text-xs text-gray-400 font-mono mb-2">MISSION COUNTDOWN</div>
            <div className="text-4xl sm:text-5xl font-bold text-cyan-400 font-mono tracking-wider mb-4">
              {timeRemaining}
            </div>
            <div className="flex items-center space-x-4">
              <div className={`px-4 py-2 rounded-lg border-2 font-mono text-sm ${
                deploymentStatus.color
              } ${deploymentStatus.pulse ? 'animate-pulse' : ''}`}>
                {deploymentStatus.label}
              </div>
              <div className="flex items-center space-x-2 text-gray-400 text-sm">
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span>Launch: {new Date(mission.launchDate).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}</span>
              </div>
            </div>
          </div>

          {/* Readiness Score */}
          <div className="flex flex-col items-center justify-center bg-slate-900/50 rounded-lg p-6 border border-cyan-500/20">
            <div className="text-xs text-gray-400 font-mono mb-2">READINESS SCORE</div>
            <div className="relative w-32 h-32">
              <svg className="transform -rotate-90 w-32 h-32">
                <circle
                  cx="64"
                  cy="64"
                  r="56"
                  stroke="currentColor"
                  strokeWidth="8"
                  fill="none"
                  className="text-slate-700"
                />
                <circle
                  cx="64"
                  cy="64"
                  r="56"
                  stroke="currentColor"
                  strokeWidth="8"
                  fill="none"
                  strokeDasharray={`${2 * Math.PI * 56}`}
                  strokeDashoffset={`${2 * Math.PI * 56 * (1 - animatedReadiness / 100)}`}
                  className={`${
                    animatedReadiness >= 80 ? 'text-green-400' :
                    animatedReadiness >= 60 ? 'text-blue-400' :
                    animatedReadiness >= 40 ? 'text-yellow-400' : 'text-red-400'
                  } transition-all duration-500`}
                  strokeLinecap="round"
                />
              </svg>
              <div className="absolute inset-0 flex items-center justify-center">
                <span className="text-3xl font-bold text-white">{animatedReadiness}%</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Key Metrics Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Progress */}
        <div className="bg-slate-800/50 rounded-lg p-4 border border-cyan-500/20 hover:border-cyan-500/40 transition-colors">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-gray-400 font-mono">PROGRESS</span>
            <svg className="w-5 h-5 text-cyan-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
            </svg>
          </div>
          <div className="text-2xl font-bold text-white mb-1">{mission.progress}%</div>
          <div className="w-full bg-slate-700 rounded-full h-2">
            <div
              className="bg-gradient-to-r from-cyan-500 to-blue-500 h-2 rounded-full transition-all duration-500"
              style={{ width: `${mission.progress}%` }}
            ></div>
          </div>
        </div>

        {/* Budget */}
        <div className="bg-slate-800/50 rounded-lg p-4 border border-cyan-500/20 hover:border-cyan-500/40 transition-colors">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-gray-400 font-mono">BUDGET</span>
            <svg className="w-5 h-5 text-green-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </div>
          <div className="text-2xl font-bold text-white mb-1">
            ${(mission.budgetUsed / 1000000).toFixed(1)}M
          </div>
          <div className="text-xs text-gray-400">
            of ${(mission.budget / 1000000).toFixed(1)}M ({Math.round((mission.budgetUsed / mission.budget) * 100)}%)
          </div>
        </div>

        {/* Tasks */}
        <div className="bg-slate-800/50 rounded-lg p-4 border border-cyan-500/20 hover:border-cyan-500/40 transition-colors">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-gray-400 font-mono">TASKS</span>
            <svg className="w-5 h-5 text-blue-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
            </svg>
          </div>
          <div className="text-2xl font-bold text-white mb-1">
            {tasks.filter(t => t.status === 'completed').length}/{tasks.length}
          </div>
          <div className="text-xs text-gray-400">Velocity: {velocity}/week</div>
        </div>

        {/* Risks */}
        <div className="bg-slate-800/50 rounded-lg p-4 border border-cyan-500/20 hover:border-cyan-500/40 transition-colors">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-gray-400 font-mono">RISKS</span>
            <svg className="w-5 h-5 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
          </div>
          <div className="text-2xl font-bold text-white mb-1">
            {riskSummary.critical + riskSummary.high}
          </div>
          <div className="text-xs text-gray-400">
            {riskSummary.critical} critical, {riskSummary.mitigated} mitigated
          </div>
        </div>
      </div>

      {/* Workstream Overview */}
      <div className="bg-slate-800/50 rounded-lg p-6 border border-cyan-500/20">
        <h3 className="text-lg font-bold text-white mb-4 flex items-center">
          <svg className="w-5 h-5 mr-2 text-cyan-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
          </svg>
          Workstream Status
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {workstreams.map((ws) => (
            <div key={ws.id} className="bg-slate-900/50 rounded-lg p-4 border border-slate-700 hover:border-cyan-500/50 transition-colors">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center space-x-2">
                  <div className={`w-3 h-3 rounded-full`} style={{ backgroundColor: ws.color }}></div>
                  <span className="font-semibold text-white">{ws.name}</span>
                </div>
                <span className="text-sm font-mono text-gray-400">{ws.progress}%</span>
              </div>
              <div className="w-full bg-slate-700 rounded-full h-2 mb-2">
                <div
                  className="h-2 rounded-full transition-all duration-500"
                  style={{ width: `${ws.progress}%`, backgroundColor: ws.color }}
                ></div>
              </div>
              <div className="flex items-center justify-between text-xs text-gray-400">
                <span>{ws.completedTaskCount}/{ws.taskCount} tasks</span>
                <span>Lead: {ws.lead}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Active Blockers Alert */}
      {activeBlockers > 0 && (
        <div className="bg-red-900/20 border border-red-500/50 rounded-lg p-4">
          <div className="flex items-start space-x-3">
            <svg className="w-6 h-6 text-red-400 flex-shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
            <div className="flex-1">
              <h4 className="text-red-400 font-semibold mb-1">Active Blockers Detected</h4>
              <p className="text-sm text-gray-300">
                {activeBlockers} task{activeBlockers !== 1 ? 's are' : ' is'} currently blocked and require immediate attention.
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
