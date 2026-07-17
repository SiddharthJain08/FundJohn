# Dashboard heatmap polish + Sharpe-decomposition alpha bars + Playwright workaround

**Date:** 2026-05-14
**Status:** approved (math + tile-C w/ pct-return + Playwright workaround all green)
**Author:** BotJohn (Claude Code)
**Target file:** `src/channels/api/server.js` (johnbot embedded dashboard, port 3000 → nginx :80)

## Problem statement

After the heatmap + alpha-bars ship, three issues remain:

1. **Colors and stats are wrong.** Sharpe-ratio bars (`strategy_sharpe / portfolio_sharpe`) don't have a meaningful sum — they're a stack of standalone ratios that don't decompose anything. Heatmap "Size %" shows raw notional exposure (e.g. WBD = 385%) because the book is on 4× margin, so the values don't sum to 100% of the portfolio.
2. **Tile content is uncentered** and uses three left-justified text rows that read like a stat block. The operator wanted figures centered in each box for visual calm at scale.
3. **Visual verification is gapped.** The MCP Playwright tool insists on `/opt/google/chrome/chrome` and won't fall back to the chromium-headless-shell that's already installed. Every UI change ships blind to me.

## What this design changes

### A. New alpha math: per-strategy contribution to ticker Sharpe

For each ticker, define a daily PnL series from closed trades:

```
combined_t[d] = Σ_s   pnl_pct of strategy s on ticker t closed on day d
strat_t,s[d]  =        pnl_pct of strategy s on ticker t closed on day d
```

Then:

```
ticker_live_sharpe(t) = mean_d(combined_t[d]) / std_d(combined_t[d])
alpha(t, s)           = mean_d(strat_t,s[d])  / std_d(combined_t[d])     ← strategy's contribution
```

**Decomposition invariant** (proved on WDC, verified live):

```
Σ_s alpha(t, s) = mean(Σ_s strat_t,s) / std(combined_t)
                = mean(combined_t) / std(combined_t)
                = ticker_live_sharpe(t)
```

The shared `std(combined_t)` denominator is what makes the strategies' means add linearly. This is the **marginal-mean Sharpe decomposition**.

**Properties:**
- Profitable ticker (`ticker_sharpe > 0`) → sum of alphas is positive.
- A single strategy's alpha *can* exceed the ticker Sharpe — happens whenever other strategies are dragging.
- Negative alphas (drag) subtract from the sum, they don't arithmetically cancel.
- Tickers with `< 2` distinct trading days cannot produce a std → alpha bars show "—" + a fallback message ("`{ticker}` has insufficient closed trades for live Sharpe — need ≥ 2 distinct closed-trade days").

**Coverage:** 76 tickers (21 %) have ≥ 5 trading days, 211 (57 %) have ≥ 3, 295 (80 %) have ≥ 2. Below the threshold, the bar chart falls back gracefully.

### B. Heatmap Size % normalized to 100 %

Replace raw `total_size_pct` on tile (currently 385 % for WBD) with:

```
display_size(t) = total_size_pct(t) / Σ_t total_size_pct(t)    × 100
```

All visible tiles sum to 100 % of total book exposure. The raw notional remains available on hover via `title=`.

### C. Tile design — option C (centered hero + dark footer strip), modified

Each tile is divided into:

- **Hero zone** (top, centered): ticker symbol on row 1, live unrealized pct return + actual dollar P&L on row 2 (pct on left, dollars on right, same line, centered as a pair).
  - Ticker: 14 px bold, letter-spacing 0.08 em, line-height 1.1.
  - Pct return: 13 px bold, tabular-nums, signed with `%` suffix (`+0.84%` / `-0.32%`). Source: size-weighted `net_pnl` from open-position aggregation (existing field, multiplied by 100).
  - Dollar P&L: 12 px bold, tabular-nums, signed with `$` prefix and compact-k notation (`+$234`, `-$1.2k`, `+$12.4k`). Source: `ticker_contrib_pct × account.equity` — i.e. `Σ(unrealized_pnl_pct × position_size_pct) × NAV` across the ticker's open signals. NAV comes from the existing `/api/portfolio/account` payload (already in `loadPortfolio`'s fetch set, threaded into `renderPositions` as a new arg).
  - Both values share the same green/red color as the tile background gradient — visually anchored to the same signal.
  - The user explicitly chose live pct return for this slot, *not* Sharpe — the tile reports current book state, the Sharpe lives in the expanded alpha-bars view.
- **Footer strip** (bottom, dark): size % (left) · days (right), 9 px, letter-spacing 0.04 em, `background: rgba(0,0,0,0.32)`, top border 1 px subtle.

Background color: red→neutral→green gradient on the **live unrealized pct return**, anchored at ± 5 % for full saturation. Selected tile gets a 2 px blue border + box-shadow halo. Hover = transform translateY(-1px).

### D. Alpha-bars header confirms the invariant

When the user clicks a tile, the alpha-bars panel now reads:

```
TICKER · 12 strategies · alpha sum = +0.84 (= live Sharpe)
```

