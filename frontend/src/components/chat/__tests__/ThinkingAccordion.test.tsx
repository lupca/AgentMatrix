import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import ThinkingAccordion from '../ThinkingAccordion';

describe('ThinkingAccordion', () => {
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

  it('renders collapsed by default with Thought process header', () => {
    act(() => {
      root.render(<ThinkingAccordion content="Deep reasoning steps" />);
    });

    expect(container.textContent).toContain('Thought process');
    expect(container.textContent).not.toContain('Deep reasoning steps');
  });

  it('expands when button clicked and displays content', () => {
    act(() => {
      root.render(<ThinkingAccordion content="Deep reasoning steps" />);
    });

    const button = container.querySelector('button') as HTMLButtonElement;
    act(() => {
      button.click();
    });

    expect(container.textContent).toContain('Deep reasoning steps');
  });

  it('shows Thinking... pulse indicator when isThinking is true', () => {
    act(() => {
      root.render(<ThinkingAccordion content="In progress" isThinking isStreaming />);
    });

    expect(container.textContent).toContain('Thinking...');
  });
});
