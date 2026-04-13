'use strict';

/**
 * Hash-based integrity checker for agent files, skills, and middleware.
 * Generates a SHA-256 manifest at deploy time; verifies on every boot.
 */

const crypto = require('crypto');
const fs     = require('fs');
const path   = require('path');

const ROOT          = path.join(__dirname, '../../');
const MANIFEST_PATH = path.join(ROOT, 'src/agent/config/integrity-manifest.json');

// Files and glob patterns to include in the manifest
const WATCH_TARGETS = [
  // Agent entrypoints
  'src/agent/flash.js',
  'src/agent/main.js',
  'src/agent/subagents/swarm.js',
  'src/agent/subagents/types.js',
  'src/agent/subagents/lifecycle.js',
  // Security modules themselves
  'src/security/integrity.js',
  'src/security/skill-vetter.js',
  'src/security/verification.js',
];

const WATCH_DIRS = [
  { dir: 'src/agent/middleware', ext: '.js' },
  { dir: 'src/skills',          ext: '.js', recursive: true },
];

// Markdown config files
const WATCH_MD = [
  'CLAUDE.md',
  'AGENTS.md',
  'IDENTITY.md',
  'SOUL.md',
];

function hashFile(filePath) {
  const abs = path.isAbsolute(filePath) ? filePath : path.join(ROOT, filePath);
  if (!fs.existsSync(abs)) return null;
  const content = fs.readFileSync(abs);
  return crypto.createHash('sha256').update(content).digest('hex');
}

function collectFiles() {
  const files = [];

  for (const rel of WATCH_TARGETS) {
    const abs = path.join(ROOT, rel);
    if (fs.existsSync(abs)) files.push(rel);
  }

  for (const { dir, ext, recursive } of WATCH_DIRS) {
    const abs = path.join(ROOT, dir);
    if (!fs.existsSync(abs)) continue;
    const walk = (d) => {
      for (const entry of fs.readdirSync(d, { withFileTypes: true })) {
        const full = path.join(d, entry.name);
        if (entry.isDirectory() && recursive) { walk(full); continue; }
        if (entry.isFile() && entry.name.endsWith(ext)) {
          files.push(path.relative(ROOT, full));
        }
      }
    };
    walk(abs);
  }

  for (const rel of WATCH_MD) {
    const abs = path.join(ROOT, rel);
    if (fs.existsSync(abs)) files.push(rel);
  }

  return [...new Set(files)].sort();
}

/**
 * Generate integrity manifest — run after any intentional code change.
 * Writes src/agent/config/integrity-manifest.json.
 */
function generateManifest() {
  const files  = collectFiles();
  const hashes = {};
  for (const rel of files) {
    const h = hashFile(rel);
    if (h) hashes[rel] = h;
  }
  const manifest = {
    generatedAt: new Date().toISOString(),
    fileCount:   Object.keys(hashes).length,
    hashes,
  };
  fs.mkdirSync(path.dirname(MANIFEST_PATH), { recursive: true });
  fs.writeFileSync(MANIFEST_PATH, JSON.stringify(manifest, null, 2));
  console.log(`[integrity] Manifest generated — ${manifest.fileCount} files`);
  return manifest;
}

/**
 * Verify current files against stored manifest.
 * Returns { valid, failures: [{ file, expected, actual }] }
 */
function verifyManifest() {
  if (!fs.existsSync(MANIFEST_PATH)) {
    console.warn('[integrity] No manifest found — run npm run integrity:generate after deploy');
    return { valid: true, failures: [], skipped: true };
  }

  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
  const failures = [];

  for (const [rel, expected] of Object.entries(manifest.hashes)) {
    const actual = hashFile(rel);
    if (actual === null) {
      failures.push({ file: rel, expected, actual: 'MISSING' });
      console.error(`[SECURITY_ALERT] integrity: FILE MISSING — ${rel}`);
    } else if (actual !== expected) {
      failures.push({ file: rel, expected, actual });
      console.error(`[SECURITY_ALERT] integrity: HASH MISMATCH — ${rel}`);
      console.error(`  expected: ${expected}`);
      console.error(`  actual:   ${actual}`);
    }
  }

  const valid = failures.length === 0;
  if (valid) {
    console.log(`[integrity] All ${Object.keys(manifest.hashes).length} files verified OK`);
  } else {
    console.error(`[SECURITY_ALERT] integrity: ${failures.length} file(s) failed verification`);
  }
  return { valid, failures, generatedAt: manifest.generatedAt };
}

module.exports = { generateManifest, verifyManifest, MANIFEST_PATH };
