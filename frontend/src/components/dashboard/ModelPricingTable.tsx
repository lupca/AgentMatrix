import React from 'react';
import { DollarSign, Sparkles, Zap } from 'lucide-react';

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

interface Props {
  pricing: PricingRow[];
  loading?: boolean;
}

const providerColors: Record<string, string> = {
  anthropic: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  google: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  openai: 'bg-green-500/20 text-green-400 border-green-500/30',
};

const providerIcons: Record<string, React.ReactNode> = {
  anthropic: <Sparkles className="w-3 h-3" />,
  google: <Zap className="w-3 h-3" />,
  openai: <DollarSign className="w-3 h-3" />,
};

function formatPrice(price: number | null): string {
  if (price === null || price === undefined) return '-';
  if (price < 1) return `$${price.toFixed(3)}`;
  return `$${price.toFixed(2)}`;
}

export const ModelPricingTable: React.FC<Props> = ({ pricing, loading }) => {
  if (loading) {
    return (
      <div className="animate-pulse space-y-3">
        {[1, 2, 3, 4].map((i) => (
          <div key={i} className="h-10 bg-gray-800/50 rounded-lg" />
        ))}
      </div>
    );
  }

  if (!pricing || pricing.length === 0) {
    return (
      <div className="text-center py-8 text-gray-500">
        No pricing data available.
      </div>
    );
  }

  const groupedByProvider = pricing.reduce((acc, row) => {
    const provider = row.provider || 'other';
    if (!acc[provider]) acc[provider] = [];
    acc[provider].push(row);
    return acc;
  }, {} as Record<string, PricingRow[]>);

  return (
    <div className="space-y-6">
      {Object.entries(groupedByProvider).map(([provider, rows]) => (
        <div key={provider} className="space-y-3">
          <div className="flex items-center gap-2">
            <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium border ${providerColors[provider] || 'bg-gray-500/20 text-gray-400 border-gray-500/30'}`}>
              {providerIcons[provider]}
              {provider.charAt(0).toUpperCase() + provider.slice(1)}
            </span>
            <span className="text-xs text-gray-500">{rows.length} models</span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 text-xs uppercase tracking-wider">
                  <th className="pb-2 pr-4 font-medium">Model</th>
                  <th className="pb-2 px-3 font-medium text-right">Input</th>
                  <th className="pb-2 px-3 font-medium text-right">Output</th>
                  <th className="pb-2 px-3 font-medium text-right">Cached</th>
                  {provider === 'anthropic' && (
                    <>
                      <th className="pb-2 px-3 font-medium text-right">5m Write</th>
                      <th className="pb-2 px-3 font-medium text-right">1h Write</th>
                    </>
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/50">
                {rows.map((row) => (
                  <tr key={row.id} className="hover:bg-gray-800/30 transition-colors">
                    <td className="py-2.5 pr-4">
                      <div className="font-medium text-gray-200">{row.model}</div>
                      {row.notes && (
                        <div className="text-xs text-gray-500 mt-0.5">{row.notes}</div>
                      )}
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-gray-300">
                      {formatPrice(row.input_price_per_mtok)}
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-gray-300">
                      {formatPrice(row.output_price_per_mtok)}
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-emerald-400">
                      {formatPrice(row.cached_input_price_per_mtok)}
                    </td>
                    {provider === 'anthropic' && (
                      <>
                        <td className="py-2.5 px-3 text-right font-mono text-gray-400">
                          {formatPrice(row.cache_write_5m_per_mtok ?? null)}
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono text-gray-400">
                          {formatPrice(row.cache_write_1h_per_mtok ?? null)}
                        </td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      <p className="text-xs text-gray-500 pt-2 border-t border-gray-800/50">
        Prices shown per 1M tokens (MTok). Cached pricing applies to prompt cache hits.
      </p>
    </div>
  );
};

export default ModelPricingTable;
