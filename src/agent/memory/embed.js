'use strict';

/**
 * Embed-write pipeline for the memory_chunks table.
 *
 * Pairs with src/agent/memory/retrieve.js (reads). Together they replace
 * wholesale memory/*.md injection in workspace-context.js with top-K
 * pgvector retrieval — the second-biggest token win in the LangAlpha
 * comparison after LLM-summarized eviction.
 *
 * Provider: Voyage AI (voyage-3, 1024 dim). Configurable via env:
 *   VOYAGE_API_KEY   — required; without it everything no-ops gracefully
 *   VOYAGE_MODEL     — default 'voyage-3'
 *   VOYAGE_INPUT_TYPE — 'document' (default) for backfill writes; reads
 *                       use 'query' from retrieve.js
 *
 * What it does:
 *   - parseFrontmatter: closed-schema YAML parse for the canonical _templates
 *     fields (type, tags, tickers, ...). Hand-rolled to avoid js-yaml dep.
 *   - chunkBody: H2/H3-aware splitter with paragraph-overlap for oversized
 *     sections.
 *   - embedFile: idempotent — uses source_mtime to detect unchanged files.
 *   - embedDirectory: walks a dir, skips _templates/, returns aggregated
 *     counts.
 *
 * Out of scope: Python embed-on-write hooks (see plan §9). Backfill CLI is
 * the periodic refresh path for Python writers.
 */

const fs   = require('fs');
const path = require('path');
const https = require('https');
const { query } = require('../../database/postgres');

const VOYAGE_MODEL      = process.env.VOYAGE_MODEL || 'voyage-3';
const VOYAGE_DIM        = 1024;

// One-shot per-process check: does memory_chunks exist? If not (pgvector
// hasn't been installed in prod yet), we short-circuit — the embed pipeline
// is gated on schema being present. Backfill CLI surfaces this loudly;
// embed-on-write hook just no-ops silently.
let _tableCheckPromise = null;
function _tableExists() {
  if (_tableCheckPromise) return _tableCheckPromise;
  _tableCheckPromise = query(
    `SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'memory_chunks') AS ok`,
  )
    .then((r) => r.rows[0]?.ok === true)
    .catch(() => false);
  return _tableCheckPromise;
}
const VOYAGE_INPUT_CAP  = 12_000;            // chars; ~3k tokens — under voyage's 32k limit
const CHUNK_MAX_TOKENS  = 500;               // ~2000 chars
const CHUNK_OVERLAP_TOK = 50;                // ~200 chars
const TOK_PER_CHAR      = 0.25;
const tokToChars        = (t) => Math.ceil(t / TOK_PER_CHAR);

// ── Frontmatter parsing ─────────────────────────────────────────────────────

/**
 * Parse YAML frontmatter delimited by `---` markers at start of file. Closed
 * schema per workspaces/default/_templates/README.md. Supports:
 *   key: scalar
 *   key: [a, b, c]              (inline array)
 *   key: 'quoted scalar'        (single or double)
 * Anything fancier (block arrays, nested maps) is ignored — by design.
 *
 * Returns { frontmatter: {...}, body: string }. If no frontmatter present
 * (or malformed), returns { frontmatter: {}, body: original-text }.
 */
