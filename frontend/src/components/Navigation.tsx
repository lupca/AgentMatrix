import { Link, useLocation } from 'react-router-dom';
import { 
  LayoutDashboard, 
  FolderKanban, 
  Bot, 
  CheckSquare, 
  LayoutGrid, 
  Settings as SettingsIcon, 
  Moon, 
  Sun, 
  Activity 
} from 'lucide-react';
import { useTheme } from '../contexts/ThemeContext';
import { NotificationCenter } from './notifications/NotificationCenter';

export const navGroups = [
  {
    title: 'Overview',
    items: [
      { path: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
    ]
  },
  {
    title: 'Work & Execution',
    items: [
      { path: '/projects', label: 'Projects', icon: FolderKanban },
      { path: '/tasks', label: 'Tasks', icon: CheckSquare },
      { path: '/kanban', label: 'Kanban', icon: LayoutGrid },
    ]
  },
  {
    title: 'AI Assets',
    items: [
      { path: '/agents', label: 'Agents', icon: Bot },
    ]
  },
  {
    title: 'System',
    items: [
      { path: '/telemetry', label: 'Telemetry', icon: Activity },
      { path: '/settings', label: 'Settings', icon: SettingsIcon },
    ]
  }
];

export function Navigation() {
  const location = useLocation();
  const { theme, toggleTheme } = useTheme();


  return (
    <aside className="w-64 h-full bg-gray-950/80 backdrop-blur border-r border-gray-800 flex flex-col shrink-0">
      <div className="h-16 flex items-center px-6 border-b border-gray-800 shrink-0">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-tr from-indigo-500 to-purple-500 flex items-center justify-center font-bold text-white shadow-lg shadow-indigo-500/20">
            CT
          </div>
          <span className="font-bold text-lg text-gray-100 tracking-wide">
            Control Tower <span className="text-xs px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-400 border border-indigo-500/30">v2</span>
          </span>
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto py-6 px-4 space-y-8">
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
                    className={`flex items-center space-x-3 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
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

      <div className="space-y-1 border-t border-gray-800 p-4 shrink-0">
        <NotificationCenter />
        <button
          onClick={toggleTheme}
          className="flex items-center w-full space-x-3 px-3 py-2 rounded-md text-sm font-medium text-gray-400 hover:text-gray-200 hover:bg-gray-800/50 transition-colors"
          aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
        >
          {theme === 'dark' ? (
            <>
              <Sun className="w-4 h-4" />
              <span>Light Mode</span>
            </>
          ) : (
            <>
              <Moon className="w-4 h-4" />
              <span>Dark Mode</span>
            </>
          )}
        </button>
      </div>
    </aside>
  );
}
