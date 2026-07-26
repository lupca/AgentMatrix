import React, { act } from 'react';
import { createRoot, Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ModelSelector } from './ModelSelector';
import { api } from '../../lib/api';
import { updateSessionModel } from '../../hooks/useChat';

vi.mock('../../lib/api', () => ({
  api: {
    patch: vi.fn(),
    get: vi.fn().mockResolvedValue([
      { id: 'agent-1', name: 'Kimi K3', model: 'kimi-k3', provider: 'openai', is_default: true },
      { id: 'agent-2', name: 'Gemini', model: 'gemini-2.5-pro', provider: 'google', is_default: false },
    ]),
  },
}));

describe('ModelSelector', () => {
  let container: HTMLDivElement;
  let root: Root;

  afterEach(() => {
    act(() => root?.unmount());
    container?.remove();
  });

  function render(onModelChange = vi.fn()) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root.render(
        <ModelSelector
          currentModel={null}
          onModelChange={onModelChange}
        />,
      );
    });
    return onModelChange;
  }

  it('shows a loading state before the coordinator API responds', async () => {
    let resolveAgents: (agents: any[]) => void = () => {};
    vi.mocked(api.get).mockImplementationOnce(() => new Promise<any[]>((resolve) => {
      resolveAgents = resolve;
    }));

    render();

    expect(container.textContent).toContain('Loading models...');

    await act(async () => {
      resolveAgents([]);
    });
  });

  it('loads coordinator models from the API and selects the DB default', async () => {
    vi.mocked(api.get).mockResolvedValueOnce([
      { id: 'agent-1', name: 'Kimi K3', model: 'kimi-k3', provider: 'openai', is_default: true },
      { id: 'agent-2', name: 'Gemini', model: 'gemini-2.5-pro', provider: 'google', is_default: false },
    ]);

    const onDefaultModelChange = vi.fn();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root.render(
        <ModelSelector
          currentModel={null}
          onModelChange={vi.fn()}
          onDefaultModelChange={onDefaultModelChange}
        />,
      );
    });

    const options = container.querySelectorAll('option');
    expect(options).toHaveLength(2);
    expect((container.querySelector('select') as HTMLSelectElement).value).toBe('kimi-k3');
    expect(onDefaultModelChange).toHaveBeenCalledWith('kimi-k3', 'openai');
  });

  it('shows the current model and emits the selected value', async () => {
    vi.mocked(api.get).mockResolvedValueOnce([
      { id: 'agent-1', name: 'Kimi K3', model: 'kimi-k3', provider: 'openai', is_default: true },
      { id: 'agent-2', name: 'Gemini', model: 'gemini-2.5-pro', provider: 'google', is_default: false },
    ]);
    const onModelChange = vi.fn();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root.render(<ModelSelector currentModel="kimi-k3" onModelChange={onModelChange} />);
    });
    const select = container.querySelector('select');

    expect(select).not.toBeNull();
    expect((select as HTMLSelectElement).value).toBe('kimi-k3');

    act(() => {
      (select as HTMLSelectElement).value = 'gemini-2.5-pro';
      select?.dispatchEvent(new Event('change', { bubbles: true }));
    });

    expect(onModelChange).toHaveBeenCalledWith('gemini-2.5-pro');
  });

  it('disables the selector and shows progress while switching', async () => {
    render();
    await act(async () => {
      await Promise.resolve();
    });

    const select = container.querySelector('select') as HTMLSelectElement;

    act(() => {
      root.render(
        <ModelSelector
          currentModel="kimi-k3"
          onModelChange={vi.fn()}
          isLoading
        />,
      );
    });

    expect(select.disabled).toBe(true);
    expect(container.querySelector('[aria-label="Switching model"]')).not.toBeNull();
  });

  it('persists a model selection with its provider', async () => {
    vi.mocked(api.patch).mockResolvedValue({
      id: 'session-1',
      selected_model: 'gemini-2.5-pro',
      selected_provider: 'google',
    });

    await updateSessionModel('session-1', 'gemini-2.5-pro');

    expect(api.patch).toHaveBeenCalledWith('/sessions/session-1', {
      selected_model: 'gemini-2.5-pro',
      selected_provider: 'google',
    });
  });
});
