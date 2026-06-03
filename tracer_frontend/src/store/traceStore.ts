import { create } from 'zustand';
import * as api from '../api/client';
import type { DeviceUpdate, GraphResponse, InterfaceDetailResult, InterfaceUpdate, PortchannelUpdate, SelectedElement } from '../types/trace';

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

  // Bidirectional mode
  bidirectional: boolean;

  // ── Forward trace ─────────────────────────────────────────────────────────
  phase:    TracePhase;
  traceId:  string | null;
  progress: string[];
  error:    string | null;
  graph:        GraphResponse | null;
  graphVersion: number;
  pendingEnrichments:   InterfaceUpdate[];
  pendingPortchannels:  PortchannelUpdate[];
  pendingDeviceUpdates: DeviceUpdate[];
  _es: EventSource | null;

  // ── Reverse trace (dst→src) ───────────────────────────────────────────────
  phaseReverse:    TracePhase;
  traceIdReverse:  string | null;
  progressReverse: string[];
  graphReverse:        GraphResponse | null;
  graphVersionReverse: number;
  pendingEnrichmentsReverse:   InterfaceUpdate[];
  pendingPortchannelsReverse:  PortchannelUpdate[];
  pendingDeviceUpdatesReverse: DeviceUpdate[];
  _esReverse: EventSource | null;

  // On-demand show-interface cache — keyed by "device/interface"
  interfaceDetailCache: Record<string, InterfaceDetailResult>;

  // Selection (detail panel)
  selectedElement: SelectedElement | null;

  // Actions
  setSrcIp:                      (ip: string) => void;
  setDstIp:                      (ip: string) => void;
  setBidirectional:              (v: boolean) => void;
  runTrace:                      () => Promise<void>;
  cancelTrace:                   () => void;
  clearTrace:                    () => void;
  loadGraph:                     (graph: GraphResponse) => void;
  setSelectedElement:            (el: SelectedElement | null) => void;
  clearPendingEnrichments:       () => void;
  clearPendingDeviceUpdates:     () => void;
  clearPendingEnrichmentsReverse:   () => void;
  clearPendingDeviceUpdatesReverse: () => void;
  cacheInterfaceDetail:          (device: string, iface: string, result: InterfaceDetailResult) => void;
}

