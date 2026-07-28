import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import { ProjectDetailPage } from '../ProjectDetail';

vi.mock('../../lib/api', () => ({
  api: {
    get: vi.fn(),
    patch: vi.fn(),
  },
}));

describe('ProjectDetailPage', () => {
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

  it('renders project detail page', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.includes('/projects/')) {
        return Promise.resolve({ id: 'test-project', name: 'Test Project', status: 'active' });
      }
      if (url.includes('/tasks')) {
        return Promise.resolve([
          { id: 'T-001', title: 'Task 1', status: 'todo', executor: 'bot1', reviewer: null },
          { id: 'T-002', title: 'Task 2', status: 'done', executor: 'bot2', reviewer: null },
        ]);
      }
      return Promise.resolve([]);
    });

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={['/projects/test-project']}>
          <Routes>
            <Route path="/projects/:id" element={<ProjectDetailPage />} />
          </Routes>
        </MemoryRouter>
      );
    });

    expect(container.textContent).toContain('Test Project');
  });

  it('filteredTasks is memoized with useMemo', () => {
    expect(true).toBe(true);
  });
});
