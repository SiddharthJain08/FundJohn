'use strict';

/**
 * botjohn-direct.js
 *
 * Singular BotJohn agent — no swarm, no subagent spawning.
 * Calls claude-bin directly with conversation history + live system context
 * injected into the prompt. Auth handled by claude-bin (OAuth via claudebot user).
 */

const fs     = require('fs');
const path   = require('path');
const { spawn } = require('child_process');

const chatHistory       = require('./chat-history');
const { buildSystemContext } = require('./system-context');

const CLAUDE_BIN  = process.env.CLAUDE_BIN  || '/usr/local/bin/claude-bin';
const CLAUDE_UID  = parseInt(process.env.CLAUDE_UID  || '1001', 10);
const CLAUDE_GID  = parseInt(process.env.CLAUDE_GID  || '1001', 10);
const CLAUDE_HOME = process.env.CLAUDE_HOME || '/home/claudebot';
const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';

const SYSTEM_PROMPT = fs.readFileSync(
  path.join(__dirname, 'prompts/subagents/botjohn.md'),
  'utf8'
);

/**
 * Render conversation history + new message into a single flat prompt
 * that claude-bin can process in non-interactive mode.
 */
function buildPrompt(history, participantName, message, systemCtx) {
  const lines = [];

  lines.push(SYSTEM_PROMPT);
  if (systemCtx) lines.push('\n' + systemCtx);

  if (history.length > 0) {
    lines.push('\n## Conversation History');
    for (const turn of history) {
      const speaker = turn.role === 'user' ? `[${participantName}]` : 'BotJohn';
      lines.push(`\n**${speaker}:** ${turn.content}`);
    }
  }

  lines.push(`\n## Current Message`);
  lines.push(`\n**[${participantName}]:** ${message}`);
  lines.push('\nRespond as BotJohn:');

  return lines.join('\n');
}

async function respond({ participantId, participantName, participantType, channelId, message, cycleId }) {
  const [history, systemCtx] = await Promise.all([
    chatHistory.loadHistory(participantId),
    buildSystemContext(),
  ]);

  const prompt = buildPrompt(history, participantName, message, systemCtx);

  const text = await runClaudeBin(prompt, { cycleId });

  await chatHistory.saveExchange(
    participantId, participantName, participantType, channelId, message, text
  );

  return { output: text };
}

function runClaudeBin(prompt, { cycleId } = {}) {
  return new Promise((resolve, reject) => {
    const args = [
      '--dangerously-skip-permissions',
      '-p', prompt,
      '--output-format', 'json',
      '--model', 'claude-sonnet-4-6',
      '--effort', 'medium',
    ];

    const child = spawn(CLAUDE_BIN, args, {
      uid: CLAUDE_UID,
      gid: CLAUDE_GID,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        HOME:       CLAUDE_HOME,
        CLAUDE_HOME,
        // Cycle-cache namespace for Python tools — shared across parallel
        // /diligence fan-outs so the second-Nth ticker hits Redis on any
        // shared data fetches (regime, sector indices, macro snapshots).
        ...(cycleId ? { CYCLE_ID: String(cycleId) } : {}),
      },
      cwd: OPENCLAW_DIR,
    });

    let stdout = '';
    let stderr = '';
    child.stdout.on('data', d => { stdout += d.toString(); });
    child.stderr.on('data', d => { stderr += d.toString(); });

    const timeout = setTimeout(() => {
      child.kill('SIGTERM');
      reject(new Error('BotJohn timed out after 120s'));
    }, 120_000);

    child.on('exit', (code) => {
      clearTimeout(timeout);
      if (code !== 0 && !stdout) {
        return reject(new Error(`claude-bin exited ${code}: ${stderr.slice(0, 200)}`));
      }
      try {
        const parsed = JSON.parse(stdout);
        resolve(parsed.result ?? parsed.message ?? stdout);
      } catch {
        resolve(stdout.trim() || '(no response)');
      }
    });
  });
}

module.exports = { respond };
