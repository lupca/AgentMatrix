export interface Project {
  id: string;
  name: string;
  description?: string | null;
  status: 'active' | 'archived' | 'completed' | 'paused' | string;
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
