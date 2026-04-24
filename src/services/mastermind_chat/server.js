'use strict';

/**
 * MasterMindJohn interactive chat service — 127.0.0.1:7871
 *
 * Wraps `claude-bin --session-id / --resume` to expose durable,
 * streaming chat sessions. Each session is one claude-bin
 * conversation, resumed across HTTP requests by its UUID.
 * Messages persist to Postgres (mastermind_chat_sessions /
 * mastermind_chat_messages) for cross-session recall.
 *
 * Endpoints:
 *   POST   /chat/session                 create, returns {id}
 *   GET    /chat/sessions                list recent sessions
 *   POST   /chat/:id/message             SSE stream of stream-json events
 *   GET    /chat/:id/history             full message transcript
 *   POST   /chat/:id/archive             mark archived
 *   GET    /health                       liveness
 */

const express = require('express');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawn } = require('child_process');
const { randomUUID } = require('crypto');

require('dotenv').config({ path: path.join(__dirname, '../../../.env') });

const { query } = require('../../database/postgres');
const { PERSONA } = require('./persona');

const PORT = parseInt(process.env.MASTERMIND_CHAT_PORT || '7871', 10);
const BIND = process.env.MASTERMIND_CHAT_BIND || '127.0.0.1';

// Stable CWD so claude-bin session state resolves to the same project key
// across resumes. Created at startup if missing.
const SESSION_CWD = process.env.MASTERMIND_CHAT_CWD
  || '/root/openclaw/workspaces/mastermind-chat';

const MODEL = process.env.MASTERMIND_CHAT_MODEL || 'claude-opus-4-7[1m]';
const MAX_BUDGET_USD = process.env.MASTERMIND_CHAT_MAX_BUDGET || '5.00';
const CLAUDE_BIN = process.env.CLAUDE_BIN || '/usr/local/bin/claude-bin';
const SNAPSHOT_PY = path.join(__dirname, 'snapshot.py');
const PY_BIN = process.env.PYTHON_BIN || '/usr/bin/python3';

// ─────────────────────── Startup bootstrap ──────────────────────────────────
fs.mkdirSync(SESSION_CWD, { recursive: true });

const app = express();
app.use(express.json({ limit: '1mb' }));

// ─────────────────────── Snapshot (dashboard context) ───────────────────────
function buildSnapshot() {
  return new Promise((resolve) => {
    const child = spawn(PY_BIN, [SNAPSHOT_PY], { cwd: SESSION_CWD });
    let out = '';
    let err = '';
    child.stdout.on('data', (d) => { out += d.toString(); });
    child.stderr.on('data', (d) => { err += d.toString(); });
    child.on('close', () => {
      try {
        resolve(JSON.parse(out));
      } catch (_) {
        resolve({ snapshot_error: err || 'snapshot returned no valid JSON' });
      }
    });
    child.on('error', (e) => resolve({ snapshot_error: e.message }));
  });
}

function composeFirstTurn(snapshot, userText) {
  return (
    PERSONA
    + '\n\n[DASHBOARD SNAPSHOT — point-in-time, cached for this session]\n'
    + '```json\n'
    + JSON.stringify(snapshot, null, 2)
    + '\n```\n\n'
    + '[USER MESSAGE]\n'
    + userText
  );
}

// ─────────────────────── DB helpers ─────────────────────────────────────────
async function sessionMessageCount(sessionId) {
  const r = await query(
    'SELECT count(*)::int AS n FROM mastermind_chat_messages WHERE session_id = $1',
    [sessionId]
  );
  return r.rows[0]?.n || 0;
}

