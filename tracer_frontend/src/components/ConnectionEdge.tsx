import React, { useState, useCallback } from 'react';
import {
  EdgeLabelRenderer,
  getBezierPath,
} from 'reactflow';
import type { EdgeProps } from 'reactflow';
import { ExternalLink, AlertTriangle } from 'lucide-react';
import type { EdgeData } from '../types/trace';
import { edgeColor } from '../transform/graphTransform';
import { useTraceStore } from '../store/traceStore';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function hasErrors(data: EdgeData): boolean {
  return (
    (data.crc          ?? 0) > 0 ||
    (data.input_error  ?? 0) > 0 ||
    (data.runts        ?? 0) > 0 ||
    (data.giants       ?? 0) > 0 ||
    (data.output_error ?? 0) > 0 ||
    (data.total_output_drops ?? 0) > 0
  );
}

/** Speed string normalised to short form: "1000Mb/s" → "1G", "10000Mbps" → "10G" */
function shortSpeed(speed: string | undefined): string {
  if (!speed) return '';
  const n = parseFloat(speed.replace(/[^0-9.]/g, ''));
  if (isNaN(n)) return speed;
  if (n >= 100_000) return '100G';
  if (n >= 40_000)  return '40G';
  if (n >= 25_000)  return '25G';
  if (n >= 10_000)  return '10G';
  if (n >= 1_000)   return '1G';
  if (n >= 100)     return '100M';
  return `${n}M`;
}

// ---------------------------------------------------------------------------
// Compact always-visible edge label
// ---------------------------------------------------------------------------

interface EdgeChipProps {
  data:     EdgeData;
  color:    string;
  selected: boolean;
}

