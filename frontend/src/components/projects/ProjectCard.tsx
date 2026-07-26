import React from 'react';
import { Link } from 'react-router-dom';
import { FolderKanban, CheckCircle2, Clock, ArrowRight, Layers } from 'lucide-react';
import { Project, ProjectStats } from '../../types/project';

interface ProjectCardProps {
  project: Project;
  stats?: ProjectStats;
  totalTasks?: number;
  completedTasks?: number;
}

export const ProjectCard: React.FC<ProjectCardProps> = ({
  project,
  stats,
  totalTasks: propTotal,
  completedTasks: propCompleted,
}) => {
  const total = stats?.total_tasks ?? propTotal ?? 0;
  const completed = stats?.done_tasks ?? propCompleted ?? 0;
  const active = stats?.active_tasks ?? Math.max(0, total - completed);
  const percentage = total > 0 ? Math.round((completed / total) * 100) : 0;

  const getStatusBadge = (status: string) => {
    switch (status.toLowerCase()) {
      case 'active':
        return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
      case 'completed':
        return 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30';
      case 'paused':
        return 'bg-amber-500/10 text-amber-400 border-amber-500/30';
      case 'archived':
        return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
      default:
        return 'bg-purple-500/10 text-purple-400 border-purple-500/30';
    }
  };

  return (
    <div className="group relative flex flex-col justify-between rounded-xl border border-gray-800/80 bg-gray-900/60 p-5 shadow-lg backdrop-blur-sm transition-all duration-200 hover:border-indigo-500/50 hover:bg-gray-900/90 hover:shadow-indigo-500/10">
      <div>
        {/* Header */}
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-lg bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400 group-hover:scale-105 transition-transform">
              <FolderKanban className="w-5 h-5" />
            </div>
            <div>
              <span className="text-[10px] font-mono font-semibold text-indigo-400 uppercase tracking-wider">
                {project.id}
              </span>
              <h3 className="text-base font-semibold text-white group-hover:text-indigo-300 transition-colors line-clamp-1">
                {project.name}
              </h3>
            </div>
          </div>
          <span className={`text-xs font-medium px-2.5 py-0.5 rounded-full border capitalize ${getStatusBadge(project.status)}`}>
            {project.status}
          </span>
        </div>

        {/* Description */}
        <p className="text-xs text-gray-400 mt-3 line-clamp-2 leading-relaxed">
          {project.description || 'No description provided for this project.'}
        </p>

        {/* Quick Metrics */}
        <div className="mt-4 grid grid-cols-2 gap-2 text-xs">
          <div className="flex items-center gap-1.5 p-2 rounded-lg bg-gray-950/50 border border-gray-800/60">
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
            <span className="text-gray-400">Done:</span>
            <span className="font-semibold text-white font-mono">{completed}</span>
          </div>
          <div className="flex items-center gap-1.5 p-2 rounded-lg bg-gray-950/50 border border-gray-800/60">
            <Clock className="w-3.5 h-3.5 text-indigo-400" />
            <span className="text-gray-400">Active:</span>
            <span className="font-semibold text-white font-mono">{active}</span>
          </div>
        </div>
      </div>

      {/* Progress & Link Footer */}
      <div className="mt-5 pt-4 border-t border-gray-800/80 space-y-3">
        <div>
          <div className="flex items-center justify-between text-xs mb-1.5">
            <span className="text-gray-400 flex items-center gap-1">
              <Layers className="w-3 h-3 text-gray-500" />
              Completion Rate
            </span>
            <span className="font-mono font-medium text-indigo-300">
              {percentage}% ({completed}/{total})
            </span>
          </div>
          <div className="w-full h-2 bg-gray-950 rounded-full overflow-hidden border border-gray-800/50">
            <div
              style={{ width: `${percentage}%` }}
              className="h-full bg-gradient-to-r from-indigo-500 via-purple-500 to-emerald-400 rounded-full transition-all duration-500"
            />
          </div>
        </div>

        <Link
          to={`/projects/${project.id}`}
          className="flex items-center justify-center gap-1.5 w-full py-2 px-3 rounded-lg bg-indigo-600/10 hover:bg-indigo-600 text-indigo-300 hover:text-white border border-indigo-500/20 text-xs font-medium transition-all group-hover:border-indigo-500/40"
        >
          <span>View Tasks & Details</span>
          <ArrowRight className="w-3.5 h-3.5 group-hover:translate-x-1 transition-transform" />
        </Link>
      </div>
    </div>
  );
};

export default ProjectCard;
