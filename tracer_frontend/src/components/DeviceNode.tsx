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
} from 'lucide-react';
import type { NodeData } from '../types/trace';
import { useTraceStore } from '../store/traceStore';

// ---------------------------------------------------------------------------
// Icon selector
// ---------------------------------------------------------------------------

function NodeIcon({ nodeType }: { nodeType: string }) {
  const size = 14;
  switch (nodeType) {
    case 'switch':
      return <Network size={size} />;
    case 'router':
    case 'gateway':
      return <Router size={size} />;
    case 'src':
      return <Monitor size={size} />;
    case 'dst':
      return <Server size={size} />;
    default:
      return <HelpCircle size={size} />;
  }
}

// ---------------------------------------------------------------------------
// Layer badge
// ---------------------------------------------------------------------------

function LayerBadge({ layer }: { layer: string }) {
  return (
    <span
      style={{
        fontSize: '9px',
        fontWeight: 700,
        letterSpacing: '0.05em',
        padding: '1px 4px',
        borderRadius: '3px',
        backgroundColor: layer === 'L3' ? 'var(--l3-color)' : 'var(--l2-color)',
        color: '#fff',
        marginLeft: '4px',
        flexShrink: 0,
      }}
    >
      {layer}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Tooltip card
// ---------------------------------------------------------------------------

function TooltipCard({ data }: { data: NodeData }) {
  return (
    <div
      style={{
        position: 'absolute',
        bottom: 'calc(100% + 8px)',
        left: '50%',
        transform: 'translateX(-50%)',
        zIndex: 9999,
        background: 'var(--bg-tooltip)',
        border: '1px solid var(--border-color)',
        borderRadius: '6px',
        padding: '10px 12px',
        minWidth: '200px',
        maxWidth: '280px',
        boxShadow: '0 4px 16px rgba(0,0,0,0.25)',
        pointerEvents: 'auto',
      }}
    >
      <div style={{ fontWeight: 700, fontSize: '13px', color: 'var(--text-primary)', marginBottom: '6px' }}>
        {data.label}
      </div>
      {data.ip && (
        <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginBottom: '4px' }}>
          IP: {data.ip}
        </div>
      )}
      {data.description && (
        <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginBottom: '4px' }}>
          {data.description}
        </div>
      )}
      {data.state && (
        <div style={{ fontSize: '11px', color: data.state === 'up' ? 'var(--color-success)' : 'var(--color-error)', marginBottom: '4px' }}>
          Status: {data.state}
        </div>
      )}
      {data.netbox_url && (
        <a
          href={data.netbox_url}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '4px',
            fontSize: '11px',
            color: 'var(--color-primary)',
            textDecoration: 'none',
            marginTop: '6px',
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
  const [hovered, setHovered] = useState(false);
  const setSelected = useTraceStore((s) => s.setSelectedElement);

  const handleClick = useCallback(() => {
    setSelected({ type: 'node', data });
  }, [data, setSelected]);

  const isEndpoint = data.node_type === 'src' || data.node_type === 'dst';

  const borderColor = isEndpoint
    ? 'var(--color-primary)'
    : data.layer === 'L3'
    ? 'var(--l3-color)'
    : data.layer === 'L2'
    ? 'var(--l2-color)'
    : 'var(--border-color)';

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onClick={handleClick}
      style={{
        position: 'relative',
        width: '180px',
        height: '60px',
        background: 'var(--node-bg)',
        border: `2px solid ${selected ? 'var(--color-primary)' : borderColor}`,
        borderStyle: isEndpoint && !selected ? 'dashed' : 'solid',
        borderRadius: isEndpoint ? '30px' : '6px',
        display: 'flex',
        alignItems: 'center',
        padding: '0 10px',
        gap: '8px',
        cursor: 'pointer',
        boxShadow: selected
          ? '0 0 0 3px var(--color-primary-alpha)'
          : hovered
          ? '0 2px 10px rgba(0,0,0,0.2)'
          : '0 1px 4px rgba(0,0,0,0.1)',
        transition: 'box-shadow 0.15s, border-color 0.15s',
        borderLeft: `4px solid ${borderColor}`,
        userSelect: 'none',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0.5 }} />

      {/* Icon */}
      <span style={{ color: borderColor, flexShrink: 0 }}>
        <NodeIcon nodeType={data.node_type} />
      </span>

      {/* Label + badge */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: '12px',
            fontWeight: 600,
            color: 'var(--text-primary)',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{data.label}</span>
          <LayerBadge layer={data.layer} />
        </div>
        {data.ip && (
          <div
            style={{
              fontSize: '10px',
              color: 'var(--text-muted)',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {data.ip}
          </div>
        )}
      </div>

      <Handle type="source" position={Position.Right} style={{ opacity: 0.5 }} />

      {/* Hover tooltip */}
      {hovered && <TooltipCard data={data} />}
    </div>
  );
}

export default React.memo(DeviceNode);
