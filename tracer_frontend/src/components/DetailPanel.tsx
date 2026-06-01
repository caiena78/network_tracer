import React, { useState } from 'react';
import { X, ExternalLink, RefreshCw } from 'lucide-react';
import type { InterfaceDetailResult, SelectedElement, NodeData, EdgeData } from '../types/trace';
import { useTraceStore } from '../store/traceStore';
import { fetchInterfaceDetail } from '../api/client';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function Row({ label, value, warn }: { label: string; value: string | number | null | undefined; warn?: boolean }) {
  if (value == null || value === '') return null;
  return (
    <tr>
      <td style={{ padding: '3px 8px 3px 0', color: 'var(--text-muted)', fontSize: '12px', whiteSpace: 'nowrap', verticalAlign: 'top' }}>
        {label}
      </td>
      <td style={{ padding: '3px 0', color: warn ? 'var(--color-warning)' : 'var(--text-primary)', fontSize: '12px', fontWeight: warn ? 600 : 400, wordBreak: 'break-all' }}>
        {String(value)}
      </td>
    </tr>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: '14px' }}>
      <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.08em', color: 'var(--text-muted)', textTransform: 'uppercase', marginBottom: '6px' }}>
        {title}
      </div>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// On-demand fetch button
// ---------------------------------------------------------------------------

