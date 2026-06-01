import React, { useCallback, useState } from 'react';
import { useReactFlow } from 'reactflow';
import { FileImage, FileCode } from 'lucide-react';
import { exportToPng, exportToSvg } from '../utils/export';
import type { GraphResponse } from '../types/trace';

interface ExportControlsProps {
  containerRef: React.RefObject<HTMLDivElement>;
  graph: GraphResponse;
}

export default function ExportControls({ containerRef, graph }: ExportControlsProps) {
  const reactFlow = useReactFlow();
  const [exporting, setExporting] = useState<'png' | 'svg' | null>(null);

  const slug = `trace-${graph.metadata.src_ip}-${graph.metadata.dst_ip}`;

  const handlePng = useCallback(async () => {
    const el = containerRef.current;
    if (!el) return;
    setExporting('png');
    try {
      await exportToPng(el, `${slug}.png`);
    } finally {
      setExporting(null);
    }
  }, [containerRef, slug]);

  const handleSvg = useCallback(() => {
    const nodes = reactFlow.getNodes();
    const edges = reactFlow.getEdges();
    exportToSvg(nodes, edges, `${slug}.svg`);
  }, [reactFlow, slug]);

  return (
    <div
      style={{
        display: 'flex',
        gap: '6px',
        background: 'var(--bg-panel)',
        border: '1px solid var(--border-color)',
        borderRadius: '6px',
        padding: '4px 6px',
        opacity: 0.92,
      }}
    >
      <button
        className="btn btn-ghost"
        onClick={handlePng}
        disabled={exporting !== null}
        title="Export as PNG"
        style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '12px', padding: '4px 8px' }}
      >
        <FileImage size={13} />
        {exporting === 'png' ? '…' : 'PNG'}
      </button>
      <button
        className="btn btn-ghost"
        onClick={handleSvg}
        disabled={exporting !== null}
        title="Export as SVG"
        style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '12px', padding: '4px 8px' }}
      >
        <FileCode size={13} />
        SVG
      </button>
    </div>
  );
}
