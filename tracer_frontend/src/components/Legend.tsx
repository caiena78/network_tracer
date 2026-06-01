import React from 'react';
import { L2_COLOR, L3_COLOR } from '../transform/graphTransform';

export default function Legend() {
  return (
    <div
      style={{
        background: 'var(--bg-panel)',
        border: '1px solid var(--border-color)',
        borderRadius: '6px',
        padding: '8px 12px',
        display: 'flex',
        flexDirection: 'column',
        gap: '5px',
        pointerEvents: 'auto',
        opacity: 0.92,
      }}
    >
      <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.08em', color: 'var(--text-muted)', textTransform: 'uppercase', marginBottom: '2px' }}>
        Legend
      </div>
      <LegendItem color={L2_COLOR} label="Layer 2 path" />
      <LegendItem color={L3_COLOR} label="Layer 3 path" />
    </div>
  );
}

function LegendItem({ color, label }: { color: string; label: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
      <svg width="28" height="12">
        <line x1="0" y1="6" x2="28" y2="6" stroke={color} strokeWidth="2.5" strokeLinecap="round" />
        <polygon points="22,2 28,6 22,10" fill={color} />
      </svg>
      <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{label}</span>
    </div>
  );
}
