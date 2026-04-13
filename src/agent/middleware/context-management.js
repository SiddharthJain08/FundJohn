'use strict';

const fs = require('fs');
const path = require('path');
const { COMPACTION, getContextLimit } = require('../config/models');

const APPROX_TOKENS_PER_CHAR = 0.25; // rough estimate

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

  const summary = {
    role: 'user',
    content: `[CONTEXT COMPACTED — ${toEvict.length} messages evicted to ${evictFile}. Summary: prior messages covered research and data gathering for the current task. Key findings are in agent.md.]`,
  };

  return [summary, ...toKeep];
}

module.exports = contextManagement;
