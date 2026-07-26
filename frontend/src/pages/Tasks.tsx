import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api';
import { Task, TaskFiltersState } from '../types/task';
import { TaskFilters } from '../components/tasks/TaskFilters';
import { TaskTable } from '../components/tasks/TaskTable';
import { LayoutGrid, RefreshCw, CheckSquare, Clock, PlayCircle, UserCheck, CheckCircle2, Plus } from 'lucide-react';

export const TasksPage: React.FC = () => {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState<TaskFiltersState>({
    search: '',
    project: '',
    status: '',
    priority: '',
    risk: '',
  });

  const fetchTasks = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<Task[]>('/tasks?limit=100');
      setTasks(data);
    } catch (err: any) {
      console.error('Failed to fetch tasks:', err);
      setError(err.message || 'Failed to load tasks from API');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  const handleStatusChange = async (taskId: string, newStatus: string) => {
    // Optimistic UI update
    setTasks((prev) =>
      prev.map((t) => (t.id === taskId ? { ...t, status: newStatus } : t))
    );

    try {
      await api.patch(`/tasks/${taskId}`, { status: newStatus });
    } catch (err: any) {
      console.error(`Failed to update task ${taskId} status:`, err);
      // Revert on error
      fetchTasks();
    }
  };

  // Dynamically extract unique projects
  const projects = useMemo(() => {
    const projs = new Set<string>();
    tasks.forEach((t) => {
      if (t.project) projs.add(t.project);
    });
    return Array.from(projs).sort();
  }, [tasks]);

  // Filter tasks based on search & dropdowns
  const filteredTasks = useMemo(() => {
    return tasks.filter((task) => {
      // Search
      if (filters.search) {
        const query = filters.search.toLowerCase();
        const matchesId = task.id.toLowerCase().includes(query);
        const matchesTitle = task.title.toLowerCase().includes(query);
        const matchesExec = (task.executor || '').toLowerCase().includes(query);
        const matchesProject = task.project.toLowerCase().includes(query);
        if (!matchesId && !matchesTitle && !matchesExec && !matchesProject) return false;
      }
      // Project
      if (filters.project && task.project !== filters.project) {
        return false;
      }
      // Status
      if (filters.status && (task.status || '').toLowerCase() !== filters.status.toLowerCase()) {
        return false;
      }
      // Priority
      if (filters.priority && (task.priority || '').toLowerCase() !== filters.priority.toLowerCase()) {
        return false;
      }
      return true;
    });
  }, [tasks, filters]);

  // Status stats
  const stats = useMemo(() => {
    return {
      total: tasks.length,
      todo: tasks.filter((t) => (t.status || '').toLowerCase() === 'todo').length,
      dispatched: tasks.filter((t) => (t.status || '').toLowerCase() === 'dispatched').length,
      inReview: tasks.filter((t) => (t.status || '').toLowerCase() === 'in-review').length,
      done: tasks.filter((t) => (t.status || '').toLowerCase() === 'done').length,
    };
  }, [tasks]);

  return (
    <div className="p-6 space-y-6">
      {/* Top Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-extrabold text-white tracking-tight flex items-center gap-2">
            <CheckSquare className="w-6 h-6 text-indigo-400" />
            <span>Tasks Table</span>
          </h1>
          <p className="text-gray-400 text-sm mt-1">
            Sort, filter, and monitor all LangGraph background tasks and gate executions.
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={fetchTasks}
            disabled={loading}
            className="flex items-center space-x-1.5 px-3 py-2 rounded-lg bg-gray-900 border border-gray-800 text-gray-300 hover:text-white hover:border-gray-700 text-sm font-medium transition-colors disabled:opacity-50"
            title="Refresh tasks"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>

          <Link
            to="/kanban"
            className="flex items-center space-x-1.5 px-3 py-2 rounded-lg bg-gray-900 border border-gray-800 text-gray-300 hover:text-white hover:border-gray-700 text-sm font-medium transition-colors"
          >
            <LayoutGrid className="w-4 h-4 text-indigo-400" />
            <span>Kanban View</span>
          </Link>
        </div>
      </div>

      {/* Summary KPI Cards */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
        <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-3.5 flex items-center justify-between">
          <div>
            <div className="text-xs text-gray-400 font-medium">Total Tasks</div>
            <div className="text-xl font-bold text-white mt-0.5">{stats.total}</div>
          </div>
          <CheckSquare className="w-5 h-5 text-indigo-400" />
        </div>
        <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-3.5 flex items-center justify-between">
          <div>
            <div className="text-xs text-gray-400 font-medium">To Do</div>
            <div className="text-xl font-bold text-gray-200 mt-0.5">{stats.todo}</div>
          </div>
          <Clock className="w-5 h-5 text-gray-400" />
        </div>
        <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-3.5 flex items-center justify-between">
          <div>
            <div className="text-xs text-gray-400 font-medium">Dispatched</div>
            <div className="text-xl font-bold text-amber-400 mt-0.5">{stats.dispatched}</div>
          </div>
          <PlayCircle className="w-5 h-5 text-amber-400" />
        </div>
        <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-3.5 flex items-center justify-between">
          <div>
            <div className="text-xs text-gray-400 font-medium">In Review</div>
            <div className="text-xl font-bold text-purple-400 mt-0.5">{stats.inReview}</div>
          </div>
          <UserCheck className="w-5 h-5 text-purple-400" />
        </div>
        <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-3.5 flex items-center justify-between col-span-2 sm:col-span-1">
          <div>
            <div className="text-xs text-gray-400 font-medium">Completed</div>
            <div className="text-xl font-bold text-emerald-400 mt-0.5">{stats.done}</div>
          </div>
          <CheckCircle2 className="w-5 h-5 text-emerald-400" />
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 text-red-400 p-4 rounded-xl text-sm">
          {error}
        </div>
      )}

      {/* Filter Controls */}
      <TaskFilters
        filters={filters}
        onChange={setFilters}
        projects={projects}
        totalCount={tasks.length}
        filteredCount={filteredTasks.length}
      />

      {/* Sortable Filterable Table */}
      <TaskTable
        tasks={filteredTasks}
        loading={loading}
        onStatusChange={handleStatusChange}
      />
    </div>
  );
};

export default TasksPage;
