import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ToolPalette, { filterTools } from '../ToolPalette';
import type { ToolInfo } from '../../../hooks/useTools';

const TOOLS: ToolInfo[] = [
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
  {
    name: 'dispatch_task',
    description: 'Assign an executor to a task.',
    slash_alias: '/dispatch',
    tier: 'deferred',
    group: 'task_lifecycle',
  },
  {
    name: 'help',
    description: 'List available commands and tools.',
    slash_alias: '/help',
    tier: 'eager',
    group: 'meta',
  },
];

describe('filterTools', () => {
  it('filters by slash alias prefix', () => {
    expect(filterTools(TOOLS, 'st').map((t) => t.name)).toEqual(['get_status']);
  });

  it('filters by canonical name prefix as a fallback', () => {
    expect(filterTools(TOOLS, 'dispatch').map((t) => t.name)).toEqual(['dispatch_task']);
  });

  it('returns every slash-aliased tool for an empty query', () => {
    expect(filterTools(TOOLS, '').map((t) => t.name)).toEqual([
      'create_task',
      'get_status',
      'dispatch_task',
      'help',
    ]);
  });

  it('is case-insensitive', () => {
    expect(filterTools(TOOLS, 'PM').map((t) => t.name)).toEqual(['create_task']);
  });
});

describe('ToolPalette', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('renders nothing when there are no matches', () => {
    act(() => {
      root.render(
        <ToolPalette
          tools={TOOLS}
          query="zzz"
          activeIndex={0}
          onSelect={vi.fn()}
          onHover={vi.fn()}
        />,
      );
    });
    expect(container.querySelector('[role="listbox"]')).toBeNull();
  });

  it('renders matching entries and highlights the active index', () => {
    act(() => {
      root.render(
        <ToolPalette
          tools={TOOLS}
          query=""
          activeIndex={1}
          onSelect={vi.fn()}
          onHover={vi.fn()}
        />,
      );
    });

    const options = container.querySelectorAll('[role="option"]');
    expect(options).toHaveLength(4);
    expect(options[1].getAttribute('aria-selected')).toBe('true');
    expect(options[0].getAttribute('aria-selected')).toBe('false');
    expect(container.textContent).toContain('/pm');
    expect(container.textContent).toContain('Create a new task.');
  });

  it('invokes onSelect when an entry is clicked', () => {
    const onSelect = vi.fn();
    act(() => {
      root.render(
        <ToolPalette
          tools={TOOLS}
          query="pm"
          activeIndex={0}
          onSelect={onSelect}
          onHover={vi.fn()}
        />,
      );
    });

    const option = container.querySelector('[role="option"]') as HTMLButtonElement;
    act(() => {
      option.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    });

    expect(onSelect).toHaveBeenCalledWith(TOOLS[0]);
  });
});
