'use strict';

/**
 * _opus_oneshot.js — spawn claude-bin (Opus 4.7 1M) with a fully-composed
 * prompt and return stdout + cost. Used by comprehensive_review /
 * position_recommender / paper_expansion_ingestor for single-turn Opus work.
 *
 * Unlike mastermind.js which uses the subagent CLI harness (requires a
 * registered prompt file), this invokes claude-bin directly with a bespoke
 * prompt per call site. Simpler for one-shot analytical tasks.
 */

const { spawn } = require('child_process');

const DEFAULT_MODEL = process.env.CURATOR_OPUS_MODEL || 'claude-opus-4-7[1m]';
const CLAUDE_BIN    = process.env.CLAUDE_BIN || '/usr/local/bin/claude-bin';

/**
 * Run one Opus turn.
 *
 * @param {Object} opts
 * @param {string} opts.prompt         — Full text prompt.
 * @param {string} [opts.model]        — Override model.
 * @param {string} [opts.cwd]          — Working directory for claude-bin.
 * @param {string[]} [opts.disallowedTools] — e.g. ['Bash','Write']
 * @param {string[]} [opts.allowedTools]    — e.g. ['WebSearch','WebFetch','Bash']
 * @param {number} [opts.timeoutMs=600000] — 10-minute default.
 * @returns {Promise<{text:string, events:object[], costUsd:number, durationMs:number, error?:string}>}
 */
function runOneShot({ prompt, model = DEFAULT_MODEL, cwd = process.cwd(),
                      disallowedTools = [], allowedTools = null,
                      timeoutMs = 600_000 } = {}) {
  return new Promise((resolve) => {
    const args = [
      '-p', prompt,
      '--model', model,
      '--output-format', 'stream-json',
      '--permission-mode', 'bypassPermissions',
      '--include-partial-messages',
      '--verbose',
    ];
    if (disallowedTools.length) args.push('--disallowedTools', disallowedTools.join(','));
    if (allowedTools && allowedTools.length) args.push('--allowedTools', allowedTools.join(','));

    const child = spawn(CLAUDE_BIN, args, {
      cwd,
      env: { ...process.env },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

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

/**
 * Parse a fenced JSON block from Opus output.
 * Opus often wraps JSON in ```json ... ``` — strip that and parse.
 */
function parseJsonBlock(text) {
  if (!text) return null;
  const fence = /```(?:json)?\s*([\s\S]*?)```/i.exec(text);
  const body = fence ? fence[1].trim() : text.trim();
  try { return JSON.parse(body); } catch { /* fall through */ }
  // Try to locate the first {...} block
  const braceStart = body.indexOf('{');
  const braceEnd   = body.lastIndexOf('}');
  if (braceStart >= 0 && braceEnd > braceStart) {
    try { return JSON.parse(body.slice(braceStart, braceEnd + 1)); } catch {}
  }
  return null;
}

module.exports = { runOneShot, parseJsonBlock };
