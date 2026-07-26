import React from 'react';
import { Bot, User, Copy, Check, AlertCircle } from 'lucide-react';
import MessageContent from './MessageContent';

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp?: string;
  isStreaming?: boolean;
  error?: boolean;
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
          <MessageContent content={message.content} />
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
