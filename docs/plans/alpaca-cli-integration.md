# Alpaca CLI Integration Plan

**Status:** Planning — not for immediate implementation. Captured 2026-04-28
after auditing https://github.com/alpacahq/cli.

## Context

The Alpaca CLI is a Go binary that wraps the full Alpaca trading + market
data API behind first-class commands. It's explicitly designed for AI
agents and automation pipelines (no positional args, JSON-on-stdout,
exit-code discipline, OpenAPI-generated flags + completion). FundJohn
currently hand-rolls every Alpaca API touch in Python/JS — order
submission, retry-on-422 base_price, dup-coid recovery, market-hours
detection, portfolio history proxying, etc. The CLI replaces hundreds
of lines of plumbing with one binary invocation per action and adds
new capabilities we don't have today.

Install (when ready):

```
go install github.com/alpacahq/cli/cmd/alpaca@latest
# or:  brew install alpacahq/tap/cli
```

Configuration via env vars (matches our current `.env`):
- `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` — both required
- `ALPACA_LIVE_TRADE=true` — opt out of paper trading default
- Otherwise paper-trading is the default

---

## Direct replacements (Tier 1 — medium effort, large reduction)

### 1. Replace `alpaca_executor.py` order-submission core

**What we have:** `src/execution/alpaca_executor.py` `execute_single`
hand-rolls `https.request()` to `/v2/orders`, with bespoke handling for
422 `base_price` errors (snap-retry the stop), 422 `client_order_id`
duplicates (recover via `/v2/orders:by_client_order_id`), and a
deterministic-coid generator. ~200 lines of plumbing.

**Replace with:**
```bash
alpaca order submit \
  --symbol CHTR --side buy --qty 122 --type market \
  --time-in-force day --order-class bracket \
  --take-profit-limit-price 38.70 --stop-loss-stop-price 33.35 \
  --client-order-id AX20260427_CHTR_S5_max_pain \
  --json
```

The CLI handles automatic retry on 429 + 5xx, the `--client-order-id`
gives idempotency, and `alpaca order get-by-client-id` is the canonical
recovery path on dup-coid. Delete our 422 retry logic, our coid recovery
loop, and our raw-https plumbing.

**Files to touch:**
- `src/execution/alpaca_executor.py` — keep the deterministic sizer
  output and DB-persistence layer; replace just the network call.
- `src/execution/alpaca_trader.py` — the `_alpaca_session` helper
  becomes unused; delete or keep as a thin shim around `alpaca account
  get` for live equity.

**Risk:** moderate. Need to map every flag we currently use (TIF,
order_class, take_profit, stop_loss, client_order_id, extended_hours)
to the CLI's exact flag spelling. Validate with `--dry-run` first.

### 2. Replace dashboard portfolio endpoints

**What we have:**
- `src/channels/api/server.js:829` — `/api/portfolio/account` proxies
  `/v2/account`
- `src/channels/api/server.js:869` — `/api/portfolio/value-curve`
  proxies `/v2/account/portfolio/history`, then synthesizes today's
  point from `/account.equity` because Alpaca paper's history endpoint
  lags 1 day.

**Replace with:** subprocess invocations of `alpaca account get` and
`alpaca account portfolio --json`. The CLI's `account portfolio` may
already handle today's running mark-to-market; verify.

**Files to touch:** `src/channels/api/server.js` — the two endpoints.

**Risk:** low. Pure read, dashboard-only.

### 3. Add daily reconciliation against actual fills

**What we have:** `signal_pnl.status` flips from open→closed via
arithmetic on parquet prices vs. signal target/stop. We never reconcile
against Alpaca's actual fill events. A signal that was rejected by
the broker but our parquet says "would have hit target" gets credited
in our P&L stats.

**Add:** new pipeline step (or end-of-cycle hook) that calls
`alpaca account activity list-by-type --type FILL --date $TODAY --json`
and reconciles each fill against `alpaca_submissions.alpaca_order_id`.
Mark each signal as `actually_filled`/`partial`/`rejected_by_broker`,
update `signal_pnl` accordingly.

**Files to touch:** new `src/execution/alpaca_reconcile.py` invoked as
the post-alpaca step.

**Risk:** low (additive); high value (closes a real attribution hole).

### 4. Replace `in_market_hours()` with `alpaca clock`

**What we have:** `src/execution/alpaca_trader.py` likely has a custom
`in_market_hours()` checking NYSE hours. It missed yesterday's OPG
window edge case (orders rejected outside 19:00–09:28 ET window).

**Replace with:** `alpaca clock --json | jq '.is_open, .next_open, .next_close'`
once at orchestrator step start. Pass results down to `alpaca_executor`.

**Risk:** trivial.

---

## New capabilities (Tier 2 — low effort, opens doors)

### 5. Broker-side options greeks via `alpaca data option chain`

**What we have:** `src/pipeline/collector.js` `runOptionsYFinance` (line
426+) fetches options from yfinance and computes greeks ourselves with
Black-Scholes. Subject to bugs in our IV solver, missing fields when
yfinance returns sparse data, etc.

**Replace with:** `alpaca data option chain --underlying-symbol AAPL
--json` returns options with broker-canonical greeks (delta, gamma,
theta, vega, rho) and live IV. Strategies S5_max_pain, S_HV*, S15,
S21 immediately benefit.

