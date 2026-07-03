# W7 — Alpaca Live-Readiness Research (Paper → Live)

**Status: DOCS-ONLY research deliverable (2026-07-02). No config changed, no orders placed.**
Account context at research time: paper ~$94k equity, 1.77x gross, ~280 positions both sides, equity + crypto lanes live, options backtest-only, AAT Plus data tier.

> **REGIME-CHANGE HEADLINE (supersedes several prior assumptions):** On **2026-06-04 FINRA retired the PDT rule**. Alpaca replaced PDT/DTBP with a new **Intraday Margin Framework**: no $25k minimum, no day-trade counting, **4x intraday BP at ≥$2,000 equity**, and Intraday Buying Power **updates in real time** (legacy DTBP was start-of-day-fixed). The account-API fields `pattern_day_trader`, `daytrade_count`, `last_daytrade_count`, `last_daytrading_buying_power`, and **`daytrading_buying_power` are removed by 2026-07-06** (dummy values until then).
> Sources: <https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/> , <https://docs.alpaca.markets/us/docs/the-intraday-margin-rule>
>
> **System impact (FIXED 2026-07-02, commit `b6a618a`):** `_dtbp_opening_budget()` read the removed field via `or 0.0` → budget 0 → every open skipped. Migrated to `min(buying_power, regt_buying_power)` with the legacy field honored while present.

## Paper-vs-Live delta table

