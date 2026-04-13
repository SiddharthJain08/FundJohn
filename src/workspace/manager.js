'use strict';

const fs = require('fs');
const path = require('path');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';
const WORKSPACES_DIR = path.join(OPENCLAW_DIR, 'workspaces');
const TEMPLATE_DIR = path.join(__dirname, 'template');

/**
 * Get or create a workspace directory.
 * @param {string} workspaceId — 'default' or a UUID
 * @returns {string} — absolute path to workspace directory
 */
async function getOrCreate(workspaceId = 'default') {
  const workspacePath = path.join(WORKSPACES_DIR, workspaceId);

  if (!fs.existsSync(workspacePath)) {
    await create(workspaceId, workspacePath);
  }

  return workspacePath;
}

async function create(workspaceId, workspacePath) {
  console.log(`[workspace] Creating workspace: ${workspaceId}`);

  // Create directory structure
  const dirs = [
    '',
    'work',
    'results',
    'data',
    'tools',
    'tools/docs',
    '.agents',
    '.agents/threads',
    '.agents/skills',
    '.agents/verdict-cache',
    '.agents/user',
  ];

  for (const dir of dirs) {
    fs.mkdirSync(path.join(workspacePath, dir), { recursive: true });
  }

  // Copy template files
  copyTemplate(TEMPLATE_DIR, workspacePath);

  console.log(`[workspace] Workspace created at ${workspacePath}`);
  return workspacePath;
}

function copyTemplate(templateDir, targetDir) {
  if (!fs.existsSync(templateDir)) return;

  const entries = fs.readdirSync(templateDir, { withFileTypes: true });
  for (const entry of entries) {
    const srcPath = path.join(templateDir, entry.name);
    const destPath = path.join(targetDir, entry.name);

    if (entry.isDirectory()) {
      fs.mkdirSync(destPath, { recursive: true });
      copyTemplate(srcPath, destPath);
    } else {
      // Don't overwrite existing files (preserve agent memory)
      if (!fs.existsSync(destPath)) {
        fs.copyFileSync(srcPath, destPath);
      }
    }
  }
}

/**
 * List all workspaces.
 */
function list() {
  if (!fs.existsSync(WORKSPACES_DIR)) return [];
  return fs.readdirSync(WORKSPACES_DIR, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => ({ id: d.name, path: path.join(WORKSPACES_DIR, d.name) }));
}

/**
 * Delete a workspace (confirmation required by caller).
 */
function remove(workspaceId) {
  const workspacePath = path.join(WORKSPACES_DIR, workspaceId);
  if (!fs.existsSync(workspacePath)) throw new Error(`Workspace ${workspaceId} not found`);
  fs.rmSync(workspacePath, { recursive: true, force: true });
}

/**
 * Get workspace preferences.
 */
function getPreferences(workspacePath) {
  const prefsPath = path.join(workspacePath, '.agents', 'user', 'preferences.json');
  if (!fs.existsSync(prefsPath)) return getDefaultPreferences();
  return JSON.parse(fs.readFileSync(prefsPath, 'utf8'));
}

function getDefaultPreferences() {
  const templatePath = path.join(TEMPLATE_DIR, '.agents', 'user', 'preferences.json');
  if (fs.existsSync(templatePath)) {
    return JSON.parse(fs.readFileSync(templatePath, 'utf8'));
  }
  return {};
}

/**
 * Initialize the default workspace (called at startup).
 */
async function initDefault() {
  return getOrCreate('default');
}

module.exports = { getOrCreate, create, list, remove, getPreferences, initDefault };
