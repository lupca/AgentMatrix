import React from 'react';
import { Task, TaskStatus } from '../../types/task';
import { KanbanColumn, ColumnDefinition } from './KanbanColumn';
import { Clock, PlayCircle, UserCheck, CheckCircle2 } from 'lucide-react';

interface KanbanBoardProps {
  tasks: Task[];
  loading?: boolean;
  onStatusChange: (taskId: string, newStatus: TaskStatus) => Promise<void> | void;
}

const COLUMNS: ColumnDefinition[] = [
  {
    id: 'todo',
    title: 'To Do',
    color: 'text-gray-300',
    badgeBg: 'bg-gray-800 text-gray-300 border border-gray-700',
    description: 'Pending execution',
    icon: <Clock className="w-4 h-4 text-gray-400" />,
  },
  {
    id: 'dispatched',
    title: 'Dispatched',
    color: 'text-amber-400',
    badgeBg: 'bg-amber-500/10 text-amber-400 border border-amber-500/30',
    description: 'Active agent execution',
    icon: <PlayCircle className="w-4 h-4 text-amber-400 animate-pulse" />,
  },
  {
    id: 'in-review',
    title: 'In Review',
    color: 'text-purple-400',
    badgeBg: 'bg-purple-500/10 text-purple-400 border border-purple-500/30',
    description: 'Four-Eyes check',
    icon: <UserCheck className="w-4 h-4 text-purple-400" />,
  },
  {
    id: 'done',
    title: 'Done',
    color: 'text-emerald-400',
    badgeBg: 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30',
    description: 'Completed tasks',
    icon: <CheckCircle2 className="w-4 h-4 text-emerald-400" />,
  },
];

export const KanbanBoard: React.FC<KanbanBoardProps> = ({
  tasks,
  loading = false,
  onStatusChange,
}) => {
  const handleDropTask = (taskId: string, targetStatus: TaskStatus) => {
    const task = tasks.find((t) => t.id === taskId);
    if (task && task.status !== targetStatus) {
      onStatusChange(taskId, targetStatus);
    }
  };

  if (loading) {
    return (
      <div className="p-12 text-center bg-gray-900/60 border border-gray-800 rounded-xl">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500 mb-3"></div>
        <p className="text-gray-400 text-sm">Loading Kanban board...</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 items-start">
      {COLUMNS.map((column) => {
        const columnTasks = tasks.filter(
          (t) => (t.status || '').toLowerCase() === column.id
        );
        return (
          <KanbanColumn
            key={column.id}
            column={column}
            tasks={columnTasks}
            onDropTask={handleDropTask}
            onStatusChange={onStatusChange}
          />
        );
      })}
    </div>
  );
};
