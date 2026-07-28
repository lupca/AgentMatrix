import React from 'react';
import { Link } from 'react-router-dom';
import { Layers, Search, Zap, ShieldCheck, ExternalLink } from 'lucide-react';
import { Task } from '../../types/task';

interface AgentTaskListProps {
  agentId: string | undefined;
  executorTasks: Task[];
  reviewerTasks: Task[];
  displayedTasks: Task[];
  activeTab: 'executor' | 'reviewer';
  search: string;
  onTabChange: (tab: 'executor' | 'reviewer') => void;
  onSearchChange: (value: string) => void;
}

export const AgentTaskList: React.FC<AgentTaskListProps> = ({
  agentId,
  executorTasks,
  reviewerTasks,
  displayedTasks,
  activeTab,
  search,
  onTabChange,
  onSearchChange,
}) => {
  return (
    <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-gray-800/80">
        <div>
          <h2 className="text-base font-semibold text-gray-100 flex items-center gap-2">
            <Layers className="w-4 h-4 text-purple-400" />
            Assigned Task Workload
          </h2>
          <p className="text-xs text-gray-400 mt-0.5">
            Tasks executed or reviewed by agent {agentId}
          </p>
        </div>

        <div className="relative w-full sm:w-72">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Search assigned tasks..."
            className="w-full bg-gray-950/80 border border-gray-800 rounded-lg pl-9 pr-3 py-1.5 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500 transition-colors"
          />
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={() => onTabChange('executor')}
          className={`px-4 py-2 rounded-lg text-xs font-medium transition-all flex items-center gap-2 ${
            activeTab === 'executor'
              ? 'bg-purple-600 text-white shadow-md'
              : 'bg-gray-950/60 text-gray-400 hover:text-gray-200 border border-gray-800/60'
          }`}
        >
          <Zap className="w-3.5 h-3.5" />
          <span>As Executor ({executorTasks.length})</span>
        </button>

        <button
          onClick={() => onTabChange('reviewer')}
          className={`px-4 py-2 rounded-lg text-xs font-medium transition-all flex items-center gap-2 ${
            activeTab === 'reviewer'
              ? 'bg-blue-600 text-white shadow-md'
              : 'bg-gray-950/60 text-gray-400 hover:text-gray-200 border border-gray-800/60'
          }`}
        >
          <ShieldCheck className="w-3.5 h-3.5" />
          <span>As Reviewer ({reviewerTasks.length})</span>
        </button>
      </div>

      {displayedTasks.length === 0 ? (
        <div className="py-12 text-center text-gray-500 text-sm">
          No tasks found where {agentId} is recorded as {activeTab}.
        </div>
      ) : (
        <div className="overflow-x-auto border border-gray-800/80 rounded-xl">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="bg-gray-950/80 text-gray-400 border-b border-gray-800/80 font-medium">
                <th className="p-3">Task ID</th>
                <th className="p-3">Project</th>
                <th className="p-3">Title</th>
                <th className="p-3">Status</th>
                <th className="p-3">Gate</th>
                <th className="p-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/60 text-gray-300">
              {displayedTasks.map((task) => (
                <tr key={task.id} className="hover:bg-gray-800/30 transition-colors">
                  <td className="p-3 font-mono font-semibold text-purple-400 whitespace-nowrap">
                    {task.id}
                  </td>
                  <td className="p-3 whitespace-nowrap">
                    <Link
                      to={`/projects/${task.project}`}
                      className="font-mono text-indigo-400 hover:underline font-medium"
                    >
                      {task.project}
                    </Link>
                  </td>
                  <td className="p-3 font-medium text-gray-100 max-w-md line-clamp-1">
                    {task.title}
                  </td>
                  <td className="p-3 whitespace-nowrap">
                    <span className="px-2 py-0.5 rounded-full border text-[11px] font-medium capitalize bg-gray-800 text-gray-300">
                      {task.status}
                    </span>
                  </td>
                  <td className="p-3 whitespace-nowrap">
                    <span className="px-2 py-0.5 rounded bg-gray-800 text-gray-300 font-mono text-[10px] uppercase">
                      {task.current_gate || 'spec'}
                    </span>
                  </td>
                  <td className="p-3 text-right whitespace-nowrap">
                    <Link
                      to="/tasks"
                      className="inline-flex items-center gap-1 text-xs text-purple-400 hover:text-purple-300 font-medium"
                    >
                      <span>View Task</span>
                      <ExternalLink className="w-3 h-3" />
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export default AgentTaskList;
