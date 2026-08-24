'use strict';

/**
 * strategy_redteam.js — Task S1: mandatory pre-backtest red-team gate.
 *
 * Runs on EVERY candidate that passes contract validation, before the
 * backtest step (research-orchestrator.js `_codeFromQueue`). Code-enforced:
 * the orchestrator calls this unconditionally, and the verdict returned here
 * is DERIVED IN CODE from structured findings — the model's own `verdict`
 * field is never trusted directly (see `deriveVerdict` / the mismatch log
 * in `redteamStrategy`).
 *
 * Invocation idiom reused from mastermind_code_review.js / _opus_oneshot.js:
 * claude-bin spawned with stdin prompt, stream-json output, bypassPermissions,
 * read-only disallowedTools. Model defaults to the `primary` (Sonnet 5) tier
 * from config/models.js — this gate runs on every candidate reaching
 * validate, so it deliberately does NOT default to the 1M-context Opus tier
 * mastermind_code_review.js uses for weekly sweeps.
 *
 * Output contract is STRICT JSON, validated BY HAND (shape + enum values,
 * no new deps — reuses the zero-dependency `parseJsonBlock` regex/JSON.parse
 * helper already in _opus_oneshot.js). Malformed output -> one retry with a
 * terser reminder -> still bad -> WARN-and-pass with `infra_fail: true`
 * (logged as `redteam_infra_fail`). Infra failure must NEVER silently block
 * research — see the global constraint in task-S1-brief.md.
 *
 * Root-privilege fix (2026-08-24 review, finding #1): the live
 * johnbot.service Node process runs as root, and claude-bin refuses
 * `--permission-mode bypassPermissions` under root/sudo ("cannot be used
 * with root/sudo privileges for security reasons"). Left unhandled, every
 * candidate reaching this gate on the root call paths
 * (cron-schedule.js -> _codeFromQueue, staging_approver via :3000) would
 * silently fall into the redteam_infra_fail WARN-and-pass branch — the gate
 * would run but never actually red-team anything. `_opus_oneshot.js`'s
 * shared `runOneShot()` is intentionally NOT modified (other callers —
 * mastermind_code_review.js, comprehensive_review.js, etc. — run on their
 * own cadences and are out of scope here); instead this file defines its own
 * `runOneShotDeprivileged()`, a minimal duplicate of `runOneShot()`'s
 * spawn + stream-json-parsing body, with one addition: when
 * `process.getuid() === 0`, the child is de-privileged to the `claudebot`
 * service account via child_process.spawn's own `uid`/`gid` options (no
 * runuser/sudo subprocess needed — root can setuid a direct child via
 * spawn()), with `HOME`/`CLAUDE_HOME` pointed at `/home/claudebot` so
 * claude-bin picks up claudebot's own OAuth credentials
 * (`/home/claudebot/.claude/.credentials.json`) instead of root's. This is
 * the exact `uid`/`gid` + `HOME` override idiom already used in production
 * by every other root-run caller of claude-bin in this repo
 * (run-subagent-cli.js, subagents/swarm.js, run_maintenance.js,
 * botjohn-direct.js — all default `CLAUDE_UID`/`CLAUDE_GID`/`CLAUDE_HOME` to
 * 1001/1001//home/claudebot the same way), not a new pattern. When this
 * process is already non-root (e.g. an interactive claudebot shell, or a
 * dev/test run as a regular user), `uid`/`gid` are simply omitted and the
 * spawn behaves exactly as `runOneShot()` always has.
 */

const fs   = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const { parseJsonBlock } = require('./_opus_oneshot');
const { MODELS } = require('../config/models');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const WORKSPACE     = path.join(OPENCLAW_DIR, 'workspaces/default');
const CLAUDE_BIN    = process.env.CLAUDE_BIN || '/usr/local/bin/claude-bin';

