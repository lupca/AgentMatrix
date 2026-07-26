import React from 'react';
import {
  CheckSquare,
  Clock,
  CheckCircle2,
  Zap,
} from 'lucide-react';

export interface KpiData {
  totalTasks: number;
  activeGates: number;
  inReview: number;
  completedTasks: number;
  failedTasks: number;
  tokenEfficiency: number;
  tokensSaved?: number;
}

interface KpiCardsProps {
  data?: KpiData;
  loading?: boolean;
}

export const KpiCards: React.FC<KpiCardsProps> = ({ data, loading }) => {
  if (loading) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5">
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            className="h-32 rounded-xl bg-gray-900/60 border border-gray-800/80 p-5 animate-pulse flex flex-col justify-between"
          >
            <div className="h-4 bg-gray-800 rounded w-1/2"></div>
            <div className="h-8 bg-gray-800 rounded w-3/4 my-2"></div>
            <div className="h-3 bg-gray-800 rounded w-1/3"></div>
          </div>
        ))}
      </div>
    );
  }

  const defaultData: KpiData = {
    totalTasks: data?.totalTasks ?? 0,
    activeGates: data?.activeGates ?? 0,
    inReview: data?.inReview ?? 0,
    completedTasks: data?.completedTasks ?? 0,
    failedTasks: data?.failedTasks ?? 0,
    tokenEfficiency: data?.tokenEfficiency ?? 0,
    tokensSaved: data?.tokensSaved ?? 0,
  };

  const cards = [
    {
      title: 'Total Tasks',
      value: defaultData.totalTasks,
      subtext: `${defaultData.activeGates} active gate execution${defaultData.activeGates === 1 ? '' : 's'}`,
      subtextColor: 'text-indigo-400',
      icon: CheckSquare,
      iconBg: 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20',
      badge: 'Active System',
      badgeBg: 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30',
    },
    {
      title: 'In Review',
      value: defaultData.inReview,
      subtext: 'Pending Four-Eyes gate verification',
      subtextColor: 'text-amber-400',
      icon: Clock,
      iconBg: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
      badge: 'Review Gate',
      badgeBg: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
    },
    {
      title: 'Completed Tasks',
      value: defaultData.completedTasks,
      subtext: `${defaultData.failedTasks} failed / aborted`,
      subtextColor: defaultData.failedTasks > 0 ? 'text-rose-400' : 'text-emerald-400',
      icon: CheckCircle2,
      iconBg: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
      badge: 'Success Stream',
      badgeBg: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
    },
    {
      title: 'Token Efficiency',
      value: `${defaultData.tokenEfficiency.toFixed(1)}%`,
      subtext: defaultData.tokensSaved
        ? `~${(defaultData.tokensSaved / 1000).toFixed(1)}k tokens saved`
        : '~80% reduction target',
      subtextColor: 'text-purple-400',
      icon: Zap,
      iconBg: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
      badge: 'LLM Opt',
      badgeBg: 'bg-purple-500/10 text-purple-400 border-purple-500/30',
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5">
      {cards.map((card, idx) => {
        const Icon = card.icon;
        return (
          <div
            key={idx}
            className="group relative overflow-hidden rounded-xl border border-gray-800/80 bg-gray-900/60 p-5 shadow-lg backdrop-blur-sm transition-all duration-200 hover:border-gray-700 hover:shadow-indigo-500/5 hover:-translate-y-0.5"
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">
                {card.title}
              </span>
              <div
                className={`flex h-9 w-9 items-center justify-center rounded-lg border ${card.iconBg} transition-transform duration-200 group-hover:scale-110`}
              >
                <Icon className="h-4 w-4" />
              </div>
            </div>

            <div className="mt-3 flex items-baseline justify-between">
              <span className="text-3xl font-extrabold tracking-tight text-white">
                {card.value}
              </span>
              <span
                className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium ${card.badgeBg}`}
              >
                {card.badge}
              </span>
            </div>

            <div className="mt-3 flex items-center text-xs">
              <span className={`font-medium ${card.subtextColor}`}>
                {card.subtext}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
};

export default KpiCards;
