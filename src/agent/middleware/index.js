'use strict';

const cacheControl       = require('./cache-control');
const secretRedaction    = require('./secret-redaction');
const steering           = require('./steering');
const skillsLoader       = require('./skills-loader');
const workspaceContext   = require('./workspace-context');
const contextManagement  = require('./context-management');
const largeResultEviction = require('./large-result-eviction');
const multimodalInjection = require('./multimodal-injection');
const hitl               = require('./hitl');

// Middleware ordering — outermost runs first, innermost closest to LLM call
const STACK = [
  cacheControl,           // 1. Tag cache breakpoints (outermost — determines cached prefix)
  secretRedaction,        // 2. Scrub API keys from all tool results
  steering,               // 3. Drain Redis steering queue, inject user messages
  skillsLoader,           // 4. Activate skills, expose bound tools
  workspaceContext,       // 5. Append agent.md to system message
  contextManagement,      // 6. Two-tier compaction (truncation + summarization)
  largeResultEviction,    // 7. >40k token results → file + preview
  multimodalInjection,    // 8. File reads → base64 content blocks
  hitl,                   // 9. Plan mode + question interrupts
];

/**
 * Compose middleware stack into a single function.
 * Each middleware receives (state, next) — must call next(state) to proceed.
 * @param {Function} core — the actual LLM call function
 * @returns {Function} — composed middleware chain
 */
function compose(core) {
  return STACK.reduceRight((next, middleware) => {
    return (state) => middleware(state, next);
  }, core);
}

/**
 * Run the full middleware stack around an LLM call.
 * @param {Object} state — { messages, systemBlocks, threadId, workspace, model }
 * @param {Function} llmCall — (state) => Promise<response>
 */
async function run(state, llmCall) {
  const chain = compose(llmCall);
  return chain(state);
}

module.exports = { run, compose, STACK };
