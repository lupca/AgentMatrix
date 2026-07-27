export interface TokenUsageBreakdown {
  operation?: string;
  model?: string;
  provider?: string;
  session_id?: string;
  task_id?: string;
  
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  cost_usd: number;
  average_latency_ms: number;
}

export interface TokenStatsApiResponse {
  total_calls: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cached_tokens: number;
  total_tokens: number;
  total_cost_usd: number;
  average_latency_ms: number;
  
  by_session?: TokenUsageBreakdown[];
  by_task?: TokenUsageBreakdown[];
  by_operation?: TokenUsageBreakdown[];
  by_model?: TokenUsageBreakdown[];
  by_provider?: TokenUsageBreakdown[];
}

export interface TokenComparisonApiResponse {
  baseline_input_tokens_per_cycle: number;
  v1_baseline_tokens_per_cycle?: number;
  cycle_count: number;
  v1_estimated_input_tokens: number;
  v2_input_tokens: number;
  v2_input_tokens_per_cycle: number;
  tokens_saved: number;
  reduction_percentage: number;
  target_reduction_percentage: number;
  target_met: boolean;
}
