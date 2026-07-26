import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api';
import { Task, TaskStatus } from '../types/task';
import { KanbanBoard } from '../components/kanban/KanbanBoard';
import { LayoutGrid, Table, RefreshCw, Filter, Search } from 'lucide-react';

export const KanbanPage: React.FC = () => {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedProject, setSelectedProject] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState<string>('');

  const fetchTasks = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<Task[]>('/tasks?limit=100');
      setTasks(data);
    } catch (err: any) {
      console.error('Failed to fetch tasks for kanban:', err);
      setError(err.message || 'Failed to load tasks from API');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  const handleStatusChange = async (taskId: string, newStatus: TaskStatus) => {
    // Optimistic state update
    setTasks((prev) =>
      prev.map((t) => (t.id === taskId ? { ...t, status: newStatus } : t))
    );

    try {
      await api.patch(`/tasks/${taskId}`, { status: newStatus });
    } catch (err: any) {
      console.error(`Failed to patch task ${taskId} status:`, err);
      // Revert if error
      fetchTasks();
    }
  };

  const projects = useMemo(() => {
    const set = new Set<string>();
    tasks.forEach((t) => {
      if (t.project) set.add(t.project);
    });
    return Array.from(set).sort();
  }, [tasks]);

  const filteredTasks = useMemo(() => {
    return tasks.filter((task) => {
      if (selectedProject && task.project !== selectedProject) return false;
      if (searchQuery) {
        const q = searchQuery.toLowerCase();
        const matchesId = task.id.toLowerCase().includes(q);
        const matchesTitle = task.title.toLowerCase().includes(q);
        const matchesExec = (task.executor || '').toLowerCase().includes(q);
        if (!matchesId && !matchesTitle && !matchesExec) return false;
      }
      return true;
    });
  }, [tasks, selectedProject, searchQuery]);

  return (
    <div className="p-6 space-y-6">
      {/* Top Bar Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-extrabold text-white tracking-tight flex items-center gap-2">
            <LayoutGrid className="w-6 h-6 text-indigo-400" />
            <span>Kanban Board</span>
          </h1>
          <p className="text-gray-400 text-sm mt-1">
            Drag and drop tasks across workflow states: To Do, Dispatched, In Review, and Done.
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={fetchTasks}
            disabled={loading}
            className="flex items-center space-x-1.5 px-3 py-2 rounded-lg bg-gray-900 border border-gray-800 text-gray-300 hover:text-white hover:border-gray-700 text-sm font-medium transition-colors disabled:opacity-50"
            title="Refresh board"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>

          <Link
            to="/tasks"
            className="flex items-center space-x-1.5 px-3 py-2 rounded-lg bg-gray-900 border border-gray-800 text-gray-300 hover:text-white hover:border-gray-700 text-sm font-medium transition-colors"
          >
            <Table className="w-4 h-4 text-indigo-400" />
            <span>Table View</span>
          </Link>
        </div>
      </div>

      {/* Filter bar */}
      <div className="bg-gray-900/80 border border-gray-800 rounded-xl p-3.5 flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3">
        <div className="flex items-center space-x-2">
          <Filter className="w-4 h-4 text-gray-400" />
          <span className="text-xs text-gray-400 font-medium">Filter Board:</span>

          <select
            value={selectedProject}
            onChange={(e) => setSelectedProject(e.target.value)}
            className="bg-gray-950 border border-gray-800 rounded-lg px-3 py-1.5 text-xs text-gray-300 focus:outline-none focus:border-indigo-500"
          >
            <option value="">All Projects</option>
            {projects.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>

        <div className="relative flex-1 max-w-xs">
          <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            placeholder="Search board tasks..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full bg-gray-950 border border-gray-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-indigo-500"
          />
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 text-red-400 p-4 rounded-xl text-sm">
          {error}
        </div>
      )}

      {/* Drag & Drop Board */}
      <KanbanBoard
        tasks={filteredTasks}
        loading={loading}
        onStatusChange={handleStatusChange}
      />
    </div>
  );
};

export default KanbanPage;
