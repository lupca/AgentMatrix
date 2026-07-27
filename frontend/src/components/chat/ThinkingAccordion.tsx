import React, { useState } from 'react';
import { Brain, ChevronDown, ChevronUp } from 'lucide-react';

export interface ThinkingAccordionProps {
  content: string;
  isThinking?: boolean;
  isStreaming?: boolean;
  defaultExpanded?: boolean;
}

export const ThinkingAccordion: React.FC<ThinkingAccordionProps> = ({
  content,
  isThinking = false,
  isStreaming = false,
  defaultExpanded = false,
}) => {
  const [isExpanded, setIsExpanded] = useState<boolean>(defaultExpanded);

  const activeThinking = isStreaming && isThinking;

  return (
    <div className="my-2 overflow-hidden rounded-xl border border-purple-500/20 bg-purple-950/10 transition-all">
      <button
        type="button"
        onClick={() => setIsExpanded((prev) => !prev)}
        className="flex w-full items-center justify-between px-3 py-2 text-xs text-purple-300/90 transition-colors hover:bg-purple-900/20 hover:text-purple-200"
        aria-expanded={isExpanded}
      >
        <div className="flex items-center gap-2 font-medium">
          <Brain
            className={`h-4 w-4 text-purple-400 ${
              activeThinking ? 'animate-pulse text-purple-300' : ''
            }`}
          />
          {activeThinking ? (
            <div className="flex items-center gap-1.5 font-semibold text-purple-300">
              <span>Thinking...</span>
              <span className="h-1.5 w-1.5 rounded-full bg-purple-400 animate-ping inline-block" />
            </div>
          ) : (
            <span className="font-semibold">Thought process</span>
          )}
        </div>
        <div className="flex items-center gap-1.5 text-[11px] text-purple-400/70">
          <span>{isExpanded ? 'Collapse' : 'Expand'}</span>
          {isExpanded ? (
            <ChevronUp className="h-3.5 w-3.5 text-purple-400" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 text-purple-400" />
          )}
        </div>
      </button>

      {isExpanded && (
        <div className="border-t border-purple-500/20 bg-gray-950/70 p-3 text-xs leading-relaxed text-gray-300 font-mono whitespace-pre-wrap max-h-96 overflow-y-auto">
          {content || (activeThinking ? 'Generating thought process...' : '')}
        </div>
      )}
    </div>
  );
};

export default ThinkingAccordion;
