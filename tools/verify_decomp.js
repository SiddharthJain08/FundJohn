#!/usr/bin/env node
// Iterate every ticker visible to the dashboard and confirm the
// per-strategy alpha decomposition invariant: |Σ alpha - live_sharpe| < 1e-6.
//
// Tickers with <2 distinct closed-trade days are reported as "skip"
// (no Sharpe possible — needs std). This isn't a failure, it's expected.
const BASE = 'http://localhost:3000';
const EPS  = 1e-6;

(async () => {
  const [pos, hist] = await Promise.all([
    fetch(BASE + '/api/portfolio/positions').then(r => r.json()),
    fetch(BASE + '/api/portfolio/history').then(r => r.json()),
  ]);
  const tickers = new Set();
  for (const r of pos)  if (r.ticker) tickers.add(r.ticker);
  for (const r of hist) if (r.ticker) tickers.add(r.ticker);
  console.log('Probing', tickers.size, 'tickers (sequential, throttled to avoid DB hammering)...');

  let pass = 0, skip = 0, fail = 0;
  const fails = [];
  const tList = [...tickers].sort();
  for (let i = 0; i < tList.length; i++) {
    const t = tList[i];
    try {
      const p = await (await fetch(`${BASE}/api/portfolio/ticker-alpha/${encodeURIComponent(t)}`)).json();
      if (p.live_sharpe == null) { skip += 1; continue; }
      const sum  = p.strategies.reduce((s, x) => s + x.alpha, 0);
      const diff = Math.abs(sum - p.live_sharpe);
      if (diff < EPS) pass += 1;
      else { fail += 1; fails.push({ t, sum: +sum.toFixed(6), sharpe: +p.live_sharpe.toFixed(6), diff: +diff.toFixed(8) }); }
    } catch (e) { fail += 1; fails.push({ t, error: e.message }); }
    if (i % 50 === 0 && i > 0) process.stdout.write(' ' + i);
  }
  console.log('\npass=' + pass + '  skip(insufficient_days)=' + skip + '  fail=' + fail);
  if (fails.length) {
    console.log('failures (first 5):');
    for (const f of fails.slice(0, 5)) console.log('  ', JSON.stringify(f));
  }
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error(e); process.exit(1); });
