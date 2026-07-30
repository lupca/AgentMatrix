import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import toast, { Toaster } from 'react-hot-toast';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useTaskEvents } from '../../hooks/useTaskEvents';
import { NotificationCenter } from './NotificationCenter';

vi.mock('../../hooks/useTaskEvents', () => ({
  useTaskEvents: vi.fn(),
}));

describe('NotificationCenter', () => {
  let container: HTMLDivElement;
  let root: Root;
  const markAllAsRead = vi.fn();

  beforeEach(() => {
    (
      globalThis as typeof globalThis & {
        IS_REACT_ACT_ENVIRONMENT: boolean;
      }
    ).IS_REACT_ACT_ENVIRONMENT = true;
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn().mockImplementation(() => ({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    });
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    markAllAsRead.mockReset();
    vi.mocked(useTaskEvents).mockReturnValue({
      events: [
        {
          id: 42,
          task_id: 'CTV2-116',
          event_type: 'done',
          payload: { result_ref: 'abc123' },
          created_at: '2026-07-28T10:00:00Z',
        },
      ],
      latestEvent: null,
      latestDecisionToast: null,
      loading: false,
      error: null,
      cursor: '2026-07-28T10:00:00Z',
      unreadCount: 1,
      refetch: vi.fn(),
      clearEvents: vi.fn(),
      markAllAsRead,
    });
  });

  afterEach(() => {
    toast.dismiss();
    act(() => root.unmount());
    container.remove();
    (
      globalThis as typeof globalThis & {
        IS_REACT_ACT_ENVIRONMENT: boolean;
      }
    ).IS_REACT_ACT_ENVIRONMENT = false;
  });

  it('shows an unread badge and recent events in the panel', () => {
    act(() => {
      root.render(
        <MemoryRouter>
          <NotificationCenter />
        </MemoryRouter>,
      );
    });

    const trigger = container.querySelector(
      'button[aria-label="Notifications, 1 unread"]',
    ) as HTMLButtonElement;
    expect(trigger).not.toBeNull();
    expect(trigger.textContent).toContain('1');

    act(() => trigger.click());

    expect(container.textContent).toContain('Recent task events');
    expect(container.textContent).toContain('CTV2-116');
    expect(container.textContent).toContain('completed');
    expect(
      container.querySelector('a[href="/tasks/CTV2-116"]'),
    ).not.toBeNull();

    const markReadButton = Array.from(
      container.querySelectorAll('button'),
    ).find((button) => button.textContent?.includes('Mark all read'));
    act(() => markReadButton?.click());
    expect(markAllAsRead).toHaveBeenCalledOnce();
  });

  it('shows a cross-session decision toast and switches to the claimed session', async () => {
    const onNavigateToSession = vi.fn();
    vi.mocked(useTaskEvents).mockReturnValue({
      events: [],
      latestEvent: null,
      latestDecisionToast: {
        taskId: 'CTV2-999',
        claimedBySessionId: 'session-B',
        claimedBySessionLabel: 'Session B',
        eventId: 77,
      },
      loading: false,
      error: null,
      cursor: '2026-07-28T10:00:00Z',
      unreadCount: 0,
      refetch: vi.fn(),
      clearEvents: vi.fn(),
      markAllAsRead,
    });

    await act(async () => {
      root.render(
        <MemoryRouter>
          <NotificationCenter
            activeSessionId="session-A"
            onNavigateToSession={onNavigateToSession}
          />
          <Toaster />
        </MemoryRouter>,
      );
      await Promise.resolve();
    });

    const decisionToast = Array.from(
      document.querySelectorAll<HTMLButtonElement>('button'),
    ).find((button) =>
      button.textContent?.includes(
        'Task CTV2-999 fail — đang xử lý ở session Session B',
      ),
    );
    expect(decisionToast).toBeDefined();

    act(() => decisionToast?.click());
    expect(onNavigateToSession).toHaveBeenCalledOnce();
    expect(onNavigateToSession).toHaveBeenCalledWith('session-B');
  });
});
