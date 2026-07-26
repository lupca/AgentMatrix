import React, { useState } from 'react';
import { Task, TaskStatus } from '../../types/task';
import { KanbanCard } from './KanbanCard';

export interface ColumnDefinition {
  id: TaskStatus;
  title: string;
  color: string; // e.g. border/text color class
  badgeBg: string;
  description: string;
  icon?: React.ReactNode;
}

interface KanbanColumnProps {
  column: ColumnDefinition;
  tasks: Task[];
  onDropTask: (taskId: string, targetStatus: TaskStatus) => void;
  onStatusChange?: (taskId: string, newStatus: TaskStatus) => void;
}

export const KanbanColumn: React.FC<KanbanColumnProps> = ({
  column,
  tasks,
  onDropTask,
  onStatusChange,
}) => {
  const [isDragOver, setIsDragOver] = useState(false);

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (!isDragOver) setIsDragOver(true);
  };

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragOver(false);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragOver(false);
    const taskId = e.dataTransfer.getData('text/plain');
    if (taskId) {
      onDropTask(taskId, column.id);
    }
  };

  return (
    <div
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      className={`flex flex-col rounded-2xl bg-gray-950/70 border ${
        isDragOver ? 'border-indigo-500 bg-indigo-950/20 ring-2 ring-indigo-500/20' : 'border-gray-800/80'
      } transition-all duration-200 min-h-[500px] shadow-sm`}
    >
      {/* Column Header */}
      <div className="p-4 border-b border-gray-800/80 flex items-center justify-between">
        <div className="flex items-center space-x-2.5">
          {column.icon}
          <div>
            <h3 className="text-sm font-bold text-gray-200 tracking-wide flex items-center gap-2">
              <span>{column.title}</span>
            </h3>
            <p className="text-[11px] text-gray-500">{column.description}</p>
          </div>
        </div>
        <span
          className={`px-2.5 py-0.5 rounded-full text-xs font-semibold ${column.badgeBg}`}
        >
          {tasks.length}
        </span>
      </div>

      {/* Cards Container */}
      <div className="p-3 flex-1 space-y-3 overflow-y-auto max-h-[calc(100vh-260px)]">
        {tasks.length === 0 ? (
          <div className="h-40 border border-dashed border-gray-800/60 rounded-xl flex flex-col items-center justify-center text-center p-4">
            <span className="text-xs text-gray-600 font-medium">No tasks in {column.title}</span>
            <span className="text-[11px] text-gray-700 mt-1">Drag a task card here</span>
          </div>
        ) : (
          tasks.map((task) => (
            <KanbanCard key={task.id} task={task} onStatusChange={onStatusChange} />
          ))
        )}
      </div>
    </div>
  );
};
