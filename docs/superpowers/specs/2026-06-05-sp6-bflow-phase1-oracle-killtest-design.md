# SP-6 B-flow Phase 1 — Oracle Kill-Test (Design)

**Date:** 2026-06-05 · **Status:** Operator-blessed path ("go ahead"); spec grounded against live schema/data same day
**Parent:** B-flow re-scope (supersedes B1-gate + B2-curve plan; see `2026-06-03-sp6-phase-b2-hawkes-execution-scheduler-design.md` — superseded design of record)
**Branch:** worktree `feat/sp6-bflow-phase1-oracle` off live `feat/sp6-phase-a-eod-open-execution` tip

## 1. Purpose

Quantify the **prize** available to an intraday entry-timing engine before building one. For every actually-emitted historical order intent, compute the hindsight-optimal entry within the worked session and compare it to the EOD-dump benchmark. Two headline outputs:

- **Available-timing-alpha distribution** (gross + net of differential cost) — if even the hindsight upper bound doesn't clear costs with room for OOS shrinkage, we STOP and keep the 3:55 dump.
- **oracle≈EOD frequency** — the measured answer to "how often was waiting (EOD) the right policy."

Doctrine (locked at path-bless): **Δ-vs-EOD out-of-sample is the only go/no-go for any future engine; distance-to-oracle is a diagnostic, never an acceptance bar.** Phase 1 measures the oracle bound itself.

## 2. Design invariant

**The order set is fixed; only fill timing varies.** We replay actually-emitted order intents — no re-deriving which orders would exist, no re-sizing.

## 3. Order set (grounded 2026-06-05)

**Primary (signal grain):** `signal_pnl sp JOIN execution_signals es ON es.id = sp.signal_id` — `execution_signals` has NO `signal_id` column; its PK `id` is the FK target (b1_order_source.py docstring, re-verified). Filters: `sp.status='closed' AND es.signal_date IS NOT NULL AND sp.close_reason IS DISTINCT FROM 'rolled_continuation'` (filter is currently a **no-op** — close_reason ∈ {NULL, target_1×3031, stop_loss×2766}; kept as future-proofing for D1 rolls). Census: **5,797 rows / 3,296 distinct (signal_date,ticker)**, span 2026-04-10..2026-06-04, direction 100% populated (LONG 3151 / SHORT 2646).
- **Eval unit = (worked_session, ticker, direction)** — distinct triples (both directions on one (date,ticker) are separate intents; live sizer nets them, but each is its own timing problem). Carry `n_signals` and `Σ position_size_pct` per triple for weighting sensitivity.
- **Worked session = first trading session strictly after signal_date** (the signals[t]→fill[t+1] scheme), resolved against the SPY date index of `data/master/prices.parquet` (2,553 clean sessions; raw `date.unique()` is polluted by 1,059 weekend crypto dates — never use it). Mirrors `_resolve_session` semantics (b1_run.py:50-57).
- Exclude worked sessions ≥ today (no settled close yet).

**Secondary ($-weighted realism slice, broker grain):** `alpaca_submissions` (2,003 rows from 04-21; real `qty`, `filled_avg_price`; `run_date` = the worked session itself, no t+1 shift — matches `live_shadow_orders` semantics). **No FK to signals** (only run_date/ticker/strategy_id) — reported as a separate table, never joined. Equity filter: `instrument_class IS NULL OR = 'equity'` (NULL=equity by SP-5.1a design). Two benchmarks here: EOD (policy) and `filled_avg_price` (what we actually paid — realized diagnostic only; pre-06-01 fills happened at varying times of day).

## 4. Intraday data (grounded empirically)

