import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { ChatMessage, Message } from './ChatMessage';
import { RefreshCw, AlertCircle } from 'lucide-react';
import {
  useWebSocket,
  type WebSocketMessage,
} from '../../hooks/useWebSocket';

interface MessageListProps {
  messages: Message[];
  loading: boolean;
  error: string | null;
  sessionId?: string | null;
}

export const MessageList: React.FC<MessageListProps> = ({
  messages,
  loading,
  error,
  sessionId,
}) => {
  const [realtimeMessages, setRealtimeMessages] = useState<Message[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const isNearBottomRef = useRef<boolean>(true);

  useEffect(() => {
    setRealtimeMessages([]);
  }, [sessionId]);

  const handleWebSocketMessage = useCallback(
    (event: WebSocketMessage) => {
      if (
        event.type !== 'coordinator_message' ||
        !sessionId ||
        event.session_id !== sessionId
      ) {
        return;
      }

      const raw = event.message;
      if (!raw || typeof raw !== 'object') {
        return;
      }

      const role =
        raw.role === 'user' || raw.role === 'system'
          ? raw.role
          : 'assistant';
      const message: Message = {
        ...raw,
        id:
          typeof raw.id === 'string'
            ? raw.id
            : `coordinator-${String(event.source_event_id ?? Date.now())}`,
        role,
        content: typeof raw.content === 'string' ? raw.content : '',
        timestamp:
          typeof raw.timestamp === 'string'
            ? raw.timestamp
            : new Date().toISOString(),
      };

      setRealtimeMessages((current) =>
        current.some((item) => item.id === message.id)
          ? current
          : [...current, message],
      );
      isNearBottomRef.current = true;
    },
    [sessionId],
  );

  useWebSocket(handleWebSocketMessage);

  const visibleMessages = useMemo(() => {
    const persistedIds = new Set(messages.map((message) => message.id));
    return [
      ...messages,
      ...realtimeMessages.filter((message) => !persistedIds.has(message.id)),
    ];
  }, [messages, realtimeMessages]);

  const checkIfNearBottom = useCallback(() => {
    const container = messagesContainerRef.current;
    if (!container) return true;
    const threshold = 100;
    return container.scrollHeight - container.scrollTop - container.clientHeight < threshold;
  }, []);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView?.({ behavior: 'smooth' });
  }, []);

  const handleScroll = useCallback(() => {
    isNearBottomRef.current = checkIfNearBottom();
  }, [checkIfNearBottom]);

  useEffect(() => {
    if (!loading && isNearBottomRef.current) {
      scrollToBottom();
    }
  }, [visibleMessages, loading, scrollToBottom]);

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
          {visibleMessages.map((msg) => (
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
