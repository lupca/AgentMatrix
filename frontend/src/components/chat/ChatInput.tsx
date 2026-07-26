import React, { useState, useRef, useEffect } from 'react';
import { Send, Loader2, Sparkles } from 'lucide-react';
import { Task } from '../../types/task';
import { QuickActions } from './QuickActions';
import { ModelSelector } from './ModelSelector';

interface ChatInputProps {
  onSendMessage: (message: string) => Promise<void> | void;
  onTaskAction?: () => Promise<void> | void;
  disabled?: boolean;
  isStreaming?: boolean;
  placeholder?: string;
  task?: Task;
  currentModel?: string | null;
  onModelChange?: (model: string) => void | Promise<void>;
  isModelLoading?: boolean;
}

export const ChatInput: React.FC<ChatInputProps> = ({
  onSendMessage,
  onTaskAction,
  disabled = false,
  isStreaming = false,
  placeholder = 'Ask Control Tower AI assistant...',
  task,
  currentModel,
  onModelChange,
  isModelLoading = false,
}) => {
  const [input, setInput] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const quickPrompts = [
    'Analyze execution plan',
    'Summarize status & gate',
    'Suggest fix for errors',
  ];

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 120)}px`;
    }
  }, [input]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || disabled || isStreaming) return;
    onSendMessage(input.trim());
    setInput('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const handleQuickPromptClick = (prompt: string) => {
    if (disabled || isStreaming) return;
    onSendMessage(prompt);
  };

  return (
    <div className="p-3 border-t border-gray-800/80 bg-gray-950/90 space-y-2">
      {task && (
        <QuickActions
          task={task}
          onSendCommand={onSendMessage}
          onActionComplete={onTaskAction}
          disabled={disabled || isStreaming}
        />
      )}
      {onModelChange && (
        <ModelSelector
          currentModel={currentModel}
          onModelChange={onModelChange}
          disabled={disabled || isStreaming}
          isLoading={isModelLoading}
        />
      )}
      {/* Quick Prompts */}
      <div className="flex flex-wrap items-center gap-1.5 overflow-x-auto pb-1 no-scrollbar">
        <span className="text-[10px] uppercase font-semibold tracking-wider text-gray-500 flex items-center gap-1 mr-1">
          <Sparkles className="w-3 h-3 text-indigo-400" /> Quick:
        </span>
        {quickPrompts.map((prompt, idx) => (
          <button
            key={idx}
            type="button"
            onClick={() => handleQuickPromptClick(prompt)}
            disabled={disabled || isStreaming}
            className="text-[11px] px-2.5 py-1 rounded-full bg-gray-900 border border-gray-800 text-gray-300 hover:text-gray-100 hover:border-indigo-500/40 hover:bg-gray-800 transition-all disabled:opacity-40 disabled:pointer-events-none whitespace-nowrap"
          >
            {prompt}
          </button>
        ))}
      </div>

      <form onSubmit={handleSubmit} className="relative flex items-end gap-2">
        <div className="relative flex-1 bg-gray-900 border border-gray-800 focus-within:border-indigo-500 rounded-xl transition-all shadow-inner">
          <textarea
            ref={textareaRef}
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={disabled || isStreaming}
            placeholder={placeholder}
            className="w-full bg-transparent px-3.5 py-2.5 text-xs sm:text-sm text-gray-100 placeholder-gray-500 focus:outline-none resize-none max-h-32 disabled:opacity-50"
          />
        </div>

        <button
          type="submit"
          disabled={!input.trim() || disabled || isStreaming}
          className="h-10 w-10 shrink-0 rounded-xl bg-indigo-600 hover:bg-indigo-500 active:bg-indigo-700 text-white flex items-center justify-center transition-all shadow-lg shadow-indigo-600/20 disabled:opacity-40 disabled:cursor-not-allowed"
          title="Send message"
        >
          {isStreaming ? (
            <Loader2 className="w-4 h-4 animate-spin text-white" />
          ) : (
            <Send className="w-4 h-4" />
          )}
        </button>
      </form>
      <div className="text-[10px] text-gray-500 text-right pr-1">
        Press <kbd className="px-1 py-0.5 rounded bg-gray-800 text-gray-400 font-mono">Enter</kbd> to send, <kbd className="px-1 py-0.5 rounded bg-gray-800 text-gray-400 font-mono">Shift+Enter</kbd> for line break
      </div>
    </div>
  );
};

export default ChatInput;
