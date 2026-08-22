# Alpaca CLI — integration reference

**Status:** LIVE reference (rewritten 2026-08-22 after a full audit of
https://github.com/alpacahq/cli against every call site in this tree).
The original 2026-04-28 planning document this file replaced is summarised in
§8; its Tier-1/Tier-2 items are all shipped.

The CLI (`github.com/alpacahq/cli`, Apache-2.0, **alpha preview** — "commands,
flags, and output formats may change without notice") is the broker + market
data surface for everything in this repo. There is no hand-rolled HTTP left
on the order path; the residual direct-HTTP sites are listed in §7.

---

## 1. Binary, version, upgrade

| | |
|---|---|
| Binary | `/root/go/bin/alpaca` — **not on PATH**; every caller uses the absolute path (`ALPACA_CLI_BIN` / `ALPACA_BIN` env override). |
| Installed | **v0.0.9** (2026-04-28 `go install`). |
| Upstream | **v0.0.13** (2026-07-22 OAS regen; 2026-08-13 docs). 12 commits since v0.0.9. |
| Built + verified | `v0.0.13` built from source on this box; byte-identical stdout on `clock / account get / calendar / position list / order list --nested / asset get / data bars / data multi-snapshots / data news`; `order submit --help` and `order list --help` identical. |
| Upgrade | `cp -p /root/go/bin/alpaca /root/go/bin/alpaca-v0.0.9 && go install github.com/alpacahq/cli/cmd/alpaca@v0.0.13` (or `alpaca update --yes`). Binary is pass-through JSON (`json.RawMessage`) so response shapes are the API's, not the CLI's. |
| Check | `alpaca update --check --quiet` → `{"current","latest","update_available","update_command"}`. Nothing in the tree checks this; `doctor.py` only checks exists+executable. |

### What changed v0.0.9 → v0.0.13 (relevant to us)

- **stderr error JSON is now pretty-printed across lines** (`11f2df3`). Every
  wrapper in this tree parses stderr as a whole document (`json.loads(proc.stderr)` /
  `JSON.parse(stderr)`) so this is safe — never regex / line-split CLI stderr.
- Removed (spec regen): `crypto-perp *`, `asset bond`, `asset treasury`,
  `account config set --dtbp-check/--pdt-check`. **None used here.**
- Added: `locate *` (short-locate — fees!), `data index values|latest-values`,
  `data fixed-income-quotes`, `order replace --notional`,
  `data corporate-actions --region` (default `us`), `wallet --chain`.
- Standardised User-Agent; `spec-check` dev command; readme.
- Unchanged for every command we call: `order submit`, `order list`,
  `order get`, `position *`, `data bars|multi-bars|multi-snapshots|news|option chain`,
  `calendar`, `clock`, `account *`, `asset *`, `option contracts`, `watchlist *`.

---

## 2. Configuration — what the CLI actually reads

The CLI reads **exactly** these env vars (grep of `Getenv` in v0.0.13):

| Var | Effect | Our `.env` |
|---|---|---|
| `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` | Credential bundle (both or neither). Env bundle beats any profile. | set |
| `ALPACA_LIVE_TRADE` | `true` → `https://api.alpaca.markets`; anything else → paper. | `false` (explicit) |
| `ALPACA_PROFILE`, `ALPACA_CONFIG_DIR` | Profile selection (`~/.config/alpaca`). | unset; `/root/.config/alpaca/profiles/` is empty |
| `ALPACA_OUTPUT` | `json` (default) or `csv`. | unset |
| `ALPACA_QUIET`, `ALPACA_VERBOSE`, `ALPACA_DEBUG`, `ALPACA_TRACE` | Diagnostics (stderr). | unset — wrappers pass `--quiet` per call |

**Not read by the CLI:** `ALPACA_BASE_URL`, `ALPACA_DATA_TIER`, `APCA_*`. Those
serve the direct-HTTP remnants (§7) only. Paper vs live for the CLI is decided
solely by `ALPACA_LIVE_TRADE`.

Data API base is hard-wired to `https://data.alpaca.markets`. Account data tier
is **Algo Trader Plus** (`ALPACA_DATA_TIER`): SIP feed, 10k data req/min.
Trading API limit is ~200 req/min/account.

Credential lookup order: env bundle → profile `access_token` → profile
`api_key`+`secret_key`. Never `source /root/openclaw/.env` from bash (unquoted
parentheses); python-dotenv / Node dotenv parse it correctly.

---

## 3. Output contract (load-bearing)

- **JSON on stdout is the default. There is NO `--json` flag.**
- Errors: a JSON document on **stderr** — `{"error","code","status","hint","method","path","request_id"}`
  (multi-line from v0.0.10). `status` is the HTTP code; `code` the Alpaca
  error code (e.g. `40410000` not found, `42210000` unprocessable).
- Exit codes: `0` success · `1` API/general error · `2` **auth error — never retry**.
- Operational commands (`version doctor profile update completion --help`)
  print text; only `update --check` is JSON.
- Global flags: `--quiet/-q` (suppress hints — **without it the CLI prints
  "Rate limited, retrying in …" lines on stderr ahead of the error JSON**),
  `--timeout <sec>` (HTTP timeout, default 30), `--jq <expr>` (built-in jq,
  no external binary), `--csv`, `--schema` (response fields, no API call),
  `--verbose/--debug/--trace` (stderr diagnostics, credentials scrubbed).
- Built-in retry: 429 and 500/502/503/504, **3 attempts**, backoff
  0.5 s·2ⁿ, `Retry-After` honoured. Application-level retry loops on top of
  this (stop_reattach 3×, alpaca_news 3×) therefore mean up to 12 HTTP attempts
  per logical call — fine for 429, but budget the subprocess timeout for it.
- Per-invocation cost measured on this VPS: **~65 ms** (`clock`): 6 ms process
  start + DNS 1 / TCP 11 / TLS 30 / TTFB 15 ms. No connection reuse between
  invocations → **batch with the multi-symbol endpoints** (§5).

---

## 4. Wrappers in this tree (all carry the same contract since 2026-08-22)

Every wrapper appends `--quiet` and `--timeout <subprocess_timeout − 1>` (unless the
caller passed them) and returns `auth_error` for exit code 2.

| Wrapper | File | Returns |
|---|---|---|
| `runAlpaca(args,{timeout,env})` | `src/channels/api/alpaca_cli.js` | `{ok, exit_code, auth_error, stdout, stderr, payload, error}` |
| `_run_alpaca_cli(args, timeout)` | `src/execution/alpaca_executor.py` | `(ok, payload, err{exit_code, auth_error, status, code, error, error_json, raw_stderr})` |
| `_run_cli` | `src/execution/regime_liquidator.py`, `alpaca_replace_stop.py`, `stop_reattach.py` (3× 2/5/10 s retry on timeout/429) | same shape |
| `alpaca_multibars.fetch_multi_bars` (Python batch) | `src/pipeline/alpaca_multibars.py` — backfiller + 30m ingest | `{api_symbol: [bars]}`; raises `MultiBarsError(status, auth_error)`; callers pre-filter with `partition_symbols` |
| `tools.alpaca` (sub-agents) | generated by `src/agent/tools/mcp/alpaca.js` → `workspaces/default/tools/alpaca.py` | typed **read-only** functions, raises `AlpacaCLIError` / `AlpacaAuthError` |

Other direct `subprocess.run([ALPACA_CLI, …])` callers (doctor, reconcile,
engine, premarket_helpers, ingestion, backfillers, scripts) do not yet pass
`--quiet`; they parse stdout only and treat any non-zero rc as failure, which
is correct but blind to rc=2.

---

## 5. Command cheat-sheet — the forms this system relies on

```bash
# Broker state
alpaca account get --quiet
alpaca position list --quiet                                  # qty_available = qty − OCO-reserved
alpaca order list --status open --nested --limit 500 --direction asc [--after-order-id <id>] --quiet
alpaca order list --status open --symbols AAPL,MSFT --nested --limit 500 --quiet
alpaca order get --order-id <uuid> --quiet                    # POSITIONAL id is REJECTED (rc=1 "--order-id required")
alpaca order get-by-client-id --client-order-id <coid> --quiet
alpaca clock --quiet · alpaca calendar --start D --end D --quiet
alpaca account activity list --activity-types FILL --date D --page-size 100 [--page-token <last id>]
alpaca account portfolio --period 1M --timeframe 1D [--intraday-reporting continuous|market_hours]

# Orders (execution paths only — always --client-order-id)
alpaca order submit --symbol X --side buy --qty 10 --type market --time-in-force day \
   --order-class bracket --take-profit '{"limit_price":"…"}' --stop-loss '{"stop_price":"…"}' \
   --client-order-id <coid> --quiet [--dry-run]
alpaca order submit --symbol X --side sell --qty 10 --type limit --limit-price P --extended-hours --client-order-id …
alpaca order submit --order-class mleg --legs '<json>' --qty 1 --type limit --limit-price P --time-in-force day …
alpaca order replace --order-id <leg> --stop-price P · alpaca order cancel --order-id <id>
alpaca position close --symbol-or-asset-id X [--qty N | --percentage P]

# Market data (batch!)
alpaca data multi-bars --symbols A,B,C --start D --end D --timeframe 1Day --adjustment raw|split --feed sip --limit 10000 [--page-token]
alpaca data bars --symbol A …                                 # single-symbol form (collector, universe_prices)
alpaca data multi-snapshots --symbols <≤200> · alpaca data latest-trades --symbols …
alpaca data news --symbols A,B --start D --limit 50 --sort desc --exclude-contentless [--page-token]
alpaca data option chain --underlying-symbol X --expiration-date-gte D --expiration-date-lte D --type call --limit 1000 [--page-token]
alpaca option contracts --underlying-symbols X --status active --limit 10000 [--page-token]
alpaca data corporate-actions --symbols … --start D --end D --types forward_split,… --limit 1000 --sort asc [--page-token]
alpaca data screener movers --top 10 · alpaca data screener most-actives --by volume --top 10
alpaca data crypto bars --symbols BTC/USD --timeframe 1Hour … · alpaca data crypto snapshots --symbols BTC/USD
```

### Pagination limits (from the bundled OpenAPI specs)

| Endpoint | `--limit` default / max | Cursor |
|---|---|---|
| `order list` | **50 / 500** | keyset: `--direction asc --after-order-id <last id>` until a short page |
| `data bars` / `multi-bars` / `crypto bars` | 1000 / **10000** (data points across all symbols) | `next_page_token` |
| `data news` | – / **50** | `next_page_token` |
| `data option chain` | 100 / **1000** | `next_page_token` |
| `data corporate-actions` | 100 / **1000** | `next_page_token` |
| `option contracts` | 100 / **10000** | `next_page_token` |
| `account activity list` | `--page-size` 100 | `--page-token` = **id of the last row** (by design) |
| `position list`, `*-snapshots`, `latest-*` | unpaginated | chunk symbols (≤ ~200 per call) |

---

## 6. Gotchas that have cost us (each has a regression test)

1. **`order get` needs `--order-id`.** Positional form → rc=1
   `"--order-id required"`. `alpaca_reconcile` was fixed 2026-06-19; three
   sibling sites in `alpaca_executor.py` (`_poll_crypto_fill` ×2,
   `_wait_for_fill`) stayed positional until 2026-08-22 — every
   direction-flip's matched open was skipped and no crypto protective stop
   was ever attached. `tests/execution/test_alpaca_cli_contract.py`.
2. **`order list` hides OCO stop legs without `--nested`** (stop lives in
   `legs[]`, `status:'held'`; `legs` key absent entirely without the flag) and
   **returns 50 rows by default** (cap 500). Unbounded reads in
   `system_checks/checks/broker.py` and `afterhours_tp.py` were fixed
   2026-08-22; `stop_reattach._fetch_open_orders` is the reference
   implementation. Exclude `pending_cancel`, keep `held`.
3. **Count CLI JSON with `--jq length`, never `wc -l`** (pretty-printed).
4. **`--quiet` or stderr is not pure JSON** (rate-limit chatter precedes the
   error document) → `error.status` silently null.
5. **rc=2 is auth** — stop the wave, don't retry 200 orders into a revoked key.
   Wrappers now surface `auth_error`; callers still owe an early-abort.
6. **`--client-order-id` on every unattended submit**; recover with
   `order get-by-client-id` after an ambiguous failure (404 ⇒ safe to resubmit).
   `regime_liquidator._submit_extended_hours_close` was the last submit
   without one (fixed 2026-08-22).
7. **`ALPACA_BASE_URL` does nothing for the CLI** — only `ALPACA_LIVE_TRADE=true`
   routes live.
8. **Alpaca paper accepts OCO exits on shorts** but rejects bracket-on-short at
   entry; reattach covers it (~15 min gap). OPG/MOO unreliable on paper.
9. **`alpaca locate create` incurs fees** and `position close-all` /
   `order cancel-all` have no confirmation — never expose them to agents
   (`tools.alpaca` is read-only by construction).

---

## 7. Residual direct-HTTP sites (not on the CLI)

| Site | Why it stays / owed |
|---|---|
| `src/execution/alpaca_trader.py` (`_alpaca_session`, `/v2/account`, positions) | Consumed by sizer/handoff/circuit-breaker; CLI equivalent is `account get`/`position list`. Migration owed (low priority — read-only). |
| `src/execution/alpaca_executor.py:2588` `data.alpaca.markets/v2/stocks/{T}/snapshot` | Bracket base-price pre-flight; `data snapshot --symbol` is the CLI form. |
| `src/execution/stop_reattach.py:_alpaca_rest_post` (OCO re-attach POST) | Own 2/5/10 s 429 backoff. CLI form: `order submit --order-class oco …`. |
| `src/ingestion/ingest_prices_30m_alpaca.py`, `quote_sources/alpaca.py` | Multi-symbol `/v2/stocks/bars` and snapshot — already batched; fine. |

---

## 8. Optimisation backlog (measured, not yet done)

1. ~~Daily price fill one process per ticker~~ **DONE 2026-08-22** —
   `collector.fillPricesAlpacaBatch` (multi-bars, grouped by gap range,
   chunked ≤200 / ≤8000 points, page-token loop, per-chunk fallback to the
   per-ticker path). Measured 2.9 ms/ticker vs 80.6 ms/ticker. Kill switch
   `OPENCLAW_PRICES_MULTI_BARS=0`. Same day: `backfillers/universe_prices.py`
   (`fetch_many_ticker_year`, year-major driver), `ingestion/ingest_prices_30m_alpaca.py`
   (`fetch_many`, 30Min) and `close_proxy_snapshot._CHUNK` 50→150 followed, all on
   the shared `src/pipeline/alpaca_multibars.py` helper. Nothing price-related is
   per-ticker any more.
2. **Executor early-abort on `auth_error`** in the main submission loop.
3. **`doctor.py` `_run_alpaca_cli`** — hard-coded 10 s, no stderr parse, no `--quiet`.
4. **Options chain** default `--limit 100` in `alpaca_options.js` /
   `backfillers/alpaca_options.py` → 1000 (10× fewer pages).
5. **Double retry** (stop_reattach / alpaca_news on top of the CLI's 3×) —
   acceptable, but subprocess timeouts must cover 4×3 attempts on a 5xx burst.
6. Migrate §7 read-only direct-HTTP sites to the wrappers so one auth/retry
   path remains.

---

## 9. Audit trail

- 2026-04-28 — plan written (Tier 1–3). Tier 1 (executor submit, dashboard
  portfolio, fill reconciliation, clock) and Tier 2 (option chain greeks,
  screener, corporate actions, `order replace`, watchlists) all shipped
  between May and July; `doctor.py` + `system_checks` cover Tier 3 §14.
- 2026-06-19 — `order get --order-id` fix in reconcile (LRN-20260619-001).
- 2026-07-15 — `--nested` + `--limit 500` lesson (dashboard "3 stops vs 9").
- 2026-08-12/18 — 50-row default truncation incident → keyset pagination in
  stop_reattach; `--symbols` filter on cancel-before-close.
- 2026-08-22 — this audit: repo v0.0.9→v0.0.13 diff, full call-site
  inventory, wrapper contract (`--quiet`/`--timeout`/`auth_error`), three
  positional `order get` bugs, two unbounded `order list`, one missing
  `--client-order-id`, `tools.alpaca` read-only module for sub-agents.
