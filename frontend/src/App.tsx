import { useState } from 'react';
import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router-dom';
import { LayoutDashboard, FolderKanban, Bot, CheckSquare, LayoutGrid, Settings as SettingsIcon, Moon, Sun, Activity } from 'lucide-react';
import Dashboard from './pages/Dashboard';
import ProjectsPage from './pages/Projects';
import ProjectDetailPage from './pages/ProjectDetail';
import AgentsPage from './pages/Agents';
import AgentDetailPage from './pages/AgentDetail';
import TasksPage from './pages/Tasks';
import TaskDetailPage from './pages/TaskDetail';
import KanbanPage from './pages/Kanban';
import TokensPage from './pages/Tokens';
import ErrorBoundary from './components/ErrorBoundary';
import { Toaster } from 'react-hot-toast';
import { ThemeProvider, useTheme } from './contexts/ThemeContext';
import { GlobalChatButton } from './components/chat/GlobalChatButton';
import { ChatPanelManager } from './components/chat/ChatPanelManager';

function Navigation() {
  const location = useLocation();
  const { theme, toggleTheme } = useTheme();

  const navItems = [
    { path: '/', label: 'Dashboard', icon: LayoutDashboard },
    { path: '/projects', label: 'Projects', icon: FolderKanban },
    { path: '/agents', label: 'Agents', icon: Bot },
    { path: '/tasks', label: 'Tasks', icon: CheckSquare },
    { path: '/kanban', label: 'Kanban', icon: LayoutGrid },
    { path: '/telemetry', label: 'Telemetry', icon: Activity },
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
            <span className="font-bold text-lg text-gray-100 tracking-wide">
              Control Tower <span className="text-xs px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-400 border border-indigo-500/30">v2</span>
            </span>
          </div>
          <nav className="flex space-x-1">
            {navItems.map((item) => {
              const Icon = item.icon;
              const isActive =
                location.pathname === item.path ||
                (item.path !== '/' && location.pathname.startsWith(item.path));
              return (
                <Link
                  key={item.path}
                  to={item.path}
                  className={`flex items-center space-x-2 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                    isActive
                      ? 'bg-gray-800 text-gray-100 shadow-sm'
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
            onClick={toggleTheme}
            className="p-2 rounded-lg bg-gray-800 text-gray-400 hover:text-gray-100 transition-colors"
            aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
          >
            {theme === 'dark' ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
          </button>
        </div>
      </div>
    </header>
  );
}

function SettingsPage() {
  return (
    <div className="p-6 space-y-4">
      <h1 className="text-2xl font-bold text-gray-100">System Settings</h1>
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

function GlobalChat() {
  const [isOpen, setIsOpen] = useState(false);

  if (!isOpen) {
    return <GlobalChatButton onClick={() => setIsOpen(true)} />;
  }

  return <ChatPanelManager threadId="global" defaultMode="floating" />;
}

export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <ErrorBoundary>
          <div className="min-h-screen bg-gray-950 text-gray-100 flex flex-col font-sans">
            <Navigation />
            <main className="flex-1 max-w-7xl w-full mx-auto">
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/projects" element={<ProjectsPage />} />
                <Route path="/projects/:id" element={<ProjectDetailPage />} />
                <Route path="/agents" element={<AgentsPage />} />
                <Route path="/agents/:id" element={<AgentDetailPage />} />
                <Route path="/tasks" element={<TasksPage />} />
                <Route path="/tasks/:id" element={<TaskDetailPage />} />
                <Route path="/kanban" element={<KanbanPage />} />
                <Route path="/telemetry" element={<TokensPage />} />
                <Route path="/settings" element={<SettingsPage />} />
              </Routes>
            </main>
            <GlobalChat />
          </div>
        </ErrorBoundary>
        <Toaster position="top-right" toastOptions={{ className: 'theme-toast' }} />
      </BrowserRouter>
    </ThemeProvider>
  );
}
