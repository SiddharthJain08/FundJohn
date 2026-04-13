'use strict';

/**
 * Data Requester — fulfills on-demand data requests from research agents.
 *
 * Research agents emit DATA_REQUEST: blocks in their output when they need
 * data not already present in the workspace. This module:
 *   1. Parses all DATA_REQUEST: blocks from agent output text
 *   2. Attempts immediate fulfillment via Python tool scripts
 *   3. Writes fetched data to {taskDir}/data/dr_{id}_{type}.json
 *   4. Returns a fulfillment report: { fulfilled[], pending[], errors[] }
 *
 * DATA_REQUEST block format (emitted by research agent):
 *
 *   DATA_REQUEST:
 *     id: req_001
 *     ticker: AAPL
 *     data_type: key_metrics | ratios | options_chain | insider_transactions |
 *                analyst_estimates | earnings_history | price_history |
 *                balance_sheet | cash_flow | sec_filing | peers | price_target
 *     params:
 *       period: ttm
 *       limit: 4
 *     reason: Need Q-by-Q gross margin trend to validate revenue quality claim
 *     priority: HIGH | MEDIUM | LOW
 */

const { spawn } = require('child_process');
const fs   = require('fs');
const path = require('path');

const TOOLS_DIR    = process.env.OPENCLAW_DIR
  ? path.join(process.env.OPENCLAW_DIR, 'workspaces/default/tools')
  : '/root/openclaw/workspaces/default/tools';

const TIMEOUT_MS   = 30_000; // per-request fetch timeout

// Maps data_type → Python one-liner that fetches and prints JSON
const FETCH_MAP = {
  key_metrics:           (t, p) => `import fmp,json; print(json.dumps(fmp.get_key_metrics('${t}', limit=${p.limit||4})))`,
  ratios:                (t, p) => `import fmp,json; print(json.dumps(fmp.get_ratios('${t}', limit=${p.limit||4})))`,
  financial_statements:  (t, p) => `import fmp,json; print(json.dumps(fmp.get_financial_statements('${t}', period='${p.period||'quarterly'}', limit=${p.limit||4})))`,
  balance_sheet:         (t, p) => `import fmp,json; print(json.dumps(fmp.get_balance_sheet('${t}', period='${p.period||'quarterly'}', limit=${p.limit||4})))`,
  cash_flow:             (t, p) => `import fmp,json; print(json.dumps(fmp.get_cash_flow('${t}', period='${p.period||'quarterly'}', limit=${p.limit||4})))`,
  price_history:         (t, p) => `import fmp,json; print(json.dumps(fmp.get_historical_prices('${t}', limit=${p.limit||90})))`,
  earnings_history:      (t, p) => `import fmp,json; print(json.dumps(fmp.get_earnings_calendar('${t}', limit=${p.limit||8})))`,
  price_target:          (t, p) => `import fmp,json; print(json.dumps(fmp.get_price_target('${t}')))`,
  peers:                 (t, p) => `import fmp,json; print(json.dumps(fmp.get_peers('${t}')))`,
  analyst_estimates:     (t, p) => `import fmp,json; print(json.dumps(fmp.get_earnings_calendar('${t}', limit=${p.limit||4})))`,
  options_chain:         (t, p) => `import yahoo,json; print(json.dumps(yahoo.get_options('${t}')))`,
  insider_transactions:  (t, p) => `import yahoo,json; print(json.dumps(yahoo.get_insider_transactions('${t}')))`,
  short_interest:        (t, p) => `import yahoo,json; print(json.dumps(yahoo.get_short_interest('${t}')))`,
  snapshot:              (t, p) => `import polygon,json; print(json.dumps(polygon.get_snapshot('${t}')))`,
};

/**
 * Parse all DATA_REQUEST: blocks from agent output text.
 * Handles indented YAML-like blocks. Returns array of request objects.
 */
function parseRequests(outputText) {
  const requests = [];
  const blockRegex = /DATA_REQUEST:\s*\n((?:[ \t]+.+\n?)*)/g;
  let match;

  while ((match = blockRegex.exec(outputText)) !== null) {
    const block = match[1];
    const req   = {};

    const extract = (key) => {
      const m = new RegExp(`^\\s+${key}:\\s*(.+)`, 'm').exec(block);
      return m ? m[1].trim() : null;
    };

    req.id         = extract('id')        || `req_${Date.now()}_${requests.length}`;
    req.ticker     = extract('ticker')    || '';
    req.data_type  = extract('data_type') || '';
    req.reason     = extract('reason')    || '';
    req.priority   = extract('priority')  || 'MEDIUM';

    // Parse params sub-block
    req.params = {};
    const paramsMatch = /params:\s*\n((?:[ \t]{6,}.+\n?)*)/m.exec(block);
    if (paramsMatch) {
      for (const line of paramsMatch[1].split('\n')) {
        const kv = /^\s+(\w+):\s*(.+)/.exec(line);
        if (kv) req.params[kv[1].trim()] = kv[2].trim();
      }
    }
    if (req.params.limit) req.params.limit = parseInt(req.params.limit, 10);

    if (req.ticker && req.data_type) {
      requests.push(req);
    }
  }

  return requests;
}

/**
 * Fetch a single data request via Python subprocess.
 * Returns { data, error }.
 */
