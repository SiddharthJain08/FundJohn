'use strict';

const fs = require('fs');
const path = require('path');
const { getModelForSubagent } = require('../config/models');
const { loadSkillDefinition } = require('../middleware/skills-loader');

const TYPES_CONFIG = path.join(__dirname, '../config/subagent-types.json');
const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';

let typesCache = null;

function loadTypes() {
  if (typesCache) return typesCache;
  typesCache = JSON.parse(fs.readFileSync(TYPES_CONFIG, 'utf8')).types;
  return typesCache;
}

function getType(name) {
  const types = loadTypes();
  const def = types[name];
  if (!def) return null;
  return {
    ...def,
    model: getModelForSubagent(name).model,
  };
}

function getPromptFile(name) {
  const def = getType(name);
  if (!def) return null;
  return path.join(OPENCLAW_DIR, def.promptFile);
}

/**
 * Build the full prompt for a subagent by assembling:
 * 1. Subagent prompt template (with TICKER substituted)
 * 2. Selected component prompts
 * 3. Custom prompt addition (if any)
 * 4. API key injections
 */
function buildPrompt(type, ticker, workspace, additionalPrompt = '', templateVars = {}) {
  const def = getType(type);
  if (!def) throw new Error(`Unknown subagent type: ${type}`);

  const promptFile = path.join(OPENCLAW_DIR, def.promptFile);
  let basePrompt = '';

  if (fs.existsSync(promptFile)) {
    basePrompt = fs.readFileSync(promptFile, 'utf8');
  } else {
    console.warn(`[types] Prompt file not found: ${promptFile}`);
    basePrompt = `You are a ${type} subagent for the OpenClaw system.`;
  }

  // Load and inject component prompts
  const componentDir = path.join(OPENCLAW_DIR, 'src/agent/prompts/components');
  let components = '';
  for (const comp of (def.components || [])) {
    const compFile = path.join(componentDir, `${comp}.md`);
    if (fs.existsSync(compFile)) {
      components += `\n\n---\n\n${fs.readFileSync(compFile, 'utf8')}`;
    }
  }

  // Substitute template variables. AlphaVantage was removed 2026-04-28 —
  // its capabilities (technical indicators, macro, economic calendar) are
  // covered by Polygon + FMP. Subagent prompts no longer reference the
  // AV_KEY substitution.
  const FMP_KEY = process.env.FMP_API_KEY || '';
  const POLYGON_KEY = process.env.POLYGON_API_KEY || '';
  const TAVILY_KEY = process.env.TAVILY_API_KEY || '';

  // Inject plugin skill definitions
  let skillsBlock = '';
  for (const skillId of (def.skills || [])) {
    const definition = loadSkillDefinition(skillId);
    if (definition) {
      skillsBlock += `\n\n---\n\n## Skill: ${skillId}\n\n${definition}`;
    }
  }

  let prompt = `${basePrompt}${components}${skillsBlock}`;
  if (additionalPrompt) prompt += `\n\n---\n\nAdditional context:\n${additionalPrompt}`;

  prompt = prompt
    .replace(/\{\{TICKER\}\}/g, ticker || '')
    .replace(/\{\{FMP_KEY\}\}/g, FMP_KEY)
    .replace(/\{\{POLYGON_KEY\}\}/g, POLYGON_KEY)
    .replace(/\{\{TAVILY_KEY\}\}/g, TAVILY_KEY)
    .replace(/\$\{FMP_API_KEY\}/g, FMP_KEY)
    .replace(/TICKER/g, ticker || '');  // bare TICKER references

  // Substitute caller-supplied template vars (e.g. SEARCH_THEME from context JSON)
  for (const [key, val] of Object.entries(templateVars)) {
    prompt = prompt.replace(new RegExp(`\\{\\{${key}\\}\\}`, 'g'), String(val || ''));
  }

  if (workspace) {
    prompt = prompt.replace(/\{WORKSPACE\}/g, workspace).replace(/WORKSPACE/g, workspace);
  }

  // Inject runtime preamble
  const runtimePreamble = [
    `# Runtime Context`,
    `TICKER: ${ticker}`,
    `WORKSPACE: ${workspace || 'N/A'}`,
    `DATE: ${new Date().toISOString().slice(0, 10)}`,
    `TASK_DIR: ${workspace ? path.join(workspace, 'work', `${ticker}-diligence`) : 'N/A'}`,
    ``,
  ].join('\n');

  return runtimePreamble + prompt;
}

module.exports = { getType, getPromptFile, buildPrompt, loadTypes };
