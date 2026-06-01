import React from 'react';
import { X, ExternalLink } from 'lucide-react';
import type { SelectedElement, NodeData, EdgeData } from '../types/trace';
import { useTraceStore } from '../store/traceStore';

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
          </tbody>
        </table>
      </Section>
      {data.netbox_url && (
        <a
          href={data.netbox_url}
          target="_blank"
          rel="noopener noreferrer"
          className="netbox-link"
        >
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
  const hasErrors =
    (data.runts ?? 0) > 0 ||
    (data.crc ?? 0) > 0 ||
    (data.input_error ?? 0) > 0 ||
    (data.output_error ?? 0) > 0 ||
    (data.total_output_drops ?? 0) > 0;

  return (
    <>
      <Section title="Link">
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <tbody>
            <Row label="Layer"         value={data.layer} />
            <Row label="VLAN"          value={data.vlan} />
            <Row label="State"         value={data.state} />
          </tbody>
        </table>
      </Section>

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
      </Section>

      <Section title="Destination">
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <tbody>
            <Row label="Device"    value={data.dst_device} />
            <Row label="Interface" value={data.dst_interface} />
          </tbody>
        </table>
        {data.dst_interface_netbox_url && (
          <a href={data.dst_interface_netbox_url} target="_blank" rel="noopener noreferrer" className="netbox-link">
            <ExternalLink size={12} /> Destination interface in NetBox
          </a>
        )}
      </Section>

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
    </>
  );
}

// ---------------------------------------------------------------------------
// DetailPanel
// ---------------------------------------------------------------------------

export default function DetailPanel() {
  const selectedElement = useTraceStore((s) => s.selectedElement);
  const setSelectedElement = useTraceStore((s) => s.setSelectedElement);

  if (!selectedElement) return null;

  const title =
    selectedElement.type === 'node'
      ? selectedElement.data.label
      : `${selectedElement.data.src_device ?? '?'} → ${selectedElement.data.dst_device ?? '?'}`;

  return (
    <div
      style={{
        width: '280px',
        flexShrink: 0,
        borderLeft: '1px solid var(--border-color)',
        background: 'var(--bg-panel)',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: '12px 14px',
          borderBottom: '1px solid var(--border-color)',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
        }}
      >
        <div
          style={{
            flex: 1,
            fontSize: '13px',
            fontWeight: 700,
            color: 'var(--text-primary)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
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
