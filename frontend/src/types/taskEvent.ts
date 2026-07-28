export interface TaskEvent {
  id: number;
  task_id: string;
  event_type:
    | 'dispatched'
    | 'running'
    | 'done'
    | 'failed'
    | 'cancelled'
    | 'gate_pending'
    | 'gate_passed'
    | 'gate_rejected'
    | 'retrying'
    | string;
  payload: Record<string, unknown>;
  created_at: string;
  consumed_at?: string | null;
}

export interface EventsPollResponse {
  events: TaskEvent[];
  cursor: string;
  has_more: boolean;
}
