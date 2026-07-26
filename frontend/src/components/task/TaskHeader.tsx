import React from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Task } from '../../types/task';
import {
  ArrowLeft,
  CheckSquare,
  Clock,
  PlayCircle,
  UserCheck,
  CheckCircle2,
  AlertTriangle,
  RefreshCw,
  Play,
  Check,
  Copy,
  Sparkles,
  ShieldAlert,
} from 'lucide-react';

interface TaskHeaderProps {
  task: Task;
  onStatusChange?: (newStatus: string) => void;
  onDispatch?: () => void;
  onApprove?: () => void;
  onRefresh?: () => void;
  isDispatching?: boolean;
}

export const TaskHeader: React.FC<TaskHeaderProps> = ({
  task,
  onStatusChange,
  onDispatch,
  onApprove,
  onRefresh,
  isDispatching = false,
}) => {
  const navigate = useNavigate();
  const [copied, setCopied] = React.useState(false);

  const handleCopyId = () => {
    navigator.clipboard.writeText(task.id);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const getStatusBadge = (status: string) => {
    switch (status.toLowerCase()) {
      case 'todo':
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700">
            <Clock className="w-3.5 h-3.5 mr-1 text-gray-400" />
            To Do
          </span>
        );
      case 'dispatched':
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/30">
            <PlayCircle className="w-3.5 h-3.5 mr-1 animate-pulse" />
            Dispatched
          </span>
        );
      case 'in-review':
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-purple-500/10 text-purple-400 border border-purple-500/30">
            <UserCheck className="w-3.5 h-3.5 mr-1" />
            In Review
          </span>
        );
      case 'done':
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
            <CheckCircle2 className="w-3.5 h-3.5 mr-1" />
            Done
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700">
            {status}
          </span>
        );
    }
  };

  const getPriorityBadge = (priority?: string | null) => {
    if (!priority) return null;
    const prio = priority.toUpperCase();
    let style = 'bg-blue-500/10 text-blue-400 border-blue-500/20';
    if (prio === 'P0' || prio === 'HIGH' || prio === 'CRITICAL') {
      style = 'bg-red-500/10 text-red-400 border-red-500/20';
    } else if (prio === 'P1' || prio === 'MEDIUM') {
      style = 'bg-amber-500/10 text-amber-400 border-amber-500/20';
    }

    return (
      <span className={`px-2.5 py-0.5 rounded text-xs font-semibold border font-mono ${style}`}>
        {priority}
      </span>
    );
  };

  const getRiskBadge = (risk?: string | null) => {
    if (!risk) return null;
    const r = risk.toLowerCase();
    let style = 'bg-gray-800 text-gray-400 border-gray-700';
    if (r === 'high' || r === 'critical') {
      style = 'bg-red-950/60 text-red-300 border-red-800/60';
    } else if (r === 'medium') {
      style = 'bg-amber-950/60 text-amber-300 border-amber-800/60';
    }
    return (
      <span className={`px-2.5 py-0.5 rounded text-xs font-medium border capitalize ${style}`}>
        Risk: {risk}
      </span>
    );
  };

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-2xl p-6 shadow-xl backdrop-blur-md space-y-4">
      {/* Top Bar: Navigation & Action Buttons */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center space-x-3">
          <button
            onClick={() => navigate('/tasks')}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-950/80 border border-gray-800 text-xs font-medium text-gray-400 hover:text-white hover:border-gray-700 transition-colors"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            <span>Tasks</span>
          </button>
          <span className="text-gray-600">/</span>
          <Link
            to={`/projects/${task.project}`}
            className="text-xs font-mono font-bold text-indigo-400 hover:underline"
          >
            {task.project}
          </Link>
          <span className="text-gray-600">/</span>
          <button
            onClick={handleCopyId}
            className="flex items-center gap-1 text-xs font-mono font-bold text-gray-300 hover:text-white bg-gray-950 px-2 py-0.5 rounded border border-gray-800"
            title="Copy Task ID"
          >
            <span>{task.id}</span>
            {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3 text-gray-500" />}
          </button>
        </div>

        <div className="flex items-center space-x-2">
          {onRefresh && (
            <button
              onClick={onRefresh}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-gray-950 border border-gray-800 text-gray-300 hover:text-white text-xs font-medium transition-colors"
              title="Reload Task data"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              <span>Refresh</span>
            </button>
          )}

          {task.awaiting_approval && onApprove && (
            <button
              onClick={onApprove}
              className="flex items-center space-x-1.5 px-3.5 py-1.5 rounded-lg bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 text-white text-xs font-semibold shadow-lg shadow-emerald-600/20 transition-all animate-bounce"
            >
              <CheckCircle2 className="w-3.5 h-3.5" />
              <span>Approve Execution</span>
            </button>
          )}

          {onDispatch && (
            <button
              onClick={onDispatch}
              disabled={isDispatching}
              className="flex items-center space-x-1.5 px-3.5 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 active:bg-indigo-700 text-white text-xs font-semibold shadow-lg shadow-indigo-600/20 transition-colors disabled:opacity-50"
            >
              <Play className={`w-3.5 h-3.5 ${isDispatching ? 'animate-spin' : ''}`} />
              <span>{isDispatching ? 'Dispatching...' : 'Dispatch Task'}</span>
            </button>
          )}
        </div>
      </div>

      {/* Title & Badges */}
      <div className="space-y-3">
        <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-3">
          <h1 className="text-xl sm:text-2xl font-extrabold text-white tracking-tight leading-snug">
            {task.title}
          </h1>

          <div className="flex items-center space-x-2 shrink-0">
            {onStatusChange ? (
              <select
                value={task.status}
                onChange={(e) => onStatusChange(e.target.value)}
                className="bg-gray-950 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-200 font-semibold focus:outline-none focus:border-indigo-500 cursor-pointer"
              >
                <option value="todo">To Do</option>
                <option value="dispatched">Dispatched</option>
                <option value="in-review">In Review</option>
                <option value="done">Done</option>
              </select>
            ) : (
              getStatusBadge(task.status)
            )}
          </div>
        </div>

        {/* Metadata Pill Bar */}
        <div className="flex flex-wrap items-center gap-2 pt-1 border-t border-gray-800/60">
          {getPriorityBadge(task.priority)}
          {getRiskBadge(task.risk)}

          {task.current_gate && (
            <span className="px-2.5 py-0.5 rounded text-xs font-mono font-semibold bg-indigo-950/80 text-indigo-300 border border-indigo-800/50 flex items-center gap-1">
              <Sparkles className="w-3 h-3 text-indigo-400" />
              Gate: {task.current_gate}
            </span>
          )}

          {task.mode && (
            <span className="px-2.5 py-0.5 rounded text-xs font-mono bg-gray-800 text-gray-300 border border-gray-700">
              Mode: {task.mode}
            </span>
          )}

          {task.awaiting_approval && (
            <span className="px-2.5 py-0.5 rounded text-xs font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/30 flex items-center gap-1">
              <ShieldAlert className="w-3.5 h-3.5" />
              Awaiting Approval
            </span>
          )}
        </div>
      </div>
    </div>
  );
};

export default TaskHeader;
