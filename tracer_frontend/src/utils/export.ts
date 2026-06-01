/**
 * Export utilities for PNG and SVG.
 *
 * PNG: uses html2canvas on the React Flow container element.
 * SVG: builds a standalone SVG from node/edge position data so the result
 *      is vector-quality and opens cleanly in any SVG viewer or Inkscape.
 */

import html2canvas from 'html2canvas';
import type { Node, Edge } from 'reactflow';
import { NODE_W, NODE_H, edgeColor } from '../transform/graphTransform';

// ---------------------------------------------------------------------------
// PNG export
// ---------------------------------------------------------------------------

export async function exportToPng(
  container: HTMLElement,
  filename = 'network-trace.png',
): Promise<void> {
  const canvas = await html2canvas(container, {
    backgroundColor: null,
    useCORS: true,
    scale: 2,
    logging: false,
    // Capture the entire scrollable area
    windowWidth: container.scrollWidth,
    windowHeight: container.scrollHeight,
  });

  triggerDownload(canvas.toDataURL('image/png'), filename);
}

// ---------------------------------------------------------------------------
// SVG export
// ---------------------------------------------------------------------------

function escapeXml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

const PADDING = 60;

export function exportToSvg(
  nodes: Node[],
  edges: Edge[],
  filename = 'network-trace.svg',
): void {
  if (nodes.length === 0) return;

  // Bounding box
  const xs = nodes.map((n) => n.position.x);
  const ys = nodes.map((n) => n.position.y);
  const minX = Math.min(...xs) - PADDING;
  const minY = Math.min(...ys) - PADDING;
  const maxX = Math.max(...xs) + NODE_W + PADDING;
  const maxY = Math.max(...ys) + NODE_H + PADDING;
  const width  = maxX - minX;
  const height = maxY - minY;

  const tx = (x: number) => x - minX;
  const ty = (y: number) => y - minY;

  // Build node map for edge endpoint lookup
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));

  // Edges SVG
  const edgesSvg = edges
    .map((e) => {
      const src = nodeMap.get(e.source);
      const tgt = nodeMap.get(e.target);
      if (!src || !tgt) return '';

      const x1 = tx(src.position.x) + NODE_W;
      const y1 = ty(src.position.y) + NODE_H / 2;
      const x2 = tx(tgt.position.x);
      const y2 = ty(tgt.position.y) + NODE_H / 2;
      const mx = (x1 + x2) / 2;

      const layer = (e.data as { layer?: string } | undefined)?.layer ?? 'L2';
      const color = edgeColor(layer);
      const markerId = `arrow-${layer}`;

      const label   = (e.data as { label?: string } | undefined)?.label ?? '';
      const labelX  = mx;
      const labelY  = (y1 + y2) / 2 - 6;

      return `
  <path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"
        stroke="${color}" stroke-width="2" fill="none"
        marker-end="url(#${markerId})"/>
  ${label ? `<text x="${labelX}" y="${labelY}" text-anchor="middle" fill="${color}" font-size="10" font-family="monospace">${escapeXml(label)}</text>` : ''}`;
    })
    .join('\n');

  // Nodes SVG
  const nodesSvg = nodes
    .map((n) => {
      const x     = tx(n.position.x);
      const y     = ty(n.position.y);
      const layer = (n.data as { layer?: string } | undefined)?.layer ?? 'L2';
      const label = (n.data as { label?: string } | undefined)?.label ?? n.id;
      const ip    = (n.data as { ip?: string } | undefined)?.ip;
      const color = edgeColor(layer);
      const bgFill = layer === 'L3' ? '#0f2640' : '#2a0d00';

      return `
  <g transform="translate(${x},${y})">
    <rect width="${NODE_W}" height="${NODE_H}" rx="5" ry="5"
          fill="${bgFill}" stroke="${color}" stroke-width="2"/>
    <rect width="4" height="${NODE_H}" rx="2" fill="${color}"/>
    <text x="18" y="${ip ? 20 : 34}" font-size="12" font-weight="bold"
          font-family="monospace,sans-serif" fill="#e8e8e8"
          clip-path="url(#clip-node)">${escapeXml(label)}</text>
    ${ip ? `<text x="18" y="38" font-size="10" font-family="monospace,sans-serif" fill="#888">${escapeXml(ip)}</text>` : ''}
  </g>`;
    })
    .join('\n');

  const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="${width}" height="${height}"
     viewBox="0 0 ${width} ${height}">
  <defs>
    <marker id="arrow-L2" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
      <polygon points="0 0, 10 3.5, 0 7" fill="${edgeColor('L2')}"/>
    </marker>
    <marker id="arrow-L3" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
      <polygon points="0 0, 10 3.5, 0 7" fill="${edgeColor('L3')}"/>
    </marker>
    <marker id="arrow-mixed" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
      <polygon points="0 0, 10 3.5, 0 7" fill="${edgeColor('mixed')}"/>
    </marker>
    <clipPath id="clip-node">
      <rect width="${NODE_W - 22}" height="${NODE_H}" x="18"/>
    </clipPath>
  </defs>
  <!-- background -->
  <rect width="${width}" height="${height}" fill="#0d1117"/>
  <!-- edges -->
  ${edgesSvg}
  <!-- nodes -->
  ${nodesSvg}
</svg>`;

  const blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' });
  const url  = URL.createObjectURL(blob);
  triggerDownload(url, filename);
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Shared download helper
// ---------------------------------------------------------------------------

function triggerDownload(href: string, filename: string): void {
  const a = document.createElement('a');
  a.href     = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}
