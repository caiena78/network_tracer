import React, { useState, useCallback } from 'react';
import {
  EdgeLabelRenderer,
  getBezierPath,
} from 'reactflow';
import type { EdgeProps } from 'reactflow';
import { ExternalLink, AlertTriangle, RefreshCw } from 'lucide-react';
import type { EdgeData, InterfaceDetailResult } from '../types/trace';
import { edgeColor } from '../transform/graphTransform';
import { useTraceStore } from '../store/traceStore';
import { fetchInterfaceDetail } from '../api/client';

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

/**
 * Convert a raw speed string to a short display label, unit-aware.
 *
 * The backend may return strings like "1000Mb/s", "10 Gb/s", "100 Gb/s",
 * "1000000 Kbps", etc.  We extract the numeric part AND the unit (G/M/K),
 * normalise to Mbps, then format.
 */
function shortSpeed(speed: string | undefined): string {
  if (!speed) return '';
  const lower = speed.toLowerCase();
  const n = parseFloat(speed.replace(/[^0-9.]/g, ''));
  if (isNaN(n) || n === 0) return speed;

  // Normalise to Mbps
  let mbps: number;
  if (lower.includes('gb') || /\d\s*g[^i]/.test(lower)) {
    mbps = n * 1_000;       // Gbps → Mbps
  } else if (lower.includes('kb') || /\d\s*k/.test(lower)) {
    mbps = n / 1_000;       // Kbps → Mbps
  } else {
    mbps = n;               // already Mbps
  }

  if (mbps >= 100_000) return '100G';
  if (mbps >= 40_000)  return '40G';
  if (mbps >= 25_000)  return '25G';
  if (mbps >= 10_000)  return '10G';
  if (mbps >= 1_000)   return '1G';
  if (mbps >= 100)     return '100M';
  if (mbps >= 10)      return '10M';
  if (mbps >= 1)       return '1M';
  return speed;
}

// ---------------------------------------------------------------------------
// Compact always-visible chip
// ---------------------------------------------------------------------------

