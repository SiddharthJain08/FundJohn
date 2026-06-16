'use strict';
// Clean-room note: we read cloned files as TEXT only — never import/exec them.
function parseLeanFile(text, filename) {
  const slug = filename.replace(/\.py$/, '').replace(/[^a-z0-9]+/gi, '_').toLowerCase().replace(/^_+|_+$/g, '');
  const strategy_id = `S_ast_${slug}`;
  // Header comment block = contiguous leading `#` lines (LEAN files put the rule + source there).
  // LEAN files lead with a `# region imports` / `from AlgorithmImports import *` / `# endregion`
  // preamble before the rule block, so we skip import/region lines instead of terminating on them.
  const lines = text.split('\n');
  const commentLines = [];
  for (const ln of lines) {
    const t = ln.trim();
    if (t.startsWith('from ') || t.startsWith('import ') || t.startsWith('# region') || t.startsWith('# endregion')) continue;
    if (t.startsWith('#') || t === '') commentLines.push(t.replace(/^#\s?/, ''));
    else break;
  }
  const rule_comment = commentLines.join('\n').trim();
  const urlMatch = rule_comment.match(/https?:\/\/[^\s)]+/);
  const cited_url = urlMatch ? urlMatch[0] : null;
  return { slug, strategy_id, rule_comment, cited_url, code: text };
}

const { runOneShot, parseJsonBlock } = require('./_opus_oneshot');

const HUNTER_FIELDS = ['strategy_id','hypothesis_one_liner','signal_logic','data_requirements','universe','inferred_universe_filter','inferred_instrument_class'];
function validateSpec(s) {
  if (!s || typeof s !== 'object') return { ok: false, why: 'not an object' };
  for (const f of ['strategy_id','hypothesis_one_liner','data_requirements','inferred_instrument_class']) {
    if (s[f] === undefined || s[f] === null && f !== 'inferred_universe_filter') return { ok: false, why: `missing ${f}` };
  }
  if (!['equity','etp','option','crypto','futures'].includes(s.inferred_instrument_class)) return { ok: false, why: 'bad class' };
  return { ok: true };
}

const EXTRACTOR_PROMPT = (parsed) => `You are extracting a trading-strategy spec from an already-coded reference (QuantConnect/LEAN). The RULE is stated in the comment block; the code shows exact params. Emit ONLY a fenced json block matching this shape (the same our PaperHunter emits):
{ "strategy_id": "${parsed.strategy_id}", "hypothesis_one_liner": "...", "signal_logic": "<explicit entry/exit + params>", "data_requirements": {"required":["prices"],"optional":[]}, "universe": "<e.g. SP500 | a fixed ETF list | NASDAQ>", "inferred_universe_filter": null, "inferred_instrument_class": "equity|etp|option|crypto" }
Rules: daily-bar US equity/ETF only; if it needs intraday/futures/options-chain we lack, still emit but note in signal_logic. inferred_instrument_class = 'etp' for ETF-basket rotations, 'equity' for single-name. Cite the source: ${parsed.cited_url || 'n/a'}.

--- RULE COMMENT ---
${parsed.rule_comment}
--- REFERENCE CODE (for params; do NOT copy verbatim downstream) ---
${parsed.code.slice(0, 6000)}`;

async function extractSpec(parsed, { runner = runOneShot } = {}) {
  const out = await runner({ prompt: EXTRACTOR_PROMPT(parsed), model: 'claude-sonnet-4-6',
    allowedTools: [], disallowedTools: ['Write','Edit','Bash','WebSearch','WebFetch'], timeoutMs: 240000 });
  if (out.error) throw new Error(`extractor failed: ${out.error}`);
  const spec = parseJsonBlock(out.text) || {};
  spec.strategy_id = spec.strategy_id || parsed.strategy_id;
  const v = validateSpec(spec);
  if (!v.ok) throw new Error(`invalid spec: ${v.why}`);
  return spec;
}

module.exports = { parseLeanFile, validateSpec, extractSpec, EXTRACTOR_PROMPT };
