export type SSEEventType =
  | 'history'
  | 'stdout'
  | 'status'
  | 'done'
  | 'error'
  | string;

export interface SSEEvent {
  type: SSEEventType;
  lastEventId?: string;
  [key: string]: unknown;
}

export type SSEEventHandler = (event: SSEEvent) => void;

interface ConnectionState {
  source: EventSource | null;
  lastEventId: number;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  replaying: boolean;
}

const RECONNECT_DELAY_MS = 2_000;
const EVENT_TYPES = ['history', 'stdout', 'status', 'done', 'error'];

/** Shared EventSource connection pool for agent run output streams. */
export class SSEManager {
  private readonly connections = new Map<string, ConnectionState>();

  private readonly subscribers = new Map<string, Set<SSEEventHandler>>();

  subscribe(runId: string, onEvent: SSEEventHandler): () => void {
    if (!runId) {
      return () => undefined;
    }

    let subscribers = this.subscribers.get(runId);
    if (!subscribers) {
      subscribers = new Set<SSEEventHandler>();
      this.subscribers.set(runId, subscribers);
    }
    subscribers.add(onEvent);

    const connection = this.connections.get(runId);
    if (!connection || (!connection.source && !connection.reconnectTimer)) {
      this.connect(runId);
    }

    return () => this.unsubscribe(runId, onEvent);
  }

  /** Close a run's connection and cancel any pending reconnect. */
  close(runId: string): void {
    const connection = this.connections.get(runId);
    if (connection?.reconnectTimer) {
      clearTimeout(connection.reconnectTimer);
    }
    connection?.source?.close();
    this.connections.delete(runId);
    this.subscribers.delete(runId);
  }

  private unsubscribe(runId: string, onEvent: SSEEventHandler): void {
    const subscribers = this.subscribers.get(runId);
    if (!subscribers) return;

    subscribers.delete(onEvent);
    if (subscribers.size === 0) {
      this.close(runId);
    }
  }

  private connect(runId: string): void {
    const subscribers = this.subscribers.get(runId);
    if (!subscribers?.size) return;

    const connection = this.connections.get(runId) ?? {
      source: null,
      lastEventId: 0,
      reconnectTimer: null,
      replaying: false,
    };

    if (connection.source || connection.replaying) return;
    connection.reconnectTimer = null;
    this.connections.set(runId, connection);

    if (connection.lastEventId > 0) {
      connection.replaying = true;
      void this.replay(runId, connection).finally(() => {
        if (this.connections.get(runId) === connection) {
          connection.replaying = false;
          this.openEventSource(runId, connection);
        }
      });
      return;
    }

    this.openEventSource(runId, connection);
  }

  private openEventSource(runId: string, connection: ConnectionState): void {
    const url = this.getStreamUrl(runId, connection.lastEventId);
    let source: EventSource;
    try {
      source = new EventSource(url);
    } catch {
      this.scheduleReconnect(runId);
      return;
    }

    connection.source = source;

    const handleEvent = (event: Event): void => {
      if (this.connections.get(runId)?.source !== source) return;

      const message = event as MessageEvent<string>;
      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(message.data) as Record<string, unknown>;
      } catch {
        return;
      }

      const eventId = message.lastEventId || undefined;
      const numericEventId = eventId ? Number(eventId) : NaN;
      if (Number.isInteger(numericEventId) && numericEventId >= 0) {
        connection.lastEventId = numericEventId;
      }

      const eventType =
        typeof payload.type === 'string' ? payload.type : message.type;
      const normalizedEvent: SSEEvent = {
        ...payload,
        type: eventType,
        ...(eventId ? { lastEventId: eventId } : {}),
      };

      this.notify(runId, normalizedEvent);
      if (eventType === 'done') {
        this.close(runId);
      }
    };

    if (typeof source.addEventListener === 'function') {
      for (const eventType of EVENT_TYPES) {
        source.addEventListener(eventType, handleEvent);
      }
    }
    // Named events use addEventListener above; the default SSE event uses
    // onmessage. Keeping both covers the native API and lightweight test mocks.
    source.onmessage = handleEvent as (event: MessageEvent) => void;

    source.onerror = () => {
      if (this.connections.get(runId)?.source !== source) return;
      source.close();
      connection.source = null;
      this.scheduleReconnect(runId);
    };
  }

  private async replay(runId: string, connection: ConnectionState): Promise<void> {
    try {
      const response = await fetch(
        `/api/runs/${encodeURIComponent(runId)}/output?after_seq=${connection.lastEventId}`,
      );
      if (!response.ok) return;
      const body = (await response.json()) as {
        chunks?: Array<{ seq?: unknown; content?: unknown }>;
      };
      for (const chunk of body.chunks ?? []) {
        const seq = typeof chunk.seq === 'number' ? chunk.seq : NaN;
        if (!Number.isInteger(seq) || seq <= connection.lastEventId) continue;
        connection.lastEventId = seq;
        this.notify(runId, {
          type: 'history',
          content: chunk.content,
          seq,
          lastEventId: String(seq),
        });
      }
    } catch {
      // EventSource remains the durable retry mechanism if replay is unavailable.
    }
  }

  private scheduleReconnect(runId: string): void {
    const connection = this.connections.get(runId);
    if (!connection || !this.subscribers.get(runId)?.size) return;
    if (connection.reconnectTimer) return;

    connection.reconnectTimer = setTimeout(() => {
      const current = this.connections.get(runId);
      if (!current || !this.subscribers.get(runId)?.size) return;
      current.reconnectTimer = null;
      this.connect(runId);
    }, RECONNECT_DELAY_MS);
  }

  private getStreamUrl(runId: string, lastEventId: number): string {
    const path = `/api/runs/${encodeURIComponent(runId)}/stream`;
    return lastEventId > 0 ? `${path}?last_event_id=${lastEventId}` : path;
  }

  private notify(runId: string, event: SSEEvent): void {
    const subscribers = this.subscribers.get(runId);
    if (!subscribers) return;

    for (const subscriber of [...subscribers]) {
      subscriber(event);
    }
  }
}

export const sseManager = new SSEManager();

export default sseManager;
