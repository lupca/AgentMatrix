import React from 'react';
import { Task, TaskStatus } from '../../types/task';
import { Bot, UserCheck, AlertTriangle, ChevronRight, ChevronLeft, ShieldAlert } from 'lucide-react';

interface KanbanCardProps {
  task: Task;
  onStatusChange?: (taskId: string, newStatus: TaskStatus) => void;
}

export const KanbanCard: React.FC<KanbanCardProps> = ({ task, onStatusChange }) => {
  const handleDragStart = (e: React.DragEvent<HTMLDivElement>) => {
    e.dataTransfer.setData('text/plain', task.id);
    e.dataTransfer.effectAllowed = 'move';
    e.currentTarget.classList.add('opacity-40');
  };

  const handleDragEnd = (e: React.DragEvent<HTMLDivElement>) => {
    e.currentTarget.classList.remove('opacity-40');
  };

  const getPriorityBadge = (priority?: string | null) => {
    if (!priority) return null;
    const prio = priority.toLowerCase();
    if (prio === 'high' || prio === 'critical') {
      return (
        <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-red-500/10 text-red-400 border border-red-500/20">
          High
        </span>
      );
    }
    if (prio === 'medium') {
      return (
        <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
          Med
        </span>
      );
    }
    return (
      <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-blue-500/10 text-blue-400 border border-blue-500/20">
        Low
      </span>
    );
  };

  const getNextStatus = (current: string): TaskStatus | null => {
    switch (current.toLowerCase()) {
      case 'todo':
        return 'dispatched';
      case 'dispatched':
        return 'in-review';
      case 'in-review':
        return 'done';
      default:
        return null;
    }
  };

  const getPrevStatus = (current: string): TaskStatus | null => {
    switch (current.toLowerCase()) {
      case 'done':
        return 'in-review';
      case 'in-review':
        return 'dispatched';
      case 'dispatched':
        return 'todo';
      default:
        return null;
    }
  };

  const nextStatus = getNextStatus(task.status);
  const prevStatus = getPrevStatus(task.status);

  return (
    <div
      draggable
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
      className="bg-gray-900 border border-gray-800 hover:border-gray-700 rounded-xl p-4 space-y-3 cursor-grab active:cursor-grabbing shadow-sm hover:shadow-md hover:bg-gray-850 transition-all group relative"
    >
      {/* Top Header Row */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center space-x-2">
          <span className="font-mono text-xs font-bold text-indigo-400 bg-indigo-950/80 px-2 py-0.5 rounded border border-indigo-800/40">
            {task.id}
          </span>
          <span className="text-[11px] font-medium text-gray-400 bg-gray-950 px-2 py-0.5 rounded border border-gray-800">
            {task.project}
          </span>
        </div>
        <div>{getPriorityBadge(task.priority)}</div>
      </div>

      {/* Title */}
      <h4 className="text-sm font-semibold text-white group-hover:text-indigo-200 transition-colors line-clamp-2">
        {task.title}
      </h4>

      {/* Approval Warning Banner */}
      {task.awaiting_approval && (
        <div className="flex items-center space-x-1.5 text-[11px] font-medium bg-amber-500/10 text-amber-300 border border-amber-500/30 px-2 py-1 rounded-md">
          <ShieldAlert className="w-3.5 h-3.5 text-amber-400 shrink-0" />
          <span className="truncate">Awaiting Four-Eyes Approval</span>
        </div>
      )}

      {/* Error / Risk Banner */}
      {task.error && (
        <div className="flex items-center space-x-1 text-[11px] bg-red-500/10 text-red-300 border border-red-500/20 px-2 py-1 rounded">
          <AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0" />
          <span className="truncate">{task.error}</span>
        </div>
      )}

      {/* Footer Info */}
      <div className="pt-2 border-t border-gray-800/60 flex items-center justify-between text-xs text-gray-400">
        <div className="flex items-center space-x-3">
          {task.current_gate && (
            <span className="text-[11px] text-gray-400 font-mono">
              gate: <strong className="text-indigo-300">{task.current_gate}</strong>
            </span>
          )}
          {task.executor && (
            <span className="flex items-center space-x-1 text-gray-300" title={`Executor: ${task.executor}`}>
              <Bot className="w-3.5 h-3.5 text-indigo-400" />
              <span className="text-[11px] truncate max-w-[80px]">{task.executor}</span>
            </span>
          )}
        </div>

        {/* Quick Shift buttons */}
        {onStatusChange && (
          <div className="flex items-center space-x-1 opacity-60 group-hover:opacity-100 transition-opacity">
            {prevStatus && (
              <button
                onClick={() => onStatusChange(task.id, prevStatus)}
                title={`Move to ${prevStatus}`}
                className="p-1 rounded hover:bg-gray-800 text-gray-400 hover:text-white"
              >
                <ChevronLeft className="w-3.5 h-3.5" />
              </button>
            )}
            {nextStatus && (
              <button
                onClick={() => onStatusChange(task.id, nextStatus)}
                title={`Move to ${nextStatus}`}
                className="p-1 rounded hover:bg-gray-800 text-gray-400 hover:text-white"
              >
                <ChevronRight className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
