export type AgentType = 'cli' | 'api';
export type AgentProvider = 'anthropic' | 'google' | 'openai';

export interface Agent {
  id: string;
  name: string;
  role: string;
  capabilities?: string[] | any[] | null;
  status: 'idle' | 'busy' | 'offline' | string;
  type?: string | null;
  model?: string | null;
  effort?: string | null;
  cli?: string | null;
  agent_type: AgentType;
  provider?: AgentProvider | null;
  base_url?: string | null;
  has_api_key?: boolean;
  is_default?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AgentSuggestion {
  agent_id: string;
  score: number;
  reason: string;
}

export interface AgentStats {
  agent_id: string;
  name: string;
  role: string;
  tasks_executed: number;
  tasks_reviewed: number;
  tasks_completed: number;
  success_rate: number;
  active_tasks: number;
}