function FetchButton({
  device,
  iface,
  deviceIpMap,
  onResult,
}: {
  device:       string;
  iface:        string;
  deviceIpMap:  Record<string, string>;
  onResult:     (r: InterfaceDetailResult) => void;
}) {
  const ip = deviceIpMap[device];
  const [busy,  setBusy]  = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!ip) return null;

  const handleClick = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await fetchInterfaceDetail(ip, iface);
      onResult(result);
      // Push into pending enrichments so the diagram chip updates too
      useTraceStore.setState((s) => ({
        pendingEnrichments: [
          ...s.pendingEnrichments,
          { device, interface: iface, data: { ...result.parsed, raw_output: result.raw_output } },
        ],
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Fetch failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ marginTop: '6px' }}>
      <button
        onClick={() => void handleClick()}
        disabled={busy}
        className="btn btn-secondary"
        style={{ fontSize: '12px', padding: '5px 10px', display: 'flex', alignItems: 'center', gap: '6px', width: '100%', justifyContent: 'center' }}
      >
        <RefreshCw size={12} className={busy ? 'spin' : ''} />
        {busy ? 'Fetching…' : `Get interface details — ${device} ${iface}`}
      </button>
      {error && (
        <div style={{ fontSize: '11px', color: 'var(--color-error)', marginTop: '4px' }}>{error}</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Raw output block
// ---------------------------------------------------------------------------

function RawBlock({ title, text }: { title: string; text: string }) {
  return (
    <div style={{ marginBottom: '10px' }}>
      {title && (
        <div style={{ fontSize: '10px', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '4px' }}>
          {title}
        </div>
      )}
      <pre
        style={{
          margin:       0,
          padding:      '6px 8px',
          background:   'var(--bg-code)',
          borderRadius: '4px',
          fontSize:     '10px',
          fontFamily:   'monospace',
          color:        'var(--text-primary)',
          whiteSpace:   'pre',
          overflowX:    'auto',
          maxHeight:    '260px',
          overflowY:    'auto',
        }}
      >
        {text}
      </pre>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Node detail
// ---------------------------------------------------------------------------

function NodeDetail({ data }: { data: NodeData }) {
  return (
    <>
      <Section title="Device">
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <tbody>
            <Row label="Name"        value={data.label} />
            <Row label="Type"        value={data.node_type} />
            <Row label="Layer"       value={data.layer} />
            <Row label="IP"          value={data.ip} />
            <Row label="Description" value={data.description} />
            <Row label="State"       value={data.state} />
            <Row label="Speed"       value={data.speed} />
            <Row label="Duplex"      value={data.duplex} />
            <Row label="Version"     value={data.os_version as string | undefined} />
            <Row label="Uptime"      value={data.uptime as string | undefined} />
          </tbody>
        </table>
      </Section>
      {data.netbox_url && (
        <a href={data.netbox_url as string} target="_blank" rel="noopener noreferrer" className="netbox-link">
          <ExternalLink size={12} />
          View device in NetBox
        </a>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Edge detail
// ---------------------------------------------------------------------------

function EdgeDetail({ data }: { data: EdgeData }) {
  const deviceIpMap = useTraceStore((s) => s.graph?.metadata?.device_ip_map ?? {});

  // Live-fetched raw output overrides enrichment output
  const [liveOutputs, setLiveOutputs] = useState<Record<string, string>>({});

  const srcRaw = liveOutputs[`${data.src_device}/${data.src_interface}`]
    ?? data.src_raw_output;
  const dstRaw = liveOutputs[`${data.dst_device}/${data.dst_interface}`]
    ?? data.dst_raw_output;

  const handleResult = (side: 'src' | 'dst') => (r: InterfaceDetailResult) => {
    const key = `${side === 'src' ? data.src_device : data.dst_device}/${side === 'src' ? data.src_interface : data.dst_interface}`;
    setLiveOutputs((prev) => ({ ...prev, [key]: r.raw_output }));
  };

  const hasErrors =
    (data.runts ?? 0) > 0 ||
    (data.crc ?? 0) > 0 ||
    (data.input_error ?? 0) > 0 ||
    (data.output_error ?? 0) > 0 ||
    (data.total_output_drops ?? 0) > 0;

  return (
    <>
      {/* Source side */}
      {(data.src_device || data.src_interface) && (
        <Section title="Source">
          <table style={{ borderCollapse: 'collapse', width: '100%' }}>
            <tbody>
              <Row label="Device"    value={data.src_device} />
              <Row label="Interface" value={data.src_interface} />
            </tbody>
          </table>
          {data.src_interface_netbox_url && (
            <a href={data.src_interface_netbox_url} target="_blank" rel="noopener noreferrer" className="netbox-link">
              <ExternalLink size={12} /> Source interface in NetBox
            </a>
          )}
          {data.src_device && data.src_interface && (
            <FetchButton
              device={data.src_device}
              iface={data.src_interface}
              deviceIpMap={deviceIpMap}
              onResult={handleResult('src')}
            />
          )}
        </Section>
      )}

      {/* Destination side */}
      <Section title="Switch Port">
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <tbody>
            <Row label="Device"      value={data.dst_device} />
            <Row label="Interface"   value={data.dst_interface} />
            <Row label="Description" value={data.description as string | undefined} />
            <Row label="Speed"       value={data.speed as string | undefined} />
            <Row label="Duplex"      value={data.duplex as string | undefined} />
            <Row label="VLAN"        value={data.vlan != null ? String(data.vlan) : undefined} />
            <Row label="State"       value={data.state as string | undefined} />
          </tbody>
        </table>
        {data.dst_interface_netbox_url && (
          <a href={data.dst_interface_netbox_url} target="_blank" rel="noopener noreferrer" className="netbox-link">
            <ExternalLink size={12} /> Destination interface in NetBox
          </a>
        )}
        {data.dst_device && data.dst_interface && (
          <FetchButton
            device={data.dst_device}
            iface={data.dst_interface}
            deviceIpMap={deviceIpMap}
            onResult={handleResult('dst')}
          />
        )}
      </Section>

      {/* Link */}
      <Section title="Link">
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <tbody>
            <Row label="Layer" value={data.layer} />
          </tbody>
        </table>
      </Section>

      {/* Error counters */}
      {hasErrors && (
        <Section title="Interface Errors">
          <table style={{ borderCollapse: 'collapse', width: '100%' }}>
            <tbody>
              <Row label="Runts"        value={data.runts}              warn={(data.runts ?? 0) > 0} />
              <Row label="Giants"       value={data.giants}             warn={(data.giants ?? 0) > 0} />
              <Row label="CRC"          value={data.crc}                warn={(data.crc ?? 0) > 0} />
              <Row label="Input errors" value={data.input_error}        warn={(data.input_error ?? 0) > 0} />
              <Row label="Output drops" value={data.total_output_drops} warn={(data.total_output_drops ?? 0) > 0} />
              <Row label="Output errors"value={data.output_error}       warn={(data.output_error ?? 0) > 0} />
            </tbody>
          </table>
        </Section>
      )}

      {/* Raw show interface output */}
      {(srcRaw || dstRaw) && (
        <Section title="Show Interface">
          {srcRaw && data.src_interface && (
            <RawBlock
              title={dstRaw ? `${data.src_device} / ${data.src_interface}` : ''}
              text={srcRaw}
            />
          )}
          {dstRaw && data.dst_interface && (
            <RawBlock
              title={srcRaw ? `${data.dst_device} / ${data.dst_interface}` : ''}
              text={dstRaw}
            />
          )}
        </Section>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// DetailPanel
// ---------------------------------------------------------------------------

export default function DetailPanel() {
  const selectedElement    = useTraceStore((s) => s.selectedElement);
  const setSelectedElement = useTraceStore((s) => s.setSelectedElement);

  if (!selectedElement) return null;

  const title =
    selectedElement.type === 'node'
      ? selectedElement.data.label
      : `${(selectedElement.data as EdgeData).src_device ?? '?'} → ${(selectedElement.data as EdgeData).dst_device ?? '?'}`;

  return (
    <div
      style={{
        width:       '300px',
        flexShrink:   0,
        borderLeft:  '1px solid var(--border-color)',
        background:  'var(--bg-panel)',
        display:     'flex',
        flexDirection:'column',
        overflow:    'hidden',
      }}
    >
      {/* Header */}
      <div
        style={{
          padding:       '12px 14px',
          borderBottom:  '1px solid var(--border-color)',
          display:       'flex',
          alignItems:    'center',
          gap:           '8px',
        }}
      >
        <div
          style={{
            flex:           1,
            fontSize:       '13px',
            fontWeight:     700,
            color:          'var(--text-primary)',
            overflow:       'hidden',
            textOverflow:   'ellipsis',
            whiteSpace:     'nowrap',
          }}
        >
          {title}
        </div>
        <button
          onClick={() => setSelectedElement(null)}
          className="btn btn-ghost"
          style={{ padding: '3px', flexShrink: 0 }}
          title="Close"
        >
          <X size={14} />
        </button>
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '14px' }}>
        {selectedElement.type === 'node' ? (
          <NodeDetail data={selectedElement.data as NodeData} />
        ) : (
          <EdgeDetail data={selectedElement.data as EdgeData} />
        )}
      </div>
    </div>
  );
}