function fetchOne(req) {
  return new Promise((resolve) => {
    const builder = FETCH_MAP[req.data_type];
    if (!builder) {
      return resolve({ data: null, error: `Unknown data_type: ${req.data_type}` });
    }

    const pyCode = builder(req.ticker, req.params || {});
    let stdout = '';
    let stderr = '';

    const child = spawn('python3', ['-c', pyCode], {
      cwd: path.dirname(TOOLS_DIR),  // workspaces/default/ so imports resolve
      env: { ...process.env },
      timeout: TIMEOUT_MS,
    });

    child.stdout.on('data', d => stdout += d.toString());
    child.stderr.on('data', d => stderr += d.toString());

    child.on('exit', (code) => {
      if (code !== 0) {
        return resolve({ data: null, error: stderr.slice(0, 300) || `exit ${code}` });
      }
      try {
        const data = JSON.parse(stdout.trim());
        resolve({ data, error: null });
      } catch {
        resolve({ data: null, error: `JSON parse failed: ${stdout.slice(0, 100)}` });
      }
    });

    child.on('error', (err) => resolve({ data: null, error: err.message }));
  });
}

/**
 * Fulfill all DATA_REQUEST blocks found in research output.
 *
 * @param {string} outputText   — full text output from research subagent
 * @param {string} taskDir      — workspace task directory (e.g. work/AAPL-diligence)
 * @param {Function} [notify]   — optional Discord notify function
 * @returns {Promise<{requests, fulfilled, pending, errors, reportSection}>}
 */
async function fulfill(outputText, taskDir, notify) {
  const requests = parseRequests(outputText);

  if (requests.length === 0) {
    return { requests: [], fulfilled: [], pending: [], errors: [], reportSection: '' };
  }

  console.log(`[data-requester] ${requests.length} DATA_REQUEST(s) found — fulfilling...`);
  if (notify) notify(`📡 Research requested ${requests.length} additional data point(s) — fetching...`);

  const fulfilled = [];
  const pending   = [];
  const errors    = [];

  const dataDir = path.join(taskDir, 'data');
  fs.mkdirSync(dataDir, { recursive: true });

  for (const req of requests) {
    const label = `${req.id}/${req.ticker}/${req.data_type}`;
    console.log(`[data-requester]   → ${label} (${req.priority})`);

    const { data, error } = await fetchOne(req);

    if (data !== null) {
      const outPath = path.join(dataDir, `dr_${req.id}_${req.data_type}.json`);
      fs.writeFileSync(outPath, JSON.stringify(data, null, 2));
      fulfilled.push({ ...req, path: outPath, rows: Array.isArray(data) ? data.length : 1 });
      console.log(`[data-requester]   ✅ ${label} → ${outPath}`);
      if (notify) notify(`✅ Data fetched: ${req.ticker} ${req.data_type} (${Array.isArray(data) ? data.length + ' rows' : 'OK'})`);
    } else {
      pending.push({ ...req, error });
      errors.push({ id: req.id, ticker: req.ticker, data_type: req.data_type, error });
      console.warn(`[data-requester]   ⚠ ${label} → PENDING: ${error}`);
      if (notify) notify(`⚠️ Data unavailable: ${req.ticker} ${req.data_type} — noted as pending`);
    }
  }

  // Write fulfillment manifest for report-builder
  const manifest = {
    run_date:  new Date().toISOString(),
    total:     requests.length,
    fulfilled: fulfilled.length,
    pending:   pending.length,
    requests:  requests.map(r => ({
      id:        r.id,
      ticker:    r.ticker,
      data_type: r.data_type,
      priority:  r.priority,
      reason:    r.reason,
      status:    fulfilled.find(f => f.id === r.id) ? 'FULFILLED' : 'PENDING',
      path:      fulfilled.find(f => f.id === r.id)?.path || null,
      error:     errors.find(e => e.id === r.id)?.error || null,
    })),
  };
  fs.writeFileSync(path.join(dataDir, 'data_requests.json'), JSON.stringify(manifest, null, 2));

  // Build report section text for report-builder to include
  const reportSection = buildReportSection(manifest);

  console.log(`[data-requester] Complete — ${fulfilled.length} fulfilled, ${pending.length} pending`);

  return { requests, fulfilled, pending, errors, reportSection, manifest };
}

/**
 * Build a markdown section for the final report.
 */
function buildReportSection(manifest) {
  if (manifest.total === 0) return '';

  const lines = [
    '',
    '## Additional Data Requests',
    '',
    `Research agent requested **${manifest.total}** additional data point(s) during analysis.`,
    `**${manifest.fulfilled} fulfilled instantly** | **${manifest.pending} pending for next run**`,
    '',
  ];

  if (manifest.fulfilled > 0) {
    lines.push('### Fulfilled');
    for (const r of manifest.requests.filter(r => r.status === 'FULFILLED')) {
      lines.push(`- ✅ \`${r.ticker}\` — \`${r.data_type}\` [${r.priority}]: ${r.reason}`);
    }
    lines.push('');
  }

  if (manifest.pending > 0) {
    lines.push('### Pending (not available in pipeline)');
    lines.push('> These data points were not retrievable in real-time. Flag for next collection cycle.');
    for (const r of manifest.requests.filter(r => r.status === 'PENDING')) {
      lines.push(`- ⚠️ \`${r.ticker}\` — \`${r.data_type}\` [${r.priority}]: ${r.reason}`);
      if (r.error) lines.push(`  - Error: \`${r.error}\``);
    }
    lines.push('');
  }

  return lines.join('\n');
}

module.exports = { parseRequests, fulfill, fetchOne, buildReportSection };
