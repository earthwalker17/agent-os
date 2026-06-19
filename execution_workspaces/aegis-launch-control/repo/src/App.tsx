import { useState, useEffect } from 'react';
import { LaunchMission, Task, Risk, Workstream, Scenario } from './types';
import { missions as staticMissions, tasks as staticTasks, risks as staticRisks, workstreams as staticWorkstreams, milestones, scenarios as staticScenarios } from './data/seedData';
import { api } from './services/api';
import MissionOverview from './components/MissionOverview';
import WorkstreamBoard from './components/WorkstreamBoard';
import DependencyMap from './components/DependencyMap';
import { RiskRadar } from './components/RiskRadar';
import LaunchTimeline from './components/LaunchTimeline';
import ScenarioSimulator from './components/ScenarioSimulator';
import Loading from './components/Loading';

type Tab = 'overview' | 'workstream' | 'analytics' | 'timeline' | 'simulator';

function App() {
  const [activeTab, setActiveTab] = useState<Tab>('overview');
  const [missions, setMissions] = useState<LaunchMission[]>(staticMissions);
  const [tasks, setTasks] = useState<Task[]>(staticTasks);
  const [risks, setRisks] = useState<Risk[]>(staticRisks);
  const [workstreams, setWorkstreams] = useState<Workstream[]>(staticWorkstreams);
  const [scenarios, setScenarios] = useState<Scenario[]>(staticScenarios);
  const [loading, setLoading] = useState(true);
  const [apiStatus, setApiStatus] = useState<'online' | 'offline'>('offline');

  const currentMission = missions[0];

  // Load data from API on mount
  useEffect(() => {
    const loadData = async () => {
      try {
        setLoading(true);
        
        // Check API health
        const health = await api.healthCheck();
        setApiStatus(health.status === 'ok' ? 'online' : 'offline');

        // Load all data
        const [missionsData, tasksData, risksData, workstreamsData, scenariosData] = await Promise.all([
          api.getMissions(),
          api.getTasks(),
          api.getRisks(),
          api.getWorkstreams(),
          api.getScenarios(),
        ]);

        setMissions(missionsData);
        setTasks(tasksData);
        setRisks(risksData);
        setWorkstreams(workstreamsData);
        setScenarios(scenariosData);
      } catch (error) {
        console.error('Failed to load data:', error);
        setApiStatus('offline');
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, []);

  const tabs: { id: Tab; label: string; icon: string }[] = [
    { id: 'overview', label: 'Overview', icon: '📊' },
    { id: 'workstream', label: 'Workstream', icon: '📋' },
    { id: 'analytics', label: 'Analytics', icon: '📈' },
    { id: 'timeline', label: 'Timeline', icon: '📅' },
    { id: 'simulator', label: 'Simulator', icon: '🎮' },
  ];

  // Calculate baseline readiness for simulator
  const calculateReadiness = (): number => {
    const completedTasks = tasks.filter(t => t.status === 'completed').length;
    const totalTasks = tasks.length;
    const taskProgress = (completedTasks / totalTasks) * 40;

    const criticalRisks = risks.filter(r => r.severity === 'critical' && r.status === 'identified').length;
    const riskPenalty = Math.min(criticalRisks * 5, 30);

    const budgetHealth = (currentMission.budget - currentMission.budgetUsed) / currentMission.budget;
    const budgetScore = budgetHealth > 0.1 ? 30 : budgetHealth * 300;

    const overallProgress = currentMission.progress * 0.3;

    return Math.max(0, Math.min(100, Math.floor(taskProgress + budgetScore + overallProgress - riskPenalty)));
  };

  const baselineReadiness = calculateReadiness();
  const baselineRisk = risks.filter(r => r.severity === 'critical' && r.status === 'identified').length > 2 ? 'high' : 
                       risks.filter(r => r.severity === 'high' && r.status === 'identified').length > 3 ? 'medium' : 'low';

  if (loading) {
    return <Loading />;
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-900 via-black to-gray-900 text-white">
      {/* Header */}
      <header className="border-b border-cyan-500/30 bg-black/50 backdrop-blur-sm sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
            <div className="flex items-center space-x-3 sm:space-x-4">
              <div className="w-10 h-10 bg-gradient-to-br from-cyan-500 to-blue-600 rounded-lg flex items-center justify-center shadow-lg shadow-cyan-500/50">
                <svg className="w-6 h-6 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                </svg>
              </div>
              <div>
                <h1 className="text-xl sm:text-2xl font-bold bg-gradient-to-r from-cyan-400 to-blue-400 bg-clip-text text-transparent">
                  Aegis Launch Control
                </h1>
                <p className="text-xs sm:text-sm text-gray-400 font-mono">{currentMission.code}</p>
              </div>
            </div>
            <div className="flex items-center space-x-4">
              <div className="text-right">
                <div className="text-xs text-gray-400">Mission</div>
                <div className="text-xs sm:text-sm font-semibold text-cyan-400 truncate max-w-[150px] sm:max-w-none">{currentMission.name}</div>
              </div>
              <div className="flex items-center space-x-2">
                <div className={`w-3 h-3 rounded-full shadow-lg ${
                  apiStatus === 'online'
                    ? 'bg-green-500 animate-pulse shadow-green-500/50'
                    : 'bg-yellow-500 shadow-yellow-500/50'
                }`}></div>
                <span className="text-xs text-gray-400 hidden sm:inline">
                  {apiStatus === 'online' ? 'API Online' : 'Static Mode'}
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Navigation Tabs */}
        <div className="border-t border-cyan-500/20 bg-black/30">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <nav className="flex space-x-1 overflow-x-auto scrollbar-hide" aria-label="Tabs">
              {tabs.map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`
                    px-3 py-3 sm:px-4 text-xs sm:text-sm font-medium whitespace-nowrap
                    border-b-2 transition-all duration-300 hover:bg-cyan-500/10
                    ${
                      activeTab === tab.id
                        ? 'border-cyan-400 text-cyan-400 bg-cyan-500/10'
                        : 'border-transparent text-gray-400 hover:text-cyan-300'
                    }
                  `}
                >
                  <span className="mr-1.5">{tab.icon}</span>
                  <span className="hidden sm:inline">{tab.label}</span>
                </button>
              ))}
            </nav>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
        {activeTab === 'overview' && (
          <MissionOverview
            mission={currentMission}
            tasks={tasks}
            risks={risks}
            workstreams={workstreams}
          />
        )}
        {activeTab === 'workstream' && (
          <WorkstreamBoard tasks={tasks} workstreams={workstreams} />
        )}
        {activeTab === 'analytics' && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <DependencyMap tasks={tasks} />
            <RiskRadar risks={risks} />
          </div>
        )}
        {activeTab === 'timeline' && (
          <LaunchTimeline milestones={milestones} />
        )}
        {activeTab === 'simulator' && (
          <ScenarioSimulator 
            scenarios={scenarios} 
            baselineReadiness={baselineReadiness}
            baselineRisk={baselineRisk}
          />
        )}
      </main>

      {/* Footer */}
      <footer className="border-t border-cyan-500/20 bg-black/30 mt-12">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
          <div className="flex flex-col sm:flex-row justify-between items-center space-y-4 sm:space-y-0">
            <div className="text-xs sm:text-sm text-gray-400">
              © 2024 Aegis Launch Control. Mission-critical systems online.
            </div>
            <div className="flex items-center space-x-6 text-xs sm:text-sm text-gray-400">
              <span>Launch T-{Math.ceil((new Date(currentMission.launchDate).getTime() - Date.now()) / (1000 * 60 * 60 * 24))} days</span>
              <span className="hidden sm:inline">•</span>
              <span className="hidden sm:inline">Status: {currentMission.status.toUpperCase()}</span>
            </div>
          </div>
        </div>
      </footer>
    </div>
  );
}

export default App;
