import React from 'react';
import { Link } from 'react-router-dom';
import { Layers, Search, Cpu, ShieldCheck, ExternalLink } from 'lucide-react';
import { Task } from '../../types/task';

interface ProjectTaskListProps {
  projectId: string | undefined;
  filteredTasks: Task[];
  search: string;
  statusFilter: string;
  onSearchChange: (value: string) => void;
  onStatusFilterChange: (value: string) => void;
}

const getStatusBadge = (status: string) => {
  switch (status.toLowerCase()) {
    case 'done':
    case 'completed':
    case 'passed':
      return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
    case 'in-review':
    case 'review':
      return 'bg-purple-500/10 text-purple-400 border-purple-500/30';
    case 'dispatched':
    case 'in_progress':
      return 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30';
    case 'todo':
      return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
    case 'failed':
    case 'cancelled':
      return 'bg-rose-500/10 text-rose-400 border-rose-500/30';
    default:
      return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
  }
};

const getPriorityBadge = (priority?: string | null) => {
  switch ((priority || '').toUpperCase()) {
    case 'P0':
      return 'bg-rose-500/10 text-rose-400 border-rose-500/30';
    case 'P1':
      return 'bg-amber-500/10 text-amber-400 border-amber-500/30';
    case 'P2':
      return 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30';
    default:
      return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
  }
};

export const ProjectTaskList: React.FC<ProjectTaskListProps> = ({
  projectId,
  filteredTasks,
  search,
  statusFilter,
  onSearchChange,
  onStatusFilterChange,
}) => {
  return (
    <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-gray-800/80">
        <div>
          <h2 className="text-base font-semibold text-gray-100 flex items-center gap-2">
            <Layers className="w-4 h-4 text-indigo-400" />
            Project Task Execution Queue
          </h2>
          <p className="text-xs text-gray-400 mt-0.5">
            Tasks assigned to {projectId || 'this project'} with executor & gate statuses
          </p>
        </div>

        <div className="relative w-full sm:w-72">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Search tasks or agents..."
            className="w-full bg-gray-950/80 border border-gray-800 rounded-lg pl-9 pr-3 py-1.5 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-indigo-500 transition-colors"
          />
        </div>
      </div>

      <div className="flex items-center gap-2 overflow-x-auto pb-1">
        {['all', 'todo', 'dispatched', 'in-review', 'done'].map((st) => (
          <button
            key={st}
            onClick={() => onStatusFilterChange(st)}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium capitalize transition-colors flex-shrink-0 ${
              statusFilter === st
                ? 'bg-indigo-600 text-white shadow-md'
                : 'bg-gray-950/60 text-gray-400 hover:text-gray-200 hover:bg-gray-800/50 border border-gray-800/60'
            }`}
          >
            {st}
          </button>
        ))}
      </div>

      {filteredTasks.length === 0 ? (
        <div className="py-12 text-center text-gray-500 text-sm">
          No tasks found for project {projectId} matching your filter criteria.
        </div>
      ) : (
        <div className="overflow-x-auto border border-gray-800/80 rounded-xl">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="bg-gray-950/80 text-gray-400 border-b border-gray-800/80 font-medium">
                <th className="p-3">Task ID</th>
                <th className="p-3">Title</th>
                <th className="p-3">Status</th>
                <th className="p-3">Gate</th>
                <th className="p-3">Priority / Risk</th>
                <th className="p-3">Executor</th>
                <th className="p-3">Reviewer</th>
                <th className="p-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/60 text-gray-300">
              {filteredTasks.map((task) => (
                <tr key={task.id} className="hover:bg-gray-800/30 transition-colors group">
                  <td className="p-3 font-mono font-semibold text-indigo-400 whitespace-nowrap">
                    {task.id}
                  </td>
                  <td className="p-3 font-medium text-gray-100 max-w-md line-clamp-1">
                    {task.title}
                  </td>
                  <td className="p-3 whitespace-nowrap">
                    <span className={`px-2 py-0.5 rounded-full border text-[11px] font-medium capitalize ${getStatusBadge(task.status)}`}>
                      {task.status}
                    </span>
                  </td>
                  <td className="p-3 whitespace-nowrap">
                    <span className="px-2 py-0.5 rounded bg-gray-800 text-gray-300 font-mono text-[10px] uppercase">
                      {task.current_gate || 'spec'}
                    </span>
                  </td>
                  <td className="p-3 whitespace-nowrap space-x-1">
                    {task.priority && (
                      <span className={`px-2 py-0.5 rounded border text-[10px] font-mono ${getPriorityBadge(task.priority)}`}>
                        {task.priority}
                      </span>
                    )}
                    {task.risk && (
                      <span className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 text-[10px]">
                        {task.risk}
                      </span>
                    )}
                  </td>
                  <td className="p-3 whitespace-nowrap">
                    {task.executor ? (
                      <Link
                        to={`/agents/${task.executor}`}
                        className="inline-flex items-center gap-1 text-purple-300 hover:underline font-mono text-[11px]"
                      >
                        <Cpu className="w-3 h-3 text-purple-400" />
                        {task.executor}
                      </Link>
                    ) : (
                      <span className="text-gray-600 font-italic">Unassigned</span>
                    )}
                  </td>
                  <td className="p-3 whitespace-nowrap">
                    {task.reviewer ? (
                      <Link
                        to={`/agents/${task.reviewer}`}
                        className="inline-flex items-center gap-1 text-blue-300 hover:underline font-mono text-[11px]"
                      >
                        <ShieldCheck className="w-3 h-3 text-blue-400" />
                        {task.reviewer}
                      </Link>
                    ) : (
                      <span className="text-gray-600 font-italic">None</span>
                    )}
                  </td>
                  <td className="p-3 text-right whitespace-nowrap">
                    <Link
                      to={`/tasks/${task.id}`}
                      className="inline-flex items-center gap-1 text-xs text-indigo-400 hover:text-indigo-300 font-medium"
                    >
                      <span>View</span>
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

export default ProjectTaskList;
