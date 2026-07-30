import React from 'react';
import { Navigation } from '../components/Navigation';
import { ChatPanelManager } from '../components/chat/ChatPanelManager';
import { GlobalChatButton } from '../components/chat/GlobalChatButton';
import { useGlobalChat } from '../contexts/GlobalChatContext';

interface DesktopLayoutProps {
  children: React.ReactNode;
}

export function DesktopLayout({ children }: DesktopLayoutProps) {
  const { mode, setMode } = useGlobalChat();

  return (
    <div className="h-screen bg-gray-950 text-gray-100 flex flex-row font-sans overflow-hidden">
      <Navigation />
      <div className="flex-1 flex overflow-hidden min-w-0">
        <main className="flex-1 max-w-7xl w-full mx-auto overflow-y-auto">
          {children}
        </main>
        
        {mode === 'docked' && (
          <aside className="w-auto border-l border-gray-800 bg-gray-900/50 flex-shrink-0 flex flex-col shadow-2xl z-40">
            <ChatPanelManager 
              threadId="global" 
              mode="docked"
              onModeChange={setMode}
              fullHeight={true}
              className="h-full border-0 rounded-none" 
            />
          </aside>
        )}
      </div>
      
      {(mode === 'floating' || mode === 'collapsed') && (
        <ChatPanelManager threadId="global" mode={mode} onModeChange={setMode} />
      )}
      
      {mode === 'closed' && (
        <GlobalChatButton onClick={() => setMode('floating')} />
      )}
    </div>
  );
}
