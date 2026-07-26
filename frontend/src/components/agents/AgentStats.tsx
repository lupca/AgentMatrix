import React from 'react';
import { Bot, ShieldCheck, CheckCircle2, Award, Zap, Activity, Users } from 'lucide-react';
import { Agent, AgentStats as AgentStatsType } from '../../types/agent';

interface AgentStatsProps {
  agents?: Agent[];
  stats?: AgentStatsType[];
  loading?: boolean;
}

export const AgentStats: React.FC<AgentStatsProps> = ({
  agents = [],
  stats = [],
  loading = false,
}) => {
  if (loading) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 animate-pulse">
        {[1, 2, 3, 4].map((i) => (
          <div key={i} className="h-28 bg-gray-900/60 rounded-xl border border-gray-800 p-4" />
        ))}
      </div>
    );
  }

  // Aggregate numbers
  const totalAgents = agents.length || stats.length;
  const activeIdleAgents = agents.filter(
    (a) => a.status.toLowerCase() === 'idle' || a.status.toLowerCase() === 'active' || a.status.toLowerCase() === 'online'
  ).length;
  const busyAgents = agents.filter(
    (a) => a.status.toLowerCase() === 'busy' || a.status.toLowerCase() === 'executing'
  ).length;

  const totalExecuted = stats.reduce((acc, curr) => acc + (curr.tasks_executed || 0), 0);
  const totalReviewed = stats.reduce((acc, curr) => acc + (curr.tasks_reviewed || 0), 0);
  const totalCompleted = stats.reduce((acc, curr) => acc + (curr.tasks_completed || 0), 0);

  const avgSuccessRatePct =
    stats.length > 0
      ? Math.round(
          (stats.reduce((acc, curr) => acc + (curr.success_rate || 0), 0) / stats.length) * 100
        )
      : totalExecuted > 0
      ? Math.round((totalCompleted / totalExecuted) * 100)
      : 100;

  // Role distribution map
  const roleCounts: Record<string, number> = {};
  agents.forEach((a) => {
    const role = a.role || 'Unspecified';
    roleCounts[role] = (roleCounts[role] || 0) + 1;
  });

  return (
    <div className="space-y-4">
      {/* Top 4 Metric Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Card 1: Total Agents */}
        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm relative overflow-hidden group hover:border-purple-500/40 transition-colors">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs font-medium text-gray-400">Total Agent Roster</p>
              <h3 className="text-2xl font-extrabold text-gray-100 mt-1 font-mono">{totalAgents}</h3>
            </div>
            <div className="w-10 h-10 rounded-xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center text-purple-400">
              <Bot className="w-5 h-5" />
            </div>
          </div>
          <div className="mt-3 flex items-center gap-3 text-[11px] text-gray-400 border-t border-gray-800/60 pt-2">
            <span className="flex items-center gap-1 text-emerald-400">
              <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              {activeIdleAgents} Ready
            </span>
            <span className="flex items-center gap-1 text-amber-400">
              <span className="w-2 h-2 rounded-full bg-amber-400" />
              {busyAgents} Busy
            </span>
          </div>
        </div>

        {/* Card 2: Tasks Executed */}
        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm relative overflow-hidden group hover:border-indigo-500/40 transition-colors">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs font-medium text-gray-400">Total Tasks Executed</p>
              <h3 className="text-2xl font-extrabold text-gray-100 mt-1 font-mono">{totalExecuted}</h3>
            </div>
            <div className="w-10 h-10 rounded-xl bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
              <Zap className="w-5 h-5" />
            </div>
          </div>
          <div className="mt-3 flex items-center gap-2 text-[11px] text-gray-400 border-t border-gray-800/60 pt-2">
            <span className="text-indigo-400 font-medium">{totalCompleted} Completed successfully</span>
          </div>
        </div>

        {/* Card 3: Four-Eyes Reviews */}
        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm relative overflow-hidden group hover:border-blue-500/40 transition-colors">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs font-medium text-gray-400">Four-Eyes Reviews</p>
              <h3 className="text-2xl font-extrabold text-gray-100 mt-1 font-mono">{totalReviewed}</h3>
            </div>
            <div className="w-10 h-10 rounded-xl bg-blue-500/10 border border-blue-500/20 flex items-center justify-center text-blue-400">
              <ShieldCheck className="w-5 h-5" />
            </div>
          </div>
          <div className="mt-3 flex items-center gap-2 text-[11px] text-gray-400 border-t border-gray-800/60 pt-2">
            <span className="text-blue-400 font-medium">Independent reviewer verification</span>
          </div>
        </div>

        {/* Card 4: Roster Success Rate */}
        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm relative overflow-hidden group hover:border-emerald-500/40 transition-colors">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs font-medium text-gray-400">Avg Roster Success</p>
              <h3 className="text-2xl font-extrabold text-emerald-400 mt-1 font-mono">
                {avgSuccessRatePct}%
              </h3>
            </div>
            <div className="w-10 h-10 rounded-xl bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center text-emerald-400">
              <Award className="w-5 h-5" />
            </div>
          </div>
          <div className="mt-3 flex items-center gap-2 text-[11px] text-gray-400 border-t border-gray-800/60 pt-2">
            <span className="text-emerald-400 font-medium">High gate pass quality</span>
          </div>
        </div>
      </div>

      {/* Role Distribution Bar (if agents exist) */}
      {Object.keys(roleCounts).length > 0 && (
        <div className="rounded-xl border border-gray-800/80 bg-gray-900/40 p-4 backdrop-blur-sm flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div className="flex items-center gap-2">
            <Users className="w-4 h-4 text-purple-400" />
            <span className="text-xs font-semibold text-gray-200">Role Composition:</span>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs">
            {Object.entries(roleCounts).map(([role, count]) => (
              <div
                key={role}
                className="flex items-center gap-1.5 px-3 py-1 rounded-lg bg-gray-950/80 border border-gray-800 text-gray-300"
              >
                <span className="font-semibold text-gray-100 capitalize">{role}</span>
                <span className="text-xs px-1.5 py-0.2 rounded bg-purple-500/20 text-purple-300 font-mono">
                  {count}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default AgentStats;
