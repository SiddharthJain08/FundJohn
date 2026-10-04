# Universe security type + backtest universe parity — spec (operator-ruled 2026-10-04 21:42 UTC: "go with your recommendations on both")

Diagnostic: `.superpowers/sdd/2026-10-04-universe-security-type/diagnostic.md`; learning LRN-20261004-002.

## 0. Problem
1. **No security-type filter.** `ticker_metadata_snapshots.in_r1000 / in_r3000` are market-cap ranks over everything the broker
   labels `us_equity`; fund AUM counts as market cap. On 2026-10-02: ~194 of 1,000 and ~747 of 3,000 members are ETFs/funds
   (cash, bond, 55 leveraged, crypto trusts). Cross-sectional stock-factor strategies rank funds with stocks — in backtest AND
   live. `low_volatility_us` and `S_low_volatility_us_gk63` select T-bill ETFs (Sharpe −2.47 / −8.12); `S_idiosyncratic_vol_puzzle`
   has 71 % of its backtest trades in funds.
2. **Backtest ≠ live universe.** 94 of 105 live strategies backtest on the unbounded ~12.9k-column panel; 83 carry a live
   `universe_filter_ref` and no `backtest_universe_cap` (`OPENCLAW_BT_UNIVERSE_FILTER_REF` is OFF); 32 of them are active. Even
   `sp500`-filtered strategies show 11–14 % fund trades in backtest. Promotion, activation and weights rest on those Sharpes.

## 1. Phase 1 — security type as data + predicates (code; NO behaviour change for any existing strategy)
- **Source.** `data/.cache/fmp_profile.json` already stores `isEtf` / `isFund` / `isAdr` per symbol (13,702 entries on 10-03:
  5,726 ETF, 368 fund, 685 ADR, 6,271 plain, 651 empty tombstones). No new vendor call.
- **Column.** Additive migration: `ticker_metadata_snapshots.security_type TEXT` (NULL = unknown). `ticker_metadata_writer`
  maps `isEtf → 'etf'`, `isFund → 'fund'`, `isAdr → 'adr'`, else `'stock'`; empty/missing profile → NULL. Precedence etf > fund > adr.
- **Dotted symbols.** Profiles for class shares (`BRK.B`) are tombstoned because the vendor uses `BRK-B`; the refresh script
  retries the hyphen form before writing a tombstone (cache key stays the broker symbol).
- **`TickerMetadata.security_type: Optional[str] = None`**, read with `.get` so older rows / frozen artifacts without the column load.
- **Predicates** (`src/strategies/universe_default.py`): `common_stock(meta)` = `security_type in ('stock','adr')` OR
  (`security_type is None` AND `in_sp500`) — unknown is EXCLUDED unless index membership vouches for it. Composed tiers
  `stocks_sp500`, `stocks_r1000`, `stocks_r3000`, `stocks_liquid` = existing tier AND `common_stock`; registered in the ladder
  predicate set so `universe_filter_ref` and `backtest_universe_cap` accept them.
- **History.** Security type is treated as a static attribute: the LATEST known value is applied to every snapshot date (an ETF was
  always an ETF). Snapshots written before this change have NULL in the column; resolvers and the frozen membership artifact
  builder (`universe_tier_membership_*.parquet`) join the latest non-NULL type per symbol. The artifact gains the four `stocks_*`
  tiers. Documented, accepted non-point-in-time attribute.
- **Report.** `scripts/report_fund_exposure.py` (read-only): per strategy, share of latest-primary-run backtest trades in
  ETF/fund tickers + live filter + activation — the evidence for Phase 2.
- Existing tiers and every strategy's universe are byte-identical after Phase 1 (pin with a resolver parity test).

### Phase 1 — AS BUILT (2026-10-04; opus review NEEDS FIXES → CLEAN; where this differs from the text above, THIS governs)
- `security_type` values: `etf`, `fund`, `spac` (industry "Shell Companies"), `deriv` (warrants / units / rights by name),
  `pref` (preferreds, notes, debentures, coupon-bearing lines by name), `cef` (closed-end funds and BDCs: industry "Asset
  Management*" with fund / lending / capital-corp naming), `adr`, `stock`; NULL = unknown. Precedence in that order. Over the
  2026-10-03 profile cache: etf 5,727 · stock 5,339 · adr 684 · spac 686 · unknown 651 · fund 369 · pref 105 · deriv 91 · cef 50.
  `common_stock` = `stock` or `adr`, or unknown AND `in_sp500`. BDCs stay excluded (pass-through investment companies).
- Migration 163 also adds a partial index for the latest-type lookup. The live resolver's type overlay is fail-open (a failed
  lookup yields no types and never changes an existing universe).
