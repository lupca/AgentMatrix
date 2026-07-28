import React from 'react';
import { Bot, RefreshCw, Trash2 } from 'lucide-react';

interface ChatHeaderProps {
  threadId: string;
  loadingHistory: boolean;
  isStreaming: boolean;
  messagesCount: number;
  onRefresh: () => void;
  onClear: () => void;
  onClose?: () => void;
}

export const ChatHeader: React.FC<ChatHeaderProps> = ({
  threadId,
  loadingHistory,
  isStreaming,
  messagesCount,
  onRefresh,
  onClear,
  onClose,
}) => {
  return (
    <div className="px-4 py-3 bg-gray-950/80 border-b border-gray-800/80 flex items-center justify-between shrink-0 select-none">
      <div className="flex items-center space-x-2.5">
        <div className="w-8 h-8 rounded-lg bg-gradient-to-tr from-indigo-500/20 via-purple-500/20 to-pink-500/20 border border-indigo-500/30 flex items-center justify-center text-indigo-400 shadow-md">
          <Bot className="w-4 h-4" />
        </div>
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-bold text-gray-100 tracking-wide">
              Task Copilot Chat
            </h3>
            <span className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 font-mono">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping inline-block" />
              SSE Ready
            </span>
          </div>
          <p className="text-[11px] text-gray-400 font-mono truncate max-w-[200px] sm:max-w-[280px]">
            Thread ID: {threadId}
          </p>
        </div>
      </div>

      <div className="flex items-center space-x-1.5">
        <button
          onClick={onRefresh}
          disabled={loadingHistory || isStreaming}
          className="p-1.5 rounded-lg text-gray-400 hover:text-gray-100 hover:bg-gray-800 transition-colors disabled:opacity-40"
          title="Refresh history"
        >
          <RefreshCw className={`w-4 h-4 ${loadingHistory ? 'animate-spin' : ''}`} />
        </button>
        <button
          onClick={onClear}
          disabled={isStreaming || messagesCount === 0}
          className="p-1.5 rounded-lg text-gray-400 hover:text-red-400 hover:bg-gray-800 transition-colors disabled:opacity-40"
          title="Clear view"
        >
          <Trash2 className="w-4 h-4" />
        </button>
        {onClose && (
          <button
            onClick={onClose}
            className="px-2 py-1 text-xs font-semibold rounded-lg bg-gray-800 text-gray-300 hover:text-gray-100 hover:bg-gray-700 transition-colors"
          >
            Close
          </button>
        )}
      </div>
    </div>
  );
};

export default ChatHeader;
