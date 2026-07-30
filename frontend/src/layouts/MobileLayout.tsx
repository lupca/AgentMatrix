import React from 'react';
import { useLocation } from 'react-router-dom';
import { MobileHeader } from '../components/mobile/MobileHeader';
import { MobileDrawer } from '../components/mobile/MobileDrawer';
import { MobileChatView } from '../components/mobile/MobileChatView';

interface MobileLayoutProps {
  children: React.ReactNode;
}

export function MobileLayout({ children }: MobileLayoutProps) {
  const location = useLocation();
  const isChatView = location.pathname === '/';

  return (
    <div className="h-screen w-screen bg-gray-950 text-gray-100 flex flex-col font-sans overflow-hidden">
      <MobileHeader />
      <MobileDrawer />

      <main className="flex-1 min-h-0 overflow-y-auto">
        {isChatView ? <MobileChatView /> : children}
      </main>
    </div>
  );
}