export const useTraceStore = create<TraceState>((set, get) => {

  // ── Internal: open SSE stream for the REVERSE trace ──────────────────────
  function openReverseStream(traceId: string) {
    const streamUrl = `${BASE}/api/v1/traces/${traceId}/stream`;
    const es = new EventSource(streamUrl);
    set({ _esReverse: es });

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
        set((s) => ({ progressReverse: [...s.progressReverse, line] }));
        return;
      }

      if (type === 'topology') {
        const { traceIdReverse } = get();
        if (!traceIdReverse) return;
        api.getGraph(traceIdReverse)
          .then((graph) => {
            set((s) => ({
              graphReverse:               graph,
              graphVersionReverse:        s.graphVersionReverse + 1,
              phaseReverse:               'enriching',
              pendingEnrichmentsReverse:  [],
              pendingPortchannelsReverse: [],
            }));
          })
          .catch(() => { /* graph not ready yet; done event will finalise */ });
        return;
      }

      if (type === 'interface_update') {
        const device = msg.device    as string;
        const iface  = msg.interface as string;
        const data   = msg.data      as Record<string, unknown>;
        set((s) => ({
          pendingEnrichmentsReverse: [
            ...s.pendingEnrichmentsReverse,
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
          pendingPortchannelsReverse: [
            ...s.pendingPortchannelsReverse,
            { device, interface: iface, members },
          ],
        }));
        return;
      }

      if (type === 'device_update') {
        const device = msg.device as string;
        const data   = msg.data   as { os_version?: string; uptime?: string };
        set((s) => ({
          pendingDeviceUpdatesReverse: [
            ...s.pendingDeviceUpdatesReverse,
            { device, data },
          ],
        }));
        return;
      }

      if (type === 'done') {
        const doneStatus = msg.status as string;
        es.close();
        set({ _esReverse: null });

        if (doneStatus === 'completed' || doneStatus === 'enriching') {
          const { traceIdReverse: tid, graphReverse: existing } = get();
          if (tid && !existing) {
            api.getGraph(tid)
              .then((g) => set((s) => ({
                graphReverse:        g,
                graphVersionReverse: s.graphVersionReverse + 1,
                phaseReverse:        'done',
              })))
              .catch(() => set({ phaseReverse: 'done' }));
          } else {
            set({ phaseReverse: 'done' });
          }
        } else if (doneStatus === 'failed') {
          const { traceIdReverse: tid } = get();
          if (tid) {
            api.getTrace(tid)
              .then((t) => set({ phaseReverse: 'error', error: t.error ?? 'Reverse trace failed' }))
              .catch(() => set({ phaseReverse: 'error' }));
          } else {
            set({ phaseReverse: 'error' });
          }
        } else {
          set({ phaseReverse: 'idle' });
        }
        return;
      }
    };

    es.onerror = () => {
      const { phaseReverse } = get();
      if (phaseReverse === 'streaming' || phaseReverse === 'enriching') {
        es.close();
        set({ _esReverse: null, phaseReverse: 'error' });
      }
    };
  }

  return {
    // ── State ───────────────────────────────────────────────────────────────
    srcIp:                  '',
    dstIp:                  '',
    bidirectional:          false,

    phase:                  'idle',
    traceId:                null,
    progress:               [],
    error:                  null,
    graph:                  null,
    graphVersion:           0,
    pendingEnrichments:     [],
    pendingPortchannels:    [],
    pendingDeviceUpdates:   [],
    _es:                    null,

    phaseReverse:                'idle',
    traceIdReverse:              null,
    progressReverse:             [],
    graphReverse:                null,
    graphVersionReverse:         0,
    pendingEnrichmentsReverse:   [],
    pendingPortchannelsReverse:  [],
    pendingDeviceUpdatesReverse: [],
    _esReverse:                  null,

    interfaceDetailCache:   {},
    selectedElement:        null,

    // ── Simple setters ───────────────────────────────────────────────────────
    setSrcIp:         (ip) => set({ srcIp: ip }),
    setDstIp:         (ip) => set({ dstIp: ip }),
    setBidirectional: (v)  => set({ bidirectional: v }),
    setSelectedElement: (el) => set({ selectedElement: el }),

    clearPendingEnrichments:    () => set({ pendingEnrichments: [], pendingPortchannels: [] }),
    clearPendingDeviceUpdates:  () => set({ pendingDeviceUpdates: [] }),
    clearPendingEnrichmentsReverse:    () => set({ pendingEnrichmentsReverse: [], pendingPortchannelsReverse: [] }),
    clearPendingDeviceUpdatesReverse:  () => set({ pendingDeviceUpdatesReverse: [] }),

    cacheInterfaceDetail: (device, iface, result) =>
      set((s) => ({
        interfaceDetailCache: {
          ...s.interfaceDetailCache,
          [`${device}/${iface}`]: result,
        },
      })),

    loadGraph: (graph) =>
      set((s) => ({
        graph,
        graphVersion:        s.graphVersion + 1,
        phase:               'done',
        progress:            [],
        error:               null,
        traceId:             null,
        pendingEnrichments:  [],
        pendingPortchannels: [],
        pendingDeviceUpdates: [],
      })),

    clearTrace: () => {
      get()._es?.close();
      get()._esReverse?.close();
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

        phaseReverse:                'idle',
        traceIdReverse:              null,
        progressReverse:             [],
        graphReverse:                null,
        pendingEnrichmentsReverse:   [],
        pendingPortchannelsReverse:  [],
        _esReverse:                  null,
      });
    },

    cancelTrace: () => {
      get()._es?.close();
      get()._esReverse?.close();
      set({
        phase:   'idle',
        traceId: null,
        progress: [],
        error:   null,
        _es:     null,

        phaseReverse:   'idle',
        traceIdReverse: null,
        progressReverse: [],
        _esReverse:     null,
      });
    },

    // ── Main trace action ────────────────────────────────────────────────────
    runTrace: async () => {
      const { srcIp, dstIp, bidirectional } = get();

      // Close any existing streams
      get()._es?.close();
      get()._esReverse?.close();

      // Reset all state
      set({
        phase:                'submitting',
        traceId:              null,
        progress:             [],
        error:                null,
        graph:                null,
        selectedElement:      null,
        pendingEnrichments:   [],
        pendingPortchannels:  [],
        interfaceDetailCache: {},
        _es:                  null,

        phaseReverse:                bidirectional ? 'submitting' : 'idle',
        traceIdReverse:              null,
        progressReverse:             [],
        graphReverse:                null,
        pendingEnrichmentsReverse:   [],
        pendingPortchannelsReverse:  [],
        pendingDeviceUpdatesReverse: [],
        _esReverse:                  null,
      });

      // ── Launch forward trace ───────────────────────────────────────────────
      try {
        const summary = await api.startTrace(srcIp, dstIp);
        set({ phase: 'streaming', traceId: summary.trace_id });

        const streamUrl = `${BASE}/api/v1/traces/${summary.trace_id}/stream`;
        const es = new EventSource(streamUrl);
        set({ _es: es });

        es.onmessage = (event: MessageEvent) => {
          let msg: Record<string, unknown>;
          try {
            msg = JSON.parse(event.data as string) as Record<string, unknown>;
          } catch { return; }

          const type = msg.type as string;

          if (type === 'progress') {
            const line = msg.message as string;
            set((s) => ({ progress: [...s.progress, line] }));
            return;
          }

          if (type === 'topology') {
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
              .catch(() => { /* done event will finalise */ });
            return;
          }

          if (type === 'interface_update') {
            const device = msg.device    as string;
            const iface  = msg.interface as string;
            const data   = msg.data      as Record<string, unknown>;
            set((s) => ({
              pendingEnrichments: [...s.pendingEnrichments, { device, interface: iface, data }],
            }));
            return;
          }

          if (type === 'portchannel_update') {
            const device  = msg.device    as string;
            const iface   = msg.interface as string;
            const members = msg.members   as string[];
            set((s) => ({
              pendingPortchannels: [...s.pendingPortchannels, { device, interface: iface, members }],
            }));
            return;
          }

          if (type === 'device_update') {
            const device = msg.device as string;
            const data   = msg.data   as { os_version?: string; uptime?: string };
            set((s) => ({
              pendingDeviceUpdates: [...s.pendingDeviceUpdates, { device, data }],
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
        // Don't launch reverse if forward submit failed
        return;
      }

      // ── Launch reverse trace (dst → src) when bidirectional ───────────────
      if (bidirectional) {
        try {
          const revSummary = await api.startTrace(dstIp, srcIp);
          set({ phaseReverse: 'streaming', traceIdReverse: revSummary.trace_id });
          openReverseStream(revSummary.trace_id);
        } catch (err) {
          set({
            phaseReverse: 'error',
          });
        }
      }
    },
  };
});
