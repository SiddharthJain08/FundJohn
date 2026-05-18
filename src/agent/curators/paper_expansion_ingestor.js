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
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { runOneShot, parseJsonBlock } = require('./_opus_oneshot');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';
const WORKSPACE    = `${OPENCLAW_DIR}/workspaces/default`;

// Phase 2-followup #21 — paper-fingerprint dedup, JS site (5th of 5).
// Single source of truth: the existing Python helper at
// src/research/paper_fingerprint.py. We shell out via a thin CLI wrapper
// at scripts/compute_paper_fingerprints.py rather than re-implement the
// SHA-1/normalization in JS (re-impl risks divergence from the 4 Python
// sites).
const FP_SCRIPT = path.join(__dirname, '..', '..', '..', 'scripts', 'compute_paper_fingerprints.py');

// Batch-fingerprint a list of {title, authors, year} records.
// Returns same-length array of 16-hex-char fingerprints (or null when
// any of title/first-author/year is missing — partial papers don't dedup).
// Fail-soft: if the shell-out itself errors, returns all-nulls so the
// ingest still proceeds without dedup rather than blocking.
function computeFingerprintsBatch(papers) {
  if (!papers || papers.length === 0) return [];
  try {
    const out = execFileSync('python3', [FP_SCRIPT], {
      input: JSON.stringify(papers),
      encoding: 'utf-8',
      maxBuffer: 16 * 1024 * 1024,  // 16MB headroom — way more than a weekly batch
    });
    return JSON.parse(out);
  } catch (err) {
    console.error('[paper-fingerprint] CLI shell-out failed; skipping fingerprint dedup:', err.message);
    return papers.map(() => null);
  }
}

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
    _query(`SELECT substring(markdown_body from 1 for 280) AS preview,
                   strategy_id, memo_date
              FROM strategy_memos
             WHERE memo_date >= CURRENT_DATE - 21
             ORDER BY memo_date DESC LIMIT 8`),
    _query(`SELECT venue, source, COUNT(*)::int AS n
              FROM research_corpus
             WHERE ingested_at >= NOW() - INTERVAL '60 days'
             GROUP BY venue, source ORDER BY n DESC LIMIT 30`),
    _query(`SELECT queries_used, sources_discovered, papers_imported,
                    papers_imported_post_phase2, run_date
              FROM paper_source_expansions
             WHERE status='completed'
             ORDER BY run_date DESC LIMIT 8`),
    _query(`SELECT COUNT(*)::int AS corpus_size,
                   MAX(ingested_at) AS last_ingest
              FROM research_corpus`),
  ]).then(r => [r[0].rows, r[1].rows, r[2].rows, r[3].rows, r[4].rows[0]]);

  // Fold in the Obsidian vault so the LLM steers from what we've already
  // catalogued. strategy-notes/ is the per-strategy memo set the agents
  // append to; surfacing the most-recently-touched ones gives Opus a real
  // picture of which signal families are active without re-reading every
  // file. We just hand it the filenames + first-paragraph blurbs — Opus
  // can Read/Glob the vault from its cwd if it wants more.
  const vaultStrategyNotes = _vaultIndex(`${WORKSPACE}/strategy-notes`, 12, 600);
  // results/strategies/ holds session reports (review-XXX, session-YYY).
  // These are short lessons-learned dumps; one-paragraph previews are
  // enough to surface themes without exploding token cost.
  const vaultResultNotes   = _vaultIndex(`${WORKSPACE}/results/strategies`, 8, 400);

  // Compute the EXCLUDE list — every domain Opus has already discovered in
  // the last 8 weekly runs. The prompt was previously rediscovering AQR /
  // FED / SSRN every week and dedup ate it silently downstream. Now we
  // tell Opus explicitly: don't propose these again.
  const seenDomains = new Set();
  for (const row of recentCorpusVenues || []) {
    for (const src of (row.sources_discovered || [])) {
      const d = (src?.domain || '').trim().toLowerCase();
      if (d) seenDomains.add(d);
    }
  }
  const excludeDomains = Array.from(seenDomains).sort();

  return {
    portfolio_strategies: strategies,
    recent_memo_themes:   recentMemoThemes,
    recent_corpus_venues: portfolio,   // note: positional arg reorder
    previous_expansion_runs: recentCorpusVenues,
    exclude_domains:      excludeDomains,
    corpus_freshness:     lastExpansion,
    vault_strategy_notes: vaultStrategyNotes,
    vault_result_notes:   vaultResultNotes,
  };
}

