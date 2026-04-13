'use strict';

/**
 * Cache control middleware — tags Anthropic cache breakpoints on system blocks.
 * Block 1 (static prompt) + Block 2 (skills manifest) get cached.
 * Block 3 (workspace/agent.md) + Block 4 (runtime) are uncached.
 * Also tags the last tool in the tool list with a cache breakpoint.
 */
async function cacheControl(state, next) {
  const { systemBlocks = [], tools = [] } = state;

  // Tag cache breakpoints on static + skills blocks (outermost cached prefix)
  const processedBlocks = systemBlocks.map((block) => {
    if (block._blockType === 'static' || block._blockType === 'skills') {
      // Ensure cache_control is set for Anthropic prompt caching
      return { ...block, cache_control: { type: 'ephemeral' } };
    }
    // Workspace and runtime blocks are dynamic — no cache
    const { cache_control, ...rest } = block;
    return rest;
  });

  // Tag last tool with cache breakpoint (tools list is stable within a session)
  let processedTools = tools;
  if (tools.length > 0) {
    processedTools = tools.map((tool, idx) => {
      if (idx === tools.length - 1) {
        return { ...tool, cache_control: { type: 'ephemeral' } };
      }
      return tool;
    });
  }

  return next({ ...state, systemBlocks: processedBlocks, tools: processedTools });
}

module.exports = cacheControl;
