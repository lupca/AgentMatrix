import React from 'react';
import { MessageSquare } from 'lucide-react';

interface GlobalChatButtonProps {
  onClick: () => void;
  hasUnread?: boolean;
  className?: string;
}

export const GlobalChatButton: React.FC<GlobalChatButtonProps> = ({
  onClick,
  hasUnread = false,
  className = '',
}) => {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="Open AI assistant"
      className={`fixed bottom-6 right-6 z-50 flex items-center gap-2.5 rounded-full border border-indigo-400/30 bg-gradient-to-r from-indigo-600 to-purple-600 px-4 py-3 text-xs font-semibold text-white shadow-2xl transition-all duration-200 hover:scale-105 ${className}`}
    >
      <div className="relative">
        <MessageSquare className="w-5 h-5" />
        {hasUnread && (
          <span className="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full border-2 border-indigo-600 bg-emerald-400 animate-pulse" />
        )}
      </div>
      <span>AI Chat</span>
    </button>
  );
};

export default GlobalChatButton;
