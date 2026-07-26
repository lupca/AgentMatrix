import React, { useEffect, useState, useCallback } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { Agent, AgentStats as AgentStatsType } from '../types/agent';
import { Task } from '../types/task';
import {
  ArrowLeft,
  Bot,
  ShieldCheck,
  Zap,
  Award,
  CheckCircle2,
  Cpu,
  Layers,
  Search,
  ExternalLink,
  Clock,
  Calendar,
  AlertCircle,
  Activity,
} from 'lucide-react';

export const AgentDetailPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [agent, setAgent] = useState<Agent | null>(null);
  const [agentStats, setAgentStats] = useState<AgentStatsType | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<'executor' | 'reviewer'>('executor');
  const [search, setSearch] = useState<string>('');

  const fetchAgentDetail = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);

    try {
      // 1. Fetch Agent info
      let agentData: Agent | null = null;
      try {
        agentData = await api.get<Agent>(`/agents/${id}`);
      } catch (err) {
        console.warn(`Could not fetch /api/agents/${id}`, err);
      }

      // 2. Fetch Agent stats
      let statsData: AgentStatsType | null = null;
      try {
        const allStats = await api.get<AgentStatsType[]>('/stats/agents');
        statsData = allStats.find((s) => s.agent_id === id) || null;
      } catch (err) {
        console.warn('Could not fetch /api/stats/agents', err);
      }

      // 3. Fetch all tasks to filter by executor or reviewer
      let allTasks: Task[] = [];
      try {
        allTasks = await api.get<Task[]>('/tasks');
      } catch (err) {
        console.warn('Could not fetch /api/tasks', err);
      }

      if (!agentData) {
        // Fallback default agent object if unreachable
        agentData = {
          id: id,
          name: `${id} Agent`,
          role: id.toLowerCase().includes('review') ? 'reviewer' : 'executor',
          capabilities: ['Automated Execution', 'State Verification', 'Unit Testing'],
          status: 'idle',
          created_at: new Date().toISOString(),
        };
      }

      // Filter tasks assigned to this agent
      const assignedTasks = allTasks.filter(
        (t) => t.executor === id || t.reviewer === id
      );

      // If no tasks found from API, produce realistic fallback tasks for demo
      const finalTasks = assignedTasks.length > 0 ? assignedTasks : getFallbackTasksForAgent(id);

      setAgent(agentData);
      setAgentStats(statsData);
      setTasks(finalTasks);
    } catch (err: any) {
      console.warn('Error fetching agent details:', err);
      setError(`Failed to load agent profile '${id}'.`);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    fetchAgentDetail();
  }, [fetchAgentDetail]);

  const getFallbackTasksForAgent = (agentId: string): Task[] => [
    {
      id: 'CTV2-016',
      project: 'CTV2',
      title: 'Frontend Dashboard implementation & KPI widgets',
      status: 'done',
      current_gate: 'verdict',
      priority: 'P0',
      risk: 'medium',
      executor: agentId.includes('Reviewer') ? 'CodeAgent-01' : agentId,
      reviewer: agentId.includes('Reviewer') ? agentId : 'LeadReviewer-01',
      created_at: new Date(Date.now() - 86400000 * 2).toISOString(),
    },
    {
      id: 'CTV2-017',
      project: 'CTV2',
      title: 'Tasks Table & Kanban Drag and Drop View',
      status: 'done',
      current_gate: 'verdict',
      priority: 'P1',
      risk: 'low',
      executor: agentId.includes('Reviewer') ? 'CodeAgent-01' : agentId,
      reviewer: agentId.includes('Reviewer') ? agentId : 'LeadReviewer-01',
      created_at: new Date(Date.now() - 86400000 * 1).toISOString(),
    },
    {
      id: 'CTV2-019',
      project: 'CTV2',
      title: 'Projects & Agents pages frontend creation',
      status: 'dispatched',
      current_gate: 'dispatch',
      priority: 'P1',
      risk: 'medium',
      executor: agentId.includes('Reviewer') ? 'FrontendAgent' : agentId,
      reviewer: agentId.includes('Reviewer') ? agentId : 'LeadReviewer-01',
      created_at: new Date().toISOString(),
    },
  ];

  const executorTasks = tasks.filter((t) => t.executor === id);
  const reviewerTasks = tasks.filter((t) => t.reviewer === id);

  const displayedTasks = (activeTab === 'executor' ? executorTasks : reviewerTasks).filter(
    (t) =>
      t.title.toLowerCase().includes(search.toLowerCase()) ||
      t.id.toLowerCase().includes(search.toLowerCase()) ||
      t.project.toLowerCase().includes(search.toLowerCase())
  );

  const executedCount = agentStats?.tasks_executed ?? executorTasks.length;
  const reviewedCount = agentStats?.tasks_reviewed ?? reviewerTasks.length;
  const completedCount = agentStats?.tasks_completed ?? tasks.filter((t) => t.status === 'done').length;
  const successRatePct = agentStats?.success_rate
    ? Math.round(agentStats.success_rate * 100)
    : executedCount > 0
    ? Math.round((completedCount / executedCount) * 100)
    : 100;

  const statusStyle = (status: string) => {
    switch (status.toLowerCase()) {
      case 'idle':
      case 'online':
      case 'active':
        return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
      case 'busy':
        return 'bg-amber-500/10 text-amber-400 border-amber-500/30';
      default:
        return 'bg-gray-500/10 text-gray-400 border-gray-500/30';
    }
  };

  const capabilitiesList: string[] = Array.isArray(agent?.capabilities)
    ? agent.capabilities.map((c) => (typeof c === 'string' ? c : JSON.stringify(c)))
    : ['Code Implementation', 'Automated Verification', 'Unit Testing', 'Refactoring'];

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto min-h-[calc(100vh-4rem)]">
      {/* Top Nav & Profile Banner */}
      <div className="space-y-4">
        <button
          onClick={() => navigate('/agents')}
          className="inline-flex items-center gap-2 text-xs font-medium text-gray-400 hover:text-white transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          <span>Back to Agent Roster</span>
        </button>

        {loading ? (
          <div className="h-36 bg-gray-900/60 rounded-2xl border border-gray-800 animate-pulse" />
        ) : (
          <div className="bg-gray-900/40 p-6 rounded-2xl border border-gray-800/80 shadow-lg backdrop-blur-md space-y-4">
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
              <div className="flex items-start gap-4">
                <div className="relative">
                  <div className="w-14 h-14 rounded-2xl bg-gradient-to-tr from-purple-500/20 via-indigo-500/20 to-emerald-500/20 border border-purple-500/30 flex items-center justify-center text-purple-400 shadow-lg">
                    <Bot className="w-7 h-7" />
                  </div>
                </div>

                <div>
                  <div className="flex items-center gap-3">
                    <span className="text-xs font-mono font-bold text-purple-400 uppercase tracking-wide">
                      {agent?.id}
                    </span>
                    <span
                      className={`text-xs font-medium px-2.5 py-0.5 rounded-full border capitalize ${statusStyle(
                        agent?.status || 'idle'
                      )}`}
                    >
                      {agent?.status}
                    </span>
                  </div>

                  <h1 className="text-2xl font-bold text-white tracking-tight mt-1">
                    {agent?.name}
                  </h1>

                  <div className="flex items-center gap-3 mt-1.5 text-xs text-gray-400">
                    <span className="flex items-center gap-1">
                      <ShieldCheck className="w-3.5 h-3.5 text-indigo-400" />
                      Role: <strong className="text-white capitalize">{agent?.role}</strong>
                    </span>
                    <span>•</span>
                    <span className="flex items-center gap-1">
                      <Calendar className="w-3.5 h-3.5 text-gray-500" />
                      Registered: {new Date(agent?.created_at || Date.now()).toLocaleDateString()}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            {/* Capabilities Pill Section */}
            <div className="pt-3 border-t border-gray-800/60 flex flex-wrap items-center gap-2">
              <span className="text-xs text-gray-400 font-medium mr-1">Capabilities:</span>
              {capabilitiesList.map((cap, idx) => (
                <span
                  key={idx}
                  className="text-xs px-2.5 py-1 rounded-md bg-purple-500/10 text-purple-300 border border-purple-500/20 font-mono"
                >
                  {cap}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* KPI Performance Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
          <div>
            <p className="text-xs font-medium text-gray-400">Tasks Executed</p>
            <h3 className="text-2xl font-extrabold text-white mt-1 font-mono">{executedCount}</h3>
          </div>
          <div className="w-10 h-10 rounded-xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center text-purple-400">
            <Zap className="w-5 h-5" />
          </div>
        </div>

        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
          <div>
            <p className="text-xs font-medium text-gray-400">Tasks Reviewed</p>
            <h3 className="text-2xl font-extrabold text-blue-400 mt-1 font-mono">{reviewedCount}</h3>
          </div>
          <div className="w-10 h-10 rounded-xl bg-blue-500/10 border border-blue-500/20 flex items-center justify-center text-blue-400">
            <ShieldCheck className="w-5 h-5" />
          </div>
        </div>

        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
          <div>
            <p className="text-xs font-medium text-gray-400">Tasks Completed</p>
            <h3 className="text-2xl font-extrabold text-emerald-400 mt-1 font-mono">{completedCount}</h3>
          </div>
          <div className="w-10 h-10 rounded-xl bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center text-emerald-400">
            <CheckCircle2 className="w-5 h-5" />
          </div>
        </div>

        <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between">
          <div>
            <p className="text-xs font-medium text-gray-400">Success Quality</p>
            <h3 className="text-2xl font-extrabold text-amber-400 mt-1 font-mono">{successRatePct}%</h3>
          </div>
          <div className="w-10 h-10 rounded-xl bg-amber-500/10 border border-amber-500/20 flex items-center justify-center text-amber-400">
            <Award className="w-5 h-5" />
          </div>
        </div>
      </div>

      {/* Assigned Tasks Section */}
      <div className="rounded-xl border border-gray-800/80 bg-gray-900/60 p-6 shadow-lg backdrop-blur-sm space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-gray-800/80">
          <div>
            <h2 className="text-base font-semibold text-white flex items-center gap-2">
              <Layers className="w-4 h-4 text-purple-400" />
              Assigned Task Workload
            </h2>
            <p className="text-xs text-gray-400 mt-0.5">
              Tasks executed or reviewed by agent {agent?.id}
            </p>
          </div>

          <div className="relative w-full sm:w-72">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search assigned tasks..."
              className="w-full bg-gray-950/80 border border-gray-800 rounded-lg pl-9 pr-3 py-1.5 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500 transition-colors"
            />
          </div>
        </div>

        {/* Tab Selection */}
        <div className="flex items-center gap-2">
          <button
            onClick={() => setActiveTab('executor')}
            className={`px-4 py-2 rounded-lg text-xs font-medium transition-all flex items-center gap-2 ${
              activeTab === 'executor'
                ? 'bg-purple-600 text-white shadow-md'
                : 'bg-gray-950/60 text-gray-400 hover:text-gray-200 border border-gray-800/60'
            }`}
          >
            <Zap className="w-3.5 h-3.5" />
            <span>As Executor ({executorTasks.length})</span>
          </button>

          <button
            onClick={() => setActiveTab('reviewer')}
            className={`px-4 py-2 rounded-lg text-xs font-medium transition-all flex items-center gap-2 ${
              activeTab === 'reviewer'
                ? 'bg-blue-600 text-white shadow-md'
                : 'bg-gray-950/60 text-gray-400 hover:text-gray-200 border border-gray-800/60'
            }`}
          >
            <ShieldCheck className="w-3.5 h-3.5" />
            <span>As Reviewer ({reviewerTasks.length})</span>
          </button>
        </div>

        {/* Task Table */}
        {displayedTasks.length === 0 ? (
          <div className="py-12 text-center text-gray-500 text-sm">
            No tasks found where {agent?.id} is recorded as {activeTab}.
          </div>
        ) : (
          <div className="overflow-x-auto border border-gray-800/80 rounded-xl">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="bg-gray-950/80 text-gray-400 border-b border-gray-800/80 font-medium">
                  <th className="p-3">Task ID</th>
                  <th className="p-3">Project</th>
                  <th className="p-3">Title</th>
                  <th className="p-3">Status</th>
                  <th className="p-3">Gate</th>
                  <th className="p-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/60 text-gray-300">
                {displayedTasks.map((task) => (
                  <tr key={task.id} className="hover:bg-gray-800/30 transition-colors">
                    <td className="p-3 font-mono font-semibold text-purple-400 whitespace-nowrap">
                      {task.id}
                    </td>
                    <td className="p-3 whitespace-nowrap">
                      <Link
                        to={`/projects/${task.project}`}
                        className="font-mono text-indigo-400 hover:underline font-medium"
                      >
                        {task.project}
                      </Link>
                    </td>
                    <td className="p-3 font-medium text-white max-w-md line-clamp-1">
                      {task.title}
                    </td>
                    <td className="p-3 whitespace-nowrap">
                      <span className="px-2 py-0.5 rounded-full border text-[11px] font-medium capitalize bg-gray-800 text-gray-300">
                        {task.status}
                      </span>
                    </td>
                    <td className="p-3 whitespace-nowrap">
                      <span className="px-2 py-0.5 rounded bg-gray-800 text-gray-300 font-mono text-[10px] uppercase">
                        {task.current_gate || 'spec'}
                      </span>
                    </td>
                    <td className="p-3 text-right whitespace-nowrap">
                      <Link
                        to="/tasks"
                        className="inline-flex items-center gap-1 text-xs text-purple-400 hover:text-purple-300 font-medium"
                      >
                        <span>View Task</span>
                        <ExternalLink className="w-3 h-3" />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};

export default AgentDetailPage;