Where the "= live Sharpe" badge is rendered *only when* the runtime sum matches `ticker_live_sharpe` to ≤ 1e-6 — the visible proof that the math is honest.

Bars themselves: one horizontal bar per strategy, sorted by signed alpha desc, centered on a zero-line, green right / red left, length proportional to `|alpha| / max(|alpha|)`. Value column shows the signed alpha to 3 decimals (e.g. `+0.263`).

### E. Playwright workaround

Add `tools/page-shot.js`:

```
node tools/page-shot.js \
     --url http://localhost:3000/ \
     --output /tmp/heatmap-after.png \
     [--viewport 1440x900] \
     [--click "selector"] \
     [--wait-ms 500]
```

- Uses `puppeteer-core` (no bundled Chrome) launched with
  `executablePath: '/root/.cache/ms-playwright/chromium_headless_shell-1223/chrome-linux/headless_shell'`
  and `args: ['--no-sandbox', '--disable-dev-shm-usage']`.
- Writes a PNG to `--output`.
- The PNG is then `Read` by Claude (the Read tool handles PNGs natively and shows them as image content).
- Install: `cd /root/openclaw && npm install --no-save puppeteer-core`.

This is *not* committed to package.json (the dashboard doesn't depend on it). It lives at `/root/openclaw/tools/` as developer tooling — same shelf as `tools/verify_*.js` files that already live there.

## API changes

| endpoint | change |
|---|---|
| `GET /api/portfolio/positions` | unchanged from the prior fix (uncapped). |
| `GET /api/portfolio/history` | unchanged (uncapped). |
| `GET /api/portfolio/strategy-sharpe` | **removed** — superseded by per-ticker endpoint. |
| `GET /api/portfolio/ticker-alpha/:ticker` | **new**. Returns `{ ticker, live_sharpe, days, strategies: [{strategy_id, alpha, dir, n_trades}] }`. 5-min in-memory LRU cache. |

The new endpoint is on-demand (called only when the user expands a tile or table row). For 370 tickers × ~12 strategies each, the SQL is a single grouped query bounded to the ticker, returns < 1 KB per call.

## Frontend changes

| element | change |
|---|---|
| `_buildHeatmapHtml` | tile content uses option C layout (`.pf-tile-hero` + `.pf-tile-strip`); size % normalized; color from `_pnlColor(live_pct_return)` unchanged. |
| `_buildAlphaBarsHtml` | new signature `(group, alphaData)` where `alphaData` is the response from `/api/portfolio/ticker-alpha/:ticker`. Bars show `alpha(t, s)`, sorted desc. Header shows `ticker · N strategies · alpha sum = X.XX (= live Sharpe)`. |
| `renderPositions` / `renderHistory` | new signature accepts `(rows, accountEquity)` so the renderer can compute tile dollar P&L. On tile/row click, fetch `/api/portfolio/ticker-alpha/:ticker` lazily, cache in module-level `_alphaCache[ticker]`, then re-render. |
| `loadPortfolio` | drop the `/api/portfolio/strategy-sharpe` fetch; pass `account.equity` into `renderPositions` and `renderHistory`. |

CSS additions: `.pf-tile-hero`, `.pf-tile-strip`. CSS rule for the "= live Sharpe" badge.

## Non-goals

- **Open-position contribution to live Sharpe.** Sharpe requires a return *distribution*, which requires multiple closed trades. Live unrealized PnL is not a return distribution. Tile colors come from unrealized PnL (the current view), alpha bars come from realized Sharpe (the history view). They are distinct metrics; the design does not try to fuse them.
- **Annualization.** `√252` cancels in ratios and adds no signal in single-ticker decomposition. We display raw daily-grain Sharpe; "= live Sharpe" is honest about its denominator.
- **Treemap-quality aspect ratios.** Slice-and-dice is fine for top-12; squarified is held until/unless the operator says aspect ratios bother them.

## Risk + rollback

- The new `ticker-alpha` endpoint hits `signal_performance` joined with `execution_signals` on each call. With 5-min cache + ticker-bounded queries, this is < 50 ms per call against the current data volume.
- If a ticker has insufficient closed-trade days, the endpoint returns `live_sharpe: null` and a sentinel reason — the client renders a fallback message instead of bars.
- Rollback path: revert the four edits in `server.js`; no DB migrations, no schema changes.

## Verification plan

1. **Math invariant:** for every ticker with `>= 2` distinct closed-trade days, `|Σ alphas - ticker_live_sharpe| < 1e-6`. Tested on WDC (live: +0.8426 matches exactly).
2. **Size normalization:** `Σ tile.display_size = 100.0 ± 0.01` across all visible tiles.
3. **Tile centering:** Playwright screenshot inspected by `Read` — visual confirmation that text is centered in tiles and the bottom strip pins to the footer.
4. **Click expansion:** Playwright clicks a tile, screenshots the alpha-bars panel, confirms the "= live Sharpe" badge appears and the leftmost bar is the biggest contributor.
5. **Negative-alpha rendering:** Screenshot of a ticker with `S5_max_pain` shows that bar on the negative (left) side of the zero-line in red.
6. **Service smoke:** `python3 -m system_checks` after restart — all 21 checks should pass.
