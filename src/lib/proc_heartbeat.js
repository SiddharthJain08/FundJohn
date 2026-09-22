'use strict';
/**
 * proc_heartbeat.js — JS twin of src/lib/proc_heartbeat.py (QD spec §5 E3).
 *
 * Same key (`proc:<host>:<pid>`), same seven string fields, same TTL default,
 * so `proc_registry` and the doctor read one shape regardless of which runtime
 * wrote the entry. Never throws: diagnostics must not break a caller.
 */
const fs = require('node:fs');
const os = require('node:os');

const KEY_PREFIX      = 'proc';
const DEFAULT_TTL_SEC = 180;

function heartbeatKey({ host, pid } = {}) {
  const h = host || os.hostname();
  const p = pid == null ? process.pid : Number(pid);
  return `${KEY_PREFIX}:${h}:${p}`;
}

function rssMb(pid) {
  const p = pid == null ? process.pid : Number(pid);
  try {
    const residentPages = Number(fs.readFileSync(`/proc/${p}/statm`, 'utf8').split(/\s+/)[1]);
    if (!Number.isFinite(residentPages)) return null;
    // 4096 = getconf PAGESIZE on this box (verified; matches the Python
    // twin's os.sysconf('SC_PAGE_SIZE') — Node has no portable equivalent).
    return Math.round((residentPages * 4096) / (1024 * 1024) * 10) / 10;
  } catch (_) {
    return null;
  }
}

async function writeHeartbeat(r, { step, argv, pid, host, startedAt, ttlSec } = {}) {
  if (!r) return null;
  const key = heartbeatKey({ host, pid });
  const now = new Date().toISOString();
  const resident = rssMb(pid);
  const fields = {
    host:       String(host || os.hostname()),
    pid:        String(pid == null ? process.pid : Number(pid)),
    step:       String(step || ''),
    argv:       (argv || []).map(String).join(' ').slice(0, 500),
    rss_mb:     resident == null ? '' : String(resident),
    started_at: String(startedAt || now),
    updated_at: now,
  };
  try {
    await r.hset(key, fields);
    await r.expire(key, Math.trunc(Number(ttlSec) || DEFAULT_TTL_SEC));
    return key;
  } catch (_) {
    return null;
  }
}

async function clearHeartbeat(r, { pid, host } = {}) {
  if (!r) return;
  try { await r.del(heartbeatKey({ host, pid })); } catch (_) { /* diagnostics only */ }
}

module.exports = { KEY_PREFIX, DEFAULT_TTL_SEC, heartbeatKey, rssMb, writeHeartbeat, clearHeartbeat };
