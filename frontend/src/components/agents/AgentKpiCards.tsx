import React from 'react';
import { Zap, ShieldCheck, CheckCircle2, Award } from 'lucide-react';

interface AgentKpiCardsProps {
  executedCount: number;
  reviewedCount: number;
  completedCount: number;
  successRatePct: number;
}

export const AgentKpiCards: React.FC<AgentKpiCardsProps> = ({
  executedCount,
  reviewedCount,
  completedCount,
  successRatePct,
}) => {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-gray-400">Tasks Executed</p>
          <h3 className="text-2xl font-extrabold text-gray-100 mt-1 font-mono">{executedCount}</h3>
        </div>
        <div className="w-10 h-10 rounded-xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center text-purple-400">
          <Zap className="w-5 h-5" />
        </div>
      </div>

      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-gray-400">Tasks Reviewed</p>
          <h3 className="text-2xl font-extrabold text-blue-400 mt-1 font-mono">{reviewedCount}</h3>
        </div>
        <div className="w-10 h-10 rounded-xl bg-blue-500/10 border border-blue-500/20 flex items-center justify-center text-blue-400">
          <ShieldCheck className="w-5 h-5" />
        </div>
      </div>

      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-gray-400">Tasks Completed</p>
          <h3 className="text-2xl font-extrabold text-emerald-400 mt-1 font-mono">{completedCount}</h3>
        </div>
        <div className="w-10 h-10 rounded-xl bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center text-emerald-400">
          <CheckCircle2 className="w-5 h-5" />
        </div>
      </div>

      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-gray-400">Success Quality</p>
          <h3 className="text-2xl font-extrabold text-amber-400 mt-1 font-mono">{successRatePct}%</h3>
        </div>
        <div className="w-10 h-10 rounded-xl bg-amber-500/10 border border-amber-500/20 flex items-center justify-center text-amber-400">
          <Award className="w-5 h-5" />
        </div>
      </div>
    </div>
  );
};

export default AgentKpiCards;
