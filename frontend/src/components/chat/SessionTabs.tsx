import React from 'react';
import { Plus, X } from 'lucide-react';
import { ChatSessionSummary } from '../../hooks/useSessions';

interface SessionTabsProps {
  sessions: ChatSessionSummary[];
  activeSessionId: string | null;
  onSwitch: (sessionId: string) => void;
  onCreate: () => void;
  onClose?: (sessionId: string) => void;
  loading?: boolean;
  className?: string;
}

function sessionLabel(session: ChatSessionSummary, index: number): string {
  return session.title?.trim() || `Session ${index + 1}`;
}

export const SessionTabs: React.FC<SessionTabsProps> = ({
  sessions,
  activeSessionId,
  onSwitch,
  onCreate,
  onClose,
  loading = false,
  className = '',
}) => {
  return (
    <div
      className={`flex items-center gap-1 px-2 py-1.5 bg-gray-950/60 border-b border-gray-800/80 overflow-x-auto no-scrollbar shrink-0 ${className}`}
    >
      {sessions.map((session, index) => {
        const isActive = session.id === activeSessionId;
        return (
          <div
            key={session.id}
            role="tab"
            aria-selected={isActive}
            onClick={() => onSwitch(session.id)}
            className={`group flex items-center gap-1.5 pl-3 pr-1.5 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap cursor-pointer transition-colors ${
              isActive
                ? 'bg-indigo-600/20 text-indigo-300 border border-indigo-500/40'
                : 'text-gray-400 border border-transparent hover:bg-gray-800/60 hover:text-gray-200'
            }`}
          >
            <span className="flex items-center gap-1.5">
              {isActive && (
                <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" />
              )}
              {sessionLabel(session, index)}
            </span>
            {onClose && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onClose(session.id);
                }}
                className="p-0.5 rounded text-gray-500 opacity-0 group-hover:opacity-100 hover:text-red-400 hover:bg-gray-800 transition-all"
                title="Close session"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
        );
      })}

      <button
        type="button"
        onClick={onCreate}
        disabled={loading}
        className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium text-gray-400 border border-dashed border-gray-700 hover:text-indigo-300 hover:border-indigo-500/50 hover:bg-indigo-500/5 transition-colors disabled:opacity-40 shrink-0"
        title="New session"
      >
        <Plus className="w-3.5 h-3.5" />
        New
      </button>
    </div>
  );
};

export default SessionTabs;
