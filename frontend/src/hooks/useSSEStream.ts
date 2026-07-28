import { useRef, useCallback } from 'react';
import { Message } from '../components/chat/ChatMessage';
import { CoordinatorProvider } from '../components/chat/ModelSelector';

interface SSEStreamOptions {
  threadId: string;
  selectedModel: string | null;
  selectedProvider: CoordinatorProvider | null;
  onMessagesUpdate: (updater: (prev: Message[]) => Message[]) => void;
  onStreamingChange: (streaming: boolean) => void;
  onError: (error: string | null) => void;
}

interface SSEStreamReturn {
  sendMessage: (userText: string, isStreaming: boolean) => Promise<void>;
  abort: () => void;
}

export function useSSEStream({
  threadId,
  selectedModel,
  selectedProvider,
  onMessagesUpdate,
  onStreamingChange,
  onError,
}: SSEStreamOptions): SSEStreamReturn {
  const streamAbortRef = useRef<AbortController | null>(null);

  const abort = useCallback(() => {
    streamAbortRef.current?.abort();
  }, []);

  const sendMessage = useCallback(async (userText: string, isCurrentlyStreaming: boolean) => {
    if (!userText.trim() || isCurrentlyStreaming) return;
    if (!selectedModel) {
      onError('Chat is still loading the default coordinator model. Please try again shortly.');
      return;
    }

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

    onMessagesUpdate((prev) => [...prev, userMessage, assistantPlaceholder]);
    onStreamingChange(true);
    onError(null);

    streamAbortRef.current?.abort();
    const controller = new AbortController();
    streamAbortRef.current = controller;

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          thread_id: threadId,
          message: userText,
          model: selectedModel,
          provider: selectedProvider,
        }),
        signal: controller.signal,
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
        if (controller.signal.aborted) break;
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data:')) continue;

          const jsonStr = trimmed.replace(/^data:\s*/, '');
          if (!jsonStr) continue;

          try {
            const data = JSON.parse(jsonStr);

            if (data.type === 'chunk' && data.content) {
              accumulatedContent += data.content;
              onMessagesUpdate((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMessageId
                    ? { ...msg, content: accumulatedContent, isStreaming: true }
                    : msg
                )
              );
            } else if (data.type === 'tool_start' || data.type === 'tool_call' || data.type === 'tool_use') {
              const toolName = data.tool || data.name || data.tool_name || 'tool';
              const toolArgs = data.args || data.arguments || data.input || {};
              const toolId = data.id || data.tool_id || data.tool_call_id || `tool-${Date.now()}`;

              onMessagesUpdate((prev) =>
                prev.map((msg) => {
                  if (msg.id !== assistantMessageId) return msg;
                  const existing = msg.toolCalls || [];
                  const updated = [
                    ...existing.filter((t) => t.id !== toolId),
                    { id: toolId, name: toolName, arguments: toolArgs, status: 'executing' as const, isExecuting: true },
                  ];
                  return { ...msg, toolCalls: updated };
                })
              );
            } else if (data.type === 'tool_result' || data.type === 'tool_done' || data.type === 'tool_end') {
              const toolId = data.id || data.tool_id || data.tool_call_id;
              const toolResult = data.result ?? data.output ?? data.content;

              onMessagesUpdate((prev) =>
                prev.map((msg) => {
                  if (msg.id !== assistantMessageId) return msg;
                  const existing = msg.toolCalls || [];
                  const updated = existing.map((t) => {
                    if (toolId ? t.id === toolId : t.isExecuting) {
                      return { ...t, result: toolResult, status: data.error ? ('error' as const) : ('completed' as const), isExecuting: false };
                    }
                    return t;
                  });
                  return { ...msg, toolCalls: updated };
                })
              );
            } else if (data.type === 'done') {
              const finalContent = data.content ?? accumulatedContent;
              onMessagesUpdate((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMessageId
                    ? { ...msg, content: finalContent, isStreaming: false }
                    : msg
                )
              );
            } else if (data.type === 'error') {
              const errContent = data.content || 'An error occurred during streaming.';
              onMessagesUpdate((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMessageId
                    ? { ...msg, content: accumulatedContent + `\n\n[Error: ${errContent}]`, isStreaming: false, error: true }
                    : msg
                )
              );
            }
          } catch (pErr) {
            console.error('Failed to parse SSE JSON payload:', jsonStr, pErr);
          }
        }
      }

      onMessagesUpdate((prev) =>
        prev.map((msg) =>
          msg.id === assistantMessageId ? { ...msg, isStreaming: false } : msg
        )
      );
    } catch (err: any) {
      if (err?.name === 'AbortError') return;
      console.error('Error during SSE stream fetch:', err);
      onError(err.message || 'Connection lost to chat SSE endpoint');
      onMessagesUpdate((prev) =>
        prev.map((msg) =>
          msg.id === assistantMessageId
            ? { ...msg, content: msg.content || 'Failed to connect to AI backend. Please retry.', isStreaming: false, error: true }
            : msg
        )
      );
    } finally {
      if (!controller.signal.aborted) {
        onStreamingChange(false);
      }
    }
  }, [threadId, selectedModel, selectedProvider, onMessagesUpdate, onStreamingChange, onError]);

  return { sendMessage, abort };
}