function EdgeChip({ data, color, selected }: { data: EdgeData; color: string; selected: boolean }) {
  const errors = hasErrors(data);
  // Use the full "src_iface → dst_iface" label built by graph_builder;
  // fall back to whichever single interface is available.
  const iface  = data.label || data.src_interface || data.dst_interface || '';
  const speed  = shortSpeed(data.speed as string | undefined);

  return (
    <div
      style={{
        background:    'var(--bg-panel)',
        border:        `1px solid ${selected ? color : 'var(--border-color)'}`,
        borderRadius:  '4px',
        padding:       '2px 6px',
        display:       'flex',
        alignItems:    'center',
        gap:           '4px',
        fontSize:      '10px',
        fontFamily:    'monospace',
        color:         'var(--text-secondary)',
        whiteSpace:    'nowrap',
        boxShadow:     '0 1px 3px rgba(0,0,0,0.15)',
        pointerEvents: 'none',
        userSelect:    'none',
      }}
    >
      {errors && <AlertTriangle size={10} style={{ color: 'var(--color-warning)', flexShrink: 0 }} />}
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
// Tabbed tooltip
// ---------------------------------------------------------------------------

type TooltipTab = 'details' | 'raw';

interface EdgeTooltipProps {
  data:          EdgeData;
  x:             number;
  y:             number;
  deviceIpMap:   Record<string, string>;
  onMouseEnter:  () => void;
  onMouseLeave:  () => void;
}

function EdgeTooltip({ data, x, y, deviceIpMap, onMouseEnter, onMouseLeave }: EdgeTooltipProps) {
  const [tab, setTab]           = useState<TooltipTab>('details');
  const [fetching, setFetching] = useState<string | null>(null); // device/iface being fetched
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [liveResults, setLiveResults] = useState<Record<string, InterfaceDetailResult>>({});
  const addEnrichment = useTraceStore((s) => s.pendingEnrichments);
  const setPending    = useTraceStore.getState;

  const handleFetch = useCallback(async (device: string, iface: string) => {
    const ip = deviceIpMap[device];
    if (!ip || !iface) return;
    const key = `${device}/${iface}`;
    setFetching(key);
    setFetchError(null);
    try {
      const result = await fetchInterfaceDetail(ip, iface);
      setLiveResults((prev) => ({ ...prev, [key]: result }));
      // Also push into pending enrichments so the edge chip + graph update
      useTraceStore.setState((s) => ({
        pendingEnrichments: [
          ...s.pendingEnrichments,
          { device, interface: iface, data: { ...result.parsed, raw_output: result.raw_output } },
        ],
      }));
      setTab('raw');
    } catch (err) {
      setFetchError(err instanceof Error ? err.message : 'Fetch failed');
    } finally {
      setFetching(null);
    }
  }, [deviceIpMap]);

  // Merge live results into displayed raw output
  const srcKey = `${data.src_device}/${data.src_interface}`;
  const dstKey = `${data.dst_device}/${data.dst_interface}`;
  const displayData: EdgeData = {
    ...data,
    src_raw_output: liveResults[srcKey]?.raw_output ?? data.src_raw_output,
    dst_raw_output: liveResults[dstKey]?.raw_output ?? data.dst_raw_output,
  };

  const hasRaw = !!(displayData.src_raw_output || displayData.dst_raw_output);

  return (
    <div
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      style={{
        position:     'absolute',
        left:          x,
        top:           y,
        transform:    'translate(-50%, -115%)',
        zIndex:        9999,
        background:   'var(--bg-tooltip)',
        border:       '1px solid var(--border-color)',
        borderRadius: '6px',
        minWidth:     '260px',
        maxWidth:     '400px',
        boxShadow:    '0 4px 16px rgba(0,0,0,0.3)',
        pointerEvents:'auto',
        fontSize:     '12px',
        color:        'var(--text-primary)',
        overflow:     'hidden',
      }}
    >
      {/* Tab bar */}
      <div style={{ display: 'flex', borderBottom: '1px solid var(--border-color)', background: 'var(--bg-code)' }}>
        {(['details', 'raw'] as TooltipTab[]).map((t) => {
          if (t === 'raw' && !hasRaw) return null;
          const label = t === 'details' ? 'Interface Details' : 'Show Interface';
          const active = tab === t;
          return (
            <button
              key={t}
              onClick={() => setTab(t)}
              style={{
                padding:     '6px 12px',
                fontSize:    '11px',
                fontWeight:   active ? 700 : 400,
                color:        active ? 'var(--color-primary)' : 'var(--text-muted)',
                background:  'transparent',
                border:      'none',
                borderBottom: active ? `2px solid var(--color-primary)` : '2px solid transparent',
                cursor:      'pointer',
                whiteSpace:  'nowrap',
              }}
            >
              {label}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      <div style={{ padding: '10px 12px' }}>
        {tab === 'details' ? (
          <DetailsTab data={data} />
        ) : (
          <RawTab data={displayData} />
        )}

        {/* Fetch buttons */}
        <div style={{ marginTop: '10px', borderTop: '1px solid var(--border-color)', paddingTop: '8px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
          {fetchError && (
            <div style={{ fontSize: '11px', color: 'var(--color-error)', marginBottom: '4px' }}>{fetchError}</div>
          )}
          {([
            { dev: data.dst_device, iface: data.dst_interface, label: 'dst' },
            { dev: data.src_device, iface: data.src_interface, label: 'src' },
          ] as Array<{ dev?: string; iface?: string | null; label: string }>)
            .filter((s) => s.dev && s.iface && deviceIpMap[s.dev!])
            .map((s) => {
              const key = `${s.dev}/${s.iface}`;
              const busy = fetching === key;
              return (
                <button
                  key={key}
                  onClick={() => void handleFetch(s.dev!, s.iface!)}
                  disabled={!!fetching}
                  className="btn btn-secondary"
                  style={{ fontSize: '11px', padding: '4px 8px', display: 'flex', alignItems: 'center', gap: '5px', justifyContent: 'center' }}
                >
                  <RefreshCw size={11} className={busy ? 'spin' : ''} />
                  {busy ? 'Fetching…' : `Get interface details — ${s.dev} ${s.iface}`}
                </button>
              );
            })}
        </div>
      </div>
    </div>
  );
}

// ── Details tab ─────────────────────────────────────────────────────────────

function DetailsTab({ data }: { data: EdgeData }) {
  return (
    <>
      {(data.src_device || data.src_interface) && (
        <Section title="Source">
          <Row label="Device"    value={data.src_device} />
          <Row label="Interface" value={data.src_interface} />
          {data.src_interface_netbox_url && (
            <NBLink href={data.src_interface_netbox_url} label="Open in NetBox" />
          )}
        </Section>
      )}

      <Section title="Switch Port">
        <Row label="Device"      value={data.dst_device} />
        <Row label="Interface"   value={data.dst_interface} />
        <Row label="Description" value={data.description as string | undefined} />
        <Row label="Speed"       value={data.speed as string | undefined} />
        <Row label="Duplex"      value={data.duplex  as string | undefined} />
        <Row label="VLAN"        value={data.vlan != null ? String(data.vlan) : undefined} />
        <Row label="State"       value={data.state   as string | undefined} />
        {data.dst_interface_netbox_url && (
          <NBLink href={data.dst_interface_netbox_url} label="Open in NetBox" />
        )}
      </Section>

      <Section title="Layer">
        <Row label="Layer" value={data.layer} />
      </Section>

      <Section title="Interface Counters">
        <Row label="CRC errors"    value={data.crc          != null ? String(data.crc)          : undefined} warn={(data.crc          ?? 0) > 0} />
        <Row label="Input errors"  value={data.input_error  != null ? String(data.input_error)  : undefined} warn={(data.input_error  ?? 0) > 0} />
        <Row label="Runts"         value={data.runts         != null ? String(data.runts)        : undefined} warn={(data.runts         ?? 0) > 0} />
        <Row label="Giants"        value={data.giants        != null ? String(data.giants)       : undefined} warn={(data.giants        ?? 0) > 0} />
        <Row label="Output errors" value={data.output_error != null ? String(data.output_error) : undefined} warn={(data.output_error ?? 0) > 0} />
        <Row label="Output drops"  value={data.total_output_drops != null ? String(data.total_output_drops) : undefined} warn={(data.total_output_drops ?? 0) > 0} />
        <Row label="Unknown drops" value={(data as Record<string, unknown>).unknown_protocol_drops != null ? String((data as Record<string, unknown>).unknown_protocol_drops) : undefined}
             warn={((data as Record<string, unknown>).unknown_protocol_drops as number ?? 0) > 0} />
      </Section>
    </>
  );
}

// ── Raw output tab ───────────────────────────────────────────────────────────

function RawTab({ data }: { data: EdgeData }) {
  const blocks: Array<{ label: string; text: string }> = [];
  if (data.src_raw_output) {
    blocks.push({
      label: data.src_interface
        ? `${data.src_device ?? ''} / ${data.src_interface}`
        : (data.src_device ?? 'Source'),
      text: data.src_raw_output,
    });
  }
  if (data.dst_raw_output) {
    blocks.push({
      label: data.dst_interface
        ? `${data.dst_device ?? ''} / ${data.dst_interface}`
        : (data.dst_device ?? 'Destination'),
      text: data.dst_raw_output,
    });
  }

  if (blocks.length === 0) {
    return (
      <div style={{ color: 'var(--text-muted)', fontSize: '12px' }}>
        No raw output yet — enrichment may still be running.
      </div>
    );
  }

  return (
    <>
      {blocks.map((b, i) => (
        <div key={i} style={{ marginBottom: i < blocks.length - 1 ? '12px' : 0 }}>
          {blocks.length > 1 && (
            <div style={{ fontSize: '10px', fontWeight: 700, color: 'var(--text-muted)', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '4px' }}>
              {b.label}
            </div>
          )}
          <pre
            style={{
              margin:      0,
              padding:     '6px 8px',
              background:  'var(--bg-code)',
              borderRadius:'4px',
              fontSize:    '10px',
              fontFamily:  'monospace',
              color:       'var(--text-primary)',
              whiteSpace:  'pre',
              overflowX:   'auto',
              maxHeight:   '280px',
              overflowY:   'auto',
            }}
          >
            {b.text}
          </pre>
        </div>
      ))}
    </>
  );
}

// ── Shared helpers ───────────────────────────────────────────────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  const kids = React.Children.toArray(children).filter(Boolean);
  if (kids.length === 0) return null;
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
  const [edgeHovered,    setEdgeHovered]    = useState(false);
  const [tooltipHovered, setTooltipHovered] = useState(false);
  const showTooltip = edgeHovered || tooltipHovered;

  const setSelectedElement = useTraceStore((s) => s.setSelectedElement);
  const deviceIpMap = useTraceStore((s) => s.graph?.metadata?.device_ip_map ?? {});

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

  const active      = edgeHovered || !!selected;
  const strokeWidth = active ? 3 : (style?.strokeWidth as number) ?? 2;
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
        onMouseEnter={() => setEdgeHovered(true)}
        onMouseLeave={() => setEdgeHovered(false)}
        onClick={handleClick}
      />

      {/* Visible edge */}
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
          cursor:     'pointer',
          filter:      active ? `drop-shadow(0 0 4px ${color})` : undefined,
          transition: 'stroke-width 0.1s, stroke 0.15s',
        }}
        onMouseEnter={() => setEdgeHovered(true)}
        onMouseLeave={() => setEdgeHovered(false)}
        onClick={handleClick}
      />

      <EdgeLabelRenderer>
        {/* Always-visible compact chip */}
        <div
          style={{ position: 'absolute', left: labelX, top: labelY, transform: 'translate(-50%, -50%)', pointerEvents: 'none' }}
          className="nodrag nopan"
        >
          <EdgeChip data={edgeData} color={color} selected={!!selected} />
        </div>

        {/* Tabbed hover tooltip — stays open when cursor moves into it */}
        {showTooltip && (
          <EdgeTooltip
            data={edgeData}
            x={labelX}
            y={labelY}
            deviceIpMap={deviceIpMap}
            onMouseEnter={() => setTooltipHovered(true)}
            onMouseLeave={() => setTooltipHovered(false)}
          />
        )}
      </EdgeLabelRenderer>
    </>
  );
}

export default React.memo(ConnectionEdge);
