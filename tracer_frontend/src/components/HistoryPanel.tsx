import React, { useEffect, useRef, useCallback } from 'react';
import { Search, Trash2, Clock, ArrowRight, RefreshCw } from 'lucide-react';
import { useHistoryStore } from '../store/historyStore';
import { useTraceStore } from '../store/traceStore';
import { getHistoryEntry } from '../api/client';
import type { HistorySummary } from '../types/trace';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

function formatDuration(s: number | null): string {
  if (s == null) return '';
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

// ---------------------------------------------------------------------------
// Single history entry row
// ---------------------------------------------------------------------------

function HistoryRow({
  entry,
  onLoad,
  onDelete,
}: {
  entry: HistorySummary;
  onLoad: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div
      className="history-row"
      onClick={() => onLoad(entry.id)}
      title={`Load trace: ${entry.src_ip} → ${entry.dst_ip}`}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        {/* Route */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '3px' }}>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{entry.src_ip}</span>
          <ArrowRight size={11} style={{ flexShrink: 0, color: 'var(--text-muted)' }} />
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{entry.dst_ip}</span>
        </div>
        {/* Meta */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px', color: 'var(--text-muted)' }}>
          <Clock size={10} style={{ flexShrink: 0 }} />
          <span>{formatDate(entry.created_at)}</span>
          {entry.duration_s != null && (
            <span style={{ color: 'var(--text-faint)' }}>· {formatDuration(entry.duration_s)}</span>
          )}
        </div>
      </div>

      {/* Delete button */}
      <button
        className="btn btn-ghost history-delete-btn"
        onClick={(e) => { e.stopPropagation(); onDelete(entry.id); }}
        title="Delete this trace"
        style={{ flexShrink: 0, padding: '4px', opacity: 0.5 }}
      >
        <Trash2 size={13} />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// HistoryPanel
// ---------------------------------------------------------------------------

export default function HistoryPanel() {
  const {
    entries, total, loading, error,
    query, setQuery, fetchPage, deleteEntry,
  } = useHistoryStore();

  const loadGraph = useTraceStore((s) => s.loadGraph);
  const setSrcIp  = useTraceStore((s) => s.setSrcIp);
  const setDstIp  = useTraceStore((s) => s.setDstIp);

  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load history on mount
  useEffect(() => {
    void fetchPage();
  }, []);

  const handleQueryChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const q = e.target.value;
      setQuery(q);
      if (debounceTimer.current) clearTimeout(debounceTimer.current);
      debounceTimer.current = setTimeout(() => {
        void fetchPage(q);
      }, 350);
    },
    [setQuery, fetchPage],
  );

  const handleLoad = useCallback(
    async (id: string) => {
      try {
        const detail = await getHistoryEntry(id);
        if (detail.graph) {
          setSrcIp(detail.src_ip);
          setDstIp(detail.dst_ip);
          loadGraph(detail.graph);
        }
      } catch (err) {
        console.error('Failed to load history entry:', err);
      }
    },
    [loadGraph, setSrcIp, setDstIp],
  );

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        borderTop: '1px solid var(--border-color)',
        minHeight: 0,
        flex: 1,
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: '10px 16px 8px',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
        }}
      >
        <div
          style={{
            flex: 1,
            fontSize: '11px',
            fontWeight: 700,
            letterSpacing: '0.08em',
            color: 'var(--text-muted)',
            textTransform: 'uppercase',
          }}
        >
          Trace History {total > 0 && `(${total})`}
        </div>
        <button
          className="btn btn-ghost"
          onClick={() => void fetchPage()}
          title="Refresh history"
          style={{ padding: '3px', opacity: 0.6 }}
          disabled={loading}
        >
          <RefreshCw size={13} className={loading ? 'spin' : ''} />
        </button>
      </div>

      {/* Search */}
      <div style={{ padding: '0 16px 8px', position: 'relative' }}>
        <Search
          size={13}
          style={{
            position: 'absolute',
            left: '26px',
            top: '50%',
            transform: 'translateY(-50%)',
            color: 'var(--text-muted)',
            pointerEvents: 'none',
          }}
        />
        <input
          type="text"
          value={query}
          onChange={handleQueryChange}
          placeholder="Search IP, date…"
          className="form-input"
          style={{ width: '100%', paddingLeft: '28px', fontSize: '12px' }}
        />
      </div>

      {/* List */}
      <div style={{ flex: 1, overflowY: 'auto', paddingBottom: '8px' }}>
        {error && (
          <div style={{ padding: '8px 16px', fontSize: '12px', color: 'var(--color-error)' }}>
            {error}
          </div>
        )}
        {loading && entries.length === 0 && (
          <div style={{ padding: '20px 16px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '12px' }}>
            Loading…
          </div>
        )}
        {!loading && entries.length === 0 && (
          <div style={{ padding: '20px 16px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '12px' }}>
            {query ? 'No matches found.' : 'No saved traces yet.'}
          </div>
        )}
        {entries.map((entry) => (
          <HistoryRow
            key={entry.id}
            entry={entry}
            onLoad={handleLoad}
            onDelete={(id) => void deleteEntry(id)}
          />
        ))}
      </div>
    </div>
  );
}
