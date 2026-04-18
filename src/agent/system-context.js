'use strict';

const fs   = require('fs');
const path = require('path');
const { query } = require('../database/postgres');

const ROOT = path.resolve(__dirname, '../..');

async function buildSystemContext() {
  const sections = [];

  // 1. Regime
  try {
    const regimePath = path.join(ROOT, '.agents', 'market-state', 'latest.json');
    const raw = JSON.parse(fs.readFileSync(regimePath, 'utf8'));
    sections.push(
      `**Regime:** ${raw.state || 'UNKNOWN'} | VIX: ${raw.vix ?? 'n/a'} | Date: ${raw.date || 'n/a'}`
    );
  } catch (_) {}

  // 2. Strategies
  try {
    const manifestPath = path.join(ROOT, 'src', 'strategies', 'manifest.json');
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    const strategies = Object.values(manifest)
      .filter(s => s.state !== 'deprecated')
      .map(s => `  - ${s.id} [${s.state}] — ${s.name}`)
      .join('\n');
    sections.push(`**Strategies:**\n${strategies}`);
  } catch (_) {}

  // 3. Portfolio
  try {
    const portfolioPath = path.join(ROOT, 'workspaces', 'default', '.agents', 'user', 'portfolio.json');
    const p = JSON.parse(fs.readFileSync(portfolioPath, 'utf8'));
    const nav = p.nav ?? p.NAV ?? p.total_value ?? 'n/a';
    const positions = p.positions
      ? Object.entries(p.positions).slice(0, 10).map(([k, v]) => `${k}: ${v}`).join(', ')
      : 'none';
    sections.push(`**Portfolio:** NAV=${nav} | Positions: ${positions}`);
  } catch (_) {}

  // 4. Recent signals
  try {
    const res = await query(
      `SELECT strategy_id, ticker, direction, confidence
       FROM signal_output
       ORDER BY created_at DESC
       LIMIT 10`
    );
    if (res.rows.length > 0) {
      const rows = res.rows.map(r =>
        `  - ${r.strategy_id} | ${r.ticker} ${r.direction} [${r.confidence}]`
      ).join('\n');
      sections.push(`**Recent Signals (last 10):**\n${rows}`);
    }
  } catch (_) {}

  if (sections.length === 0) return '';

  return `## LIVE SYSTEM STATE\n${sections.join('\n\n')}`;
}

module.exports = { buildSystemContext };
