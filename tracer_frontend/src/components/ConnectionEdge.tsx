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
  const check = (f: string) => ((data as Record<string, unknown>)[f] as number ?? 0) > 0;
  return (
    check('src_crc') || check('dst_crc') ||
    check('src_input_error') || check('dst_input_error') ||
    check('src_runts') || check('dst_runts') ||
    check('src_giants') || check('dst_giants') ||
    check('src_output_error') || check('dst_output_error') ||
    check('src_total_output_drops') || check('dst_total_output_drops') ||
    // legacy flat fields (topology-phase data before enrichment arrives)
    check('crc') || check('input_error') || check('runts') ||
    check('giants') || check('output_error') || check('total_output_drops')
  );
}

/** Resolve a counter value — prefer the per-side prefixed field, fall back to unprefixed. */
function counter(data: EdgeData, side: 'src' | 'dst', field: string): number | undefined {
  const prefixed = (data as Record<string, unknown>)[`${side}_${field}`];
  if (prefixed != null) return prefixed as number;
  const flat = (data as Record<string, unknown>)[field];
  if (flat != null) return flat as number;
  return undefined;
}

/** True if the given side has any non-zero error counter. */
function sideHasErrors(data: EdgeData, side: 'src' | 'dst'): boolean {
  return ['crc', 'input_error', 'runts', 'giants', 'output_error', 'total_output_drops']
    .some((f) => (counter(data, side, f) ?? 0) > 0);
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
  const [fetching, setFetching] = useState<string | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);

  // Read from and write to the persistent store cache
  const cache            = useTraceStore((s) => s.interfaceDetailCache);
  const cacheResult      = useTraceStore((s) => s.cacheInterfaceDetail);

  const handleFetch = useCallback(async (device: string, iface: string) => {
    const ip = deviceIpMap[device];
    if (!ip || !iface) return;
    const key = `${device}/${iface}`;
    setFetching(key);
    setFetchError(null);
    try {
      const result = await fetchInterfaceDetail(ip, iface);
      // Persist in store so tooltip and sidebar share the same data
      cacheResult(device, iface, result);
      // Also push into pending enrichments so the edge chip updates
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
  }, [deviceIpMap, cacheResult]);

  // Prefer store cache over enrichment-phase raw output
  const srcKey = `${data.src_device}/${data.src_interface}`;
  const dstKey = `${data.dst_device}/${data.dst_interface}`;
  const displayData: EdgeData = {
    ...data,
    src_raw_output: cache[srcKey]?.raw_output ?? data.src_raw_output,
    dst_raw_output: cache[dstKey]?.raw_output ?? data.dst_raw_output,
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
            { dev: data.dst_device, iface: data.dst_interface },
            { dev: data.src_device, iface: data.src_interface },
          ] as Array<{ dev?: string; iface?: string | null }>)
            .filter((s) => s.dev && s.iface && deviceIpMap[s.dev!])
            .map((s) => {
              const key      = `${s.dev}/${s.iface}`;
              const busy     = fetching === key;
              const hasCache = !!cache[key];
              const btnLabel = busy
                ? 'Fetching live data…'
                : hasCache
                ? `Refresh — ${s.dev} ${s.iface}`
                : `Get interface details — ${s.dev} ${s.iface}`;
              return (
                <button
                  key={key}
                  onClick={() => void handleFetch(s.dev!, s.iface!)}
                  disabled={!!fetching}
                  className="btn btn-secondary"
                  style={{ fontSize: '11px', padding: '4px 8px', display: 'flex', alignItems: 'center', gap: '5px', justifyContent: 'center' }}
                >
                  <RefreshCw size={11} className={busy ? 'spin' : ''} />
                  {btnLabel}
                </button>
              );
            })}
        </div>
      </div>
    </div>
  );
}

// ── Routing section (shared between tooltip and sidebar) ────────────────────

export function RoutingSection({ data }: { data: EdgeData }) {
  if (data.layer !== 'L3' && data.layer !== 'mixed') return null;
  if (!data.route_source && !data.prefix) return null;

  const isBgp = (data.route_source ?? '').toLowerCase().includes('bgp');

  return (
    <Section title="Routing">
      <Row label="Protocol"    value={data.route_source} />
      <Row label="Prefix"      value={data.prefix} />
      <Row label="Next hop"    value={data.next_hop_ip} />
      <Row label="Egress"      value={data.egress_iface} />
      <Row label="Route age"   value={data.route_age} />
      {isBgp ? (
        <>
          <Row label="AS path"     value={data.bgp_as_path} />
          <Row label="Community"   value={data.bgp_community} />
          <Row label="Local pref"  value={data.bgp_local_pref != null ? String(data.bgp_local_pref) : undefined} />
          <Row label="Origin"      value={data.bgp_origin} />
          <Row label="MED"         value={data.bgp_med  != null ? String(data.bgp_med)  : undefined} />
          <Row label="Weight"      value={data.bgp_weight != null ? String(data.bgp_weight) : undefined} />
        </>
      ) : (
        <Row label="Tag" value={data.route_tag} />
      )}
    </Section>
  );
}

// ── Per-side interface counters (shared between tooltip and sidebar) ─────────

export function InterfaceCounters({
  data, side, title,
}: {
  data:  EdgeData;
  side:  'src' | 'dst';
  title: string;
}) {
  const fields: Array<[string, string]> = [
    ['runts',               'Runts'],
    ['giants',              'Giants'],
    ['crc',                 'CRC errors'],
    ['input_error',         'Input errors'],
    ['total_output_drops',  'Output drops'],
    ['output_error',        'Output errors'],
    ['output_discard',      'Output discards'],
    ['unknown_protocol_drops', 'Unknown proto drops'],
  ];

  const rows = fields
    .map(([f, label]) => ({ label, value: counter(data, side, f) }))
    .filter((r) => r.value != null);

  if (rows.length === 0) return null;

  return (
    <Section title={title}>
      {rows.map(({ label, value }) => (
        <Row
          key={label}
          label={label}
          value={String(value)}
          warn={(value ?? 0) > 0}
        />
      ))}
    </Section>
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

      <RoutingSection data={data} />

      <Section title="Layer">
        <Row label="Layer" value={data.layer} />
      </Section>

      <InterfaceCounters data={data} side="src"
        title={`Egress counters — ${data.src_device ?? ''} ${data.src_interface ?? ''}`} />
      <InterfaceCounters data={data} side="dst"
        title={`Ingress counters — ${data.dst_device ?? ''} ${data.dst_interface ?? ''}`} />
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
  const [showTooltip, setShowTooltip] = useState(false);
  const hideTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const scheduleHide = () => {
    hideTimer.current = setTimeout(() => setShowTooltip(false), 120);
  };
  const cancelHide = () => {
    if (hideTimer.current) {
      clearTimeout(hideTimer.current);
      hideTimer.current = null;
    }
  };

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

  const active      = showTooltip || !!selected;
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
        onMouseEnter={() => { cancelHide(); setShowTooltip(true); }}
        onMouseLeave={scheduleHide}
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
        onMouseEnter={() => { cancelHide(); setShowTooltip(true); }}
        onMouseLeave={scheduleHide}
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
            onMouseEnter={cancelHide}
            onMouseLeave={scheduleHide}
          />
        )}
      </EdgeLabelRenderer>
    </>
  );
}

export default React.memo(ConnectionEdge);
