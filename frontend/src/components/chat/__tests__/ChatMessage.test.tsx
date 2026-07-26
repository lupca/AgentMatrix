import React, { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import ChatMessage, { type Message } from '../ChatMessage';

describe('ChatMessage Markdown rendering', () => {
  let container: HTMLDivElement;
  let root: Root;
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const renderMessage = (content: string, role: Message['role'] = 'assistant') => {
    act(() => {
      root.render(
        <ChatMessage
          message={{ id: 'message-1', role, content }}
        />,
      );
    });
  };

  it('renders common Markdown elements and sanitizes unsafe HTML and URLs', () => {
    renderMessage(
      '# Heading\n\n**bold** *italic* ~~removed~~\n\n- one\n- two\n\n1. first\n2. second\n\n[Safe link](https://example.com) [Unsafe link](javascript:alert(1))\n\n| Name | Value |\n| --- | --- |\n| region | prod |\n\n<script>alert(1)</script>',
    );

    expect(container.querySelector('h1')?.textContent).toBe('Heading');
    expect(container.querySelector('strong')?.textContent).toBe('bold');
    expect(container.querySelector('em')?.textContent).toBe('italic');
    expect(container.querySelector('del')?.textContent).toBe('removed');
    expect(container.querySelector('ul li')?.textContent).toBe('one');
    expect(container.querySelector('ol li')?.textContent).toBe('first');
    expect(container.querySelectorAll('table tbody tr')).toHaveLength(1);
    expect(container.querySelector('a[href="https://example.com"]')).not.toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });

  it('renders fenced code with syntax highlighting and copies the block', async () => {
    renderMessage('```typescript\nconst answer: number = 42;\n```');

    const code = container.querySelector('pre code');
    expect(code?.textContent).toContain('const answer: number = 42;');
    expect(code?.querySelectorAll('span').length).toBeGreaterThan(0);
    expect(container.querySelector('[aria-label="Copy code"]')).not.toBeNull();

    await act(async () => {
      container.querySelector<HTMLButtonElement>('[aria-label="Copy code"]')?.click();
    });

    expect(writeText).toHaveBeenCalledWith('const answer: number = 42;');
    expect(container.querySelector('[aria-label="Copy code"]')?.textContent).toContain('Copied');
  });

  it('renders Markdown in user messages while retaining the chat copy action', () => {
    renderMessage('**operator note**', 'user');

    expect(container.querySelector('strong')?.textContent).toBe('operator note');
    expect(container.querySelector('[title="Copy message"]')).not.toBeNull();
  });
});
