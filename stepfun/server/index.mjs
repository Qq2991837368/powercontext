import express from 'express';
import cors from 'cors';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  SeekdbClient,
  Schema,
  FulltextIndexConfig,
  VectorIndexConfig,
} from 'seekdb';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_PATH = process.env.SEEKDB_PATH || path.join(__dirname, 'data', 'seekdb');
// Embedded seekdb ships with the built-in `test` database; an explicit database
// must already exist, otherwise connect() returns null.
const DB_DATABASE = process.env.SEEKDB_DATABASE || 'test';
const PORT = Number(process.env.PORT || 8787);
const VECTOR_DIM = 512;
const COLLECTION_PREFIX = 'kb_';

fs.mkdirSync(DATA_PATH, { recursive: true });

const app = express();
app.use(cors());
app.use(express.json({ limit: '40mb' }));

const startedAt = Date.now();
const client = new SeekdbClient({ path: DATA_PATH, database: DB_DATABASE });

/**
 * Embedded seekdb uses a native single-instance engine. Concurrent requests
 * (React mounts fire /api/health and /api/kbs together) can race inside the
 * native layer and crash the process with 0xC0000005, so every database
 * operation goes through this serial queue.
 */
let dbQueue = Promise.resolve();
function withDb(task) {
  const run = dbQueue.then(task, task);
  dbQueue = run.then(
    () => undefined,
    () => undefined
  );
  return run;
}

// Serialize the whole API request lifecycle; responses are tiny, so the queue
// never becomes a throughput bottleneck at this scale.
app.use('/api', (req, res, next) => {
  withDb(
    () =>
      new Promise((resolve) => {
        res.on('finish', resolve);
        res.on('close', resolve);
        next();
      })
  ).catch(next);
});

function buildSchema() {
  return new Schema({
    fulltextIndex: new FulltextIndexConfig('ngram2', {
      min_ngram_size: 2,
      max_ngram_size: 3,
    }),
    vectorIndex: new VectorIndexConfig({
      hnsw: { dimension: VECTOR_DIM, distance: 'cosine' },
      embeddingFunction: null,
    }),
  });
}

function sanitizeCollectionName(name) {
  return String(name).toLowerCase().replace(/[^a-z0-9_]/g, '_').slice(0, 64);
}

