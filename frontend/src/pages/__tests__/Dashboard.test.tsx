import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import { Dashboard } from '../Dashboard';

vi.mock('../../lib/api', () => ({
  api: {
    get: vi.fn(),
  },
}));

vi.mock('../../lib/store', () => ({
  useAppStore: () => ({ user: { name: 'test' } }),
}));

describe('Dashboard', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (
      globalThis as typeof globalThis & {
        IS_REACT_ACT_ENVIRONMENT: boolean;
      }
    ).IS_REACT_ACT_ENVIRONMENT = true;
    vi.mocked(api.get).mockReset();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('fetches all dashboard stats in parallel via Promise.all', async () => {
    const callOrder: string[] = [];

    vi.mocked(api.get).mockImplementation((url: string) => {
      callOrder.push(url);
      if (url === '/stats/overview') {
        return Promise.resolve({ totalTasks: 10, completedTasks: 5 });
      }
      if (url === '/projects') {
        return Promise.resolve([{ id: 'p1', name: 'Project 1', status: 'active' }]);
      }
      if (url === '/stats/tokens') {
        return Promise.resolve({ total_tokens: 1000 });
      }
      if (url === '/stats/tokens/comparison') {
        return Promise.resolve({ tokens_saved: 500 });
      }
      if (url === '/stats/projects') {
        return Promise.resolve([{ project_id: 'p1', total_tasks: 10, done_tasks: 5 }]);
      }
      return Promise.resolve({});
    });

    await act(async () => {
      root.render(
        <MemoryRouter>
          <Dashboard />
        </MemoryRouter>
      );
    });

    expect(api.get).toHaveBeenCalledWith('/stats/overview');
    expect(api.get).toHaveBeenCalledWith('/projects');
    expect(api.get).toHaveBeenCalledWith('/stats/tokens');
    expect(api.get).toHaveBeenCalledWith('/stats/tokens/comparison');
    expect(api.get).toHaveBeenCalledWith('/stats/projects');
    expect(callOrder.length).toBe(5);
  });
});
