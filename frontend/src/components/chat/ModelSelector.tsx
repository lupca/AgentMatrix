import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, Loader2 } from 'lucide-react';
import { api } from '../../lib/api';
import { Agent } from '../../types/agent';

export type CoordinatorProvider = 'anthropic' | 'google' | 'openai';

export interface CoordinatorModelOption {
  label: string;
  value: string;
  provider: CoordinatorProvider;
}

export function providerForModel(model: string): CoordinatorProvider {
  const normalized = model.toLowerCase();
  if (normalized.includes('gemini')) return 'google';
  if (
    normalized.includes('openai') ||
    normalized.startsWith('gpt-') ||
    normalized.startsWith('o1-') ||
    normalized.startsWith('chatgpt-') ||
    normalized.includes('codex')
  ) {
    return 'openai';
  }
  return 'anthropic';
}

function optionForAgent(agent: Agent): CoordinatorModelOption | null {
  if (!agent.model) return null;
  const provider = agent.provider || (agent.cli?.toLowerCase() === 'agy'
    ? 'google'
    : providerForModel(agent.model));
  return { label: agent.name, value: agent.model, provider };
}

interface ProviderBadgeProps {
  provider: CoordinatorProvider;
}

const ProviderBadge: React.FC<ProviderBadgeProps> = ({ provider }) => (
  <span
    aria-hidden="true"
    title={provider === 'anthropic' ? 'Anthropic' : provider === 'google' ? 'Google' : 'OpenAI'}
    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[10px] font-bold ${
      provider === 'anthropic'
        ? 'bg-purple-500/15 text-purple-300 ring-1 ring-purple-400/30'
        : provider === 'google'
          ? 'bg-blue-500/15 text-blue-300 ring-1 ring-blue-400/30'
          : 'bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-400/30'
    }`}
  >
    {provider === 'anthropic' ? 'A' : provider === 'google' ? 'G' : 'O'}
  </span>
);

export interface ModelSelectorProps {
  currentModel?: string | null;
  onModelChange: (model: string, provider?: CoordinatorProvider) => void | Promise<void>;
  onDefaultModelChange?: (model: string, provider?: CoordinatorProvider) => void;
  disabled?: boolean;
  isLoading?: boolean;
  className?: string;
}

export const ModelSelector: React.FC<ModelSelectorProps> = ({
  currentModel,
  onModelChange,
  onDefaultModelChange,
  disabled = false,
  isLoading = false,
  className = '',
}) => {
  const [models, setModels] = useState<CoordinatorModelOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const currentModelRef = useRef(currentModel);

  useEffect(() => {
    currentModelRef.current = currentModel;
  }, [currentModel]);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    setError(null);

    api.get<Agent[]>('/agents?role=coordinator')
      .then((agents) => {
        if (!mounted) return;
        const options = agents
          .map(optionForAgent)
          .filter((option): option is CoordinatorModelOption => option !== null);

        if (options.length === 0) {
          setError('No coordinator agents configured. Go to Agents page to add.');
          setModels([]);
        } else {
          const defaultAgent = agents.find((agent) => agent.is_default && agent.model);
          const orderedOptions = defaultAgent
            ? [optionForAgent(defaultAgent)!, ...options.filter((o) => o.value !== defaultAgent.model)]
            : options;
          setModels(orderedOptions);
          if (!currentModelRef.current && defaultAgent?.model) {
            onDefaultModelChange?.(
              defaultAgent.model,
              optionForAgent(defaultAgent)?.provider,
            );
          }
        }
      })
      .catch((err) => {
        if (!mounted) return;
        setError(`Failed to load coordinators: ${err.message || 'API error'}`);
        setModels([]);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => { mounted = false; };
  }, []);

  const defaultOption = models[0];
  const selectedModel = currentModel || defaultOption?.value || '';
  const selectedOption = models.find((option) => option.value === selectedModel);
  const provider = selectedOption?.provider || (selectedModel ? providerForModel(selectedModel) : 'anthropic');
  const isDisabled = disabled || isLoading || loading || models.length === 0;

  // Show error state
  if (error) {
    return (
      <div className={`flex items-center gap-2 ${className}`}>
        <span className="text-xs text-red-400">{error}</span>
        <a href="/agents" className="text-xs text-indigo-400 hover:underline">Configure</a>
      </div>
    );
  }

  // Show loading state
  if (loading) {
    return (
      <div className={`flex items-center gap-2 ${className}`}>
        <Loader2 className="h-4 w-4 animate-spin text-gray-400" />
        <span className="text-xs text-gray-500">Loading models...</span>
      </div>
    );
  }

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <label
        className="text-[10px] font-semibold uppercase tracking-wider text-gray-500"
        htmlFor="coordinator-model-selector"
      >
        Model
      </label>
      <div className="relative flex min-w-0 items-center">
        <ProviderBadge provider={provider} />
        <select
          id="coordinator-model-selector"
          data-testid="model-selector"
          value={selectedModel}
          onChange={(event) => {
            const model = event.target.value;
            const selected = models.find((option) => option.value === model);
            const selectedProvider = selected?.provider;
            onModelChange(
              model,
              selectedProvider && selectedProvider !== providerForModel(model)
                ? selectedProvider
                : undefined,
            );
          }}
          disabled={isDisabled}
          aria-label="Select coordinator model"
          aria-busy={isLoading}
          className="h-8 min-w-40 appearance-none rounded-lg border border-gray-700 bg-gray-950 py-1.5 pl-8 pr-8 text-xs font-medium text-gray-200 outline-none transition-colors hover:border-gray-600 focus:border-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {models.map((model) => (
            <option key={model.value} value={model.value}>
              {model.label}
            </option>
          ))}
        </select>
        {isLoading ? (
          <Loader2
            className="pointer-events-none absolute right-2 h-3.5 w-3.5 animate-spin text-indigo-400"
            aria-label="Switching model"
          />
        ) : (
          <ChevronDown className="pointer-events-none absolute right-2 h-3.5 w-3.5 text-gray-500" />
        )}
      </div>
    </div>
  );
};

export default ModelSelector;
