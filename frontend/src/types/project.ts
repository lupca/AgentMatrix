export interface Project {
  id: string;
  name: string;
  description?: string | null;
  status: 'active' | 'archived' | 'completed' | 'paused' | string;
  context_md?: string | null;
  repo_root?: string | null;
  task_prefix?: string | null;
  graph_status?: string | null;
  next_task_seq?: number;
  autonomy_policy?: any | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ProjectStats {
  project_id: string;
  project_name: string;
  total_tasks: number;
  done_tasks: number;
  active_tasks: number;
  by_status: Record<string, number>;
}