// Sonnet 5, not the 1M Opus tier mastermind_code_review.js defaults to — this
// gate runs on every candidate reaching validate (high call volume), while
// mastermind_code_review.js runs on a weekly/live sweep cadence.
const DEFAULT_MODEL = process.env.OPENCLAW_REDTEAM_MODEL || MODELS.primary.model;
const TIMEOUT_MS     = parseInt(process.env.OPENCLAW_REDTEAM_TIMEOUT_MS || '180000', 10) || 180_000;

/**
 * Duplicate (deliberately — see module docstring) of _opus_oneshot.js's
 * runOneShot(): identical claude-bin args, stdin prompt, stream-json event
 * parsing, timeout/SIGTERM handling, and { text, events, costUsd,
 * durationMs, error? } return shape. The ONLY behavioral addition is
 * de-privileging the child to `claudebot` when this process is root.
 *
 * @returns {Promise<{text:string, events:object[], costUsd:number, durationMs:number, error?:string}>}
 */
function runOneShotDeprivileged({ prompt, model = DEFAULT_MODEL, cwd = process.cwd(),
                                   disallowedTools = [], allowedTools = null,
                                   timeoutMs = 180_000 } = {}) {
  return new Promise((resolve) => {
    const args = [
      '-p',
      '--model', model,
      '--output-format', 'stream-json',
      '--input-format', 'text',
      '--permission-mode', 'bypassPermissions',
      '--include-partial-messages',
      '--verbose',
    ];
    if (disallowedTools.length) args.push('--disallowedTools', disallowedTools.join(','));
    if (allowedTools && allowedTools.length) args.push('--allowedTools', allowedTools.join(','));

    const spawnOpts = { cwd, env: { ...process.env }, stdio: ['pipe', 'pipe', 'pipe'] };

    // De-privilege iff this process is root — matches run-subagent-cli.js /
    // swarm.js / run_maintenance.js / botjohn-direct.js's CLAUDE_UID/
    // CLAUDE_GID/CLAUDE_HOME convention exactly (same env var names/defaults).
    const isRoot = typeof process.getuid === 'function' && process.getuid() === 0;
    if (isRoot) {
      const CLAUDE_UID  = parseInt(process.env.CLAUDE_UID  || '1001', 10);
      const CLAUDE_GID  = parseInt(process.env.CLAUDE_GID  || '1001', 10);
      const CLAUDE_HOME = process.env.CLAUDE_HOME || '/home/claudebot';
      spawnOpts.uid = CLAUDE_UID;
      spawnOpts.gid = CLAUDE_GID;
      spawnOpts.env = { ...spawnOpts.env, HOME: CLAUDE_HOME, CLAUDE_HOME };
    }

    const child = spawn(CLAUDE_BIN, args, spawnOpts);
    child.stdin.write(prompt);
    child.stdin.end();

    const events = [];
    let text = '';
    let costUsd = 0;
    let durationMs = 0;
    let buf = '';
    let errBuf = '';
    let timedOut = false;
    const tStart = Date.now();

    const timer = setTimeout(() => {
      timedOut = true;
      try { child.kill('SIGTERM'); } catch {}
    }, timeoutMs);

    child.stdout.on('data', (d) => {
      buf += d.toString();
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const ev = JSON.parse(line);
          events.push(ev);
          if (ev.type === 'assistant' && ev.message?.content) {
            for (const c of ev.message.content) {
              if (c.type === 'text' && c.text) text += c.text;
            }
          }
          if (ev.type === 'result') {
            if (ev.total_cost_usd != null) costUsd = Number(ev.total_cost_usd);
            if (ev.duration_ms != null)    durationMs = Number(ev.duration_ms);
            if (ev.result && typeof ev.result === 'string' && !text) text = ev.result;
          }
        } catch { /* partial / non-JSON line */ }
      }
    });

    child.stderr.on('data', (d) => { errBuf += d.toString(); });

    child.on('close', (code) => {
      clearTimeout(timer);
      const out = {
        text:       text.trim(),
        events,
        costUsd,
        durationMs: durationMs || (Date.now() - tStart),
      };
      if (timedOut) out.error = `timeout after ${timeoutMs}ms`;
      else if (code !== 0) out.error = `exit ${code}: ${errBuf.slice(0, 500)}`;
      resolve(out);
    });
    child.on('error', (e) => {
      clearTimeout(timer);
      resolve({ text: '', events, costUsd: 0, durationMs: Date.now() - tStart, error: e.message });
    });
  });
}

