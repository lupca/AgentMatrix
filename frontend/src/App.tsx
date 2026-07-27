import { useState } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { LayoutDashboard, FolderKanban, Bot, CheckSquare, LayoutGrid, Settings as SettingsIcon, Activity } from 'lucide-react';
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
import { ThemeProvider } from './contexts/ThemeContext';
import { GlobalChatButton } from './components/chat/GlobalChatButton';
import { ChatPanelManager } from './components/chat/ChatPanelManager';
import { GlobalChatProvider, useGlobalChat } from './contexts/GlobalChatContext';
import { Navigation } from './components/Navigation';
// Removed inline Navigation component

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

function GlobalChatLayer() {
  const { mode, setMode } = useGlobalChat();

  if (mode === 'closed') {
    return <GlobalChatButton onClick={() => setMode('floating')} />;
  }

  if (mode === 'floating' || mode === 'collapsed') {
    return <ChatPanelManager threadId="global" mode={mode} onModeChange={setMode} />;
  }
  
  return null;
}

function AppLayout() {
  const { mode, setMode } = useGlobalChat();

  return (
    <div className="h-screen bg-gray-950 text-gray-100 flex flex-row font-sans overflow-hidden">
      <Navigation />
      <div className="flex-1 flex overflow-hidden min-w-0">
        <main className="flex-1 max-w-7xl w-full mx-auto overflow-y-auto">
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
        
        {mode === 'docked' && (
          <aside className="w-auto border-l border-gray-800 bg-gray-900/50 flex-shrink-0 flex flex-col shadow-2xl z-40">
            <ChatPanelManager 
              threadId="global" 
              mode="docked"
              onModeChange={setMode}
              fullHeight={true}
              className="h-full border-0 rounded-none" 
            />
          </aside>
        )}
      </div>
      <GlobalChatLayer />
    </div>
  );
}

export default function App() {
  return (
    <GlobalChatProvider>
      <ThemeProvider>
        <BrowserRouter>
          <ErrorBoundary>
            <AppLayout />
          </ErrorBoundary>
          <Toaster position="top-right" toastOptions={{ className: 'theme-toast' }} />
        </BrowserRouter>
      </ThemeProvider>
    </GlobalChatProvider>
  );
}
