'use strict';

/**
 * memory-writer.js
 *
 * Node.js utility for writing to the workspace memory directory.
 * Called by bot.js to track BotJohn's active tasks and fund journal entries.
 * Python scripts (research_report.py, trade_agent.py) have their own append logic.
 *
 * Memory directory: workspace/memory/
 *   active_tasks.md  — BotJohn task queue, persists across bot restarts
 *   fund_journal.md  — daily operational log
 */

const fs   = require('fs');
const path = require('path');

const DEFAULT_WORKSPACE = process.env.OPENCLAW_DIR
  ? path.join(process.env.OPENCLAW_DIR, 'workspaces/default')
  : '/root/openclaw/workspaces/default';

function memDir(workspace) {
  return path.join(workspace || DEFAULT_WORKSPACE, 'memory');
}

function appendToFile(filePath, entry) {
  try {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.appendFileSync(filePath, entry + '\n');
  } catch (e) {
    console.warn(`[memory] Failed to write ${filePath}: ${e.message}`);
  }
}

function readFile(filePath) {
  try { return fs.readFileSync(filePath, 'utf8'); } catch { return ''; }
}

function writeFile(filePath, content) {
  try {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, content, 'utf8');
  } catch (e) {
    console.warn(`[memory] Failed to write ${filePath}: ${e.message}`);
  }
}

// ── Fund Journal ──────────────────────────────────────────────────────────────

/**
 * Append an entry to fund_journal.md.
 * @param {'DECISION'|'OBSERVATION'|'ERROR'|'TASK_OPEN'|'TASK_CLOSE'|'REGIME_NOTE'} type
 * @param {string} entry
 * @param {string} [workspace]
 */
function journalEntry(type, entry, workspace) {
  const ts = new Date().toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
  const line = `${ts} | ${type} | ${entry}`;
  appendToFile(path.join(memDir(workspace), 'fund_journal.md'), line);
}

// ── Active Tasks ──────────────────────────────────────────────────────────────

/**
 * Open a new task in active_tasks.md.
 * Returns a taskId (timestamp-based) for later closure.
 */
function openTask(description, notes, workspace) {
  const ts = new Date().toISOString().slice(0, 10);
  const taskId = Date.now().toString(36);
  const line = `[OPEN] ${ts} | ${description} | ${notes || ''} | id:${taskId}`;
  appendToFile(path.join(memDir(workspace), 'active_tasks.md'), line);
  journalEntry('TASK_OPEN', description, workspace);
  return taskId;
}

/**
 * Close a task by taskId.
 */
function closeTask(taskId, outcome, workspace) {
  const fpath = path.join(memDir(workspace), 'active_tasks.md');
  let content = readFile(fpath);
  const updated = content.replace(
    new RegExp(`(\\[OPEN\\][^\\n]*id:${taskId}[^\\n]*)`, 'g'),
    (match) => match.replace('[OPEN]', '[CLOSED]') + ` → ${outcome}`
  );
  writeFile(fpath, updated);
  journalEntry('TASK_CLOSE', `id:${taskId} — ${outcome}`, workspace);
}

/**
 * Update a task status to IN_PROGRESS.
 */
function progressTask(taskId, note, workspace) {
  const fpath = path.join(memDir(workspace), 'active_tasks.md');
  let content = readFile(fpath);
  const updated = content.replace(
    new RegExp(`(\\[OPEN\\][^\\n]*id:${taskId}[^\\n]*)`, 'g'),
    (match) => match.replace('[OPEN]', '[IN_PROGRESS]') + (note ? ` [${note}]` : '')
  );
  writeFile(fpath, updated);
}

/**
 * Read all OPEN and IN_PROGRESS tasks.
 * Returns array of task line strings.
 */
function getActiveTasks(workspace) {
  const content = readFile(path.join(memDir(workspace), 'active_tasks.md'));
  return content.split('\n').filter(l => l.startsWith('[OPEN]') || l.startsWith('[IN_PROGRESS]'));
}

module.exports = { journalEntry, openTask, closeTask, progressTask, getActiveTasks };
