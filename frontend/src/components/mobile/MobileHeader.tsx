import React from 'react';
import { Menu, ArrowLeft, Sun, Moon } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useGlobalChat } from '../../contexts/GlobalChatContext';
import { useTheme } from '../../contexts/ThemeContext';
import { NotificationCenter } from '../notifications/NotificationCenter';

export function MobileHeader() {
  const { setIsMobileDrawerOpen } = useGlobalChat();
  const { theme, toggleTheme } = useTheme();
  const location = useLocation();
  const navigate = useNavigate();

  const isRootPage = location.pathname === '/';

  return (
    <header className="h-14 bg-gray-950/90 backdrop-blur border-b border-gray-800 flex items-center justify-between px-4 sticky top-0 z-30 shrink-0">
      <div className="flex items-center space-x-3">
        {!isRootPage ? (
          <button
            onClick={() => navigate('/')}
            className="p-2 rounded-lg text-gray-400 hover:text-gray-100 hover:bg-gray-800/60 transition-colors"
            aria-label="Back to Chat"
          >
            <ArrowLeft className="w-5 h-5" />
          </button>
        ) : (
          <button
            onClick={() => setIsMobileDrawerOpen(true)}
            className="p-2 rounded-lg text-gray-400 hover:text-gray-100 hover:bg-gray-800/60 transition-colors"
            aria-label="Open Menu"
          >
            <Menu className="w-5 h-5" />
          </button>
        )}

        <div className="flex items-center space-x-2">
          <div className="w-6 h-6 rounded bg-gradient-to-tr from-indigo-500 to-purple-500 flex items-center justify-center font-bold text-xs text-white shadow-sm">
            CT
          </div>
          <span className="font-bold text-base text-gray-100 tracking-wide">
            Control Tower
          </span>
        </div>
      </div>

      <div className="flex items-center space-x-1">
        <NotificationCenter />
        <button
          onClick={toggleTheme}
          className="p-2 rounded-lg text-gray-400 hover:text-gray-100 hover:bg-gray-800/60 transition-colors"
          aria-label="Toggle Theme"
        >
          {theme === 'dark' ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
        </button>
      </div>
    </header>
  );
}
