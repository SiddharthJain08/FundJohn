'use strict';
const assert = require('assert');
const { formatIngestionSummary } = require('../src/pipeline/run_collector_once.js');

const sodBody = formatIngestionSummary('SOD', {
  ok: true,
  elapsed_s: 412,
  date: '2026-05-29',
  phases: [
    { phase: 'prices',       rows: 426 },
    { phase: 'options',      rows: 18 },
    { phase: 'fundamentals', rows: 51 },
    { phase: 'news',         rows: 240 },
    { phase: 'sentiment',    rows: 79 },
  ],
});
assert(sodBody.includes('Start-of-day ingestion'), 'has SOD header');
assert(sodBody.includes('prices') && sodBody.includes('426'), 'lists prices rows');
assert(sodBody.includes('sentiment') && sodBody.includes('79'), 'lists every phase');
const phaseBullets = sodBody.split('\n').filter(l => l.trim().startsWith('•'));
assert.strictEqual(phaseBullets.length, 5, 'one bullet per phase (full breakdown, not last-15 truncation)');

const failBody = formatIngestionSummary('SOD', { ok: false, date: '2026-05-29' });
assert(failBody.includes('FAILED'), 'failure body marked FAILED');

console.log('OK test_sod_ingestion_summary');
