import React, { useEffect, useState, useCallback } from 'react';
import { api } from '../lib/api';
import { useAppStore } from '../lib/store';
import KpiCards, { KpiData } from '../components/dashboard/KpiCards';
import StatusChart, { TaskStatusCounts, GateCounts } from '../components/dashboard/StatusChart';
import ProjectCards, { ProjectProgress } from '../components/dashboard/ProjectCards';
import RecentActivity, { AuditItem } from '../components/dashboard/RecentActivity';
import { Project, ProjectStats } from '../types/project';
import { RefreshCw, LayoutDashboard, AlertCircle, Coins, Gauge } from 'lucide-react';

export interface OverviewApiResponse {
  totalTasks?: number;
  activeGates?: number;
  inReview?: number;
  completedTasks?: number;
  failedTasks?: number;
  tokenEfficiency?: number;
  tokensSaved?: number;
  tasksByStatus?: TaskStatusCounts;
  tasksByGate?: GateCounts;
  projectProgress?: ProjectProgress[];
  recentActivity?: AuditItem[];
  kpis?: KpiData;
}

export interface TokenUsageBreakdown {
  operation: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  cost_usd: number;
  average_latency_ms: number;
}

export interface TokenStatsApiResponse {
  total_calls: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cached_tokens: number;
  total_tokens: number;
  total_cost_usd: number;
  average_latency_ms: number;
  by_operation: TokenUsageBreakdown[];
}

export interface TokenComparisonApiResponse {
  baseline_input_tokens_per_cycle: number;
  cycle_count: number;
  v1_estimated_input_tokens: number;
  v2_input_tokens: number;
  v2_input_tokens_per_cycle: number;
  tokens_saved: number;
  reduction_percentage: number;
  target_reduction_percentage: number;
  target_met: boolean;
}

const formatTokens = (value: number): string => {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return value.toLocaleString();
};

