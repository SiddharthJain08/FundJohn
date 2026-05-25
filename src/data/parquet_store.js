'use strict';

/**
 * parquet_store.js — Node.js facade for OpenClaw master parquet reads and writes.
 *
 * Strategy: delegate all parquet I/O to the Python CLI (src/data/parquet_reader.py).
 * This keeps a single canonical reader/writer (parquet_store.py) used by both the
 * Python execution engine and the Node collector/dashboard, eliminating schema
 * drift between languages.
 *
 * For read operations with large result sets we stream stdout directly; for
 * write operations with large payloads (e.g., 5000 options rows) we pass the
 * JSON via a temp file to avoid argv size limits.
 *
 * A small LRU cache in front of reads absorbs the ~100ms Python cold-start
 * penalty for high-frequency dashboard endpoints (market-overview @ 60s TTL).
 */

const { spawn } = require('child_process');
const path      = require('path');
const fs        = require('fs');
const os        = require('os');
const crypto    = require('crypto');

const ROOT         = path.resolve(__dirname, '..', '..');
const CLI          = path.join(ROOT, 'src', 'data', 'parquet_reader.py');
const PYTHON       = process.env.PYTHON_BIN || 'python3';
const PYTHONPATH   = ROOT;
const ARGV_LIMIT   = 100 * 1024;   // 100KB safety margin vs ~2MB argv cap

// LRU cache: key → { value, expiresAt }
const CACHE        = new Map();
const CACHE_MAX    = 128;

// TTL per op (seconds). 0 = no cache.
const TTL_SECONDS = {
  'market-overview':  60,
  'market_overview':  60,
  'freshness':        15,
  'latest_snapshots': 30,
  'latest-snapshots': 30,
  'prices':           5,
  'options':          30,
  'fundamentals':     300,
  'insider':          300,
  'macro':            30,
  'chart':            30,
};


function _cacheKey(op, args) {
  return op + ':' + JSON.stringify(args);
}

function _getCached(op, args) {
  const ttl = TTL_SECONDS[op];
  if (!ttl) return null;
  const key = _cacheKey(op, args);
  const hit = CACHE.get(key);
  if (!hit) return null;
  if (hit.expiresAt < Date.now()) {
    CACHE.delete(key);
    return null;
  }
  // LRU: re-insert to move to end
  CACHE.delete(key);
  CACHE.set(key, hit);
  return hit.value;
}

function _setCached(op, args, value) {
  const ttl = TTL_SECONDS[op];
  if (!ttl) return;
  if (CACHE.size >= CACHE_MAX) {
    const firstKey = CACHE.keys().next().value;
    CACHE.delete(firstKey);
  }
  CACHE.set(_cacheKey(op, args), {
    value,
    expiresAt: Date.now() + ttl * 1000,
  });
}


function _runPython(op, args, timeoutMs = 30000) {
  const argsJson = JSON.stringify(args || {});
  const useFile  = argsJson.length > ARGV_LIMIT;

  return new Promise((resolve, reject) => {
    const cliArgs = ['--op', op];
    let tmpPath = null;

    if (useFile) {
      tmpPath = path.join(os.tmpdir(),
        `parquet_args_${process.pid}_${crypto.randomBytes(6).toString('hex')}.json`);
      fs.writeFileSync(tmpPath, argsJson);
      cliArgs.push('--args-file', tmpPath);
    } else {
      cliArgs.push('--args', argsJson);
    }

    const proc = spawn(PYTHON, [CLI, ...cliArgs], {
      cwd: ROOT,
      env: { ...process.env, PYTHONPATH },
    });

    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      proc.kill('SIGKILL');
      reject(new Error(`parquet op ${op} timed out after ${timeoutMs}ms`));
    }, timeoutMs);

    proc.stdout.on('data', (b) => { stdout += b.toString(); });
    proc.stderr.on('data', (b) => { stderr += b.toString(); });

    proc.on('close', (code) => {
      clearTimeout(timer);
      if (tmpPath) { try { fs.unlinkSync(tmpPath); } catch (_) {} }
      if (code !== 0) {
        return reject(new Error(`parquet op ${op} exited ${code}: ${stderr.trim()}`));
      }
      try {
        resolve(stdout.trim() ? JSON.parse(stdout) : null);
      } catch (e) {
        reject(new Error(`parquet op ${op} returned non-JSON: ${stdout.slice(0, 200)}`));
      }
    });

    proc.on('error', (err) => {
      clearTimeout(timer);
      if (tmpPath) { try { fs.unlinkSync(tmpPath); } catch (_) {} }
      reject(err);
    });
  });
}


async function readParquet(op, args = {}) {
  const cached = _getCached(op, args);
  if (cached !== null) return cached;
  const result = await _runPython(op, args);
  _setCached(op, args, result);
  return result;
}


// ── Named writers used by the collector ─────────────────────────────────────

function _invalidateCache(prefix) {
  for (const key of Array.from(CACHE.keys())) {
    if (key.startsWith(prefix)) CACHE.delete(key);
  }
}

async function writePrices(rows) {
  if (!rows || !rows.length) return 0;
  const r = await _runPython('write_prices', { rows });
  _invalidateCache('prices:');
  _invalidateCache('market-overview:');
  _invalidateCache('market_overview:');
  _invalidateCache('latest_snapshots:');
  _invalidateCache('latest-snapshots:');
  _invalidateCache('chart:');
  _invalidateCache('freshness:');
  return r.rows_after;
}

async function writeOptions(rows) {
  if (!rows || !rows.length) return 0;
  const r = await _runPython('write_options', { rows });
  _invalidateCache('options:');
  _invalidateCache('freshness:');
  return r.rows_after;
}

async function writeFundamentals(rows) {
  if (!rows || !rows.length) return 0;
  const r = await _runPython('write_fundamentals', { rows });
  _invalidateCache('fundamentals:');
  _invalidateCache('freshness:');
  return r.rows_after;
}

async function writeInsider(rows) {
  if (!rows || !rows.length) return 0;
  const r = await _runPython('write_insider', { rows });
  _invalidateCache('insider:');
  _invalidateCache('freshness:');
  return r.rows_after;
}

async function writeMacro(rows) {
  if (!rows || !rows.length) return 0;
  const r = await _runPython('write_macro', { rows });
  _invalidateCache('macro:');
  _invalidateCache('freshness:');
  return r.rows_after;
}

function clearCache() {
  CACHE.clear();
}


module.exports = {
  readParquet,
  writePrices,
  writeOptions,
  writeFundamentals,
  writeInsider,
  writeMacro,
  clearCache,
};
