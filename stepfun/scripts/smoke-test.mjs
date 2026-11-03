const BASE = process.env.BASE_URL || 'http://localhost:8787';

async function api(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'content-type': 'application/json' },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}: ${JSON.stringify(body)}`);
  return body;
}

const DIM = 512;
function vec(seed) {
  const v = new Array(DIM).fill(0);
  v[seed % DIM] = 1;
  v[(seed + 1) % DIM] = 0.15;
  return v;
}

const started = Date.now();
const health = await api('/api/health');
console.log('health:', JSON.stringify(health));

const { kb } = await api('/api/kbs', {
  method: 'POST',
  body: JSON.stringify({ name: `smoke-${Date.now()}`, dtype: 'q8', model: 'Xenova/bge-small-zh-v1.5' }),
});
console.log('created:', kb.id, 'count=', kb.count);

const docs = [
  { id: 'smoke-1', text: 'seekdb 是 OceanBase 推出的向量数据库，支持向量与全文混合检索。', metadata: { source: 'smoke-a' }, embedding: vec(0) },
  { id: 'smoke-2', text: '浏览器端 embedding 模型使用 INT8 量化可以显著减小模型体积。', metadata: { source: 'smoke-b' }, embedding: vec(1) },
  { id: 'smoke-3', text: 'WebGPU 为端侧推理提供 GPU 加速，无法使用时自动降级到 WASM。', metadata: { source: 'smoke-c' }, embedding: vec(2) },
];

const add1 = await api(`/api/kbs/${kb.id}/documents`, {
  method: 'POST',
  body: JSON.stringify({ documents: docs, refresh: true }),
});
console.log('add+refresh:', JSON.stringify(add1));

const hybrid = await api(`/api/kbs/${kb.id}/search`, {
  method: 'POST',
  body: JSON.stringify({ query: 'seekdb 混合检索', embedding: vec(0), mode: 'hybrid', nResults: 5 }),
});
console.log('hybrid:', hybrid.searchMs.toFixed(1) + 'ms', JSON.stringify(hybrid.results.map((r) => r.id)));
console.log('hybrid terms:', JSON.stringify(hybrid.terms));

const vector = await api(`/api/kbs/${kb.id}/search`, {
  method: 'POST',
  body: JSON.stringify({ query: 'seekdb 混合检索', embedding: vec(0), mode: 'vector', nResults: 5 }),
});
console.log('vector:', vector.searchMs.toFixed(1) + 'ms', JSON.stringify(vector.results.map((r) => r.id)));

// Incremental import + index refresh: the newly added chunk must become searchable.
const add2 = await api(`/api/kbs/${kb.id}/documents`, {
  method: 'POST',
  body: JSON.stringify({
    documents: [{ id: 'smoke-4', text: '混合检索使用 RRF 融合向量与全文的排序结果。', metadata: { source: 'smoke-d' }, embedding: vec(3) }],
    refresh: true,
  }),
});
console.log('incremental add+refresh:', JSON.stringify(add2));

const after = await api(`/api/kbs/${kb.id}/search`, {
  method: 'POST',
  body: JSON.stringify({ query: 'RRF 融合排序', embedding: vec(3), mode: 'hybrid', nResults: 5 }),
});
console.log('after refresh:', JSON.stringify(after.results.map((r) => r.id)));
console.log('fulltext mode:', JSON.stringify((await api(`/api/kbs/${kb.id}/search`, {
  method: 'POST',
  body: JSON.stringify({ query: 'RRF', embedding: vec(3), mode: 'fulltext', nResults: 5 }),
})).results.map((r) => r.id)));

await api(`/api/kbs/${kb.id}`, { method: 'DELETE' });
console.log('deleted', kb.id, 'total', Date.now() - started, 'ms');
