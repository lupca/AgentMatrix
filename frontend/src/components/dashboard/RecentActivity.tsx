import React, { useState } from 'react';
import { Activity, User, Terminal, ChevronDown, ChevronUp, Clock, FileText, CheckCircle, AlertTriangle, ShieldCheck } from 'lucide-react';

export interface AuditItem {
  id: string | number;
  task_id?: string;
  action: string;
  actor: string;
  details?: Record<string, any> | string;
  created_at: string;
}

interface RecentActivityProps {
  activities?: AuditItem[];
  loading?: boolean;
}

export const RecentActivity: React.FC<RecentActivityProps> = ({
  activities = [],
  loading = false,
}) => {
  const [expandedId, setExpandedId] = useState<string | number | null>(null);

  if (loading) {
    return (
      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm">
        <div className="h-6 bg-gray-800 rounded w-1/3 mb-4"></div>
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-16 bg-gray-950/60 rounded-lg p-3 animate-pulse border border-gray-800"></div>
          ))}
        </div>
      </div>
    );
  }

  const toggleExpand = (id: string | number) => {
    setExpandedId(expandedId === id ? null : id);
  };

  const getActionIcon = (action: string) => {
    const act = action.toLowerCase();
    if (act.includes('create')) return <FileText className="w-3.5 h-3.5 text-indigo-400" />;
    if (act.includes('approval') || act.includes('review')) return <ShieldCheck className="w-3.5 h-3.5 text-amber-400" />;
    if (act.includes('verdict') || act.includes('complete')) return <CheckCircle className="w-3.5 h-3.5 text-emerald-400" />;
    if (act.includes('error') || act.includes('fail')) return <AlertTriangle className="w-3.5 h-3.5 text-rose-400" />;
    return <Terminal className="w-3.5 h-3.5 text-blue-400" />;
  };

  const formatTime = (isoString: string) => {
    try {
      const date = new Date(isoString);
      return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch {
      return isoString;
    }
  };

  return (
    <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm">
      <div className="flex items-center justify-between pb-4 border-b border-gray-800/80">
        <div>
          <h2 className="text-base font-semibold text-gray-100 flex items-center gap-2">
            <Activity className="w-4 h-4 text-indigo-400" />
            Recent Activity & Audit Log
          </h2>
          <p className="text-xs text-gray-400 mt-0.5">
            Real-time telemetry and state change stream from agent execution engine
          </p>
        </div>
        <span className="text-xs font-mono px-2 py-1 rounded bg-gray-950 border border-gray-800 text-gray-400">
          {activities.length} entries
        </span>
      </div>

      {activities.length === 0 ? (
        <div className="py-12 text-center text-gray-500 text-sm">
          No audit log events recorded yet.
        </div>
      ) : (
        <div className="mt-4 divide-y divide-gray-800/60">
          {activities.map((item) => {
            const isExpanded = expandedId === item.id;
            return (
              <div
                key={item.id}
                className="py-3 transition-colors hover:bg-gray-950/40 px-2 rounded-lg"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="p-2 rounded-lg bg-gray-950 border border-gray-800 flex-shrink-0">
                      {getActionIcon(item.action)}
                    </div>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-xs font-semibold text-gray-200">
                          {item.action}
                        </span>
                        {item.task_id && (
                          <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                            {item.task_id}
                          </span>
                        )}
                        <span className="text-xs text-gray-400 flex items-center gap-1">
                          by <User className="w-3 h-3 text-gray-500" />{' '}
                          <span className="text-gray-300 font-medium">{item.actor}</span>
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 flex-shrink-0">
                    <span className="text-[11px] font-mono text-gray-500 flex items-center gap-1">
                      <Clock className="w-3 h-3 text-gray-600" />
                      {formatTime(item.created_at)}
                    </span>
                    {item.details && (
                      <button
                        onClick={() => toggleExpand(item.id)}
                        className="p-1 rounded text-gray-500 hover:text-gray-300 hover:bg-gray-800 transition-colors"
                        title="Toggle payload details"
                      >
                        {isExpanded ? (
                          <ChevronUp className="w-3.5 h-3.5" />
                        ) : (
                          <ChevronDown className="w-3.5 h-3.5" />
                        )}
                      </button>
                    )}
                  </div>
                </div>

                {isExpanded && item.details && (
                  <div className="mt-2 ml-11 p-3 rounded-lg bg-gray-950 border border-gray-800/80 font-mono text-[11px] text-gray-300 overflow-x-auto">
                    <pre>
                      {typeof item.details === 'object'
                        ? JSON.stringify(item.details, null, 2)
                        : item.details}
                    </pre>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default RecentActivity;
