import React, { useEffect, useState, useCallback } from 'react';
import { api } from '../lib/api';
import { TokenStatsApiResponse, TokenComparisonApiResponse } from '../types/token';
import { TokenOverviewCards } from '../components/dashboard/TokenOverviewCards';
import { TokenUsageBars } from '../components/dashboard/TokenUsageBars';
import { ModelPricingTable } from '../components/dashboard/ModelPricingTable';
import { Activity, RefreshCw, AlertCircle, DollarSign } from 'lucide-react';

interface PricingRow {
  id: number;
  model: string;
  provider: string;
  input_price_per_mtok: number | null;
  output_price_per_mtok: number | null;
  cached_input_price_per_mtok: number | null;
  cache_write_5m_per_mtok?: number | null;
  cache_write_1h_per_mtok?: number | null;
  notes?: string | null;
}

export const TokensPage: React.FC = () => {
  const [tokenStats, setTokenStats] = useState<TokenStatsApiResponse | null>(null);
  const [tokenComparison, setTokenComparison] = useState<TokenComparisonApiResponse | null>(null);
  const [pricing, setPricing] = useState<PricingRow[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date>(new Date());
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
  const [showPricing, setShowPricing] = useState<boolean>(false);

  const fetchTokenData = useCallback(async (isManualRefresh = false) => {
    if (isManualRefresh) {
      setIsRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);

    try {
      const [usage, comparison, pricingData] = await Promise.all([
        api.get<TokenStatsApiResponse>('/stats/tokens'),
        api.get<TokenComparisonApiResponse>('/stats/tokens/comparison'),
        api.get<PricingRow[]>('/stats/pricing')
      ]);

      setTokenStats(usage);
      setTokenComparison(comparison);
      setPricing(pricingData);
      setLastRefreshed(new Date());
    } catch (err: any) {
      console.warn('Failed to fetch token telemetry data:', err);
      setError(err?.message || 'Failed to connect to backend tokens API.');
    } finally {
      setLoading(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchTokenData();
  }, [fetchTokenData]);

  return (
    <div className="p-6 space-y-8 max-w-7xl mx-auto min-h-[calc(100vh-4rem)]">
      {/* Top Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 bg-gray-900/40 p-6 rounded-2xl border border-gray-800/80 shadow-lg backdrop-blur-md">
        <div>
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-purple-500/10 border border-purple-500/20 shadow-inner">
              <Activity className="w-5 h-5 text-purple-400" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-gray-100 tracking-tight">
                Token Telemetry
              </h1>
              <p className="text-gray-400 text-sm mt-0.5">
                Detailed usage ledgers, cost analysis, and model performance metrics.
              </p>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3 self-start md:self-auto">
          <span className="text-xs text-gray-500 font-mono hidden sm:inline-block">
            Updated: {lastRefreshed.toLocaleTimeString()}
          </span>
          <button
            onClick={() => fetchTokenData(true)}
            disabled={isRefreshing || loading}
            className="flex items-center gap-2 px-4 py-2 rounded-xl bg-gray-800 hover:bg-gray-700 text-gray-200 border border-gray-700 font-medium text-xs transition-all disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isRefreshing ? 'animate-spin' : ''}`} />
            <span>{isRefreshing ? 'Refreshing...' : 'Refresh'}</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <AlertCircle className="w-4 h-4 text-amber-400 flex-shrink-0" />
            <span>{error}</span>
          </div>
          <button
            onClick={() => fetchTokenData(true)}
            className="underline hover:text-amber-200 font-medium flex-shrink-0"
          >
            Retry Connection
          </button>
        </div>
      )}

      {/* Main Stats Overview */}
      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-xl backdrop-blur-sm relative overflow-hidden">
        <div className="absolute top-0 right-0 p-32 bg-purple-500/5 blur-[100px] rounded-full pointer-events-none" />
        
        <div className="flex flex-col gap-2 border-b border-gray-800/80 pb-5">
          <h2 className="text-lg font-semibold text-gray-100 flex items-center gap-2">
            Telemetry Overview
          </h2>
        </div>

        <TokenOverviewCards 
          tokenStats={tokenStats}
          tokenComparison={tokenComparison}
          loading={loading}
        />
      </div>

      {/* Breakdown Grids */}
      {!loading && tokenStats && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
          <div className="rounded-xl border border-gray-800/80 bg-gray-900/40 p-6 shadow-lg backdrop-blur-sm">
            <TokenUsageBars 
              title="Usage by Model"
              subtitle="Token distribution across LLM models"
              items={tokenStats.by_model || []}
              emptyMessage="No model usage recorded."
            />
          </div>
          <div className="rounded-xl border border-gray-800/80 bg-gray-900/40 p-6 shadow-lg backdrop-blur-sm">
            <TokenUsageBars 
              title="Usage by Provider"
              subtitle="Token distribution across API providers"
              items={tokenStats.by_provider || []}
              emptyMessage="No provider usage recorded."
            />
          </div>
          <div className="rounded-xl border border-gray-800/80 bg-gray-900/40 p-6 shadow-lg backdrop-blur-sm xl:col-span-2">
            <TokenUsageBars
              title="Usage by Operation"
              subtitle="Token distribution by internal system operation"
              items={tokenStats.by_operation || []}
            />
          </div>
        </div>
      )}

      {/* Model Pricing Section */}
      <div className="rounded-xl border border-gray-800/80 bg-gray-900/40 p-6 shadow-lg backdrop-blur-sm">
        <button
          onClick={() => setShowPricing(!showPricing)}
          className="w-full flex items-center justify-between text-left"
        >
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-500/10 border border-emerald-500/20">
              <DollarSign className="w-4 h-4 text-emerald-400" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-gray-100">Model Pricing</h2>
              <p className="text-xs text-gray-500">
                {pricing.length} models configured
              </p>
            </div>
          </div>
          <span className="text-gray-500 text-sm">
            {showPricing ? 'Hide' : 'Show'} pricing table
          </span>
        </button>

        {showPricing && (
          <div className="mt-6 pt-4 border-t border-gray-800/50">
            <ModelPricingTable pricing={pricing} loading={loading} />
          </div>
        )}
      </div>
    </div>
  );
};

export default TokensPage;
