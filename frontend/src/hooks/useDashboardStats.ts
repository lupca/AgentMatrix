import { useState, useCallback } from 'react';
import { api } from '../lib/api';
import { ProjectProgress } from '../components/dashboard/ProjectCards';
import { Project, ProjectStats } from '../types/project';
import {
  OverviewApiResponse,
  TokenStatsApiResponse,
  TokenComparisonApiResponse,
} from '../pages/Dashboard';

export interface DashboardStatsState {
  data: OverviewApiResponse | null;
  projects: ProjectProgress[];
  tokenStats: TokenStatsApiResponse | null;
  tokenComparison: TokenComparisonApiResponse | null;
  loading: boolean;
  error: string | null;
  lastRefreshed: Date;
  isRefreshing: boolean;
}

export interface UseDashboardStatsReturn extends DashboardStatsState {
  refresh: (isManual?: boolean) => Promise<void>;
}

export function useDashboardStats(): UseDashboardStatsReturn {
  const [data, setData] = useState<OverviewApiResponse | null>(null);
  const [projects, setProjects] = useState<ProjectProgress[]>([]);
  const [tokenStats, setTokenStats] = useState<TokenStatsApiResponse | null>(null);
  const [tokenComparison, setTokenComparison] = useState<TokenComparisonApiResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date>(new Date());
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);

  const refresh = useCallback(async (isManualRefresh = false) => {
    if (isManualRefresh) {
      setIsRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);

    try {
      const [res, projectList, usage, comparison, projectStats] = await Promise.all([
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
        api.get<ProjectStats[]>('/stats/projects').catch((err) => {
          console.warn('Failed to fetch /api/stats/projects:', err);
          return [] as ProjectStats[];
        }),
      ]);

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

  return {
    data,
    projects,
    tokenStats,
    tokenComparison,
    loading,
    error,
    lastRefreshed,
    isRefreshing,
    refresh,
  };
}
