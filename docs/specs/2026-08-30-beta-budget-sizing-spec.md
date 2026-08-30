# Spec — Beta budget: the book degrades to buy-and-hold SPY, never to re-normalized alpha

**Status:** LANDED 2026-08-30 (`bf1d1d10..99eb1ade`, final review + one fix wave clean; flag
`OPENCLAW_BENCH_BETA_BUDGET` unset = shadow; flip per §5 after two clean
shadow cycles). Amends
`docs/specs/2026-08-29-benchmark-relative-sizing-spec.md` §2.5 (rule C) and
follows amendment 1 (`docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md`).
Operator directive (chat, 2026-08-30 09:11 UTC):
"we want to benchmark our system against simply putting the full portfolio
into SPY directly and holding indefinitely and we should always beat this or
have an equivalent system."
§4's figures are the pre-implementation replay; the §5.2 `--beta-budget`
replay against a live `S_beta_spy` signal (first possible Mon 2026-08-31 after
15:00 ET) is the outstanding parity gate.

**Grounding:** every symbol below was verified against the working tree at
`d8dccc1a` on 2026-08-30. Line numbers drift; symbol names are the stable
reference.

---

## 0. Findings that motivate this spec (all measured, not asserted)

**F1 — the hurdle `S_m` IS buy-and-hold SPY partitioned by regime.** Rebuilt
from `data/derived/prices_spy_only.parquet` + `data/master/historical_regimes.parquet`
(2016-04-11..2026-08-28, 2,610 labeled days): entry-tagged H=1 slices
LOW_VOL 0.805 (920 d) / TRANSITIONING 0.422 (1,129 d) / HIGH_VOL 0.487 (411 d)
/ CRISIS 1.539 (150 d), rf 5 %. The four slices reassemble into the full
series: product of regime cum returns 4.44× = buy-and-hold 4.44×; union
Sharpe 0.618 = buy-and-hold Sharpe. Nothing is sold or re-bought; no cost.
The per-regime values equal `pipeline_config.benchmark_regime_sharpe`
(schema 2, H=1) to three decimals.

**F2 — the beta sleeve now IS that benchmark** (`d8dccc1a`, 2026-08-30):
`strategy_weights.rebuild` persists `effective_sharpe = S_m[regime]` for every
registry `benchmark_sleeve=true` strategy (`_benchmark_sleeve_effective_sharpe`).
Its own backtest sleeves (`540e3def`) equal `S_m` exactly before cost and sat
0.10–0.13 lower only from per-lot spread on 2,604 round trips a real
buy-and-hold never pays.

**F3 — rule C never moves the book TOWARD SPY.** `benchmark_sizing.apply_benchmark_hurdle`
sizes alpha on `|S_adj| − S_m` and *discards* the subtracted conviction; the
sizer then re-normalizes the survivors to `λ·NAV` (`scale = (lam * nav) / gross`,
`regime_blended_sizer._sharpe_cadence_path`). Replay 2026-08-30 (LOW_VOL,
NAV $92,343, gross `λ·NAV` = $139,491, 276 tickers, `S_m` 0.805, `S_beta_spy`
at 0.805):

