'use strict';

/**
 * paper_expansion_ingestor.js — Sunday 08:00 ET paper hunt.
 *
 * Opus 4.7 is given WebSearch + WebFetch + Bash access and asked to:
 *   1. Inspect portfolio gaps + recent strategy memo themes + corpus gaps.
 *   2. Formulate N diverse search queries that target sources BEYOND the
 *      standard arXiv + OpenAlex pipelines (journal sites, working-paper
 *      series, conference proceedings, research blogs with citations,
 *      SSRN author pages, central-bank research, prop-shop white papers).
 *   3. Run those searches, fetch landing pages, and extract candidate
 *      paper metadata (title, abstract, authors, source_url, venue,
 *      published_date).
 *   4. Dedupe against `research_corpus.source_url` UNIQUE, then INSERT.
 *   5. Log the run to `paper_source_expansions` with the list of queries
 *      used, the list of sources discovered, and the counts.
 *
 * By design, this does NOT rate the imported papers — that's the Saturday
 * corpus-rater's job. It just populates `research_corpus` for the next
 * Saturday rating pass.
 *
 * Cost budget: ~$3–8 per weekly run (Opus with a few WebSearch calls).
 */

const fs = require('fs');
const path = require('path');
const { runOneShot, parseJsonBlock } = require('./_opus_oneshot');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';
const WORKSPACE    = `${OPENCLAW_DIR}/workspaces/default`;

async function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  return _query._pool.query(sql, params);
}

async function _gatherContext() {
  const [portfolio, strategies, recentMemoThemes, recentCorpusVenues, lastExpansion] = await Promise.all([
    _query(`SELECT id, name, status, tier, universe, signal_frequency,
                   backtest_sharpe, backtest_return_pct
              FROM strategy_registry
             WHERE status IN ('live','monitoring','approved')
             ORDER BY backtest_sharpe DESC NULLS LAST LIMIT 40`),
    _query(`SELECT DISTINCT substring(markdown_body from 1 for 280) AS preview, strategy_id
              FROM strategy_memos
             WHERE memo_date >= CURRENT_DATE - 21
             ORDER BY memo_date DESC LIMIT 8`),
    _query(`SELECT venue, source, COUNT(*)::int AS n
              FROM research_corpus
             WHERE ingested_at >= NOW() - INTERVAL '60 days'
             GROUP BY venue, source ORDER BY n DESC LIMIT 30`),
    _query(`SELECT queries_used, sources_discovered, papers_imported, run_date
              FROM paper_source_expansions
             ORDER BY run_date DESC LIMIT 3`),
    _query(`SELECT COUNT(*)::int AS corpus_size,
                   MAX(ingested_at) AS last_ingest
              FROM research_corpus`),
  ]).then(r => [r[0].rows, r[1].rows, r[2].rows, r[3].rows, r[4].rows[0]]);
  return {
    portfolio_strategies: strategies,
    recent_memo_themes:   recentMemoThemes,
    recent_corpus_venues: portfolio,   // note: positional arg reorder
    corpus_freshness:     lastExpansion,
  };
}

const EXPANSION_PROMPT_PREAMBLE = `\
You are MasterMindJohn (Opus 4.7, 1M ctx) running the Sunday paper-expansion
pass. Your job: discover NEW sources of quantitative finance research
BEYOND the standard arXiv + OpenAlex ingestion pipelines. You have
WebSearch + WebFetch + Bash access in this workspace.

**Target sources beyond arXiv/OpenAlex:**
  * Federal Reserve / ECB / BIS / IMF / central-bank working papers
  * CFA Institute, Journal of Portfolio Management, Risk.net white papers
  * Prop-shop research: AQR, Two Sigma, Renaissance, D.E. Shaw (public)
  * Corporate research: Goldman / Morgan Stanley / JPM quant strategy
  * Conference proceedings: QuantCon, WBS, Global Derivatives
  * University research pages (Chicago Booth, NYU Stern, MIT Sloan…)
  * Trading blogs with citations: Alpha Architect, Flirting with Models
  * SSRN author pages (individual quant researchers)

**Your process (MUST FOLLOW):**

1. **Read the context** below (portfolio, strategy memo themes, last 60
   days of corpus venues, previous expansion runs). Identify 2–4 THEMES
   where the research_corpus is thin relative to the portfolio's needs.

2. **Formulate 4–8 diverse search queries** that target the themes via
   the sources above. Prefer queries that surface NEW venues not in
   recent_corpus_venues. Avoid duplicating prior queries (provided).

3. **Run WebSearch on each query**, then WebFetch the 1–3 most promising
   results per query. Extract paper metadata. If a result links to a PDF
   or preprint, record the landing page as source_url.

4. **For each candidate paper, collect:**
     { source_url, title, abstract (≤ 2000 chars), authors (array),
       venue, published_date, source (e.g. 'ssrn','nber','journal','blog') }
   Skip anything that's just a news article or review without a
   hypothesis / strategy / empirical result.

5. **Emit a final JSON block** (fenced \`\`\`json ... \`\`\`) with:
   {
     "queries_used":       ["q1", "q2", ...],
     "sources_discovered": [
       { "domain": "...", "name": "...", "papers_found": N, "notes": "..." }
     ],
     "papers":             [ { source_url, title, abstract, authors[],
                               venue, published_date, source }, ... ]
   }

   Target: 10–40 new candidate papers. Do NOT flood — quality > quantity.

**Do NOT insert into Postgres yourself** — the caller will handle INSERT
with dedup by source_url.

Context follows below.`;

