import React, { useState } from 'react';
import { Bot, Calendar, ChevronDown, ChevronRight, Clock, Square, Terminal } from 'lucide-react';
import AgentOutputViewer from '../agent/AgentOutputViewer';
import RunStatusBadge from '../agent/RunStatusBadge';

export interface TaskRun {
  id: string;
  task_id: string;
  agent_id: string;
  agent_name?: string | null;
  cli: string;
  command: string;
  status: string;
  pid?: number | null;
  queued_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  timeout_seconds: number;
  exit_code?: number | null;
  result_ref?: string | null;
  error_message?: string | null;
  output_lines: number;
  output_bytes: number;
  attempt: number;
  max_attempts: number;
}

interface RunCardProps {
  run: TaskRun;
  onCancel?: (runId: string) => Promise<void> | void;
}

const ACTIVE_STATUSES = new Set(['queued', 'pending', 'running']);

function formatTimestamp(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export const RunCard: React.FC<RunCardProps> = ({ run, onCancel }) => {
  const [expanded, setExpanded] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const normalizedStatus = run.status.toLowerCase();
  const canCancel = ACTIVE_STATUSES.has(normalizedStatus) && Boolean(onCancel);

  const handleCancel = async () => {
    if (!onCancel) return;
    setCancelling(true);
    try {
      await onCancel(run.id);
    } finally {
      setCancelling(false);
    }
  };

  return (
    <article className="overflow-hidden rounded-xl border border-gray-800 bg-gray-900/60 shadow-lg">
      <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <RunStatusBadge status={run.status} />
            <span className="font-mono text-xs text-gray-500" title={run.id}>
              {run.id.slice(0, 8)}
            </span>
            {run.attempt > 1 && (
              <span className="text-xs text-gray-500">
                Attempt {run.attempt}/{run.max_attempts}
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-400">
            <span className="inline-flex items-center gap-1.5 truncate" title={run.agent_id}>
              <Bot className="h-3.5 w-3.5 shrink-0 text-purple-400" />
              {run.agent_name || run.agent_id}
            </span>
            <span className="inline-flex items-center gap-1.5">
              <Calendar className="h-3.5 w-3.5 shrink-0 text-gray-500" />
              {formatTimestamp(run.queued_at)}
            </span>
            {run.started_at && (
              <span className="inline-flex items-center gap-1.5">
                <Clock className="h-3.5 w-3.5 shrink-0 text-gray-500" />
                Started {formatTimestamp(run.started_at)}
              </span>
            )}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {canCancel && (
            <button
              type="button"
              onClick={handleCancel}
              disabled={cancelling}
              className="inline-flex items-center gap-1.5 rounded-lg border border-red-500/30 bg-red-500/10 px-2.5 py-1.5 text-xs font-semibold text-red-300 transition-colors hover:bg-red-500/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Square className="h-3 w-3 fill-current" />
              {cancelling ? 'Cancelling...' : 'Cancel'}
            </button>
          )}
          <button
            type="button"
            onClick={() => setExpanded((current) => !current)}
            aria-expanded={expanded}
            className="inline-flex items-center gap-1.5 rounded-lg border border-gray-700 bg-gray-950 px-2.5 py-1.5 text-xs font-semibold text-gray-300 transition-colors hover:border-gray-600 hover:text-white"
          >
            {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
            {expanded ? 'Hide output' : 'View output'}
          </button>
        </div>
      </div>

      {expanded && (
        <div className="space-y-3 border-t border-gray-800 bg-gray-950/30 p-4">
          <div className="flex items-center gap-2 text-xs text-gray-500">
            <Terminal className="h-3.5 w-3.5" />
            <code className="truncate" title={run.command}>{run.command}</code>
          </div>
          <AgentOutputViewer runId={run.id} />
          {run.error_message && (
            <p className="rounded-lg border border-red-500/20 bg-red-500/10 p-3 text-xs text-red-300">
              {run.error_message}
            </p>
          )}
        </div>
      )}
    </article>
  );
};

export default RunCard;
