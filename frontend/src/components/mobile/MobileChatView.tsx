import React from 'react';
import { ChatPanelManager } from '../chat/ChatPanelManager';
import { useGlobalChat } from '../../contexts/GlobalChatContext';

export function MobileChatView() {
  const { setMode } = useGlobalChat();

  return (
    <div className="w-full h-full flex flex-col bg-gray-950 overflow-hidden">
      <ChatPanelManager
        threadId="global"
        mode="docked"
        onModeChange={setMode}
        fullHeight={true}
        className="h-full w-full border-0 rounded-none bg-gray-950"
      />
    </div>
  );
}
