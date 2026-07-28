import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { StatCard } from '../stat-card';

// @ts-expect-error global IS_REACT_ACT_ENVIRONMENT flag for React 19
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe('StatCard', () => {
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

  it('renders label and value correctly', () => {
    act(() => {
      root.render(<StatCard label="Total Tasks" value={42} />);
    });

    expect(container.textContent).toContain('Total Tasks');
    expect(container.textContent).toContain('42');
  });

  it('renders icon when icon prop is provided', () => {
    act(() => {
      root.render(
        <StatCard
          label="Active Agents"
          value="8"
          icon={<span data-testid="custom-icon">IconElement</span>}
        />
      );
    });

    const iconEl = container.querySelector('[data-testid="custom-icon"]');
    expect(iconEl).not.toBeNull();
    expect(iconEl?.textContent).toBe('IconElement');
  });

  it('does not render icon element when icon prop is not provided', () => {
    act(() => {
      root.render(<StatCard label="Revenue" value="$10,000" />);
    });

    expect(container.querySelector('[data-testid="custom-icon"]')).toBeNull();
    // The icon container div should not be rendered
    const flexContainers = container.querySelectorAll('.flex.h-10.w-10');
    expect(flexContainers.length).toBe(0);
  });

  it('renders trend information when provided', () => {
    act(() => {
      root.render(
        <StatCard
          label="Completion Rate"
          value="95%"
          trend={{ value: '+5%', isPositive: true, label: 'vs last week' }}
        />
      );
    });

    expect(container.textContent).toContain('↑');
    expect(container.textContent).toContain('+5%');
    expect(container.textContent).toContain('vs last week');
  });
});