// Read up to `max` most-recently-modified .md files in `dir`; return
// {filename, mtime, preview} for each. preview = first ~previewLen chars
// after the YAML frontmatter, with whitespace collapsed. Best-effort —
// returns [] on any error so a missing/empty vault never blocks the run.
function _vaultIndex(dir, max, previewLen) {
  try {
    if (!fs.existsSync(dir)) return [];
    const files = fs.readdirSync(dir)
      .filter(f => f.endsWith('.md'))
      .map(f => {
        const p = path.join(dir, f);
        try {
          const st = fs.statSync(p);
          return { filename: f, mtime: st.mtimeMs };
        } catch (_) { return null; }
      })
      .filter(Boolean)
      .sort((a, b) => b.mtime - a.mtime)
      .slice(0, max);
    return files.map(({ filename, mtime }) => {
      let preview = '';
      try {
        const raw = fs.readFileSync(path.join(dir, filename), 'utf8');
        const body = raw.replace(/^---[\s\S]*?---\s*/, '').replace(/\s+/g, ' ').trim();
        preview = body.slice(0, previewLen);
      } catch (_) {}
      return {
        filename,
        last_touched: new Date(mtime).toISOString(),
        preview,
      };
    });
  } catch (_) { return []; }
}

const EXPANSION_PROMPT_PREAMBLE = `\
You are MasterMindJohn (Opus 4.7, 1M ctx) running Phase 1 of the Saturday
brain. Your job: discover NEW SOURCE FEEDS of quantitative finance research
BEYOND the standard arXiv + OpenAlex ingestion pipelines. You have
WebSearch + WebFetch + Bash access in this workspace.

**Reframed for Saturday brain (2026-04-25):** you are no longer scraping
individual papers. Phase 2 (\`expanded_sources.py\`) does the bulk ingestion
once you point it at structured feeds. Your output is the *source registry*
that drives Phase 2. Quality over quantity — 6–15 well-characterised feeds
beat 50 one-off papers.

**Target feed kinds:**
  * RSS / Atom feeds (working-paper series, conference proceedings, blogs)
  * JSON sitemaps (OpenAlex-style cursor-paginated APIs)
  * HTML index pages with stable anchor structure (author pages, journal
    "latest articles" pages)

**Target sources beyond arXiv/OpenAlex:**
  * Federal Reserve / ECB / BIS / IMF / central-bank working papers
  * CFA Institute, Journal of Portfolio Management, Risk.net white papers
  * Prop-shop research: AQR insights, Two Sigma articles, D.E. Shaw white papers
  * Corporate research: Goldman / Morgan Stanley / JPM quant strategy
  * Conference proceedings: QuantCon, WBS, Global Derivatives
  * University research pages (Chicago Booth, NYU Stern, MIT Sloan…)
  * Trading blogs with citations: Alpha Architect, Flirting with Models
  * SSRN author pages (individual quant researchers)

**Your process (MUST FOLLOW):**

1. **Read the context** below (portfolio, strategy memo themes, recent
   corpus venues, previous expansion runs, **the Obsidian vault index of
   recent strategy + result notes**). Identify 2–4 THEMES where the
   research_corpus is thin relative to the portfolio's needs.

   The vault is your authoritative source for "what the portfolio is
   actually doing" — the previews under VAULT STRATEGY NOTES + VAULT
   RESULT NOTES are real notes the agents wrote during prior cycles. If
   a preview catches your eye, **Read** the full file (cwd is the vault
   root, so e.g. Read strategy-notes/S_TR06_baltussen_eod_reversal.md)
   before committing to a theme.

2. **Formulate 4–8 search queries** targeting your themes. Use WebSearch
   to find candidate FEED URLs (the index page that lists papers — not
   individual papers). Visit promising ones with WebFetch and inspect:
     * does the page have an RSS/Atom feed link? (Look for
       \`<link rel="alternate" type="application/rss+xml">\` or similar.)
     * if no feed, is the page structured enough that anchor-link
       extraction would work? (PDFs or paper-shaped HTML pages with
       titles in anchor text)
     * does it cover papers we don't already have in
       \`recent_corpus_venues\`?

3. **For each viable feed, characterise it:**
     - feed_url       (the URL Phase 2 will fetch)
     - kind           ('rss' | 'atom' | 'html' — your best guess; Phase 2
                       sniffs the response anyway)
     - name           (human label, e.g. "AQR Insights", "Fed FEDS papers")
     - domain         (registrable domain, for dedup)
     - strategy_types (what kinds of strategies typically appear here:
                       e.g. ["momentum","factor","options"])
     - notes          (≤140 chars: anything Phase 2 / curator should
                       know — paywalled? volume? backfill window?)

4. **Emit a final JSON block** (fenced \`\`\`json ... \`\`\`):
   \`\`\`json
   {
     "queries_used":       ["q1", "q2", ...],
     "sources_discovered": [
       {
         "domain":         "federalreserve.gov",
         "name":           "Fed FEDS working papers",
         "feed_url":       "https://www.federalreserve.gov/econres/feds/feds.htm",
         "kind":           "html",
         "strategy_types": ["macro","factor","monetary policy"],
         "notes":          "Quarterly drop, ~30 papers/year. Title in anchor text."
       }
     ]
   }
   \`\`\`

   Target: 6–15 new feeds across diverse domains. Don't pad with feeds
   we already ingest (cross-check \`recent_corpus_venues\` and
   \`previous_expansion_runs.sources_discovered\`).

**Do NOT WebFetch every paper from each feed yourself.** Phase 2 will
do bulk ingestion using the feed_urls you provide. Your role is feed
*discovery*, not paper extraction.

Context follows below.`;

