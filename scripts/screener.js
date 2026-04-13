#!/usr/bin/env node
'use strict';

/**
 * scripts/screener.js — Universe Quantitative Screener (pre-diligence filter)
 *
 * Screens tickers in output/universe.json against quantitative filters
 * (market cap, revenue growth, gross margin, EV/NTM Rev, insider selling)
 * to select which tickers warrant full diligence.
 *
 * Results saved to: output/screen-results.json
 * Stdout:           SCREENER_RESULT:{json} events, then raw screener output
 *
 * Usage:
 *   node screener.js [--sector Technology] [--min-score 0.6] [--extra-args "max_ev_rev=12"]
 */

const { spawn } = require('child_process');
const fs        = require('fs');
const path      = require('path');

// ── Config ────────────────────────────────────────────────────────────────────
const CLAUDE_BIN   = process.env.CLAUDE_BIN   || '/usr/local/bin/claude-bin';
const CLAUDE_UID   = parseInt(process.env.CLAUDE_UID   || '1001', 10);
const CLAUDE_GID   = parseInt(process.env.CLAUDE_GID   || '1001', 10);
const CLAUDE_HOME  = process.env.CLAUDE_HOME  || '/home/claudebot';
const WORKDIR      = process.env.OPENCLAW_DIR || '/root/openclaw';
const SCREEN_MODEL = 'claude-sonnet-4-6';

const UNIVERSE_FILE      = path.join(WORKDIR, 'output', 'universe.json');
const SCREEN_CMD_FILE    = path.join(WORKDIR, '.claude', 'commands', 'screen.md');
const SCREEN_RESULT_FILE = path.join(WORKDIR, 'output', 'screen-results.json');

