import React from 'react';
import { AlertCircle, AlertTriangle, Info, RefreshCw } from 'lucide-react';
import { cn } from '@/lib/utils';

export type AlertSeverity = 'error' | 'warning' | 'info';

export interface AlertBannerProps extends Omit<React.HTMLAttributes<HTMLDivElement>, 'title'> {
  severity?: AlertSeverity;
  title?: React.ReactNode;
  message?: React.ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  action?: React.ReactNode;
}

const severityConfig: Record<
  AlertSeverity,
  {
    container: string;
    icon: React.ComponentType<{ className?: string }>;
    iconColor: string;
    button: string;
  }
> = {
  error: {
    container: 'bg-red-500/10 border-red-500/30 text-red-400',
    icon: AlertCircle,
    iconColor: 'text-red-400',
    button: 'bg-red-500/20 hover:bg-red-500/30 text-red-300 border-red-500/30',
  },
  warning: {
    container: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
    icon: AlertTriangle,
    iconColor: 'text-amber-400',
    button: 'bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border-amber-500/30',
  },
  info: {
    container: 'bg-blue-500/10 border-blue-500/30 text-blue-400',
    icon: Info,
    iconColor: 'text-blue-400',
    button: 'bg-blue-500/20 hover:bg-blue-500/30 text-blue-300 border-blue-500/30',
  },
};

export const AlertBanner = React.forwardRef<HTMLDivElement, AlertBannerProps>(
  (
    {
      className,
      severity = 'error',
      title,
      message,
      children,
      onRetry,
      retryLabel = 'Retry',
      action,
      ...props
    },
    ref
  ) => {
    const config = severityConfig[severity] || severityConfig.error;
    const IconComponent = config.icon;
    const content = message || children;

    return (
      <div
        ref={ref}
        role="alert"
        className={cn(
          'p-4 rounded-xl border flex items-start justify-between gap-3 text-sm backdrop-blur-sm transition-colors',
          config.container,
          className
        )}
        {...props}
      >
        <div className="flex items-start gap-3 flex-1 min-w-0">
          <IconComponent className={cn('h-5 w-5 shrink-0 mt-0.5', config.iconColor)} />
          <div className="flex-1 min-w-0">
            {title && <h5 className="font-semibold leading-none mb-1 text-gray-100">{title}</h5>}
            {content && <div className="text-sm opacity-90 leading-relaxed break-words">{content}</div>}
          </div>
        </div>
        {(onRetry || action) && (
          <div className="flex items-center gap-2 shrink-0">
            {onRetry && (
              <button
                type="button"
                onClick={onRetry}
                className={cn(
                  'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-all duration-150 active:scale-95 cursor-pointer',
                  config.button
                )}
              >
                <RefreshCw className="h-3.5 w-3.5" />
                <span>{retryLabel}</span>
              </button>
            )}
            {action}
          </div>
        )}
      </div>
    );
  }
);

AlertBanner.displayName = 'AlertBanner';