async function insertMessage(row) {
  const { session_id, role, content, paper_id = null, strategy_id = null,
          tokens_in = null, tokens_out = null, cost_usd = null } = row;
  await query(
    `INSERT INTO mastermind_chat_messages
       (session_id, role, content, paper_id, strategy_id, tokens_in, tokens_out, cost_usd)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
    [session_id, role, content, paper_id, strategy_id, tokens_in, tokens_out, cost_usd]
  );
  await query(
    'UPDATE mastermind_chat_sessions SET last_active_at = NOW() WHERE id = $1',
    [session_id]
  );
}

async function bumpSessionCost(sessionId, deltaUsd) {
  if (!deltaUsd || !isFinite(deltaUsd)) return;
  await query(
    'UPDATE mastermind_chat_sessions SET total_cost_usd = total_cost_usd + $2 WHERE id = $1',
    [sessionId, deltaUsd]
  );
}

// ─────────────────────── Sessions ───────────────────────────────────────────
app.post('/chat/session', async (req, res) => {
  const id = randomUUID();
  const title = (req.body?.title || '').slice(0, 200) || null;
  try {
    await query(
      `INSERT INTO mastermind_chat_sessions (id, title, claude_session_id)
       VALUES ($1::uuid, $2, $1::text)`,
      [id, title]
    );
    res.json({ id, title, started_at: new Date().toISOString() });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/chat/sessions', async (_req, res) => {
  try {
    const r = await query(
      `SELECT id, title, started_at, last_active_at, total_cost_usd, status
         FROM mastermind_chat_sessions
        WHERE status = 'active'
        ORDER BY last_active_at DESC
        LIMIT 50`
    );
    res.json({ sessions: r.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/chat/:id/history', async (req, res) => {
  try {
    const r = await query(
      `SELECT id, role, content, created_at, paper_id, strategy_id,
              tokens_in, tokens_out, cost_usd
         FROM mastermind_chat_messages
        WHERE session_id = $1
        ORDER BY id ASC`,
      [req.params.id]
    );
    res.json({ messages: r.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/chat/:id/archive', async (req, res) => {
  try {
    await query(
      `UPDATE mastermind_chat_sessions SET status='archived' WHERE id=$1`,
      [req.params.id]
    );
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ─────────────────────── Chat streaming ─────────────────────────────────────
app.post('/chat/:id/message', async (req, res) => {
  const sessionId = req.params.id;
  const userText = (req.body?.text || '').toString();
  if (!userText.trim()) {
    return res.status(400).json({ error: 'text required' });
  }

  // Validate session exists.
  const sr = await query(
    'SELECT id, claude_session_id FROM mastermind_chat_sessions WHERE id=$1',
    [sessionId]
  ).catch(() => ({ rows: [] }));
  if (!sr.rows.length) {
    return res.status(404).json({ error: 'session not found' });
  }

  const turnCount = await sessionMessageCount(sessionId);
  const isFirstTurn = turnCount === 0;

  let promptText = userText;
  if (isFirstTurn) {
    const snapshot = await buildSnapshot();
    promptText = composeFirstTurn(snapshot, userText);
    await query(
      'UPDATE mastermind_chat_sessions SET last_snapshot_at = NOW() WHERE id = $1',
      [sessionId]
    );
  }

  // Persist user turn immediately (content stores the raw user text, not the
  // preamble-wrapped version — the wrapper is ephemeral framing).
  await insertMessage({
    session_id: sessionId,
    role: 'user',
    content: { text: userText, first_turn: isFirstTurn },
  });

  // Open SSE to client.
  res.set({
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  res.flushHeaders?.();
  const sse = (event, data) => {
    res.write(`event: ${event}\n`);
    res.write(`data: ${JSON.stringify(data)}\n\n`);
  };

  // Spawn claude-bin. First turn uses --session-id; subsequent use --resume.
  const args = [
    '-p', promptText,
    '--output-format', 'stream-json',
    '--verbose',
    '--model', MODEL,
    '--permission-mode', 'bypassPermissions',
    '--max-budget-usd', MAX_BUDGET_USD,
  ];
  if (isFirstTurn) args.push('--session-id', sessionId);
  else args.push('--resume', sessionId);

  const child = spawn(CLAUDE_BIN, args, {
    cwd: SESSION_CWD,
    env: { ...process.env, HOME: process.env.HOME || os.homedir() },
  });

  let stdoutBuf = '';
  let lastResultCost = null;

  child.stdout.on('data', async (chunk) => {
    stdoutBuf += chunk.toString();
    let nl;
    while ((nl = stdoutBuf.indexOf('\n')) >= 0) {
      const line = stdoutBuf.slice(0, nl).trim();
      stdoutBuf = stdoutBuf.slice(nl + 1);
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { sse('raw', { line }); continue; }
      sse(ev.type || 'event', ev);

      try {
        if (ev.type === 'assistant' && ev.message?.content) {
          const texts = ev.message.content.filter(c => c.type === 'text').map(c => c.text).join('');
          const tools = ev.message.content.filter(c => c.type === 'tool_use');
          if (texts) {
            await insertMessage({
              session_id: sessionId, role: 'assistant',
              content: { text: texts, message_id: ev.message.id },
              tokens_in: ev.message.usage?.input_tokens,
              tokens_out: ev.message.usage?.output_tokens,
            });
          }
          for (const t of tools) {
            await insertMessage({
              session_id: sessionId, role: 'tool_use',
              content: { id: t.id, name: t.name, input: t.input },
            });
          }
        } else if (ev.type === 'user' && ev.message?.content) {
          // Tool results returned to assistant
          for (const c of (ev.message.content || [])) {
            if (c.type === 'tool_result') {
              await insertMessage({
                session_id: sessionId, role: 'tool_result',
                content: { tool_use_id: c.tool_use_id, result: c.content },
              });
            }
          }
        } else if (ev.type === 'result') {
          lastResultCost = ev.total_cost_usd;
        }
      } catch (persistErr) {
        console.error('[chat] persist error:', persistErr.message);
      }
    }
  });

  child.stderr.on('data', (d) => {
    sse('stderr', { line: d.toString() });
  });

  child.on('close', async (code) => {
    if (lastResultCost) await bumpSessionCost(sessionId, lastResultCost);
    sse('done', { code, cost_usd: lastResultCost });
    res.end();
  });

  child.on('error', (e) => {
    sse('error', { message: e.message });
    try { res.end(); } catch (_) { /* ignore */ }
  });

  req.on('close', () => {
    if (!child.killed) child.kill('SIGTERM');
  });
});

app.get('/health', (_req, res) => res.json({ ok: true, port: PORT }));

app.listen(PORT, BIND, () => {
  console.log(`[mastermind-chat] listening on http://${BIND}:${PORT} (cwd=${SESSION_CWD}, model=${MODEL})`);
});
