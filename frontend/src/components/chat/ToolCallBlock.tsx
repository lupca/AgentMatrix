import React, { useState } from 'react';
import { Wrench, ChevronDown, ChevronUp, Loader2, CheckCircle2, XCircle } from 'lucide-react';

export interface ToolCall {
  id?: string;
  name: string;
  arguments?: Record<string, any> | string;
  result?: string | Record<string, any>;
  status?: 'executing' | 'completed' | 'error';
  isExecuting?: boolean;
}

export interface ToolCallBlockProps {
  toolCall?: ToolCall;
  name?: string;
  arguments?: Record<string, any> | string;
  result?: string | Record<string, any>;
  status?: 'executing' | 'completed' | 'error';
  isExecuting?: boolean;
  defaultExpanded?: boolean;
}

export const ToolCallBlock: React.FC<ToolCallBlockProps> = ({
  toolCall,
  name: propName,
  arguments: propArguments,
  result: propResult,
  status: propStatus,
  isExecuting: propIsExecuting,
  defaultExpanded = false,
}) => {
  const [isExpanded, setIsExpanded] = useState<boolean>(defaultExpanded);

  const name = toolCall?.name ?? propName ?? 'Tool Call';
  const args = toolCall?.arguments ?? propArguments;
  const result = toolCall?.result ?? propResult;
  const isExecuting =
    toolCall?.isExecuting ??
    propIsExecuting ??
    (toolCall?.status === 'executing' || propStatus === 'executing');
  const isError = toolCall?.status === 'error' || propStatus === 'error';

  const formattedArgs =
    typeof args === 'string'
      ? args
      : args
      ? JSON.stringify(args, null, 2)
      : null;

  const formattedResult =
    typeof result === 'string'
      ? result
      : result
      ? JSON.stringify(result, null, 2)
      : null;

  return (
    <div className="my-2 overflow-hidden rounded-xl border border-amber-500/20 bg-amber-950/10 transition-all">
      <button
        type="button"
        onClick={() => setIsExpanded((prev) => !prev)}
        className="flex w-full items-center justify-between px-3 py-2 text-xs text-amber-300/90 transition-colors hover:bg-amber-900/20 hover:text-amber-200"
        aria-expanded={isExpanded}
      >
        <div className="flex items-center gap-2 font-medium">
          <Wrench className="h-3.5 w-3.5 text-amber-400" />
          <span className="font-mono font-semibold text-amber-300">{name}</span>
          {isExecuting ? (
            <div className="flex items-center gap-1 text-[11px] text-amber-400">
              <Loader2 className="h-3 w-3 animate-spin text-amber-400" />
              <span>Executing...</span>
            </div>
          ) : isError ? (
            <span title="Error"><XCircle className="h-3.5 w-3.5 text-red-400" /></span>
          ) : (
            <span title="Completed"><CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" /></span>
          )}
        </div>
        <div className="flex items-center gap-1.5 text-[11px] text-amber-400/70">
          <span>{isExpanded ? 'Collapse' : 'Details'}</span>
          {isExpanded ? (
            <ChevronUp className="h-3.5 w-3.5 text-amber-400" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 text-amber-400" />
          )}
        </div>
      </button>

      {isExpanded && (
        <div className="border-t border-amber-500/20 bg-gray-950/80 p-3 text-xs space-y-2.5">
          {formattedArgs && (
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-wider text-amber-400/80 mb-1">
                Arguments
              </div>
              <pre className="p-2 rounded bg-gray-900/90 border border-gray-800 font-mono text-[11px] text-gray-300 overflow-x-auto whitespace-pre-wrap">
                {formattedArgs}
              </pre>
            </div>
          )}

          {formattedResult && (
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-wider text-amber-400/80 mb-1">
                Result
              </div>
              <pre className="p-2 rounded bg-gray-900/90 border border-gray-800 font-mono text-[11px] text-gray-300 overflow-x-auto whitespace-pre-wrap max-h-60">
                {formattedResult}
              </pre>
            </div>
          )}

          {!formattedArgs && !formattedResult && (
            <div className="text-gray-400 italic text-[11px]">No parameters or result details.</div>
          )}
        </div>
      )}
    </div>
  );
};

export default ToolCallBlock;
