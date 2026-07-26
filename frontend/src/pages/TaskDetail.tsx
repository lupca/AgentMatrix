import React, { useEffect, useState, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { Task } from '../types/task';
import { TaskHeader } from '../components/task/TaskHeader';
import { TaskSpec } from '../components/task/TaskSpec';
import { TaskMeta } from '../components/task/TaskMeta';
import { ChatPanelManager } from '../components/chat/ChatPanelManager';
import { AlertCircle, RefreshCw } from 'lucide-react';

export const TaskDetailPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [task, setTask] = useState<Task | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [isDispatching, setIsDispatching] = useState<boolean>(false);

  const fetchTaskDetail = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);

    try {
      let taskData: Task | null = null;
      try {
        taskData = await api.get<Task>(`/tasks/${id}`);
      } catch (err) {
        console.warn(`Could not fetch /api/tasks/${id}:`, err);
      }

      if (!taskData) {
        setError(`Task '${id}' not found`);
        return;
      }

      setTask(taskData);
    } catch (err: any) {
      console.error('Error loading task detail:', err);
      setError(err.message || `Failed to fetch task '${id}'`);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    fetchTaskDetail();
  }, [fetchTaskDetail]);

  const handleStatusChange = async (newStatus: string) => {
    if (!task) return;
    setTask((prev) => (prev ? { ...prev, status: newStatus } : null));

    try {
      await api.patch(`/tasks/${task.id}`, { status: newStatus });
    } catch (err) {
      console.error('Failed to update status on server:', err);
    }
  };

  const handleDispatch = async () => {
    if (!task) return;
    setIsDispatching(true);
    try {
      try {
        await api.post(`/tasks/${task.id}/dispatch`);
      } catch {
        await api.patch(`/tasks/${task.id}`, { status: 'dispatched' });
      }
      setTask((prev) => (prev ? { ...prev, status: 'dispatched' } : null));
    } catch (err: any) {
      console.error('Failed to dispatch task:', err);
    } finally {
      setIsDispatching(false);
    }
  };

  const handleApprove = async () => {
    if (!task) return;
    try {
      try {
        await api.post(`/tasks/${task.id}/approve`);
      } catch {
        await api.patch(`/tasks/${task.id}`, { awaiting_approval: false });
      }
      setTask((prev) => (prev ? { ...prev, awaiting_approval: false } : null));
    } catch (err: any) {
      console.error('Failed to approve task prompt:', err);
    }
  };

  if (loading) {
    return (
      <div className="p-12 text-center flex flex-col items-center justify-center min-h-[60vh] space-y-4">
        <RefreshCw className="w-8 h-8 animate-spin text-indigo-500" />
        <p className="text-gray-400 text-sm">Loading task specification for '{id}'...</p>
      </div>
    );
  }

  if (error || !task) {
    return (
      <div className="p-6 max-w-4xl mx-auto space-y-4">
        <div className="bg-red-500/10 border border-red-500/30 p-6 rounded-2xl text-red-400 space-y-3">
          <div className="flex items-center gap-2 font-bold text-lg">
            <AlertCircle className="w-5 h-5" />
            <span>Task Not Found</span>
          </div>
          <p className="text-sm text-red-300">{error || `Unable to load task details for ID '${id}'`}</p>
          <button
            onClick={() => navigate('/tasks')}
            className="px-4 py-2 rounded-lg bg-red-600 hover:bg-red-500 text-white text-xs font-semibold"
          >
            Back to Tasks List
          </button>
        </div>
      </div>
    );
  }

  const threadId = task.session_id || `session-${task.id.toLowerCase()}`;

  return (
    <div className="p-4 sm:p-6 space-y-6 max-w-[1600px] mx-auto min-h-[calc(100vh-4rem)]">
      {/* Header Banner */}
      <TaskHeader
        task={task}
        onStatusChange={handleStatusChange}
        onDispatch={handleDispatch}
        onApprove={handleApprove}
        onRefresh={fetchTaskDetail}
        isDispatching={isDispatching}
      />

      {/* Main Grid: Left side Task Details, Right side Copilot Chat */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
        {/* Left Column (Task Spec & Metadata) */}
        <div className="lg:col-span-7 xl:col-span-8 space-y-6">
          <TaskSpec task={task} />
          <TaskMeta task={task} />
        </div>

        {/* Right Column (AI Copilot Chat Sidecar) */}
        <div className="lg:col-span-5 xl:col-span-4 sticky top-20">
          <ChatPanelManager
            threadId={threadId}
            taskId={task.id}
            taskTitle={task.title}
            defaultMode="docked"
          />
        </div>
      </div>
    </div>
  );
};

export default TaskDetailPage;