**Risk:** low. Replace one collector phase; keep parquet schema.

### 6. Dynamic universe via screener

**What we have:** static universe in `universe_config` table.

**Add:** Saturday brain runs `alpaca data screener most-actives --json`
+ `alpaca data screener movers --json` to pull volume + momentum-ranked
candidates daily. Feed candidates into the corpus or staging buckets.

**Risk:** low (additive).

### 7. Corporate actions tracking

**What we have:** parquet has stale prices through corporate actions —
splits, dividends, mergers create phantom drawdowns in backtests.

**Add:** `alpaca data corporate-actions --symbols AAPL,GOOG --json`
nightly pull. Adjust historical prices for splits; keep a separate
`corporate_actions.parquet` ledger.

**Risk:** medium (touches backtest math); high value (real bug fix).

### 8. `alpaca order replace` for stop-adjustment

**What we have:** we don't currently move stops post-fill. MastermindJohn's
position-recommendations flow proposes stop deltas but we have no
mechanism to apply them to live orders.

**Add:** wire `alpaca order replace --order-id ... --stop-price-loss ...`
into the position-recs Sunday flow.

**Risk:** medium (live-account impact); requires careful staging.

### 9. Watchlist-as-a-service

**What we have:** `output/portfolio.json` is operator-edited manually
(per the file's own _comment field).

**Replace with:** `alpaca watchlist create/get/add/remove`. Dashboard
buttons hit the broker directly. Eliminates the manual JSON-edit
pattern.

**Risk:** low.

---

## Agent infrastructure patterns to adopt (Tier 3)

### 10. SKILL.md template structure

Their `.agents/skills/alpaca-cli/SKILL.md` defines the canonical skill
shape (`name / description / triggers / instructions / examples`). Our
existing skills under `src/plugins/fundjohn/skills/` and prompts under
`src/agent/prompts/subagents/` are inconsistent. Align to their template
so our skills are auto-discoverable by Claude Code and other agent
harnesses.

### 11. OAS-driven flag generation

Their CLI auto-generates flags + validation + completion from
`api/specs/*.json` (OpenAPI). Our `alpaca_executor.py` body-building is
hand-coded. If we ever expose more order types (limit, OCO, OTOCO,
trailing-stop), generating the order-body schema from Alpaca's OAS spec
is the right path — or just shell out to the CLI which already does it.

### 12. `--dry-run` everywhere

Their `alpaca order submit --dry-run` previews-without-network. Our
`alpaca_executor.py --dry-run` exists but the orchestrator's
`PIPELINE_ALPACA_DRY_RUN=1` env wraps it awkwardly. Adopt
`--dry-run` as a first-class flag on every script that touches
external systems (`trade_agent_llm.py`, `auto_backtest.py`,
`run_collector_once.js`).

### 13. Exit-code discipline

Their convention: `0=success / 1=API error / 2=auth error`. Adopt for
our scripts so the orchestrator can differentiate "credentials revoked"
from "broker rejected order" without parsing stderr.

### 14. `alpaca doctor` health check

Their connectivity/credentials self-check. Build `python3 src/execution/alpaca_doctor.py`
that:
- Validates `.env` has all four `ALPACA_*` vars
- Hits `/v2/account` (or runs `alpaca account get`) and reports HTTP
- Verifies `data/master/` is writable by the running user
- Reports against `data_coverage` for staleness

Wire it into the maintenance-report cron so we get an early warning
before a daily cycle blows up on auth or perms.

---

## Explicitly NOT recommended

- **Don't replace our backtester or strategy engine.** The CLI is broker
  + data-API surface only. Our deterministic sizer + per-strategy
  backtest loop stays.
- **Don't move to Go.** Adopting the CLI as a subprocess is fine;
  rewriting our pipeline in Go would shred months of working
  Python/JS infrastructure.
- **Don't replace yfinance/Polygon for daily prices.** Alpaca's data
  is excellent for new-day-forward but the 10-year history we have in
  `prices.parquet` predates the Alpaca account. Use Alpaca for *new*
  data going forward; keep yfinance/Polygon as the historical anchor.

---

## Suggested phasing (planning only)

| Phase | Scope | Risk |
|---|---|---|
| 1 | Install CLI on VPS. Wire `alpaca clock` + `account get` + `account portfolio` into dashboard. Pure read. | Low |
| 2 | Rewrite `alpaca_executor.py` order-submit body to shell out to `alpaca order submit`. Add `alpaca account activity list-by-type` reconciliation as a new step. | Medium |
| 3 | Opt into `alpaca data option chain` (broker greeks) and `alpaca data corporate-actions`. Augment parquet, don't replace. | Medium |
| 4 | Adopt skill template, exit-code convention, `alpaca doctor` health-check. | Low |
| 5 | (Maybe) `order replace` for live stop-adjustment via MastermindJohn position-recs. | High — live-account |

## Verification before each phase

1. `alpaca doctor` passes against the production paper account.
2. `alpaca order submit --dry-run` against a known signal returns the
   identical order body our `alpaca_executor` would have built.
3. Orchestrator's exit-code mapping covers `0/1/2`.
4. Tests under `tests/test_alpaca_*.py` mock the subprocess invocation
   so we don't burn paper trades on test runs.
