import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router-dom';
import { useAppStore } from './lib/store';
import { LayoutDashboard, CheckSquare, LayoutGrid, Settings as SettingsIcon, Moon, Sun } from 'lucide-react';
import Dashboard from './pages/Dashboard';
import TasksPage from './pages/Tasks';
import KanbanPage from './pages/Kanban';

function Navigation() {
  const location = useLocation();
  const { darkMode, toggleDarkMode } = useAppStore();

  const navItems = [
    { path: '/', label: 'Dashboard', icon: LayoutDashboard },
    { path: '/tasks', label: 'Tasks', icon: CheckSquare },
    { path: '/kanban', label: 'Kanban', icon: LayoutGrid },
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
            <Route path="/" element={<Dashboard />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/kanban" element={<KanbanPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

