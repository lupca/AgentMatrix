import { create } from 'zustand';

interface User {
  id: string;
  name: string;
  email: string;
}

interface AppState {
  darkMode: boolean;
  toggleDarkMode: () => void;
  user: User | null;
  setUser: (user: User | null) => void;
  activeTaskId: string | null;
  setActiveTaskId: (id: string | null) => void;
}

export const useAppStore = create<AppState>((set) => ({
  darkMode: true,
  toggleDarkMode: () =>
    set((state) => {
      const nextMode = !state.darkMode;
      if (typeof document !== 'undefined') {
        if (nextMode) {
          document.documentElement.classList.add('dark');
        } else {
          document.documentElement.classList.remove('dark');
        }
      }
      return { darkMode: nextMode };
    }),
  user: {
    id: '1',
    name: 'Operator',
    email: 'operator@controltower.local',
  },
  setUser: (user) => set({ user }),
  activeTaskId: null,
  setActiveTaskId: (activeTaskId) => set({ activeTaskId }),
}));
