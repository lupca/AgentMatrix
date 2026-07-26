import React, { useState } from 'react';
import { BarChart3, Layers, PieChart } from 'lucide-react';

export interface TaskStatusCounts {
  todo?: number;
  in_progress?: number;
  in_review?: number;
  completed?: number;
  failed?: number;
}

export interface GateCounts {
  spec?: number;
  plan?: number;
  dispatch?: number;
  review?: number;
  verdict?: number;
}

interface StatusChartProps {
  statusCounts?: TaskStatusCounts;
  gateCounts?: GateCounts;
  loading?: boolean;
}

export const StatusChart: React.FC<StatusChartProps> = ({
  statusCounts = {},
  gateCounts = {},
  loading = false,
}) => {
  const [activeTab, setActiveTab] = useState<'status' | 'gate'>('status');

  if (loading) {
    return (
      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm animate-pulse h-80 flex flex-col justify-between">
        <div className="h-6 bg-gray-800 rounded w-1/3"></div>
        <div className="space-y-4 my-auto">
          <div className="h-4 bg-gray-800 rounded w-full"></div>
          <div className="h-4 bg-gray-800 rounded w-5/6"></div>
          <div className="h-4 bg-gray-800 rounded w-4/6"></div>
        </div>
      </div>
    );
  }

  const statuses = [
    { key: 'todo', label: 'To Do', count: statusCounts.todo ?? 0, color: 'bg-slate-500', barBg: 'bg-slate-500/20', textColor: 'text-slate-400', border: 'border-slate-500/30' },
    { key: 'in_progress', label: 'In Progress', count: statusCounts.in_progress ?? 0, color: 'bg-indigo-500', barBg: 'bg-indigo-500/20', textColor: 'text-indigo-400', border: 'border-indigo-500/30' },
    { key: 'in_review', label: 'In Review', count: statusCounts.in_review ?? 0, color: 'bg-amber-500', barBg: 'bg-amber-500/20', textColor: 'text-amber-400', border: 'border-amber-500/30' },
    { key: 'completed', label: 'Completed', count: statusCounts.completed ?? 0, color: 'bg-emerald-500', barBg: 'bg-emerald-500/20', textColor: 'text-emerald-400', border: 'border-emerald-500/30' },
    { key: 'failed', label: 'Failed', count: statusCounts.failed ?? 0, color: 'bg-rose-500', barBg: 'bg-rose-500/20', textColor: 'text-rose-400', border: 'border-rose-500/30' },
  ];

  const gates = [
    { key: 'spec', label: 'Spec Gate', count: gateCounts.spec ?? 0, color: 'bg-cyan-500', textColor: 'text-cyan-400' },
    { key: 'plan', label: 'Plan Gate', count: gateCounts.plan ?? 0, color: 'bg-blue-500', textColor: 'text-blue-400' },
    { key: 'dispatch', label: 'Dispatch Gate', count: gateCounts.dispatch ?? 0, color: 'bg-violet-500', textColor: 'text-violet-400' },
    { key: 'review', label: 'Review Gate', count: gateCounts.review ?? 0, color: 'bg-amber-500', textColor: 'text-amber-400' },
    { key: 'verdict', label: 'Verdict Gate', count: gateCounts.verdict ?? 0, color: 'bg-emerald-500', textColor: 'text-emerald-400' },
  ];

  const currentItems = activeTab === 'status' ? statuses : gates;
  const totalCount = currentItems.reduce((acc, curr) => acc + curr.count, 0);

  return (
    <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-gray-800/80">
        <div>
          <h2 className="text-base font-semibold text-gray-100 flex items-center gap-2">
            <BarChart3 className="w-4 h-4 text-indigo-400" />
            Task Execution Distribution
          </h2>
          <p className="text-xs text-gray-400 mt-0.5">
            Breakdown of tasks across workflow states and execution gates
          </p>
        </div>

        <div className="flex items-center p-1 bg-gray-950/80 rounded-lg border border-gray-800 text-xs">
          <button
            onClick={() => setActiveTab('status')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium transition-all ${
              activeTab === 'status'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-gray-400 hover:text-gray-200'
            }`}
          >
            <PieChart className="w-3.5 h-3.5" />
            <span>By Status</span>
          </button>
          <button
            onClick={() => setActiveTab('gate')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md font-medium transition-all ${
              activeTab === 'gate'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-gray-400 hover:text-gray-200'
            }`}
          >
            <Layers className="w-3.5 h-3.5" />
            <span>By Gate</span>
          </button>
        </div>
      </div>

      {/* Segmented bar view */}
      <div className="mt-6 space-y-2">
        <div className="flex justify-between items-center text-xs text-gray-400">
          <span>Distribution Progress</span>
          <span className="font-mono text-gray-300">{totalCount} total items</span>
        </div>
        <div className="h-4 w-full bg-gray-950 rounded-full overflow-hidden flex p-0.5 border border-gray-800">
          {totalCount === 0 ? (
            <div className="w-full h-full bg-gray-800/50 rounded-full"></div>
          ) : (
            currentItems.map((item) => {
              const pct = (item.count / totalCount) * 100;
              if (pct === 0) return null;
              return (
                <div
                  key={item.key}
                  style={{ width: `${pct}%` }}
                  className={`h-full ${item.color} transition-all duration-300 first:rounded-l-full last:rounded-r-full hover:opacity-80`}
                  title={`${item.label}: ${item.count} (${pct.toFixed(1)}%)`}
                />
              );
            })
          )}
        </div>
      </div>

      {/* Breakdown list */}
      <div className="mt-6 grid grid-cols-1 sm:grid-cols-2 gap-3">
        {currentItems.map((item) => {
          const pct = totalCount > 0 ? ((item.count / totalCount) * 100).toFixed(1) : '0.0';
          return (
            <div
              key={item.key}
              className="flex items-center justify-between p-3 rounded-lg bg-gray-950/50 border border-gray-800/50 hover:border-gray-700/60 transition-colors"
            >
              <div className="flex items-center gap-3">
                <span className={`w-3 h-3 rounded-full ${item.color}`} />
                <span className="text-xs font-medium text-gray-300">{item.label}</span>
              </div>
              <div className="flex items-center gap-3">
                <span className={`text-xs font-mono font-semibold ${item.textColor}`}>
                  {item.count}
                </span>
                <span className="text-[11px] font-mono text-gray-500 w-12 text-right">
                  {pct}%
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default StatusChart;
