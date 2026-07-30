import React from 'react';
import { Link } from 'react-router-dom';
import {
  Activity,
  AlertCircle,
  Bot,
  Check,
  Copy,
  LockKeyhole,
  User,
} from 'lucide-react';
import MessageContent from './MessageContent';
import type { ToolCall } from './ToolCallBlock';

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  kind?: string;
  task_id?: string;
  event_type?: string;
  result?: string;
  claimed_by_session_id?: string;
  claimed_by_session_name?: string;
  payload?: Record<string, unknown>;
  timestamp?: string;
  isStreaming?: boolean;
  error?: boolean;
  toolCalls?: ToolCall[];
}

interface ChatMessageProps {
  message: Message;
}

export const ChatMessage: React.FC<ChatMessageProps> = ({ message }) => {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const isUser = message.role === 'user';
  const isSystem = message.role === 'system';
  const isDigest =
    message.kind === 'digest' ||
    message.kind === 'system-event' ||
    message.kind === 'task_event_digest';

  if (isDigest) {
    const payload = message.payload ?? {};
    const taskId =
      message.task_id ??
      (typeof payload.task_id === 'string' ? payload.task_id : undefined);
    const result =
      message.result ??
      (typeof payload.result === 'string' ? payload.result : undefined) ??
      message.event_type;
    const claimedBySessionId =
      message.claimed_by_session_id ??
      (typeof payload.claimed_by_session_id === 'string'
        ? payload.claimed_by_session_id
        : undefined);
    const claimedBySessionLabel =
      message.claimed_by_session_name ??
      (typeof payload.claimed_by_session_name === 'string'
        ? payload.claimed_by_session_name
        : undefined) ??
      claimedBySessionId;
    const isFailure =
      result === 'fail' ||
      result === 'failed' ||
      message.event_type === 'run_failed';

    return (
      <article
        className={`my-3 rounded-xl border px-4 py-3 ${
          claimedBySessionId
            ? 'border-amber-500/30 bg-amber-500/5'
            : 'border-cyan-500/25 bg-cyan-500/5'
        }`}
        data-message-kind="digest"
      >
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span
            className="inline-flex items-center gap-1.5 rounded-full border border-cyan-500/30 bg-cyan-500/10 px-2 py-0.5 font-semibold uppercase tracking-wide text-cyan-300"
            data-testid="task-event-badge"
          >
            <Activity className="h-3 w-3" />
            Task event
          </span>
          {taskId && (
            <Link
              to={`/tasks/${encodeURIComponent(taskId)}`}
              className="font-mono font-semibold text-indigo-300 underline decoration-indigo-500/50 underline-offset-2 hover:text-indigo-200"
            >
              {taskId}
            </Link>
          )}
          {result && (
            <span
              className={`rounded px-1.5 py-0.5 font-mono font-semibold ${
                isFailure
                  ? 'bg-red-500/15 text-red-300'
                  : 'bg-gray-800 text-gray-300'
              }`}
            >
              {result}
            </span>
          )}
        </div>

        {message.content && (
          <p className="mt-2 whitespace-pre-wrap text-sm text-gray-300">
            {message.content}
          </p>
        )}

        {claimedBySessionId && (
          <div
            className="mt-2 flex items-center gap-1.5 text-xs font-medium text-amber-300"
            aria-label="Read-only claimed decision"
          >
            <LockKeyhole className="h-3.5 w-3.5" />
            <span>
              đang xử lý ở session {claimedBySessionLabel}
            </span>
          </div>
        )}
      </article>
    );
  }

  if (isSystem) {
    return (
      <div className="flex justify-center my-2">
        <div className="px-3 py-1 rounded-full bg-gray-800/80 border border-gray-700/60 text-xs text-gray-400 font-mono flex items-center gap-1.5 max-w-md text-center">
          <span>{message.content}</span>
        </div>
      </div>
    );
  }

  return (
    <div className={`flex gap-3 my-3 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
      <div
        className={`w-8 h-8 rounded-xl flex items-center justify-center shrink-0 text-sm font-semibold shadow-md ${
          isUser
            ? 'bg-gradient-to-tr from-indigo-500 to-purple-600 text-white'
            : message.error
            ? 'bg-red-500/20 text-red-400 border border-red-500/30'
            : 'bg-gray-800 text-indigo-400 border border-gray-700'
        }`}
      >
        {isUser ? <User className="w-4 h-4" /> : <Bot className="w-4 h-4" />}
      </div>

      <div
        className={`group relative max-w-[85%] rounded-2xl p-3.5 text-sm shadow-md transition-all ${
          isUser
            ? 'bg-indigo-600 text-white rounded-tr-none'
            : message.error
            ? 'bg-red-950/40 border border-red-500/30 text-red-200 rounded-tl-none'
            : 'bg-gray-900/90 border border-gray-800 text-gray-200 rounded-tl-none'
        }`}
      >
        <div className="flex items-center justify-between gap-4 mb-1.5 border-b border-white/10 pb-1 text-[11px] text-gray-400 font-medium">
          <span className={isUser ? 'text-indigo-200' : 'text-indigo-400 font-semibold'}>
            {isUser ? 'Operator' : 'Control Tower AI'}
          </span>
          <div className="flex items-center gap-2">
            {message.timestamp && (
              <span>
                {new Date(message.timestamp).toLocaleTimeString([], {
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </span>
            )}
            <button
              onClick={handleCopy}
              className="opacity-0 group-hover:opacity-100 transition-opacity p-0.5 hover:text-white rounded"
              title="Copy message"
            >
              {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
            </button>
          </div>
        </div>

        <div className="font-sans text-xs sm:text-sm">
          <MessageContent
            content={message.content}
            isStreaming={message.isStreaming}
            toolCalls={message.toolCalls}
          />
          {message.isStreaming && (
            <span className="inline-block w-2 h-4 ml-1 bg-indigo-400 animate-pulse align-middle" />
          )}
        </div>

        {message.error && (
          <div className="mt-2 flex items-center gap-1.5 text-xs text-red-400 font-medium">
            <AlertCircle className="w-3.5 h-3.5" />
            <span>Failed to generate response</span>
          </div>
        )}
      </div>
    </div>
  );
};

export default ChatMessage;
