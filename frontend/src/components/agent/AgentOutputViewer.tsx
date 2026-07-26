import React, { useEffect, useRef, useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { useAgentStream } from '../../lib/sse/useAgentStream';

export interface AgentOutputViewerProps {
  /** Pass lines directly, or pass runId to stream them from the backend. */
  lines?: string[];
  runId?: string | null;
  className?: string;
  maxHeight?: string;
}

export const AgentOutputViewer: React.FC<AgentOutputViewerProps> = ({
  lines,
  runId = null,
  className = '',
  maxHeight = 'max-h-96',
}) => {
  const stream = useAgentStream(runId);
  const outputLines = lines ?? stream.lines;
  const outputRef = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const output = outputRef.current;
    if (output) output.scrollTop = output.scrollHeight;
  }, [outputLines]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(outputLines.join('\n'));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2_000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <section className={`relative overflow-hidden rounded-xl border border-gray-800 bg-gray-950/80 ${className}`}>
      <div className="flex items-center justify-between border-b border-gray-800 px-4 py-2">
        <span className="font-mono text-xs font-semibold uppercase tracking-wider text-gray-500">
          Agent output
        </span>
        <button
          type="button"
          onClick={handleCopy}
          className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-gray-400 transition-colors hover:bg-gray-800 hover:text-gray-100"
          title="Copy agent output"
          aria-label="Copy agent output"
        >
          {copied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre
        ref={outputRef}
        className={`${maxHeight} min-h-24 overflow-y-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-relaxed text-gray-300`}
        aria-label="Agent output"
      >
        {outputLines.join('\n')}
      </pre>
    </section>
  );
};

export default AgentOutputViewer;
