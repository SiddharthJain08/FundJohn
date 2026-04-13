'use strict';

/**
 * Skill vetting gate — scans skill source for banned patterns before loading.
 * Results logged to skill_audit table. Vetted skills cached to avoid re-scanning.
 */

const fs   = require('fs');
const path = require('path');

const ROOT       = path.join(__dirname, '../../');
const ALLOWLIST  = path.join(ROOT, 'src/agent/config/skill-allowlist.json');

// Banned patterns with labels
const BANNED_PATTERNS = [
  { label: 'outbound_fetch',    re: /\bfetch\s*\(/ },
  { label: 'http_request',      re: /\bhttp\.request\s*\(|\bhttps\.request\s*\(/ },
  { label: 'axios',             re: /\baxios\b/ },
  { label: 'got',               re: /\bgot\s*\(/ },
  { label: 'request_lib',       re: /\brequest\s*\(/ },
  { label: 'net_connect',       re: /\bnet\.connect\s*\(/ },
  { label: 'dgram',             re: /\bdgram\b/ },
  { label: 'curl_wget_exec',    re: /child_process\.exec[^)]*(?:curl|wget)/ },
  { label: 'eval',              re: /\beval\s*\(/ },
  { label: 'new_function',      re: /new\s+Function\s*\(/ },
  { label: 'vm_run',            re: /vm\.runInNewContext\s*\(/ },
  { label: 'env_access',        re: /process\.env\b/ },
  { label: 'fs_read_arbitrary', re: /\bfs\.readFile(?:Sync)?\s*\(\s*(?!['"`](?:\.\/data\/|\.\/|\${skill))/ },
];

function loadAllowlist() {
  if (!fs.existsSync(ALLOWLIST)) return {};
  return JSON.parse(fs.readFileSync(ALLOWLIST, 'utf8'));
}

function isAllowed(skillName, label, snippet, allowlist) {
  const rules = allowlist[skillName] || [];
  return rules.some(r => r.label === label && snippet.includes(r.snippet));
}

function scanFile(filePath, skillName, allowlist) {
  const src = fs.readFileSync(filePath, 'utf8');
  const lines = src.split('\n');
  const violations = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    for (const { label, re } of BANNED_PATTERNS) {
      if (re.test(line) && !isAllowed(skillName, label, line, allowlist)) {
        violations.push({
          file: path.relative(ROOT, filePath),
          line: i + 1,
          pattern: label,
          snippet: line.trim().slice(0, 120),
        });
      }
    }
  }
  return violations;
}

/**
 * Vet a skill directory — scans all .js files for banned patterns.
 * @param {string} skillPath  Absolute path to skill directory
 * @returns {{ approved: boolean, skillName: string, violations: Array }}
 */
async function vetSkill(skillPath) {
  const skillName = path.basename(skillPath);
  const allowlist = loadAllowlist();
  const violations = [];

  const walk = (dir) => {
    if (!fs.existsSync(dir)) return;
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) { walk(full); continue; }
      if (entry.isFile() && entry.name.endsWith('.js')) {
        violations.push(...scanFile(full, skillName, allowlist));
      }
    }
  };
  walk(skillPath);

  const approved = violations.length === 0;

  // Log to DB (best-effort)
  try {
    const { query } = require('../database/postgres');
    await query(
      `INSERT INTO skill_audit (skill_name, vetted_at, approved, violations_json, vetted_by)
       VALUES ($1, NOW(), $2, $3, 'skill-vetter')
       ON CONFLICT (skill_name) DO UPDATE SET
         vetted_at=NOW(), approved=EXCLUDED.approved,
         violations_json=EXCLUDED.violations_json`,
      [skillName, approved, JSON.stringify(violations)]
    );
  } catch { /* DB unavailable — don't fail the vet */ }

  if (!approved) {
    console.error(`[SECURITY_ALERT] skill-vetter: ${skillName} BLOCKED — ${violations.length} violation(s)`);
    violations.forEach(v => console.error(`  ${v.file}:${v.line} [${v.pattern}] ${v.snippet}`));
  } else {
    console.log(`[skill-vetter] ${skillName} approved`);
  }

  return { approved, skillName, violations };
}

/**
 * Run vetting on all installed skills and return results.
 * Used for initial audit + grandfathering of existing skills.
 */
async function vetAllSkills(skillsDir) {
  const results = [];
  if (!fs.existsSync(skillsDir)) return results;
  for (const entry of fs.readdirSync(skillsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const result = await vetSkill(path.join(skillsDir, entry.name));
    results.push(result);
  }
  return results;
}

/**
 * Generate an allowlist entry for a known-legitimate pattern.
 * Writes to src/agent/config/skill-allowlist.json.
 */
function addAllowlistEntry(skillName, label, snippet) {
  const al = loadAllowlist();
  if (!al[skillName]) al[skillName] = [];
  // Avoid duplicates
  if (!al[skillName].some(r => r.label === label && r.snippet === snippet)) {
    al[skillName].push({ label, snippet, addedAt: new Date().toISOString() });
    fs.mkdirSync(path.dirname(ALLOWLIST), { recursive: true });
    fs.writeFileSync(ALLOWLIST, JSON.stringify(al, null, 2));
    console.log(`[skill-vetter] Allowlist updated: ${skillName} / ${label}`);
  }
}

module.exports = { vetSkill, vetAllSkills, addAllowlistEntry, ALLOWLIST };