- The four `stocks_*` names are valid `universe_filter_ref` / `backtest_universe_cap` values but appear in no auto-adoption path
  (the recommender's candidate lists are hard-coded) and not in the PaperHunter mint menu.
- **Owed before Phase 2 (runbook):** (1) SECONDARY LINES listed under the parent's plain name (NTRSO, TPGXL, MGRB…) are still
  typed `stock` — add a cache-wide `secondary_line` post-pass (same `cik` AND identical name AND parent symbol + 1–2 trailing
  letters from the preferred/warrant/unit/note suffix set, typed `stock` only, excluding real class shares such as GOOGL) plus a
  manual deny-list; reviewer hit list: 226 symbols, 63 at ≥ $1B. (2) SURVIVORSHIP: symbols delisted before migration 163 have no
  type and are excluded from `stocks_*` unless `in_sp500`, biasing stock-tier backtests upward — quantify from the first rebuilt
  membership artifact and rule on unknown symbols before any strategy opts in. (3) Live vs artifact: until the first post-163
  metadata write (weekday 13:30Z unit) the live resolver sees no types while the artifact builder falls back to the profile cache.
- Operator step after merge: rebuild the tier membership artifact (`scripts/build_tier_membership.py`) via a transient unit with
  `EnvironmentFile=` (never source `.env`), outside the compute windows.

## 2. Phase 2 — opt stock-factor strategies into `stocks_*` (operator script, manifest lock, dry-run default)
`scripts/apply_stock_universe_optin.py` rewrites `metadata.universe_filter_ref` tier → the matching `stocks_*` tier for an explicit,
reviewed list (and records the prior value in `metadata.universe_filter_ref_prior`). Initial recommended list = strategies that are
stock anomalies by design with a non-trivial fund share (5 %–99 %) in the 2026-10-04 measurement, e.g. `low_volatility_us`,
`S_low_volatility_us_gk63`, `S_idiosyncratic_vol_puzzle`, `S_microcap_insider_purchase_momentum`, `S_amihud_illiquidity_premium`,
`S_ast_roa_effect_within_stocks`, `S_ast_residual_momentum_factor`, `S_ast_trend_following_effect_in_stocks`,
`S_cross_sectional_price_momentum`, `S_long_term_price_reversal`, `S_downside_beta_premium`, `S_intl_momentum_attention_regime`,
`S_macro_risk_momentum_ip_beta`, `S_overnight_intraday_tug_of_war`, `S_volume_shock_overnight_drift`, `S_52wk_low_capitulation_reversal`,
`S24_52wk_high_proximity`, `S_news_sentiment_long_short`, `S_value_momentum_everywhere` … (final list = Phase 1 report + operator
review). NOT opted in: ETF/cross-asset strategies by design (the `oxf_*` family, gold, sector/country ETF, commodity ETP, asset-class
trend, dual-momentum rotations, `S_beta_spy`, index-timing strategies) and pairs/stat-arb strategies pending a per-strategy look.
A live strategy's universe changes on its next signals run; the operator applies the list in a safe window.

## 3. Phase 3 — backtest universe parity epoch
Turn `OPENCLAW_BT_UNIVERSE_FILTER_REF=1` on for the nightly fleet and re-gate every strategy once (after Phase 2, so one epoch covers
both changes): checkpoint of the current primary runs, a systemd drop-in for the fleet units, a weekend catch-up unit, a uniformity
check, then weights rebuild → activation apply → floor recheck — the 2026-09-10 target-geometry epoch runbook reused. Bounded
universes are cheaper per run than the unbounded panel. Gate for declaring the epoch done: fleet uniform on the flag, and a
before/after table of per-strategy Sharpe and activation changes reviewed by the operator before the activation apply.

## 4. Out of scope
Point-in-time security type; reclassifying crypto trusts beyond `etf`; changing index-membership flags themselves; options universes.
