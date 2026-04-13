'use strict';

const fs   = require('fs');
const path = require('path');

const MAX_RESULT_CHARS   = 160_000; // ~40k tokens — evict to disk
const PREVIEW_CHARS      = 2_000;   // ~500 tokens head/tail each

// Session pruning defaults — overridden per subagent type via state.pruningConfig
const DEFAULT_PRUNING = {
  enabled:             true,
  maxToolResultTokens: 2000,  // cap old results at this many tokens (~4 chars/tok)
  maxToolResultAge:    10,    // messages from end before pruning kicks in
};

/**
 * Large result eviction — two layers:
 *
 * 1. SIZE eviction: tool results > MAX_RESULT_CHARS are written to disk and
 *    replaced with head + tail + filepath. Always active.
 *
 * 2. AGE pruning: tool results older than maxToolResultAge messages from the
 *    end of the conversation are truncated to maxToolResultTokens * 4 chars.
 *    Applied per-subagent based on state.pruningConfig (set by swarm.js from
 *    subagent-types.json defaults/overrides).
 */
async function largeResultEviction(state, next) {
  const { messages, workspace, threadId, pruningConfig } = state;
  if (!messages || !messages.length) return next(state);

  const pruning  = { ...DEFAULT_PRUNING, ...(pruningConfig || {}) };
  const maxChars = pruning.maxToolResultTokens * 4; // ~4 chars per token
  const maxAge   = pruning.maxToolResultAge;

  const evictDir = workspace && threadId
    ? path.join(workspace, '.agents', 'threads', threadId, 'evicted')
    : null;

  let modified = false;
  const totalMessages = messages.length;

  const processedMessages = messages.map((msg, idx) => {
    if (msg.role !== 'tool') return msg;

    const content    = typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content);
    const ageFromEnd = totalMessages - 1 - idx; // 0 = most recent

    // Layer 1: SIZE eviction — always active
    if (content.length > MAX_RESULT_CHARS) {
      modified = true;
      if (evictDir) {
        fs.mkdirSync(evictDir, { recursive: true });
        const filename = `tool-result-${msg.tool_use_id || Date.now()}.txt`;
        const filePath = path.join(evictDir, filename);
        fs.writeFileSync(filePath, content);
        const head    = content.slice(0, PREVIEW_CHARS);
        const tail    = content.slice(-PREVIEW_CHARS);
        const preview = `[LARGE RESULT EVICTED — ${content.length} chars → ${filePath}]\n\n--- HEAD ---\n${head}\n\n--- TAIL ---\n${tail}\n\n[Read full result: ${filePath}]`;
        return { ...msg, content: preview };
      }
      return { ...msg, content: content.slice(0, PREVIEW_CHARS) + `\n\n[TRUNCATED — ${content.length} chars total]` };
    }

    // Layer 2: AGE pruning — only for old results exceeding token cap
    if (pruning.enabled && ageFromEnd > maxAge && content.length > maxChars) {
      modified = true;
      return {
        ...msg,
        content: content.slice(0, maxChars) +
          `\n\n[PRUNED — result is ${ageFromEnd} messages old, capped at ${pruning.maxToolResultTokens} tokens]`,
      };
    }

    return msg;
  });

  if (modified) console.log('[largeResultEviction] Applied size/age eviction to message history');
  return next({ ...state, messages: processedMessages });
}

module.exports = largeResultEviction;
