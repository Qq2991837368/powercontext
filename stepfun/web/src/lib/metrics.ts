export function recallAtK(ranked: string[], relevant: string[], k: number): number {
  if (!relevant.length) return 0;
  const top = new Set(ranked.slice(0, k));
  const hit = relevant.filter((id) => top.has(id)).length;
  return hit / relevant.length;
}

export function ndcgAtK(ranked: string[], relevant: string[], k: number): number {
  const rel = new Set(relevant);
  let dcg = 0;
  ranked.slice(0, k).forEach((id, index) => {
    if (rel.has(id)) dcg += 1 / Math.log2(index + 2);
  });
  const idealHits = Math.min(relevant.length, k);
  let idcg = 0;
  for (let i = 0; i < idealHits; i += 1) idcg += 1 / Math.log2(i + 2);
  return idcg ? dcg / idcg : 0;
}

export function mean(values: number[]): number {
  if (!values.length) return 0;
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}

export function median(values: number[]): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** index;
  return `${value.toFixed(index === 0 ? 0 : 2)} ${units[index]}`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '-';
  if (ms < 1) return `${ms.toFixed(2)} ms`;
  if (ms < 100) return `${ms.toFixed(1)} ms`;
  return `${Math.round(ms)} ms`;
}
