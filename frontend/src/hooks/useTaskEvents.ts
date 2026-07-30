import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import { EventsPollResponse, TaskEvent } from '../types/taskEvent';
import { useWebSocket, type WebSocketMessage } from './useWebSocket';

const DEFAULT_POLL_INTERVAL_MS = 10_000;
const INITIAL_LOOKBACK_MS = 60_000;
const MAX_PAGES_PER_POLL = 10;
const MAX_RETAINED_EVENTS = 100;

export interface UseTaskEventsOptions {
  taskId?: string;
  types?: string[];
  refetchInterval?: number;
  enabled?: boolean;
}

export interface UseTaskEventsResult {
  events: TaskEvent[];
  latestEvent: TaskEvent | null;
  latestDecisionToast: DecisionToast | null;
  loading: boolean;
  error: string | null;
  cursor: string | null;
  unreadCount: number;
  refetch: () => void;
  clearEvents: () => void;
  markAllAsRead: () => void;
}

export interface DecisionToast {
  taskId: string;
  claimedBySessionId: string;
  claimedBySessionLabel: string;
  eventId: string | number;
}

function initialCursor(): string {
  return new Date(Date.now() - INITIAL_LOOKBACK_MS).toISOString();
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object'
    ? (value as Record<string, unknown>)
    : {};
}

function decisionToastFromMessage(
  message: WebSocketMessage,
): DecisionToast | null {
  const normalizedType = message.type.replace(/[_.]/g, '-');
  if (normalizedType !== 'decision-claimed') {
    return null;
  }

  const payload = asRecord(message.payload);
  const taskId = payload.task_id ?? message.task_id;
  const claimedBySessionId =
    payload.claimed_by_session_id ?? message.claimed_by_session_id;
  const eventId = payload.event_id ?? message.event_id;
  const claimedBySessionLabel =
    payload.claimed_by_session_name ??
    message.claimed_by_session_name ??
    claimedBySessionId;

  if (
    typeof taskId !== 'string' ||
    typeof claimedBySessionId !== 'string' ||
    (typeof eventId !== 'string' && typeof eventId !== 'number') ||
    typeof claimedBySessionLabel !== 'string'
  ) {
    return null;
  }

  return {
    taskId,
    claimedBySessionId,
    claimedBySessionLabel,
    eventId,
  };
}

export function useTaskEvents({
  taskId,
  types,
  refetchInterval = DEFAULT_POLL_INTERVAL_MS,
  enabled = true,
}: UseTaskEventsOptions = {}): UseTaskEventsResult {
  const typesKey = types ? [...new Set(types)].sort().join(',') : '';
  const filterKey = `${taskId ?? ''}|${typesKey}`;
  const startingCursor = useRef(initialCursor());

  const [cursor, setCursor] = useState<string | null>(startingCursor.current);
  const [eventsMap, setEventsMap] = useState<Map<number, TaskEvent>>(new Map());
  const [readEventIds, setReadEventIds] = useState<Set<number>>(new Set());
  const [latestDecisionToast, setLatestDecisionToast] =
    useState<DecisionToast | null>(null);

  const cursorRef = useRef<string>(startingCursor.current);
  const activeFilterRef = useRef(filterKey);
  const visibleFilterRef = useRef(filterKey);

  const handleWebSocketMessage = useCallback((message: WebSocketMessage) => {
    const decisionToast = decisionToastFromMessage(message);
    if (decisionToast) {
      setLatestDecisionToast(decisionToast);
    }
  }, []);

  useWebSocket(handleWebSocketMessage);

  useEffect(() => {
    if (visibleFilterRef.current !== filterKey) {
      visibleFilterRef.current = filterKey;
      if (activeFilterRef.current !== filterKey) {
        const nextCursor = initialCursor();
        activeFilterRef.current = filterKey;
        cursorRef.current = nextCursor;
      }
      setCursor(cursorRef.current);
      setEventsMap(new Map());
      setReadEventIds(new Set());
    }
  }, [filterKey]);

  const fetchEvents = useCallback(async () => {
    // A query can begin before the filter-reset effect runs. Resetting the
    // ref here prevents a new task filter from reusing the previous cursor.
    if (activeFilterRef.current !== filterKey) {
      activeFilterRef.current = filterKey;
      cursorRef.current = initialCursor();
    }

    let nextCursor = cursorRef.current;
    const fetchedEvents: TaskEvent[] = [];

    for (let page = 0; page < MAX_PAGES_PER_POLL; page += 1) {
      const params = new URLSearchParams({ since: nextCursor });
      if (taskId) {
        params.set('task_id', taskId);
      }
      if (typesKey) {
        params.set('types', typesKey);
      }

      const response = await api.get<EventsPollResponse>(
        `/events?${params.toString()}`,
      );

      // Ignore an in-flight response from filters that are no longer active.
      if (activeFilterRef.current !== filterKey) {
        return [];
      }

      fetchedEvents.push(...response.events);

      const cursorAdvanced = response.cursor !== nextCursor;
      nextCursor = response.cursor;
      if (!response.has_more || !cursorAdvanced) {
        break;
      }
    }

    if (fetchedEvents.length > 0) {
      setEventsMap((prev) => {
        const next = new Map(prev);
        fetchedEvents.forEach((evt) => {
          next.set(evt.id, evt);
        });

        const recent = Array.from(next.values())
          .sort((a, b) => {
            const byTime =
              new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
            return byTime || b.id - a.id;
          })
          .slice(0, MAX_RETAINED_EVENTS);

        return new Map(recent.map((event) => [event.id, event]));
      });
    }

    cursorRef.current = nextCursor;
    setCursor(nextCursor);

    return fetchedEvents;
  }, [filterKey, taskId, typesKey]);

  const query = useQuery({
    queryKey: ['task-events-polling', filterKey],
    queryFn: fetchEvents,
    refetchInterval,
    enabled,
    staleTime: 5000,
  });

  const eventsList = Array.from(eventsMap.values()).sort(
    (a, b) => {
      const byTime =
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
      return byTime || b.id - a.id;
    },
  );

  const latestEvent = eventsList.length > 0 ? eventsList[0] : null;

  const unreadCount = eventsList.filter((e) => !readEventIds.has(e.id)).length;

  const clearEvents = useCallback(() => {
    setEventsMap(new Map());
    setReadEventIds(new Set());
  }, []);

  const markAllAsRead = useCallback(() => {
    setReadEventIds((prev) => {
      const next = new Set(prev);
      eventsMap.forEach((_, id) => next.add(id));
      return next;
    });
  }, [eventsMap]);

  const refetch = useCallback(() => {
    void query.refetch();
  }, [query]);

  return {
    events: eventsList,
    latestEvent,
    latestDecisionToast,
    loading: query.isLoading,
    error: query.error ? (query.error as Error).message : null,
    cursor,
    unreadCount,
    refetch,
    clearEvents,
    markAllAsRead,
  };
}

export default useTaskEvents;
