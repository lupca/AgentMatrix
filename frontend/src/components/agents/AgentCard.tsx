import React from 'react';
import { Link } from 'react-router-dom';
import { Bot, Cpu, CheckCircle2, Award, ArrowRight, ShieldCheck, Zap } from 'lucide-react';
import { Agent, AgentStats } from '../../types/agent';

interface AgentCardProps {
  agent: Agent;
  stats?: AgentStats;
}

export const AgentCard: React.FC<AgentCardProps> = ({ agent, stats }) => {
  const executed = stats?.tasks_executed ?? 0;
  const reviewed = stats?.tasks_reviewed ?? 0;
  const completed = stats?.tasks_completed ?? 0;
  const successRatePct = stats?.success_rate ? Math.round(stats.success_rate * 100) : (executed > 0 ? Math.round((completed / executed) * 100) : 100);

  const getStatusBadge = (status: string) => {
    switch (status.toLowerCase()) {
      case 'idle':
      case 'online':
      case 'active':
        return {
          color: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
          dot: 'bg-emerald-400 animate-pulse',
        };
      case 'busy':
      case 'executing':
        return {
          color: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
          dot: 'bg-amber-400 animate-ping',
        };
      default:
        return {
          color: 'bg-gray-500/10 text-gray-400 border-gray-500/30',
          dot: 'bg-gray-400',
        };
    }
  };

  const statusStyle = getStatusBadge(agent.status);

  // Array of capabilities or fallback defaults
  const capabilitiesList: string[] = Array.isArray(agent.capabilities)
    ? agent.capabilities.map((c) => (typeof c === 'string' ? c : JSON.stringify(c)))
    : ['Code Generation', 'Automated Testing', 'Code Review'];

  return (
    <div className="group relative flex flex-col justify-between rounded-xl border border-gray-800/80 bg-gray-900/60 p-5 shadow-lg backdrop-blur-sm transition-all duration-200 hover:border-purple-500/50 hover:bg-gray-900/90 hover:shadow-purple-500/10">
      <div>
        {/* Header */}
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="relative">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-purple-500/20 via-indigo-500/20 to-emerald-500/20 border border-purple-500/30 flex items-center justify-center text-purple-400 shadow-md group-hover:scale-105 transition-transform">
                <Bot className="w-5 h-5" />
              </div>
              <span
                className={`absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full border-2 border-gray-900 ${statusStyle.dot}`}
              />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-base font-semibold text-gray-100 group-hover:text-purple-300 transition-colors">
                  {agent.name}
                </h3>
              </div>
              <span className="text-[11px] font-mono text-purple-400/90 flex items-center gap-1 mt-0.5">
                <Cpu className="w-3 h-3" />
                {agent.id}
              </span>
            </div>
          </div>
          <span
            className={`text-xs font-medium px-2.5 py-0.5 rounded-full border capitalize flex items-center gap-1.5 ${statusStyle.color}`}
          >
            {agent.status}
          </span>
        </div>

        {/* Role Pill */}
        <div className="mt-3.5 flex items-center gap-2">
          <span className="text-xs px-2.5 py-1 rounded-md bg-gray-950/80 border border-gray-800 text-gray-300 font-medium capitalize flex items-center gap-1.5">
            <ShieldCheck className="w-3.5 h-3.5 text-indigo-400" />
            Role: <strong className="text-gray-100">{agent.role}</strong>
          </span>
        </div>

        {/* Capabilities Pills */}
        <div className="mt-3.5">
          <span className="text-[11px] text-gray-400 font-medium block mb-1.5">Capabilities:</span>
          <div className="flex flex-wrap gap-1.5">
            {capabilitiesList.slice(0, 3).map((cap, idx) => (
              <span
                key={idx}
                className="text-[10px] px-2 py-0.5 rounded bg-purple-500/10 text-purple-300 border border-purple-500/20 font-mono"
              >
                {cap}
              </span>
            ))}
            {capabilitiesList.length > 3 && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-gray-800 text-gray-400">
                +{capabilitiesList.length - 3} more
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Stats Summary & Action */}
      <div className="mt-5 pt-4 border-t border-gray-800/80 space-y-3">
        <div className="grid grid-cols-3 gap-2 text-center text-xs">
          <div className="p-2 rounded-lg bg-gray-950/60 border border-gray-800/50">
            <span className="text-[10px] text-gray-400 block">Executed</span>
                <span className="font-bold text-gray-100 font-mono">{executed}</span>
          </div>
          <div className="p-2 rounded-lg bg-gray-950/60 border border-gray-800/50">
            <span className="text-[10px] text-gray-400 block">Reviewed</span>
                <span className="font-bold text-gray-100 font-mono">{reviewed}</span>
          </div>
          <div className="p-2 rounded-lg bg-gray-950/60 border border-gray-800/50">
            <span className="text-[10px] text-gray-400 block">Success Rate</span>
            <span className="font-bold text-emerald-400 font-mono">{successRatePct}%</span>
          </div>
        </div>

        <Link
          to={`/agents/${agent.id}`}
          className="flex items-center justify-center gap-1.5 w-full py-2 px-3 rounded-lg bg-purple-600/10 hover:bg-purple-600 text-purple-300 hover:text-white border border-purple-500/20 text-xs font-medium transition-all group-hover:border-purple-500/40"
        >
          <span>View Agent Profile</span>
          <ArrowRight className="w-3.5 h-3.5 group-hover:translate-x-1 transition-transform" />
        </Link>
      </div>
    </div>
  );
};

export default AgentCard;
