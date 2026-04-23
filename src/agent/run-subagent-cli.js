#!/usr/bin/env node
'use strict';

/**
 * run-subagent-cli.js — Invoke a named subagent with injected context.
 *
 * Usage:
 *   node src/agent/run-subagent-cli.js --type tradejohn --ticker 2026-04-18 \
 *     [--workspace /path] [--context-file /tmp/ctx.json]
 *
 * Stdout/stderr are piped directly from claude-bin.
 * Exit code mirrors claude-bin exit code.
 */

const fs   = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const CLAUDE_BIN   = process.env.CLAUDE_BIN   || '/usr/local/bin/claude-bin';
const CLAUDE_UID   = parseInt(process.env.CLAUDE_UID  || '1001', 10);
const CLAUDE_GID   = parseInt(process.env.CLAUDE_GID  || '1001', 10);
const CLAUDE_HOME  = process.env.CLAUDE_HOME  || '/home/claudebot';

function getArg(name, defaultVal = null) {
  const idx = process.argv.indexOf(name);
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : defaultVal;
}

const type        = getArg('--type',      'tradejohn');
const ticker      = getArg('--ticker',    '');
const workspace   = getArg('--workspace', path.join(OPENCLAW_DIR, 'workspaces/default'));
const contextFile = getArg('--context-file');

if (!type) {
  console.error('Usage: run-subagent-cli.js --type <type> [--ticker ...] [--context-file ...]');
  process.exit(1);
}

// Load context file → additional prompt block + template vars
let additionalContext = '';
let templateVars = {};
if (contextFile) {
  if (!fs.existsSync(contextFile)) {
    console.error(`[run-subagent-cli] Context file not found: ${contextFile}`);
    process.exit(1);
  }
  try {
    const raw = fs.readFileSync(contextFile, 'utf8');
    const ctx = JSON.parse(raw);
    // Extract UPPER_SNAKE_CASE string keys as prompt template variables (e.g. SEARCH_THEME)
    for (const [k, v] of Object.entries(ctx)) {
      if (/^[A-Z][A-Z0-9_]*$/.test(k) && typeof v === 'string') templateVars[k] = v;
    }
    additionalContext = '## Injected Context\n```json\n' + JSON.stringify(ctx, null, 2) + '\n```';
  } catch (e) {
    console.error(`[run-subagent-cli] Failed to parse context file: ${e.message}`);
    process.exit(1);
  }
}

// Build the full prompt (injects skills, template vars, runtime preamble)
const { buildPrompt } = require(path.join(OPENCLAW_DIR, 'src/agent/subagents/types'));
let prompt;
try {
  prompt = buildPrompt(type, ticker, workspace, additionalContext, templateVars);
} catch (e) {
  console.error(`[run-subagent-cli] buildPrompt failed for type "${type}": ${e.message}`);
  process.exit(1);
}

// Resolve model config + subagent definition
const { getModelForSubagent } = require(path.join(OPENCLAW_DIR, 'src/agent/config/models'));
const typesConfig = JSON.parse(
  fs.readFileSync(path.join(OPENCLAW_DIR, 'src/agent/config/subagent-types.json'), 'utf8')
);
const def         = typesConfig.types[type] || {};
const modelConfig = getModelForSubagent(type);

const effort  = def.effortLevel  || 'medium';
const budget  = String(def.maxBudgetUsd || 0.30);
const bare    = def.bare === true;

console.error(`[run-subagent-cli] Spawning ${type} | model=${modelConfig.model} effort=${effort} budget=$${budget}${bare ? ' bare' : ''}`);

// Pass the prompt via stdin rather than argv so batches of hundreds of papers
// (tens to hundreds of KB) don't trip the OS E2BIG argv limit. `--print`
// without a positional prompt reads from stdin.
const claudeArgs = [
  '--dangerously-skip-permissions',
  '--print',
  '--output-format', 'json',
  '--model', modelConfig.model,
  '--effort', effort,
  '--max-budget-usd', budget,
];
// Bare mode skips CLAUDE.md auto-discovery, hooks, memory, and skills — used
// by self-contained subagents (e.g. mastermind in corpus mode) to minimise
// token overhead.
if (bare) claudeArgs.splice(1, 0, '--bare');

const child = spawn(CLAUDE_BIN, claudeArgs, {
  cwd: OPENCLAW_DIR,
  uid: CLAUDE_UID,
  gid: CLAUDE_GID,
  env: {
    ...process.env,
    HOME:      CLAUDE_HOME,
    CLAUDE_HOME,
    TICKER:    ticker,
    WORKSPACE: workspace,
    OPENCLAW_DIR,
  },
  stdio: ['pipe', 'pipe', 'pipe'],
});

// Write the prompt to stdin, then close so the child sees EOF.
child.stdin.write(prompt);
child.stdin.end();

child.stdout.pipe(process.stdout);
child.stderr.pipe(process.stderr);

child.on('exit', (code) => {
  console.error(`[run-subagent-cli] ${type} exited with code ${code}`);
  process.exit(code ?? 1);
});

child.on('error', (err) => {
  console.error(`[run-subagent-cli] spawn error: ${err.message}`);
  process.exit(1);
});
