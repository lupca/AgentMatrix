import React, { useEffect, useState, useCallback } from 'react';
import { api } from '../lib/api';
import { Project, ProjectStats } from '../types/project';
import ProjectCard from '../components/projects/ProjectCard';
import {
  FolderKanban,
  Search,
  Plus,
  RefreshCw,
  Filter,
  Layers,
  CheckCircle2,
  Clock,
  AlertCircle,
  X,
} from 'lucide-react';

export const ProjectsPage: React.FC = () => {
  const [projects, setProjects] = useState<Project[]>([]);
  const [statsMap, setStatsMap] = useState<Record<string, ProjectStats>>({});
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);

  // New project modal state
  const [showModal, setShowModal] = useState<boolean>(false);
  const [newProjectId, setNewProjectId] = useState<string>('');
  const [newProjectName, setNewProjectName] = useState<string>('');
  const [newProjectDesc, setNewProjectDesc] = useState<string>('');
  const [creating, setCreating] = useState<boolean>(false);

  const fetchProjectsData = useCallback(async (isManual = false) => {
    if (isManual) setIsRefreshing(true);
    else setLoading(true);
    setError(null);

    try {
      // 1. Fetch projects
      const projectList = await api.get<Project[]>('/projects');
      
      // 2. Fetch project stats
      let projectStats: ProjectStats[] = [];
      try {
        projectStats = await api.get<ProjectStats[]>('/stats/projects');
      } catch (err) {
        console.warn('Failed to fetch /api/stats/projects', err);
      }

      const map: Record<string, ProjectStats> = {};
      projectStats.forEach((s) => {
        map[s.project_id] = s;
      });

      if (projectList && projectList.length > 0) {
        setProjects(projectList);
        setStatsMap(map);
      } else {
        // Fallback default projects if empty response
        setProjects(getFallbackProjects());
        setStatsMap(getFallbackStats());
      }
    } catch (err: any) {
      console.warn('Error fetching projects API, using fallback data:', err);
      setError('Could not connect to /api/projects. Showing stored/fallback project list.');
      setProjects(getFallbackProjects());
      setStatsMap(getFallbackStats());
    } fontFinally: {
      setLoading(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchProjectsData();
  }, [fetchProjectsData]);

  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProjectId.trim() || !newProjectName.trim()) return;

    setCreating(true);
    try {
      const created = await api.post<Project>('/projects', {
        id: newProjectId.trim().toUpperCase(),
        name: newProjectName.trim(),
        description: newProjectDesc.trim() || null,
        status: 'active',
      });
      setProjects((prev) => [created, ...prev]);
      setShowModal(false);
      setNewProjectId('');
      setNewProjectName('');
      setNewProjectDesc('');
    } catch (err: any) {
      alert(`Failed to create project: ${err?.message || 'Unknown error'}`);
    } finally {
      setCreating(false);
    }
  };

  const getFallbackProjects = (): Project[] => [
    {
      id: 'CTV2',
      name: 'Control Tower V2 Core',
      description: 'Agentic graph orchestration engine with four-eyes verification & gate approval flows.',
      status: 'active',
      created_at: new Date().toISOString(),
    },
    {
      id: 'TOPVN-OMS',
      name: 'Order Management System',
      description: 'Multi-channel order ingestion, fulfillment dispatch & inventory sync.',
      status: 'active',
      created_at: new Date().toISOString(),
    },
    {
      id: 'TOPVN-WMS',
      name: 'Warehouse Management System',
      description: 'Stock tracking, barcode location indexing & picking strategy pipeline.',
      status: 'completed',
      created_at: new Date().toISOString(),
    },
    {
      id: 'TOPVN-PMI',
      name: 'Partner Integration Hub',
      description: 'Supplier APIs, EDI connector bridges, and webhook audit log dispatchers.',
      status: 'paused',
      created_at: new Date().toISOString(),
    },
  ];

  const getFallbackStats = (): Record<string, ProjectStats> => ({
    CTV2: {
      project_id: 'CTV2',
      project_name: 'Control Tower V2 Core',
      total_tasks: 16,
      done_tasks: 12,
      active_tasks: 4,
      by_status: { todo: 2, dispatched: 1, 'in-review': 1, done: 12 },
    },
    'TOPVN-OMS': {
      project_id: 'TOPVN-OMS',
      project_name: 'Order Management System',
      total_tasks: 10,
      done_tasks: 6,
      active_tasks: 4,
      by_status: { todo: 2, dispatched: 2, done: 6 },
    },
    'TOPVN-WMS': {
      project_id: 'TOPVN-WMS',
      project_name: 'Warehouse Management System',
      total_tasks: 8,
      done_tasks: 8,
      active_tasks: 0,
      by_status: { done: 8 },
    },
    'TOPVN-PMI': {
      project_id: 'TOPVN-PMI',
      project_name: 'Partner Integration Hub',
      total_tasks: 5,
      done_tasks: 2,
      active_tasks: 3,
      by_status: { todo: 3, done: 2 },
    },
  });

  // Filter projects based on search query & status filter
  const filteredProjects = projects.filter((p) => {
    const matchesSearch =
      p.name.toLowerCase().includes(search.toLowerCase()) ||
      p.id.toLowerCase().includes(search.toLowerCase()) ||
      (p.description && p.description.toLowerCase().includes(search.toLowerCase()));

    const matchesStatus =
      statusFilter === 'all' || p.status.toLowerCase() === statusFilter.toLowerCase();

    return matchesSearch && matchesStatus;
  });

  // Aggregated totals
  const totalProjects = projects.length;
  const activeCount = projects.filter((p) => p.status.toLowerCase() === 'active').length;
  const completedCount = projects.filter((p) => p.status.toLowerCase() === 'completed').length;

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto min-h-[calc(100vh-4rem)]">
      {/* Top Banner Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 bg-gray-900/40 p-6 rounded-2xl border border-gray-800/80 shadow-lg backdrop-blur-md">
        <div>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-indigo-500 to-purple-600 flex items-center justify-center text-white shadow-lg shadow-indigo-500/20">
              <FolderKanban className="w-5 h-5" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-white tracking-tight flex items-center gap-2">
                Projects Directory
              </h1>
              <p className="text-gray-400 text-xs mt-0.5">
                Manage project registries, multi-task workflows & execution progress.
              </p>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={() => fetchProjectsData(true)}
            disabled={isRefreshing || loading}
            className="flex items-center gap-2 px-3 py-2 rounded-xl bg-gray-800 hover:bg-gray-700 text-gray-200 font-medium text-xs border border-gray-700 transition-all disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isRefreshing ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>

          <button
            onClick={() => setShowModal(true)}
            className="flex items-center gap-2 px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-medium text-xs shadow-lg shadow-indigo-600/20 transition-all"
          >
            <Plus className="w-4 h-4" />
            <span>New Project</span>
          </button>
        </div>
      </div>

      {/* Error Alert */}
      {error && (
        <div className="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <AlertCircle className="w-4 h-4 text-amber-400 flex-shrink-0" />
            <span>{error}</span>
          </div>
          <button
            onClick={() => fetchProjectsData(true)}
            className="underline hover:text-amber-200 font-medium flex-shrink-0"
          >
            Retry
          </button>
        </div>
      )}

      {/* Project Overview Stats Summary */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
          <div>
            <p className="text-xs font-medium text-gray-400">Total Projects</p>
            <h3 className="text-2xl font-extrabold text-white mt-1 font-mono">{totalProjects}</h3>
          </div>
          <div className="w-10 h-10 rounded-xl bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
            <Layers className="w-5 h-5" />
          </div>
        </div>

        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
          <div>
            <p className="text-xs font-medium text-gray-400">Active Pipelines</p>
            <h3 className="text-2xl font-extrabold text-emerald-400 mt-1 font-mono">{activeCount}</h3>
          </div>
          <div className="w-10 h-10 rounded-xl bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center text-emerald-400">
            <Clock className="w-5 h-5" />
          </div>
        </div>

        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
          <div>
            <p className="text-xs font-medium text-gray-400">Completed Projects</p>
            <h3 className="text-2xl font-extrabold text-purple-400 mt-1 font-mono">{completedCount}</h3>
          </div>
          <div className="w-10 h-10 rounded-xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center text-purple-400">
            <CheckCircle2 className="w-5 h-5" />
          </div>
        </div>
      </div>

      {/* Filter and Search Bar */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 p-4 rounded-xl border border-gray-800/80 bg-gray-900/60 backdrop-blur-sm">
        {/* Search */}
        <div className="relative flex-1 max-w-md">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search projects by ID, name, or description..."
            className="w-full bg-gray-950/80 border border-gray-800 rounded-lg pl-9 pr-3 py-2 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-indigo-500 transition-colors"
          />
        </div>

        {/* Status Filter Tabs */}
        <div className="flex items-center gap-1.5 overflow-x-auto pb-1 sm:pb-0">
          {['all', 'active', 'completed', 'paused', 'archived'].map((st) => (
            <button
              key={st}
              onClick={() => setStatusFilter(st)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium capitalize transition-colors flex-shrink-0 ${
                statusFilter === st
                  ? 'bg-indigo-600 text-white shadow-md'
                  : 'bg-gray-950/60 text-gray-400 hover:text-gray-200 hover:bg-gray-800/50 border border-gray-800/60'
              }`}
            >
              {st}
            </button>
          ))}
        </div>
      </div>

      {/* Projects Grid */}
      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 animate-pulse">
          {[1, 2, 3, 4, 5, 6].map((i) => (
            <div key={i} className="h-60 rounded-xl bg-gray-900/60 border border-gray-800" />
          ))}
        </div>
      ) : filteredProjects.length === 0 ? (
        <div className="p-12 rounded-xl border border-gray-800/80 bg-gray-900/40 text-center space-y-3">
          <FolderKanban className="w-10 h-10 mx-auto text-gray-600" />
          <h3 className="text-base font-semibold text-gray-300">No Projects Found</h3>
          <p className="text-xs text-gray-500 max-w-sm mx-auto">
            No projects matched your search or status filter. Try clearing filters or create a new project.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {filteredProjects.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              stats={statsMap[project.id]}
            />
          ))}
        </div>
      )}

      {/* Modal to Create Project */}
      {showModal && (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-md shadow-2xl space-y-4 relative">
            <div className="flex items-center justify-between pb-3 border-b border-gray-800">
              <h3 className="text-lg font-bold text-white flex items-center gap-2">
                <FolderKanban className="w-5 h-5 text-indigo-400" />
                Register New Project
              </h3>
              <button
                onClick={() => setShowModal(false)}
                className="p-1 rounded-lg text-gray-400 hover:text-white hover:bg-gray-800"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <form onSubmit={handleCreateProject} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-gray-300 uppercase tracking-wider mb-1">
                  Project ID (Key) *
                </label>
                <input
                  type="text"
                  required
                  placeholder="e.g. CTV2, TOPVN-OMS"
                  value={newProjectId}
                  onChange={(e) => setNewProjectId(e.target.value)}
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500 font-mono"
                />
              </div>

              <div>
                <label className="block text-xs font-semibold text-gray-300 uppercase tracking-wider mb-1">
                  Project Name *
                </label>
                <input
                  type="text"
                  required
                  placeholder="e.g. Control Tower Core Engine"
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value)}
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"
                />
              </div>

              <div>
                <label className="block text-xs font-semibold text-gray-300 uppercase tracking-wider mb-1">
                  Description
                </label>
                <textarea
                  rows={3}
                  placeholder="Describe the scope and objective of this project..."
                  value={newProjectDesc}
                  onChange={(e) => setNewProjectDesc(e.target.value)}
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500 resize-none"
                />
              </div>

              <div className="pt-3 border-t border-gray-800 flex items-center justify-end gap-3">
                <button
                  type="button"
                  onClick={() => setShowModal(false)}
                  className="px-4 py-2 rounded-xl text-xs font-medium text-gray-400 hover:text-white hover:bg-gray-800"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={creating}
                  className="px-5 py-2 rounded-xl text-xs font-medium bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-600/20 disabled:opacity-50"
                >
                  {creating ? 'Creating...' : 'Create Project'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};

export default ProjectsPage;
