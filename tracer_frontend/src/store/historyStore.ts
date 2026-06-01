import { create } from 'zustand';
import * as api from '../api/client';
import type { HistorySummary } from '../types/trace';

interface HistoryState {
  entries:   HistorySummary[];
  total:     number;
  loading:   boolean;
  error:     string | null;
  query:     string;

  setQuery:   (q: string) => void;
  fetchPage:  (q?: string) => Promise<void>;
  deleteEntry:(id: string) => Promise<void>;
  refresh:    () => Promise<void>;
}

export const useHistoryStore = create<HistoryState>((set, get) => ({
  entries: [],
  total:   0,
  loading: false,
  error:   null,
  query:   '',

  setQuery: (q) => set({ query: q }),

  fetchPage: async (q?: string) => {
    const query = q ?? get().query;
    set({ loading: true, error: null });
    try {
      const result = await api.listHistory({ q: query || undefined, limit: 100 });
      set({ entries: result.entries, total: result.total, loading: false });
    } catch (err) {
      set({
        loading: false,
        error: err instanceof Error ? err.message : 'Failed to load history',
      });
    }
  },

  deleteEntry: async (id) => {
    try {
      await api.deleteHistoryEntry(id);
      set((s) => ({
        entries: s.entries.filter((e) => e.id !== id),
        total:   Math.max(0, s.total - 1),
      }));
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to delete entry' });
    }
  },

  refresh: async () => {
    await get().fetchPage(get().query);
  },
}));
