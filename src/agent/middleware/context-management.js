'use strict';

const fs = require('fs');
const path = require('path');
const https = require('https');
const { COMPACTION, getContextLimit } = require('../config/models');

const APPROX_TOKENS_PER_CHAR = 0.25; // rough estimate

// Haiku-summarizer parameters for tier-2 eviction
const SUMMARY_MAX_TOKENS = 700;
const SUMMARY_INPUT_CHAR_CAP = 60_000; // ~15k tokens — enough to capture the
                                       // arc; cheaper than feeding raw evict
const SUMMARY_MODEL = 'claude-haiku-4-5-20251001';

function estimateTokens(text) {
  return Math.ceil(typeof text === 'string' ? text.length * APPROX_TOKENS_PER_CHAR : JSON.stringify(text).length * APPROX_TOKENS_PER_CHAR);
}

function estimateMessagesTokens(messages) {
  return messages.reduce((sum, m) => {
    const content = typeof m.content === 'string' ? m.content : JSON.stringify(m.content);
    return sum + estimateTokens(content) + 4; // 4 tokens overhead per message
  }, 0);
}

/**
 * Two-tier context management:
 * Tier 1 (>60% context): truncate large tool call args and redundant tool results
 * Tier 2 (>85% context): evict old messages to filesystem, replace with summary
 */
async function contextManagement(state, next) {
  const { messages, model, workspace, threadId, lastTokenCount } = state;
  const contextLimit = getContextLimit(model || 'claude-sonnet-4-6');

  // Use last known token count if available, else estimate
  const tokenCount = lastTokenCount || estimateMessagesTokens(messages);
  const utilization = tokenCount / contextLimit;

  let processedMessages = messages;

  if (utilization > COMPACTION.tier2_summarize) {
    // Tier 2: evict old messages to file, replace with summary stub
    console.log(`[context] Tier 2 compaction — ${(utilization * 100).toFixed(1)}% utilized`);
    processedMessages = await tier2Evict(messages, workspace, threadId);
  } else if (utilization > COMPACTION.tier1_truncate) {
    // Tier 1: truncate large tool results in older messages
    console.log(`[context] Tier 1 truncation — ${(utilization * 100).toFixed(1)}% utilized`);
    processedMessages = tier1Truncate(messages);
  }

  return next({ ...state, messages: processedMessages, contextUtilization: utilization });
}

function tier1Truncate(messages) {
  const MAX_TOOL_RESULT_CHARS = 4000;
  return messages.map((msg, idx) => {
    // Only truncate older messages (keep last 10 intact)
    if (idx >= messages.length - 10) return msg;
    if (msg.role !== 'tool' && msg.role !== 'user') return msg;

    const content = msg.content;
    if (typeof content === 'string' && content.length > MAX_TOOL_RESULT_CHARS) {
      return {
        ...msg,
        content: content.slice(0, MAX_TOOL_RESULT_CHARS) + '\n[TRUNCATED — full result in thread history]',
      };
    }
    return msg;
  });
}

async function tier2Evict(messages, workspace, threadId) {
  if (!workspace || !threadId) return messages;

  const historyDir = path.join(workspace, '.agents', 'threads', threadId, 'history');
  fs.mkdirSync(historyDir, { recursive: true });

  // Keep last 20 messages, evict the rest
  const KEEP_RECENT = 20;
  if (messages.length <= KEEP_RECENT) return messages;

  const toEvict = messages.slice(0, messages.length - KEEP_RECENT);
  const toKeep = messages.slice(messages.length - KEEP_RECENT);

  const evictFile = path.join(historyDir, `evicted-${Date.now()}.json`);
  fs.writeFileSync(evictFile, JSON.stringify(toEvict, null, 2));

  const llmSummary = await summarizeEvictedTurns(toEvict).catch((err) => {
    console.warn('[context] LLM eviction summary failed, falling back to stub:', err.message);
    return null;
  });

  const summaryText = llmSummary
    ? `[CONTEXT COMPACTED — ${toEvict.length} messages evicted to ${evictFile}]\n\n${llmSummary}\n\n[Full evicted history available via Read on ${evictFile}; key findings should also be in agent.md.]`
    : `[CONTEXT COMPACTED — ${toEvict.length} messages evicted to ${evictFile}. Summary: prior messages covered research and data gathering for the current task. Key findings are in agent.md.]`;

  return [{ role: 'user', content: summaryText }, ...toKeep];
}

/**
 * Compress an array of evicted messages into a 300-700 token narrative.
 * Uses Haiku via the raw HTTPS endpoint (matches src/budget/batch.js style;
 * avoids pulling the SDK into the middleware path). Returns null on any
 * failure — the caller falls back to the legacy static stub.
 */
async function summarizeEvictedTurns(toEvict) {
  if (!process.env.ANTHROPIC_API_KEY) return null;

  const compact = toEvict.map((m) => {
    const c = typeof m.content === 'string' ? m.content : JSON.stringify(m.content);
    return `[${m.role || 'msg'}] ${c}`;
  }).join('\n\n');

  const trimmed = compact.length > SUMMARY_INPUT_CHAR_CAP
    ? compact.slice(0, SUMMARY_INPUT_CHAR_CAP / 2) +
      `\n\n[…middle elided, ${(compact.length - SUMMARY_INPUT_CHAR_CAP).toLocaleString()} chars…]\n\n` +
      compact.slice(-SUMMARY_INPUT_CHAR_CAP / 2)
    : compact;

  const prompt = `You are compressing prior agent conversation turns so the agent can continue without the full history. Produce a faithful summary in <=600 tokens, structured as:

1. **What the task was** (1 line)
2. **What was tried** (bullet list, ≤6 bullets, mention specific tools/data sources)
3. **What was found** (bullet list, ≤6 bullets, only conclusions and key numbers)
4. **What's still open or unresolved**

No flattery. No "in this conversation we...". Quote tickers/dates/numbers verbatim. If a tool result was important, name the tool and the salient field. Skip pleasantries.

--- TURNS TO SUMMARIZE ---
${trimmed}`;

  const body = {
    model: SUMMARY_MODEL,
    max_tokens: SUMMARY_MAX_TOKENS,
    messages: [{ role: 'user', content: prompt }],
  };

  const text = await new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const req = https.request({
      hostname: 'api.anthropic.com',
      path: '/v1/messages',
      method: 'POST',
      headers: {
        'x-api-key':         process.env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
        'content-type':      'application/json',
        'content-length':    Buffer.byteLength(payload),
      },
      timeout: 30_000,
    }, (res) => {
      let data = '';
      res.on('data', (d) => data += d);
      res.on('end', () => {
        try {
          const json = JSON.parse(data);
          if (res.statusCode !== 200) return reject(new Error(`status ${res.statusCode}: ${data.slice(0, 200)}`));
          const out = (json.content || []).map((b) => b.text || '').join('').trim();
          resolve(out || null);
        } catch (e) { reject(e); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('summary request timed out')));
    req.on('error', reject);
    req.write(payload);
    req.end();
  });

  return text;
}

module.exports = contextManagement;
