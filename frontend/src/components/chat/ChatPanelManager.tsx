import React, { useState } from 'react';
import { ChatPanel } from './ChatPanel';
import { Task } from '../../types/task';
import { MessageSquare, Minimize2, Maximize2, X, PanelRightClose, PanelRightOpen } from 'lucide-react';
import { ContextLevel, useSessions } from '../../hooks/useSessions';

interface ChatPanelManagerProps {
  threadId: string;
  taskId?: string;
  taskTitle?: string;
  task?: Task;
  projectId?: string | null;
  projectName?: string | null;
  onTaskAction?: () => Promise<void> | void;
  defaultMode?: 'docked' | 'floating' | 'collapsed';
  className?: string;
}

export const ChatPanelManager: React.FC<ChatPanelManagerProps> = ({
  threadId,
  taskId,
  taskTitle,
  task,
  projectId,
  projectName,
  onTaskAction,
  defaultMode = 'docked',
  className = '',
}) => {
  const [mode, setMode] = useState<'docked' | 'floating' | 'collapsed'>(defaultMode);
  const [isExpandedFull, setIsExpandedFull] = useState<boolean>(false);

  const resolvedProjectId = projectId ?? task?.project ?? null;
  const contextLevel: ContextLevel = taskId ? 'task' : resolvedProjectId ? 'project' : 'global';

  // Fetches sessions for this context on mount and whenever the context changes.
  const {
    sessions,
    activeSessionId,
    activeSession,
    loading: sessionsLoading,
    createSession,
    switchSession,
    closeSession,
  } = useSessions({ level: contextLevel, project_id: resolvedProjectId, task_id: taskId });

  // ChatPanel resolves this value via GET /sessions/{id}, so use the session's
  // primary key here (not thread_id, which is a distinct LangGraph checkpoint field).
  const activeThreadId = activeSession?.id || threadId;

  const sessionProps = {
    contextLevel,
    projectName,
    sessions,
    activeSessionId,
    sessionsLoading,
    onSwitchSession: switchSession,
    onCreateSession: () => {
      createSession().catch((err) => console.error('Failed to create session:', err));
    },
    onCloseSession: closeSession,
  };

  if (mode === 'collapsed') {
    return (
      <div className="fixed bottom-6 right-6 z-50">
        <button
          onClick={() => setMode('floating')}
          className="flex items-center gap-2.5 px-4 py-3 rounded-full bg-gradient-to-r from-indigo-600 to-purple-600 text-white font-semibold text-xs shadow-2xl hover:scale-105 transition-all duration-200 border border-indigo-400/30 group"
        >
          <div className="relative">
            <MessageSquare className="w-5 h-5" />
            <span className="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse border-2 border-indigo-600" />
          </div>
          <span>AI Task Copilot</span>
        </button>
      </div>
    );
  }

  if (mode === 'floating') {
    return (
      <div className="fixed bottom-6 right-6 z-50 w-[90vw] sm:w-[420px] h-[580px] max-h-[85vh] min-h-0 shadow-2xl flex flex-col transition-all duration-300">
        <div className="bg-gray-950 px-4 py-2 border-t border-l border-r border-gray-800 rounded-t-2xl flex items-center justify-between text-xs text-gray-400">
          <div className="flex items-center gap-2 font-mono text-indigo-400 font-medium">
            <MessageSquare className="w-3.5 h-3.5" />
            <span>Floating Mode</span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setMode('docked')}
              className="p-1 hover:text-white rounded hover:bg-gray-800"
              title="Dock into page layout"
            >
              <PanelRightOpen className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={() => setMode('collapsed')}
              className="p-1 hover:text-white rounded hover:bg-gray-800"
              title="Minimize chat"
            >
              <Minimize2 className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={() => setMode('collapsed')}
              className="p-1 hover:text-red-400 rounded hover:bg-gray-800"
              title="Close chat"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        <ChatPanel
          key={activeThreadId}
          threadId={activeThreadId}
          taskId={taskId}
          taskTitle={taskTitle}
          task={task}
          onTaskAction={onTaskAction}
          className="rounded-t-none h-full min-h-0 border-t-0"
          {...sessionProps}
        />
      </div>
    );
  }

  // Docked Mode
  return (
    <div
      className={`flex flex-col h-full min-h-0 max-h-[calc(100vh-200px)] rounded-2xl overflow-hidden border border-gray-800/80 bg-gray-950/40 ${
        isExpandedFull ? 'fixed inset-4 z-50 bg-gray-950 shadow-2xl' : ''
      } ${className}`}
    >
      <div className="px-4 py-2 bg-gray-950/90 border-b border-gray-800/80 flex items-center justify-between text-xs text-gray-400 shrink-0 select-none">
        <div className="flex items-center gap-2">
          <span className="font-mono text-indigo-400 font-semibold text-[11px] uppercase tracking-wider">
            Copilot Sidecar
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            onClick={() => setIsExpandedFull(!isExpandedFull)}
            className="p-1 hover:text-white rounded hover:bg-gray-800 transition-colors"
            title={isExpandedFull ? 'Exit fullscreen' : 'Fullscreen chat'}
          >
            {isExpandedFull ? (
              <Minimize2 className="w-3.5 h-3.5" />
            ) : (
              <Maximize2 className="w-3.5 h-3.5" />
            )}
          </button>
          <button
            onClick={() => setMode('floating')}
            className="p-1 hover:text-white rounded hover:bg-gray-800 transition-colors"
            title="Pop out into floating window"
          >
            <PanelRightClose className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => setMode('collapsed')}
            className="p-1 hover:text-red-400 rounded hover:bg-gray-800 transition-colors"
            title="Collapse chat"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      <ChatPanel
        key={activeThreadId}
        threadId={activeThreadId}
        taskId={taskId}
        taskTitle={taskTitle}
        task={task}
        onTaskAction={onTaskAction}
        className="border-0 rounded-t-none flex-1 min-h-0"
        {...sessionProps}
      />
    </div>
  );
};

export default ChatPanelManager;