function buildPrompt(ctx) {
  return `${EXPANSION_PROMPT_PREAMBLE}

--- PORTFOLIO STRATEGIES ---
${JSON.stringify(ctx.portfolio_strategies || [], null, 2)}

--- RECENT MEMO THEMES (last 3 weeks) ---
${JSON.stringify(ctx.recent_memo_themes || [], null, 2)}

--- RECENT CORPUS VENUES (last 60d) ---
${JSON.stringify(ctx.recent_corpus_venues || [], null, 2)}

--- CORPUS FRESHNESS ---
${JSON.stringify(ctx.corpus_freshness || {}, null, 2)}

Begin. Think step-by-step in your natural reply, run your searches and
fetches, and end with the fenced JSON block.`;
}

async function _insertPapers(papers, notify) {
  let imported = 0;
  let skipped = 0;
  for (const p of papers) {
    if (!p || !p.source_url || !p.title) { skipped++; continue; }
    try {
      const { rowCount } = await _query(
        `INSERT INTO research_corpus
           (source_url, title, abstract, authors, venue, published_date, source, raw_metadata)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
         ON CONFLICT (source_url) DO NOTHING`,
        [
          p.source_url,
          String(p.title).slice(0, 800),
          p.abstract ? String(p.abstract).slice(0, 20_000) : null,
          Array.isArray(p.authors) ? p.authors.slice(0, 20) : (p.authors ? [String(p.authors)] : null),
          p.venue || null,
          p.published_date && /^\d{4}-\d{2}-\d{2}/.test(String(p.published_date)) ? p.published_date : null,
          p.source || 'expansion',
          JSON.stringify({ ingested_via: 'paper_expansion', original: p }),
        ],
      );
      if (rowCount > 0) imported++; else skipped++;
    } catch (e) {
      notify(`insert failed for ${p.source_url}: ${e.message}`);
      skipped++;
    }
  }
  return { imported, skipped };
}

async function run({ dryRun = false, notify = () => {} } = {}) {
  const tStart = Date.now();
  const { rows: expRow } = await _query(
    `INSERT INTO paper_source_expansions (status) VALUES ('running') RETURNING id`
  );
  const expansionId = expRow[0].id;
  notify(`expansion ${expansionId.slice(0, 8)} started`);

  const ctx = await _gatherContext();
  notify(`ctx built — ${ctx.portfolio_strategies.length} strategies, ${ctx.recent_corpus_venues.length} recent venues`);

  const prompt = buildPrompt(ctx);
  notify(`prompting Opus with WebSearch/WebFetch access`);
  const out = await runOneShot({
    prompt,
    cwd: WORKSPACE,
    allowedTools: ['WebSearch','WebFetch','Bash','Read','Grep','Glob'],
    disallowedTools: ['Write','Edit','NotebookEdit','Task'],
    timeoutMs: 1_500_000,  // 25 min cap
  });

  if (out.error) {
    await _query(
      `UPDATE paper_source_expansions
          SET status='failed', error_detail=$1, completed_at=NOW(),
              duration_seconds=$2, cost_usd=$3
        WHERE id=$4`,
      [out.error, Math.round((Date.now() - tStart) / 1000), out.costUsd, expansionId]
    );
    return { expansion_id: expansionId, error: out.error, costUsd: out.costUsd };
  }

  const parsed = parseJsonBlock(out.text);
  const papers = Array.isArray(parsed?.papers) ? parsed.papers : [];
  const queries = Array.isArray(parsed?.queries_used) ? parsed.queries_used : [];
  const sources = Array.isArray(parsed?.sources_discovered) ? parsed.sources_discovered : [];
  notify(`Opus returned: ${papers.length} papers, ${queries.length} queries, ${sources.length} sources`);

  let imported = 0, skipped = 0;
  if (!dryRun && papers.length) {
    const ins = await _insertPapers(papers, notify);
    imported = ins.imported; skipped = ins.skipped;
  }

  const duration = Math.round((Date.now() - tStart) / 1000);
  if (!dryRun) {
    await _query(
      `UPDATE paper_source_expansions
          SET status='completed', completed_at=NOW(),
              queries_used=$1, sources_discovered=$2::jsonb,
              papers_imported=$3, papers_skipped_dup=$4,
              duration_seconds=$5, cost_usd=$6
        WHERE id=$7`,
      [queries, JSON.stringify(sources), imported, skipped,
       duration, out.costUsd, expansionId]
    );
  }
  notify(`done — imported=${imported} skipped=${skipped} cost=$${out.costUsd.toFixed(2)} ${duration}s`);
  return {
    expansion_id: expansionId,
    imported, skipped,
    queries_used: queries,
    sources_discovered: sources,
    duration_seconds: duration,
    costUsd: out.costUsd,
  };
}

module.exports = { run };