| book | tickers | SPY $ | SPY % NAV | SPY % gross | alpha gross |
|---|---|---|---|---|---|
| rule C OFF (raw S_adj) | 276 | 1,037 | 1.1 % | 0.7 % | 138,454 |
| rule C ON (today's code) | 273 | 2,102 | 2.3 % | 1.5 % | 137,389 |

Σ|S_adj| = 433.6 before the hurdle, 214.3 after: rule C removes **half of the
book's conviction and hands none of it to the benchmark**.

**Correction (operator, 09:52 UTC):** the *endpoint* is already right under
rule C — with a qualified benchmark ticker, a book whose every alpha sits at or
below `S_m` leaves SPY as the only survivor and normalization puts the whole
gross on it (`λ·NAV` = 151 % NAV today, 185 % at LOW_VOL λ; §3.4's cap is
needed either way). What rule C gets wrong is the **path** to that endpoint:
SPY's share is `S_spy / (S_spy + Σ excess)`, so it stays negligible until
nearly every alpha ticker has been dropped — a cliff, not a slope. Today's
alpha vector scaled down toward the hurdle (LOW_VOL, `S_m` 0.805, SPY raw
3.23 incl. the sleeve):

| alpha scale | median \|S\| / S_m | dropped | rule C: SPY % gross / % NAV | budget: SPY % gross / % NAV |
|---|---|---|---|---|
| 1.00 (today) | 1.95 | 3 | 1.5 % / 2.3 % | 51 % / 78 % |
| 0.80 | 1.56 | 21 | 2.5 % / 3.8 % | 64 % / 97 % |
| 0.60 | 1.17 | 88 | 5.8 % / 8.8 % | 80 % / 100 % (capped) |
| 0.51 | **1.00** (median AT the hurdle) | 182 | **12 % / 18 %** | 90 % / 100 % |
| 0.45 | 0.88 | 184 | 22 % / 33 % | 94 % / 100 % |
| 0.30 | 0.59 | 268 | 79 % / 119 % | 99 % / 100 % |

With the *median* alpha exactly at `S_m` — half the fleet no better than the
benchmark — rule C still holds 82 % of NAV in alpha names, because the
surviving half's excess dominates a single SPY row. The budget makes SPY's
share the fraction of conviction the benchmark accounts for
(`(S_spy + Σ min(|S_i|, S_m)) / Σ|S|`), continuous from today's 51 % up to
the endpoint. The zero-conviction flatten is not reachable through rule C
either way (a qualified benchmark ticker is never dropped; with none, B1
skips rule C).

**F4 — realized.** Since 2026-06-23 the book went $130k → $92.3k (−29 %);
buy-and-hold SPY did +4.9 % (+4.1 % since 07-24). Nothing in the system
reports this comparison.

---

## 1. Principle

The reference portfolio is **100 % of NAV in SPY, bought once, never sold**.
Every dollar of the book must be justified as *better than leaving it in the
reference*: an alpha ticker earns capital only for the part of its conviction
that exceeds `S_m`; the part it does not exceed — the conviction the benchmark
would have supplied anyway — is **redirected to the benchmark ticker, not
destroyed**. Conviction is conserved; only its owner changes.

```
for each alpha ticker i (signed S_i, hurdle S_m ≥ 0):
    excess_i = sign(S_i) · max(|S_i| − S_m, 0)      # stays with ticker i (rule C, unchanged)
    base_i   = min(|S_i|, S_m)                       # goes to the benchmark ticker (NEW)
benchmark ticker b:
    w_b = S_b(raw, its own contributors) + Σ_i base_i / |B|      # |B| = number of qualified benchmark tickers
Σ|w| after == Σ|w| before (up to sign cancellation on b)
```

Endpoints: alpha far above `S_m` → today's book; every alpha at or below `S_m`
→ the whole conviction sits on SPY → 100 % of NAV in SPY (capped, §3.4) —
*the equivalent system*. Rule C already reaches the second endpoint (F3); the
budget fixes the interior so SPY's share rises continuously with the share of
conviction the benchmark accounts for, instead of only at the cliff.

---

## 2. Approaches considered

| | rule | SPY today (LOW_VOL replay) | zero-alpha endpoint | verdict |
|---|---|---|---|---|
| **A — conviction-conserving redirect (recommended)** | per-ticker split above; SPY inside the `λ·NAV` gross; SPY capped at `1.0·NAV` | **$71,731 = 77.7 % NAV = 51.4 % gross; alpha gross $67,760** | 100 % NAV SPY, alpha 0 | one invariant (Σ conviction conserved), one new cap, no second leverage regime |
| D — unlevered base + levered overlay | `α = Σ excess / Σ|S|`; SPY = `(1−α)·NAV` unlevered; alpha = `α·λ·NAV` | $47k SPY (51 % NAV), alpha $84k | 100 % NAV SPY | literal reading of "full portfolio in SPY" but two leverage regimes and SPY's own alpha contributors need a second home |
| B — excess-share scalar | `α = Σ excess / Σ|S|`, SPY = `(1−α)` of gross | ≈ A (49/51) | 100 % gross SPY (needs the same cap) | same numbers as A with a less local rule; dropped tickers handled by a global ratio instead of their own `base_i` |
| C — rule C alone (today) | hurdle only | $2.1k (1.5 % gross) | flatten / accidental | **rejected**: does not degrade to SPY (F3) |

Stress (A, same book): every alpha scaled to exactly `S_m` → 275 dropped,
pool 221.4, SPY 100 % of gross = **151 % NAV before the cap** — hence §3.4.
Alpha halved → 182 dropped, SPY 90 % of gross.

**D-1 (recommended, operator may override): approach A.**

---

## 3. Design

### 3.1 Pure rule (`src/execution/benchmark_sizing.py`)

Add, next to `apply_benchmark_hurdle` (unchanged signature and semantics):

```python
def apply_beta_budget(before: dict, hurdled: dict, s_m, bench_tickers: set) -> tuple[dict, float]:
    """Pure. Redirect the conviction rule C removed to the benchmark tickers.
    before  = ticker_w handed to apply_benchmark_hurdle
    hurdled = its first return value (alpha at excess, benchmark at raw)
    Returns (budgeted_weights, pool). s_m None or no bench_tickers -> (dict(hurdled), 0.0)."""
    if s_m is None or not bench_tickers:
        return dict(hurdled), 0.0
    s_m = float(s_m)
    pool = sum(min(abs(float(s)), s_m) for t, s in before.items() if t not in bench_tickers)
    out = dict(hurdled)
    share = pool / len(bench_tickers)
    for b in bench_tickers:
        out[b] = out.get(b, 0.0) + share
    return out, pool
```

- `base_i` for a ticker rule C **dropped** is its whole `|S_i|` (< `S_m`); for a
  survivor it is exactly `S_m`. Shorts contribute `min(|S_i|, S_m)` like longs
  (D7 already hurdles shorts; the alternative use of that capital is the
  benchmark) — **D-2**.
- A benchmark ticker whose raw `S_b` is negative in the GATE map (net short,
  e.g. index-short strategies outweigh the sleeve) is not in `bench_tickers`
  (net-direction qualification, base spec §2.4 i) and receives nothing; with no
  qualified benchmark ticker the whole block falls back to rule C's B1 guard
  (sized on raw `S_adj`, WARN) — unchanged. Qualification reads the gate map
  while `before` is the SIZE map, and under `OPENCLAW_STRATEGY_SIZE_SCALAR=1`
  the two can disagree in sign — so `apply_beta_budget` itself guards (final
  review fix wave, `7b347667`): any qualified benchmark ticker with
  `before[b] < 0` ⇒ WARN and the budget is skipped that cycle (rule C
  unchanged, pool 0). Several qualified benchmark tickers split the pool
  equally — **D-3** (only SPY exists today).
- `s_m ≤ 0` (never observed; CRISIS is the smallest-n at 150 d): the hurdle is
  0, the pool is 0, the book is today's — the benchmark is not worth holding
  in that regime and nothing is redirected to it. No special case.

### 3.2 Sizer wiring (`regime_blended_sizer._sharpe_cadence_path`, rule-C block)

```python
_hurdled, _bench_dropped = _bsz.apply_benchmark_hurdle(_before, _s_m, _bench_tkrs)
_budgeted, _beta_pool = _bsz.apply_beta_budget(_before, _hurdled, _s_m, _bench_tkrs)
_budget_on = _bsz.beta_budget_enabled()          # OPENCLAW_BENCH_BETA_BUDGET == '1'
_apply_hurdle = _bench_on and bool(_bench_tkrs)   # unchanged (B1)
_apply_budget = _apply_hurdle and _budget_on      # the budget is rule C's redirect; never without rule C
_bline = _bsz.shadow_line(..., budgeted=_budgeted, beta_pool=_beta_pool,
                          budget_mode='apply' if _apply_budget else 'shadow', h=_h)
if _apply_hurdle:
    ...pop dropped from ticker_meta (unchanged)...
    ticker_w = defaultdict(float, _budgeted if _apply_budget else _hurdled)
```

Downstream — acting gate (benchmark exempt, unchanged), dust floor,
`scale = λ·NAV / Σ|w|`, per-ticker cap (benchmark exempt under
`_bench_exempt`, unchanged), cluster cap (benchmark excluded, unchanged),
options overlay, broker netting — **unchanged**. The benchmark's dollar
target therefore equals `(S_b + pool/|B|) / Σ|w| · λ·NAV` before §3.4.

### 3.3 Shadow line

`shadow_line` gains four fields after `gross_moved_frac=`:
`beta_budget=shadow|apply pool=<Σ base_i, 1 dp> beta_share_budget=<SPY share of Σ|w| under the budget, 3 dp> beta_usd_budget=<that share × λ·NAV, 0 dp> beta_usd_budget_capped=<min(that, benchmark_max_nav_frac × NAV), 0 dp>`.
`beta_usd_budget` is PRE-cap (it is the §3.1 arithmetic); `beta_usd_budget_capped`
is what the book would actually carry after §3.4 and is the figure to read when
the two differ — the sizer supplies the cap via `shadow_line(..., beta_usd_cap=…)`
from inside rule C's fail-open `try` (final review fix wave, 2026-08-30).
Emitted every cycle in both lanes exactly like today's line (same
`_post_corr_cumsharpe_log` expiry gating). Existing fields keep their names so
the two owed "clean rule-C cycles" remain readable.

