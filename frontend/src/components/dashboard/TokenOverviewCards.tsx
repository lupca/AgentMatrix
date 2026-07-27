import React from 'react';
import { TokenStatsApiResponse, TokenComparisonApiResponse } from '../../types/token';
import { formatTokens } from '../../utils/formatters';

interface TokenOverviewCardsProps {
  tokenStats?: TokenStatsApiResponse | null;
  tokenComparison?: TokenComparisonApiResponse | null;
  loading?: boolean;
}

export const TokenOverviewCards: React.FC<TokenOverviewCardsProps> = ({
  tokenStats,
  tokenComparison,
  loading
}) => {
  if (loading) {
    return (
      <div className="mt-5 grid grid-cols-2 gap-4 md:grid-cols-4 lg:grid-cols-5">
        {[1, 2, 3, 4, 5].map((item) => (
          <div key={item} className="h-24 animate-pulse rounded-lg bg-gray-800/60" />
        ))}
      </div>
    );
  }

  return (
    <div className="mt-5 grid grid-cols-2 gap-4 lg:grid-cols-5">
      <div className="rounded-lg border border-purple-500/20 bg-purple-500/5 p-4 transition-all hover:bg-purple-500/10 hover:shadow-lg hover:shadow-purple-500/10">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">Total tokens</p>
        <p className="mt-1 text-2xl font-bold text-gray-100">
          {formatTokens(tokenStats?.total_tokens ?? 0)}
        </p>
        <p className="mt-1 text-xs text-purple-400">{tokenStats?.total_calls ?? 0} LLM calls</p>
      </div>
      
      <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-4 transition-all hover:bg-gray-800/60 hover:shadow-lg">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">Input</p>
        <p className="mt-1 text-xl font-bold text-gray-200">
          {formatTokens(tokenStats?.total_input_tokens ?? 0)}
        </p>
        <p className="mt-1 text-xs text-gray-500">
          {formatTokens(tokenStats?.total_cached_tokens ?? 0)} cached
        </p>
      </div>
      
      <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-4 transition-all hover:bg-gray-800/60 hover:shadow-lg">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">Output</p>
        <p className="mt-1 text-xl font-bold text-gray-200">
          {formatTokens(tokenStats?.total_output_tokens ?? 0)}
        </p>
        <p className="mt-1 text-xs text-gray-500">
          {Math.round(tokenStats?.average_latency_ms ?? 0).toLocaleString()} ms avg
        </p>
      </div>
      
      <div className="rounded-lg border border-emerald-500/10 bg-gray-950/50 p-4 transition-all hover:bg-emerald-500/5 hover:shadow-lg hover:border-emerald-500/20">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">Estimated cost</p>
        <p className="mt-1 text-xl font-bold text-emerald-400">
          ${(tokenStats?.total_cost_usd ?? 0).toFixed(4)}
        </p>
        <p className="mt-1 text-xs text-gray-500">Provider list pricing</p>
      </div>
      
      <div className="col-span-2 rounded-lg border border-indigo-500/20 bg-indigo-500/5 p-4 lg:col-span-1 transition-all hover:bg-indigo-500/10 hover:shadow-lg hover:shadow-indigo-500/10">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">V1 reduction</p>
        <p className={`mt-1 text-2xl font-bold ${
          (tokenComparison?.reduction_percentage ?? 0) >= 80
            ? 'text-emerald-400'
            : 'text-indigo-300'
        }`}>
          {(tokenComparison?.reduction_percentage ?? 0).toFixed(1)}%
        </p>
        <p className="mt-1 text-xs text-gray-500">
          {tokenComparison?.cycle_count ?? 0} measured cycles
        </p>
      </div>
    </div>
  );
};
