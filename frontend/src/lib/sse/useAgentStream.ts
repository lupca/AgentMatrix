import { useEffect, useState } from 'react';
import sseManager, { type SSEEvent } from './SSEManager';

export type AgentStreamStatus = 'pending' | 'running' | 'done' | 'failed';

export interface AgentStreamState {
  lines: string[];
  status: AgentStreamStatus;
}

function toStreamStatus(status: unknown): AgentStreamStatus | null {
  switch (String(status).toLowerCase()) {
    case 'queued':
    case 'pending':
    case 'retrying':
      return 'pending';
    case 'running':
      return 'running';
    case 'success':
    case 'done':
      return 'done';
    case 'failed':
    case 'timeout':
    case 'cancelled':
      return 'failed';
    default:
      return null;
  }
}

export function useAgentStream(runId: string | null): AgentStreamState {
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState<AgentStreamStatus>('pending');

  useEffect(() => {
    setLines([]);
    setStatus('pending');

    if (!runId) return undefined;

    const handleEvent = (event: SSEEvent) => {
      if (event.type === 'history' || event.type === 'stdout') {
        if (typeof event.content === 'string') {
          setLines((currentLines) => [...currentLines, event.content as string]);
        }
        return;
      }

      if (event.type === 'status') {
        const nextStatus = toStreamStatus(event.status);
        if (nextStatus) setStatus(nextStatus);
        return;
      }

      if (event.type === 'error') {
        setStatus('failed');
        return;
      }

      if (event.type === 'done') {
        setStatus((currentStatus) =>
          currentStatus === 'failed' ? currentStatus : 'done',
        );
      }
    };

    return sseManager.subscribe(runId, handleEvent);
  }, [runId]);

  return { lines, status };
}

export default useAgentStream;
