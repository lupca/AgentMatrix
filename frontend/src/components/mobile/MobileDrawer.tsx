import React, { useState } from 'react';
import { X, MessageSquare, Menu as MenuIcon } from 'lucide-react';
import { Link, useLocation } from 'react-router-dom';
import { useGlobalChat } from '../../contexts/GlobalChatContext';
import { navGroups } from '../Navigation';
import { useSessions } from '../../hooks/useSessions';
import { SessionSidebar } from '../chat/SessionSidebar';

export function MobileDrawer() {
  const { isMobileDrawerOpen, setIsMobileDrawerOpen } = useGlobalChat();
  const [activeTab, setActiveTab] = useState<'chats' | 'menu'>('chats');
  const location = useLocation();

  const sessionsState = useSessions({ level: 'global' });

  if (!isMobileDrawerOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Overlay backdrop */}
      <div 
        className="fixed inset-0 bg-black/60 backdrop-blur-sm transition-opacity"
        onClick={() => setIsMobileDrawerOpen(false)}
      />

      {/* Drawer content */}
      <div className="relative w-4/5 max-w-xs bg-gray-950 h-full flex flex-col z-10 shadow-2xl border-r border-gray-800">
        {/* Header with tabs & close button */}
        <div className="flex items-center justify-between p-3 border-b border-gray-800 shrink-0">
          <div className="flex space-x-1 bg-gray-900 p-1 rounded-lg">
            <button
              onClick={() => setActiveTab('chats')}
              className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                activeTab === 'chats'
                  ? 'bg-indigo-600 text-white shadow'
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              <MessageSquare className="w-3.5 h-3.5" />
              <span>Chats</span>
            </button>
            <button
              onClick={() => setActiveTab('menu')}
              className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                activeTab === 'menu'
                  ? 'bg-indigo-600 text-white shadow'
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              <MenuIcon className="w-3.5 h-3.5" />
              <span>Menu</span>
            </button>
          </div>

          <button
            onClick={() => setIsMobileDrawerOpen(false)}
            className="p-1.5 text-gray-400 hover:text-gray-200 hover:bg-gray-800 rounded-lg transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Tab content */}
        <div className="flex-1 overflow-y-auto">
          {activeTab === 'chats' ? (
            <div className="h-full [&>div]:w-full [&>div]:border-0">
              <SessionSidebar
                sessions={sessionsState.sessions}
                activeSessionId={sessionsState.activeSessionId}
                onSwitch={(id) => {
                  sessionsState.switchSession(id);
                  setIsMobileDrawerOpen(false);
                }}
                onCreate={sessionsState.createSession}
                onClose={sessionsState.closeSession}
                onRename={sessionsState.renameSession}
                onToggleSidebar={() => setIsMobileDrawerOpen(false)}
                loading={sessionsState.loading}
              />
            </div>
          ) : (
            <nav className="p-4 space-y-6">
              {navGroups.map((group, groupIdx) => (
                <div key={groupIdx}>
                  <h3 className="px-3 text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
                    {group.title}
                  </h3>
                  <div className="space-y-1">
                    {group.items.map((item) => {
                      const Icon = item.icon;
                      const isActive =
                        location.pathname === item.path ||
                        (item.path !== '/' && location.pathname.startsWith(item.path));

                      return (
                        <Link
                          key={item.path}
                          to={item.path}
                          onClick={() => setIsMobileDrawerOpen(false)}
                          className={`flex items-center space-x-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                            isActive
                              ? 'bg-gray-800 text-gray-100 shadow-sm'
                              : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800/50'
                          }`}
                        >
                          <Icon className="w-4 h-4" />
                          <span>{item.label}</span>
                        </Link>
                      );
                    })}
                  </div>
                </div>
              ))}
            </nav>
          )}
        </div>
      </div>
    </div>
  );
}
