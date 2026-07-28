import React from 'react';

interface ProjectKpiCardsProps {
  totalTasks: number;
  completedTasks: number;
  activeTasks: number;
  progressPct: number;
}

export const ProjectKpiCards: React.FC<ProjectKpiCardsProps> = ({
  totalTasks,
  completedTasks,
  activeTasks,
  progressPct,
}) => {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm">
        <p className="text-xs font-medium text-gray-400">Total Project Tasks</p>
        <h3 className="text-2xl font-extrabold text-gray-100 mt-1 font-mono">{totalTasks}</h3>
      </div>

      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm">
        <p className="text-xs font-medium text-gray-400">Completed Tasks</p>
        <h3 className="text-2xl font-extrabold text-emerald-400 mt-1 font-mono">{completedTasks}</h3>
      </div>

      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm">
        <p className="text-xs font-medium text-gray-400">Active / Pending Tasks</p>
        <h3 className="text-2xl font-extrabold text-indigo-400 mt-1 font-mono">{activeTasks}</h3>
      </div>

      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm">
        <div className="flex items-center justify-between">
          <p className="text-xs font-medium text-gray-400">Progress Percentage</p>
          <span className="text-xs font-mono text-purple-400 font-semibold">{progressPct}%</span>
        </div>
        <div className="w-full h-2 bg-gray-950 rounded-full overflow-hidden border border-gray-800 mt-3">
          <div
            style={{ width: `${progressPct}%` }}
            className="h-full bg-gradient-to-r from-indigo-500 to-emerald-400 rounded-full"
          />
        </div>
      </div>
    </div>
  );
};

export default ProjectKpiCards;
