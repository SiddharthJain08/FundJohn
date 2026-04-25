'use strict';

/**
 * pgvector retrieval for workspace memory.
 *
 * Replaces the wholesale memory/*.md injection in workspace-context.js.
 * Given a query string + caller context, returns the top-K relevant chunks
 * across all embedded memory files. Pre-filters on note_type / tags / tickers
 * when the caller can provide them — this is what gives us the big token win
 * over flat retrieval.
 *
 * Feature-flagged on MEMORY_RETRIEVAL=on. When off, returns null and the
 * middleware falls back to the legacy wholesale-injection path. Enable in
 * production only after the embed pipeline (memory/embed.js) has populated
 * memory_chunks.
 *
 * Provider: Voyage AI (voyage-3, 1024 dim — matches migration 054). Same
 * call shape as memory/embed.js. Read calls use input_type=query while
 * embed/write calls use input_type=document — Voyage tunes the projection
 * for asymmetric retrieval.
 */

const https = require('https');
const { query } = require('../../database/postgres');

const ENABLED = (process.env.MEMORY_RETRIEVAL || '').toLowerCase() === 'on';
const TOP_K = parseInt(process.env.MEMORY_RETRIEVAL_K || '8', 10);
const EMBED_MODEL = process.env.VOYAGE_MODEL || 'voyage-3';
const VOYAGE_DIM  = 1024;

function isEnabled() { return ENABLED; }

/**
 * Return top-K relevant memory chunks for a query.
 *
 * @param {object} args
 * @param {string} args.workspace        Absolute workspace path (matches embed write key)
 * @param {string} args.queryText        Free text the agent's about to think about
 * @param {string[]} [args.noteTypes]    Optional pre-filter: paper/strategy/position/...
 * @param {string[]} [args.tags]         Optional pre-filter on tag overlap
 * @param {string[]} [args.tickers]      Optional pre-filter on ticker overlap
 * @param {number}   [args.k]            Override TOP_K
 * @returns {Promise<Array<{source_file, chunk_text, note_type, tags, tickers, score}>|null>}
 *          Null when feature flag is off or embed call fails — caller must fall back.
 */
async function retrieveRelevant({ workspace, queryText, noteTypes, tags, tickers, k }) {
  if (!ENABLED) return null;
  if (!queryText || !workspace) return null;

  const embedding = await embed(queryText).catch((err) => {
    console.warn('[memory/retrieve] embed failed:', err.message);
    return null;
  });
  if (!embedding) return null;

  const limit = k || TOP_K;
  const vecLit = `[${embedding.join(',')}]`;
  const params = [workspace, vecLit, limit];
  let where = 'WHERE workspace = $1';
  if (noteTypes && noteTypes.length) { params.push(noteTypes); where += ` AND note_type = ANY($${params.length})`; }
  if (tags && tags.length)           { params.push(tags);      where += ` AND tags && $${params.length}`; }
  if (tickers && tickers.length)     { params.push(tickers);   where += ` AND tickers && $${params.length}`; }

  try {
    const { rows } = await query(
      `SELECT source_file, chunk_text, note_type, tags, tickers,
              1 - (embedding <=> $2::vector) AS score
       FROM memory_chunks
       ${where}
       ORDER BY embedding <=> $2::vector
       LIMIT $3`,
      params,
    );
    return rows;
  } catch (err) {
    console.warn('[memory/retrieve] query failed:', err.message);
    return null;
  }
}

/**
 * Embed a single query string via Voyage AI (voyage-3, 1024 dim).
 * Uses input_type='query' (vs 'document' on the embed/write side) — Voyage
 * tunes the projection for asymmetric retrieval.
 */
async function embed(text) {
  if (!process.env.VOYAGE_API_KEY) throw new Error('VOYAGE_API_KEY not set');

  const body = JSON.stringify({
    model:      EMBED_MODEL,
    input:      [text.slice(0, 12_000)],
    input_type: 'query',
  });
  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: 'api.voyageai.com',
      path: '/v1/embeddings',
      method: 'POST',
      headers: {
        'authorization': `Bearer ${process.env.VOYAGE_API_KEY}`,
        'content-type':  'application/json',
        'content-length': Buffer.byteLength(body),
      },
      timeout: 15_000,
    }, (res) => {
      let data = '';
      res.on('data', (d) => data += d);
      res.on('end', () => {
        try {
          if (res.statusCode !== 200) return reject(new Error(`status ${res.statusCode}: ${data.slice(0, 200)}`));
          const json = JSON.parse(data);
          const v = json.data?.[0]?.embedding;
          if (!Array.isArray(v) || v.length !== VOYAGE_DIM) {
            return reject(new Error(`voyage returned dim ${v?.length} (expected ${VOYAGE_DIM})`));
          }
          resolve(v);
        } catch (e) { reject(e); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('embed timed out')));
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

/**
 * Count chunks in a workspace — used by the runbook to verify the table is
 * populated before flipping MEMORY_RETRIEVAL=on. Returns 0 when table is
 * absent (pgvector not yet installed) so callers don't have to special-case.
 */
async function getChunkCount({ workspace } = {}) {
  try {
    const params = workspace ? [workspace] : [];
    const where  = workspace ? 'WHERE workspace = $1' : '';
    const { rows } = await query(`SELECT COUNT(*)::int AS n FROM memory_chunks ${where}`, params);
    return rows[0]?.n || 0;
  } catch (err) {
    if (/relation "memory_chunks" does not exist/i.test(err.message)) return 0;
    console.warn('[memory/retrieve] getChunkCount failed:', err.message);
    return 0;
  }
}

module.exports = { retrieveRelevant, isEnabled, getChunkCount };
