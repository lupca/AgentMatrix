import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';

export interface ToolInfo {
  name: string;
  description: string;
  slash_alias: string | null;
  tier: 'eager' | 'deferred';
  group: string;
}

interface ToolsResponse {
  tools: ToolInfo[];
}

// '/help' is a router-only command (backend/app/services/command_router.py),
// not a TOOL_REGISTRY entry, so GET /api/tools doesn't return it. Mirror the
// same HELP_COMMAND shape here so the palette still surfaces it.
const HELP_TOOL: ToolInfo = {
  name: 'help',
  description: 'List available commands and tools.',
  slash_alias: '/help',
  tier: 'eager',
  group: 'meta',
};

interface UseToolsResult {
  tools: ToolInfo[];
  loading: boolean;
  error: string | null;
}

/** Tool registry projection for the chat palette + `/help`, from `GET /api/tools`. */
export function useTools(): UseToolsResult {
  const query = useQuery({
    queryKey: ['tools'],
    queryFn: async () => {
      const res = await api.get<ToolsResponse>('/tools');
      return [...(res.tools ?? []), HELP_TOOL];
    },
    staleTime: Infinity,
  });

  return {
    tools: query.data ?? [],
    loading: query.isLoading,
    error: query.error ? (query.error as Error).message : null,
  };
}

export default useTools;
