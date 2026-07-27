import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import ToolCallBlock from '../ToolCallBlock';

describe('ToolCallBlock', () => {
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

  it('renders tool name and collapsed state by default', () => {
    act(() => {
      root.render(
        <ToolCallBlock
          name="search_files"
          arguments={{ query: 'think' }}
          result="Found 3 matches"
        />,
      );
    });

    expect(container.textContent).toContain('search_files');
    expect(container.textContent).not.toContain('Found 3 matches');
  });

  it('expands to show arguments and result', () => {
    act(() => {
      root.render(
        <ToolCallBlock
          name="search_files"
          arguments={{ query: 'think' }}
          result="Found 3 matches"
        />,
      );
    });

    const button = container.querySelector('button') as HTMLButtonElement;
    act(() => {
      button.click();
    });

    expect(container.textContent).toContain('query');
    expect(container.textContent).toContain('Found 3 matches');
  });

  it('displays Executing indicator when isExecuting is true', () => {
    act(() => {
      root.render(
        <ToolCallBlock
          name="search_files"
          isExecuting
        />,
      );
    });

    expect(container.textContent).toContain('Executing...');
  });
});
