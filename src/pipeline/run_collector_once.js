'use strict';

/**
 * run_collector_once.js — standalone wrapper around collector.start().
 *
 * The daily orchestrator (src/execution/pipeline_orchestrator.py) invokes this
 * script as the `collect` step. The collector runs one cycle against the
 * master parquets + configured universe and exits 0 on clean completion,
 * non-zero on any phase error. Posts a descriptive end-of-run summary to
 * #data-alerts via the BotJohn webhook (inherited from the retired
 * DataBot persona; bypasses bot-token 403s).
 */

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

// Ensure any transitive require of src/channels/api/server.js inside the
// collector's broadcast helpers does NOT try to listen on :3000 (johnbot
// already owns that socket). We only need the broadcast exports here.
process.env.OPENCLAW_NO_HTTP_LISTEN = '1';

const https = require('https');
const { Client } = require('pg');

const collector = require('./collector');

async function getWebhook() {
  const client = new Client({ connectionString: process.env.POSTGRES_URI });
  await client.connect();
  try {
    const r = await client.query(
      "SELECT webhook_urls FROM agent_registry WHERE id='botjohn'"
    );
    return ((r.rows[0]?.webhook_urls) || {})['data-alerts'] || null;
  } finally { await client.end(); }
}

function post(url, content) {
  return new Promise((resolve) => {
    const u = new URL(url);
    const body = JSON.stringify({ content: content.slice(0, 1900) });
    const req = https.request({
      hostname: u.hostname, path: u.pathname + u.search, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, (res) => { res.on('data',()=>{}); res.on('end', () => resolve(res.statusCode)); });
    req.on('error', () => resolve(0));
    req.write(body); req.end();
  });
}

// Scrape the collector's stdout as it runs so we can attribute rows-written
// and time-per-phase into the summary message without having to re-query
// the collector's internal state.
const phases = []; // { phase, line, ts }
const phaseStart = Date.now();
const origLog = console.log;
console.log = (...args) => {
  const line = args.join(' ');
  origLog(...args);
  // Capture collector's phase summary lines
  if (/\[collector\]\s+✅|\[collector\]\s+⚠️|\[collector\]\s+❌|\[collector\]\s+🎲|\[collector\]\s+🌐/.test(line)) {
    phases.push({ line: line.replace(/^.*?\[collector\]\s+/, ''), ts: Date.now() });
  }
};

// `--eod-only` selects the slimmed-down post-market refresh that only fetches
// today's equity + market price bars. Used by openclaw-eod-refresh.timer at
// 20:30 UTC (4:30pm ET), 30 min after market close, to capture EOD bars
// the 10am ET cycle structurally cannot. Skips options/fundamentals/news/
// insider/orchestrator-spawn — pure data hygiene.
const eodOnly = process.argv.includes('--eod-only');
// `--prices-only` runs ONLY the price-fill stage for the union universe
// (equity + market bars via runHistoricalPrices + runMarketPricesNonEquity).
// Skips options, fundamentals, macro, news, insider, and snapshots.
// Used by the intraday 15-min regime prefetch (scripts/refetch_prices.py) to
// pull fresh prices before a tick-3 redeploy without running the full cycle.
// Exits 0 on success, 1 on error — no Discord post (intraday hot path).
const pricesOnly = process.argv.includes('--prices-only');
// `--dry-run` propagates to collector.js — API calls still happen so we
// can validate auth/quota, but parquet writes (store.upsert*) and DB
// updates (store.updateCoverage) are SKIPPED. Set OPENCLAW_DRY_RUN=1
// for the same effect via env so subprocess invocations inherit cleanly.
const dryRun = process.argv.includes('--dry-run');
if (dryRun) {
  process.env.OPENCLAW_DRY_RUN = '1';
  console.log('[collector-once] DRY-RUN: parquet writes + coverage updates will be skipped');
}

function formatIngestionSummary(kind, data) {
  // kind ∈ {'SOD','EOD'}. data.ok=false → failure line.
  const day = data && data.date ? data.date : new Date().toISOString().slice(0, 10);
  if (!data || !data.ok) {
    const label = kind === 'EOD' ? 'EOD refresh' : 'Start-of-day ingestion';
    return `❌ **${label} FAILED** — ${day}`;
  }
  const elapsed = data.elapsed_s != null ? ` · ${data.elapsed_s}s` : '';
  const lines = [];
  if (kind === 'SOD') {
    lines.push(`📦 **Start-of-day ingestion complete** — ${day}${elapsed}`);
    const phases = data.phases || [];
    if (!phases.length) {
      lines.push('(no phase output captured)');
    } else {
      for (const p of phases) {
        if (p.rows != null) {
          lines.push(`  • ${p.phase}: **${p.rows}** rows`);
        } else if (p.line) {
          lines.push(`  • ${p.phase}: ${p.line}`);
        } else {
          // No row count and no captured line → render the phase alone, with
          // NO trailing ': ' (the remapped {phase, rows:null} live-capture shape).
          lines.push(`  • ${p.phase}`);
        }
      }
    }
  } else {
    return formatEodAlert(data.summary, data.ok);
  }
  return lines.join('\n');
}

function formatEodAlert(summary, ok) {
  // summary is what runEodRefresh returns; ok=false means an exception was thrown.
  if (!summary || !ok) {
    return `❌ **EOD refresh FAILED** — ${new Date().toISOString().slice(0,10)}`;
  }
  const { elapsed_s, today, total_tickers, equity_tickers, advanced_count,
          advanced_by_date, stagnant } = summary;
  const lines = [];

  if (advanced_count === 0) {
    const stuckCount = stagnant.filter(s => !s.date || s.date < today).length;
    if (stuckCount === 0) {
      lines.push(`🌅 **EOD refresh — no gaps to fill** · ${elapsed_s}s`);
      lines.push(`All ${total_tickers} tickers already current as of ${today}.`);
    } else {
      lines.push(`⚠️ **EOD refresh — no bars fetched** · ${elapsed_s}s`);
      lines.push(`${stuckCount} of ${total_tickers} tickers still behind today (upstream delay or empty response).`);
    }
  } else {
    lines.push(`🌅 **EOD refresh — ${advanced_count} tickers gained bars** · ${elapsed_s}s`);
    // Filled dates breakdown, descending (today first)
    const dateKeys = Object.keys(advanced_by_date).sort().reverse();
    lines.push('**Filled dates:**');
    for (const d of dateKeys) {
      const list = advanced_by_date[d];
      const eq = list.filter(t => equity_tickers.includes(t)).length;
      const mk = list.length - eq;
      lines.push(`  • ${d}: **${list.length}** tickers  (${eq} equity / ${mk} market)`);
    }
    // Tickers that didn't advance and remain behind today are the ones
    // worth flagging — they signal a persistent upstream blank.
    const stuck = stagnant.filter(s => !s.date || s.date < today);
    if (stuck.length > 0) {
      const sample = stuck.slice(0, 12).map(s => s.ticker).join(', ');
      const more   = stuck.length > 12 ? ` … (+${stuck.length - 12} more)` : '';
      lines.push(`**Still behind today (${stuck.length}):** ${sample}${more}`);
    }
  }
  return lines.join('\n');
}

async function main() {
  // --prices-only: fast intraday price refetch — no Discord post, no snapshot,
  // no options/fundamentals/news/insider. Reuses runEodRefresh which already
  // isolates prices (runHistoricalPrices + runMarketPricesNonEquity) for the
  // full union universe. Exits immediately when done.
  if (pricesOnly) {
    console.log('[collector-once] --prices-only: running price-fill for union universe');
    try {
      await collector.runEodRefresh();
      console.log('[collector-once] --prices-only: price-fill complete');
      process.exit(0);
    } catch (err) {
      console.error('[collector-once] --prices-only: price-fill failed:', err && err.stack || err);
      process.exit(1);
    }
    return; // unreachable; explicit for linters
  }

  const mode = eodOnly ? 'EOD refresh' : 'full daily collection';
  console.log(`[collector-once] starting single-shot ${mode} cycle`);
  let ok = true;
  let eodSummary = null;

  // Wire #data-alerts progress posts. collector.js's tickProgress()
  // already builds the per-10-ticker progress block:
  //   **{phase}** — {progressBar} `{n}/{total}` ({pct}%)
  //   Ticker: `{ticker}` | Speed: **{rate} tickers/min** | ETA: **{eta}**
  //   Rows written this phase: **{rows}** | Errors: {errors}
  // Without setDiscordHooks the alerts go to console only — fix is to
  // pipe an alertPost callback that POSTs to the BotJohn webhook.
  let _webhookUrl = null;
  try { _webhookUrl = await getWebhook(); } catch (_) { /* best effort */ }
  if (_webhookUrl) {
    collector.setDiscordHooks({
      alertPost: (msg) => post(_webhookUrl, msg).catch(() => {}),
    });
  }

  try {
    if (eodOnly) {
      eodSummary = await collector.runEodRefresh();
    } else {
      await collector.runDailyCollection();
    }
    console.log('[collector-once] cycle complete');
  } catch (err) {
    console.error('[collector-once] cycle failed:', err && err.stack || err);
    ok = false;
  }

  // End-of-run summary to #data-alerts
  try {
    const total_s = Math.round((Date.now() - phaseStart) / 1000);
    const url = await getWebhook();
    if (url) {
      let body;
      if (eodOnly) {
        body = formatEodAlert(eodSummary, ok);
      } else {
        body = formatIngestionSummary('SOD', {
          ok,
          elapsed_s: total_s,
          date: new Date().toISOString().slice(0, 10),
          phases: (phases || []).map(p => (p.phase ? p : { phase: p.line, rows: null })),
        });
      }
      await post(url, body);
    }
  } catch (err) {
    console.warn('[collector-once] data-alerts summary post failed:', err.message);
  }

  process.exit(ok ? 0 : 1);
}

if (require.main === module) { main(); }

module.exports = { formatIngestionSummary, formatEodAlert };
