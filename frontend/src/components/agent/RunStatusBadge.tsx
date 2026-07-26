import React from 'react';

export type RunStatus =
  | 'queued'
  | 'pending'
  | 'running'
  | 'success'
  | 'done'
  | 'failed'
  | 'timeout'
  | 'cancelled'
  | 'retrying'
  | string;

interface RunStatusBadgeProps {
  status: RunStatus;
  className?: string;
}

const STATUS_STYLES: Record<string, string> = {
  queued: 'bg-gray-800 text-gray-300 border-gray-700',
  pending: 'bg-gray-800 text-gray-300 border-gray-700',
  running: 'bg-blue-500/10 text-blue-400 border-blue-500/30',
  success: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
  done: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
  failed: 'bg-red-500/10 text-red-400 border-red-500/30',
  timeout: 'bg-red-500/10 text-red-400 border-red-500/30',
  cancelled: 'bg-red-500/10 text-red-400 border-red-500/30',
  retrying: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
};

function displayStatus(status: string): string {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

export const RunStatusBadge: React.FC<RunStatusBadgeProps> = ({
  status,
  className = '',
}) => {
  const normalizedStatus = status.toLowerCase();
  const styles = STATUS_STYLES[normalizedStatus] ?? STATUS_STYLES.queued;
  const isRunning = normalizedStatus === 'running';

  return (
    <span
      data-status={normalizedStatus}
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-semibold ${styles} ${className}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full bg-current ${isRunning ? 'animate-pulse' : ''}`} />
      {displayStatus(normalizedStatus)}
    </span>
  );
};

export default RunStatusBadge;