### 3.4 Benchmark NAV cap — **D-4**

After normalization and the existing caps, before the options overlay:

```python
if _apply_budget:
    _max = _bsz.benchmark_max_nav_frac() * nav          # pipeline_config 'benchmark_max_nav_frac', default 1.0
    for _t in _bench_tkrs:
        if abs(target_usd.get(_t, 0.0)) > _max:
            target_usd[_t] = math.copysign(_max, target_usd[_t])   # shaved, NOT redistributed
```

Rationale: the reference portfolio is unlevered. Without the cap the
zero-alpha endpoint is `λ·NAV` in SPY (151 % NAV today, 185 % at
LOW_VOL λ). Shaved capital is not re-spread (same philosophy as the per-ticker
and cluster caps: no renorm-up). On today's book the cap does not bind
(77.7 % NAV). Migration seeds `benchmark_max_nav_frac = '1.0'`
(`ON CONFLICT DO NOTHING`). Applies under the budget only; with the budget
off the benchmark keeps today's uncapped D6 treatment.

### 3.5 Zero-conviction flatten interaction

`_maybe_flatten_zero_conviction` is reached only when `ticker_w` is empty
after the acting gate. That is already unreachable through rule C: a qualified
benchmark ticker is never dropped, and with none qualified B1 skips rule C
entirely (B2 is a defensive log for an impossible branch). The budget keeps
that: the benchmark ticker always carries the pool. When the budget applies
and every alpha ticker was dropped, log at INFO
`bench_sizing: beta budget moved the whole book to beta (pool=…, dropped=N)` so the
100 %-SPY day is attributed correctly. No change to the flatten's data-absence
branches.

