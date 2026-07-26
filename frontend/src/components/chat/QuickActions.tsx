import React, { useState } from 'react';
import { ChevronDown, CircleStop, Play, Search, ShieldCheck } from 'lucide-react';
import { Task } from '../../types/task';

export type QuickActionId = 'dispatch' | 'status' | 'cancel' | 'verdict';

export interface QuickAction {
  id: QuickActionId;
  label: string;
}

export const getActionsForStatus = (status: string): QuickAction[] => {
  switch (status.toLowerCase()) {
    case 'todo':
      return [{ id: 'dispatch', label: 'Dispatch' }];
    case 'dispatched':
      return [
        { id: 'status', label: 'Status' },
        { id: 'cancel', label: 'Cancel' },
      ];
    case 'in-review':
    case 'in_review':
      return [{ id: 'verdict', label: 'Verdict' }];
    default:
      return [];
  }
};

interface QuickActionsProps {
  task: Task;
  onSendCommand: (command: string) => Promise<void> | void;
  onActionComplete?: () => Promise<void> | void;
  disabled?: boolean;
}

const commandFor = (action: QuickActionId, task: Task, value?: string) => {
  switch (action) {
    case 'dispatch':
      return `/dispatch ${task.id}${task.executor ? ` ${task.executor}` : ''}`;
    case 'status':
      return `/status ${task.id}`;
    case 'cancel':
      return `/cancel ${task.id}`;
    case 'verdict':
      return `/verdict ${task.id} ${value}`;
  }
};

export const QuickActions: React.FC<QuickActionsProps> = ({
  task,
  onSendCommand,
  onActionComplete,
  disabled = false,
}) => {
  const [verdict, setVerdict] = useState('');
  const actions = getActionsForStatus(task.status);

  if (actions.length === 0) return null;

  const icons = {
    dispatch: Play,
    status: Search,
    cancel: CircleStop,
    verdict: ShieldCheck,
  } satisfies Record<QuickActionId, React.ComponentType<{ className?: string }>>;

  const handleAction = async (action: QuickActionId, value?: string) => {
    const command = commandFor(action, task, value);
    if (!command) return;

    try {
      await onSendCommand(command);
    } finally {
      if (action !== 'status') {
        await onActionComplete?.();
      }
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5" aria-label="Task quick actions">
      {actions.map((action) => {
        const Icon = icons[action.id];
        if (action.id === 'verdict') {
          return (
            <label key={action.id} className="relative inline-flex items-center">
              <Icon className="pointer-events-none absolute left-2 h-3 w-3 text-purple-300" />
              <select
                aria-label="Choose verdict"
                value={verdict}
                onChange={(event) => {
                  const value = event.target.value;
                  setVerdict(value);
                  if (value) void handleAction(action.id, value);
                }}
                disabled={disabled}
                className="h-7 appearance-none rounded-lg border border-purple-500/30 bg-purple-500/10 py-1 pl-7 pr-7 text-[11px] font-semibold text-purple-200 outline-none hover:bg-purple-500/20 disabled:pointer-events-none disabled:opacity-40"
              >
                <option value="">Verdict</option>
                <option value="pass">Pass</option>
                <option value="changes">Request changes</option>
              </select>
              <ChevronDown className="pointer-events-none absolute right-1.5 h-3 w-3 text-purple-300" />
            </label>
          );
        }

        return (
          <button
            key={action.id}
            type="button"
            onClick={() => void handleAction(action.id)}
            disabled={disabled}
            className="inline-flex h-7 items-center gap-1 rounded-lg border border-gray-700 bg-gray-900 px-2.5 text-[11px] font-semibold text-gray-300 transition-colors hover:border-indigo-500/50 hover:bg-gray-800 hover:text-white disabled:pointer-events-none disabled:opacity-40"
          >
            <Icon className="h-3 w-3" />
            {action.label}
            {action.id === 'dispatch' && <ChevronDown className="h-3 w-3 text-gray-500" />}
          </button>
        );
      })}
    </div>
  );
};

export default QuickActions;
