import React from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
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
import { GlobalChatProvider } from './contexts/GlobalChatContext';
import { ResponsiveLayout } from './layouts/ResponsiveLayout';

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

export default function App() {
  return (
    <GlobalChatProvider>
      <ThemeProvider>
        <BrowserRouter>
          <ErrorBoundary>
            <ResponsiveLayout>
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/dashboard" element={<Dashboard />} />
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
            </ResponsiveLayout>
          </ErrorBoundary>
          <Toaster position="top-right" toastOptions={{ className: 'theme-toast' }} />
        </BrowserRouter>
      </ThemeProvider>
    </GlobalChatProvider>
  );
}