### 3.6 Lanes, fail-open, determinism

- Both lanes (daily 15:00 ET compute and `OPENCLAW_INTRADAY_REDEPLOY=1`)
  run the same block, as rule C does today. The intraday lane reads the same
  schema-2 cache (no parquet re-read).
- Whole block stays inside rule C's `try` — any exception → sized on raw
  `S_adj`, WARN (unchanged contract).
- Pure functions, no randomness; the replay reproduces the live book.

### 3.7 Replay (`scripts/bench_relative_sizing_replay.py`)

Add `--beta-budget` (sets `OPENCLAW_BENCH_BETA_BUDGET=1` for the ON leg and
prints `beta_usd`/`alpha_gross` beside the existing diff) and
`--max-nav-frac` (overrides the cap for what-ifs, read-only). The OFF leg is
unchanged so the printed diff stays the parity artefact.

---

## 4. Expected effect (measured on today's book; live signals from 08-28)

| | rule C only | rule C + beta budget (A) |
|---|---|---|
| tickers | 273 | 273 (same 3 dropped: BWLP, SHY, WDC) |
| SPY | $2,102 (2.3 % NAV) | **$71,731 (77.7 % NAV, 51.4 % gross)** |
| alpha gross | $137,389 | **$67,760** |
| largest alpha position | $1,558 (AKTS) | $768 |
| Σ conviction | 214.3 | 434.5 (conserved) |

The alpha book halves; SPY becomes ~half the gross. That is the direct
consequence of the operator principle on a book whose median `|S_adj|` (1.57)
is roughly 2× `S_m` (0.805). CRISIS will redirect more (16 of 50 approved
strategies sit below `S_m` = 1.54 there) but its `liquidity_param` 0.25
de-levers the whole gross to 0.46·NAV first.

