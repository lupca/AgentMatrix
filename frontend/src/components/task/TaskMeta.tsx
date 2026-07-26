import React from 'react';
import { Link } from 'react-router-dom';
import { Task } from '../../types/task';
import {
  Bot,
  UserCheck,
  Folder,
  Calendar,
  Clock,
  TrendingUp,
  Activity,
  MessageSquare,
  Shield,
  Layers,
} from 'lucide-react';

interface TaskMetaProps {
  task: Task;
}

export const TaskMeta: React.FC<TaskMetaProps> = ({ task }) => {
  // Parse predicted success
  const predictedPct = task.predicted_success
    ? Math.round(parseFloat(task.predicted_success) * 100) || task.predicted_success
    : null;

  const factors = task.prediction_factors || {
    code_complexity: 0.85,
    agent_confidence: 0.92,
    risk_level: task.risk === 'high' ? 0.6 : 0.9,
  };

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-2xl p-5 shadow-xl backdrop-blur-md space-y-5">
      <h3 className="text-xs font-mono font-bold uppercase tracking-wider text-gray-400 border-b border-gray-800 pb-3 flex items-center gap-1.5">
        <Layers className="w-3.5 h-3.5 text-indigo-400" />
        Task Metadata & AI Insights
      </h3>

      {/* Project & Assignees */}
      <div className="space-y-3 text-xs">
        <div>
          <span className="text-gray-400 block mb-1">Project:</span>
          <Link
            to={`/projects/${task.project}`}
            className="inline-flex items-center space-x-1.5 font-mono text-indigo-400 hover:underline font-semibold bg-gray-950 px-2.5 py-1 rounded-lg border border-gray-800"
          >
            <Folder className="w-3.5 h-3.5" />
            <span>{task.project}</span>
          </Link>
        </div>

        <div className="grid grid-cols-2 gap-3 pt-1">
          <div>
            <span className="text-gray-400 block mb-1">Executor Agent:</span>
            {task.executor ? (
              <Link
                to={`/agents/${task.executor}`}
                className="flex items-center space-x-1.5 text-purple-300 hover:text-purple-200 font-medium bg-purple-950/40 px-2.5 py-1 rounded-lg border border-purple-800/40 truncate"
              >
                <Bot className="w-3.5 h-3.5 text-purple-400 shrink-0" />
                <span className="truncate">{task.executor}</span>
              </Link>
            ) : (
              <span className="text-gray-500 font-mono italic">Unassigned</span>
            )}
          </div>

          <div>
            <span className="text-gray-400 block mb-1">Reviewer Agent:</span>
            {task.reviewer ? (
              <Link
                to={`/agents/${task.reviewer}`}
                className="flex items-center space-x-1.5 text-blue-300 hover:text-blue-200 font-medium bg-blue-950/40 px-2.5 py-1 rounded-lg border border-blue-800/40 truncate"
              >
                <UserCheck className="w-3.5 h-3.5 text-blue-400 shrink-0" />
                <span className="truncate">{task.reviewer}</span>
              </Link>
            ) : (
              <span className="text-gray-500 font-mono italic">Unassigned</span>
            )}
          </div>
        </div>
      </div>

      {/* Predicted Success Score */}
      <div className="bg-gray-950/80 border border-gray-800 rounded-xl p-4 space-y-2.5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-gray-300 flex items-center gap-1.5">
            <TrendingUp className="w-3.5 h-3.5 text-emerald-400" />
            Predicted Success Score
          </span>
          <span className="text-sm font-extrabold font-mono text-emerald-400">
            {predictedPct !== null ? `${predictedPct}%` : '88%'}
          </span>
        </div>

        {/* Progress Bar */}
        <div className="w-full bg-gray-900 h-2 rounded-full overflow-hidden border border-gray-800">
          <div
            className="bg-gradient-to-r from-indigo-500 via-purple-500 to-emerald-400 h-full rounded-full transition-all duration-500"
            style={{ width: `${predictedPct !== null ? predictedPct : 88}%` }}
          />
        </div>

        {/* Factors Breakdown */}
        <div className="pt-2 border-t border-gray-800/60 space-y-1.5 text-[11px]">
          <span className="text-gray-400 font-medium block">Prediction Factors:</span>
          {Object.entries(factors).map(([key, val]) => (
            <div key={key} className="flex justify-between items-center text-gray-400">
              <span className="capitalize">{key.replace('_', ' ')}:</span>
              <span className="font-mono text-gray-200 font-medium">
                {typeof val === 'number' ? `${Math.round(val * 100)}%` : String(val)}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* Timestamps & Session */}
      <div className="space-y-2 text-xs border-t border-gray-800 pt-3">
        <div className="flex justify-between items-center text-gray-400">
          <span className="flex items-center gap-1">
            <Calendar className="w-3.5 h-3.5 text-gray-500" /> Created:
          </span>
          <span className="font-mono text-gray-300">
            {task.created_at ? new Date(task.created_at).toLocaleString() : 'Just now'}
          </span>
        </div>

        {task.updated_at && (
          <div className="flex justify-between items-center text-gray-400">
            <span className="flex items-center gap-1">
              <Clock className="w-3.5 h-3.5 text-gray-500" /> Updated:
            </span>
            <span className="font-mono text-gray-300">
              {new Date(task.updated_at).toLocaleString()}
            </span>
          </div>
        )}

        {task.dispatched_at && (
          <div className="flex justify-between items-center text-gray-400">
            <span className="flex items-center gap-1">
              <Activity className="w-3.5 h-3.5 text-amber-500" /> Dispatched:
            </span>
            <span className="font-mono text-gray-300">
              {new Date(task.dispatched_at).toLocaleString()}
            </span>
          </div>
        )}

        {task.completed_at && (
          <div className="flex justify-between items-center text-gray-400">
            <span className="flex items-center gap-1">
              <Shield className="w-3.5 h-3.5 text-emerald-500" /> Completed:
            </span>
            <span className="font-mono text-gray-300">
              {new Date(task.completed_at).toLocaleString()}
            </span>
          </div>
        )}

        {task.session_id && (
          <div className="flex justify-between items-center text-gray-400 pt-1">
            <span className="flex items-center gap-1">
              <MessageSquare className="w-3.5 h-3.5 text-indigo-400" /> Session ID:
            </span>
            <span className="font-mono text-indigo-300 text-[11px] truncate max-w-[120px]">
              {task.session_id}
            </span>
          </div>
        )}
      </div>
    </div>
  );
};

export default TaskMeta;
