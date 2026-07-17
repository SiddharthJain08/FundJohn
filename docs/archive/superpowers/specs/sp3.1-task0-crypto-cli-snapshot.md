# SP-3.1 Phase A — Task 0 Crypto CLI Grounding Snapshot

**Captured:** 2026-05-26 ~07:13 UTC, live Alpaca **paper** account `PAXXXXXXXXXX` (`paper-api.alpaca.markets`, CLI v0.0.9, active profile `paper`). Real ~$15 paper orders, position flattened afterward.

This file is the empirical ground truth for SP-3.1 Phase A plan Tasks 3/4/6. Where it differs from the plan's assumptions, **this file wins.**

---

## Decisions (what the code must use)

| Param | Value | Evidence |
|---|---|---|
| `_CRYPTO_TIF` | **`gtc`** | `day` → HTTP 422 `invalid crypto time_in_force`; `gtc` and `ioc` both accepted. |
| `_CRYPTO_QTY_DECIMALS` | **`9`** (safe) | qty `0.000000001` parsed without precision error; close returned `0.00009975` (8 dp). |
| Price JSON path | **`.trades["BTC/USD"].p`** (float) | confirmed below. |
| Submit-response order-id key | **`id`** | confirmed below (`client_order_id` also present). |
| `position close` crypto symbol | **accepts `BTC/USD`** (CLI maps to `BTCUSD` in the REST path) | full + partial both worked on a real position. |
| **Minimum order size** | **$10 cost basis** (NOT $5) | `cost basis must be >= minimal amount of order 10` (code 40310000, 403). |

**Plan corrections:**
- The spec/plan "one live $5 paper BTC order" exit criterion is wrong — Alpaca's crypto minimum is **$10 notional**. Task 6 smoke must size **≥ $10** (use ~$15–20). Sub-$10 crypto orders are rejected with 403; the open path returns a `rejected` result dict (fail-safe, no crash) — acceptable, but the smoke must use a valid size.
- `notional` is **`null`** in the order-submit response when the order is placed by `--qty` (not `--notional`). The close-path code's `(...).get('notional') or notional_oc` fallback is therefore load-bearing and correct. The open path does not read response-notional (uses its own computed value), so it is unaffected.

All of T3/T4's other plan assumptions (`gtc`, `9` decimals, `.trades[sym].p`, `id`) are **confirmed correct — no routing-code change needed.**

---

## Raw evidence

### 1. `alpaca data crypto latest-trades --symbols BTC/USD`
```json
{ "trades": { "BTC/USD": { "i": 9043453504574080850, "p": 76612.949,
  "s": 0.005213505, "t": "2026-05-26T07:11:39.541510883Z", "tks": "S" } } }
```
Price access: `payload["trades"]["BTC/USD"]["p"]` → `76612.949` (float).

### 2. Minimum order size (sub-$10 rejected)
`order submit --symbol BTC/USD --qty 0.0001 --side buy --type market --time-in-force gtc`
(0.0001 × $76,612 ≈ $7.66, below the minimum):
```json
{"code":40310000,"error":"cost basis must be >= minimal amount of order 10",
 "status":403,"method":"POST","path":".../v2/orders"}
```
`--qty 0.000000001` → same 403. Minimum is **$10 cost basis**.

### 3. TIF probes (@ qty 0.0001, all hit the $10 floor first — but the TIF itself is what we read)
- `gtc` → 403 cost-basis (TIF valid; rejected only for size)
- `ioc` → 403 cost-basis (TIF valid; rejected only for size)
- `day` → **422 `invalid crypto time_in_force`** (TIF itself rejected)

### 4. Successful BUY (`--qty 0.0002` ≈ $15.3, `gtc`) — response shape
```json
{ "asset_class": "crypto", "asset_id": "276e2673-...", "client_order_id": "039eb67a-...",
  "created_at": "...", "filled_qty": "0", "id": "4df76fc4-25ed-4f7d-93c2-20c868fdf2b1",
  "notional": null, "order_type": "market", "position_intent": "buy_to_open",
  "qty": "0.0002", "side": "buy", "status": "pending_new", "symbol": "BTC/USD",
  "time_in_force": "gtc", "type": "market" }
```
Order id key = `id`. `qty` is a string. `notional` is `null` for qty-based orders. `status` is `pending_new` at submit (crypto fills async; it had filled by the close 2s later).

### 5. Close path on the real position (symbol accepted, partial + full)
Partial `position close --symbol-or-asset-id BTC/USD --percentage 50`:
```json
{ "id": "7cd4fde7-...", "side": "sell", "position_intent": "sell_to_close",
  "qty": "0.00009975", "notional": null, "symbol": "BTC/USD", "time_in_force": "gtc",
  "status": "pending_new", "asset_class": "crypto" }
```
Full `position close --symbol-or-asset-id BTC/USD`: same shape, sold the remaining `0.00009975`. Final `position list` → no BTC position (flat). On an empty position the close returns 404 `position not found: BTCUSD`.
