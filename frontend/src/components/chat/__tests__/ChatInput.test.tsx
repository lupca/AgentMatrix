import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import ChatInput from '../ChatInput';
import { api } from '../../../lib/api';

vi.mock('../../../lib/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue({
      tools: [
        {
          name: 'create_task',
          description: 'Create a new task.',
          slash_alias: '/pm',
          tier: 'eager',
          group: 'task_lifecycle',
        },
        {
          name: 'get_status',
          description: 'Get the status of a task.',
          slash_alias: '/status',
          tier: 'eager',
          group: 'query',
        },
      ],
    }),
  },
}));

describe('ChatInput tool palette', () => {
  let container: HTMLDivElement;
  let root: Root;
  let queryClient: QueryClient;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const renderInput = (onSendMessage = vi.fn()) => {
    act(() => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <ChatInput onSendMessage={onSendMessage} />
        </QueryClientProvider>,
      );
    });
    return onSendMessage;
  };

  const flush = async () => {
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
  };

  // React overrides the textarea's `value` setter on the instance to track
  // controlled-input state, so a plain `textarea.value = x` doesn't produce a
  // change React's onChange notices. Go through the prototype-level native
  // setter instead (same trick @testing-library/user-event uses) so the
  // dispatched 'input' event is seen as an actual value change.
  const nativeTextareaValueSetter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype,
    'value',
  )!.set!;

  const typeInto = (textarea: HTMLTextAreaElement, value: string) => {
    act(() => {
      nativeTextareaValueSetter.call(textarea, value);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
  };

  it('does not show the palette for plain text', async () => {
    renderInput();
    await flush();

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    typeInto(textarea, 'hello world');

    expect(container.querySelector('[role="listbox"]')).toBeNull();
  });

  it('shows filtered palette entries while composing a slash command', async () => {
    renderInput();
    await flush();

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    typeInto(textarea, '/st');

    const options = container.querySelectorAll('[role="option"]');
    expect(options).toHaveLength(1);
    expect(container.textContent).toContain('/status');
  });

  it('hides the palette once a space starts the argument text', async () => {
    renderInput();
    await flush();

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    typeInto(textarea, '/status ');

    expect(container.querySelector('[role="listbox"]')).toBeNull();
  });

  it('fills the input with the selected alias on Enter and does not submit', async () => {
    const onSendMessage = renderInput();
    await flush();

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    typeInto(textarea, '/st');

    act(() => {
      textarea.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
      );
    });

    expect(textarea.value).toBe('/status ');
    expect(onSendMessage).not.toHaveBeenCalled();
    expect(container.querySelector('[role="listbox"]')).toBeNull();
  });

  it('navigates entries with arrow keys before selecting', async () => {
    renderInput();
    await flush();

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    typeInto(textarea, '/');

    act(() => {
      textarea.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }),
      );
    });
    expect(container.querySelectorAll('[role="option"]')[1].getAttribute('aria-selected')).toBe(
      'true',
    );

    act(() => {
      textarea.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
      );
    });
    expect(textarea.value).toBe('/status ');
  });

  it('dismisses the palette on Escape without clearing the input', async () => {
    renderInput();
    await flush();

    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    typeInto(textarea, '/st');
    expect(container.querySelector('[role="listbox"]')).not.toBeNull();

    act(() => {
      textarea.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
      );
    });

    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(textarea.value).toBe('/st');
  });
});