Costs of the flip day: one SPY buy (~$70k, spread ≈ 1 bp) and ~$70k of alpha
sells across ~270 small names (the real cost; same names would have been
trimmed by rule C's 11 % move anyway). Recommend **one** flip of rule C +
budget together rather than two churn events — **D-5**.

---

## 5. Rollout

1. Code lands with `OPENCLAW_BENCH_BETA_BUDGET` unset (= shadow). The shadow
   line then prints both `dropped=` (rule C) and `beta_usd_budget=` (budget)
   every cycle.
2. Two clean shadow cycles (first Mon 2026-08-31 15:00 ET) with the sleeve
   emitting its first live signal — check `bench=['SPY']`, `h=1`,
   `beta_usd_budget` in the $60–80k range for LOW_VOL, **`beta_usd_budget_capped`**
   (the post-§3.4 figure the book will actually carry — read this one when the
   two differ) and the replay's `--beta-budget` diff agreeing with the line.
3. Operator flips **both** `OPENCLAW_BENCH_RELATIVE_SIZING=1` and
   `OPENCLAW_BENCH_BETA_BUDGET=1` in `.env`, user-scope johnbot restart
   (`XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot`), outside
   13:00–20:15 UTC (D-5). The budget flag alone does nothing (§3.2).
4. Watch the first apply cycle: `bench_sizing.apply[...] beta_budget=apply`,
   SPY target within the NAV cap, `[exit_hook]` untouched, the 15:55 executor
   filling one SPY buy plus the alpha trims.
5. Kill switch: `OPENCLAW_BENCH_BETA_BUDGET=0` + restart returns to rule C
   alone; `OPENCLAW_BENCH_RELATIVE_SIZING=0` returns to raw `S_adj`.

---

## 6. Monitoring — "we should always beat this" made visible (**D-6**)

A read-only daily line, `bench_realized`, computed at the end of the 16:15 ET
collect step from `logs/pnl_daily_ohlc.json` (`days[<date>].close` = NAV) and
SPY closes (`data/derived/prices_spy_only.parquet` / the prices DB):

```
bench_realized: since=2026-06-23 book=-29.0% spy=+4.9% gap=-33.9pp | 20d book=… spy=… | 60d book=… spy=… | regime=LOW_VOL book_sharpe_20d=… S_m=0.805
```

Posted to #botjohn-log with the other daily lines. Pure report; it gates
nothing. The anchor date is `pipeline_config.bench_realized_anchor`
(seed `2026-06-23`, the P&L-bleed memory's start). Per-regime realized Sharpe
uses the same regime-of-record stamps the rollup already keeps
(`strategy_regime_live_pnl_rollup`); if that join proves awkward the line ships
without the regime clause first.

Implementation note: the line is appended to the #trade-reports digest by
send_report (same daily post), not posted separately to #botjohn-log.

---

## 7. Tests (named files only; never the full suite while the fleet runs)

- `tests/execution/test_benchmark_sizing.py` — `apply_beta_budget`: conserves
  Σ|w| (survivor contributes `S_m`, dropped contributes `|S_i|`, shorts count),
  `s_m None` / empty bench → identity + pool 0, two benchmark tickers split
  equally, benchmark's own raw weight preserved, `shadow_line` prints the three
  new fields.
- `tests/execution/test_sizer_benchmark_hurdle_wiring.py` — budget applied only
  when both flags are `'1'` and a qualified benchmark ticker exists; SPY target
  = `(S_b + pool)/Σ|w|·λ·NAV`; flag OFF book byte-identical to rule C ON;
  both flags unset byte-identical to today (`setenv` both to `'0'`, dotenv
  re-populates on `delenv`).
- `tests/execution/test_sizer_benchmark_cap_exemptions.py` — NAV cap clamps
  the benchmark at `benchmark_max_nav_frac·NAV`, no redistribution; alpha
  untouched; cap inert with the budget off.
- `tests/execution/test_sizer_flatten_zero_conviction.py` (existing) — all alpha ≤ `S_m` with a qualified
  benchmark ticker → 100 % NAV SPY, no flatten; without one → today's flatten.
- `scripts/bench_relative_sizing_replay.py --beta-budget` run once as the
  parity artefact before the flip (§5.2).

---

## 8. Out of scope / deferred

- Mean-variance-optimal beta/alpha split (would size alpha on Sharpe, not
  excess) — contradicts the operator's rule-C principle; not proposed.
- Leveraging the benchmark above NAV (`benchmark_max_nav_frac > 1`) — a
  config value, not a code path; default stays 1.0.
- Per-strategy "beat SPY" gates — R1 was deliberately removed (base spec D1)
  and stays removed; the benchmark acts through sizing only.
- Replacing the alpha book with SPY futures/options for margin efficiency —
  not on this account.

---

## 9. Decisions recorded (operator may override before planning)

- **D-1** Approach A (conviction-conserving redirect inside `λ·NAV`), not D/B.
- **D-2** Shorts contribute `min(|S_i|, S_m)` to the beta pool.
- **D-3** Several qualified benchmark tickers split the pool equally.
- **D-4** Benchmark capped at `benchmark_max_nav_frac·NAV`, default 1.0, shaved not redistributed, budget-only.
- **D-5** One combined flip (rule C + budget) after two clean shadow cycles; no rule-C-alone step.
- **D-6** `bench_realized` daily line ships with this spec (report only).
- Not changed by this spec: `S_m` definition (forward H=1, rf 5 %), the sleeve's
  weight (= `S_m`), the acting gate / per-ticker cap / cluster cap exemptions,
  the flatten's data-absence branches, D7 (shorts hurdled), D9 (similarity).
