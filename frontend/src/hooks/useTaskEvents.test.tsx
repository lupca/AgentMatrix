import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../lib/api';
import { useTaskEvents, type UseTaskEventsResult } from './useTaskEvents';

vi.mock('../lib/api', () => ({
  api: {
    get: vi.fn(),
  },
}));

describe('useTaskEvents', () => {
  let container: HTMLDivElement;
  let root: Root;
  let queryClient: QueryClient;
  let result: UseTaskEventsResult | null;

  beforeEach(() => {
    (
      globalThis as typeof globalThis & {
        IS_REACT_ACT_ENVIRONMENT: boolean;
      }
    ).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-28T10:00:00.000Z'));
    vi.mocked(api.get).mockReset();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    result = null;
  });

  afterEach(() => {
    act(() => root.unmount());
    queryClient.clear();
    container.remove();
    vi.useRealTimers();
    (
      globalThis as typeof globalThis & {
        IS_REACT_ACT_ENVIRONMENT: boolean;
      }
    ).IS_REACT_ACT_ENVIRONMENT = false;
  });

  function Probe() {
    result = useTaskEvents({
      taskId: 'CTV2-116',
      types: ['failed', 'done'],
    });
    return null;
  }

  async function flush() {
    await act(async () => {
      for (let index = 0; index < 5; index += 1) {
        await Promise.resolve();
      }
    });
  }

  it('polls from the last minute, advances the cursor, and polls again after 10s', async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce({
        events: [
          {
            id: 1,
            task_id: 'CTV2-116',
            event_type: 'done',
            payload: {},
            created_at: '2026-07-28T09:59:30.000Z',
          },
        ],
        cursor: '2026-07-28T09:59:30.000Z',
        has_more: false,
      })
      .mockResolvedValueOnce({
        events: [],
        cursor: '2026-07-28T09:59:30.000Z',
        has_more: false,
      });

    act(() => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <Probe />
        </QueryClientProvider>,
      );
    });
    await flush();

    expect(api.get).toHaveBeenCalledTimes(1);
    const firstUrl = new URL(
      vi.mocked(api.get).mock.calls[0][0],
      'http://control-tower.test',
    );
    expect(firstUrl.pathname).toBe('/events');
    expect(firstUrl.searchParams.get('since')).toBe(
      '2026-07-28T09:59:00.000Z',
    );
    expect(firstUrl.searchParams.get('task_id')).toBe('CTV2-116');
    expect(firstUrl.searchParams.get('types')).toBe('done,failed');
    expect(result?.events).toHaveLength(1);
    expect(result?.cursor).toBe('2026-07-28T09:59:30.000Z');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    await flush();

    expect(api.get).toHaveBeenCalledTimes(2);
    const secondUrl = new URL(
      vi.mocked(api.get).mock.calls[1][0],
      'http://control-tower.test',
    );
    expect(secondUrl.searchParams.get('since')).toBe(
      '2026-07-28T09:59:30.000Z',
    );
  });
});
