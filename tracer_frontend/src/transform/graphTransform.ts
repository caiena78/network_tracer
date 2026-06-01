/**
 * Transforms the Cytoscape.js-compatible graph from the backend API into
 * React Flow nodes and edges, then applies a dagre left-to-right layout.
 *
 * Backend elements use:
 *   nodes: { data: { id, label, node_type, layer, ... } }
 *   edges: { data: { id, source, target, layer, ... } }
 *
 * React Flow needs:
 *   nodes: { id, type, data, position }
 *   edges: { id, source, target, type, style, data, markerEnd }
 */

import dagre from 'dagre';
import { MarkerType, Position } from 'reactflow';
import type { Node, Edge } from 'reactflow';
import type { GraphElement, GraphResponse, NodeData, EdgeData } from '../types/trace';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const NODE_W = 180;
export const NODE_H = 60;

export const L2_COLOR    = '#a0522d'; // sienna — readable in both themes
export const L3_COLOR    = '#1e90ff'; // dodger blue
export const MIXED_COLOR = '#9b59b6'; // purple for mixed L2/L3

// ---------------------------------------------------------------------------
// Type guards
// ---------------------------------------------------------------------------

export function isEdgeData(data: NodeData | EdgeData): data is EdgeData {
  return 'source' in data && 'target' in data;
}

export function isNodeData(data: NodeData | EdgeData): data is NodeData {
  return !isEdgeData(data);
}

// ---------------------------------------------------------------------------
// Edge colour and style
// ---------------------------------------------------------------------------

export function edgeColor(layer: string): string {
  if (layer === 'L2')    return L2_COLOR;
  if (layer === 'L3')    return L3_COLOR;
  return MIXED_COLOR;
}

// ---------------------------------------------------------------------------
// Dagre layout
// ---------------------------------------------------------------------------

function applyDagreLayout(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'LR', nodesep: 90, ranksep: 180, marginx: 50, marginy: 50 });

  nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
  edges.forEach((e) => {
    // dagre requires both endpoints to exist
    if (g.hasNode(e.source) && g.hasNode(e.target)) {
      g.setEdge(e.source, e.target);
    }
  });

  dagre.layout(g);

  return nodes.map((n) => {
    const pos = g.node(n.id);
    return {
      ...n,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      position: pos
        ? { x: pos.x - NODE_W / 2, y: pos.y - NODE_H / 2 }
        : { x: 0, y: 0 },
    };
  });
}

// ---------------------------------------------------------------------------
// Main transform
// ---------------------------------------------------------------------------

export interface TransformedGraph {
  nodes: Node[];
  edges: Edge[];
}

export function transformGraph(graph: GraphResponse): TransformedGraph {
  const rawNodes: Node[] = [];
  const rawEdges: Edge[] = [];

  for (const el of graph.elements) {
    if (isEdgeData(el.data as NodeData | EdgeData)) {
      const ed = el.data as EdgeData;
      const color = edgeColor(ed.layer);
      rawEdges.push({
        id:        ed.id,
        source:    ed.source,
        target:    ed.target,
        type:      'connectionEdge',
        data:      ed,
        style:     { stroke: color, strokeWidth: 2 },
        markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
        // Keep label empty by default — shown in tooltip/detail panel
        label: '',
      });
    } else {
      const nd = el.data as NodeData;
      rawNodes.push({
        id:       nd.id,
        type:     'deviceNode',
        data:     nd,
        position: { x: 0, y: 0 }, // overwritten by dagre
      });
    }
  }

  const layoutedNodes = applyDagreLayout(rawNodes, rawEdges);
  return { nodes: layoutedNodes, edges: rawEdges };
}
