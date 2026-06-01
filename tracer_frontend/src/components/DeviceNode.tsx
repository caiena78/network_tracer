import React, { useState, useCallback } from 'react';
import { Handle, Position } from 'reactflow';
import type { NodeProps } from 'reactflow';
import {
  Network,
  Router,
  Monitor,
  Server,
  HelpCircle,
  ExternalLink,
  Layers,
} from 'lucide-react';
import type { NodeData, StackMember } from '../types/trace';
import { useTraceStore } from '../store/traceStore';

// ---------------------------------------------------------------------------
// Icon selector
// ---------------------------------------------------------------------------

function NodeIcon({ nodeType }: { nodeType: string }) {
  const size = 14;
  switch (nodeType) {
    case 'switch':  return <Network size={size} />;
    case 'router':
    case 'gateway': return <Router  size={size} />;
    case 'src':     return <Monitor size={size} />;
    case 'dst':     return <Server  size={size} />;
    default:        return <HelpCircle size={size} />;
  }
}

// ---------------------------------------------------------------------------
// Layer badge
// ---------------------------------------------------------------------------

function LayerBadge({ layer }: { layer: string }) {
  return (
    <span style={{
      fontSize: '9px', fontWeight: 700, letterSpacing: '0.05em',
      padding: '1px 4px', borderRadius: '3px',
      backgroundColor: layer === 'L3' ? 'var(--l3-color)' : 'var(--l2-color)',
      color: '#fff', marginLeft: '4px', flexShrink: 0,
    }}>
      {layer}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Small label + value row
// ---------------------------------------------------------------------------

function InfoRow({ label, value, highlight }: { label: string; value?: string | null; highlight?: 'ok' | 'warn' }) {
  if (!value) return null;
  const color = highlight === 'ok'   ? 'var(--color-success)'
              : highlight === 'warn' ? 'var(--color-error)'
              : 'var(--text-primary)';
  return (
    <div style={{ display: 'flex', gap: '6px', fontSize: '11px', marginBottom: '2px' }}>
      <span style={{ color: 'var(--text-muted)', flexShrink: 0, minWidth: '52px' }}>{label}</span>
      <span style={{ color, fontWeight: highlight ? 600 : 400, wordBreak: 'break-all' }}>{value}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-stack-member card
// ---------------------------------------------------------------------------

function MemberCard({ mb }: { mb: StackMember }) {
  const isActive = mb.role?.toUpperCase().includes('ACTIVE') ?? false;
  const roleColor = isActive ? 'var(--color-success)' : 'var(--text-muted)';

  return (
    <div style={{
      padding: '6px 8px',
      marginBottom: '5px',
      background: 'var(--bg-code)',
      borderRadius: '4px',
      borderLeft: `3px solid ${isActive ? 'var(--color-success)' : 'var(--border-color)'}`,
    }}>
      {/* Header: "Switch 1 — ACTIVE" */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '4px' }}>
        <span style={{ fontWeight: 700, fontSize: '12px', color: 'var(--text-primary)' }}>
          Switch {mb.switch_num}
        </span>
        {mb.role && (
          <span style={{ fontSize: '10px', fontWeight: 700, color: roleColor, textTransform: 'uppercase' }}>
            {mb.role}
          </span>
        )}
      </div>
      <InfoRow label="Version" value={mb.os_version} />
      <InfoRow label="Uptime"  value={mb.uptime} />
      <InfoRow label="Model"   value={mb.model} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tooltip card (main hover popup)
// ---------------------------------------------------------------------------

function TooltipCard({ data, deviceIp }: { data: NodeData; deviceIp: string | null }) {
  const members = Array.isArray(data.stack_members)
    ? (data.stack_members as StackMember[])
    : [];
  const isStack = members.length > 0;

  // Device status — prefer explicit state field, otherwise infer from node type
  const stateValue = data.state as string | undefined;
  const stateHighlight: 'ok' | 'warn' | undefined =
    stateValue === 'up'   ? 'ok'
  : stateValue === 'down' ? 'warn'
  : undefined;

  // IP: node data first (src/dst endpoints), then management IP from device_ip_map
  const displayIp = (data.ip as string | undefined) || deviceIp;

  return (
    <div style={{
      position:     'absolute',
      bottom:       'calc(100% + 10px)',
      left:         '50%',
      transform:    'translateX(-50%)',
      zIndex:        9999,
      background:   'var(--bg-tooltip)',
      border:       '1px solid var(--border-color)',
      borderRadius: '8px',
      padding:      '12px 14px',
      minWidth:     '240px',
      maxWidth:     '320px',
      boxShadow:    '0 6px 20px rgba(0,0,0,0.3)',
      pointerEvents:'auto',
    }}>
      {/* ── Device name ─────────────────────────────────────────── */}
      <div style={{ fontWeight: 700, fontSize: '14px', color: 'var(--text-primary)', marginBottom: '8px', lineHeight: 1.3 }}>
        {data.label}
      </div>

      {/* ── Core device fields ───────────────────────────────────── */}
      <InfoRow label="IP"       value={displayIp}    />
      <InfoRow label="Status"   value={stateValue}   highlight={stateHighlight} />
      <InfoRow label="Version"  value={data.os_version  as string | undefined} />
      <InfoRow label="Uptime"   value={data.uptime      as string | undefined} />

      {/* ── Stack section ────────────────────────────────────────── */}
      {isStack && (
        <>
          <div style={{
            display:       'flex',
            alignItems:    'center',
            gap:           '5px',
            marginTop:     '10px',
            marginBottom:  '6px',
            paddingTop:    '8px',
            borderTop:     '1px solid var(--border-color)',
            fontSize:      '10px',
            fontWeight:    700,
            letterSpacing: '0.07em',
            color:         'var(--text-muted)',
            textTransform: 'uppercase',
          }}>
            <Layers size={11} />
            Stack — {members.length} member{members.length !== 1 ? 's' : ''}
          </div>
          {members.map((mb) => (
            <MemberCard key={mb.switch_num} mb={mb} />
          ))}
        </>
      )}

      {/* ── NetBox link ──────────────────────────────────────────── */}
      {data.netbox_url && (
        <a
          href={data.netbox_url as string}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display:    'inline-flex',
            alignItems: 'center',
            gap:        '4px',
            fontSize:   '11px',
            color:      'var(--color-primary)',
            textDecoration: 'none',
            marginTop:  '8px',
          }}
        >
          <ExternalLink size={11} />
          View in NetBox
        </a>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// DeviceNode component
// ---------------------------------------------------------------------------

function DeviceNode({ data, selected }: NodeProps<NodeData>) {
  const [hovered, setHovered]  = useState(false);
  const setSelected            = useTraceStore((s) => s.setSelectedElement);
  // Management IP for intermediate devices (switches/routers) that don't have
  // data.ip set directly — read from the graph metadata device map.
  const deviceIpMap            = useTraceStore((s) => s.graph?.metadata?.device_ip_map ?? {});
  const deviceIp               = deviceIpMap[data.label] ?? null;

  const handleClick = useCallback(() => {
    setSelected({ type: 'node', data });
  }, [data, setSelected]);

  const isEndpoint  = data.node_type === 'src' || data.node_type === 'dst';
  const borderColor = isEndpoint
    ? 'var(--color-primary)'
    : data.layer === 'L3' ? 'var(--l3-color)'
    : data.layer === 'L2' ? 'var(--l2-color)'
    : 'var(--border-color)';

  // Display IP on the node card itself (src/dst IP or management IP)
  const displayIp = (data.ip as string | undefined) || deviceIp;

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onClick={handleClick}
      style={{
        position:     'relative',
        width:        '180px',
        height:       '60px',
        background:   'var(--node-bg)',
        border:       `2px solid ${selected ? 'var(--color-primary)' : borderColor}`,
        borderStyle:  isEndpoint && !selected ? 'dashed' : 'solid',
        borderRadius: isEndpoint ? '30px' : '6px',
        display:      'flex',
        alignItems:   'center',
        padding:      '0 10px',
        gap:          '8px',
        cursor:       'pointer',
        boxShadow:    selected
          ? '0 0 0 3px var(--color-primary-alpha)'
          : hovered
          ? '0 2px 10px rgba(0,0,0,0.2)'
          : '0 1px 4px rgba(0,0,0,0.1)',
        transition:   'box-shadow 0.15s, border-color 0.15s',
        borderLeft:   `4px solid ${borderColor}`,
        userSelect:   'none',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0.5 }} />

      {/* Icon */}
      <span style={{ color: borderColor, flexShrink: 0 }}>
        <NodeIcon nodeType={data.node_type} />
      </span>

      {/* Label + IP */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize:     '12px',
          fontWeight:   600,
          color:        'var(--text-primary)',
          whiteSpace:   'nowrap',
          overflow:     'hidden',
          textOverflow: 'ellipsis',
          display:      'flex',
          alignItems:   'center',
        }}>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{data.label}</span>
          <LayerBadge layer={data.layer} />
        </div>
        {displayIp && (
          <div style={{
            fontSize:     '10px',
            color:        'var(--text-muted)',
            whiteSpace:   'nowrap',
            overflow:     'hidden',
            textOverflow: 'ellipsis',
          }}>
            {displayIp}
          </div>
        )}
      </div>

      <Handle type="source" position={Position.Right} style={{ opacity: 0.5 }} />

      {/* Hover tooltip */}
      {hovered && <TooltipCard data={data} deviceIp={deviceIp} />}
    </div>
  );
}

export default React.memo(DeviceNode);
