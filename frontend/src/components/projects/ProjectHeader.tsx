import React from 'react';
import { FolderKanban, Calendar, Settings } from 'lucide-react';
import { Project } from '../../types/project';

interface ProjectHeaderProps {
  project: Project | null;
  loading: boolean;
  onSettingsClick: () => void;
}

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

export const ProjectHeader: React.FC<ProjectHeaderProps> = ({ project, loading, onSettingsClick }) => {
  if (loading) {
    return <div className="h-32 bg-gray-900/60 rounded-2xl border border-gray-800 animate-pulse" />;
  }

  return (
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
          <span>
            Created: {project?.created_at
              ? new Date(project.created_at).toLocaleDateString()
              : 'Unknown'}
          </span>
          <button
            onClick={onSettingsClick}
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
  );
};

export default ProjectHeader;
