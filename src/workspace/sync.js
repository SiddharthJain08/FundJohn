'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

/**
 * SHA-256 manifest diffing for workspace reconnect.
 * Detects which files changed between sessions.
 */

function buildManifest(dir, baseDir = dir) {
  const manifest = {};
  if (!fs.existsSync(dir)) return manifest;

  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    const relPath = path.relative(baseDir, fullPath);

    // Skip hidden dirs except .agents
    if (entry.name.startsWith('.') && entry.name !== '.agents') continue;
    // Skip node_modules, tools (auto-generated)
    if (entry.name === 'node_modules' || entry.name === 'tools') continue;

    if (entry.isDirectory()) {
      Object.assign(manifest, buildManifest(fullPath, baseDir));
    } else {
      const content = fs.readFileSync(fullPath);
      manifest[relPath] = crypto.createHash('sha256').update(content).digest('hex');
    }
  }
  return manifest;
}

/**
 * Diff two manifests. Returns { added, removed, changed }.
 */
function diff(previous, current) {
  const added   = Object.keys(current).filter((k) => !previous[k]);
  const removed = Object.keys(previous).filter((k) => !current[k]);
  const changed = Object.keys(current).filter((k) => previous[k] && previous[k] !== current[k]);
  return { added, removed, changed };
}

/**
 * Save manifest to .agents/threads/{tid}/manifest.json.
 */
function saveManifest(workspacePath, threadId, manifest) {
  const manifestDir = path.join(workspacePath, '.agents', 'threads', threadId);
  fs.mkdirSync(manifestDir, { recursive: true });
  fs.writeFileSync(
    path.join(manifestDir, 'manifest.json'),
    JSON.stringify({ timestamp: new Date().toISOString(), files: manifest }, null, 2)
  );
}

/**
 * Load manifest from previous session.
 */
function loadManifest(workspacePath, threadId) {
  const manifestPath = path.join(workspacePath, '.agents', 'threads', threadId, 'manifest.json');
  if (!fs.existsSync(manifestPath)) return {};
  return JSON.parse(fs.readFileSync(manifestPath, 'utf8')).files || {};
}

/**
 * On reconnect: diff workspace state to identify what changed since last session.
 */
function reconcile(workspacePath, threadId) {
  const previous = loadManifest(workspacePath, threadId);
  const current  = buildManifest(workspacePath);
  const changes  = diff(previous, current);
  saveManifest(workspacePath, threadId, current);
  return changes;
}

module.exports = { buildManifest, diff, saveManifest, loadManifest, reconcile };
