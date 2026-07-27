import React from 'react';
import { TokenUsageBreakdown } from '../../types/token';
import { formatTokens } from '../../utils/formatters';

interface TokenUsageBarsProps {
  title: string;
  subtitle?: string;
  items: TokenUsageBreakdown[];
  emptyMessage?: string;
}

export const TokenUsageBars: React.FC<TokenUsageBarsProps> = ({
  title,
  subtitle = 'Input + output tokens',
  items,
  emptyMessage = 'No LLM usage has been recorded yet.'
}) => {
  const maxTokens = Math.max(1, ...(items || []).map((item) => item.total_tokens));

  const getItemLabel = (item: TokenUsageBreakdown): string => {
    const label = item.operation || item.model || item.provider || item.task_id || item.session_id || 'Unknown';
    return label.replace(/_/g, ' ');
  };

  return (
    <div className="mt-5">
      <div className="mb-4 flex items-center justify-between border-b border-gray-800/80 pb-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400">
          {title}
        </h3>
        <span className="text-[11px] text-gray-500">{subtitle}</span>
      </div>
      
      {!items || items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-gray-800 p-5 text-center text-xs text-gray-500">
          {emptyMessage}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {items.map((item, idx) => (
            <div
              key={idx}
              className="group rounded-lg border border-gray-800/80 bg-gray-950/40 p-4 transition-all hover:bg-gray-800/40 hover:border-gray-700/80"
            >
              <div className="mb-2 flex items-center justify-between text-xs">
                <span className="font-medium capitalize text-gray-300 group-hover:text-white transition-colors">
                  {getItemLabel(item)}
                </span>
                <span className="font-mono text-gray-400 font-semibold group-hover:text-indigo-400 transition-colors">
                  {formatTokens(item.total_tokens)}
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-gray-900 shadow-inner">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-indigo-500 via-purple-500 to-pink-500 transition-all duration-1000 ease-out"
                  style={{ width: `${(item.total_tokens / maxTokens) * 100}%` }}
                />
              </div>
              <div className="mt-3 flex items-center justify-between text-[11px] text-gray-500">
                <p>
                  <span className="text-gray-400">{item.calls}</span> call{item.calls === 1 ? '' : 's'}
                </p>
                <p>
                  <span className="text-gray-400">{formatTokens(item.input_tokens)}</span> in
                  <span className="mx-1 text-gray-700">|</span>
                  <span className="text-gray-400">{formatTokens(item.output_tokens)}</span> out
                </p>
                <p className="text-emerald-500/80 font-mono">
                  ${item.cost_usd.toFixed(4)}
                </p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
