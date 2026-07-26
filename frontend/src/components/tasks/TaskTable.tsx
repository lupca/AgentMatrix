import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { Task } from '../../types/task';
import {
  ChevronDown,
  ChevronUp,
  ArrowUpDown,
  UserCheck,
  Bot,
  AlertCircle,
  CheckCircle2,
  Clock,
  PlayCircle,
  FileText,
} from 'lucide-react';

interface TaskTableProps {
  tasks: Task[];
  loading?: boolean;
  onStatusChange?: (taskId: string, newStatus: string) => void;
  onSelectTask?: (task: Task) => void;
}

type SortField = 'id' | 'title' | 'project' | 'status' | 'priority' | 'current_gate' | 'updated_at';
type SortDirection = 'asc' | 'desc';

export const TaskTable: React.FC<TaskTableProps> = ({
  tasks,
  loading = false,
  onStatusChange,
  onSelectTask,
}) => {
  const [sortField, setSortField] = useState<SortField>('id');
  const [sortDirection, setSortDirection] = useState<SortDirection>('asc');
  const [expandedTaskId, setExpandedTaskId] = useState<string | null>(null);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortDirection('asc');
    }
  };

  const sortedTasks = [...tasks].sort((a, b) => {
    let valA = a[sortField] || '';
    let valB = b[sortField] || '';

    if (typeof valA === 'string') valA = valA.toLowerCase();
    if (typeof valB === 'string') valB = valB.toLowerCase();

    if (valA < valB) return sortDirection === 'asc' ? -1 : 1;
    if (valA > valB) return sortDirection === 'asc' ? 1 : -1;
    return 0;
  });

  const toggleExpandRow = (taskId: string) => {
    setExpandedTaskId(expandedTaskId === taskId ? null : taskId);
  };

  const getStatusBadge = (status: string) => {
    switch (status.toLowerCase()) {
      case 'todo':
        return (
          <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700">
            <Clock className="w-3 h-3 mr-1 text-gray-400" />
            To Do
          </span>
        );
      case 'dispatched':
        return (
          <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/30">
            <PlayCircle className="w-3 h-3 mr-1 animate-pulse" />
            Dispatched
          </span>
        );
      case 'in-review':
        return (
          <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold bg-purple-500/10 text-purple-400 border border-purple-500/30">
            <UserCheck className="w-3 h-3 mr-1" />
            In Review
          </span>
        );
      case 'done':
        return (
          <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
            <CheckCircle2 className="w-3 h-3 mr-1" />
            Done
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700">
            {status}
          </span>
        );
    }
  };

  const getPriorityBadge = (priority?: string | null) => {
    if (!priority) return null;
    const prio = priority.toLowerCase();
    if (prio === 'high' || prio === 'critical') {
      return (
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-red-500/10 text-red-400 border border-red-500/20">
          High
        </span>
      );
    }
    if (prio === 'medium') {
      return (
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
          Med
        </span>
      );
    }
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-500/10 text-blue-400 border border-blue-500/20">
        Low
      </span>
    );
  };

  const renderSortIcon = (field: SortField) => {
    if (sortField !== field) {
      return <ArrowUpDown className="w-3 h-3 ml-1 text-gray-600 group-hover:text-gray-400 transition-colors" />;
    }
    return sortDirection === 'asc' ? (
      <ChevronUp className="w-3.5 h-3.5 ml-1 text-indigo-400" />
    ) : (
      <ChevronDown className="w-3.5 h-3.5 ml-1 text-indigo-400" />
    );
  };

  if (loading) {
    return (
      <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-12 text-center">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500 mb-3"></div>
        <p className="text-gray-400 text-sm">Loading tasks data...</p>
      </div>
    );
  }

  if (tasks.length === 0) {
    return (
      <div className="bg-gray-900/60 border border-dashed border-gray-800 rounded-xl p-12 text-center">
        <AlertCircle className="w-10 h-10 text-gray-500 mx-auto mb-3" />
        <h3 className="text-base font-medium text-gray-300">No tasks found</h3>
        <p className="text-sm text-gray-500 mt-1">There are no tasks matching your selected filters.</p>
      </div>
    );
  }

  return (
    <div className="bg-gray-900/80 border border-gray-800 rounded-xl overflow-hidden shadow-lg backdrop-blur">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm text-gray-300">
          <thead className="bg-gray-950/80 text-xs uppercase tracking-wider text-gray-400 border-b border-gray-800 select-none">
            <tr>
              <th className="py-3.5 px-4 w-10"></th>
              <th
                onClick={() => handleSort('id')}
                className="py-3.5 px-4 font-semibold cursor-pointer group hover:text-gray-200"
              >
                <div className="flex items-center">
                  <span>Task ID</span>
                  {renderSortIcon('id')}
                </div>
              </th>
              <th
                onClick={() => handleSort('title')}
                className="py-3.5 px-4 font-semibold cursor-pointer group hover:text-gray-200"
              >
                <div className="flex items-center">
                  <span>Title</span>
                  {renderSortIcon('title')}
                </div>
              </th>
              <th
                onClick={() => handleSort('project')}
                className="py-3.5 px-4 font-semibold cursor-pointer group hover:text-gray-200"
              >
                <div className="flex items-center">
                  <span>Project</span>
                  {renderSortIcon('project')}
                </div>
              </th>
              <th
                onClick={() => handleSort('status')}
                className="py-3.5 px-4 font-semibold cursor-pointer group hover:text-gray-200"
              >
                <div className="flex items-center">
                  <span>Status</span>
                  {renderSortIcon('status')}
                </div>
              </th>
              <th
                onClick={() => handleSort('current_gate')}
                className="py-3.5 px-4 font-semibold cursor-pointer group hover:text-gray-200"
              >
                <div className="flex items-center">
                  <span>Gate</span>
                  {renderSortIcon('current_gate')}
                </div>
              </th>
              <th className="py-3.5 px-4 font-semibold">Priority</th>
              <th className="py-3.5 px-4 font-semibold">Executor / Reviewer</th>
              <th className="py-3.5 px-4 font-semibold text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {sortedTasks.map((task) => {
              const isExpanded = expandedTaskId === task.id;
              return (
                <React.Fragment key={task.id}>
                  <tr
                    className={`hover:bg-gray-800/40 transition-colors ${
                      isExpanded ? 'bg-gray-800/30' : ''
                    }`}
                  >
                    <td className="py-3.5 px-4 text-center">
                      <button
                        onClick={() => toggleExpandRow(task.id)}
                        className="text-gray-500 hover:text-gray-300 p-1 rounded hover:bg-gray-800 transition-colors"
                        title="Toggle task details"
                      >
                        {isExpanded ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                    </td>
                    <td className="py-3.5 px-4 font-mono font-bold text-indigo-400 whitespace-nowrap">
                      <Link to={`/tasks/${task.id}`} className="hover:underline hover:text-indigo-300">
                        {task.id}
                      </Link>
                    </td>
                    <td className="py-3.5 px-4 font-medium text-white max-w-xs truncate">
                      <Link
                        to={`/tasks/${task.id}`}
                        onClick={() => onSelectTask?.(task)}
                        className="hover:underline hover:text-indigo-300 text-left truncate block w-full"
                      >
                        {task.title}
                      </Link>
                    </td>
                    <td className="py-3.5 px-4 whitespace-nowrap">
                      <span className="px-2 py-0.5 rounded text-xs font-medium bg-gray-800 text-gray-300 border border-gray-700">
                        {task.project}
                      </span>
                    </td>
                    <td className="py-3.5 px-4 whitespace-nowrap">
                      {getStatusBadge(task.status)}
                    </td>
                    <td className="py-3.5 px-4 whitespace-nowrap text-xs text-gray-400">
                      {task.current_gate ? (
                        <span className="px-2 py-0.5 rounded bg-indigo-950/60 text-indigo-300 border border-indigo-800/40">
                          {task.current_gate}
                        </span>
                      ) : (
                        '-'
                      )}
                    </td>
                    <td className="py-3.5 px-4 whitespace-nowrap">
                      {getPriorityBadge(task.priority)}
                    </td>
                    <td className="py-3.5 px-4 whitespace-nowrap text-xs text-gray-400">
                      <div className="flex items-center space-x-2">
                        {task.executor && (
                          <span className="flex items-center space-x-1 text-gray-300" title="Executor">
                            <Bot className="w-3.5 h-3.5 text-indigo-400" />
                            <span>{task.executor}</span>
                          </span>
                        )}
                        {task.reviewer && (
                          <span className="flex items-center space-x-1 text-purple-300" title="Reviewer">
                            <UserCheck className="w-3.5 h-3.5 text-purple-400" />
                            <span>{task.reviewer}</span>
                          </span>
                        )}
                        {!task.executor && !task.reviewer && <span className="text-gray-600">-</span>}
                      </div>
                    </td>
                    <td className="py-3.5 px-4 whitespace-nowrap text-right text-xs">
                      {onStatusChange ? (
                        <select
                          value={task.status}
                          onChange={(e) => onStatusChange(task.id, e.target.value)}
                          className="bg-gray-950 border border-gray-800 rounded px-2 py-1 text-xs text-gray-300 focus:outline-none focus:border-indigo-500 cursor-pointer"
                        >
                          <option value="todo">To Do</option>
                          <option value="dispatched">Dispatched</option>
                          <option value="in-review">In Review</option>
                          <option value="done">Done</option>
                        </select>
                      ) : (
                        <span className="text-gray-500">View</span>
                      )}
                    </td>
                  </tr>

                  {/* Expanded Detail Drawer */}
                  {isExpanded && (
                    <tr className="bg-gray-950/60 border-t border-b border-gray-800/80">
                      <td colSpan={9} className="p-4 pl-14">
                        <div className="space-y-3 text-xs text-gray-300">
                          {task.raw_input && (
                            <div>
                              <span className="font-semibold text-gray-400 block mb-1">
                                Raw Input / Prompt:
                              </span>
                              <p className="bg-gray-900 p-2.5 rounded border border-gray-800 font-mono text-gray-300 whitespace-pre-wrap">
                                {task.raw_input}
                              </p>
                            </div>
                          )}

                          {task.plan && (
                            <div>
                              <span className="font-semibold text-gray-400 flex items-center mb-1">
                                <FileText className="w-3.5 h-3.5 mr-1 text-indigo-400" /> Plan
                                Summary:
                              </span>
                              <p className="bg-gray-900 p-2.5 rounded border border-gray-800 text-gray-300 whitespace-pre-wrap">
                                {task.plan}
                              </p>
                            </div>
                          )}

                          {task.acceptance_criteria && task.acceptance_criteria.length > 0 && (
                            <div>
                              <span className="font-semibold text-gray-400 block mb-1">
                                Acceptance Criteria:
                              </span>
                              <ul className="list-disc list-inside space-y-1 bg-gray-900 p-2.5 rounded border border-gray-800">
                                {task.acceptance_criteria.map((item, idx) => (
                                  <li key={idx} className="text-gray-300">
                                    {item}
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}

                          {task.error && (
                            <div className="bg-red-500/10 border border-red-500/30 p-2.5 rounded text-red-300">
                              <span className="font-semibold flex items-center mb-1 text-red-400">
                                <AlertCircle className="w-3.5 h-3.5 mr-1" /> Failure Log:
                              </span>
                              <pre className="font-mono text-xs overflow-x-auto whitespace-pre-wrap">
                                {task.error}
                              </pre>
                            </div>
                          )}

                          <div className="flex flex-wrap gap-4 text-gray-500 text-xs pt-1">
                            {task.created_at && (
                              <span>Created: {new Date(task.created_at).toLocaleString()}</span>
                            )}
                            {task.updated_at && (
                              <span>Updated: {new Date(task.updated_at).toLocaleString()}</span>
                            )}
                            {task.mode && (
                              <span className="capitalize">Mode: {task.mode}</span>
                            )}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
};
