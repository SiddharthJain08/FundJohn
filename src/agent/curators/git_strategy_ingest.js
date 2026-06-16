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
module.exports = { parseLeanFile };