export const Dashboard: React.FC = () => {
  const { user } = useAppStore();
  const [data, setData] = useState<OverviewApiResponse | null>(null);
  const [projects, setProjects] = useState<ProjectProgress[]>([]);
  const [tokenStats, setTokenStats] = useState<TokenStatsApiResponse | null>(null);
  const [tokenComparison, setTokenComparison] = useState<TokenComparisonApiResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date>(new Date());
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);

  const fetchStatsOverview = useCallback(async (isManualRefresh = false) => {
    if (isManualRefresh) {
      setIsRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);

    try {
      // Project progress is not included in /api/stats/overview. Fetch the
      // project registry separately, then combine it with task statistics.
      const [res, projectList, usage, comparison] = await Promise.all([
        api.get<OverviewApiResponse>('/stats/overview'),
        api.get<Project[]>('/projects'),
        api.get<TokenStatsApiResponse>('/stats/tokens').catch((err) => {
          console.warn('Failed to fetch /api/stats/tokens:', err);
          return null;
        }),
        api.get<TokenComparisonApiResponse>('/stats/tokens/comparison').catch((err) => {
          console.warn('Failed to fetch /api/stats/tokens/comparison:', err);
          return null;
        }),
      ]);

      let projectStats: ProjectStats[] = [];
      try {
        projectStats = await api.get<ProjectStats[]>('/stats/projects');
      } catch (err) {
        console.warn('Failed to fetch /api/stats/projects:', err);
      }

      const statsByProject = new Map(projectStats.map((stats) => [stats.project_id, stats]));
      const projectProgress: ProjectProgress[] = (projectList || []).map((project) => {
        const stats = statsByProject.get(project.id);
        const totalTasks = stats?.total_tasks ?? 0;
        const completedTasks = stats?.done_tasks ?? 0;

        return {
          id: project.id,
          name: project.name,
          description: project.description ?? undefined,
          status: project.status,
          totalTasks,
          completedTasks,
          progressPercentage: totalTasks > 0 ? (completedTasks / totalTasks) * 100 : 0,
          lastUpdated: project.updated_at ?? project.created_at ?? undefined,
        };
      });

      setData(res);
      setProjects(projectProgress);
      setTokenStats(usage);
      setTokenComparison(comparison);
      setLastRefreshed(new Date());
    } catch (err: any) {
      console.warn('Failed to fetch /api/stats/overview:', err);
      setError(err?.message || 'Failed to connect to backend overview API.');
    } finally {
      setLoading(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchStatsOverview();
  }, [fetchStatsOverview]);

  const kpiData: KpiData = {
    totalTasks: data?.totalTasks ?? data?.kpis?.totalTasks ?? 0,
    activeGates: data?.activeGates ?? data?.kpis?.activeGates ?? 0,
    inReview: data?.inReview ?? data?.kpis?.inReview ?? 0,
    completedTasks: data?.completedTasks ?? data?.kpis?.completedTasks ?? 0,
    failedTasks: data?.failedTasks ?? data?.kpis?.failedTasks ?? 0,
    tokenEfficiency:
      tokenComparison?.reduction_percentage
      ?? data?.tokenEfficiency
      ?? data?.kpis?.tokenEfficiency
      ?? 0,
    tokensSaved:
      tokenComparison?.tokens_saved
      ?? data?.tokensSaved
      ?? data?.kpis?.tokensSaved
      ?? 0,
  };

  const maxOperationTokens = Math.max(
    1,
    ...(tokenStats?.by_operation || []).map((item) => item.total_tokens),
  );

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto min-h-[calc(100vh-4rem)]">
      {/* Top Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 bg-gray-900/40 p-6 rounded-2xl border border-gray-800/80 shadow-lg backdrop-blur-md">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold text-gray-100 tracking-tight flex items-center gap-2">
              <LayoutDashboard className="w-6 h-6 text-indigo-400" />
              Welcome back, {user?.name || 'Operator'}
            </h1>
            <span className="text-xs px-2.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 font-medium flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
              Live Pipeline
            </span>
          </div>
          <p className="text-gray-400 text-sm mt-1">
            Control Tower V2 task orchestration, gate approvals & LLM token analytics.
          </p>
        </div>

        <div className="flex items-center gap-3 self-start md:self-auto">
          <span className="text-xs text-gray-500 font-mono hidden sm:inline-block">
            Updated: {lastRefreshed.toLocaleTimeString()}
          </span>
          <button
            onClick={() => fetchStatsOverview(true)}
            disabled={isRefreshing || loading}
            className="flex items-center gap-2 px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 active:bg-indigo-700 text-white font-medium text-xs shadow-lg shadow-indigo-600/20 transition-all disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isRefreshing ? 'animate-spin' : ''}`} />
            <span>{isRefreshing ? 'Refreshing...' : 'Refresh Stats'}</span>
          </button>
        </div>
      </div>

      {/* Error Alert Banner */}
      {error && (
        <div className="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <AlertCircle className="w-4 h-4 text-amber-400 flex-shrink-0" />
            <span>{error}</span>
          </div>
          <button
            onClick={() => fetchStatsOverview(true)}
            className="underline hover:text-amber-200 font-medium flex-shrink-0"
          >
            Retry Connection
          </button>
        </div>
      )}

      {/* KPI Cards Component */}
      <KpiCards data={kpiData} loading={loading} />

      {/* Measured token telemetry */}
      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm">
        <div className="flex flex-col gap-4 border-b border-gray-800/80 pb-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-base font-semibold text-gray-100">
              <Coins className="h-4 w-4 text-purple-400" />
              LLM Token Telemetry
            </h2>
            <p className="mt-0.5 text-xs text-gray-400">
              Measured provider usage compared with the V1 baseline of 3,575 input tokens per cycle
            </p>
          </div>
          <span
            className={`inline-flex w-fit items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold ${
              tokenComparison?.target_met
                ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
                : 'border-amber-500/30 bg-amber-500/10 text-amber-400'
            }`}
          >
            <Gauge className="h-3.5 w-3.5" />
            {tokenComparison?.target_met ? '80% target met' : '80% target pending'}
          </span>
        </div>

        {loading ? (
          <div className="mt-5 grid grid-cols-2 gap-4 md:grid-cols-4">
            {[1, 2, 3, 4].map((item) => (
              <div key={item} className="h-20 animate-pulse rounded-lg bg-gray-800/60" />
            ))}
          </div>
        ) : (
          <>
            <div className="mt-5 grid grid-cols-2 gap-4 lg:grid-cols-5">
              <div className="rounded-lg border border-purple-500/20 bg-purple-500/5 p-4">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">Total tokens</p>
                <p className="mt-1 text-2xl font-bold text-gray-100">
                  {formatTokens(tokenStats?.total_tokens ?? 0)}
                </p>
                <p className="mt-1 text-xs text-purple-400">{tokenStats?.total_calls ?? 0} LLM calls</p>
              </div>
              <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-4">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">Input</p>
                <p className="mt-1 text-xl font-bold text-gray-200">
                  {formatTokens(tokenStats?.total_input_tokens ?? 0)}
                </p>
                <p className="mt-1 text-xs text-gray-500">
                  {formatTokens(tokenStats?.total_cached_tokens ?? 0)} cached
                </p>
              </div>
              <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-4">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">Output</p>
                <p className="mt-1 text-xl font-bold text-gray-200">
                  {formatTokens(tokenStats?.total_output_tokens ?? 0)}
                </p>
                <p className="mt-1 text-xs text-gray-500">
                  {Math.round(tokenStats?.average_latency_ms ?? 0).toLocaleString()} ms avg
                </p>
              </div>
              <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-4">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">Estimated cost</p>
                <p className="mt-1 text-xl font-bold text-gray-200">
                  ${(tokenStats?.total_cost_usd ?? 0).toFixed(4)}
                </p>
                <p className="mt-1 text-xs text-gray-500">Provider list pricing</p>
              </div>
              <div className="col-span-2 rounded-lg border border-indigo-500/20 bg-indigo-500/5 p-4 lg:col-span-1">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">V1 reduction</p>
                <p className={`mt-1 text-2xl font-bold ${
                  (tokenComparison?.reduction_percentage ?? 0) >= 80
                    ? 'text-emerald-400'
                    : 'text-indigo-300'
                }`}>
                  {(tokenComparison?.reduction_percentage ?? 0).toFixed(1)}%
                </p>
                <p className="mt-1 text-xs text-gray-500">
                  {tokenComparison?.cycle_count ?? 0} measured cycles
                </p>
              </div>
            </div>

            <div className="mt-5">
              <div className="mb-3 flex items-center justify-between">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400">
                  Usage by operation
                </h3>
                <span className="text-[11px] text-gray-500">Input + output tokens</span>
              </div>
              {(tokenStats?.by_operation?.length ?? 0) === 0 ? (
                <div className="rounded-lg border border-dashed border-gray-800 p-5 text-center text-xs text-gray-500">
                  No LLM usage has been recorded yet.
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  {tokenStats?.by_operation.map((operation) => (
                    <div
                      key={operation.operation}
                      className="rounded-lg border border-gray-800/80 bg-gray-950/40 p-3"
                    >
                      <div className="mb-2 flex items-center justify-between text-xs">
                        <span className="font-medium capitalize text-gray-300">
                          {operation.operation.replaceAll('_', ' ')}
                        </span>
                        <span className="font-mono text-gray-400">
                          {formatTokens(operation.total_tokens)}
                        </span>
                      </div>
                      <div className="h-1.5 overflow-hidden rounded-full bg-gray-800">
                        <div
                          className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-purple-500"
                          style={{ width: `${(operation.total_tokens / maxOperationTokens) * 100}%` }}
                        />
                      </div>
                      <p className="mt-2 text-[11px] text-gray-500">
                        {operation.calls} call{operation.calls === 1 ? '' : 's'} · {formatTokens(operation.input_tokens)} in · {formatTokens(operation.output_tokens)} out
                      </p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {/* Main Grid: Status Chart & Project Progress Cards */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-1">
          <StatusChart
            statusCounts={data?.tasksByStatus}
            gateCounts={data?.tasksByGate}
            loading={loading}
          />
        </div>
        <div className="lg:col-span-2">
          <ProjectCards
            projects={projects}
            loading={loading}
          />
        </div>
      </div>

      {/* Recent Activity Audit Log */}
      <RecentActivity
        activities={data?.recentActivity}
        loading={loading}
      />
    </div>
  );
};

export default Dashboard;
