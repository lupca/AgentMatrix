import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, Loader2 } from 'lucide-react';
import { api } from '../../lib/api';
import { Agent } from '../../types/agent';

export type CoordinatorProvider = 'anthropic' | 'google';

export interface CoordinatorModelOption {
  label: string;
  value: string;
  provider: CoordinatorProvider;
}

export const MODELS: CoordinatorModelOption[] = [
  {
    label: 'Claude Sonnet',
    value: 'claude-sonnet-4-20250514',
    provider: 'anthropic',
  },
  {
    label: 'Claude Opus',
    value: 'claude-opus-4-5-20251101',
    provider: 'anthropic',
  },
  {
    label: 'Gemini Pro',
    value: 'gemini-2.5-pro',
    provider: 'google',
  },
  {
    label: 'Gemini Flash',
    value: 'gemini-2.5-flash',
    provider: 'google',
  },
];

export const MODEL_OPTIONS = MODELS;

export const DEFAULT_COORDINATOR_MODEL = MODELS[0].value;

export function providerForModel(model: string): CoordinatorProvider {
  return MODELS.find((option) => option.value === model)?.provider ||
    (model.toLowerCase().includes('gemini') ? 'google' : 'anthropic');
}

function optionForAgent(agent: Agent): CoordinatorModelOption | null {
  if (!agent.model) return null;
  const provider = agent.cli?.toLowerCase() === 'agy'
    ? 'google'
    : providerForModel(agent.model);
  return { label: agent.name, value: agent.model, provider };
}

interface ProviderBadgeProps {
  provider: CoordinatorProvider;
}

const ProviderBadge: React.FC<ProviderBadgeProps> = ({ provider }) => (
  <span
    aria-hidden="true"
    title={provider === 'anthropic' ? 'Anthropic' : 'Google'}
    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[10px] font-bold ${
      provider === 'anthropic'
        ? 'bg-purple-500/15 text-purple-300 ring-1 ring-purple-400/30'
        : 'bg-blue-500/15 text-blue-300 ring-1 ring-blue-400/30'
    }`}
  >
    {provider === 'anthropic' ? 'A' : 'G'}
  </span>
);

export interface ModelSelectorProps {
  currentModel?: string | null;
  onModelChange: (model: string) => void | Promise<void>;
  onDefaultModelChange?: (model: string) => void;
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
  const [models, setModels] = useState<CoordinatorModelOption[]>(MODELS);
  const currentModelRef = useRef(currentModel);

  useEffect(() => {
    currentModelRef.current = currentModel;
  }, [currentModel]);

  useEffect(() => {
    let mounted = true;
    api.get<Agent[]>('/agents?role=coordinator&status=idle')
      .then((agents) => {
        if (!mounted) return;
        const options = agents
          .map(optionForAgent)
          .filter((option): option is CoordinatorModelOption => option !== null);
        if (options.length > 0) {
          const defaultOption = agents.find((agent) => agent.is_default && agent.model);
          const orderedOptions = defaultOption
            ? [optionForAgent(defaultOption)!, ...options.filter((option) => option.value !== defaultOption.model)]
            : options;
          setModels(orderedOptions);
          if (!currentModelRef.current && defaultOption?.model) {
            onDefaultModelChange?.(defaultOption.model);
          }
        }
      })
      .catch(() => {
        // Keep the built-in list available when the roster API is unavailable.
      });
    return () => { mounted = false; };
  }, []);

  const defaultOption = models[0];
  const selectedModel = currentModel || defaultOption?.value || DEFAULT_COORDINATOR_MODEL;
  const selectedOption = models.find((option) => option.value === selectedModel);
  const provider = selectedOption?.provider || providerForModel(selectedModel);
  const isDisabled = disabled || isLoading;

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
          onChange={(event) => onModelChange(event.target.value)}
          disabled={isDisabled}
          aria-label="Select coordinator model"
          aria-busy={isLoading}
          className="h-8 min-w-40 appearance-none rounded-lg border border-gray-700 bg-gray-950 py-1.5 pl-8 pr-8 text-xs font-medium text-gray-200 outline-none transition-colors hover:border-gray-600 focus:border-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {!selectedOption && currentModel && (
            <option value={currentModel}>{currentModel}</option>
          )}
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
