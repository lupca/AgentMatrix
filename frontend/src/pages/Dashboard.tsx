import React, { useEffect, useState, useCallback } from 'react';
import { api } from '../lib/api';
import { useAppStore } from '../lib/store';
import KpiCards, { KpiData } from '../components/dashboard/KpiCards';
import StatusChart, { TaskStatusCounts, GateCounts } from '../components/dashboard/StatusChart';
import ProjectCards, { ProjectProgress } from '../components/dashboard/ProjectCards';
import RecentActivity, { AuditItem } from '../components/dashboard/RecentActivity';
import { Project, ProjectStats } from '../types/project';
import { RefreshCw, LayoutDashboard, AlertCircle, ShieldAlert, Sparkles } from 'lucide-react';

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

export const Dashboard: React.FC = () => {
  const { user } = useAppStore();
  const [data, setData] = useState<OverviewApiResponse | null>(null);
  const [projects, setProjects] = useState<ProjectProgress[]>([]);
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
      const [res, projectList] = await Promise.all([
        api.get<OverviewApiResponse>('/stats/overview'),
        api.get<Project[]>('/projects'),
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
    tokenEfficiency: data?.tokenEfficiency ?? data?.kpis?.tokenEfficiency ?? 0,
    tokensSaved: data?.tokensSaved ?? data?.kpis?.tokensSaved ?? 0,
  };

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto min-h-[calc(100vh-4rem)]">
      {/* Top Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 bg-gray-900/40 p-6 rounded-2xl border border-gray-800/80 shadow-lg backdrop-blur-md">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold text-white tracking-tight flex items-center gap-2">
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