/** Split a user query into full-text terms compatible with query_string syntax. */
function extractTerms(query, maxTerms = 8) {
  const cleaned = String(query || '')
    .replace(/[+\-!(){}\[\]^"~*?:\\/|&<>'`]/g, ' ')
    .replace(/[，。；、？！.,;?!]/g, ' ')
    .trim();
  if (!cleaned) return [];
  const parts = cleaned
    .split(/\s+/)
    .map((t) => t.trim())
    .filter((t) => t.length >= 2)
    .flatMap((t) => {
      // Long CJK runs are split into 2-4 char chunks so the ngram2 query stays selective.
      if (/[\u4e00-\u9fff]/.test(t) && t.length > 6) {
        const chunks = [];
        for (let i = 0; i < t.length; i += 3) {
          const c = t.slice(i, i + 3);
          if (c.length >= 2) chunks.push(c);
        }
        return chunks;
      }
      return [t];
    });
  return [...new Set(parts)].slice(0, maxTerms);
}

function buildWhereDocument(query) {
  const terms = extractTerms(query);
  if (!terms.length) return null;
  return { $or: terms.map((t) => ({ $contains: t })) };
}

function normalizeResults(raw) {
  const ids = raw?.ids?.[0] ?? [];
  const documents = raw?.documents?.[0] ?? [];
  const metadatas = raw?.metadatas?.[0] ?? [];
  const distances = raw?.distances?.[0] ?? [];
  return ids.map((id, i) => ({
    id,
    text: documents[i] ?? '',
    metadata: metadatas[i] ?? null,
    distance: typeof distances[i] === 'number' ? distances[i] : null,
  }));
}

async function kbInfo(col) {
  const info = await col.describe();
  const count = await col.count();
  const meta = info.metadata || {};
  return {
    id: col.name,
    name: meta.displayName || col.name,
    dtype: meta.dtype || 'unknown',
    model: meta.model || null,
    createdAt: meta.createdAt || null,
    count,
    dimension: info.dimension,
    distance: info.distance,
  };
}

app.get('/api/health', async (req, res, next) => {
  try {
    let collections = [];
    try {
      collections = (await client.listCollections({ withEmbeddingFunction: false }))
        .filter((c) => c.name.startsWith(COLLECTION_PREFIX));
    } catch (e) {
      collections = [];
    }
    res.json({
      ok: true,
      engine: 'seekdb',
      mode: 'embedded',
      path: DATA_PATH,
      database: DB_DATABASE,
      vectorDim: VECTOR_DIM,
      collectionCount: collections.length,
      uptimeMs: Date.now() - startedAt,
    });
  } catch (e) {
    next(e);
  }
});

app.get('/api/kbs', async (req, res, next) => {
  try {
    const collections = (await client.listCollections({ withEmbeddingFunction: false }))
      .filter((c) => c.name.startsWith(COLLECTION_PREFIX))
      .sort((a, b) => a.name.localeCompare(b.name));
    const infos = [];
    for (const col of collections) {
      infos.push(await kbInfo(col));
    }
    res.json({ kbs: infos });
  } catch (e) {
    next(e);
  }
});

app.post('/api/kbs', async (req, res, next) => {
  try {
    const { name, dtype = 'q8', model = null } = req.body || {};
    const stamp = Date.now().toString(36);
    const collectionName = sanitizeCollectionName(
      `${COLLECTION_PREFIX}${stamp}_${String(dtype).replace(/[^a-z0-9]/gi, '')}`
    );
    const displayName = String(name || `知识库 ${stamp}`).slice(0, 60);
    const col = await client.createCollection({
      name: collectionName,
      schema: buildSchema(),
      metadata: {
        displayName,
        dtype,
        model,
        createdAt: new Date().toISOString(),
      },
    });
    res.status(201).json({ kb: await kbInfo(col) });
  } catch (e) {
    next(e);
  }
});

app.get('/api/kbs/:id', async (req, res, next) => {
  try {
    const col = await client.getCollection({ name: req.params.id, embeddingFunction: null });
    res.json({ kb: await kbInfo(col) });
  } catch (e) {
    next(e);
  }
});

app.delete('/api/kbs/:id', async (req, res, next) => {
  try {
    await client.deleteCollection(req.params.id);
    res.json({ ok: true, id: req.params.id });
  } catch (e) {
    next(e);
  }
});

app.post('/api/kbs/:id/documents', async (req, res, next) => {
  try {
    const { documents = [], refresh = false } = req.body || {};
    if (!Array.isArray(documents) || documents.length === 0) {
      return res.status(400).json({ error: 'documents 不能为空' });
    }
    for (const d of documents) {
      if (!d.id || typeof d.text !== 'string' || !Array.isArray(d.embedding)) {
        return res.status(400).json({ error: '每条文档需要 id / text / embedding' });
      }
      if (d.embedding.length !== VECTOR_DIM) {
        return res.status(400).json({
          error: `向量维度错误：期望 ${VECTOR_DIM}，实际 ${d.embedding.length}`,
        });
      }
    }
    const col = await client.getCollection({ name: req.params.id, embeddingFunction: null });
    const t0 = performance.now();
    await col.upsert({
      ids: documents.map((d) => d.id),
      documents: documents.map((d) => d.text),
      embeddings: documents.map((d) => d.embedding),
      metadatas: documents.map((d) => d.metadata || {}),
    });
    const writeMs = performance.now() - t0;
    let refreshMs = null;
    if (refresh) {
      const t1 = performance.now();
      await col.refresh_index();
      refreshMs = performance.now() - t1;
    }
    const count = await col.count();
    res.json({ ok: true, written: documents.length, writeMs, refreshMs, count });
  } catch (e) {
    next(e);
  }
});

app.post('/api/kbs/:id/refresh', async (req, res, next) => {
  try {
    const col = await client.getCollection({ name: req.params.id, embeddingFunction: null });
    const t0 = performance.now();
    await col.refresh_index();
    const refreshMs = performance.now() - t0;
    const count = await col.count();
    res.json({ ok: true, refreshMs, count });
  } catch (e) {
    next(e);
  }
});

app.post('/api/kbs/:id/search', async (req, res, next) => {
  try {
    const {
      query = '',
      embedding,
      mode = 'hybrid',
      nResults = 8,
    } = req.body || {};
    if (!Array.isArray(embedding) || embedding.length !== VECTOR_DIM) {
      return res
        .status(400)
        .json({ error: `查询向量维度错误：期望 ${VECTOR_DIM} 维` });
    }
    const col = await client.getCollection({ name: req.params.id, embeddingFunction: null });
    const whereDocument = buildWhereDocument(query);
    const t0 = performance.now();
    let raw;
    if (mode === 'vector') {
      raw = await col.query({
        queryEmbeddings: embedding,
        nResults,
        include: ['documents', 'metadatas', 'distances'],
      });
    } else if (mode === 'fulltext') {
      raw = await col.hybridSearch({
        query: { whereDocument, nResults },
        nResults,
        include: ['documents', 'metadatas', 'distances'],
      });
    } else {
      raw = await col.hybridSearch({
        query: { whereDocument, nResults },
        knn: { queryEmbeddings: embedding, nResults },
        rank: { rrf: {} },
        nResults,
        include: ['documents', 'metadatas', 'distances'],
      });
    }
    const searchMs = performance.now() - t0;
    res.json({
      ok: true,
      mode,
      terms: extractTerms(query),
      searchMs,
      results: normalizeResults(raw),
    });
  } catch (e) {
    next(e);
  }
});

app.get('/api/kbs/:id/documents/:docId', async (req, res, next) => {
  try {
    const col = await client.getCollection({ name: req.params.id, embeddingFunction: null });
    const result = await col.get({ ids: [req.params.docId], include: ['documents', 'metadatas'] });
    if (!result.ids.length) return res.status(404).json({ error: 'document not found' });
    res.json({
      id: result.ids[0],
      text: result.documents?.[0] ?? '',
      metadata: result.metadatas?.[0] ?? null,
    });
  } catch (e) {
    next(e);
  }
});

// Static build (production)
const distDir = path.join(__dirname, '..', 'web', 'dist');
if (fs.existsSync(distDir)) {
  app.use(express.static(distDir));
  app.use((req, res, next) => {
    if (req.path.startsWith('/api/')) return next();
    res.sendFile(path.join(distDir, 'index.html'));
  });
}

app.use((err, req, res, next) => {
  console.error('[stepfun] API error:', err?.message || err);
  res.status(500).json({ error: err?.message || String(err) });
});

app.listen(PORT, () => {
  console.log(`[stepfun] seekdb embedded server listening on http://localhost:${PORT}`);
  console.log(`[stepfun] seekdb data path: ${DATA_PATH}`);
});
