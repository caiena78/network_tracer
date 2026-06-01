import React from 'react';
import { Network } from 'lucide-react';
import TraceForm from './components/TraceForm';
import DiagramCanvas from './components/DiagramCanvas';
import DetailPanel from './components/DetailPanel';
import HistoryPanel from './components/HistoryPanel';
import ThemeToggle from './components/ThemeToggle';
import { useTraceStore } from './store/traceStore';

export default function App() {
  const graph           = useTraceStore((s) => s.graph);
  const selectedElement = useTraceStore((s) => s.selectedElement);
  const phase           = useTraceStore((s) => s.phase);

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100vh',
        width: '100vw',
        overflow: 'hidden',
        background: 'var(--bg-app)',
      }}
    >
      {/* ── Header ─────────────────────────────────────────── */}
      <header
        style={{
          height: '48px',
          minHeight: '48px',
          display: 'flex',
          alignItems: 'center',
          padding: '0 16px',
          background: 'var(--bg-header)',
          borderBottom: '1px solid var(--border-color)',
          gap: '10px',
          flexShrink: 0,
          zIndex: 20,
        }}
      >
        <Network size={18} style={{ color: 'var(--color-primary)', flexShrink: 0 }} />
        <span
          style={{
            fontWeight: 700,
            fontSize: '15px',
            color: 'var(--text-primary)',
            letterSpacing: '-0.01em',
          }}
        >
          Network Path Tracer
        </span>
        {phase === 'streaming' && (
          <span style={{ fontSize: '11px', color: 'var(--color-primary)', display: 'flex', alignItems: 'center', gap: '5px', marginLeft: '4px' }}>
            <span className="spinner" style={{ width: '10px', height: '10px', borderWidth: '1.5px' }} />
            Tracing…
          </span>
        )}
        {phase === 'enriching' && (
          <span style={{ fontSize: '11px', color: 'var(--color-success)', display: 'flex', alignItems: 'center', gap: '5px', marginLeft: '4px' }}>
            <span className="spinner" style={{ width: '10px', height: '10px', borderWidth: '1.5px', borderTopColor: 'var(--color-success)' }} />
            Enriching interfaces…
          </span>
        )}
        <div style={{ flex: 1 }} />
        <ThemeToggle />
      </header>

      {/* ── Body ───────────────────────────────────────────── */}
      <div
        style={{
          flex: 1,
          display: 'flex',
          overflow: 'hidden',
          minHeight: 0,
        }}
      >
        {/* ── Left sidebar ──────────────────────────────────── */}
        <aside
          style={{
            width: '300px',
            minWidth: '260px',
            maxWidth: '340px',
            flexShrink: 0,
            background: 'var(--bg-sidebar)',
            borderRight: '1px solid var(--border-color)',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          <TraceForm />
          <HistoryPanel />
        </aside>

        {/* ── Main content ──────────────────────────────────── */}
        <main
          style={{
            flex: 1,
            display: 'flex',
            flexDirection: 'row',
            overflow: 'hidden',
            minWidth: 0,
          }}
        >
          {/* Diagram */}
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, overflow: 'hidden' }}>
            <DiagramCanvas graph={graph} />
          </div>

          {/* Detail panel — shown when a node/edge is selected */}
          {selectedElement && <DetailPanel />}
        </main>
      </div>
    </div>
  );
}
