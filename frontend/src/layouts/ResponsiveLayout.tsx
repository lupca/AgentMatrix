import React from 'react';
import { useIsMobile } from '../hooks/useIsMobile';
import { MobileLayout } from './MobileLayout';
import { DesktopLayout } from './DesktopLayout';

interface ResponsiveLayoutProps {
  children: React.ReactNode;
}

export function ResponsiveLayout({ children }: ResponsiveLayoutProps) {
  const isMobile = useIsMobile();

  if (isMobile) {
    return <MobileLayout>{children}</MobileLayout>;
  }

  return <DesktopLayout>{children}</DesktopLayout>;
}
