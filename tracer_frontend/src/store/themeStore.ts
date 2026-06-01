import { create } from 'zustand';
import type { AppTheme } from '../types/trace';

const STORAGE_KEY = 'tracer-theme';

function getInitialTheme(): AppTheme {
  try {
    const saved = localStorage.getItem(STORAGE_KEY) as AppTheme | null;
    if (saved === 'dark' || saved === 'light') return saved;
  } catch {
    // localStorage unavailable
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function applyTheme(mode: AppTheme) {
  document.documentElement.setAttribute('data-theme', mode);
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // ignore
  }
}

interface ThemeState {
  mode:       AppTheme;
  toggleTheme: () => void;
  setTheme:    (mode: AppTheme) => void;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  mode: getInitialTheme(),

  toggleTheme: () => {
    const next = get().mode === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    set({ mode: next });
  },

  setTheme: (mode) => {
    applyTheme(mode);
    set({ mode });
  },
}));
