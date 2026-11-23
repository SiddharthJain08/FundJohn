# Fractional shares across the board — design (operator ruling 2026-11-23)

## 1. Intent and why
The equity executor rounds every order to whole shares and forces a minimum of one share
(`qty = max(1, int(notional/entry))`). On the $90k paper book 49 % of orders are 1-share orders
and 83/625 on 11-19 were larger than their sized target. The live book starts at ≈ $3,000, where
most sized targets are below one share: whole-share rounding would either drop most of the book
(round down) or over-size it several-fold (round up). Operator ruling: fractional shares across
the board; over-sizing is the larger harm.

## 2. Goals / non-goals / anti-goals
- Goals: every LONG order (open, add, reduce, close) sizes to its dollar target as a fractional
  quantity; shorts never exceed their target; no order is ever forced above target.
- Non-goals: options (contracts stay integers); crypto (already fractional, own path).
- Anti-goals: a fractional position left with no loss cutoff; silent skips (every drop logs a reason).

## 3. Broker constraints (Alpaca, verified 2026-11-23 — docs + paper probe §7)
- Fractional orders: long side only ("we do not support short sales in fractional orders").
- time_in_force must be `day`; GTC fractional orders are rejected (so no GTC protective stop can
  carry a fractional quantity).
- Order types: market, limit, stop, stop_limit. `position close --qty` accepts 9 decimals.
- Asset must be `fractionable` (7,872 / 13,546 tradable symbols; all 625 tickers submitted 11-19).
- Minimum notional ≈ $1 (the sizer's `DUST_FLOOR_USD = 1.0` already matches).
- Bracket/OCO + fractional: REJECTED — `fractional orders must have order_class=simple` (probe A). So a fractional
  position can carry at most ONE simple DAY protective order (a stop); no take-profit leg.

## 4. Approaches considered
A. **Fractional longs + whole-share-floor shorts (chosen).** Longs: exact fractional qty on every
   path; stop protection per §6. Shorts: `floor(notional/entry)` whole shares, a sub-share short is
   skipped as dust (logged `short_sub_share`), a partial reduce of a short that rounds to zero is
   skipped unless the target is a full exit. Never forces up.
B. Round-to-nearest shorts (0.5 sh → 1 sh). Rejected: on a $3k book one share of a $100 stock is
   3 % NAV — the over-sizing the operator wants gone.
C. Notional orders (`--notional`) for longs. Rejected: market-only, no limit price, and the
   executor's extended-hours path needs limit orders; fractional `--qty` covers every type.

## 5. What changes (file level)
- `src/execution/alpaca_executor.py`
  - open path: `qty = _size_qty(side, notional, entry, fractionable)`; longs → `round(notional/entry, 6)`
    (≥ `DUST_FLOOR_USD`), shorts → `floor`; log `x{qty:g} sh`; SKIP reasons `dust_below_1usd`,
    `short_sub_share`, `not_fractionable_sub_share` (long on a non-fractionable asset below 1 share).
  - close-only / extended-hours path (`qty_co = max(1, int(...))`): same helper.
  - partial reduce (`--percentage`): for longs use `--qty <decimal>` (exact dollars); for shorts round
    the reduce to whole shares, skip when 0 and not a full exit (kills the "N % of 1 shares would
    result in order size of zero" rejections: 80 on 11-19).
  - OPG / day-sweep closes: `int(_fetch_position_qty)` → float; filled-qty bookkeeping float.
  - bracket legs follow the probe result (§7).
- `src/execution/stop_reattach.py`: `str(int(qty))` → decimal string on the protective stop and the
  OCO body; TIF `day` when qty is fractional (GTC only for whole-share positions); whole-share
  positions unchanged (GTC). Fractional positions are re-armed every session by the existing
  reattach step (runs after every execute).
- `src/execution/trade_handoff_builder.py`: a LONG signal on a non-fractionable asset is tradable
  only if its target is ≥ 1 share — that is sizing-time knowledge, so the veto stays at the executor
  (`not_fractionable_sub_share`); the handoff only gains `fractionable` in the signal row for reporting.
- `src/execution/regime_liquidator.py`, `position_circuit_breaker.py`: full closes already use
  `position close` without qty — verify no int casts on the audit path.
- `alpaca_submissions.qty` is already numeric.

## 6. Loss protection for fractional longs
- Opens: fractional qty ⇒ `order_class='simple'`, `tif='day'` (the executor already drops bracket→simple for
  shorts; the same branch now fires for fractional longs, logged `↓ fractional → simple order_class`).
  Whole-share longs keep the market DAY bracket exactly as today.
- Reattach (runs after every execute): for a fractional position skip the OCO attempt and submit the
  simple protective STOP with `tif='day'` (fractional GTC is rejected); whole-share positions keep OCO/GTC.
  The take-profit leg is lost for fractional positions — acceptable: targets are re-trued by the sizer
  every cycle and the breakers cut losses.
- Intraday: `position_circuit_breaker` (5-min, per-position loss ≥ `position_circuit_breaker_pct` of NAV)
  and `account_breaker` are unchanged and cover fractional positions (they close by symbol).
- Overnight: no broker-side stop can exist on a fractional position (DAY expires at the close). Accepted
  by design; the next session's reattach re-arms within the first execute cycle. Runbook notes the gap.

## 7. Probe result (paper, 2026-11-23 21:01Z, operator-authorized; F ≈ $11, limits far from market, all cancelled)
- A fractional BRACKET limit day buy 0.5 → REJECTED 42210000 `fractional orders must have order_class=simple`
- B fractional SIMPLE limit day buy 0.5 → ACCEPTED (`qty "0.5"`, `time_in_force day`) → cancelled; 0 open F orders after
- C fractional SHORT simple limit day sell 0.5 → REJECTED 42210000 `fractional orders cannot be sold short`
- D fractional BRACKET gtc → REJECTED 42210000 `fractional orders must be DAY orders`

## 8. Left to the builder
- `_size_qty` decimals (6) and the exact log/skip strings.
- Test fixtures: current executor tests do not pin `--qty` strings.
- Rounding of `filled_qty`/notional in reconciliation (float everywhere; no int()).

## 9. As built (2026-11-23, branch fractional-shares, 9 commits)
Built as §5–§6 describe, with the probe in §7 deciding the order types. `_size_qty` in `alpaca_executor.py` returns `(qty, skip_reason)`: longs on fractionable assets are `round(notional/price, 6)`, shorts and non-fractionable assets floor to whole shares, and targets under `DUST_FLOOR_USD` are skipped. Skip reasons are `dust_below_1usd`, `short_sub_share`, `not_fractionable_sub_share` and `reduce_rounds_to_zero` (partial reduce only). `_is_fractionable` caches the `fractionable` column once per process and fails open. Partial reduces use a decimal `position close --qty` from `_reduce_qty` on the signed position (`_fetch_position_signed_qty`), falling back to `--percentage` when the lookup fails. Fractional opens drop the bracket to a simple DAY order, per probe B and A. The OPG close is skipped for fractional positions and falls back to the RTH day-sweep, because fractional orders must be DAY (probe D) and OPG is not a DAY order. `stop_reattach.py` (`_qty_str`, `_tif_for_qty`) gives fractional positions one simple DAY STOP with no OCO, after a coverage check and a cancel-before-re-arm; whole-share positions keep OCO/GTC. `afterhours_tp.py` keeps float qty.
Probe-driven decisions (§7): fractional brackets rejected (A), fractional simple DAY buys accepted (B), fractional shorts rejected (C), fractional GTC rejected (D). Together these set the protection model in §6: no take-profit leg and no overnight stop on fractional positions.
Known follow-ups: (1) whole-share positions still use OCO/GTC, so the two stop paths differ by design; (2) the coverage check compares quantity only, not stop price; (3) the `already_covered` stats key is created lazily, so it is absent from counters until the first hit; (4) a fractional DAY stop priced earlier in a session is not refreshed intraday; the next execute cycle's reattach re-prices it.
