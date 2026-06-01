import React, { useRef, useMemo, useEffect } from 'react';
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  MarkerType,
  useNodesState,
  useEdgesState,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { Network } from 'lucide-react';
import type { EdgeData, GraphResponse } from '../types/trace';
import { transformGraph, edgeColor } from '../transform/graphTransform';
import DeviceNode from './DeviceNode';
import ConnectionEdge from './ConnectionEdge';
import Legend from './Legend';
import ExportControls from './ExportControls';
import { useTraceStore } from '../store/traceStore';

// ---------------------------------------------------------------------------
// Custom node / edge type registry
// MUST be defined outside any render function to prevent re-registration loops
// ---------------------------------------------------------------------------

const nodeTypes = { deviceNode: DeviceNode };
const edgeTypes = { connectionEdge: ConnectionEdge };

// ---------------------------------------------------------------------------
// Inner canvas — receives a stable graph reference, mounts fresh per graphVersion
// ---------------------------------------------------------------------------

interface InnerCanvasProps {
  graph:        GraphResponse;
  containerRef: React.RefObject<HTMLDivElement>;
}

function InnerCanvas({ graph, containerRef }: InnerCanvasProps) {
  // Transform once on mount (component remounts per graphVersion via key)
  const { nodes: initNodes, edges: initEdges } = useMemo(
    () => transformGraph(graph),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [], // intentional: init only on mount; key-based remount handles re-init
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initEdges);

  const pendingEnrichments      = useTraceStore((s) => s.pendingEnrichments);
  const clearPendingEnrichments = useTraceStore((s) => s.clearPendingEnrichments);
  const pendingDeviceUpdates    = useTraceStore((s) => s.pendingDeviceUpdates);
  const clearPendingDeviceUpdates = useTraceStore((s) => s.clearPendingDeviceUpdates);

  // Apply streaming interface updates to edge data without remounting the canvas
  useEffect(() => {
    if (pendingEnrichments.length === 0) return;
    setEdges((prev) =>
      prev.map((edge) => {
        const ed = edge.data as EdgeData | undefined;
        if (!ed) return edge;
        const match = pendingEnrichments.find(
          (u) =>
            (ed.src_device === u.device && ed.src_interface === u.interface) ||
            (ed.dst_device === u.device && ed.dst_interface === u.interface),
        );
        if (!match) return edge;
        const merged: EdgeData = { ...ed, ...(match.data as Partial<EdgeData>) };
        const color = edgeColor(merged.layer ?? 'L2');
        return {
          ...edge,
          data:      merged,
          style:     { stroke: color, strokeWidth: 2 },
          markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
        };
      }),
    );
    clearPendingEnrichments();
  }, [pendingEnrichments, setEdges, clearPendingEnrichments]);

  // Apply streaming device updates (os_version, uptime) to nodes
  useEffect(() => {
    if (pendingDeviceUpdates.length === 0) return;
    setNodes((prev) =>
      prev.map((node) => {
        const nd = node.data as NodeData | undefined;
        if (!nd) return node;
        const update = pendingDeviceUpdates.find((u) => u.device === nd.label);
        if (!update) return node;
        return {
          ...node,
          data: {
            ...nd,
            os_version:    update.data.os_version    ?? nd.os_version,
            uptime:        update.data.uptime        ?? nd.uptime,
            stack_members: update.data.stack_members ?? nd.stack_members,
          },
        };
      }),
    );
    clearPendingDeviceUpdates();
  }, [pendingDeviceUpdates, setNodes, clearPendingDeviceUpdates]);

  const proOptions = useMemo(() => ({ hideAttribution: true }), []);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      fitView
      fitViewOptions={{ padding: 0.25, maxZoom: 1.4 }}
      minZoom={0.05}
      maxZoom={4}
      defaultEdgeOptions={{ animated: false }}
      proOptions={proOptions}
      style={{ width: '100%', height: '100%', background: 'var(--diagram-bg)' }}
    >
      <Background
        variant={BackgroundVariant.Dots}
        gap={20}
        size={1}
        color="var(--diagram-dots)"
      />
      <Controls
        style={{
          background: 'var(--bg-panel)',
          border: '1px solid var(--border-color)',
          borderRadius: '6px',
        }}
      />
      <MiniMap
        nodeColor={(n) => {
          const layer = (n.data as { layer?: string })?.layer;
          return layer === 'L3' ? 'var(--l3-color)' : 'var(--l2-color)';
        }}
        maskColor="var(--minimap-mask)"
        style={{
          background: 'var(--bg-panel)',
          border: '1px solid var(--border-color)',
          borderRadius: '6px',
        }}
      />

      {/* Legend — bottom left overlay */}
      <div
        style={{
          position: 'absolute',
          bottom: '12px',
          left: '12px',
          zIndex: 10,
          pointerEvents: 'none',
        }}
      >
        <Legend />
      </div>

      {/* Export controls — top right overlay (inside ReactFlow for useReactFlow access) */}
      <div
        style={{
          position: 'absolute',
          top: '12px',
          right: '12px',
          zIndex: 10,
        }}
      >
        <ExportControls containerRef={containerRef} graph={graph} />
      </div>
    </ReactFlow>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyState() {
  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '16px',
        color: 'var(--text-muted)',
        background: 'var(--diagram-bg)',
      }}
    >
      <Network size={52} strokeWidth={1} />
      <div style={{ textAlign: 'center' }}>
        <div style={{ fontSize: '16px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '6px' }}>
          No trace loaded
        </div>
        <div style={{ fontSize: '13px' }}>
          Enter source and destination IPs in the sidebar, then run a trace.
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DiagramCanvas (public, exported)
// ---------------------------------------------------------------------------

interface DiagramCanvasProps {
  graph: GraphResponse | null;
}

export default function DiagramCanvas({ graph }: DiagramCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  // graphVersion increments each time a new graph is loaded, forcing InnerCanvas remount
  const graphVersion = useTraceStore((s) => s.graphVersion);

  return (
    <div
      ref={containerRef}
      style={{
        flex: 1,
        position: 'relative',
        overflow: 'hidden',
        minHeight: 0,
        height: '100%',
      }}
    >
      {graph ? (
        // key={graphVersion} ensures React remounts InnerCanvas for each new graph,
        // which re-runs fitView and reinitialises node/edge state cleanly.
        <InnerCanvas key={graphVersion} graph={graph} containerRef={containerRef} />
      ) : (
        <EmptyState />
      )}
    </div>
  );
}
