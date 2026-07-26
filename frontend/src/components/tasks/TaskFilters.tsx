import React from 'react';
import { Search, Filter, RotateCcw, X } from 'lucide-react';
import { TaskFiltersState } from '../../types/task';

interface TaskFiltersProps {
  filters: TaskFiltersState;
  onChange: (filters: TaskFiltersState) => void;
  projects: string[];
  totalCount: number;
  filteredCount: number;
}

export const TaskFilters: React.FC<TaskFiltersProps> = ({
  filters,
  onChange,
  projects,
  totalCount,
  filteredCount,
}) => {
  const handleSearchChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    onChange({ ...filters, search: e.target.value });
  };

  const handleProjectChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    onChange({ ...filters, project: e.target.value });
  };

  const handleStatusChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    onChange({ ...filters, status: e.target.value });
  };

  const handlePriorityChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    onChange({ ...filters, priority: e.target.value });
  };

  const handleReset = () => {
    onChange({
      search: '',
      project: '',
      status: '',
      priority: '',
      risk: '',
    });
  };

  const isFiltered =
    filters.search || filters.project || filters.status || filters.priority || filters.risk;

  return (
    <div className="bg-gray-900/80 border border-gray-800 rounded-xl p-4 space-y-3 backdrop-blur shadow-sm">
      <div className="flex flex-col md:flex-row gap-3 items-stretch md:items-center justify-between">
        {/* Search bar */}
        <div className="relative flex-1">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            placeholder="Filter by title, ID, executor..."
            value={filters.search}
            onChange={handleSearchChange}
            className="w-full bg-gray-950 border border-gray-800 rounded-lg pl-9 pr-8 py-2 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-indigo-500 transition-colors"
          />
          {filters.search && (
            <button
              onClick={() => onChange({ ...filters, search: '' })}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-200"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>

        {/* Dropdown Filters */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Project Filter */}
          <div className="flex items-center space-x-1">
            <select
              value={filters.project}
              onChange={handleProjectChange}
              className="bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-300 focus:outline-none focus:border-indigo-500 transition-colors"
            >
              <option value="">All Projects</option>
              {projects.map((proj) => (
                <option key={proj} value={proj}>
                  {proj}
                </option>
              ))}
            </select>
          </div>

          {/* Status Filter */}
          <select
            value={filters.status}
            onChange={handleStatusChange}
            className="bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-300 focus:outline-none focus:border-indigo-500 transition-colors"
          >
            <option value="">All Statuses</option>
            <option value="todo">To Do</option>
            <option value="dispatched">Dispatched</option>
            <option value="in-review">In Review</option>
            <option value="done">Done</option>
          </select>

          {/* Priority Filter */}
          <select
            value={filters.priority}
            onChange={handlePriorityChange}
            className="bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-300 focus:outline-none focus:border-indigo-500 transition-colors"
          >
            <option value="">All Priorities</option>
            <option value="high">High Priority</option>
            <option value="medium">Medium Priority</option>
            <option value="low">Low Priority</option>
          </select>

          {/* Reset Filters */}
          {isFiltered && (
            <button
              onClick={handleReset}
              className="flex items-center space-x-1.5 px-3 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-xs font-medium text-gray-300 hover:text-white transition-colors"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              <span>Reset</span>
            </button>
          )}
        </div>
      </div>

      {/* Filter Stats bar */}
      <div className="flex items-center justify-between text-xs text-gray-400 pt-1 border-t border-gray-800/60">
        <div className="flex items-center space-x-2">
          <Filter className="w-3.5 h-3.5 text-indigo-400" />
          <span>
            Showing <strong className="text-gray-200">{filteredCount}</strong> of{' '}
            <strong className="text-gray-200">{totalCount}</strong> tasks
          </span>
        </div>
        {isFiltered && (
          <div className="flex items-center space-x-1 text-indigo-400">
            <span>Filters active</span>
          </div>
        )}
      </div>
    </div>
  );
};
