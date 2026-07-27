import React, { useEffect, useState, useCallback } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { Project } from '../types/project';
import { Task } from '../types/task';
import {
  ArrowLeft,
  FolderKanban,
  CheckCircle2,
  Clock,
  AlertCircle,
  Search,
  Filter,
  Layers,
  ChevronRight,
  ShieldCheck,
  Cpu,
  Calendar,
  ExternalLink,
  Settings,
} from 'lucide-react';
import { ProjectSettingsModal } from '../components/projects/ProjectSettingsModal';

export const ProjectDetailPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [search, setSearch] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);

  const fetchProjectDetail = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);

    try {
      // Fetch project by ID
      const projData = await api.get<Project>(`/projects/${id}`);

      // Fetch tasks for this project
      let taskList: Task[] = [];
      try {
        taskList = await api.get<Task[]>(`/tasks?project=${id}`);
      } catch (err) {
        console.warn(`Could not fetch /api/tasks?project=${id}`, err);
      }

      setProject(projData);
      setTasks(taskList || []);
    } catch (err: any) {
      console.warn('Error fetching project detail:', err);
      setError(err?.message || `Failed to load project '${id}'.`);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    fetchProjectDetail();
  }, [fetchProjectDetail]);

  const handleSaveSettings = async (updatedProject: Partial<Project>) => {
    if (!id) return;
    try {
      const result = await api.patch<Project>(`/projects/${id}`, updatedProject);
      setProject(result);
    } catch (err: any) {
      throw new Error(err.message || 'Failed to update project');
    }
  };

  // Filter tasks
  const filteredTasks = tasks.filter((t) => {
    const matchesSearch =
      t.title.toLowerCase().includes(search.toLowerCase()) ||
      t.id.toLowerCase().includes(search.toLowerCase()) ||
      (t.executor && t.executor.toLowerCase().includes(search.toLowerCase())) ||
      (t.reviewer && t.reviewer.toLowerCase().includes(search.toLowerCase()));

    const matchesStatus =
      statusFilter === 'all' || t.status.toLowerCase() === statusFilter.toLowerCase();

    return matchesSearch && matchesStatus;
  });

  const totalTasks = tasks.length;
  const completedTasks = tasks.filter(
    (t) => t.status === 'done' || t.status === 'completed' || t.status === 'passed'
  ).length;
  const activeTasks = totalTasks - completedTasks;
  const progressPct = totalTasks > 0 ? Math.round((completedTasks / totalTasks) * 100) : 0;

  const getStatusBadge = (status: string) => {
    switch (status.toLowerCase()) {
      case 'done':
      case 'completed':
      case 'passed':
        return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
      case 'in-review':
      case 'review':
        return 'bg-purple-500/10 text-purple-400 border-purple-500/30';
      case 'dispatched':
      case 'in_progress':
        return 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30';
      case 'todo':
        return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
      case 'failed':
      case 'cancelled':
        return 'bg-rose-500/10 text-rose-400 border-rose-500/30';
      default:
        return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
    }
  };

  const getPriorityBadge = (priority?: string | null) => {
    switch ((priority || '').toUpperCase()) {
      case 'P0':
        return 'bg-rose-500/10 text-rose-400 border-rose-500/30';
      case 'P1':
        return 'bg-amber-500/10 text-amber-400 border-amber-500/30';
      case 'P2':
        return 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30';
      default:
        return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
    }
  };

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto min-h-[calc(100vh-4rem)]">
      {/* Top Navigation & Header */}
      <div className="space-y-4">
        <button
          onClick={() => navigate('/projects')}
          className="inline-flex items-center gap-2 text-xs font-medium text-gray-400 hover:text-gray-100 transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          <span>Back to Projects Directory</span>
        </button>

        {/* Error Alert Banner */}
        {error && (
          <div className="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <AlertCircle className="w-4 h-4 text-amber-400 flex-shrink-0" />
              <span>{error}</span>
            </div>
            <button
              onClick={() => fetchProjectDetail()}
              className="underline hover:text-amber-200 font-medium flex-shrink-0"
            >
              Retry
            </button>
          </div>
        )}

        {loading ? (
          <div className="h-32 bg-gray-900/60 rounded-2xl border border-gray-800 animate-pulse" />
        ) : (
          <div className="bg-gray-900/40 p-6 rounded-2xl border border-gray-800/80 shadow-lg backdrop-blur-md space-y-4">
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
              <div className="flex items-start gap-3">
                <div className="w-12 h-12 rounded-xl bg-gradient-to-tr from-indigo-500/20 to-purple-600/20 border border-indigo-500/30 flex items-center justify-center text-indigo-400 shadow-md">
                  <FolderKanban className="w-6 h-6" />
                </div>
                <div>
                  <div className="flex items-center gap-3">
                    <span className="text-xs font-mono font-bold text-indigo-400 uppercase tracking-wide">
                      {project?.id}
                    </span>
                    <span
                      className={`text-xs font-medium px-2.5 py-0.5 rounded-full border capitalize ${getStatusBadge(
                        project?.status || 'active'
                      )}`}
                    >
                      {project?.status}
                    </span>
                  </div>
                  <h1 className="text-2xl font-bold text-gray-100 tracking-tight mt-1">
                    {project?.name}
                  </h1>
                </div>
              </div>

              <div className="flex items-center gap-3 text-xs text-gray-400">
                <Calendar className="w-4 h-4 text-gray-500" />
                <span>Created: {new Date(project?.created_at || Date.now()).toLocaleDateString()}</span>
                <button
                  onClick={() => setIsSettingsOpen(true)}
                  className="ml-2 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-200 transition-colors"
                >
                  <Settings className="w-3.5 h-3.5" />
                  <span>Settings</span>
                </button>
              </div>
            </div>

            {project?.description && (
              <p className="text-sm text-gray-300 max-w-3xl leading-relaxed border-t border-gray-800/60 pt-3">
                {project.description}
              </p>
            )}
          </div>
        )}
      </div>

      {/* KPI Cards for Project */}
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm">
          <p className="text-xs font-medium text-gray-400">Total Project Tasks</p>
          <h3 className="text-2xl font-extrabold text-gray-100 mt-1 font-mono">{totalTasks}</h3>
        </div>

        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm">
          <p className="text-xs font-medium text-gray-400">Completed Tasks</p>
          <h3 className="text-2xl font-extrabold text-emerald-400 mt-1 font-mono">{completedTasks}</h3>
        </div>

        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm">
          <p className="text-xs font-medium text-gray-400">Active / Pending Tasks</p>
          <h3 className="text-2xl font-extrabold text-indigo-400 mt-1 font-mono">{activeTasks}</h3>
        </div>

        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm">
          <div className="flex items-center justify-between">
            <p className="text-xs font-medium text-gray-400">Progress Percentage</p>
            <span className="text-xs font-mono text-purple-400 font-semibold">{progressPct}%</span>
          </div>
          <div className="w-full h-2 bg-gray-950 rounded-full overflow-hidden border border-gray-800 mt-3">
            <div
              style={{ width: `${progressPct}%` }}
              className="h-full bg-gradient-to-r from-indigo-500 to-emerald-400 rounded-full"
            />
          </div>
        </div>
      </div>

      {/* Task List Header & Controls */}
      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-gray-800/80">
          <div>
            <h2 className="text-base font-semibold text-gray-100 flex items-center gap-2">
              <Layers className="w-4 h-4 text-indigo-400" />
              Project Task Execution Queue
            </h2>
            <p className="text-xs text-gray-400 mt-0.5">
              Tasks assigned to {project?.id || 'this project'} with executor & gate statuses
            </p>
          </div>

          <div className="relative w-full sm:w-72">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search tasks or agents..."
              className="w-full bg-gray-950/80 border border-gray-800 rounded-lg pl-9 pr-3 py-1.5 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-indigo-500 transition-colors"
            />
          </div>
        </div>

        {/* Filter Pills */}
        <div className="flex items-center gap-2 overflow-x-auto pb-1">
          {['all', 'todo', 'dispatched', 'in-review', 'done'].map((st) => (
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

        {/* Tasks Table */}
        {filteredTasks.length === 0 ? (
          <div className="py-12 text-center text-gray-500 text-sm">
            No tasks found for project {project?.id} matching your filter criteria.
          </div>
        ) : (
          <div className="overflow-x-auto border border-gray-800/80 rounded-xl">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="bg-gray-950/80 text-gray-400 border-b border-gray-800/80 font-medium">
                  <th className="p-3">Task ID</th>
                  <th className="p-3">Title</th>
                  <th className="p-3">Status</th>
                  <th className="p-3">Gate</th>
                  <th className="p-3">Priority / Risk</th>
                  <th className="p-3">Executor</th>
                  <th className="p-3">Reviewer</th>
                  <th className="p-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/60 text-gray-300">
                {filteredTasks.map((task) => (
                  <tr
                    key={task.id}
                    className="hover:bg-gray-800/30 transition-colors group"
                  >
                    <td className="p-3 font-mono font-semibold text-indigo-400 whitespace-nowrap">
                      {task.id}
                    </td>
                    <td className="p-3 font-medium text-gray-100 max-w-md line-clamp-1">
                      {task.title}
                    </td>
                    <td className="p-3 whitespace-nowrap">
                      <span
                        className={`px-2 py-0.5 rounded-full border text-[11px] font-medium capitalize ${getStatusBadge(
                          task.status
                        )}`}
                      >
                        {task.status}
                      </span>
                    </td>
                    <td className="p-3 whitespace-nowrap">
                      <span className="px-2 py-0.5 rounded bg-gray-800 text-gray-300 font-mono text-[10px] uppercase">
                        {task.current_gate || 'spec'}
                      </span>
                    </td>
                    <td className="p-3 whitespace-nowrap space-x-1">
                      {task.priority && (
                        <span
                          className={`px-2 py-0.5 rounded border text-[10px] font-mono ${getPriorityBadge(
                            task.priority
                          )}`}
                        >
                          {task.priority}
                        </span>
                      )}
                      {task.risk && (
                        <span className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 text-[10px]">
                          {task.risk}
                        </span>
                      )}
                    </td>
                    <td className="p-3 whitespace-nowrap">
                      {task.executor ? (
                        <Link
                          to={`/agents/${task.executor}`}
                          className="inline-flex items-center gap-1 text-purple-300 hover:underline font-mono text-[11px]"
                        >
                          <Cpu className="w-3 h-3 text-purple-400" />
                          {task.executor}
                        </Link>
                      ) : (
                        <span className="text-gray-600 font-italic">Unassigned</span>
                      )}
                    </td>
                    <td className="p-3 whitespace-nowrap">
                      {task.reviewer ? (
                        <Link
                          to={`/agents/${task.reviewer}`}
                          className="inline-flex items-center gap-1 text-blue-300 hover:underline font-mono text-[11px]"
                        >
                          <ShieldCheck className="w-3 h-3 text-blue-400" />
                          {task.reviewer}
                        </Link>
                      ) : (
                        <span className="text-gray-600 font-italic">None</span>
                      )}
                    </td>
                    <td className="p-3 text-right whitespace-nowrap">
                      <Link
                        to={`/tasks/${task.id}`}
                        className="inline-flex items-center gap-1 text-xs text-indigo-400 hover:text-indigo-300 font-medium"
                      >
                        <span>View</span>
                        <ExternalLink className="w-3 h-3" />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      
      {project && (
        <ProjectSettingsModal
          project={project}
          isOpen={isSettingsOpen}
          onClose={() => setIsSettingsOpen(false)}
          onSave={handleSaveSettings}
        />
      )}
    </div>
  );
};

export default ProjectDetailPage;
