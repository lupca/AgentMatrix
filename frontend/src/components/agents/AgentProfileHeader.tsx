import React from 'react';
import { Bot, ShieldCheck, Calendar } from 'lucide-react';
import { Agent } from '../../types/agent';

interface AgentProfileHeaderProps {
  agent: Agent | null;
  loading: boolean;
}

const statusStyle = (status: string) => {
  switch (status.toLowerCase()) {
    case 'idle':
    case 'online':
    case 'active':
      return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
    case 'busy':
      return 'bg-amber-500/10 text-amber-400 border-amber-500/30';
    default:
      return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
  }
};

export const AgentProfileHeader: React.FC<AgentProfileHeaderProps> = ({ agent, loading }) => {
  const capabilitiesList: string[] = Array.isArray(agent?.capabilities)
    ? agent.capabilities.filter(
        (cap): cap is string => typeof cap === 'string' && cap.trim().length > 0,
      )
    : [];

  if (loading) {
    return <div className="h-36 bg-gray-900/60 rounded-2xl border border-gray-800 animate-pulse" />;
  }

  return (
    <div className="bg-gray-900/40 p-6 rounded-2xl border border-gray-800/80 shadow-lg backdrop-blur-md space-y-4">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div className="flex items-start gap-4">
          <div className="relative">
            <div className="w-14 h-14 rounded-2xl bg-gradient-to-tr from-purple-500/20 via-indigo-500/20 to-emerald-500/20 border border-purple-500/30 flex items-center justify-center text-purple-400 shadow-lg">
              <Bot className="w-7 h-7" />
            </div>
          </div>

          <div>
            <div className="flex items-center gap-3">
              <span className="text-xs font-mono font-bold text-purple-400 uppercase tracking-wide">
                {agent?.id}
              </span>
              <span
                className={`text-xs font-medium px-2.5 py-0.5 rounded-full border capitalize ${statusStyle(
                  agent?.status || 'idle'
                )}`}
              >
                {agent?.status}
              </span>
            </div>

            <h1 className="text-2xl font-bold text-gray-100 tracking-tight mt-1">
              {agent?.name}
            </h1>

            <div className="flex items-center gap-3 mt-1.5 text-xs text-gray-400">
              <span className="flex items-center gap-1">
                <ShieldCheck className="w-3.5 h-3.5 text-indigo-400" />
                Role: <strong className="text-gray-100 capitalize">{agent?.role}</strong>
              </span>
              <span>•</span>
              <span className="flex items-center gap-1">
                <Calendar className="w-3.5 h-3.5 text-gray-500" />
                Registered: {agent?.created_at
                  ? new Date(agent.created_at).toLocaleDateString()
                  : 'Unknown'}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="pt-3 border-t border-gray-800/60 flex flex-wrap items-center gap-2">
        <span className="text-xs text-gray-400 font-medium mr-1">Capabilities:</span>
        {capabilitiesList.map((cap, idx) => (
          <span
            key={idx}
            className="text-xs px-2.5 py-1 rounded-md bg-purple-500/10 text-purple-300 border border-purple-500/20 font-mono"
          >
            {cap}
          </span>
        ))}
      </div>
    </div>
  );
};

export default AgentProfileHeader;
