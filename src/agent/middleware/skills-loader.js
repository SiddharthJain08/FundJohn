'use strict';

const fs   = require('fs');
const path = require('path');
const { vetSkill } = require('../../security/skill-vetter');

const SKILLS_DIR  = path.join(__dirname, '../../../src/skills');
const PLUGINS_DIR = path.join(__dirname, '../../../src/plugins');

let skillsManifest = null;
const _vetCache    = {};  // skillName → { approved, ts }
const VET_TTL_MS   = 3600_000; // re-vet after 1h (catches integrity changes mid-session)

function loadSkillsManifest() {
  if (skillsManifest) return skillsManifest;
  const skills = [];

  // Legacy skills: src/skills/{name}/skill.json
  if (fs.existsSync(SKILLS_DIR)) {
    for (const name of fs.readdirSync(SKILLS_DIR, { withFileTypes: true })
        .filter((d) => d.isDirectory()).map((d) => d.name)) {
      const jsonPath = path.join(SKILLS_DIR, name, 'skill.json');
      if (!fs.existsSync(jsonPath)) continue;
      const meta = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
      skills.push({ name, ...meta });
    }
  }

  // Plugin bundles: src/plugins/{plugin}/skills/{name}/skill.json
  if (fs.existsSync(PLUGINS_DIR)) {
    for (const pluginName of fs.readdirSync(PLUGINS_DIR, { withFileTypes: true })
        .filter((d) => d.isDirectory()).map((d) => d.name)) {
      const skillsDir = path.join(PLUGINS_DIR, pluginName, 'skills');
      if (!fs.existsSync(skillsDir)) continue;
      for (const name of fs.readdirSync(skillsDir, { withFileTypes: true })
          .filter((d) => d.isDirectory()).map((d) => d.name)) {
        const jsonPath = path.join(skillsDir, name, 'skill.json');
        if (!fs.existsSync(jsonPath)) continue;
        const meta = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
        skills.push({ name: `${pluginName}:${name}`, ...meta });
      }
    }
  }

  skillsManifest = skills;
  return skills;
}

function buildSkillsBlock(activeSkills = []) {
  const manifest = loadSkillsManifest();
  const lines = ['## Available Skills'];
  for (const skill of manifest) {
    const active = activeSkills.includes(skill.name) ? ' [ACTIVE]' : '';
    lines.push(`- **/${skill.name}**${active} — ${skill.description}`);
  }
  return lines.join('\n');
}

function loadSkillDefinition(skillName) {
  if (skillName.includes(':')) {
    // Plugin-namespaced: fundjohn:memo-schema → src/plugins/fundjohn/skills/memo-schema/SKILL.md
    const [pluginId, skillId] = skillName.split(':');
    const p = path.join(PLUGINS_DIR, pluginId, 'skills', skillId, 'SKILL.md');
    return fs.existsSync(p) ? fs.readFileSync(p, 'utf8') : null;
  }
  // Legacy: try definition.md first, then SKILL.md
  const defPath   = path.join(SKILLS_DIR, skillName, 'definition.md');
  const skillPath = path.join(SKILLS_DIR, skillName, 'SKILL.md');
  if (fs.existsSync(defPath))   return fs.readFileSync(defPath, 'utf8');
  if (fs.existsSync(skillPath)) return fs.readFileSync(skillPath, 'utf8');
  return null;
}

/**
 * Vet a skill (cached per session, re-checked after TTL).
 * Returns true if approved, false if blocked.
 */
async function isSkillApproved(skillName) {
  const cached = _vetCache[skillName];
  if (cached && (Date.now() - cached.ts) < VET_TTL_MS) return cached.approved;

  const skillPath = path.join(SKILLS_DIR, skillName);
  if (!fs.existsSync(skillPath)) return false;

  const result = await vetSkill(skillPath).catch(() => ({ approved: true, skillName })); // fail-open for DB errors
  _vetCache[skillName] = { approved: result.approved, ts: Date.now() };

  if (!result.approved) {
    // SSE alert (best-effort)
    try {
      const { broadcast } = require('../../channels/api/server');
      broadcast({ type: 'skill-blocked', skill: skillName, violations: result.violations });
    } catch { /* server may not be up */ }
  }
  return result.approved;
}

/**
 * Skills loader middleware — rebuilds skills manifest block every LLM call.
 * Vets any newly activated skill before injecting its definition.
 */
async function skillsLoader(state, next) {
  const { systemBlocks = [], activeSkills = [] } = state;

  // Block 2: Skills manifest (rebuilt every call, but cacheable in practice)
  const skillsBlock = {
    type: 'text',
    text: buildSkillsBlock(activeSkills),
    cache_control: { type: 'ephemeral' },
  };

  // Vet and inject definitions for active skills
  const extraBlocks = [];
  for (const skillName of activeSkills) {
    const approved = await isSkillApproved(skillName);
    if (!approved) {
      console.error(`[SECURITY_ALERT] skills-loader: ${skillName} blocked by vetter — not injecting`);
      extraBlocks.push({
        type: 'text',
        text: `## Skill /${skillName} — BLOCKED\nThis skill failed security vetting and cannot be loaded. Contact the operator.`,
      });
      continue;
    }
    const definition = loadSkillDefinition(skillName);
    if (definition) {
      extraBlocks.push({ type: 'text', text: `## Skill Definition: /${skillName}\n\n${definition}` });
    }
  }

  // Rebuild system blocks: [Block 1 static] [Block 2 skills] [skill definitions...] [Block 3 workspace] [Block 4 runtime]
  const staticBlocks   = systemBlocks.filter((b) => b._blockType === 'static');
  const workspaceBlock = systemBlocks.find((b) => b._blockType === 'workspace');
  const runtimeBlock   = systemBlocks.find((b) => b._blockType === 'runtime');

  const newSystemBlocks = [
    ...staticBlocks,
    { ...skillsBlock, _blockType: 'skills' },
    ...extraBlocks.map((b) => ({ ...b, _blockType: 'skill-def' })),
    ...(workspaceBlock ? [workspaceBlock] : []),
    ...(runtimeBlock   ? [runtimeBlock]   : []),
  ];

  return next({ ...state, systemBlocks: newSystemBlocks });
}

module.exports = skillsLoader;
module.exports.loadSkillsManifest  = loadSkillsManifest;
module.exports.buildSkillsBlock    = buildSkillsBlock;
module.exports.isSkillApproved     = isSkillApproved;
module.exports.loadSkillDefinition = loadSkillDefinition;
