import React, { useState } from 'react';
import { FolderKanban, Search, CheckCircle2, Clock, AlertCircle, ArrowUpRight } from 'lucide-react';

export interface ProjectProgress {
  id: string;
  name: string;
  description?: string;
  status: 'active' | 'completed' | 'paused' | string;
  totalTasks: number;
  completedTasks: number;
  progressPercentage: number;
  lastUpdated?: string;
}

interface ProjectCardsProps {
  projects?: ProjectProgress[];
  loading?: boolean;
}

export const ProjectCards: React.FC<ProjectCardsProps> = ({
  projects = [],
  loading = false,
}) => {
  const [search, setSearch] = useState('');

  if (loading) {
    return (
      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm">
        <div className="h-6 bg-gray-800 rounded w-1/4 mb-4"></div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {[1, 2].map((i) => (
            <div key={i} className="h-36 bg-gray-950/60 rounded-xl p-4 animate-pulse border border-gray-800"></div>
          ))}
        </div>
      </div>
    );
  }

  const filteredProjects = projects.filter(
    (p) =>
      p.name.toLowerCase().includes(search.toLowerCase()) ||
      p.id.toLowerCase().includes(search.toLowerCase()) ||
      (p.description && p.description.toLowerCase().includes(search.toLowerCase()))
  );

  return (
    <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-gray-800/80">
        <div>
          <h2 className="text-base font-semibold text-white flex items-center gap-2">
            <FolderKanban className="w-4 h-4 text-indigo-400" />
            Project Progress & Status
          </h2>
          <p className="text-xs text-gray-400 mt-0.5">
            Track active multi-task orchestration pipelines across projects
          </p>
        </div>

        <div className="relative w-full sm:w-64">
          <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search projects..."
            className="w-full bg-gray-950/80 border border-gray-800 rounded-lg pl-9 pr-3 py-1.5 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-indigo-500/80 transition-colors"
          />
        </div>
      </div>

      {filteredProjects.length === 0 ? (
        <div className="py-12 text-center text-gray-500 text-sm">
          No projects found matching your query.
        </div>
      ) : (
        <div className="mt-6 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredProjects.map((project) => {
            const pct = Math.min(100, Math.max(0, project.progressPercentage ?? 0));
            const getBadgeColor = (status: string) => {
              switch (status.toLowerCase()) {
                case 'completed':
                  return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
                case 'paused':
                  return 'bg-amber-500/10 text-amber-400 border-amber-500/30';
                default:
                  return 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30';
              }
            };

            return (
              <div
                key={project.id}
                className="group relative flex flex-col justify-between rounded-xl border border-gray-800/70 bg-gray-950/60 p-4 transition-all duration-200 hover:border-gray-700 hover:bg-gray-950/90 shadow-md"
              >
                <div>
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <span className="text-[10px] font-mono font-medium text-indigo-400 uppercase tracking-wide">
                        {project.id}
                      </span>
                      <h3 className="text-sm font-semibold text-white group-hover:text-indigo-300 transition-colors">
                        {project.name}
                      </h3>
                    </div>
                    <span
                      className={`text-[10px] font-medium px-2 py-0.5 rounded border capitalize ${getBadgeColor(
                        project.status
                      )}`}
                    >
                      {project.status}
                    </span>
                  </div>

                  {project.description && (
                    <p className="text-xs text-gray-400 mt-2 line-clamp-2">
                      {project.description}
                    </p>
                  )}
                </div>

                <div className="mt-4 pt-3 border-t border-gray-800/60">
                  <div className="flex items-center justify-between text-xs text-gray-400 mb-1.5">
                    <span>Task Completion</span>
                    <span className="font-mono font-medium text-gray-200">
                      {project.completedTasks} / {project.totalTasks} ({pct.toFixed(0)}%)
                    </span>
                  </div>

                  <div className="w-full h-2 bg-gray-800 rounded-full overflow-hidden">
                    <div
                      style={{ width: `${pct}%` }}
                      className="h-full bg-gradient-to-r from-indigo-500 via-purple-500 to-emerald-400 rounded-full transition-all duration-500"
                    />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default ProjectCards;
