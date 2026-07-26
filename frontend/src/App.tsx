import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router-dom';
import { useAppStore } from './lib/store';
import { LayoutDashboard, CheckSquare, Settings as SettingsIcon, Moon, Sun } from 'lucide-react';

function Navigation() {
  const location = useLocation();
  const { darkMode, toggleDarkMode } = useAppStore();

  const navItems = [
    { path: '/', label: 'Dashboard', icon: LayoutDashboard },
    { path: '/tasks', label: 'Tasks', icon: CheckSquare },
    { path: '/settings', label: 'Settings', icon: SettingsIcon },
  ];

  return (
    <header className="border-b border-gray-800 bg-gray-950/80 backdrop-blur sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
        <div className="flex items-center space-x-8">
          <div className="flex items-center space-x-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-tr from-indigo-500 to-purple-500 flex items-center justify-center font-bold text-white shadow-lg shadow-indigo-500/20">
              CT
            </div>
            <span className="font-bold text-lg text-white tracking-wide">
              Control Tower <span className="text-xs px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-400 border border-indigo-500/30">v2</span>
            </span>
          </div>
          <nav className="flex space-x-1">
            {navItems.map((item) => {
              const Icon = item.icon;
              const isActive = location.pathname === item.path;
              return (
                <Link
                  key={item.path}
                  to={item.path}
                  className={`flex items-center space-x-2 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                    isActive
                      ? 'bg-gray-800 text-white shadow-sm'
                      : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800/50'
                  }`}
                >
                  <Icon className="w-4 h-4" />
                  <span>{item.label}</span>
                </Link>
              );
            })}
          </nav>
        </div>
        <div className="flex items-center space-x-4">
          <button
            onClick={toggleDarkMode}
            className="p-2 rounded-lg bg-gray-800 text-gray-400 hover:text-white transition-colors"
            title="Toggle theme"
          >
            {darkMode ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
          </button>
        </div>
      </div>
    </header>
  );
}

function DashboardPage() {
  const { user } = useAppStore();
  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">Welcome back, {user?.name || 'Operator'}</h1>
        <p className="text-gray-400 text-sm mt-1">Control Tower V2 task coordination dashboard.</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="p-5 rounded-xl border border-gray-800 bg-gray-900/60 shadow-sm">
          <div className="text-sm font-medium text-gray-400">Total Tasks</div>
          <div className="text-3xl font-extrabold text-white mt-2">12</div>
          <div className="text-xs text-emerald-400 mt-1">↑ 2 active gates</div>
        </div>
        <div className="p-5 rounded-xl border border-gray-800 bg-gray-900/60 shadow-sm">
          <div className="text-sm font-medium text-gray-400">In Review</div>
          <div className="text-3xl font-extrabold text-white mt-2">3</div>
          <div className="text-xs text-amber-400 mt-1">Pending Four-Eyes check</div>
        </div>
        <div className="p-5 rounded-xl border border-gray-800 bg-gray-900/60 shadow-sm">
          <div className="text-sm font-medium text-gray-400">Token Efficiency</div>
          <div className="text-3xl font-extrabold text-white mt-2">82.4%</div>
          <div className="text-xs text-indigo-400 mt-1">~80% reduction goal target</div>
        </div>
      </div>
    </div>
  );
}

function TasksPage() {
  return (
    <div className="p-6 space-y-4">
      <h1 className="text-2xl font-bold text-white">Tasks Overview</h1>
      <p className="text-gray-400 text-sm">Manage and track LangGraph task executions.</p>
      <div className="p-8 rounded-xl border border-dashed border-gray-800 bg-gray-900/30 text-center text-gray-400">
        No active tasks found in current session.
      </div>
    </div>
  );
}

function SettingsPage() {
  return (
    <div className="p-6 space-y-4">
      <h1 className="text-2xl font-bold text-white">System Settings</h1>
      <p className="text-gray-400 text-sm">Configure backend endpoints and LLM provider settings.</p>
      <div className="p-6 rounded-xl border border-gray-800 bg-gray-900/60 max-w-xl space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-300">Backend API URL</label>
          <input
            type="text"
            readOnly
            value="/api"
            className="mt-1 block w-full rounded-md border-gray-700 bg-gray-800 text-gray-200 text-sm px-3 py-2"
          />
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-gray-950 text-gray-100 flex flex-col font-sans">
        <Navigation />
        <main className="flex-1 max-w-7xl w-full mx-auto">
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
