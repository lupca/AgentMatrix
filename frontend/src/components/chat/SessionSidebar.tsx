import React, { useState, useRef, useEffect } from 'react';
import { Plus, X, MessageSquare, PanelRightClose, Pencil, Check } from 'lucide-react';
import { ChatSessionSummary } from '../../hooks/useSessions';

interface SessionSidebarProps {
  sessions: ChatSessionSummary[];
  activeSessionId: string | null;
  onSwitch: (sessionId: string) => void;
  onCreate: () => void;
  onClose?: (sessionId: string) => void;
  onRename?: (sessionId: string, title: string) => Promise<void>;
  onToggleSidebar: () => void;
  loading?: boolean;
}

function sessionLabel(session: ChatSessionSummary): string {
  return session.title?.trim() || `Session ${session.id.slice(0, 6)}`;
}

export const SessionSidebar: React.FC<SessionSidebarProps> = ({
  sessions,
  activeSessionId,
  onSwitch,
  onCreate,
  onClose,
  onRename,
  onToggleSidebar,
  loading = false,
}) => {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editingId && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editingId]);

  const startEdit = (session: ChatSessionSummary) => {
    setEditingId(session.id);
    setEditValue(session.title?.trim() || '');
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditValue('');
  };

  const saveEdit = async () => {
    if (!editingId || !onRename || isSaving) return;
    const trimmed = editValue.trim();
    if (!trimmed) {
      cancelEdit();
      return;
    }
    setIsSaving(true);
    try {
      await onRename(editingId, trimmed);
    } catch (err) {
      console.error('Failed to rename session:', err);
    } finally {
      setIsSaving(false);
      setEditingId(null);
      setEditValue('');
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      saveEdit();
    } else if (e.key === 'Escape') {
      cancelEdit();
    }
  };

  return (
    <div className="w-[300px] h-full flex flex-col bg-gray-950/80 border-l border-gray-800/80 shrink-0">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-800/80 flex items-center justify-between shrink-0">
        <h3 className="text-sm font-semibold text-gray-200 tracking-wide flex items-center gap-2">
          <MessageSquare className="w-4 h-4 text-indigo-400" />
          Chat History
        </h3>
        <button
          onClick={onToggleSidebar}
          className="p-1.5 rounded-lg text-gray-400 hover:text-gray-100 hover:bg-gray-800 transition-colors"
          title="Collapse history"
        >
          <PanelRightClose className="w-4 h-4" />
        </button>
      </div>

      {/* New Session Button */}
      <div className="p-3 shrink-0">
        <button
          type="button"
          onClick={onCreate}
          disabled={loading}
          className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-500 shadow-md hover:shadow-lg hover:shadow-indigo-500/20 transition-all disabled:opacity-50"
        >
          <Plus className="w-4 h-4" />
          New Chat
        </button>
      </div>

      {/* Session List */}
      <div className="flex-1 overflow-y-auto no-scrollbar p-2 space-y-1">
        {sessions.map((session) => {
          const isActive = session.id === activeSessionId;
          const isEditing = editingId === session.id;

          return (
            <div
              key={session.id}
              role="button"
              tabIndex={isEditing ? -1 : 0}
              onKeyDown={(e) => !isEditing && e.key === 'Enter' && onSwitch(session.id)}
              onClick={() => !isEditing && onSwitch(session.id)}
              className={`group flex items-center justify-between px-3 py-2.5 rounded-xl text-sm font-medium cursor-pointer transition-all ${
                isActive
                  ? 'bg-gray-800/80 text-gray-100 shadow-sm border border-gray-700/50'
                  : 'text-gray-400 border border-transparent hover:bg-gray-800/40 hover:text-gray-200'
              }`}
            >
              <div className="flex items-center gap-2.5 overflow-hidden flex-1 min-w-0">
                {isActive ? (
                  <div className="w-2 h-2 rounded-full bg-indigo-500 shrink-0 shadow-[0_0_8px_rgba(99,102,241,0.6)] animate-pulse" />
                ) : (
                  <MessageSquare className="w-3.5 h-3.5 shrink-0 opacity-50" />
                )}

                {isEditing ? (
                  <input
                    ref={inputRef}
                    type="text"
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    onKeyDown={handleKeyDown}
                    onBlur={saveEdit}
                    onClick={(e) => e.stopPropagation()}
                    disabled={isSaving}
                    maxLength={200}
                    className="flex-1 min-w-0 px-1.5 py-0.5 text-sm bg-gray-700 border border-gray-600 rounded text-gray-100 outline-none focus:border-indigo-500 disabled:opacity-50"
                    placeholder="Session name..."
                  />
                ) : (
                  <span
                    className="truncate"
                    onDoubleClick={(e) => {
                      if (onRename) {
                        e.stopPropagation();
                        startEdit(session);
                      }
                    }}
                  >
                    {sessionLabel(session)}
                  </span>
                )}
              </div>

              <div className="flex items-center gap-0.5 shrink-0">
                {isEditing ? (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      saveEdit();
                    }}
                    disabled={isSaving}
                    className="p-1.5 rounded-md text-indigo-400 hover:text-indigo-300 hover:bg-gray-700/80 transition-all"
                    title="Save"
                  >
                    <Check className="w-3.5 h-3.5" />
                  </button>
                ) : (
                  <>
                    {onRename && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          startEdit(session);
                        }}
                        className="p-1.5 rounded-md text-gray-500 opacity-0 group-hover:opacity-100 hover:text-indigo-400 hover:bg-gray-700/80 transition-all"
                        title="Rename session"
                      >
                        <Pencil className="w-3 h-3" />
                      </button>
                    )}
                    {onClose && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onClose(session.id);
                        }}
                        className="p-1.5 rounded-md text-gray-500 opacity-0 group-hover:opacity-100 hover:text-red-400 hover:bg-gray-700/80 transition-all"
                        title="Delete session"
                      >
                        <X className="w-3.5 h-3.5" />
                      </button>
                    )}
                  </>
                )}
              </div>
            </div>
          );
        })}

        {sessions.length === 0 && !loading && (
          <div className="px-4 py-8 text-center text-sm text-gray-500 italic">
            No chat history found.
          </div>
        )}
      </div>
    </div>
  );
};
