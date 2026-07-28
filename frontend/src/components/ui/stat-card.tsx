import React from 'react';
import { cn } from '@/lib/utils';

export interface StatCardTrend {
  value: string | number;
  isPositive?: boolean;
  label?: string;
}

export interface StatCardProps extends React.HTMLAttributes<HTMLDivElement> {
  label: React.ReactNode;
  value: React.ReactNode;
  icon?: React.ReactNode;
  trend?: StatCardTrend | React.ReactNode;
  description?: React.ReactNode;
}

export const StatCard = React.forwardRef<HTMLDivElement, StatCardProps>(
  ({ className, label, value, icon, trend, description, ...props }, ref) => {
    const renderTrend = () => {
      if (!trend) return null;
      if (React.isValidElement(trend) || typeof trend === 'string' || typeof trend === 'number') {
        return <div className="text-xs font-medium text-gray-400">{trend}</div>;
      }
      const trendObj = trend as StatCardTrend;
      const isPos = trendObj.isPositive;
      return (
        <div
          className={cn(
            'inline-flex items-center gap-1 text-xs font-medium',
            isPos === true && 'text-emerald-400',
            isPos === false && 'text-red-400',
            isPos === undefined && 'text-gray-400'
          )}
        >
          <span>{trendObj.isPositive ? '↑' : trendObj.isPositive === false ? '↓' : ''}</span>
          <span>{trendObj.value}</span>
          {trendObj.label && <span className="text-gray-500 font-normal">{trendObj.label}</span>}
        </div>
      );
    };

    return (
      <div
        ref={ref}
        className={cn(
          'rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between transition-all duration-200 hover:border-gray-700/80',
          className
        )}
        {...props}
      >
        <div className="space-y-1">
          <p className="text-xs font-medium text-gray-400">{label}</p>
          <div className="text-2xl font-bold text-gray-100">{value}</div>
          {trend && renderTrend()}
          {description && <p className="text-xs text-gray-500">{description}</p>}
        </div>
        {icon && (
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-gray-800/60 text-gray-300">
            {icon}
          </div>
        )}
      </div>
    );
  }
);

StatCard.displayName = 'StatCard';
