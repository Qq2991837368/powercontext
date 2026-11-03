#!/usr/bin/env node
/**
 * Download Xenova/bge-small-zh-v1.5 (ONNX) into web/public/models so the demo can
 * run fully offline. Files are gitignored; run `npm run fetch:model` once.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const host = process.env.HF_ENDPOINT || 'https://hf-mirror.com';
const model = 'Xenova/bge-small-zh-v1.5';
const target = path.join(root, 'web', 'public', 'models', ...model.split('/'));

const dtypeArg = process.argv.find((arg) => arg.startsWith('--dtype='))?.split('=')[1] || 'all';
const weights = [
  { file: 'onnx/model_quantized.onnx', dtype: 'q8' },
  { file: 'onnx/model.onnx', dtype: 'fp32' },
];
const common = [
  'config.json',
  'tokenizer.json',
  'tokenizer_config.json',
  'special_tokens_map.json',
];

async function download(file) {
  const destination = path.join(target, file);
  if (fs.existsSync(destination) && fs.statSync(destination).size > 0) {
    console.log(`skip   ${file} (${fs.statSync(destination).size} bytes)`);
    return;
  }
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const url = `${host}/${model}/resolve/main/${file}`;
  console.log(`fetch  ${url}`);
  const res = await fetch(url, { redirect: 'follow' });
  if (!res.ok) throw new Error(`${file}: HTTP ${res.status}`);
  const buffer = Buffer.from(await res.arrayBuffer());
  fs.writeFileSync(destination, buffer);
  console.log(`saved  ${file} (${buffer.length} bytes)`);
}

const files = [
  ...common,
  ...weights.filter((w) => dtypeArg === 'all' || w.dtype === dtypeArg).map((w) => w.file),
];

for (const file of files) {
  await download(file);
}

const total = files.reduce((sum, file) => {
  const p = path.join(target, file);
  return fs.existsSync(p) ? sum + fs.statSync(p).size : sum;
}, 0);
console.log(`\nDone: ${target}`);
console.log(`Total bytes: ${total} (${(total / 1024 / 1024).toFixed(2)} MiB)`);
