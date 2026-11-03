import type { HealthInfo, KbInfo, SearchMode, SearchResponse } from './types';

const BASE = import.meta.env.VITE_API_BASE || '';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'content-type': 'application/json' },
    ...init,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error((body as { error?: string }).error || `HTTP ${res.status}`);
  }
  return body as T;
}

export const api = {
  health: () => request<HealthInfo>('/api/health'),
  listKbs: () => request<{ kbs: KbInfo[] }>('/api/kbs').then((r) => r.kbs),
  createKb: (name: string, dtype: string, model: string) =>
    request<{ kb: KbInfo }>('/api/kbs', {
      method: 'POST',
      body: JSON.stringify({ name, dtype, model }),
    }).then((r) => r.kb),
  deleteKb: (id: string) => request<{ ok: boolean }>(`/api/kbs/${id}`, { method: 'DELETE' }),
  addDocuments: (
    kbId: string,
    documents: {
      id: string;
      text: string;
      embedding: number[];
      metadata: Record<string, unknown>;
    }[],
    refresh = false
  ) =>
    request<{
      ok: boolean;
      written: number;
      writeMs: number;
      refreshMs: number | null;
      count: number;
    }>(`/api/kbs/${kbId}/documents`, {
      method: 'POST',
      body: JSON.stringify({ documents, refresh }),
    }),
  refresh: (kbId: string) =>
    request<{ ok: boolean; refreshMs: number; count: number }>(`/api/kbs/${kbId}/refresh`, {
      method: 'POST',
      body: '{}',
    }),
  search: (
    kbId: string,
    payload: { query: string; embedding: number[]; mode: SearchMode; nResults: number }
  ) =>
    request<SearchResponse>(`/api/kbs/${kbId}/search`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
};
