import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { Task } from '../../types/task';
import Pagination from '../common/Pagination';
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
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

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

  const totalPages = Math.max(1, Math.ceil(sortedTasks.length / pageSize));
  const paginatedTasks = sortedTasks.slice((currentPage - 1) * pageSize, currentPage * pageSize);

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
      <div className="w-full overflow-x-auto">
        <table className="w-full text-left text-sm text-gray-300 table-auto border-collapse">
          <thead className="bg-gray-950/90 text-[11px] uppercase tracking-wider text-gray-400 border-b border-gray-800 select-none">
            <tr>
              <th className="py-3 px-3 w-8"></th>
              <th
                onClick={() => handleSort('title')}
                className="py-3 px-4 font-semibold cursor-pointer group hover:text-gray-200"
              >
                <div className="flex items-center space-x-1">
                  <span>Task & Details</span>
                  {renderSortIcon('title')}
                </div>
              </th>
              <th
                onClick={() => handleSort('status')}
                className="py-3 px-4 font-semibold cursor-pointer group hover:text-gray-200 w-36"
              >
                <div className="flex items-center space-x-1">
                  <span>Status</span>
                  {renderSortIcon('status')}
                </div>
              </th>
              <th className="py-3 px-4 font-semibold w-44">
                <span>Assignees</span>
              </th>
              <th className="py-3 px-4 font-semibold text-right w-36">
                <span>Actions</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {paginatedTasks.map((task) => {
              const isExpanded = expandedTaskId === task.id;
              return (
                <React.Fragment key={task.id}>
                  <tr
                    className={`hover:bg-gray-800/40 transition-colors ${
                      isExpanded ? 'bg-gray-800/30' : ''
                    }`}
                  >
                    <td className="py-3.5 px-3 text-center align-top pt-4">
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

                    {/* Compact Task & Details Column */}
                    <td className="py-3.5 px-4 align-top">
                      <div className="space-y-1.5 max-w-2xl">
                        <div className="flex items-start flex-wrap gap-2">
                          <Link
                            to={`/tasks/${task.id}`}
                            className="inline-flex items-center font-mono font-bold text-xs px-2 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 hover:bg-indigo-500/20 transition-colors shrink-0"
                          >
                            {task.id}
                          </Link>
                          <Link
                            to={`/tasks/${task.id}`}
                            onClick={() => onSelectTask?.(task)}
                            className="font-medium text-gray-100 hover:text-indigo-300 transition-colors text-sm leading-snug break-words"
                          >
                            {task.title}
                          </Link>
                        </div>

                        {/* Subline Meta Pills */}
                        <div className="flex items-center flex-wrap gap-2 text-xs">
                          {task.project && (
                            <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium bg-gray-800 text-gray-300 border border-gray-700/70">
                              {task.project}
                            </span>
                          )}
                          {task.current_gate && (
                            <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-mono bg-indigo-950/60 text-indigo-300 border border-indigo-800/40 uppercase">
                              gate: {task.current_gate}
                            </span>
                          )}
                          {task.priority && getPriorityBadge(task.priority)}
                        </div>
                      </div>
                    </td>

                    {/* Status Column */}
                    <td className="py-3.5 px-4 align-top pt-4">
                      {getStatusBadge(task.status)}
                    </td>

                    {/* Assignees Column */}
                    <td className="py-3.5 px-4 align-top text-xs text-gray-400 space-y-1">
                      {task.executor ? (
                        <div className="flex items-center space-x-1.5 text-gray-200" title="Executor">
                          <Bot className="w-3.5 h-3.5 text-indigo-400 shrink-0" />
                          <span className="font-mono text-xs truncate max-w-[120px]">{task.executor}</span>
                        </div>
                      ) : null}
                      {task.reviewer ? (
                        <div className="flex items-center space-x-1.5 text-purple-300" title="Reviewer">
                          <UserCheck className="w-3.5 h-3.5 text-purple-400 shrink-0" />
                          <span className="font-mono text-xs truncate max-w-[120px]">{task.reviewer}</span>
                        </div>
                      ) : null}
                      {!task.executor && !task.reviewer && <span className="text-gray-600">-</span>}
                    </td>

                    {/* Actions Column */}
                    <td className="py-3.5 px-4 align-top text-right text-xs">
                      {onStatusChange ? (
                        <select
                          value={task.status}
                          onChange={(e) => onStatusChange(task.id, e.target.value)}
                          className="bg-gray-950 border border-gray-800 rounded px-2 py-1.5 text-xs text-gray-300 focus:outline-none focus:border-indigo-500 cursor-pointer hover:border-gray-700 transition-colors"
                        >
                          <option value="todo">To Do</option>
                          <option value="dispatched">Dispatched</option>
                          <option value="in-review">In Review</option>
                          <option value="done">Done</option>
                        </select>
                      ) : (
                        <Link
                          to={`/tasks/${task.id}`}
                          className="inline-flex items-center px-2.5 py-1 rounded bg-gray-800 text-indigo-400 hover:text-indigo-300 text-xs font-medium"
                        >
                          View
                        </Link>
                      )}
                    </td>
                  </tr>

                  {/* Expanded Detail Drawer */}
                  {isExpanded && (
                    <tr className="bg-gray-950/60 border-t border-b border-gray-800/80">
                      <td colSpan={5} className="p-4 pl-12">
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
      <div className="px-4 pb-4">
        <Pagination
          currentPage={currentPage}
          totalPages={totalPages}
          totalItems={tasks.length}
          pageSize={pageSize}
          onPageChange={setCurrentPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setCurrentPage(1);
          }}
        />
      </div>
    </div>
  );
};
