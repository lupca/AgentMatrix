import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import { AgentDetailPage } from '../AgentDetail';

vi.mock('../../lib/api', () => ({
  api: {
    get: vi.fn(),
  },
}));

describe('AgentDetailPage', () => {
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
    (
      globalThis as typeof globalThis & {
        IS_REACT_ACT_ENVIRONMENT: boolean;
      }
    ).IS_REACT_ACT_ENVIRONMENT = false;
  });

  async function flush() {
    await act(async () => {
      for (let index = 0; index < 5; index += 1) {
        await Promise.resolve();
      }
    });
  }

  it('ignores tasks without an id in executor and reviewer lists', async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce({
        id: 'agent-1',
        name: 'Agent One',
        role: 'builder',
        status: 'idle',
        capabilities: [],
        agent_type: 'cli',
      })
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        {
          id: 'TASK-1',
          project: 'project-1',
          title: 'Valid executor task',
          status: 'todo',
          executor: 'agent-1',
          reviewer: null,
        },
        {
          id: null,
          project: 'project-1',
          title: 'Invalid executor task',
          status: 'todo',
          executor: 'agent-1',
          reviewer: null,
        },
        {
          id: 'TASK-2',
          project: 'project-1',
          title: 'Valid reviewer task',
          status: 'in-review',
          executor: null,
          reviewer: 'agent-1',
        },
        {
          id: null,
          project: 'project-1',
          title: 'Invalid reviewer task',
          status: 'in-review',
          executor: null,
          reviewer: 'agent-1',
        },
      ]);

    act(() => {
      root.render(
        <MemoryRouter initialEntries={['/agents/agent-1']}>
          <Routes>
            <Route path="/agents/:id" element={<AgentDetailPage />} />
          </Routes>
        </MemoryRouter>,
      );
    });
    await flush();

    expect(container.textContent).toContain('As Executor (1)');
    expect(container.textContent).not.toContain('Invalid executor task');
    expect(container.textContent).not.toContain('Code Implementation');

    const reviewerButton = Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent?.includes('As Reviewer'),
    );
    expect(reviewerButton).not.toBeNull();
    act(() => reviewerButton?.click());

    expect(container.textContent).toContain('As Reviewer (1)');
    expect(container.textContent).not.toContain('Invalid reviewer task');
  });
});
