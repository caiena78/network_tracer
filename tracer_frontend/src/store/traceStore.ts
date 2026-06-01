import { create } from 'zustand';
import * as api from '../api/client';
import type { GraphResponse, InterfaceUpdate, PortchannelUpdate, SelectedElement } from '../types/trace';

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');

export type TracePhase =
  | 'idle'
  | 'submitting'
  | 'streaming'    // SSE open, waiting for topology
  | 'enriching'    // topology drawn, interface counters streaming in
  | 'done'
  | 'error';

interface TraceState {
  // Form
  srcIp: string;
  dstIp: string;

  // Execution
  phase:    TracePhase;
  traceId:  string | null;
  progress: string[];
  error:    string | null;

  // Result
  graph:        GraphResponse | null;
  graphVersion: number;

  // Streaming enrichment — pending interface updates not yet applied to edges
  pendingEnrichments:   InterfaceUpdate[];
  pendingPortchannels:  PortchannelUpdate[];

  // Selection (detail panel)
  selectedElement: SelectedElement | null;

  // SSE handle
  _es: EventSource | null;

  // Actions
  setSrcIp:               (ip: string) => void;
  setDstIp:               (ip: string) => void;
  runTrace:               () => Promise<void>;
  cancelTrace:            () => void;
  clearTrace:             () => void;
  loadGraph:              (graph: GraphResponse) => void;
  setSelectedElement:     (el: SelectedElement | null) => void;
  clearPendingEnrichments:() => void;
}

export const useTraceStore = create<TraceState>((set, get) => ({
  srcIp:                  '',
  dstIp:                  '',
  phase:                  'idle',
  traceId:                null,
  progress:               [],
  error:                  null,
  graph:                  null,
  graphVersion:           0,
  pendingEnrichments:     [],
  pendingPortchannels:    [],
  selectedElement:        null,
  _es:                    null,

  setSrcIp: (ip) => set({ srcIp: ip }),
  setDstIp: (ip) => set({ dstIp: ip }),
  setSelectedElement: (el) => set({ selectedElement: el }),
  clearPendingEnrichments: () => set({ pendingEnrichments: [], pendingPortchannels: [] }),

  loadGraph: (graph) =>
    set((s) => ({
      graph,
      graphVersion:    s.graphVersion + 1,
      phase:           'done',
      progress:        [],
      error:           null,
      traceId:         null,
      pendingEnrichments:  [],
      pendingPortchannels: [],
    })),

  clearTrace: () => {
    get()._es?.close();
    set({
      phase:               'idle',
      traceId:             null,
      progress:            [],
      error:               null,
      graph:               null,
      selectedElement:     null,
      pendingEnrichments:  [],
      pendingPortchannels: [],
      _es:                 null,
    });
  },

  cancelTrace: () => {
    get()._es?.close();
    set({ phase: 'idle', traceId: null, progress: [], error: null, _es: null });
  },

  runTrace: async () => {
    const { srcIp, dstIp } = get();

    // Close any existing SSE stream
    get()._es?.close();

    set({
      phase:               'submitting',
      traceId:             null,
      progress:            [],
      error:               null,
      graph:               null,
      selectedElement:     null,
      pendingEnrichments:  [],
      pendingPortchannels: [],
      _es:                 null,
    });

    try {
      const summary = await api.startTrace(srcIp, dstIp);
      set({ phase: 'streaming', traceId: summary.trace_id });

      // ── Open SSE stream ──────────────────────────────────────────────────
      const streamUrl = `${BASE}/api/v1/traces/${summary.trace_id}/stream`;
      const es = new EventSource(streamUrl);

      set({ _es: es });

      es.onmessage = (event: MessageEvent) => {
        let msg: Record<string, unknown>;
        try {
          msg = JSON.parse(event.data as string) as Record<string, unknown>;
        } catch {
          return;
        }

        const type = msg.type as string;

        if (type === 'progress') {
          const line = msg.message as string;
          set((s) => ({ progress: [...s.progress, line] }));
          return;
        }

        if (type === 'topology') {
          // Phase 1 complete — build graph from sparse flat paths via /graph endpoint
          const { traceId } = get();
          if (!traceId) return;
          api.getGraph(traceId)
            .then((graph) => {
              set((s) => ({
                graph,
                graphVersion:    s.graphVersion + 1,
                phase:           'enriching',
                pendingEnrichments:  [],
                pendingPortchannels: [],
              }));
            })
            .catch(() => {
              // Graph endpoint may not be ready yet — ignore; done event will finalise
            });
          return;
        }

        if (type === 'interface_update') {
          const device    = msg.device    as string;
          const iface     = msg.interface as string;
          const data      = msg.data      as Record<string, unknown>;
          set((s) => ({
            pendingEnrichments: [
              ...s.pendingEnrichments,
              { device, interface: iface, data },
            ],
          }));
          return;
        }

        if (type === 'portchannel_update') {
          const device  = msg.device    as string;
          const iface   = msg.interface as string;
          const members = msg.members   as string[];
          set((s) => ({
            pendingPortchannels: [
              ...s.pendingPortchannels,
              { device, interface: iface, members },
            ],
          }));
          return;
        }

        if (type === 'done') {
          const doneStatus = msg.status as string;
          es.close();
          set({ _es: null });

          if (doneStatus === 'completed' || doneStatus === 'enriching') {
            const { traceId: tid, graph: existing } = get();
            if (tid && !existing) {
              // Topology event may have been missed — fetch graph now
              api.getGraph(tid)
                .then((g) => set((s) => ({
                  graph:        g,
                  graphVersion: s.graphVersion + 1,
                  phase:        'done',
                })))
                .catch(() => set({ phase: 'done' }));
            } else {
              set({ phase: 'done' });
            }
          } else if (doneStatus === 'failed') {
            // Fetch error message from the trace record
            const { traceId: tid } = get();
            if (tid) {
              api.getTrace(tid)
                .then((t) => set({ phase: 'error', error: t.error ?? 'Trace failed' }))
                .catch(() => set({ phase: 'error', error: 'Trace failed' }));
            } else {
              set({ phase: 'error', error: 'Trace failed' });
            }
          } else {
            set({ phase: 'idle' });
          }
          return;
        }
      };

      es.onerror = () => {
        // SSE connection dropped — mark error if still streaming
        const { phase } = get();
        if (phase === 'streaming' || phase === 'enriching') {
          es.close();
          set({ _es: null, phase: 'error', error: 'Lost connection to trace stream.' });
        }
      };
    } catch (err) {
      set({
        phase: 'error',
        error: err instanceof Error ? err.message : 'Unexpected error',
        _es:   null,
      });
    }
  },
}));
