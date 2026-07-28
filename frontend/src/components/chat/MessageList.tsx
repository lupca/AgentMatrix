import React, { useRef, useCallback, useEffect } from 'react';
import { ChatMessage, Message } from './ChatMessage';
import { RefreshCw, AlertCircle } from 'lucide-react';

interface MessageListProps {
  messages: Message[];
  loading: boolean;
  error: string | null;
}

export const MessageList: React.FC<MessageListProps> = ({
  messages,
  loading,
  error,
}) => {
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const isNearBottomRef = useRef<boolean>(true);

  const checkIfNearBottom = useCallback(() => {
    const container = messagesContainerRef.current;
    if (!container) return true;
    const threshold = 100;
    return container.scrollHeight - container.scrollTop - container.clientHeight < threshold;
  }, []);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  const handleScroll = useCallback(() => {
    isNearBottomRef.current = checkIfNearBottom();
  }, [checkIfNearBottom]);

  useEffect(() => {
    if (!loading && isNearBottomRef.current) {
      scrollToBottom();
    }
  }, [messages, loading, scrollToBottom]);

  return (
    <div
      ref={messagesContainerRef}
      onScroll={handleScroll}
      className="flex-1 min-h-0 overflow-y-auto overscroll-contain p-4 space-y-2 no-scrollbar"
    >
      {loading ? (
        <div className="flex flex-col items-center justify-center h-full py-12 text-gray-500 space-y-2">
          <RefreshCw className="w-6 h-6 animate-spin text-indigo-500" />
          <span className="text-xs">Loading session history...</span>
        </div>
      ) : (
        <>
          {messages.map((msg) => (
            <ChatMessage key={msg.id} message={msg} />
          ))}
          <div ref={messagesEndRef} />
        </>
      )}

      {error && (
        <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-xl text-red-400 text-xs flex items-center gap-2">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}
    </div>
  );
};

export default MessageList;
