import React from 'react';
import { ChevronRight } from 'lucide-react';
import { ContextLevel } from '../../hooks/useSessions';

interface ContextIndicatorProps {
  level: ContextLevel;
  projectName?: string | null;
  taskId?: string | null;
  taskTitle?: string | null;
  className?: string;
}

interface Crumb {
  icon: string;
  label: string;
}

export const ContextIndicator: React.FC<ContextIndicatorProps> = ({
  level,
  projectName,
  taskId,
  taskTitle,
  className = '',
}) => {
  const crumbs: Crumb[] = [{ icon: '🌍', label: 'Global' }];

  if (level === 'project' || level === 'task') {
    crumbs.push({ icon: '📁', label: projectName || 'Project' });
  }
  if (level === 'task') {
    crumbs.push({ icon: '📋', label: taskTitle || taskId || 'Task' });
  }

  return (
    <div
      aria-label="Chat context"
      className={`flex items-center gap-1.5 text-[11px] font-mono text-gray-400 ${className}`}
    >
      {crumbs.map((crumb, index) => (
        <React.Fragment key={`${crumb.icon}-${index}`}>
          {index > 0 && <ChevronRight className="w-3 h-3 shrink-0 text-gray-600" />}
          <span className="flex max-w-[140px] items-center gap-1 truncate">
            <span aria-hidden="true">{crumb.icon}</span>
            <span className="truncate">{crumb.label}</span>
          </span>
        </React.Fragment>
      ))}
    </div>
  );
};

export default ContextIndicator;
