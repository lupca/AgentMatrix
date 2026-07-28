import React from 'react';
import { cn } from '@/lib/utils';

export interface StatusBadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  status?: string;
  size?: 'sm' | 'md' | 'lg';
  children?: React.ReactNode;
}

export const getStatusColorClass = (status: string = ''): string => {
  const s = status.toLowerCase().trim();
  switch (s) {
    case 'done':
    case 'completed':
    case 'passed':
    case 'success':
    case 'active':
      return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
    case 'in-review':
    case 'in_review':
    case 'review':
      return 'bg-purple-500/10 text-purple-400 border-purple-500/30';
    case 'dispatched':
    case 'in_progress':
    case 'in-progress':
    case 'busy':
    case 'running':
      return 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30';
    case 'todo':
    case 'idle':
    case 'queued':
    case 'pending':
      return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
    case 'failed':
    case 'cancelled':
    case 'error':
    case 'offline':
      return 'bg-rose-500/10 text-rose-400 border-rose-500/30';
    case 'warning':
    case 'paused':
    case 'archived':
      return 'bg-amber-500/10 text-amber-400 border-amber-500/30';
    default:
      return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
  }
};

export const StatusBadge = React.forwardRef<HTMLSpanElement, StatusBadgeProps>(
  ({ className, status, size = 'sm', children, ...props }, ref) => {
    const statusText = children || status || '';
    const colorClass = getStatusColorClass(typeof status === 'string' ? status : String(statusText));

    const sizeClasses = {
      sm: 'px-2.5 py-0.5 text-xs',
      md: 'px-3 py-1 text-xs',
      lg: 'px-3.5 py-1.5 text-sm',
    };

    return (
      <span
        ref={ref}
        className={cn(
          'inline-flex items-center font-medium rounded-full border capitalize transition-colors',
          sizeClasses[size],
          colorClass,
          className
        )}
        {...props}
      >
        {statusText}
      </span>
    );
  }
);

StatusBadge.displayName = 'StatusBadge';