// ── CLI args ──────────────────────────────────────────────────────────────────
function getArg(flag, fallback = null) {
  const i = process.argv.indexOf(flag);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const sectorArg   = getArg('--sector', 'all');
const minScoreArg = parseFloat(getArg('--min-score', '0'));
const extraArgs   = getArg('--extra-args', '');

// ── Progress / logging ────────────────────────────────────────────────────────
function progress(event, data = {}) {
  process.stdout.write(`SCREENER_RESULT:${JSON.stringify({ event, ts: Date.now(), ...data })}\n`);
}
function log(msg) {
  process.stderr.write(`[${new Date().toISOString()}] [SCREENER] ${msg}\n`);
}

// ── Universe helpers ──────────────────────────────────────────────────────────
function loadUniverse() {
  try { return JSON.parse(fs.readFileSync(UNIVERSE_FILE, 'utf8')); }
  catch { return { sectors: {} }; }
}

/**
 * Returns all universe tickers for the given sector (or all sectors).
 * Returns flat array of ticker strings.
 */
function resolveUniverseTickers(sector) {
  const u = loadUniverse();
  if (!sector || sector.toLowerCase() === 'all') {
    return Object.values(u.sectors).flatMap(s => s.tickers || []);
  }
  for (const [name, data] of Object.entries(u.sectors)) {
    if (name.toLowerCase() === sector.toLowerCase()) return data.tickers || [];
  }
  // Try prefix match
  for (const [name, data] of Object.entries(u.sectors)) {
    if (name.toLowerCase().startsWith(sector.toLowerCase())) return data.tickers || [];
  }
  return [];
}

// ── Output parser ─────────────────────────────────────────────────────────────
/**
 * Parse the markdown table from screen.md output to extract passing tickers.
 * Only includes tickers present in the universe (guards against hallucinations).
 * Returns array of { rank, ticker, compositeScore }.
 */
function parseScreenTable(output, universeTickers) {
  const tickerSet = new Set(universeTickers.map(t => t.toUpperCase()));
  const passing   = [];
  const lines     = output.split('\n');
  let inTable     = false;

  for (const line of lines) {
    if (line.includes('| Rank') && line.includes('Ticker')) {
      inTable = true;
      continue;
    }
    if (inTable && /^\|[-:\s|]+\|$/.test(line)) continue;   // separator row
    if (inTable && line.startsWith('|')) {
      const cols = line.split('|').map(c => c.trim()).filter(Boolean);
      if (cols.length >= 2) {
        const rank   = parseInt(cols[0], 10);
        const ticker = cols[1]?.toUpperCase().replace(/[^A-Z]/g, '');
        const score  = parseFloat(cols[cols.length - 1]) || 0;
        // Only accept tickers from our universe (prevents hallucination bleed)
        if (ticker && tickerSet.has(ticker) && !isNaN(rank)) {
          if (!passing.find(p => p.ticker === ticker)) {
            passing.push({ rank, ticker, compositeScore: score });
          }
        }
      }
    } else if (inTable && !line.startsWith('|')) {
      inTable = false;
    }
  }
  return passing.sort((a, b) => a.rank - b.rank);
}

// ── Main ──────────────────────────────────────────────────────────────────────
async function main() {
  const allTickers = resolveUniverseTickers(sectorArg);

  if (allTickers.length === 0) {
    const u = loadUniverse();
    log(`No tickers found for sector="${sectorArg}". Available: ${Object.keys(u.sectors).join(', ')}`);
    process.exit(1);
  }

  log(`SCREENER START — ${allTickers.length} tickers | sector=${sectorArg}`);
  progress('SCREENER_START', { tickers: allTickers, sector: sectorArg, count: allTickers.length });

  // Load screen.md template and strip frontmatter
  let screenTemplate;
  try {
    screenTemplate = fs.readFileSync(SCREEN_CMD_FILE, 'utf8')
      .replace(/^---[\s\S]*?---\s*\n/, '');
  } catch (err) {
    log(`ERROR: Cannot read ${SCREEN_CMD_FILE}: ${err.message}`);
    process.exit(1);
  }

  // Build prompt — inject our universe at the top so Claude skips market-wide pulls
  const screenArgs = [`sector=${sectorArg}`, extraArgs].filter(Boolean).join(' ');
  const universeOverride = [
    `**UNIVERSE OVERRIDE — IMPORTANT**: Do NOT pull the Russell 1000, S&P 500, or any market index.`,
    `Screen ONLY the following ${allTickers.length} tickers from the OpenClaw coverage universe:`,
    ``,
    allTickers.join(', '),
    ``,
    `Fetch data from Yahoo Finance and SEC EDGAR for each ticker above. Apply all quantitative`,
    `filters from the instructions below. Output the ranked table containing ONLY the tickers`,
    `from the list above that pass ALL filters. Do not add tickers not in this list.`,
    ``,
    `---`,
    ``,
  ].join('\n');

  const fullPrompt = universeOverride + screenTemplate.replace(/\$ARGUMENTS/g, screenArgs);

  // Run via claude-bin (sonnet — needs multi-step data fetching + reasoning)
  log(`Launching screener agent (${allTickers.length} tickers, model=${SCREEN_MODEL})...`);

  const rawOutput = await new Promise((resolve, reject) => {
    let stdout = '', stderr = '';
    const proc = spawn(
      CLAUDE_BIN,
      ['--dangerously-skip-permissions', '--model', SCREEN_MODEL, '-p', fullPrompt],
      {
        cwd: WORKDIR,
        uid: CLAUDE_UID,
        gid: CLAUDE_GID,
        env: {
          ...process.env,
          HOME:         CLAUDE_HOME,
          USER:         'claudebot',
          LOGNAME:      'claudebot',
          SUDO_USER:    undefined,
          SUDO_UID:     undefined,
          SUDO_GID:     undefined,
          SUDO_COMMAND: undefined,
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      }
    );
    proc.stdout.on('data', d => { stdout += d.toString(); });
    proc.stderr.on('data', d => { stderr += d.toString(); });
    proc.on('close', (code) => {
      if (code === 0 || stdout.trim()) resolve(stdout.trim());
      else reject(new Error(stderr.trim().slice(0, 400) || `exit ${code}`));
    });
    proc.on('error', reject);
  });

  // Parse passing tickers from the ranked table
  const passing  = parseScreenTable(rawOutput, allTickers);
  const filtered = minScoreArg > 0 ? passing.filter(p => p.compositeScore >= minScoreArg) : passing;
  const tickers  = filtered.map(p => p.ticker);

  // Save results — pipeline-runner reads this when --pre-screen is set
  const results = {
    ts:        Date.now(),
    date:      new Date().toISOString().slice(0, 10),
    sector:    sectorArg,
    scanned:   allTickers.length,
    passing:   filtered.length,
    tickers,
    ranked:    filtered,
    rawOutput,
  };
  fs.writeFileSync(SCREEN_RESULT_FILE, JSON.stringify(results, null, 2));

  log(`SCREENER DONE — ${allTickers.length} scanned | ${filtered.length} passing: [${tickers.join(', ')}]`);
  progress('SCREENER_COMPLETE', {
    sector:  sectorArg,
    scanned: allTickers.length,
    passing: filtered.length,
    tickers,
    ranked:  filtered,
  });

  // Print raw output for index.js to relay to Discord
  process.stdout.write(rawOutput + '\n');
}

main().catch(err => {
  log(`FATAL: ${err.message}`);
  progress('SCREENER_ERROR', { error: err.message });
  process.exit(1);
});
