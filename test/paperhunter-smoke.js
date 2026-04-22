/**
 * Smoke test: paperhunter fan-out.
 * 10 candidates, stub extract. Verify all results aggregate and some
 * parallelism occurred (wall-time < serial sum).
 */
'use strict';

const { runPaperHunt } = require('../src/agent/graphs/paperhunter');

(async () => {
  const N = 10;
  const DELAY_MS = 150;
  const candidates = Array.from({ length: N }, (_, i) => ({
    candidate_id: `cand-${i}`,
    source_url: `https://example.org/paper-${i}`,
  }));
  const extract = async (c) => {
    await new Promise(r => setTimeout(r, DELAY_MS));
    // Reject odd indexes as a gate-decision stand-in
    const idx = parseInt(c.candidate_id.split('-')[1], 10);
    return idx % 2
      ? { candidate_id: c.candidate_id, rejection_reason_if_any: 'odd_index_gate' }
      : { candidate_id: c.candidate_id, strategy_spec: { stop_pct: 5, target_pct: 10 } };
  };

  const t0 = Date.now();
  const { results } = await runPaperHunt({ candidates, extract });
  const elapsed = Date.now() - t0;

  const serialMin = N * DELAY_MS;
  console.log(`[paperhunter-smoke] N=${N} elapsed=${elapsed}ms serialMin=${serialMin}ms`);
  if (results.length !== N) { console.error('FAIL count', results.length); process.exit(1); }
  if (elapsed >= serialMin * 0.9) { console.error('FAIL not parallel'); process.exit(1); }
  const accepted = results.filter(r => !r.rejection_reason_if_any).length;
  if (accepted !== 5) { console.error('FAIL accepted count', accepted); process.exit(1); }
  console.log('[paperhunter-smoke] OK accepted=5 rejected=5 parallel');
  process.exit(0);
})().catch((e) => { console.error('ERROR', e); process.exit(1); });
