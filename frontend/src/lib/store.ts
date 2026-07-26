import { create } from 'zustand';

interface User {
  id: string;
  name: string;
  email: string;
}

interface AppState {
  user: User | null;
  setUser: (user: User | null) => void;
  activeTaskId: string | null;
  setActiveTaskId: (id: string | null) => void;
}

export const useAppStore = create<AppState>((set) => ({
  user: {
    id: '1',
    name: 'Operator',
    email: 'operator@controltower.local',
  },
  setUser: (user) => set({ user }),
  activeTaskId: null,
  setActiveTaskId: (activeTaskId) => set({ activeTaskId }),
}));
