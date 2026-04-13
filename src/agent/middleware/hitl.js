'use strict';

/**
 * Human-in-the-loop (HITL) middleware.
 * Intercepts plan mode and clarifying question patterns from agent output.
 * If agent asks a question or enters plan mode, pause and await operator input.
 */
async function hitl(state, next) {
  // HITL logic runs AFTER the LLM call (post-processing hook)
  // Pass through to LLM, then check response
  const response = await next(state);
  if (!response) return response;

  const text = extractText(response);
  if (!text) return response;

  // Detect plan mode request: agent outputs [PLAN MODE] or [AWAITING APPROVAL]
  if (text.includes('[PLAN MODE]') || text.includes('[AWAITING APPROVAL]')) {
    response._hitl = { type: 'plan_mode', text };
  }

  // Detect clarifying question: agent outputs [QUESTION: ...]
  const questionMatch = text.match(/\[QUESTION:\s*(.+?)\]/s);
  if (questionMatch) {
    response._hitl = { type: 'question', question: questionMatch[1].trim(), text };
  }

  return response;
}

function extractText(response) {
  if (!response) return '';
  if (typeof response.content === 'string') return response.content;
  if (Array.isArray(response.content)) {
    return response.content
      .filter((b) => b.type === 'text')
      .map((b) => b.text)
      .join('\n');
  }
  return '';
}

module.exports = hitl;
