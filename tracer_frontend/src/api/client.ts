import axios, { AxiosError } from 'axios';
import type {
  GraphResponse,
  HistoryDetail,
  HistoryListResponse,
  InterfaceDetailResult,
  OrdrDeviceData,
  TraceSummary,
  TraceResponse,
} from '../types/trace';

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');

const http = axios.create({ baseURL: BASE, timeout: 30_000 });

// ---------------------------------------------------------------------------
// Error normaliser — always returns a plain string message
// ---------------------------------------------------------------------------
function extractMessage(err: unknown): string {
  if (err instanceof AxiosError) {
    const detail = err.response?.data?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) return detail.map((d) => d.msg ?? d).join('; ');
    return err.message;
  }
  if (err instanceof Error) return err.message;
  return 'An unknown error occurred';
}

// ---------------------------------------------------------------------------
// Traces
// ---------------------------------------------------------------------------

export async function startTrace(srcIp: string, dstIp: string): Promise<TraceSummary> {
  try {
    const res = await http.post<TraceSummary>('/api/v1/traces', {
      src_ip: srcIp,
      dst_ip: dstIp,
    });
    return res.data;
  } catch (err) {
    throw new Error(extractMessage(err));
  }
}

export async function getTrace(traceId: string): Promise<TraceResponse> {
  try {
    const res = await http.get<TraceResponse>(`/api/v1/traces/${traceId}`);
    return res.data;
  } catch (err) {
    throw new Error(extractMessage(err));
  }
}

export async function getGraph(traceId: string): Promise<GraphResponse> {
  try {
    const res = await http.get<GraphResponse>(`/api/v1/traces/${traceId}/graph`);
    return res.data;
  } catch (err) {
    throw new Error(extractMessage(err));
  }
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

export async function listHistory(params: {
  src_ip?: string;
  dst_ip?: string;
  q?:      string;
  limit?:  number;
  offset?: number;
}): Promise<HistoryListResponse> {
  try {
    const res = await http.get<HistoryListResponse>('/api/v1/history', { params });
    return res.data;
  } catch (err) {
    throw new Error(extractMessage(err));
  }
}

export async function getHistoryEntry(id: string): Promise<HistoryDetail> {
  try {
    const res = await http.get<HistoryDetail>(`/api/v1/history/${id}`);
    return res.data;
  } catch (err) {
    throw new Error(extractMessage(err));
  }
}

export async function queryOrdr(ip: string): Promise<OrdrDeviceData> {
  try {
    const res = await http.get<OrdrDeviceData>(`/api/v1/ordr/${encodeURIComponent(ip)}`);
    return res.data;
  } catch (err) {
    throw new Error(extractMessage(err));
  }
}

export async function fetchInterfaceDetail(
  deviceIp: string,
  iface:    string,
): Promise<InterfaceDetailResult> {
  try {
    const res = await http.post<InterfaceDetailResult>('/api/v1/interfaces/detail', {
      device_ip: deviceIp,
      interface: iface,
    });
    return res.data;
  } catch (err) {
    throw new Error(extractMessage(err));
  }
}

export async function clearTraceCache(srcIp?: string, dstIp?: string): Promise<{ cleared: number }> {
  try {
    const params: Record<string, string> = {};
    if (srcIp) params.src_ip = srcIp;
    if (dstIp) params.dst_ip = dstIp;
    const res = await http.delete<{ cleared: number }>('/api/v1/cache', { params });
    return res.data;
  } catch (err) {
    throw new Error(extractMessage(err));
  }
}

export async function deleteHistoryEntry(id: string): Promise<void> {
  try {
    await http.delete(`/api/v1/history/${id}`);
  } catch (err) {
    throw new Error(extractMessage(err));
  }
}