| Dimension | Paper | Live | System impact | Param to tune |
|---|---|---|---|---|
| Fill price | Matched at NBBO touch (marketable-only fills); no midpoint, no price improvement ([paper docs](https://docs.alpaca.markets/docs/paper-trading)) | Wholesaler execution (Virtu/Citadel/Jane Street/GTS per [1Q2026 606 report](https://files.alpaca.markets/disclosures/library/SEC+606a1+-+2026Q1.xml)); PFOF ~64–82¢/100sh marketable; possible improvement, also impact | Live fills can be better or worse than paper's NBBO-touch assumption | `INSTRUMENT_COST_BPS` (`src/backtest/unified_backtest.py:77`, equity 10bps one-way); `CAPTURE_RATIO=0.80` (`src/execution/trade_agent.py:70`) |
| Fill size / partials | Always fillable regardless of liquidity; synthetic partials "for a random size 10% of the time"; qty NOT checked vs NBBO size | Real liquidity; frequent fragmented partials on thin names; non-marketable limits queue by real priority | Wide-universe (5,180-name) thin tickers fill materially worse live; the 280-position paper book overstates capacity | ADV-based per-ticker size caps in sizer; revisit `SIZE_CAP=0.25` (`src/execution/b1_order_source.py:16`) |
| Not simulated at all (Alpaca's list) | Market impact, information leakage, latency slippage, queue position, price improvement, **regulatory fees**, **dividends**; borrow fees "Coming Soon" | All real | Live P&L structurally differs on sells (reg fees), shorts (dividends-in-lieu, borrow), longs (dividends) | Add fee/dividend legs to P&L reconcile (deferred U1 = this stream) |
| DTBP / intraday margin | PDT checks deprecated 2026-06-04; `daytrading_buying_power` dummy until 2026-07-06 removal | Real-time Intraday BP incl. intraday P&L; pre-trade rejects on margin deficit; IMD call → 2 business days to cure; unmet by day 5 → up-to-90-day restriction | **Was BREAKING** — fixed in `b6a618a`; sizer reads (`regime_blended_sizer.py:771,874`) already prefer `buying_power` | Kill-switch `OPENCLAW_DTBP_GUARD` unchanged |
| 4x intraday eligibility | N/A (permissive) | `multiplier=4`: 4x intraday / 2x Reg-T overnight, now at ≥$2k equity ([account plans](https://docs.alpaca.markets/us/docs/account-plans)) | 4x sizer fallback (`4.0*nav`) defensible IF account has multiplier=4 | Verify live `multiplier == 4` at go-live |
| Overnight leverage | Not enforced realistically | Reg-T 2x overnight (`regt_buying_power`); EOD breach → margin call next morning; Alpaca may liquidate ([margin docs](https://docs.alpaca.markets/us/docs/margin-and-short-selling)) | **1.77x gross is only ~12% below the 2x ceiling** — a day like 07-01 (−20.6%) is a margin-call day live | `pipeline_config.position_sizing_lambda` (1.85 target) → recommend ≤1.6–1.7 + explicit EOD `regt_buying_power` headroom check |
| Negative cash | Cosmetic | Normal on margin (all Alpaca accounts are margin), but debit accrues **6.25% margin interest** (4.75% Elite), (balance×rate)/360 daily, 3 days over weekends | Structural negative-cash survives live but becomes real carry, unsimulated in paper | Add margin-interest leg to carry/cost model |
| Shorting | No locate/borrow simulation | ETB (~5,000+ names): **$0 borrow fees**; shorts in names flipping ETB→HTB overnight are **auto-cancelled pre-open**; forced buy-ins on recall; HTB availability **conflicting docs** (margin doc shows `/v1/locates` workflow; 07-2026 fee schedule says "Not currently available") | Universe predicate already requires `easy_to_borrow` (`src/strategies/universe_default.py:64`) ✓; handle pre-open auto-cancels + new `borrow_status` field | Daily `borrow_status` check in `refresh_tradable_universe.py`; treat HTB as unavailable |
| Rejects | 403s for wash-trade & insufficient qty; user-protections "same in paper" ([user protection](https://docs.alpaca.markets/us/docs/user-protection)) | Adds margin-deficit pre-trade rejects, locate rejects, liquidity failures, 600%-of-equity restriction (liquidation-only) | **The 73 close rejections on 07-01 likely wash-trade / qty-reserved-by-bracket-legs — reproduces live and worsens under real partials** | Ensure cancel-bracket-legs-before-close ordering everywhere (pattern at `alpaca_executor.py:2101`) |
| OPG/CLS auction orders | **Auctions not simulated** — treated as regular market orders ([staff, unofficial](https://forum.alpaca.markets/t/accurate-opg-and-cls-prices-for-paper-trading/3762)) → explains the ~7% paper OPG fill rate | Routed to primary-exchange auction; OPG rejected 9:28am–7:00pm ET submission; CLS rejected 3:50pm–7:00pm. **CONFLICT:** a Learn article says OPG/CLS Elite-only; API docs show no restriction | The 3:55 AM open-fill lane should fill near-100% live — paper 7% is an artifact — **IF** OPG is available on a standard account | Verify OPG availability with support (lane-critical) |
| Extended hours | ~24% unfilled + ~1% NAV slippage (our paper data); paper doesn't model queue/liquidity → live likely worse | Limit + `extended_hours=true`, TIF day or gtc; pre 4:00–9:30, after 16:00–20:00, **plus overnight 20:00–04:00 ET Sun–Fri via Blue Ocean ATS** (`overnight_tradable` flag, limit-only, `feed=boats`) ([24/5](https://docs.alpaca.markets/us/docs/245-trading)) | Keep after-hours lanes gated; overnight session is new unused capability | Keep `OPENCLAW_REDEPLOY_EXTENDED_HOURS=0` |
| Brackets | RTH-only enforced | Brackets reject `extended_hours=true`; TIF day/gtc; **GTC auto-cancels after 90 days** at 4:15pm ET | Matches current behavior; note 90-day GTC expiry for `stop_reattach.py` gtc stops | Track `expires_at` on gtc legs |
| Crypto | Simulated fills; fee simulation UNCONFIRMED | Maker/taker tiers (base 15/25bps), fee in credited asset; gtc/ioc only (matches `_CRYPTO_TIF='gtc'`); non-marginable, non-shortable; custody Alpaca Crypto LLC, not SIPC/FDIC ([crypto fees](https://docs.alpaca.markets/us/docs/crypto-fees)) | Backtest crypto 25bps one-way ≈ base taker — adequate | Confirm sizer never margins crypto (`non_marginable_buying_power`) |
| Trading API rate limit | 200 req/min per account (429 on excess); no paper/live difference documented | Same 200/min; increases via support | EOD bursts on a ~280-position book can brush 200/min; **no 429 backoff in the CLI submit path** | Add pacing/429 retry around `alpaca` CLI submits in `alpaca_executor.py` |
| trade_updates stream | Binary frames on paper endpoint; synthetic partials | Real `partial_fill` cadence far higher on thin names | Fix any consumer assuming ≤1 partial | — |

## Fees table (live; paper simulates none of the regulatory/margin items)

Primary source: [Brokerage Fee Schedule, revised 2026-07-01](https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf)

| Fee | Rate | Applies to |
|---|---|---|
| Equity commission | $0 (self-directed retail API) | all trades |
| SEC fee | $0.0000206 × trade value | equity+option **sells** |
| FINRA TAF (equity) | $0.000195/share, cap $9.79/trade | **sells** |
| FINRA CAT | $0.000003/executed-equivalent share | buys AND sells (options: 1 contract = 100 equiv) |
| Options commission | $0 retail | — |
| OCC clearing | $0.025/contract (capped beyond 2,750) | option buys+sells |
| ORF | $0.015/contract | option buys+sells |
| FINRA TAF (options) | $0.00329/contract | option **sells** |
| Crypto | Tiered maker/taker 0.15%/0.25% (<$100k 30d vol) → 0.00%/0.10% ($100M+) | crypto trades |
| ETB short borrow | $0 | ETB shorts |
| HTB borrow | Σ(short MV × HTB rate)/360 + locate fees — availability conflicting | HTB shorts |
| Margin interest | 6.25% (4.75% Elite) × settlement debit /360, daily accrual | overnight debit balances |
| AAT Plus data | $99/month | subscription |

Fees aggregate per type per day, each rounded UP to the cent, posted EOD ([regulatory fees](https://docs.alpaca.markets/us/docs/regulatory-fees)). Sell-side reg drag ≈ 0.2bp + TAF (per-share → heavier on low-priced names).

## Hard blockers / risks for going live (ranked)

1. ~~**`daytrading_buying_power` removal 2026-07-06 breaks all opens (paper too).**~~ **FIXED** commit `b6a618a` (2026-07-02).
2. **Overnight leverage headroom.** 1.77x gross vs hard 2x Reg-T overnight ceiling; live enforces via morning margin calls + broker liquidation; the new framework adds pre-trade rejects + IMD calls. The 07-01 −20.6% day would have been a margin-call day live.
3. **Fill realism on the wide universe.** Paper fills any size at NBBO touch. Live: real depth, wholesaler handling, fragmentation. Expect worse prices and stranded partials on the 5,180-name tail.
4. **Close-order rejection cascade (07-01's 73 rejections) is structural, not paper noise.** Wash-trade protection + qty-reserved-by-open-bracket-legs apply live. A failed-close loop live = unhedged overnight exposure.
5. **OPG availability unconfirmed for standard accounts** (Elite-gating conflict). The next-open fill lane depends on it.
6. **Short-side events not in paper:** ETB→HTB overnight auto-cancels, lender recalls/buy-ins, dividends-in-lieu.
7. **Unmodeled carry:** margin interest, reg fees on every sell, dividends both ways — reshapes a high-turnover both-sides book's net P&L.
8. **Rate limits:** 200 req/min on order bursts; no 429 backoff in the CLI submit path.
9. **(Operator-owned)** credential rotations still pending — rotate all keys before funding live.

## Param tuning recommendations (exact keys)

- ~~`_dtbp_opening_budget` migration~~ — DONE (`b6a618a`); also purge remaining `pattern_day_trader`/`daytrade_count` reads opportunistically.
- `pipeline_config.position_sizing_lambda` (~1.85 target): live overnight cap is 2.0 hard → recommend ≤1.6–1.7 + explicit EOD `regt_buying_power` headroom check; keep `position_sizing_lambda_intraday`=1.0.
- `INSTRUMENT_COST_BPS` equity 10bps one-way: plausible liquid, optimistic for wide-universe tails; add sell-side reg-fee leg; crypto 25bps fine.
- `CAPTURE_RATIO=0.80`: recalibrate from live fills after a soak period.
- Keep `OPENCLAW_REDEPLOY_EXTENDED_HOURS=0`; `afterhours_tp.py` (limit+day+ext=true) stays valid (gtc now also allowed).
- `refresh_tradable_universe.py`: ingest the new `borrow_status` field alongside `shortable`/`easy_to_borrow`; keep ETB-only predicate.
- OPG lane: submission window OK (after 7:00pm ET queues for next open; 9:28am cutoff); re-baseline lane-health alerts after go-live (expect ≫7% fills).
- Add 429/pacing wrapper around `alpaca` CLI order submits (~200/min budget).
- U1 P&L reconcile: add dividends, payments-in-lieu, reg fees, margin interest, crypto fees as first-class legs.

## AAT Plus findings

- **Contents** ($99/mo): full SIP real-time equities (free tier = IEX-only); no 15-min historical restriction; unlimited websocket symbols but **1 concurrent connection** (2nd → error 406); real-time OPRA options feed (free tier: indicative quotes, 15-min-delayed trades); crypto included; rate limit 10,000/min (plans page now says "Unlimited" — UNCONFIRMED). ([data plans](https://alpaca.markets/data), [market data API](https://docs.alpaca.markets/us/docs/about-market-data-api))
- **Zero-greeks-on-SPY:** staff-confirmed ([forum, unofficial](https://forum.alpaca.markets/t/0dte-options-greeks/14697)) — greeks computed via Black-Scholes return **null for 0DTE contracts** (T−t=0). Not a tier problem; snapshots/chain are the only greeks source. **Fix: compute greeks locally** (BS from OPRA quote + underlying) for same-day expiries — relevant to `src/pipeline/backfillers/alpaca_options.py:202`. Also verify the API key is attached to the AAT Plus subscription — `feed` silently falls back to indicative data otherwise.

## Open questions for the operator

1. ~~Is OPG/CLS available on a standard (non-Elite) live account?~~ **ANSWERED by Alpaca support (2026-07-02): OPG works on live accounts** — submit market/limit OPG before the open; executes ONLY in the opening auction, unfilled portions cancelled; submissions 9:28am–7:00pm ET are rejected, after 7:00pm queued for next day; execution timing per exchange auction rules. → The EOD→next-open fill lane is live-viable; the paper 7% fill rate stays an artifact.
2. Will the live account be provisioned `multiplier=4`? (Needs only ≥$2k equity now, but confirm.)
3. HTB shorting availability (margin doc vs fee schedule conflict) — recommendation: stay ETB-only regardless.
4. Does paper deduct crypto maker/taker fees? (UNCONFIRMED both ways.)
5. Is the Intraday Margin Framework (IMD calls, restrictions) simulated in paper? (Assume not.)
6. Root-cause the 07-01 73 close rejections (wash-trade vs qty-reserved) before live — the failure mode persists live.
7. Consider Alpaca Elite at go-live: 4.75% margin interest, bundled data, smart-router/DMA — but Elite routing "may preclude commission-free trades."
8. Request a Trading-API rate-limit increase if order-burst modeling shows >200/min peaks.
