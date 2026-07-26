import React, { useState, useEffect, useRef, useCallback } from 'react';
import { ChatMessage, Message } from './ChatMessage';
import { ChatInput } from './ChatInput';
import { api } from '../../lib/api';
import { Task } from '../../types/task';
import { Bot, RefreshCw, Trash2, AlertCircle } from 'lucide-react';
import {
  DEFAULT_COORDINATOR_MODEL,
  MODELS,
  providerForModel,
} from './ModelSelector';
import { ChatSession, useChat } from '../../hooks/useChat';
import { showSuccess } from '../../lib/toast';

interface ChatPanelProps {
  threadId: string;
  taskId?: string;
  taskTitle?: string;
  task?: Task;
  onTaskAction?: () => Promise<void> | void;
  onClose?: () => void;
  className?: string;
}

export const ChatPanel: React.FC<ChatPanelProps> = ({
  threadId,
  taskId,
  taskTitle,
  task,
  onTaskAction,
  onClose,
  className = '',
}) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [loadingHistory, setLoadingHistory] = useState<boolean>(true);
  const [isStreaming, setIsStreaming] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [selectedProvider, setSelectedProvider] = useState(
    providerForModel(DEFAULT_COORDINATOR_MODEL),
  );

  const { isModelSwitching, updateSessionModel } = useChat(sessionId || threadId);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  // Fetch session history on mount or threadId change
  const fetchSessionHistory = useCallback(async () => {
    setLoadingHistory(true);
    setError(null);
    try {
      let sessionData: any = null;
      try {
        sessionData = await api.get<any>(`/sessions/${threadId}`);
      } catch {
        if (taskId) {
          const sessions = await api.get<any[]>(`/sessions?task_id=${taskId}`);
          if (sessions && sessions.length > 0) {
            sessionData = sessions[0];
          }
        }
      }

      const session = sessionData as ChatSession | null;
      setSessionId(session?.id || null);
      const nextModel = session?.selected_model || null;
      setSelectedModel(nextModel);
      setSelectedProvider(
        (session?.selected_provider as 'anthropic' | 'google' | null) ||
          providerForModel(nextModel || DEFAULT_COORDINATOR_MODEL),
      );

      if (sessionData && Array.isArray(sessionData.messages)) {
        const formattedMessages: Message[] = sessionData.messages.map((m: any) => ({
          id: m.id || `msg-${Math.random()}`,
          role: m.role || 'assistant',
          content: m.content || '',
          timestamp: m.timestamp || new Date().toISOString(),
        }));
        setMessages(formattedMessages);
      } else {
        // Default initial welcome message
        setMessages([
          {
            id: 'welcome-1',
            role: 'assistant',
            content: `Hello! I am Control Tower AI Assistant${
              taskTitle ? ` for task: "${taskTitle}"` : ''
            }. How can I assist you with task specification, code review, or debugging?`,
            timestamp: new Date().toISOString(),
          },
        ]);
      }
    } catch (err: any) {
      console.warn('Could not fetch chat session history:', err);
      setSessionId(null);
      setSelectedModel(DEFAULT_COORDINATOR_MODEL);
      setSelectedProvider(providerForModel(DEFAULT_COORDINATOR_MODEL));
      setMessages([
        {
          id: 'welcome-1',
          role: 'assistant',
          content: `Hello! I am ready to help with this task session. Ask me anything!`,
          timestamp: new Date().toISOString(),
        },
      ]);
    } finally {
      setLoadingHistory(false);
    }
  }, [threadId, taskId, taskTitle]);

  const handleModelChange = async (model: string) => {
    const previousModel = selectedModel;
    const previousProvider = selectedProvider;
    setSelectedModel(model);
    setSelectedProvider(providerForModel(model));

    try {
      const updatedSession = await updateSessionModel(model);
      const persistedModel = updatedSession.selected_model || model;
      setSessionId(updatedSession.id || sessionId || threadId);
      setSelectedModel(persistedModel);
      setSelectedProvider(
        (updatedSession.selected_provider as 'anthropic' | 'google' | null) ||
          providerForModel(persistedModel),
      );
      const modelLabel = MODELS.find((model) => model.value === persistedModel)?.label;
      showSuccess(`Coordinator switched to ${modelLabel || persistedModel}`);
    } catch (err) {
      setSelectedModel(previousModel);
      setSelectedProvider(previousProvider);
      console.error('Failed to switch coordinator model:', err);
    }
  };

  useEffect(() => {
    fetchSessionHistory();
  }, [fetchSessionHistory]);

  useEffect(() => {
    if (!loadingHistory) {
      scrollToBottom();
    }
  }, [messages, loadingHistory, scrollToBottom]);

  // Handle SSE streaming chat message send
  const handleSendMessage = async (userText: string) => {
    if (!userText.trim() || isStreaming) return;

    const userMessageId = `user-${Date.now()}`;
    const userMessage: Message = {
      id: userMessageId,
      role: 'user',
      content: userText,
      timestamp: new Date().toISOString(),
    };

    const assistantMessageId = `assistant-${Date.now()}`;
    const assistantPlaceholder: Message = {
      id: assistantMessageId,
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      isStreaming: true,
    };

    setMessages((prev) => [...prev, userMessage, assistantPlaceholder]);
    setIsStreaming(true);
    setError(null);

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          thread_id: threadId,
          message: userText,
          model: selectedModel || DEFAULT_COORDINATOR_MODEL,
          provider: selectedProvider,
        }),
      });

      if (!response.ok) {
        throw new Error(`Chat API error (${response.status}): ${response.statusText}`);
      }

      if (!response.body) {
        throw new Error('Readable stream not supported in this environment');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      let accumulatedContent = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || ''; // keep incomplete tail in buffer

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data:')) continue;

          const jsonStr = trimmed.replace(/^data:\s*/, '');
          if (!jsonStr) continue;

          try {
            const data = JSON.parse(jsonStr);

            if (data.type === 'start') {
              // Message generation started
            } else if (data.type === 'chunk' && data.content) {
              accumulatedContent += data.content;
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMessageId
                    ? { ...msg, content: accumulatedContent, isStreaming: true }
                    : msg
                )
              );
            } else if (data.type === 'done') {
              const finalContent = data.content ?? accumulatedContent;
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMessageId
                    ? { ...msg, content: finalContent, isStreaming: false }
                    : msg
                )
              );
            } else if (data.type === 'error') {
              const errContent = data.content || 'An error occurred during streaming.';
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMessageId
                    ? {
                        ...msg,
                        content: accumulatedContent + `\n\n[Error: ${errContent}]`,
                        isStreaming: false,
                        error: true,
                      }
                    : msg
                )
              );
            }
          } catch (pErr) {
            console.error('Failed to parse SSE JSON payload:', jsonStr, pErr);
          }
        }
      }

      // Final cleanup to ensure isStreaming is cleared
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === assistantMessageId ? { ...msg, isStreaming: false } : msg
        )
      );
    } catch (err: any) {
      console.error('Error during SSE stream fetch:', err);
      setError(err.message || 'Connection lost to chat SSE endpoint');
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === assistantMessageId
            ? {
                ...msg,
                content:
                  msg.content || 'Failed to connect to AI backend. Please retry.',
                isStreaming: false,
                error: true,
              }
            : msg
        )
      );
    } finally {
      setIsStreaming(false);
    }
  };

  const handleClearHistory = () => {
    if (confirm('Clear chat view history for this session?')) {
      setMessages([
        {
          id: `sys-${Date.now()}`,
          role: 'system',
          content: 'Chat view cleared.',
        },
      ]);
    }
  };

  return (
    <div className={`flex flex-col min-h-0 h-full max-h-[calc(100vh-200px)] bg-gray-900/90 border border-gray-800 rounded-2xl overflow-hidden shadow-2xl backdrop-blur-md ${className}`}>
      {/* Header */}
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
            onClick={fetchSessionHistory}
            disabled={loadingHistory || isStreaming}
            className="p-1.5 rounded-lg text-gray-400 hover:text-gray-100 hover:bg-gray-800 transition-colors disabled:opacity-40"
            title="Refresh history"
          >
            <RefreshCw className={`w-4 h-4 ${loadingHistory ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={handleClearHistory}
            disabled={isStreaming || messages.length === 0}
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

      {/* Messages List Area */}
      <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain p-4 space-y-2 no-scrollbar">
        {loadingHistory ? (
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

      {/* Chat Input */}
      <div className="shrink-0">
        <ChatInput
          onSendMessage={handleSendMessage}
          disabled={loadingHistory}
          isStreaming={isStreaming}
          task={task}
          onTaskAction={onTaskAction}
          currentModel={selectedModel}
          onModelChange={handleModelChange}
          onDefaultModelChange={(model) => {
            setSelectedModel(model);
            setSelectedProvider(providerForModel(model));
          }}
          isModelLoading={isModelSwitching}
          placeholder={`Message Control Tower AI (${taskId ? `Task ${taskId}` : 'Assistant'})...`}
        />
      </div>
    </div>
  );
};

export default ChatPanel;