const VALID_VERDICTS  = new Set(['pass', 'block']);
const VALID_SEVERITIES = new Set(['critical', 'warning']);

const RETRY_REMINDER = [
  '',
  'Your previous reply could not be parsed as the required JSON.',
  'STRICT JSON ONLY. Reply with EXACTLY one JSON object and NOTHING else —',
  'no markdown fences, no prose, no leading/trailing text:',
  '{"verdict": "pass"|"block", "findings": [{"concern": "...", "severity": "critical"|"warning", "evidence": "..."}]}',
].join('\n');

/**
 * Build the red-team prompt. `code` is inlined in full — the reviewer never
 * shells out or greps; it reads the file content given to it.
 */
function buildPrompt({ implPath, code, paperContext }) {
  const contextBlock = paperContext
    ? [
        '',
        'Background context (the paper / hypothesis this strategy implements —',
        'for orientation ONLY; do not audit alpha quality or parameter choices):',
        typeof paperContext === 'string' ? paperContext : JSON.stringify(paperContext),
      ].join('\n')
    : '';

  return [
    `You are a red-team reviewer auditing a quant trading strategy's Python`,
    `implementation for BACKTEST-INTEGRITY bugs before it is allowed to reach`,
    `backtesting. You are adversarial: assume the author may have made an`,
    `honest mistake that would silently inflate backtest performance versus`,
    `live trading, and hunt for it.`,
    contextBlock,
    '',
    `File: ${implPath}`,
    '```python',
    code,
    '```',
    '',
    `Hunt ONLY for these five defect classes — say nothing about anything else`,
    `(not alpha quality, not style, not parameter tuning):`,
    '',
    `(a) FUTURE-BAR ACCESS — any NEGATIVE shift on a price/series column`,
    `    (e.g. \`.shift(-1)\`, \`.shift(-n)\`), indexing past the current bar`,
    `    (e.g. \`iloc[i+1]\`, \`iloc[-1]\` on a series already advanced past`,
    `    "today"), or using the signal bar's OWN close/high/low to decide an`,
    `    entry the live engine would only observe after that bar closes.`,
    `(b) OFF-BY-ONE WINDOW ALIGNMENT — a rolling/expanding window that`,
    `    INCLUDES the decision bar itself when the computation must be`,
    `    strictly prior to it (e.g. a rolling mean meant to end at t-1 that`,
    `    actually ends at t and therefore leaks the decision bar's own value).`,
    `(c) FULL-SAMPLE PARAMETER FITTING inside generate_signals — any`,
    `    normalization, z-score, quantile threshold, or regression fit`,
    `    computed over the ENTIRE input series (past AND future bars) rather`,
    `    than an expanding/rolling window ending at the current bar.`,
    `(d) SURVIVORSHIP / UNIVERSE ASSUMPTIONS — hardcoded ticker lists baked`,
    `    into the signal logic instead of using the supplied \`universe\`/`,
    `    columns argument.`,
    `(e) SIGNAL-CAN-NEVER-FIRE LOGIC BUGS — predicates that are always`,
    `    False/True, contradictory conditions, or thresholds that are`,
    `    unreachable given the computed series' actual range.`,
    '',
    `Respond with STRICT JSON ONLY — no markdown code fences, no prose before`,
    `or after, exactly this shape and nothing else:`,
    `{"verdict": "pass"|"block", "findings": [{"concern": "<short label>", "severity": "critical"|"warning", "evidence": "<the specific line/expression and why it is a defect>"}]}`,
    '',
    `Use "critical" for any (a)/(b)/(c) defect that would inflate backtest`,
    `performance versus live trading, or an (e) bug that makes the strategy`,
    `silently inert. Use "warning" for (d) and for anything suspicious but not`,
    `certain. If you find nothing, return {"verdict":"pass","findings":[]}.`,
  ].join('\n');
}

