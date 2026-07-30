import React, { createContext, useContext, useState, ReactNode } from 'react';

export type GlobalChatMode = 'closed' | 'floating' | 'docked' | 'collapsed' | 'fullscreen';

interface GlobalChatContextType {
  mode: GlobalChatMode;
  setMode: (mode: GlobalChatMode) => void;
  toggleChat: () => void;
  isMobileDrawerOpen: boolean;
  setIsMobileDrawerOpen: (isOpen: boolean) => void;
}

const GlobalChatContext = createContext<GlobalChatContextType | undefined>(undefined);

export function GlobalChatProvider({ children }: { children: ReactNode }) {
  // Sidebar open by default
  const [mode, setMode] = useState<GlobalChatMode>('docked');
  const [isMobileDrawerOpen, setIsMobileDrawerOpen] = useState(false);

  const toggleChat = () => {
    setMode((prev) => (prev === 'closed' ? 'docked' : 'closed'));
  };

  return (
    <GlobalChatContext.Provider value={{ mode, setMode, toggleChat, isMobileDrawerOpen, setIsMobileDrawerOpen }}>
      {children}
    </GlobalChatContext.Provider>
  );
}

export function useGlobalChat() {
  const context = useContext(GlobalChatContext);
  if (context === undefined) {
    throw new Error('useGlobalChat must be used within a GlobalChatProvider');
  }
  return context;
}
