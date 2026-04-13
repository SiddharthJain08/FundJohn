'use strict';

const fs = require('fs');
const path = require('path');

/**
 * Workspace context middleware.
 *
 * Injects two blocks into every agent session:
 *   Block A — agent.md (operational frameworks, lessons learned, architecture decisions)
 *   Block B — memory/*.md files (persistent learning: signal patterns, trade learnings,
 *             active tasks, regime context, fund journal)
 *
 * Both blocks are dynamic (no cache_control) so agents always see current state.
 * Memory files accumulate over time — agents read them to build continuity,
 * and write to them after each run to pass knowledge to future invocations.
 */
async function workspaceContext(state, next) {
  const { systemBlocks = [], workspace } = state;
  if (!workspace) return next(state);

  // ── agent.md ──────────────────────────────────────────────────────────────
  let agentMd = '';
  try {
    agentMd = fs.readFileSync(path.join(workspace, 'agent.md'), 'utf8');
  } catch {
    const templatePath = path.join(__dirname, '../../../src/workspace/template/agent.md');
    if (fs.existsSync(templatePath)) {
      agentMd = fs.readFileSync(templatePath, 'utf8');
    }
  }

  // ── memory/*.md ───────────────────────────────────────────────────────────
  let memorySection = '';
  const memDir = path.join(workspace, 'memory');
  if (fs.existsSync(memDir)) {
    // Ordered by relevance: active tasks first, then operational memory
    const ORDER = ['active_tasks', 'fund_journal', 'regime_context', 'signal_patterns', 'trade_learnings'];
    const allFiles = fs.readdirSync(memDir).filter(f => f.endsWith('.md')).sort((a, b) => {
      const ai = ORDER.indexOf(a.replace('.md', ''));
      const bi = ORDER.indexOf(b.replace('.md', ''));
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });

    const parts = [];
    for (const fname of allFiles) {
      try {
        const content = fs.readFileSync(path.join(memDir, fname), 'utf8').trim();
        // Skip files that only contain headers/comments (no real entries yet)
        const hasEntries = content.split('\n').some(l => l.trim() && !l.startsWith('#') && !l.startsWith('<!--') && !l.startsWith('Format:') && !l.startsWith('Status:') && !l.startsWith('Types:'));
        if (hasEntries) {
          const title = fname.replace('.md', '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
          parts.push(`### ${title}\n\n${content}`);
        }
      } catch { /* skip unreadable */ }
    }

    if (parts.length > 0) {
      memorySection = `\n\n---\n\n## Persistent Memory\n\n${parts.join('\n\n---\n\n')}`;
    }
  }

  // ── Also inject /root/.learnings/ if it has content ──────────────────────
  let learningsSection = '';
  const learningsDir = '/root/.learnings';
  if (fs.existsSync(learningsDir)) {
    const learningsParts = [];
    for (const fname of ['LEARNINGS.md', 'ERRORS.md']) {
      const fpath = path.join(learningsDir, fname);
      if (!fs.existsSync(fpath)) continue;
      const content = fs.readFileSync(fpath, 'utf8').trim();
      // Only inject if there are actual entries (not just the header template)
      const lines = content.split('\n').filter(l => l.startsWith('LRN-') || l.startsWith('ERR-'));
      if (lines.length > 0) {
        // Take last 20 entries to bound context size
        const recent = content.split('\n---\n').filter(e => e.trim()).slice(-20).join('\n---\n');
        learningsParts.push(`### ${fname.replace('.md','')}\n\n${recent}`);
      }
    }
    if (learningsParts.length > 0) {
      learningsSection = `\n\n---\n\n## System Learnings (/root/.learnings)\n\n${learningsParts.join('\n\n')}`;
    }
  }

  const workspaceBlock = {
    type: 'text',
    text: `## Workspace Memory (agent.md)\n\n${agentMd}${memorySection}${learningsSection}`,
    _blockType: 'workspace',
    // No cache_control — dynamic block, changes every run
  };

  // Replace existing workspace block or append before runtime block
  const otherBlocks = systemBlocks.filter((b) => b._blockType !== 'workspace');
  const runtimeIdx = otherBlocks.findIndex((b) => b._blockType === 'runtime');

  let newBlocks;
  if (runtimeIdx >= 0) {
    newBlocks = [
      ...otherBlocks.slice(0, runtimeIdx),
      workspaceBlock,
      ...otherBlocks.slice(runtimeIdx),
    ];
  } else {
    newBlocks = [...otherBlocks, workspaceBlock];
  }

  return next({ ...state, systemBlocks: newBlocks });
}

module.exports = workspaceContext;