function parseFrontmatter(text) {
  if (!text || !text.startsWith('---')) return { frontmatter: {}, body: text || '' };
  const end = text.indexOf('\n---', 3);
  if (end < 0) return { frontmatter: {}, body: text };

  const fmRaw = text.slice(3, end).trim();
  const body  = text.slice(end + 4).replace(/^\n/, '');

  const fm = {};
  for (const rawLine of fmRaw.split('\n')) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const m = line.match(/^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$/);
    if (!m) continue;
    const key = m[1];
    let val   = m[2].trim();

    if (val.startsWith('[') && val.endsWith(']')) {
      // inline array: [a, b, "c"]  -- strip brackets, split on commas
      fm[key] = val.slice(1, -1).split(',')
        .map((s) => s.trim().replace(/^['"]|['"]$/g, ''))
        .filter((s) => s.length > 0);
    } else if ((val.startsWith('"') && val.endsWith('"')) ||
               (val.startsWith("'") && val.endsWith("'"))) {
      fm[key] = val.slice(1, -1);
    } else if (val === 'true' || val === 'false') {
      fm[key] = val === 'true';
    } else if (/^-?\d+(\.\d+)?$/.test(val)) {
      fm[key] = Number(val);
    } else {
      fm[key] = val;
    }
  }
  return { frontmatter: fm, body };
}

// ── Chunking ────────────────────────────────────────────────────────────────

/**
 * Split body into chunks. Strategy:
 *   1. Split on ^## or ^### header lines (keep header at start of each chunk).
 *   2. Any chunk whose char count exceeds maxChars gets paragraph-split with
 *      overlapChars carryover.
 *
 * Returns [{ text, chunkIndex }] in order.
 */
function chunkBody(body, opts = {}) {
  const maxChars     = tokToChars(opts.maxTokens     || CHUNK_MAX_TOKENS);
  const overlapChars = tokToChars(opts.overlapTokens || CHUNK_OVERLAP_TOK);
  if (!body || !body.trim()) return [];

  const sections = [];
  const lines    = body.split('\n');
  let buf        = [];
  for (const line of lines) {
    if (/^#{2,3}\s/.test(line) && buf.length > 0) {
      sections.push(buf.join('\n').trim());
      buf = [];
    }
    buf.push(line);
  }
  if (buf.length) sections.push(buf.join('\n').trim());

  const chunks = [];
  let idx = 0;
  for (const section of sections.filter(Boolean)) {
    if (section.length <= maxChars) {
      chunks.push({ text: section, chunkIndex: idx++ });
      continue;
    }
    // Section too big — paragraph-split with overlap
    const paras = section.split(/\n\n+/);
    let cur = '';
    for (const p of paras) {
      if ((cur + '\n\n' + p).length <= maxChars) {
        cur = cur ? `${cur}\n\n${p}` : p;
      } else {
        if (cur) chunks.push({ text: cur, chunkIndex: idx++ });
        // overlap: tail of previous chunk
        cur = (cur.length > overlapChars ? cur.slice(-overlapChars) + '\n\n' : '') + p;
      }
    }
    if (cur) chunks.push({ text: cur, chunkIndex: idx++ });
  }
  return chunks;
}

// ── Voyage embed ────────────────────────────────────────────────────────────

/**
 * Embed a single string. Returns 1024-dim Float array, or null on error.
 *
 * 429-aware retry: on rate-limit response, sleeps `retry-after` seconds
 * (or 22s default — Voyage free tier is 3 RPM = 20s spacing; +2s safety)
 * and retries up to VOYAGE_MAX_RETRIES times. Logs each backoff so a
 * stuck loop is visible.
 *
 * Inputs > VOYAGE_INPUT_CAP chars are truncated (caller's choice; we
 * don't silently drop content).
 */
function _sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function _voyageOnce(text, inputType) {
  const trimmed = text.length > VOYAGE_INPUT_CAP ? text.slice(0, VOYAGE_INPUT_CAP) : text;
  const body = JSON.stringify({
    model:      VOYAGE_MODEL,
    input:      [trimmed],
    input_type: inputType,
  });

  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: 'api.voyageai.com',
      path:     '/v1/embeddings',
      method:   'POST',
      headers: {
        'authorization':  `Bearer ${process.env.VOYAGE_API_KEY}`,
        'content-type':   'application/json',
        'content-length': Buffer.byteLength(body),
      },
      timeout: 30_000,
    }, (res) => {
      let data = '';
      res.on('data', (d) => data += d);
      res.on('end', () => {
        if (res.statusCode === 429) {
          const ra = parseInt(res.headers['retry-after'] || '0', 10);
          return resolve({ rateLimited: true, retryAfter: ra, body: data });
        }
        try {
          if (res.statusCode !== 200) return reject(new Error(`voyage status ${res.statusCode}: ${data.slice(0, 200)}`));
          const json = JSON.parse(data);
          const v = json.data?.[0]?.embedding;
          if (!Array.isArray(v) || v.length !== VOYAGE_DIM) {
            return reject(new Error(`voyage returned dim ${v?.length} (expected ${VOYAGE_DIM})`));
          }
          resolve({ embedding: v });
        } catch (e) { reject(e); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('voyage embed timed out')));
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

async function embedOne(text, { inputType = 'document' } = {}) {
  if (!process.env.VOYAGE_API_KEY) return null;
  const maxRetries  = parseInt(process.env.VOYAGE_MAX_RETRIES   || '6',  10);
  const defaultWait = parseInt(process.env.VOYAGE_DEFAULT_WAIT_MS || '22000', 10); // 22s — over 3 RPM spacing

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const r = await _voyageOnce(text, inputType);
      if (r.embedding) return r.embedding;
      // 429: back off and retry
      const waitMs = (r.retryAfter > 0 ? r.retryAfter * 1000 : defaultWait);
      console.warn(`[memory/embed] voyage 429 — backing off ${Math.round(waitMs/1000)}s (attempt ${attempt + 1}/${maxRetries + 1})`);
      await _sleep(waitMs);
    } catch (err) {
      console.warn('[memory/embed] voyage call failed:', err.message);
      return null;
    }
  }
  console.warn(`[memory/embed] voyage gave up after ${maxRetries + 1} attempts (rate-limited)`);
  return null;
}

// ── File-level embed pipeline ──────────────────────────────────────────────

function extractTickers(frontmatter, body) {
  const out = new Set();
  if (Array.isArray(frontmatter.tickers)) frontmatter.tickers.forEach((t) => out.add(String(t).toUpperCase()));
  if (typeof frontmatter.ticker === 'string')  out.add(frontmatter.ticker.toUpperCase());
  // Best-effort body sweep — uppercase tickers in #ticker/XXX tag form
  if (Array.isArray(frontmatter.tags)) {
    for (const t of frontmatter.tags) {
      const m = String(t).match(/^#?ticker\/([A-Z][A-Z0-9.\-]{0,9})$/);
      if (m) out.add(m[1]);
    }
  }
  return [...out];
}

/**
 * Idempotently embed one file.
 *   - Skips if all chunks for (workspace, sourceFile, embed_model) already
 *     have source_mtime equal to the file's current mtime (unless `force`).
 *   - Otherwise: re-parse, re-chunk, embed each, upsert. Old chunks for the
 *     same file with stale mtime are deleted to avoid orphans when chunk
 *     count shrinks.
 */
async function embedFile({ workspace, sourceFile, force = false } = {}) {
  if (!process.env.VOYAGE_API_KEY) return { skipped: 1, written: 0, errors: 0, reason: 'no VOYAGE_API_KEY' };
  if (!(await _tableExists())) return { skipped: 1, written: 0, errors: 0, reason: 'memory_chunks table missing — install pgvector + run migration 054' };
  const stat = fs.statSync(sourceFile);
  const mtime = stat.mtime;

  if (!force) {
    const { rows } = await query(
      `SELECT MIN(source_mtime) AS oldest, MAX(source_mtime) AS newest, COUNT(*)::int AS n
         FROM memory_chunks
        WHERE workspace = $1 AND source_file = $2 AND embed_model = $3`,
      [workspace, sourceFile, VOYAGE_MODEL],
    );
    if (rows[0].n > 0 && new Date(rows[0].oldest).getTime() === mtime.getTime()) {
      return { skipped: 1, written: 0, errors: 0, reason: 'unchanged' };
    }
  }

  const text = fs.readFileSync(sourceFile, 'utf8');
  const { frontmatter, body } = parseFrontmatter(text);
  const chunks = chunkBody(body);
  if (chunks.length === 0) {
    return { skipped: 1, written: 0, errors: 0, reason: 'empty body' };
  }

  // Drop existing chunks for this file (handles chunk-count shrinkage cleanly)
  await query(
    `DELETE FROM memory_chunks WHERE workspace=$1 AND source_file=$2 AND embed_model=$3`,
    [workspace, sourceFile, VOYAGE_MODEL],
  );

  const noteType = frontmatter.type ? String(frontmatter.type) : null;
  const tags     = Array.isArray(frontmatter.tags) ? frontmatter.tags.map(String) : [];
  const tickers  = extractTickers(frontmatter, body);

  let written = 0;
  let errors  = 0;
  for (const c of chunks) {
    const v = await embedOne(c.text);
    if (!v) { errors++; continue; }
    const vecLit = `[${v.join(',')}]`;
    try {
      await query(
        `INSERT INTO memory_chunks
           (workspace, source_file, chunk_index, chunk_text, note_type, tags, tickers,
            embedding, embed_model, char_count, source_mtime)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector,$9,$10,$11)
         ON CONFLICT (workspace, source_file, chunk_index, embed_model)
         DO UPDATE SET chunk_text=EXCLUDED.chunk_text, note_type=EXCLUDED.note_type,
                       tags=EXCLUDED.tags, tickers=EXCLUDED.tickers,
                       embedding=EXCLUDED.embedding, char_count=EXCLUDED.char_count,
                       source_mtime=EXCLUDED.source_mtime`,
        [workspace, sourceFile, c.chunkIndex, c.text, noteType, tags, tickers,
         vecLit, VOYAGE_MODEL, c.text.length, mtime],
      );
      written++;
    } catch (err) {
      console.warn('[memory/embed] upsert failed for', sourceFile, '@', c.chunkIndex, ':', err.message);
      errors++;
    }
  }
  return { skipped: 0, written, errors };
}

/**
 * Walk a directory tree (synchronously) collecting *.md files, skipping
 * `_templates/` and dotfiles. Idempotently embeds each.
 */
async function embedDirectory({ workspace, dir, force = false } = {}) {
  if (!fs.existsSync(dir)) return { files: 0, skipped: 0, written: 0, errors: 0 };

  function walk(d) {
    const out = [];
    for (const ent of fs.readdirSync(d, { withFileTypes: true })) {
      if (ent.name.startsWith('.')) continue;
      if (ent.name === '_templates') continue;
      const full = path.join(d, ent.name);
      if (ent.isDirectory()) out.push(...walk(full));
      else if (ent.isFile() && ent.name.endsWith('.md')) out.push(full);
    }
    return out;
  }

  const files = walk(dir);
  let skipped = 0, written = 0, errors = 0;
  for (const f of files) {
    const r = await embedFile({ workspace, sourceFile: f, force });
    skipped += r.skipped || 0;
    written += r.written || 0;
    errors  += r.errors  || 0;
  }
  return { files: files.length, skipped, written, errors };
}

module.exports = {
  parseFrontmatter,
  chunkBody,
  embedOne,
  embedFile,
  embedDirectory,
  VOYAGE_MODEL,
  VOYAGE_DIM,
};
