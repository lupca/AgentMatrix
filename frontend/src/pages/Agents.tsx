import React, { useEffect, useState, useCallback } from 'react';
import { api } from '../lib/api';
import { Agent, AgentStats as AgentStatsType } from '../types/agent';
import AgentCard from '../components/agents/AgentCard';
import AgentStats from '../components/agents/AgentStats';
import {
  Bot,
  Search,
  Plus,
  RefreshCw,
  Filter,
  ShieldCheck,
  AlertCircle,
  X,
  Cpu,
} from 'lucide-react';

export const AgentsPage: React.FC = () => {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [statsList, setStatsList] = useState<AgentStatsType[]>([]);
  const [statsMap, setStatsMap] = useState<Record<string, AgentStatsType>>({});
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [search, setSearch] = useState<string>('');
  const [roleFilter, setRoleFilter] = useState<string>('all');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);

  // New agent modal state
  const [showModal, setShowModal] = useState<boolean>(false);
  const [newAgentId, setNewAgentId] = useState<string>('');
  const [newAgentName, setNewAgentName] = useState<string>('');
  const [newAgentRole, setNewAgentRole] = useState<string>('executor');
  const [newCapabilities, setNewCapabilities] = useState<string>('code-generation, testing, review');
  const [creating, setCreating] = useState<boolean>(false);

  const fetchAgentsData = useCallback(async (isManual = false) => {
    if (isManual) setIsRefreshing(true);
    else setLoading(true);
    setError(null);

    try {
      // 1. Fetch agents
      const agentList = await api.get<Agent[]>('/agents');

      // 2. Fetch agent stats
      let stats: AgentStatsType[] = [];
      try {
        stats = await api.get<AgentStatsType[]>('/stats/agents');
      } catch (err) {
        console.warn('Failed to fetch /api/stats/agents', err);
      }

      const map: Record<string, AgentStatsType> = {};
      stats.forEach((s) => {
        map[s.agent_id] = s;
      });

      setAgents(agentList || []);
      setStatsList(stats);
      setStatsMap(map);
    } catch (err: any) {
      console.warn('Error fetching agents API:', err);
      setError(err?.message || 'Could not connect to /api/agents.');
    } finally {
      setLoading(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchAgentsData();
  }, [fetchAgentsData]);

  const handleCreateAgent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newAgentId.trim() || !newAgentName.trim()) return;

    setCreating(true);
    const capsArray = newCapabilities
      .split(',')
      .map((c) => c.trim())
      .filter(Boolean);

    try {
      const created = await api.post<Agent>('/agents', {
        id: newAgentId.trim(),
        name: newAgentName.trim(),
        role: newAgentRole.trim(),
        capabilities: capsArray,
        status: 'idle',
      });
      setAgents((prev) => [created, ...prev]);
      setShowModal(false);
      setNewAgentId('');
      setNewAgentName('');
    } catch (err: any) {
      alert(`Failed to create agent: ${err?.message || 'Unknown error'}`);
    } finally {
      setCreating(false);
    }
  };


  // Unique roles from agents
  const availableRoles = Array.from(new Set(agents.map((a) => a.role || 'executor')));

  // Filtered agents
  const filteredAgents = agents.filter((a) => {
    const matchesSearch =
      a.name.toLowerCase().includes(search.toLowerCase()) ||
      a.id.toLowerCase().includes(search.toLowerCase()) ||
      a.role.toLowerCase().includes(search.toLowerCase()) ||
      (Array.isArray(a.capabilities) &&
        a.capabilities.some((c) => String(c).toLowerCase().includes(search.toLowerCase())));

    const matchesRole =
      roleFilter === 'all' || a.role.toLowerCase() === roleFilter.toLowerCase();

    const matchesStatus =
      statusFilter === 'all' || a.status.toLowerCase() === statusFilter.toLowerCase();

    return matchesSearch && matchesRole && matchesStatus;
  });

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto min-h-[calc(100vh-4rem)]">
      {/* Header Banner */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 bg-gray-900/40 p-6 rounded-2xl border border-gray-800/80 shadow-lg backdrop-blur-md">
        <div>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-purple-500 to-indigo-600 flex items-center justify-center text-white shadow-lg shadow-purple-500/20">
              <Bot className="w-5 h-5" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-gray-100 tracking-tight flex items-center gap-2">
                Agent Roster & Performance
              </h1>
              <p className="text-gray-400 text-xs mt-0.5">
                Manage autonomous AI agents, role assignments, capabilities & four-eyes audit trails.
              </p>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={() => fetchAgentsData(true)}
            disabled={isRefreshing || loading}
            className="flex items-center gap-2 px-3 py-2 rounded-xl bg-gray-800 hover:bg-gray-700 text-gray-200 font-medium text-xs border border-gray-700 transition-all disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isRefreshing ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>

          <button
            onClick={() => setShowModal(true)}
            className="flex items-center gap-2 px-4 py-2 rounded-xl bg-purple-600 hover:bg-purple-500 text-white font-medium text-xs shadow-lg shadow-purple-600/20 transition-all"
          >
            <Plus className="w-4 h-4" />
            <span>Register Agent</span>
          </button>
        </div>
      </div>

      {/* Error Alert */}
      {error && (
        <div className="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <AlertCircle className="w-4 h-4 text-amber-400 flex-shrink-0" />
            <span>{error}</span>
          </div>
          <button
            onClick={() => fetchAgentsData(true)}
            className="underline hover:text-amber-200 font-medium flex-shrink-0"
          >
            Retry
          </button>
        </div>
      )}

      {/* Agent Roster Stats Component */}
      <AgentStats agents={agents} stats={statsList} loading={loading} />

      {/* Filters & Search Controls */}
      <div className="p-4 rounded-xl border border-gray-800/80 bg-gray-900/60 backdrop-blur-sm space-y-3">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          {/* Search */}
          <div className="relative flex-1 max-w-md">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by agent ID, name, role, capability..."
              className="w-full bg-gray-950/80 border border-gray-800 rounded-lg pl-9 pr-3 py-2 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500 transition-colors"
            />
          </div>

          {/* Status filter dropdown */}
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-400 font-medium">Status:</span>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="bg-gray-950 border border-gray-800 text-xs text-gray-200 rounded-lg px-3 py-1.5 focus:outline-none focus:border-purple-500 capitalize"
            >
              <option value="all">All Statuses</option>
              <option value="idle">Idle / Ready</option>
              <option value="busy">Busy / Executing</option>
              <option value="offline">Offline</option>
            </select>
          </div>
        </div>

        {/* Role Pills */}
        <div className="flex items-center gap-1.5 overflow-x-auto pt-1 border-t border-gray-800/60">
          <span className="text-xs text-gray-400 font-medium mr-1 flex items-center gap-1">
            <Filter className="w-3 h-3 text-purple-400" /> Role Filter:
          </span>
          <button
            onClick={() => setRoleFilter('all')}
            className={`px-3 py-1 rounded-lg text-xs font-medium capitalize transition-colors ${
              roleFilter === 'all'
                ? 'bg-purple-600 text-white shadow-md'
                : 'bg-gray-950/60 text-gray-400 hover:text-gray-200 border border-gray-800/60'
            }`}
          >
            All Roles ({agents.length})
          </button>
          {availableRoles.map((role) => (
            <button
              key={role}
              onClick={() => setRoleFilter(role)}
              className={`px-3 py-1 rounded-lg text-xs font-medium capitalize transition-colors ${
                roleFilter === role
                  ? 'bg-purple-600 text-white shadow-md'
                  : 'bg-gray-950/60 text-gray-400 hover:text-gray-200 border border-gray-800/60'
              }`}
            >
              {role}
            </button>
          ))}
        </div>
      </div>

      {/* Agents Roster Grid */}
      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 animate-pulse">
          {[1, 2, 3, 4, 5, 6].map((i) => (
            <div key={i} className="h-64 rounded-xl bg-gray-900/60 border border-gray-800" />
          ))}
        </div>
      ) : filteredAgents.length === 0 ? (
        <div className="p-12 rounded-xl border border-gray-800/80 bg-gray-900/40 text-center space-y-3">
          <Bot className="w-10 h-10 mx-auto text-gray-600" />
          <h3 className="text-base font-semibold text-gray-300">No Agents Found</h3>
          <p className="text-xs text-gray-500 max-w-sm mx-auto">
            No agents matched your search query or filters. Try adjusting search options or register a new agent.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {filteredAgents.map((agent) => (
            <AgentCard
              key={agent.id}
              agent={agent}
              stats={statsMap[agent.id]}
            />
          ))}
        </div>
      )}

      {/* Modal to Register Agent */}
      {showModal && (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-md shadow-2xl space-y-4 relative">
            <div className="flex items-center justify-between pb-3 border-b border-gray-800">
                <h3 className="text-lg font-bold text-gray-100 flex items-center gap-2">
                <Bot className="w-5 h-5 text-purple-400" />
                Register New AI Agent
              </h3>
              <button
                onClick={() => setShowModal(false)}
                className="p-1 rounded-lg text-gray-400 hover:text-gray-100 hover:bg-gray-800"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <form onSubmit={handleCreateAgent} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-gray-300 uppercase tracking-wider mb-1">
                  Agent ID *
                </label>
                <input
                  type="text"
                  required
                  placeholder="e.g. CodeAgent-02, SecurityReviewer-01"
                  value={newAgentId}
                  onChange={(e) => setNewAgentId(e.target.value)}
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-purple-500 font-mono"
                />
              </div>

              <div>
                <label className="block text-xs font-semibold text-gray-300 uppercase tracking-wider mb-1">
                  Agent Display Name *
                </label>
                <input
                  type="text"
                  required
                  placeholder="e.g. Frontend Specialist Agent"
                  value={newAgentName}
                  onChange={(e) => setNewAgentName(e.target.value)}
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-purple-500"
                />
              </div>

              <div>
                <label className="block text-xs font-semibold text-gray-300 uppercase tracking-wider mb-1">
                  Agent Role *
                </label>
                <select
                  value={newAgentRole}
                  onChange={(e) => setNewAgentRole(e.target.value)}
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-purple-500 capitalize"
                >
                  <option value="executor">Executor (Task Execution)</option>
                  <option value="reviewer">Reviewer (Four-Eyes Gate Approval)</option>
                  <option value="spec_author">Spec Author (Requirements & Spec)</option>
                  <option value="orchestrator">Orchestrator (State Router)</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-semibold text-gray-300 uppercase tracking-wider mb-1">
                  Capabilities (Comma separated)
                </label>
                <input
                  type="text"
                  placeholder="e.g. typescript, unit-testing, react, refactoring"
                  value={newCapabilities}
                  onChange={(e) => setNewCapabilities(e.target.value)}
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-purple-500 text-xs"
                />
              </div>

              <div className="pt-3 border-t border-gray-800 flex items-center justify-end gap-3">
                <button
                  type="button"
                  onClick={() => setShowModal(false)}
                  className="px-4 py-2 rounded-xl text-xs font-medium text-gray-400 hover:text-gray-100 hover:bg-gray-800"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={creating}
                  className="px-5 py-2 rounded-xl text-xs font-medium bg-purple-600 hover:bg-purple-500 text-white shadow-lg shadow-purple-600/20 disabled:opacity-50"
                >
                  {creating ? 'Registering...' : 'Register Agent'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};

export default AgentsPage;
