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
 * production only after the embed pipeline (memory/embed.js — TBD) has
 * populated memory_chunks.
 *
 * Cost note: this issues one embedding call (text-embedding-3-small via
 * Anthropic-compatible Voyage or OpenAI; we already use OpenAI for some flows,
 * but the call site is encapsulated so we can switch).
 */

const https = require('https');
const { query } = require('../../database/postgres');

const ENABLED = (process.env.MEMORY_RETRIEVAL || '').toLowerCase() === 'on';
const TOP_K = parseInt(process.env.MEMORY_RETRIEVAL_K || '8', 10);
const EMBED_MODEL = process.env.MEMORY_EMBED_MODEL || 'text-embedding-3-small';

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
  const params = [workspace, JSON.stringify(embedding), limit];
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
 * Embed a single string. Uses OpenAI's embeddings endpoint by default
 * (text-embedding-3-small, 1536 dim — matches migration 054 schema).
 * Override with MEMORY_EMBED_PROVIDER=voyage if/when we move there.
 */
async function embed(text) {
  if ((process.env.MEMORY_EMBED_PROVIDER || 'openai') !== 'openai') {
    throw new Error('Only openai embed provider implemented; set MEMORY_EMBED_PROVIDER=openai');
  }
  if (!process.env.OPENAI_API_KEY) throw new Error('OPENAI_API_KEY not set');

  const body = JSON.stringify({ model: EMBED_MODEL, input: text.slice(0, 8000) });
  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: 'api.openai.com',
      path: '/v1/embeddings',
      method: 'POST',
      headers: {
        'authorization': `Bearer ${process.env.OPENAI_API_KEY}`,
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
          resolve(json.data?.[0]?.embedding || null);
        } catch (e) { reject(e); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('embed timed out')));
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

module.exports = { retrieveRelevant, isEnabled };
