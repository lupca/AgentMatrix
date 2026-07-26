import React, { useEffect, useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { Agent, AgentProvider, AgentType } from '../../types/agent';

export interface AgentFormData {
  id: string;
  name: string;
  role: string;
  capabilities: string[];
  status: string;
  model?: string;
  effort?: string;
  cli?: string;
  agent_type: AgentType;
  provider?: AgentProvider;
  api_key?: string;
  base_url?: string;
  is_default: boolean;
}

interface AgentFormProps {
  agent?: Agent | null;
  submitting?: boolean;
  onSubmit: (data: AgentFormData) => void | Promise<void>;
  onCancel: () => void;
}

const roles = [
  ['executor', 'Executor (Task Execution)'],
  ['reviewer', 'Reviewer (Four-Eyes Gate Approval)'],
  ['coordinator', 'Coordinator (Chat Model)'],
  ['spec_author', 'Spec Author (Requirements & Spec)'],
  ['orchestrator', 'Orchestrator (State Router)'],
];

const inputClass =
  'w-full rounded-lg border border-gray-800 bg-gray-950 px-3 py-2 text-sm text-gray-200 outline-none focus:border-purple-500';

export const AgentForm: React.FC<AgentFormProps> = ({
  agent,
  submitting = false,
  onSubmit,
  onCancel,
}) => {
  const [id, setId] = useState('');
  const [name, setName] = useState('');
  const [role, setRole] = useState('executor');
  const [capabilities, setCapabilities] = useState('');
  const [status, setStatus] = useState('idle');
  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('');
  const [cli, setCli] = useState('claude');
  const [agentType, setAgentType] = useState<AgentType>('cli');
  const [provider, setProvider] = useState<AgentProvider>('anthropic');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [showApiKey, setShowApiKey] = useState(false);
  const [validationError, setValidationError] = useState('');
  const [isDefault, setIsDefault] = useState(false);

  useEffect(() => {
    setId(agent?.id || '');
    setName(agent?.name || '');
    setRole(agent?.role || 'executor');
    setCapabilities(Array.isArray(agent?.capabilities) ? agent.capabilities.join(', ') : '');
    setStatus(agent?.status || 'idle');
    setModel(agent?.model || '');
    setEffort(agent?.effort || '');
    setCli(agent?.cli || 'claude');
    setAgentType(agent?.agent_type || (agent?.provider ? 'api' : 'cli'));
    setProvider(agent?.provider || 'anthropic');
    setApiKey('');
    setBaseUrl(agent?.base_url || '');
    setShowApiKey(false);
    setValidationError('');
    setIsDefault(Boolean(agent?.is_default));
  }, [agent]);

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (agentType === 'cli' && !cli.trim()) {
      setValidationError('Select a CLI tool for CLI agents.');
      return;
    }
    if (agentType === 'api' && !provider) {
      setValidationError('Select a provider for API agents.');
      return;
    }
    if (agentType === 'api' && !apiKey.trim() && !agent?.has_api_key) {
      setValidationError('Enter an API key for API agents.');
      return;
    }
    setValidationError('');
    onSubmit({
      id: id.trim(),
      name: name.trim(),
      role,
      capabilities: capabilities.split(',').map((value) => value.trim()).filter(Boolean),
      status,
      model: model.trim() || undefined,
      effort: effort.trim() || undefined,
      cli: agentType === 'cli' ? cli.trim() : undefined,
      agent_type: agentType,
      provider: agentType === 'api' ? provider : undefined,
      api_key: agentType === 'api' ? apiKey.trim() || undefined : undefined,
      base_url: agentType === 'api' ? baseUrl.trim() : undefined,
      is_default: role === 'coordinator' && isDefault,
    });
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">
          Agent ID *
        </label>
        <input required disabled={Boolean(agent)} value={id} onChange={(event) => setId(event.target.value)} className={`${inputClass} font-mono disabled:opacity-50`} placeholder="e.g. CodeAgent-02" />
      </div>

      <div>
        <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">Display Name *</label>
        <input required value={name} onChange={(event) => setName(event.target.value)} className={inputClass} placeholder="e.g. Frontend Specialist Agent" />
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">Role *</label>
          <select value={role} onChange={(event) => setRole(event.target.value)} className={`${inputClass} capitalize`}>
            {roles.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">Status</label>
          <select value={status} onChange={(event) => setStatus(event.target.value)} className={`${inputClass} capitalize`}>
            <option value="idle">Idle / Ready</option>
            <option value="busy">Busy / Executing</option>
            <option value="offline">Offline</option>
          </select>
        </div>
      </div>

      <div>
        <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">Agent Type *</span>
        <div className="grid grid-cols-2 gap-2">
          {([
            ['cli', 'CLI Tool', 'Run through claude, agy, or codex.'],
            ['api', 'API Key', 'Call a provider directly.'],
          ] as const).map(([value, label, description]) => (
            <label
              key={value}
              className={`flex cursor-pointer items-start gap-2 rounded-lg border p-3 text-xs transition-colors ${
                agentType === value
                  ? 'border-purple-500/70 bg-purple-500/10 text-gray-100'
                  : 'border-gray-800 bg-gray-950/60 text-gray-400 hover:border-gray-700'
              }`}
            >
              <input
                type="radio"
                name="agent_type"
                value={value}
                checked={agentType === value}
                onChange={() => setAgentType(value)}
                className="mt-0.5 accent-purple-500"
              />
              <span>
                <strong className="block">{label}</strong>
                <span className="text-[11px] text-gray-500">{description}</span>
              </span>
            </label>
          ))}
        </div>
      </div>

      {agentType === 'cli' ? (
        <div>
          <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">CLI Tool *</label>
          <select value={cli} onChange={(event) => setCli(event.target.value)} className={inputClass} required>
            <option value="">Select a CLI tool</option>
            <option value="claude">Claude</option>
            <option value="agy">Agy</option>
            <option value="codex">Codex</option>
          </select>
        </div>
      ) : (
        <div className="space-y-3 rounded-lg border border-purple-500/20 bg-purple-500/5 p-3">
          <div>
            <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">Provider *</label>
            <select value={provider} onChange={(event) => setProvider(event.target.value as AgentProvider)} className={inputClass} required>
              <option value="anthropic">Anthropic</option>
              <option value="google">Google</option>
              <option value="openai">OpenAI</option>
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">API Key {agent?.has_api_key ? '(leave blank to keep current)' : '*'}</label>
            <div className="relative">
              <input
                type={showApiKey ? 'text' : 'password'}
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                className={`${inputClass} pr-10`}
                placeholder={agent?.has_api_key ? 'Stored key will be kept' : 'Enter provider API key'}
                autoComplete="new-password"
              />
              <button
                type="button"
                onClick={() => setShowApiKey((visible) => !visible)}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-gray-400 hover:text-gray-100"
                aria-label={showApiKey ? 'Hide API key' : 'Show API key'}
              >
                {showApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>
          <div>
            <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">API Base URL (optional)</label>
            <input
              type="url"
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              className={inputClass}
              placeholder="https://api.siliconflow.cn/v1"
              autoComplete="url"
            />
            <p className="mt-1 text-[11px] text-gray-500">Leave empty for OpenAI, or enter a URL for an OpenAI-compatible API.</p>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-3">
        <div>
          <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">Model</label>
          <input value={model} onChange={(event) => setModel(event.target.value)} className={inputClass} placeholder="e.g. claude-sonnet-4-20250514" />
        </div>
      </div>

      <div>
        <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-300">Capabilities (comma separated)</label>
        <input value={capabilities} onChange={(event) => setCapabilities(event.target.value)} className={inputClass} placeholder="e.g. typescript, unit-testing, refactoring" />
      </div>

      {role === 'coordinator' && (
        <label className="flex cursor-pointer items-center gap-3 rounded-lg border border-indigo-500/20 bg-indigo-500/10 p-3 text-xs text-gray-200">
          <input type="checkbox" checked={isDefault} onChange={(event) => setIsDefault(event.target.checked)} className="h-4 w-4 accent-indigo-500" />
          <span>
            <strong className="block">Default coordinator</strong>
            <span className="text-gray-400">Setting this unsets the current coordinator default.</span>
          </span>
        </label>
      )}

      {validationError && <p className="text-xs text-rose-400" role="alert">{validationError}</p>}

      <div className="flex items-center justify-end gap-3 border-t border-gray-800 pt-3">
        <button type="button" onClick={onCancel} className="rounded-xl px-4 py-2 text-xs font-medium text-gray-400 hover:bg-gray-800 hover:text-gray-100">Cancel</button>
        <button type="submit" disabled={submitting} className="rounded-xl bg-purple-600 px-5 py-2 text-xs font-medium text-white shadow-lg shadow-purple-600/20 hover:bg-purple-500 disabled:opacity-50">
          {submitting ? 'Saving...' : agent ? 'Save Changes' : 'Register Agent'}
        </button>
      </div>
    </form>
  );
};

export default AgentForm;
