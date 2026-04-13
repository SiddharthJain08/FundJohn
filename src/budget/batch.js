'use strict';

/**
 * Anthropic Batch API client — 50% cheaper for non-interactive scheduled operations.
 *
 * Usage:
 *   const batch = require('./batch');
 *   const jobId = await batch.submit(requests); // returns batchId
 *   const results = await batch.poll(batchId);  // blocks until complete
 *
 * Interactive (operator-triggered) calls MUST use the standard synchronous API.
 * Only use batch for: scheduled diligence scans, background research, screen runs.
 * NEVER use batch for equity-analyst (veto authority requires synchronous flow).
 */

const https = require('https');
const { query } = require('../database/postgres');

const BATCH_ELIGIBLE_TYPES = new Set(['research', 'data-prep', 'compute', 'report-builder']);
const BATCH_NEVER_TYPES    = new Set(['equity-analyst']); // veto requires sync

// Standard vs batch pricing per million tokens (Sonnet 4.6)
const PRICING = {
  standard: { input: 3.0, output: 15.0 },
  batch:    { input: 1.5, output:  7.5 },
};

function apiRequest(method, path, body = null) {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) throw new Error('ANTHROPIC_API_KEY not set');

  return new Promise((resolve, reject) => {
    const payload = body ? JSON.stringify(body) : null;
    const options = {
      hostname: 'api.anthropic.com',
      path,
      method,
      headers: {
        'x-api-key':         apiKey,
        'anthropic-version': '2023-06-01',
        'anthropic-beta':    'message-batches-2024-09-24',
        'content-type':      'application/json',
      },
    };
    if (payload) options.headers['content-length'] = Buffer.byteLength(payload);

    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', d => data += d);
      res.on('end', () => {
        try {
          resolve({ status: res.statusCode, body: JSON.parse(data) });
        } catch {
          resolve({ status: res.statusCode, body: data });
        }
      });
    });
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

/**
 * Submit a batch of requests to the Anthropic batch API.
 * @param {Array<{customId: string, model: string, maxTokens: number, messages: Array}>} requests
 * @returns {Promise<string>} batchId
 */
async function submit(requests) {
  const batchRequests = requests.map(r => ({
    custom_id: r.customId,
    params: {
      model:      r.model || 'claude-sonnet-4-6',
      max_tokens: r.maxTokens || 2000,
      messages:   r.messages,
    },
  }));

  const res = await apiRequest('POST', '/v1/messages/batches', { requests: batchRequests });
  if (res.status !== 200) throw new Error(`Batch submit failed: ${res.status} — ${JSON.stringify(res.body)}`);

  const batchId = res.body?.id;
  if (!batchId) throw new Error(`Batch submit returned no ID: ${JSON.stringify(res.body)}`);
  console.log(`[batch] Submitted ${requests.length} requests — batch ID: ${batchId}`);

  // Store in DB for tracking
  await query(
    `INSERT INTO pipeline_runs (ticker, run_type, status, records_written)
     VALUES ($1, $2, $3, $4)`,
    ['BATCH', `batch:${batchId}`, 'pending', 0]
  ).catch(() => null);

  return batchId;
}

/**
 * Poll until batch completes (or timeout). Returns results array.
 * @param {string} batchId
 * @param {number} pollIntervalMs
 * @param {number} timeoutMs
 */
async function poll(batchId, pollIntervalMs = 60_000, timeoutMs = 3_600_000) {
  const start = Date.now();
  console.log(`[batch] Polling ${batchId} every ${pollIntervalMs / 1000}s (timeout ${timeoutMs / 60000}m)`);

  while (Date.now() - start < timeoutMs) {
    const res = await apiRequest('GET', `/v1/messages/batches/${batchId}`);
    if (res.status !== 200) throw new Error(`Batch poll failed: ${res.status}`);

    const { processing_status, request_counts } = res.body;
    console.log(`[batch] ${batchId} status: ${processing_status} — ${JSON.stringify(request_counts)}`);

    if (processing_status === 'ended') {
      return await fetchResults(batchId);
    }
    await new Promise(r => setTimeout(r, pollIntervalMs));
  }
  throw new Error(`Batch ${batchId} timed out after ${timeoutMs / 60000}m`);
}

async function fetchResults(batchId) {
  const res = await apiRequest('GET', `/v1/messages/batches/${batchId}/results`);
  if (res.status !== 200) throw new Error(`Batch results fetch failed: ${res.status}`);

  // Results come as JSONL — parse each line
  const lines   = (typeof res.body === 'string' ? res.body : JSON.stringify(res.body)).split('\n').filter(Boolean);
  const results = lines.map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);

  console.log(`[batch] ${batchId} — ${results.length} results retrieved`);
  return results;
}

/**
 * Estimate batch savings for a set of subagent runs.
 * @param {Array<{inputTokens, outputTokens}>} runs
 */
function estimateSavings(runs) {
  const totalIn  = runs.reduce((s, r) => s + (r.inputTokens  || 0), 0);
  const totalOut = runs.reduce((s, r) => s + (r.outputTokens || 0), 0);
  const standard = (totalIn / 1e6) * PRICING.standard.input + (totalOut / 1e6) * PRICING.standard.output;
  const batch    = (totalIn / 1e6) * PRICING.batch.input    + (totalOut / 1e6) * PRICING.batch.output;
  return { standardUsd: standard, batchUsd: batch, savingsUsd: standard - batch, savingsPct: 50 };
}

module.exports = { submit, poll, fetchResults, estimateSavings, BATCH_ELIGIBLE_TYPES, BATCH_NEVER_TYPES };
