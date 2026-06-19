import { useState } from 'react';
import { Scenario } from '../types';

interface ScenarioSimulatorProps {
  scenarios: Scenario[];
  baselineReadiness: number;
  baselineRisk: string;
}

const ScenarioSimulator = ({
  scenarios,
  baselineReadiness,
  baselineRisk
}: ScenarioSimulatorProps) => {
  const [selectedScenarioId, setSelectedScenarioId] = useState<string>(
    scenarios.find(s => s.type === 'nominal')?.id || scenarios[0]?.id || ''
  );

  const selectedScenario = scenarios.find(s => s.id === selectedScenarioId);

  if (!selectedScenario) {
    return (
      <div className="bg-gray-800/30 rounded-lg border border-gray-700 p-6">
        <p className="text-gray-500">No scenarios available</p>
      </div>
    );
  }

  // Calculate scenario impacts
  const readinessScore = Math.max(
    0,
    Math.min(100, baselineReadiness + (selectedScenario.impacts.budget / 1000000))
  );
  const timelineDelta = selectedScenario.impacts.timeline;
  const riskLevel = selectedScenario.impacts.risk;

  const getScenarioTypeColor = (type: Scenario['type']) => {
    switch (type) {
      case 'optimistic':
        return 'bg-green-500/20 border-green-500/50 text-green-400';
      case 'pessimistic':
        return 'bg-red-500/20 border-red-500/50 text-red-400';
      case 'contingency':
        return 'bg-yellow-500/20 border-yellow-500/50 text-yellow-400';
      default:
        return 'bg-blue-500/20 border-blue-500/50 text-blue-400';
    }
  };

  const getRiskColor = (risk: string) => {
    switch (risk.toLowerCase()) {
      case 'low':
        return 'text-green-400';
      case 'medium':
        return 'text-yellow-400';
      case 'high':
        return 'text-red-400';
      default:
        return 'text-gray-400';
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

  return (
    <div className="bg-gray-800/30 rounded-lg border border-gray-700 p-6">
      {/* Scenario Selection */}
      <div className="mb-6">
        <label className="block text-sm font-mono text-gray-400 mb-2">
          SELECT SCENARIO
        </label>
        <select
          value={selectedScenarioId}
          onChange={(e) => setSelectedScenarioId(e.target.value)}
          className="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-3 text-white font-mono focus:outline-none focus:border-cyan-500 transition-colors"
        >
          {scenarios.map((scenario) => (
            <option key={scenario.id} value={scenario.id}>
              {scenario.name} ({scenario.type.toUpperCase()})
            </option>
          ))}
        </select>
      </div>

      {/* Scenario Info */}
      <div className="bg-gray-900/50 rounded-lg border border-gray-700 p-4 mb-6">
        <div className="flex items-start justify-between mb-3">
          <div className="flex-1">
            <div className="flex items-center space-x-2 mb-2">
              <h3 className="text-lg font-semibold text-white">
                {selectedScenario.name}
              </h3>
              <span
                className={`px-2 py-1 rounded text-xs font-mono uppercase border ${getScenarioTypeColor(selectedScenario.type)}`}
              >
                {selectedScenario.type}
              </span>
            </div>
            <p className="text-sm text-gray-400">{selectedScenario.description}</p>
          </div>
          <div className="text-right ml-4">
            <div className="text-xs text-gray-500 font-mono">PROBABILITY</div>
            <div className="text-2xl font-bold text-cyan-400">
              {selectedScenario.probability}%
            </div>
          </div>
        </div>

        {/* Assumptions */}
        <div className="mt-4 pt-4 border-t border-gray-700">
          <div className="text-xs text-gray-500 font-mono mb-2">ASSUMPTIONS</div>
          <ul className="space-y-1">
            {selectedScenario.assumptions.map((assumption, index) => (
              <li key={index} className="text-sm text-gray-400 flex items-start">
                <span className="text-cyan-500 mr-2">▸</span>
                <span>{assumption}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      {/* Real-time Impact Display */}
      <div className="space-y-4">
        <h4 className="text-sm font-mono text-gray-400 mb-3">SCENARIO IMPACT ANALYSIS</h4>

        {/* Readiness Score Impact */}
        <div className="bg-gray-900/50 rounded-lg border border-gray-700 p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-mono text-gray-400">READINESS SCORE</span>
            <div className="flex items-center space-x-2">
              <span className="text-xs text-gray-500">{baselineReadiness}%</span>
              <span className="text-cyan-400">→</span>
              <span className="text-lg font-bold text-white">{readinessScore.toFixed(1)}%</span>
              <span
                className={`text-sm font-mono ${
                  readinessScore > baselineReadiness ? 'text-green-400' : readinessScore < baselineReadiness ? 'text-red-400' : 'text-gray-400'
                }`}
              >
                {readinessScore > baselineReadiness ? '↑' : readinessScore < baselineReadiness ? '↓' : '='}
                {Math.abs(readinessScore - baselineReadiness).toFixed(1)}%
              </span>
            </div>
          </div>
          <div className="w-full bg-gray-800 rounded-full h-2">
            <div
              className="h-2 rounded-full bg-gradient-to-r from-cyan-500 to-blue-500 transition-all duration-500"
              style={{ width: `${readinessScore}%` }}
            />
          </div>
        </div>

        {/* Risk Level Impact */}
        <div className="bg-gray-900/50 rounded-lg border border-gray-700 p-4">
          <div className="flex items-center justify-between">
            <span className="text-sm font-mono text-gray-400">RISK LEVEL</span>
            <div className="flex items-center space-x-2">
              <span className={`text-xs ${getRiskColor(baselineRisk)}`}>
                {baselineRisk.toUpperCase()}
              </span>
              <span className="text-cyan-400">→</span>
              <span className={`text-lg font-bold uppercase ${getRiskColor(riskLevel)}`}>
                {riskLevel}
              </span>
            </div>
          </div>
          <div className="mt-3 grid grid-cols-3 gap-2">
            <div
              className={`h-2 rounded ${
                riskLevel === 'low' ? 'bg-green-500' : 'bg-gray-700'
              }`}
            />
            <div
              className={`h-2 rounded ${
                riskLevel === 'medium' ? 'bg-yellow-500' : 'bg-gray-700'
              }`}
            />
            <div
              className={`h-2 rounded ${
                riskLevel === 'high' ? 'bg-red-500' : 'bg-gray-700'
              }`}
            />
          </div>
        </div>

        {/* Timeline Impact */}
        <div className="bg-gray-900/50 rounded-lg border border-gray-700 p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-mono text-gray-400">TIMELINE SHIFT</span>
            <div className="flex items-center space-x-2">
              <span
                className={`text-2xl font-bold ${
                  timelineDelta < 0 ? 'text-green-400' : timelineDelta > 0 ? 'text-red-400' : 'text-gray-400'
                }`}
              >
                {timelineDelta > 0 ? '+' : ''}{timelineDelta}
              </span>
              <span className="text-sm text-gray-500">days</span>
            </div>
          </div>
          <div className="text-xs text-gray-500 font-mono">
            NEW LAUNCH DATE: {formatDate(selectedScenario.launchDate)}
          </div>
        </div>

        {/* Budget Impact */}
        <div className="bg-gray-900/50 rounded-lg border border-gray-700 p-4">
          <div className="flex items-center justify-between">
            <span className="text-sm font-mono text-gray-400">BUDGET DELTA</span>
            <div className="flex items-center space-x-2">
              <span
                className={`text-xl font-bold ${
                  selectedScenario.impacts.budget < 0
                    ? 'text-green-400'
                    : selectedScenario.impacts.budget > 0
                    ? 'text-red-400'
                    : 'text-gray-400'
                }`}
              >
                {selectedScenario.impacts.budget >= 0 ? '+' : ''}
                ${(selectedScenario.impacts.budget / 1000000).toFixed(1)}M
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Scenario Comparison Footer */}
      <div className="mt-6 pt-6 border-t border-gray-700">
        <div className="text-xs text-gray-500 font-mono mb-2">AVAILABLE SCENARIOS</div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {scenarios.map((scenario) => (
            <button
              key={scenario.id}
              onClick={() => setSelectedScenarioId(scenario.id)}
              className={`px-3 py-2 rounded text-xs font-mono border transition-colors ${
                scenario.id === selectedScenarioId
                  ? 'bg-cyan-500/20 border-cyan-500 text-cyan-400'
                  : 'bg-gray-800 border-gray-600 text-gray-400 hover:border-gray-500'
              }`}
            >
              {scenario.type.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
};

export default ScenarioSimulator;
