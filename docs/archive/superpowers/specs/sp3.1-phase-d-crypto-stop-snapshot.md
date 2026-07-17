# SP-3.1 Phase D — Crypto Stop CLI Grounding Snapshot

**Captured:** 2026-05-26 ~13:38 UTC, live Alpaca **paper** account `PAXXXXXXXXXX` (`paper-api.alpaca.markets`, CLI v0.0.9). Real ~$15 paper BTC order + stop probes; position flattened afterward.

This file is the empirical ground truth for Phase D Task 6 (`_submit_crypto_stop`). Where it differs from the plan's assumptions, **this file wins.**

---

## Decisions (what the code must use)

| Param | Value | Evidence |
|---|---|---|
| Accepted stop order `--type` | **`stop_limit`** ONLY | plain `--type stop` → 422 `invalid order type for crypto order` (code 40010001). `stop_limit` accepted (status `new`). |
| Required flags | `--type stop_limit --stop-price <s> --limit-price <l> --side sell --time-in-force gtc` | `stop_limit` accepted with all four; `gtc` valid (same as entry). |
| limit vs stop price (sell stop) | limit **below** stop (probe used stop 74366 / limit 73983, ~0.5% below) | accepted; for a marketable protective exit place the limit a small % below the stop. |
| Order-id key | **`id`** | `d06f87e7-...` (also `client_order_id`). |
| Resting status | **`new`** | the stop sits resting until triggered. `stop_price`/`limit_price` echoed as **strings**. |
| Cancel syntax | `order cancel --order-id <id>` | positional id → error `--order-id required`. |

## Critical gotchas (drive the implementation shape)

1. **Entry fills async + short.** A crypto market buy returns `status: pending_new` (not filled) at submit, and fills a few seconds later for a qty **slightly less than requested** (fees): requested `0.0002` → filled `0.000199499`. A sell stop for the **requested** qty is rejected **403 `insufficient balance for BTC (requested: 0.0002, available: 0.000199499)`**.
   - **Implication:** `_submit_crypto_stop` must size the stop to the **actual filled qty**, not the requested qty. The entry path must **poll the entry order to a filled state** (read `filled_qty`) before placing the stop. If the fill hasn't landed within a bounded wait, skip the stop (fail-safe) — the next daily/regime cycle re-evaluates.

2. **A resting sell stop reserves the position.** `position close BTC/USD` returns **403** while an open sell `stop_limit` holds the qty. Any close/flatten path must **cancel the resting stop first**, then close. (Relevant to the close path + operator flattens, not the entry path.)

3. **`stop_limit` does pass type-validation immediately** (the 403 in probe A was purely the qty/balance issue, not the type) — confirming the type is supported; only the qty was wrong.

---

## Raw evidence

### 1. Entry market buy (`--qty 0.0002 --type market --tif gtc`)
```json
{ "asset_class":"crypto","id":"3942dfd7-d0ee-429a-a5ac-782b6665b024",
  "filled_qty":"0","notional":null,"order_type":"market","position_intent":"buy_to_open",
  "qty":"0.0002","side":"buy","status":"pending_new","symbol":"BTC/USD",
  "time_in_force":"gtc","type":"market" }
```
Filled a few seconds later → position qty `0.000199499` (< 0.0002).

### 2. Probe B — plain `stop` → REJECTED
```json
{"code":40010001,"error":"invalid order type for crypto order","status":422}
```

### 3. Probe A — `stop_limit` for the full requested qty → REJECTED (balance, not type)
```json
{"code":40310000,"error":"insufficient balance for BTC (requested: 0.0002, available: 0.000199499)","status":403}
```

### 4. Probe A2 — `stop_limit` for a valid qty (`0.0001`) → ACCEPTED
```json
{ "asset_class":"crypto","id":"d06f87e7-079c-42eb-94f7-084826eb7efc",
  "limit_price":"73983","notional":null,"order_type":"stop_limit",
  "position_intent":"sell_to_close","qty":"0.0001","side":"sell","status":"new",
  "stop_price":"74366","symbol":"BTC/USD","time_in_force":"gtc","type":"stop_limit" }
```

### 5. Cleanup
`order cancel --order-id d06f87e7-...` → `{}` (success). Then `position close --symbol-or-asset-id BTC/USD` → close order `4516e335-...` qty `0.000199499` → position flat (`[]`).
