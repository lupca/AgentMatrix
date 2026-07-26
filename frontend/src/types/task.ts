export type TaskStatus = 'todo' | 'dispatched' | 'in-review' | 'done';

export interface Task {
  id: string;
  session_id?: string | null;
  project: string;
  title: string;
  raw_input?: string | null;
  status: TaskStatus | string;
  current_gate?: string | null;
  mode?: string | null;
  priority?: string | null;
  risk?: string | null;
  executor?: string | null;
  reviewer?: string | null;
  acceptance_criteria?: string[] | null;
  files?: string[] | null;
  tests?: string[] | null;
  flows?: string[] | null;
  plan?: string | null;
  result_ref?: string | null;
  findings?: string[] | null;
  verdict?: string | null;
  predicted_success?: string | null;
  prediction_factors?: Record<string, any> | null;
  awaiting_approval?: boolean | null;
  approval_prompt?: string | null;
  error?: string | null;
  deadline?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  dispatched_at?: string | null;
  completed_at?: string | null;
}

export interface TaskFiltersState {
  search: string;
  project: string;
  status: string;
  priority: string;
  risk: string;
}
