import React, { useEffect, useState } from 'react';
import { Bot, Loader2, Play } from 'lucide-react';
import { api } from '../../lib/api';
import { showError, showSuccess } from '../../lib/toast';
import { Agent } from '../../types/agent';

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
  const [selectedAgentId, setSelectedAgentId] = useState(defaultAgentId || '');
  const [loadingAgents, setLoadingAgents] = useState(true);
  const [isDispatching, setIsDispatching] = useState(false);

  useEffect(() => {
    let mounted = true;

    const loadAgents = async () => {
      setLoadingAgents(true);
      try {
        const availableAgents = await api.get<Agent[]>('/agents');
        if (!mounted) return;

        const nextAgents = availableAgents || [];
        setAgents(nextAgents);
        setSelectedAgentId((current) => {
          if (current && nextAgents.some((agent) => agent.id === current)) return current;
          if (defaultAgentId && nextAgents.some((agent) => agent.id === defaultAgentId)) {
            return defaultAgentId;
          }
          return nextAgents[0]?.id || '';
        });
      } catch (error) {
        console.error('Failed to load agents for dispatch:', error);
      } finally {
        if (mounted) setLoadingAgents(false);
      }
    };

    loadAgents();
    return () => {
      mounted = false;
    };
  }, [defaultAgentId]);

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
            agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.name || agent.id} ({agent.id})
              </option>
            ))
          )}
        </select>
      </div>
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
