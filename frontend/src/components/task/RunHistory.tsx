import React, { useCallback, useEffect, useState } from 'react';
import { History, Loader2, RefreshCw } from 'lucide-react';
import { api } from '../../lib/api';
import { showSuccess } from '../../lib/toast';
import RunCard, { TaskRun } from './RunCard';

interface RunHistoryProps {
  taskId: string;
  refreshKey?: number;
}

const ACTIVE_STATUSES = new Set(['queued', 'pending', 'running', 'retrying']);

function sortMostRecentFirst(runs: TaskRun[]): TaskRun[] {
  return [...runs].sort((left, right) => {
    const leftTime = new Date(left.queued_at || 0).getTime();
    const rightTime = new Date(right.queued_at || 0).getTime();
    return rightTime - leftTime;
  });
}

export const RunHistory: React.FC<RunHistoryProps> = ({ taskId, refreshKey = 0 }) => {
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchRuns = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    else setLoading(true);
    setError(null);

    try {
      const response = await api.get<TaskRun[]>(`/tasks/${taskId}/runs`);
      setRuns(sortMostRecentFirst(response || []));
    } catch (fetchError: any) {
      console.error(`Failed to load runs for task ${taskId}:`, fetchError);
      setError(fetchError?.message || 'Unable to load run history.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [taskId]);

  useEffect(() => {
    fetchRuns();
  }, [fetchRuns, refreshKey]);

  useEffect(() => {
    if (!runs.some((run) => ACTIVE_STATUSES.has(run.status.toLowerCase()))) return undefined;

    const interval = window.setInterval(() => fetchRuns(true), 5_000);
    return () => window.clearInterval(interval);
  }, [fetchRuns, runs]);

  const handleCancel = async (runId: string) => {
    await api.post(`/runs/${runId}/cancel`);
    setRuns((currentRuns) =>
      currentRuns.map((run) =>
        run.id === runId
          ? { ...run, status: 'cancelled', error_message: 'Cancelled by user' }
          : run,
      ),
    );
    showSuccess('Run cancelled.');
  };

  return (
    <section className="space-y-4 rounded-2xl border border-gray-800 bg-gray-900/60 p-5 shadow-xl backdrop-blur-md">
      <div className="flex items-center justify-between gap-3 border-b border-gray-800 pb-3">
        <h2 className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-gray-300">
          <History className="h-4 w-4 text-indigo-400" />
          Run History
          {!loading && <span className="font-mono text-gray-500">({runs.length})</span>}
        </h2>
        <button
          type="button"
          onClick={() => fetchRuns(true)}
          disabled={loading || refreshing}
          className="inline-flex items-center gap-1.5 rounded-lg border border-gray-800 bg-gray-950 px-2.5 py-1.5 text-xs font-medium text-gray-400 transition-colors hover:text-gray-100 disabled:cursor-not-allowed disabled:opacity-50"
          title="Refresh run history"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center gap-2 py-8 text-xs text-gray-500">
          <Loader2 className="h-4 w-4 animate-spin text-indigo-400" />
          Loading runs...
        </div>
      ) : error ? (
        <div className="space-y-3 rounded-lg border border-red-500/20 bg-red-500/10 p-4 text-xs text-red-300">
          <p>{error}</p>
          <button
            type="button"
            onClick={() => fetchRuns(true)}
            className="rounded-md border border-red-500/30 px-2.5 py-1.5 font-semibold text-red-200 hover:bg-red-500/10"
          >
            Try again
          </button>
        </div>
      ) : runs.length === 0 ? (
        <p className="py-8 text-center text-xs text-gray-500">No runs have been dispatched for this task yet.</p>
      ) : (
        <div className="space-y-3">
          {runs.map((run) => (
            <RunCard key={run.id} run={run} onCancel={handleCancel} />
          ))}
        </div>
      )}
    </section>
  );
};

export default RunHistory;
