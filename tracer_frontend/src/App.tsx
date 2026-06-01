import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Network } from 'lucide-react';
import TraceForm from './components/TraceForm';
import DiagramCanvas from './components/DiagramCanvas';
import DetailPanel from './components/DetailPanel';
import HistoryPanel from './components/HistoryPanel';
import ThemeToggle from './components/ThemeToggle';
import { useTraceStore } from './store/traceStore';

const SIDEBAR_MIN   = 200;
const SIDEBAR_MAX   = 700;
const SIDEBAR_DEFAULT = 300;
const STORAGE_KEY   = 'tracer-sidebar-width';

function useSidebarResize() {
  const [width, setWidth] = useState<number>(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) {
        const n = parseInt(saved, 10);
        if (n >= SIDEBAR_MIN && n <= SIDEBAR_MAX) return n;
      }
    } catch { /* ignore */ }
    return SIDEBAR_DEFAULT;
  });

  const isResizing  = useRef(false);
  const startX      = useRef(0);
  const startWidth  = useRef(0);
  const latestWidth = useRef(width);

  // Keep latestWidth in sync without causing re-renders
  useEffect(() => { latestWidth.current = width; }, [width]);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isResizing.current = true;
    startX.current     = e.clientX;
    startWidth.current = latestWidth.current;
    document.body.style.cursor     = 'col-resize';
    document.body.style.userSelect = 'none';
  }, []);

  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!isResizing.current) return;
      const delta    = e.clientX - startX.current;
      const newWidth = Math.min(Math.max(startWidth.current + delta, SIDEBAR_MIN), SIDEBAR_MAX);
      setWidth(newWidth);
    };

    const onMouseUp = () => {
      if (!isResizing.current) return;
      isResizing.current             = false;
      document.body.style.cursor     = '';
      document.body.style.userSelect = '';
      try {
        localStorage.setItem(STORAGE_KEY, String(latestWidth.current));
      } catch { /* ignore */ }
    };

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup',   onMouseUp);
    return () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup',   onMouseUp);
    };
  }, []);

  return { width, onMouseDown };
}

export default function App() {
  const graph           = useTraceStore((s) => s.graph);
  const selectedElement = useTraceStore((s) => s.selectedElement);
  const phase           = useTraceStore((s) => s.phase);

  const { width: sidebarWidth, onMouseDown: startResize } = useSidebarResize();

  return (
    <div
      style={{
        display:       'flex',
        flexDirection: 'column',
        height:        '100vh',
        width:         '100vw',
        overflow:      'hidden',
        background:    'var(--bg-app)',
      }}
    >
      {/* ── Header ─────────────────────────────────────────── */}
      <header
        style={{
          height:       '48px',
          minHeight:    '48px',
          display:      'flex',
          alignItems:   'center',
          padding:      '0 16px',
          background:   'var(--bg-header)',
          borderBottom: '1px solid var(--border-color)',
          gap:          '10px',
          flexShrink:    0,
          zIndex:        20,
        }}
      >
        <Network size={18} style={{ color: 'var(--color-primary)', flexShrink: 0 }} />
        <span style={{ fontWeight: 700, fontSize: '15px', color: 'var(--text-primary)', letterSpacing: '-0.01em' }}>
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
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden', minHeight: 0 }}>

        {/* ── Left sidebar (resizable) ───────────────────────── */}
        <aside
          style={{
            width:         sidebarWidth,
            minWidth:      SIDEBAR_MIN,
            maxWidth:      SIDEBAR_MAX,
            flexShrink:    0,
            background:    'var(--bg-sidebar)',
            display:       'flex',
            flexDirection: 'column',
            overflow:      'hidden',
            position:      'relative',   // needed for the drag handle
          }}
        >
          <TraceForm />
          <HistoryPanel />

          {/* ── Drag handle ───────────────────────────────────── */}
          <div
            onMouseDown={startResize}
            title="Drag to resize sidebar"
            style={{
              position: 'absolute',
              top:      0,
              right:    0,
              bottom:   0,
              width:    '5px',
              cursor:   'col-resize',
              zIndex:   30,
              background: 'transparent',
              transition: 'background 0.15s',
            }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLDivElement).style.background =
                'var(--color-primary)';
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLDivElement).style.background = 'transparent';
            }}
          />
        </aside>

        {/* ── Main content ──────────────────────────────────── */}
        <main
          style={{
            flex:          1,
            display:       'flex',
            flexDirection: 'row',
            overflow:      'hidden',
            minWidth:      0,
          }}
        >
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, overflow: 'hidden' }}>
            <DiagramCanvas graph={graph} />
          </div>

          {selectedElement && <DetailPanel />}
        </main>
      </div>
    </div>
  );
}
