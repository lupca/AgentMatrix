import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle,
  Bell,
  Check,
  CheckCircle2,
  CirclePlay,
  Clock3,
  RefreshCw,
  RotateCw,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  XCircle,
  type LucideIcon,
} from 'lucide-react';

import { useTaskEvents } from '../../hooks/useTaskEvents';
import { TaskEvent } from '../../types/taskEvent';

interface EventPresentation {
  label: string;
  icon: LucideIcon;
  iconClassName: string;
  backgroundClassName: string;
}

const EVENT_PRESENTATIONS: Record<string, EventPresentation> = {
  dispatched: {
    label: 'dispatched',
    icon: CirclePlay,
    iconClassName: 'text-blue-400',
    backgroundClassName: 'bg-blue-500/10 border-blue-500/20',
  },
  running: {
    label: 'started running',
    icon: Clock3,
    iconClassName: 'text-indigo-400',
    backgroundClassName: 'bg-indigo-500/10 border-indigo-500/20',
  },
  done: {
    label: 'completed',
    icon: CheckCircle2,
    iconClassName: 'text-emerald-400',
    backgroundClassName: 'bg-emerald-500/10 border-emerald-500/20',
  },
  failed: {
    label: 'failed',
    icon: AlertTriangle,
    iconClassName: 'text-red-400',
    backgroundClassName: 'bg-red-500/10 border-red-500/20',
  },
  cancelled: {
    label: 'was cancelled',
    icon: XCircle,
    iconClassName: 'text-gray-400',
    backgroundClassName: 'bg-gray-500/10 border-gray-500/20',
  },
  gate_pending: {
    label: 'needs gate approval',
    icon: ShieldAlert,
    iconClassName: 'text-amber-400',
    backgroundClassName: 'bg-amber-500/10 border-amber-500/20',
  },
  gate_passed: {
    label: 'passed its gate',
    icon: ShieldCheck,
    iconClassName: 'text-emerald-400',
    backgroundClassName: 'bg-emerald-500/10 border-emerald-500/20',
  },
  gate_rejected: {
    label: 'had its gate rejected',
    icon: ShieldX,
    iconClassName: 'text-red-400',
    backgroundClassName: 'bg-red-500/10 border-red-500/20',
  },
  retrying: {
    label: 'is retrying',
    icon: RotateCw,
    iconClassName: 'text-purple-400',
    backgroundClassName: 'bg-purple-500/10 border-purple-500/20',
  },
};

const FALLBACK_PRESENTATION: EventPresentation = {
  label: 'was updated',
  icon: Bell,
  iconClassName: 'text-gray-400',
  backgroundClassName: 'bg-gray-500/10 border-gray-500/20',
};

function payloadText(event: TaskEvent): string | null {
  const candidates =
    event.event_type === 'failed'
      ? [event.payload.error]
      : event.event_type.startsWith('gate_')
        ? [event.payload.gate, event.payload.reason]
        : event.event_type === 'done'
          ? [event.payload.result_ref]
          : event.event_type === 'retrying'
            ? [
                event.payload.attempt !== undefined
                  ? `Attempt ${String(event.payload.attempt)}`
                  : undefined,
              ]
            : [];

  const value = candidates.find(
    (candidate) => typeof candidate === 'string' && candidate.trim().length > 0,
  );
  return typeof value === 'string' ? value : null;
}

function formatEventTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function NotificationCenter() {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const {
    events,
    unreadCount,
    loading,
    error,
    refetch,
    clearEvents,
    markAllAsRead,
  } = useTaskEvents();

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsOpen(false);
      }
    };

    document.addEventListener('mousedown', closeOnOutsideClick);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeOnOutsideClick);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [isOpen]);

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setIsOpen((open) => !open)}
        className="relative flex w-full items-center space-x-3 rounded-md px-3 py-2 text-sm font-medium text-gray-400 transition-colors hover:bg-gray-800/50 hover:text-gray-200"
        aria-label={
          unreadCount > 0
            ? `Notifications, ${unreadCount} unread`
            : 'Notifications'
        }
        aria-expanded={isOpen}
        aria-controls="task-notifications-panel"
      >
        <Bell className="h-4 w-4" />
        <span>Notifications</span>
        {unreadCount > 0 && (
          <span className="ml-auto inline-flex min-w-5 items-center justify-center rounded-full bg-indigo-500 px-1.5 py-0.5 text-[10px] font-bold leading-none text-white">
            {unreadCount > 99 ? '99+' : unreadCount}
          </span>
        )}
      </button>

      {isOpen && (
        <section
          id="task-notifications-panel"
          aria-label="Recent task notifications"
          className="fixed bottom-4 left-[16.5rem] z-50 flex max-h-[min(36rem,calc(100vh-2rem))] w-[min(24rem,calc(100vw-17.5rem))] min-w-80 flex-col overflow-hidden rounded-xl border border-gray-800 bg-gray-900 shadow-2xl shadow-black/30"
        >
          <div className="flex items-start justify-between gap-4 border-b border-gray-800 px-4 py-3">
            <div>
              <h2 className="text-sm font-semibold text-gray-100">
                Recent task events
              </h2>
              <p className="mt-0.5 text-xs text-gray-500">
                Updates are checked every 10 seconds
              </p>
            </div>
            <button
              type="button"
              onClick={refetch}
              className="rounded-md p-1.5 text-gray-500 transition-colors hover:bg-gray-800 hover:text-gray-200"
              aria-label="Refresh notifications"
            >
              <RefreshCw className="h-4 w-4" />
            </button>
          </div>

          {error && (
            <div
              role="alert"
              className="border-b border-red-500/20 bg-red-500/10 px-4 py-2 text-xs text-red-300"
            >
              Notifications could not be refreshed: {error}
            </div>
          )}

          <div className="min-h-0 flex-1 overflow-y-auto">
            {loading && events.length === 0 ? (
              <div className="space-y-3 p-4" aria-label="Loading notifications">
                {[1, 2, 3].map((item) => (
                  <div
                    key={item}
                    className="h-16 animate-pulse rounded-lg bg-gray-800/70"
                  />
                ))}
              </div>
            ) : events.length === 0 ? (
              <div className="px-6 py-12 text-center">
                <Bell className="mx-auto h-7 w-7 text-gray-600" />
                <p className="mt-3 text-sm font-medium text-gray-300">
                  No recent events
                </p>
                <p className="mt-1 text-xs text-gray-500">
                  New task activity will appear here.
                </p>
              </div>
            ) : (
              <ul className="divide-y divide-gray-800/80">
                {events.map((event) => {
                  const presentation =
                    EVENT_PRESENTATIONS[event.event_type] ??
                    FALLBACK_PRESENTATION;
                  const Icon = presentation.icon;
                  const detail = payloadText(event);

                  return (
                    <li key={event.id}>
                      <Link
                        to={`/tasks/${encodeURIComponent(event.task_id)}`}
                        onClick={() => {
                          markAllAsRead();
                          setIsOpen(false);
                        }}
                        className="flex gap-3 px-4 py-3 transition-colors hover:bg-gray-800/50"
                      >
                        <span
                          className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border ${presentation.backgroundClassName}`}
                        >
                          <Icon
                            className={`h-4 w-4 ${presentation.iconClassName}`}
                          />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block text-sm text-gray-200">
                            <span className="font-semibold">{event.task_id}</span>{' '}
                            {presentation.label}
                          </span>
                          {detail && (
                            <span className="mt-0.5 block truncate text-xs text-gray-500">
                              {detail}
                            </span>
                          )}
                          <time
                            dateTime={event.created_at}
                            className="mt-1 block text-[11px] text-gray-600"
                          >
                            {formatEventTime(event.created_at)}
                          </time>
                        </span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {events.length > 0 && (
            <div className="flex items-center justify-between border-t border-gray-800 px-3 py-2">
              <button
                type="button"
                onClick={clearEvents}
                className="rounded-md px-2 py-1 text-xs text-gray-500 transition-colors hover:bg-gray-800 hover:text-gray-300"
              >
                Clear
              </button>
              <button
                type="button"
                onClick={markAllAsRead}
                disabled={unreadCount === 0}
                className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-indigo-400 transition-colors hover:bg-indigo-500/10 disabled:cursor-default disabled:text-gray-600 disabled:hover:bg-transparent"
              >
                <Check className="h-3.5 w-3.5" />
                Mark all read
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

export default NotificationCenter;
