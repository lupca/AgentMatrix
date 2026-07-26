import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api';

export type ContextLevel = 'global' | 'project' | 'task';

export interface SessionContext {
  level: ContextLevel;
  project_id?: string | null;
  task_id?: string | null;
}

export interface ChatSessionSummary {
  id: string;
  title?: string | null;
  context_level?: ContextLevel | null;
  project_id?: string | null;
  task_id?: string | null;
  thread_id?: string | null;
  status?: string | null;
  pinned?: boolean | null;
  selected_model?: string | null;
  selected_provider?: string | null;
  messages?: Array<Record<string, unknown>> | null;
  created_at?: string | null;
  updated_at?: string | null;
}

interface UseSessionsResult {
  sessions: ChatSessionSummary[];
  activeSessionId: string | null;
  activeSession: ChatSessionSummary | null;
  loading: boolean;
  error: string | null;
  refetch: () => Promise<void>;
  createSession: (title?: string) => Promise<ChatSessionSummary>;
  switchSession: (sessionId: string) => void;
  closeSession: (sessionId: string) => Promise<void>;
}

/** Fetch/create/switch chat sessions scoped to a context level (global/project/task). */
export function useSessions(context: SessionContext): UseSessionsResult {
  const { level, project_id, task_id } = context;

  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const query = useMemo(() => {
    const params = new URLSearchParams();
    params.set('context_level', level);
    if (project_id) params.set('project_id', project_id);
    if (task_id) params.set('task_id', task_id);
    params.set('status', 'active');
    return params.toString();
  }, [level, project_id, task_id]);

  const abortRef = useRef<AbortController | null>(null);

  const fetchSessions = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setError(null);
    try {
      const result = await api.get<ChatSessionSummary[]>(`/sessions?${query}`, {
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      const list = Array.isArray(result) ? result : [];
      setSessions(list);
      setActiveSessionId((current) =>
        current && list.some((s) => s.id === current) ? current : list[0]?.id || null,
      );
    } catch (err: any) {
      if (controller.signal.aborted || err?.name === 'AbortError') return;
      setError(err?.message || 'Failed to load sessions');
      setSessions([]);
      setActiveSessionId(null);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    fetchSessions();
    return () => abortRef.current?.abort();
  }, [fetchSessions]);

  const createSession = useCallback(
    async (title?: string) => {
      const created = await api.post<ChatSessionSummary>('/sessions', {
        context_level: level,
        project_id: project_id || undefined,
        task_id: task_id || undefined,
        thread_id: `${task_id || project_id || 'global'}-${Date.now()}-${Math.random()
          .toString(36)
          .slice(2, 8)}`,
        title: title || null,
      });
      setSessions((prev) => [...prev, created]);
      setActiveSessionId(created.id);
      return created;
    },
    [level, project_id, task_id],
  );

  const switchSession = useCallback((sessionId: string) => {
    setActiveSessionId(sessionId);
  }, []);

  const closeSession = useCallback(
    async (sessionId: string) => {
      try {
        await api.patch<ChatSessionSummary>(`/sessions/${sessionId}`, { status: 'closed' });
      } catch (err) {
        console.warn('Failed to persist session close, removing locally only:', err);
      }

      const remaining = sessions.filter((s) => s.id !== sessionId);
      setSessions(remaining);

      if (activeSessionId !== sessionId) return;

      if (remaining.length > 0) {
        setActiveSessionId(remaining[0].id);
      } else {
        // Closing the last session would otherwise leave the panel writing to a
        // closed session via the stale threadId fallback, so replace it.
        await createSession();
      }
    },
    [sessions, activeSessionId, createSession],
  );

  const activeSession = sessions.find((s) => s.id === activeSessionId) || null;

  return {
    sessions,
    activeSessionId,
    activeSession,
    loading,
    error,
    refetch: fetchSessions,
    createSession,
    switchSession,
    closeSession,
  };
}

export default useSessions;