- **Source:** Alpaca `GET https://data.alpaca.markets/v2/stocks/bars`, `timeframe=1Min`, **`feed=sip`** (confirmed NOT subscription-blocked on our key, 2026-06-05: HTTP 200, fields o/h/l/c/v/n/**vw**, history ≥2023-06-02, rate limit 10,000 req/min, multi-symbol batching works, RFC3339-UTC bar-start timestamps). IEX = fallback only (thinner tape).
- **Fetch envelope:** union of (worked_session, ticker) pairs from both grains (~3.3-4k). Batched multi-symbol per session, `limit=10000`, paginate on `next_page_token` (b64 cursor), 0.2s inter-page sleep (mirrors ingest_prices_30m_alpaca.py convention). ≲300 requests total — trivial vs the 10k/min limit; still run `nice -n 19`, sequential.
- **Cache:** parquet per worked-session under `data/cache/min_bars/` — a **rebuildable cache, NOT part of the append-only `data/master/` family**. Phase 2/3 reuse it. (~1.3M rows ≈ tens of MB; 59G disk free.)
- **RTH windowing:** 09:30–16:00 ET via UTC bounds (13:30–20:00 UTC in EDT; convert with ZoneInfo, not fixed offsets — eval spans only Apr-Jun 2026 = EDT, but write it DST-safe; cautionary precedent: b1 `bucket_of` is EDT-only).

## 5. Oracle & benchmark math (all prices from the SAME minute-bar pull — split-adjustment-proof: splits never occur intraday, so within-day consistency is automatic regardless of the `adjustment` param)

Per (worked_session, ticker, direction):

- **Achievable-price convention:** minute-bar **vwap (`vw`)**, never bar low/high (microstructure fantasy). Skip bars where `not (vw > 0)` (NaN-safe — the `nan<=0 is False` trap, feedback_silent_failure_pattern) or `v == 0`.
- **EOD benchmark `P_eod`:** volume-weighted VWAP of the final 5 RTH minute bars (15:55–15:59) — the actual 3:55-dump window. Cross-check vs `prices.parquet` close: report divergence distribution; flag rows >50bps (adjustment/source mismatch) — flagged rows stay in but are reported.
- **Strict oracle `P_or`:** LONG → min vwap over RTH minutes; SHORT → max.
- **Window oracle `P_or15` (realistic-best):** best contiguous 15-minute volume-weighted VWAP (LONG → min window; SHORT → max). This is the headline prize; the strict oracle is the absolute bound.
- **Gross alpha (bps):** LONG `(P_eod − P_or)/P_eod·1e4`; SHORT `(P_or − P_eod)/P_eod·1e4`. (≥0 by construction vs the strict oracle when the EOD window is in the search range; window-oracle can exceed strict-window EOD slightly — fine.)
- **Net alpha:** gross − **differential cost** = haircut(oracle minute/window) − haircut(EOD window), using b1_simulator's model verbatim: `min(0.5·(high−low)/vwap·1e4, 50) + 2.0` bps (b1_simulator.py:7,15-17,20). Entry cost is paid under BOTH policies; only the difference is a real cost of moving earlier.
- **Oracle time:** minute offset from 9:30 of the (window-)oracle. **oracle≈EOD flags:** time-based (window starts within last 15 RTH minutes) and value-based (net alpha ≤ 0).
- **Sign-flip null (load-bearing — the kill-test is meaningless without it):** the directed oracle is an extreme-value statistic; best-of-~26 windows beats the close by 20–80bps on a *driftless* path with zero timing information, so a raw magnitude threshold can never kill. For every intent, compute the same window-oracle prize with the **opposite** direction on the **same bars**. Per-intent **excess = net_alpha(direction) − net_alpha(−direction)**. Paired on identical bars ⇒ extreme-value bias cancels exactly; E[excess]=0 under the no-directional-structure null; deterministic (no RNG). The raw directed prize is reported only as the **range-adequacy bound** (volatility harvest, not capturable signal); the **null-relative excess is the headline everywhere**, including Discord.
- **Two EOD benchmarks, named, never conflated:** `P_eod_dump` = final-5-min VWAP (= what the 3:55 dump pays → "beats our current execution") is the headline benchmark; `P_eod_close` = last RTH minute close, cross-checked vs `prices.parquet` close (= the backtest **parity anchor** → "beats the validated backtest") carried in the artifact and one report table.
- Missing/halted session bars → row excluded **with a counted, reported reason** (no silent caps).

## 6. Outputs

1. **Artifact** `analysis/bflow_phase1_oracle_orders.parquet` — one row per eval unit: ids, P_eod, P_or, P_or15, oracle minutes, gross/net alphas (strict + window), flags, regime, n_signals, Σ size_pct, qty/filled_avg_price (broker slice).
2. **Report** `analysis/bflow_phase1_report.md` + Discord summary to `#data-alerts` (webhook via `agent_registry.webhook_urls->>'data-alerts'`, explicit User-Agent — Cloudflare 1010 rule):
   - **Headline: null-relative excess distribution** (directed − sign-flipped, net, window oracle): median/P25/P75, equal-weight, conviction-weight (Σ size_pct), $-weight (broker slice). Raw directed prize reported separately, labeled "range-adequacy bound (not capturable)".
   - **Per-session aggregates** (n≈40 sessions → session-clustered; report per-session medians and the dispersion ACROSS sessions, not naive pooled CIs).
   - Oracle-time histogram (minute-of-day; open/mid/close buckets) + **oracle≈EOD frequency reported directed AND sign-flipped** (the difference is the direction-conditioned part; the raw frequency is mechanically low under extreme-value bias and must not be read alone).
   - LONG vs SHORT split; per-regime split (regime = `es.regime_state` per row; fallback session tag via `execution_runs` DISTINCT ON(run_date) — the only full-span source: 41 dates, full LOW_VOL/TRANSITIONING/HIGH_VOL vocabulary; `market_regime` starts 04-29 and lacks HIGH_VOL — gapped, not used).
   - Data-quality table: excluded rows by reason, parquet-vs-minute close divergence, broker-slice coverage.
   - **DTBP/PDT note (context, not modeled in Phase 1):** early entries + same-day stop/TP exits book day-trades the dump avoids — realized capture in Phase 4 is capped below this prize; modeled in the Phase 2 eval.
3. **Pre-committed kill criterion (locked now, before seeing data; on the NULL-RELATIVE EXCESS, primary grain, equal-weight):** **GO requires BOTH** (a) across-session median of per-session median net excess **> 2 bps**, AND (b) **≥60% of sessions** with positive per-session median excess (sign-test-style, session = the cluster unit). Anything less → **KILL**: the directed prize is indistinguishable from volatility harvesting and no causal engine has anything to capture; keep the 3:55 dump. On GO → Phase 2 (tick backfill + Hawkes offline true-vs-sim), reporting `capture_needed = hurdle/excess`.

## 7. Non-goals (Phase 1)

No engine, no Hawkes, no tick data, no live-path code changes, no DB migrations, no johnbot restart. Read-only against Postgres + Alpaca data API; writes only: cache parquet, analysis artifacts, new source/test files. Live-critical uncommitted files untouched. Nothing in `data/master/` is written.

## 8. Module layout

```
src/research/bflow/__init__.py
src/research/bflow/order_set.py      # §3 extraction (both grains), worked-session resolution
src/research/bflow/minbar_cache.py   # §4 fetch + parquet cache (pure fetch fn + cache wrapper)
src/research/bflow/oracle.py         # §5 math — PURE functions, no I/O
src/research/bflow/run_phase1.py     # driver: extract → fetch → compute → artifact → report → post
tests/test_bflow_order_set.py
tests/test_bflow_minbar_cache.py
tests/test_bflow_oracle.py           # synthetic-bar truth tables incl. NaN/zero-volume/short-session edges
```

Conventions: POSTGRES_URI/ALPACA_* read from `/root/openclaw/.env` directly (worktrees lack .env — b1_order_source.py:19-27 pattern); subagent-TDD, red→green, sequential test runs (2-core); `nice -n 19` for the fetch/run.

## 9. Risks / honesty notes

- **n≈40 sessions, Apr–Jun 2026, zero bear-tape:** the prize estimate is regime-thin by construction. Reported per-regime; not extrapolated. (Operator expects minimal regime dependency — this measures rather than assumes.)
- Strict oracle is an extreme-value statistic; the 15-min window oracle is the honest headline.
- Eval applies t+1 semantics to signals that were historically filled under older regimes (10am cycle / close[t] proxy) — deliberate: the counterfactual is about the CURRENT scheme, the historical fill times are irrelevant to the prize definition.
- `signal_pnl` close_reason today contains no rolls; after tonight's D1 first-mint, future re-runs will exclude them properly.
