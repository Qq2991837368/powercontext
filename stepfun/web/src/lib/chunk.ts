export interface Chunk {
  text: string;
  source: string;
  chunkIndex: number;
}

interface ChunkOptions {
  size?: number;
  overlap?: number;
  source?: string;
}

/** Split text on paragraph / sentence boundaries with a fixed character budget and overlap. */
export function chunkText(text: string, options: ChunkOptions = {}): Chunk[] {
  const size = options.size ?? 320;
  const overlap = Math.min(options.overlap ?? 64, Math.floor(size / 2));
  const source = options.source ?? '手动导入';
  const normalized = text.replace(/\r\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  if (!normalized) return [];

  const paragraphs = normalized.split(/\n+/).map((p) => p.trim()).filter(Boolean);
  const chunks: string[] = [];

  for (const paragraph of paragraphs) {
    if (paragraph.length <= size) {
      chunks.push(paragraph);
      continue;
    }
    // Split long paragraphs into sentences first, then pack them into windows.
    const sentences = paragraph
      .split(/(?<=[。！？!?；;])/)
      .map((s) => s.trim())
      .filter(Boolean);
    let buffer = '';
    for (const sentence of sentences) {
      if (buffer.length + sentence.length > size && buffer) {
        chunks.push(buffer);
        buffer = overlap > 0 ? buffer.slice(-overlap) : '';
      }
      buffer += sentence;
      while (buffer.length > size) {
        chunks.push(buffer.slice(0, size));
        buffer = buffer.slice(size - overlap);
      }
    }
    if (buffer.trim()) chunks.push(buffer.trim());
  }

  return chunks.map((chunk, chunkIndex) => ({
    text: chunk,
    source,
    chunkIndex,
  }));
}

export function charCount(chunks: Chunk[]): number {
  return chunks.reduce((sum, c) => sum + c.text.length, 0);
}
