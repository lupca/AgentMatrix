import React from 'react';
import { Wrench } from 'lucide-react';
import type { ToolInfo } from '../../hooks/useTools';

export function filterTools(tools: ToolInfo[], query: string): ToolInfo[] {
  const needle = query.toLowerCase();
  return tools.filter((tool) => {
    if (!tool.slash_alias) return false;
    const alias = tool.slash_alias.replace(/^\//, '').toLowerCase();
    return alias.startsWith(needle) || tool.name.toLowerCase().startsWith(needle);
  });
}

interface ToolPaletteProps {
  tools: ToolInfo[];
  query: string;
  activeIndex: number;
  onSelect: (tool: ToolInfo) => void;
  onHover: (index: number) => void;
}

export const ToolPalette: React.FC<ToolPaletteProps> = ({
  tools,
  query,
  activeIndex,
  onSelect,
  onHover,
}) => {
  const matches = filterTools(tools, query);
  if (matches.length === 0) return null;

  return (
    <div
      role="listbox"
      aria-label="Command palette"
      className="absolute bottom-full left-0 right-0 mb-2 max-h-64 overflow-y-auto rounded-xl border border-gray-800 bg-gray-950/95 shadow-2xl backdrop-blur-md z-20"
    >
      {matches.map((tool, index) => (
        <button
          key={tool.name}
          type="button"
          role="option"
          aria-selected={index === activeIndex}
          // onMouseDown (not onClick) fires before the textarea's blur, so the
          // selection still lands even though the input never loses focus.
          onMouseDown={(e) => {
            e.preventDefault();
            onSelect(tool);
          }}
          onMouseEnter={() => onHover(index)}
          className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs transition-colors ${
            index === activeIndex
              ? 'bg-indigo-500/20 text-indigo-200'
              : 'text-gray-300 hover:bg-gray-800/70'
          }`}
        >
          <div className="flex items-center gap-2 min-w-0">
            <Wrench className="w-3.5 h-3.5 shrink-0 text-amber-400" />
            <span className="font-mono font-semibold shrink-0">{tool.slash_alias}</span>
            <span className="truncate text-gray-400">{tool.description}</span>
          </div>
          <span className="shrink-0 text-[10px] uppercase tracking-wide text-gray-500">
            {tool.group}
          </span>
        </button>
      ))}
    </div>
  );
};

export default ToolPalette;
