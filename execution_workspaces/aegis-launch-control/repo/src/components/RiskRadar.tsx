import React, { useState } from 'react';
import { Risk } from '../types';

interface RiskRadarProps {
  risks: Risk[];
}

type RiskCategory = 'technical' | 'schedule' | 'budget' | 'regulatory' | 'external';

interface MatrixCell {
  impact: 'low' | 'high';
  probability: 'low' | 'high';
  risks: Risk[];
}

export const RiskRadar: React.FC<RiskRadarProps> = ({ risks }) => {
  const [selectedRisk, setSelectedRisk] = useState<Risk | null>(null);
  const [viewMode, setViewMode] = useState<'matrix' | 'list'>('matrix');

  // Group risks by category
  const risksByCategory = React.useMemo(() => {
    const categories: Record<RiskCategory, Risk[]> = {
      technical: [],
      schedule: [],
      budget: [],
      regulatory: [],
      external: [],
    };

    risks.forEach((risk) => {
      const category = risk.category as RiskCategory;
      if (categories[category]) {
        categories[category].push(risk);
      }
    });

    return categories;
  }, [risks]);

  // Build 2x2 matrix cells based on impact and probability
  const matrixCells: MatrixCell[] = React.useMemo(() => {
    return [
      {
        impact: 'high',
        probability: 'high',
        risks: risks.filter(
          (r) => r.impact >= 70 && r.probability >= 70
        ),
      },
      {
        impact: 'high',
        probability: 'low',
        risks: risks.filter(
          (r) => r.impact >= 70 && r.probability < 70
        ),
      },
      {
        impact: 'low',
        probability: 'high',
        risks: risks.filter(
          (r) => r.impact < 70 && r.probability >= 70
        ),
      },
      {
        impact: 'low',
        probability: 'low',
        risks: risks.filter(
          (r) => r.impact < 70 && r.probability < 70
        ),
      },
    ];
  }, [risks]);

  const getSeverityColor = (severity: Risk['severity']) => {
    switch (severity) {
      case 'critical':
        return 'bg-red-600';
      case 'high':
        return 'bg-orange-500';
      case 'medium':
        return 'bg-yellow-500';
      case 'low':
        return 'bg-green-500';
      default:
        return 'bg-gray-500';
    }
  };

  const getCategoryIcon = (category: string) => {
    switch (category) {
      case 'technical':
        return '⚙️';
      case 'schedule':
        return '⏱️';
      case 'budget':
        return '💰';
      case 'regulatory':
        return '📋';
      case 'external':
        return '🌐';
      default:
        return '⚠️';
    }
  };

  return (
    <div className="bg-slate-800 rounded-lg p-6">
      <div className="flex justify-between items-center mb-6">
        <h2 className="text-xl font-bold text-white">Risk Radar</h2>
        <div className="flex gap-2">
          <button
            onClick={() => setViewMode('matrix')}
            className={`px-3 py-1 rounded ${
              viewMode === 'matrix'
                ? 'bg-blue-600 text-white'
                : 'bg-slate-700 text-slate-300'
            }`}
          >
            Matrix
          </button>
          <button
            onClick={() => setViewMode('list')}
            className={`px-3 py-1 rounded ${
              viewMode === 'list'
                ? 'bg-blue-600 text-white'
                : 'bg-slate-700 text-slate-300'
            }`}
          >
            List
          </button>
        </div>
      </div>

      {viewMode === 'matrix' ? (
        <div className="grid grid-cols-2 gap-4 mb-6">
          {/* High Impact, High Probability */}
          <div className="bg-red-900/30 border-2 border-red-500 rounded-lg p-4">
            <div className="text-white font-semibold mb-2">Critical Risk</div>
            <div className="text-xs text-slate-400 mb-3">High Impact × High Probability</div>
            <div className="space-y-2">
              {matrixCells[0].risks.length > 0 ? (
                matrixCells[0].risks.slice(0, 3).map((risk) => (
                  <div
                    key={risk.id}
                    className="bg-slate-800/50 p-2 rounded cursor-pointer hover:bg-slate-700/50"
                    onClick={() => setSelectedRisk(risk)}
                  >
                    <div className="flex items-start gap-2">
                      <span className="text-lg">{getCategoryIcon(risk.category)}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-white text-sm font-medium truncate">
                          {risk.title}
                        </div>
                        <div className="text-xs text-slate-400 mt-1 line-clamp-2">
                          {risk.mitigation}
                        </div>
                      </div>
                    </div>
                  </div>
                ))
              ) : (
                <div className="text-slate-500 text-sm italic">No critical risks</div>
              )}
            </div>
          </div>

          {/* High Impact, Low Probability */}
          <div className="bg-orange-900/30 border-2 border-orange-500 rounded-lg p-4">
            <div className="text-white font-semibold mb-2">Monitor Closely</div>
            <div className="text-xs text-slate-400 mb-3">High Impact × Low Probability</div>
            <div className="space-y-2">
              {matrixCells[1].risks.length > 0 ? (
                matrixCells[1].risks.slice(0, 3).map((risk) => (
                  <div
                    key={risk.id}
                    className="bg-slate-800/50 p-2 rounded cursor-pointer hover:bg-slate-700/50"
                    onClick={() => setSelectedRisk(risk)}
                  >
                    <div className="flex items-start gap-2">
                      <span className="text-lg">{getCategoryIcon(risk.category)}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-white text-sm font-medium truncate">
                          {risk.title}
                        </div>
                        <div className="text-xs text-slate-400 mt-1 line-clamp-2">
                          {risk.mitigation}
                        </div>
                      </div>
                    </div>
                  </div>
                ))
              ) : (
                <div className="text-slate-500 text-sm italic">No risks in this quadrant</div>
              )}
            </div>
          </div>

          {/* Low Impact, High Probability */}
          <div className="bg-yellow-900/30 border-2 border-yellow-500 rounded-lg p-4">
            <div className="text-white font-semibold mb-2">Track & Manage</div>
            <div className="text-xs text-slate-400 mb-3">Low Impact × High Probability</div>
            <div className="space-y-2">
              {matrixCells[2].risks.length > 0 ? (
                matrixCells[2].risks.slice(0, 3).map((risk) => (
                  <div
                    key={risk.id}
                    className="bg-slate-800/50 p-2 rounded cursor-pointer hover:bg-slate-700/50"
                    onClick={() => setSelectedRisk(risk)}
                  >
                    <div className="flex items-start gap-2">
                      <span className="text-lg">{getCategoryIcon(risk.category)}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-white text-sm font-medium truncate">
                          {risk.title}
                        </div>
                        <div className="text-xs text-slate-400 mt-1 line-clamp-2">
                          {risk.mitigation}
                        </div>
                      </div>
                    </div>
                  </div>
                ))
              ) : (
                <div className="text-slate-500 text-sm italic">No risks in this quadrant</div>
              )}
            </div>
          </div>

          {/* Low Impact, Low Probability */}
          <div className="bg-green-900/30 border-2 border-green-500 rounded-lg p-4">
            <div className="text-white font-semibold mb-2">Low Priority</div>
            <div className="text-xs text-slate-400 mb-3">Low Impact × Low Probability</div>
            <div className="space-y-2">
              {matrixCells[3].risks.length > 0 ? (
                matrixCells[3].risks.slice(0, 3).map((risk) => (
                  <div
                    key={risk.id}
                    className="bg-slate-800/50 p-2 rounded cursor-pointer hover:bg-slate-700/50"
                    onClick={() => setSelectedRisk(risk)}
                  >
                    <div className="flex items-start gap-2">
                      <span className="text-lg">{getCategoryIcon(risk.category)}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-white text-sm font-medium truncate">
                          {risk.title}
                        </div>
                        <div className="text-xs text-slate-400 mt-1 line-clamp-2">
                          {risk.mitigation}
                        </div>
                      </div>
                    </div>
                  </div>
                ))
              ) : (
                <div className="text-slate-500 text-sm italic">No risks in this quadrant</div>
              )}
            </div>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          {Object.entries(risksByCategory).map(([category, categoryRisks]) => (
            <div key={category} className="bg-slate-900 rounded-lg p-4">
              <div className="flex items-center gap-2 mb-3">
                <span className="text-2xl">{getCategoryIcon(category)}</span>
                <h3 className="text-white font-semibold capitalize">
                  {category} Risks ({categoryRisks.length})
                </h3>
              </div>
              <div className="space-y-2">
                {categoryRisks.length > 0 ? (
                  categoryRisks.map((risk) => (
                    <div
                      key={risk.id}
                      className="bg-slate-800 p-3 rounded cursor-pointer hover:bg-slate-700"
                      onClick={() => setSelectedRisk(risk)}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex-1">
                          <div className="text-white font-medium">{risk.title}</div>
                          <div className="text-sm text-slate-400 mt-1">
                            {risk.mitigation}
                          </div>
                        </div>
                        <span
                          className={`px-2 py-1 rounded text-xs font-semibold text-white shrink-0 ${
                            getSeverityColor(risk.severity)
                          }`}
                        >
                          {risk.severity}
                        </span>
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="text-slate-500 text-sm italic">No {category} risks identified</div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Risk Detail Modal */}
      {selectedRisk && (
        <div
          className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
          onClick={() => setSelectedRisk(null)}
        >
          <div
            className="bg-slate-800 rounded-lg p-6 max-w-lg w-full mx-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between mb-4">
              <div className="flex items-center gap-3">
                <span className="text-3xl">{getCategoryIcon(selectedRisk.category)}</span>
                <div>
                  <h3 className="text-xl font-bold text-white">{selectedRisk.title}</h3>
                  <span
                    className={`inline-block px-2 py-1 rounded text-xs font-semibold text-white mt-1 ${
                      getSeverityColor(selectedRisk.severity)
                    }`}
                  >
                    {selectedRisk.severity} severity
                  </span>
                </div>
              </div>
              <button
                onClick={() => setSelectedRisk(null)}
                className="text-slate-400 hover:text-white text-2xl"
              >
                ×
              </button>
            </div>
            <div className="space-y-4">
              <div>
                <div className="text-sm text-slate-400 mb-1">Category</div>
                <div className="text-white capitalize">{selectedRisk.category}</div>
              </div>
              <div>
                <div className="text-sm text-slate-400 mb-1">Risk Metrics</div>
                <div className="grid grid-cols-2 gap-2">
                  <div className="bg-slate-900 p-2 rounded">
                    <div className="text-xs text-slate-400">Impact</div>
                    <div className="text-white font-semibold">{selectedRisk.impact}%</div>
                  </div>
                  <div className="bg-slate-900 p-2 rounded">
                    <div className="text-xs text-slate-400">Probability</div>
                    <div className="text-white font-semibold">{selectedRisk.probability}%</div>
                  </div>
                </div>
              </div>
              <div>
                <div className="text-sm text-slate-400 mb-1">Description</div>
                <div className="text-white">{selectedRisk.description}</div>
              </div>
              <div>
                <div className="text-sm text-slate-400 mb-1">Mitigation Strategy</div>
                <div className="text-white">{selectedRisk.mitigation}</div>
              </div>
              <div>
                <div className="text-sm text-slate-400 mb-1">Owner</div>
                <div className="text-white">{selectedRisk.owner}</div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