function EdgeChip({ data, color, selected }: EdgeChipProps) {
  const errors = hasErrors(data);
  const iface  = data.dst_interface || data.src_interface || '';
  const speed  = shortSpeed(data.speed as string | undefined);

  return (
    <div
      style={{
        background:   'var(--bg-panel)',
        border:       `1px solid ${selected ? color : 'var(--border-color)'}`,
        borderRadius: '4px',
        padding:      '2px 6px',
        display:      'flex',
        alignItems:   'center',
        gap:          '4px',
        fontSize:     '10px',
        fontFamily:   'monospace',
        color:        'var(--text-secondary)',
        whiteSpace:   'nowrap',
        boxShadow:    '0 1px 3px rgba(0,0,0,0.15)',
        pointerEvents:'none',
        userSelect:   'none',
      }}
    >
      {errors && (
        <AlertTriangle
          size={10}
          style={{ color: 'var(--color-warning)', flexShrink: 0 }}
        />
      )}
      <span style={{ color, fontWeight: 600 }}>{iface}</span>
      {speed && (
        <span style={{ color: 'var(--text-muted)', borderLeft: '1px solid var(--border-color)', paddingLeft: '4px' }}>
          {speed}
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Full hover tooltip
// ---------------------------------------------------------------------------

interface EdgeTooltipProps {
  data: EdgeData;
  x:    number;
  y:    number;
}

function EdgeTooltip({ data, x, y }: EdgeTooltipProps) {
  return (
    <div
      style={{
        position:    'absolute',
        left:        x,
        top:         y,
        transform:   'translate(-50%, -115%)',
        zIndex:      9999,
        background:  'var(--bg-tooltip)',
        border:      '1px solid var(--border-color)',
        borderRadius:'6px',
        padding:     '10px 12px',
        minWidth:    '240px',
        maxWidth:    '340px',
        boxShadow:   '0 4px 16px rgba(0,0,0,0.3)',
        pointerEvents:'auto',
        fontSize:    '12px',
        color:       'var(--text-primary)',
      }}
    >
      <div style={{ fontWeight: 700, marginBottom: '8px', fontSize: '13px' }}>
        Interface Details
      </div>

      {/* Source side */}
      {(data.src_device || data.src_interface) && (
        <Section title="Source">
          <Row label="Device"    value={data.src_device} />
          <Row label="Interface" value={data.src_interface} />
          {data.src_interface_netbox_url && (
            <NBLink href={data.src_interface_netbox_url} label="Open in NetBox" />
          )}
        </Section>
      )}

      {/* Destination / switch-port side */}
      <Section title="Switch Port">
        <Row label="Device"      value={data.dst_device} />
        <Row label="Interface"   value={data.dst_interface} />
        <Row label="Description" value={data.description as string | undefined} />
        <Row label="Speed"       value={data.speed as string | undefined} />
        <Row label="Duplex"      value={data.duplex as string | undefined} />
        <Row label="VLAN"        value={data.vlan != null ? String(data.vlan) : undefined} />
        <Row label="State"       value={data.state as string | undefined} />
        {data.dst_interface_netbox_url && (
          <NBLink href={data.dst_interface_netbox_url} label="Open in NetBox" />
        )}
      </Section>

      {/* Layer */}
      <Section title="Link">
        <Row label="Layer" value={data.layer} />
      </Section>

      {/* Error counters — show even if zero so operators can confirm clean */}
      <Section title="Interface Counters">
        <Row label="CRC errors"    value={data.crc          != null ? String(data.crc)          : undefined} warn={(data.crc          ?? 0) > 0} />
        <Row label="Input errors"  value={data.input_error  != null ? String(data.input_error)  : undefined} warn={(data.input_error  ?? 0) > 0} />
        <Row label="Runts"         value={data.runts         != null ? String(data.runts)        : undefined} warn={(data.runts         ?? 0) > 0} />
        <Row label="Giants"        value={data.giants        != null ? String(data.giants)       : undefined} warn={(data.giants        ?? 0) > 0} />
        <Row label="Output errors" value={data.output_error != null ? String(data.output_error) : undefined} warn={(data.output_error ?? 0) > 0} />
        <Row label="Output drops"  value={data.total_output_drops != null ? String(data.total_output_drops) : undefined} warn={(data.total_output_drops ?? 0) > 0} />
        <Row label="Unknown drops" value={(data as any).unknown_protocol_drops != null ? String((data as any).unknown_protocol_drops) : undefined} warn={((data as any).unknown_protocol_drops ?? 0) > 0} />
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  const hasContent = React.Children.toArray(children).some(Boolean);
  if (!hasContent) return null;
  return (
    <div style={{ marginBottom: '8px' }}>
      <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.07em', color: 'var(--text-muted)', textTransform: 'uppercase', marginBottom: '4px' }}>
        {title}
      </div>
      <table style={{ borderCollapse: 'collapse', width: '100%' }}>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

function Row({ label, value, warn }: { label: string; value?: string; warn?: boolean }) {
  if (value == null || value === '' || value === 'null') return null;
  return (
    <tr>
      <td style={{ color: 'var(--text-muted)', paddingRight: '8px', whiteSpace: 'nowrap', fontSize: '11px', paddingBottom: '1px' }}>{label}</td>
      <td style={{ color: warn ? 'var(--color-warning)' : 'var(--text-primary)', fontWeight: warn ? 700 : 400, fontSize: '11px' }}>{value}</td>
    </tr>
  );
}

function NBLink({ href, label }: { href: string; label: string }) {
  return (
    <tr>
      <td colSpan={2} style={{ paddingTop: '4px' }}>
        <a href={href} target="_blank" rel="noopener noreferrer" className="netbox-link" style={{ pointerEvents: 'auto' }}>
          <ExternalLink size={11} /> {label}
        </a>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// ConnectionEdge component
// ---------------------------------------------------------------------------

function ConnectionEdge({
  id,
  sourceX, sourceY,
  targetX, targetY,
  sourcePosition, targetPosition,
  data,
  selected,
  style,
  markerEnd,
}: EdgeProps<EdgeData>) {
  const [hovered, setHovered] = useState(false);
  const setSelectedElement = useTraceStore((s) => s.setSelectedElement);

  const edgeData = data ?? ({} as EdgeData);
  const color    = edgeColor(edgeData.layer ?? 'L2');
  const errors   = hasErrors(edgeData);

  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX, sourceY, sourcePosition,
    targetX, targetY, targetPosition,
  });

  const handleClick = useCallback(() => {
    setSelectedElement({ type: 'edge', data: edgeData });
  }, [edgeData, setSelectedElement]);

  const active      = hovered || !!selected;
  const strokeWidth = active ? 3 : (style?.strokeWidth as number) ?? 2;
  // Edges with errors pulse a warning tint
  const strokeColor = active ? color : errors ? '#d97706' : color;

  return (
    <>
      {/* Wide invisible hit area */}
      <path
        d={edgePath}
        fill="none"
        stroke="transparent"
        strokeWidth={20}
        style={{ cursor: 'pointer' }}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        onClick={handleClick}
      />

      {/* Visible edge line */}
      <path
        id={id}
        className="react-flow__edge-path"
        d={edgePath}
        stroke={strokeColor}
        strokeWidth={strokeWidth}
        fill="none"
        markerEnd={markerEnd}
        style={{
          ...style,
          cursor: 'pointer',
          filter: active ? `drop-shadow(0 0 4px ${color})` : undefined,
          transition: 'stroke-width 0.1s, stroke 0.15s',
        }}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        onClick={handleClick}
      />

      {/* Always-visible compact label at edge midpoint */}
      <EdgeLabelRenderer>
        <div
          style={{
            position:  'absolute',
            left:      labelX,
            top:       labelY,
            transform: 'translate(-50%, -50%)',
            pointerEvents: 'none',
          }}
          className="nodrag nopan"
        >
          <EdgeChip data={edgeData} color={color} selected={!!selected} />
        </div>

        {/* Full hover tooltip */}
        {hovered && (
          <EdgeTooltip data={edgeData} x={labelX} y={labelY} />
        )}
      </EdgeLabelRenderer>
    </>
  );
}

export default React.memo(ConnectionEdge);