function buildPrompt(ctx) {
  const excludeDomains = ctx.exclude_domains || [];
  const excludeBlock = excludeDomains.length
    ? `\n--- EXCLUDE_DOMAINS (already in our corpus from prior expansion runs — DO NOT re-propose) ---\n${excludeDomains.join('\n')}\n`
    : '';
  return `${EXPANSION_PROMPT_PREAMBLE}

--- PORTFOLIO STRATEGIES ---
${JSON.stringify(ctx.portfolio_strategies || [], null, 2)}

--- RECENT MEMO THEMES (last 3 weeks) ---
${JSON.stringify(ctx.recent_memo_themes || [], null, 2)}

--- RECENT CORPUS VENUES (last 60d) ---
${JSON.stringify(ctx.recent_corpus_venues || [], null, 2)}

--- PREVIOUS EXPANSION RUNS (last 8) ---
${JSON.stringify(ctx.previous_expansion_runs || [], null, 2)}
${excludeBlock}
--- CORPUS FRESHNESS ---
${JSON.stringify(ctx.corpus_freshness || {}, null, 2)}

--- VAULT STRATEGY NOTES (most-recent in workspaces/default/strategy-notes/) ---
${JSON.stringify(ctx.vault_strategy_notes || [], null, 2)}

--- VAULT RESULT NOTES (most-recent in workspaces/default/results/strategies/) ---
${JSON.stringify(ctx.vault_result_notes || [], null, 2)}

**HARD CONSTRAINT — do not propose any feed whose registrable domain
appears in EXCLUDE_DOMAINS above.** Recurring weekly rediscovery of
AQR / Fed / SSRN was burning $4/run with zero net new sources. If
your strongest themes lead you back to excluded domains, *change theme*
and surface 2-4 less-explored adjacent areas instead.

Begin. Think step-by-step in your natural reply, run your searches and
fetches, and end with the fenced JSON block.`;
}