/** Validate + normalize the parsed JSON by hand. Returns null if unusable. */
function validateRedteamJson(obj) {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
  if (!VALID_VERDICTS.has(obj.verdict)) return null;
  if (!Array.isArray(obj.findings)) return null;
  const findings = [];
  for (const f of obj.findings) {
    if (!f || typeof f !== 'object' || Array.isArray(f)) return null;
    if (typeof f.concern !== 'string' || !f.concern.trim()) return null;
    if (!VALID_SEVERITIES.has(f.severity)) return null;
    if (typeof f.evidence !== 'string') return null;
    findings.push({ concern: f.concern.trim(), severity: f.severity, evidence: f.evidence });
  }
  return { verdict: obj.verdict, findings };
}

/** verdict is DERIVED, never trusted: block iff any finding is 'critical'. */
function deriveVerdict(findings) {
  return findings.some((f) => f.severity === 'critical') ? 'block' : 'pass';
}

/**
 * Run one claude-bin turn and try to extract a valid redteam JSON object.
 * Returns { parsed, res } — parsed is null on any failure (infra error,
 * unparseable text, or shape/enum violation).
 */
async function _attempt(prompt, { model, runOneShotFn }) {
  const res = await runOneShotFn({
    prompt,
    model,
    cwd: WORKSPACE,
    disallowedTools: ['Bash', 'Write', 'Edit', 'NotebookEdit'],  // read-only audit
    timeoutMs: TIMEOUT_MS,
  });
  if (res.error) return { parsed: null, res };
  const parsed = validateRedteamJson(parseJsonBlock(res.text));
  return { parsed, res };
}

/**
 * @param {Object} opts
 * @param {string} opts.implPath      — path to the strategy .py to review.
 * @param {string|object} [opts.paperContext] — optional hypothesis/paper
 *   context to orient the reviewer (never widens what it's allowed to flag).
 * @param {string} [opts.model]       — override the claude-bin model id.
 * @param {function} [opts.runOneShotFn] — injection point for tests.
 * @returns {Promise<{verdict:'pass'|'block', findings:object[], infra_fail:boolean}>}
 */
async function redteamStrategy({ implPath, paperContext = null, model, runOneShotFn } = {}) {
  const runFn = runOneShotFn || runOneShotDeprivileged;
  const useModel = model || DEFAULT_MODEL;
  const code = fs.readFileSync(implPath, 'utf8');
  const prompt = buildPrompt({ implPath, code, paperContext });

  let { parsed, res } = await _attempt(prompt, { model: useModel, runOneShotFn: runFn });

  if (!parsed) {
    console.error(
      `[redteam] first attempt unusable for ${implPath}` +
      `${res.error ? ` (infra: ${res.error})` : ' (malformed JSON)'} — retrying once`
    );
    ({ parsed, res } = await _attempt(prompt + '\n' + RETRY_REMINDER, { model: useModel, runOneShotFn: runFn }));
  }

  if (!parsed) {
    console.error(
      `[redteam_infra_fail] reviewer output unusable after retry for ${implPath}` +
      `${res.error ? ` — ${res.error}` : ''}; WARN-and-pass (fail-open)`
    );
    return { verdict: 'pass', findings: [], infra_fail: true };
  }

  const derived = deriveVerdict(parsed.findings);
  if (derived !== parsed.verdict) {
    console.error(
      `[redteam] verdict mismatch for ${implPath}: model said '${parsed.verdict}', ` +
      `derived '${derived}' from findings' severities — using the derived verdict`
    );
  }
  return { verdict: derived, findings: parsed.findings, infra_fail: false };
}

module.exports = {
  redteamStrategy,
  buildPrompt,
  validateRedteamJson,
  deriveVerdict,
  runOneShotDeprivileged,
  DEFAULT_MODEL,
};
