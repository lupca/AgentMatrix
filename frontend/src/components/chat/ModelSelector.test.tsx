import React, { act } from 'react';
import { createRoot, Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_COORDINATOR_MODEL, MODELS, ModelSelector } from './ModelSelector';
import { api } from '../../lib/api';
import { updateSessionModel } from '../../hooks/useChat';

vi.mock('../../lib/api', () => ({
  api: {
    patch: vi.fn(),
    get: vi.fn().mockResolvedValue([]),
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
          currentModel={DEFAULT_COORDINATOR_MODEL}
          onModelChange={onModelChange}
        />,
      );
    });
    return onModelChange;
  }

  it('renders all supported coordinator models', () => {
    render();

    const options = container.querySelectorAll('option');
    expect(options).toHaveLength(4);
    expect(Array.from(options).map((option) => option.textContent)).toEqual(
      MODELS.map((model) => model.label),
    );
  });

  it('shows the current model and emits the selected value', () => {
    const onModelChange = render();
    const select = container.querySelector('select');

    expect(select).not.toBeNull();
    expect((select as HTMLSelectElement).value).toBe(DEFAULT_COORDINATOR_MODEL);

    act(() => {
      (select as HTMLSelectElement).value = 'gemini-2.5-pro';
      select?.dispatchEvent(new Event('change', { bubbles: true }));
    });

    expect(onModelChange).toHaveBeenCalledWith('gemini-2.5-pro');
  });

  it('disables the selector and shows progress while switching', () => {
    render();
    const select = container.querySelector('select') as HTMLSelectElement;

    act(() => {
      root.render(
        <ModelSelector
          currentModel={DEFAULT_COORDINATOR_MODEL}
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