async function _insertPapers(papers, notify) {
  let imported = 0;
  let skipped = 0;

  // Phase 2-followup #21 — compute fingerprints for the whole batch in
  // ONE subprocess (per-paper exec would be ~25ms × N; one exec is bounded).
  // Shape matches the CLI: {title, authors, year}.  published_date here is
  // an ISO 'YYYY-MM-DD' string per the input contract — same convention as
  // the 4 Python sites (commit b7c17ca), so int(str[:4]) extracts year.
  const fpInput = papers.map(p => ({
    title: (p && p.title) ? String(p.title) : '',
    authors: (p && Array.isArray(p.authors)) ? p.authors :
             (p && p.authors) ? [String(p.authors)] : [],
    year: (p && p.published_date && /^\d{4}/.test(String(p.published_date)))
            ? Number(String(p.published_date).slice(0, 4)) : null,
  }));
  const fps = computeFingerprintsBatch(fpInput);

  for (let i = 0; i < papers.length; i++) {
    const p = papers[i];
    if (!p || !p.source_url || !p.title) { skipped++; continue; }
    const fp = fps[i] || null;

    let dedup_dropped = false;
    let dedup_group_id = null;

    // Cross-source dedup check — same paper at a different URL fires here
    // (ON CONFLICT (source_url) only catches identical URLs).  Per-fingerprint
    // group_id model: all rows sharing a fingerprint share the group_id;
    // first-seen has dedup_dropped=FALSE, subsequent ones have TRUE but are
    // still preserved (append-only invariant).  Small race under concurrent
    // writers — acceptable for the Sunday batch ingest pattern (single writer).
    if (fp) {
      try {
        const existing = await _query(
          `SELECT paper_id, dedup_group_id FROM research_corpus
            WHERE paper_fingerprint = $1
            ORDER BY ingested_at ASC LIMIT 1`,
          [fp],
        );
        if (existing.rows.length > 0) {
          let existingGroup = existing.rows[0].dedup_group_id;
          if (existingGroup === null) {
            existingGroup = crypto.randomUUID();
            await _query(
              `UPDATE research_corpus SET dedup_group_id = $1 WHERE paper_id = $2`,
              [existingGroup, existing.rows[0].paper_id],
            );
          }
          dedup_group_id = existingGroup;
          dedup_dropped = true;
        }
      } catch (e) {
        // Lookup failure should not block the ingest — log and proceed
        // without dedup metadata for this row.
        notify(`fingerprint lookup failed for ${p.source_url}: ${e.message}`);
      }
    }

    try {
      const { rowCount } = await _query(
        `INSERT INTO research_corpus
           (source_url, title, abstract, authors, venue, published_date, source, raw_metadata,
            paper_fingerprint, dedup_group_id, dedup_dropped)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11)
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
          fp,
          dedup_group_id,
          dedup_dropped,
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

  // Audit: how many of the "newly discovered" sources are actually domains
  // we've seen in prior expansion runs? Pre-EXCLUDE_DOMAINS this number was
  // frequently 100% (Opus rediscovering AQR/Fed/SSRN). Post-fix we expect
  // this to trend toward 0; a non-zero number despite the EXCLUDE_DOMAINS
  // directive means Opus is ignoring the constraint and operator should
  // tighten the prompt.
  const excludeSet = new Set((ctx.exclude_domains || []).map(d => d.toLowerCase()));
  const sourcesAlreadySeen = sources.filter(s => {
    const d = (s?.domain || '').trim().toLowerCase();
    return d && excludeSet.has(d);
  }).length;

  notify(`Opus returned: ${papers.length} papers, ${queries.length} queries, ${sources.length} sources (${sourcesAlreadySeen} already-seen)`);

  let imported = 0, skipped = 0;
  if (!dryRun && papers.length) {
    const ins = await _insertPapers(papers, notify);
    imported = ins.imported; skipped = ins.skipped;
  }

  // Operator-facing summary written to paper_source_expansions.notes.
  // Composes the right metric (most ingest happens via Phase 2, not here)
  // so the audit doesn't read "$4 → 0 imports" anymore.
  const newSources = sources.length - sourcesAlreadySeen;
  const noteParts = [
    `${sources.length} sources discovered (${newSources} new, ${sourcesAlreadySeen} already-seen)`,
    `${papers.length} inline papers (Phase 1 — main ingest happens via expanded_sources.py Phase 2)`,
  ];
  if (newSources === 0 && sources.length > 0) {
    noteParts.push('WARNING: all sources were already-seen — operator should tighten EXCLUDE_DOMAINS directive or review Opus prompt steering');
  }
  if (sources.length === 0) {
    noteParts.push('WARNING: zero sources discovered — Opus may be stuck on themes or the EXCLUDE_DOMAINS list is exhausting the natural pool');
  }
  const notesLine = noteParts.join(' | ');

  const duration = Math.round((Date.now() - tStart) / 1000);
  if (!dryRun) {
    await _query(
      `UPDATE paper_source_expansions
          SET status='completed', completed_at=NOW(),
              queries_used=$1, sources_discovered=$2::jsonb,
              papers_imported=$3, papers_skipped_dup=$4,
              duration_seconds=$5, cost_usd=$6,
              sources_already_seen=$7, notes=$8
        WHERE id=$9`,
      [queries, JSON.stringify(sources), imported, skipped,
       duration, out.costUsd, sourcesAlreadySeen, notesLine, expansionId]
    );
  }
  notify(`done — imported=${imported} skipped=${skipped} cost=$${out.costUsd.toFixed(2)} ${duration}s`);
  return {
    expansion_id: expansionId,
    imported, skipped,
    sources_already_seen: sourcesAlreadySeen,
    new_sources: newSources,
    queries_used: queries,
    sources_discovered: sources,
    duration_seconds: duration,
    costUsd: out.costUsd,
  };
}

module.exports = { run, computeFingerprintsBatch };
