import React, { useEffect, useState } from 'react';
import { Bot, Loader2, Play } from 'lucide-react';
import { api } from '../../lib/api';
import { showError, showSuccess } from '../../lib/toast';
import { Agent, AgentSuggestion } from '../../types/agent';

export interface DispatchResponse {
  run_id: string;
  task_id: string;
  agent_id: string;
  command?: string;
  status: string;
}

interface DispatchButtonProps {
  taskId: string;
  defaultAgentId?: string | null;
  onDispatched?: (response: DispatchResponse) => void;
}

export const DispatchButton: React.FC<DispatchButtonProps> = ({
  taskId,
  defaultAgentId,
  onDispatched,
}) => {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [suggestions, setSuggestions] = useState<AgentSuggestion[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(defaultAgentId || '');
  const [loadingAgents, setLoadingAgents] = useState(true);
  const [isDispatching, setIsDispatching] = useState(false);

  useEffect(() => {
    let mounted = true;

    const loadAgents = async () => {
      setLoadingAgents(true);
      try {
        const rankedSuggestions = await api.get<AgentSuggestion[]>(
          `/tasks/${taskId}/suggested-agents`,
        );
        const availableAgents = await api.get<Agent[]>('/agents');
        if (!mounted) return;

        const nextAgents = availableAgents || [];
        const nextSuggestions = (rankedSuggestions || []).filter((suggestion) =>
          nextAgents.some((agent) => agent.id === suggestion.agent_id),
        );
        setAgents(nextAgents);
        setSuggestions(nextSuggestions);
        setSelectedAgentId(
          nextSuggestions[0]?.agent_id ||
            (defaultAgentId && nextAgents.some((agent) => agent.id === defaultAgentId)
              ? defaultAgentId
              : nextAgents[0]?.id || ''),
        );
      } catch (error) {
        // Keep dispatch usable when the suggestion endpoint is unavailable.
        try {
          const availableAgents = await api.get<Agent[]>('/agents');
          if (!mounted) return;
          setAgents(availableAgents || []);
          setSuggestions([]);
          setSelectedAgentId(
            defaultAgentId || availableAgents?.[0]?.id || '',
          );
        } catch (fallbackError) {
          console.error('Failed to load agents for dispatch:', fallbackError);
        }
      } finally {
        if (mounted) setLoadingAgents(false);
      }
    };

    loadAgents();
    return () => {
      mounted = false;
    };
  }, [defaultAgentId, taskId]);

  const handleDispatch = async () => {
    if (!selectedAgentId) {
      showError('Select an agent before dispatching this task.');
      return;
    }

    setIsDispatching(true);
    try {
      const response = await api.post<DispatchResponse>('/dispatch', {
        task_id: taskId,
        agent_id: selectedAgentId,
      });
      showSuccess(`Task ${taskId} dispatched (run: ${response.run_id})`);
      onDispatched?.(response);
    } catch (error) {
      console.error('Failed to dispatch task:', error);
    } finally {
      setIsDispatching(false);
    }
  };

  const disabled = loadingAgents || isDispatching || agents.length === 0;
  const suggestedAgentIds = new Set(suggestions.map((suggestion) => suggestion.agent_id));
  const remainingAgents = agents.filter((agent) => !suggestedAgentIds.has(agent.id));

  return (
    <div className="flex items-center gap-2">
      <label className="sr-only" htmlFor={`dispatch-agent-${taskId}`}>
        Select dispatch agent
      </label>
      <div className="relative">
        <Bot className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-purple-400" />
        <select
          id={`dispatch-agent-${taskId}`}
          value={selectedAgentId}
          onChange={(event) => setSelectedAgentId(event.target.value)}
          disabled={disabled}
          className="h-8 max-w-52 appearance-none rounded-lg border border-gray-700 bg-gray-950 py-1.5 pl-8 pr-7 text-xs font-medium text-gray-200 outline-none transition-colors focus:border-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
          aria-label="Select dispatch agent"
        >
          {agents.length === 0 ? (
            <option value="">No agents available</option>
          ) : (
            <>
              {suggestions.map((suggestion) => {
                const agent = agents.find((item) => item.id === suggestion.agent_id);
                return (
                  <option
                    key={suggestion.agent_id}
                    value={suggestion.agent_id}
                    title={suggestion.reason}
                  >
                    {agent?.name || suggestion.agent_id} · {Math.round(suggestion.score * 100)}% — {suggestion.reason}
                  </option>
                );
              })}
              {remainingAgents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name || agent.id} ({agent.id})
                </option>
              ))}
            </>
          )}
        </select>
      </div>
      {suggestions.length > 0 && (
        <span className="max-w-64 truncate text-[10px] text-gray-500" title={suggestions.find((item) => item.agent_id === selectedAgentId)?.reason}>
          {suggestions.find((item) => item.agent_id === selectedAgentId)?.reason}
        </span>
      )}
      <button
        type="button"
        onClick={handleDispatch}
        disabled={disabled}
        className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-indigo-600 px-3.5 text-xs font-semibold text-white shadow-lg shadow-indigo-600/20 transition-colors hover:bg-indigo-500 active:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {isDispatching || loadingAgents ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Play className="h-3.5 w-3.5" />
        )}
        <span>
          {loadingAgents ? 'Loading agents...' : isDispatching ? 'Dispatching...' : 'Dispatch Task'}
        </span>
      </button>
    </div>
  );
};

export default DispatchButton;
