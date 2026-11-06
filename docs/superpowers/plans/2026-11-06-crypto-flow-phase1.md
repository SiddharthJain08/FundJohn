# Crypto Engine Phase 1 — Flow Data Layer, Signal Layer, Crypto Dashboard Page: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a 24/7, keyless-first crypto flow data layer (whale transfers, exchange netflow, derivatives positioning, implied vol, stablecoin supply, sentiment) for every Alpaca-tradable pair, compute per-asset hourly features and a 0–100 whale-pressure score, and surface it all on a new "Crypto" dashboard page that also takes over the crypto regime panel.

**Architecture:** A Python package `src/ingestion/crypto_flow/` with one module per data provider behind a common `Provider.fetch()` contract, a pure-function `features.py`, and a `store.py` that writes two append-only master parquets plus three Postgres tables (latest snapshot, provider health, 7-day live event cache). A driver `scripts/run_crypto_flow_tick.py` runs every 15 minutes from the johnbot in-process scheduler (`_sched`, UTC) inside a MemoryMax-capped scope, gated by `OPENCLAW_CRYPTO_FLOWS=1`. A new Express router `src/channels/api/routes_crypto.js` reads only Postgres; the inline SPA in `src/channels/api/server.js` gains a `#crypto-page` right of Market.

**Tech Stack:** Python 3.13, pandas 3.0, pyarrow 23, duckdb 1.1 (via `parquet_store`), psycopg2, urllib via `_http_retry.fetch_with_retry`; Node 24, Express 4, Chart.js 4.4 (already loaded in the SPA); PostgreSQL; systemd-run scopes via `src/lib/capped_spawn.js`.

**Spec:** `docs/specs/2026-11-06-crypto-engine-whale-flow-design.md` (§0.2, §2, §3, §6, §7 row P1, §10). The executor reads the spec alongside this plan.

## Global Constraints

- **NEVER-DELETE**: `data/master/crypto_whale_events.parquet` and `data/master/crypto_flows_1h.parquet` are append-only masters; writes go through `parquet_store.append_insert_only` / `append_dedup` (tmp + `os.replace`, file-locked). Add both to the CLAUDE.md invariant list (Task 2). The Postgres tables `crypto_flow_snapshot` and `crypto_flow_provider_health` are append-only; `crypto_whale_events_live` is an explicitly documented 7-day **cache** (parquet is the archive) and is the only table that prunes.
- **No paid data sources.** Every provider is keyless or uses a free key from `.env`; a missing optional key disables that provider with a counter, never an error (`ETHERSCAN_API_KEY`, `HELIUS_API_KEY`, `SANTIMENT_API_KEY` are present in `.env` as of 2026-11-06 21:10Z; `COINGECKO_API_KEY`, `BLOCKCYPHER_TOKEN`, `BLOCKCHAIR_API_KEY` are optional and currently absent — the operator found CoinGecko Demo, Dune and Glassnode not free or trial-only, so no code path may require them).
- **Memory**: the tick runs under `MemoryMax=600M`; never `pd.read_parquet` a whole master — use `parquet_store.read_filtered`/duckdb windowed reads; every HTTP call has `timeout ≤ 20 s`; process `nice -n 19`.
- **Compute-window skip**: the tick exits 0 with `skipped=compute_window` during 15:00–15:50 America/New_York on Monday–Friday, or whenever `pgrep -f pipeline_orchestrator.py` finds a process.
- **Counters are the contract**: every tick prints one `[crypto-flow]` summary line with `providers_ok/providers_failed/events_new/flows_rows/snapshot_rows/skipped`; rc=1 only when the driver itself cannot run (no DB, no universe file).
- **Ticker forms**: internal `BTC-USD`; Alpaca order `BTC/USD`; Alpaca position `BTCUSD`; venue symbols per the universe map. One normaliser (Task 1).
- **Flags default inert**: `OPENCLAW_CRYPTO_FLOWS` unset ⇒ the `_sched` job returns immediately and the driver prints `gate OFF` rc 0.
- **Timestamps**: all `ts_utc` are timezone-aware UTC, floored to the hour for `crypto_flows_1h` and `crypto_flow_snapshot`, raw for events.
- **Dashboard**: inside `getDashboardHtml()`'s template literal every backtick is `\`` and every `${` is `\${`. GitHub-dark tokens only (`--bg --panel --border --text --muted --blue --green --red --yellow --orange --purple`). Regime colours reuse `_STATE_COLOR/_STATE_SHORT/_STATE_CLASS`.
- **Tests**: `pytest tests/ingestion/test_crypto_flow_*.py tests/scripts/test_run_crypto_flow_tick.py -q` for Python; `node --test tests/test_routes_crypto.test.js` for Node. Never run the full suite while the fleet runs. Tests must not hit the network or the real DB (stub `urlopen`, pass a fake `conn`).
- **Deployment**: johnbot is user-scope (`XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot`); the `_sched` entry and the dashboard pick up code only on that restart. Never start the system unit.

## Review Focus

1. **A provider returns HTTP 200 with an unexpected JSON shape** (Binance error page as HTML, OKX `code!="0"`, Etherscan `status:"0"` with `result:"Max rate limit reached"`). Expected: that provider's result is `ok=False` with the message in `err`, every other provider still runs, the tick still writes a snapshot. Pinned in Task 3 (`test_get_json_non_json_body_returns_none`) and Task 5 (`test_etherscan_rate_limit_message_is_error_not_rows`).
2. **The same on-chain transfer seen twice** (mempool then confirmed block; a re-run of the tick over the same window). Expected: one row in the events parquet, one row in the live cache. Pinned in Task 2 (`test_write_events_replay_is_idempotent`).
3. **A tick inside the compute window or with the orchestrator running.** Expected: exit 0, `skipped=compute_window`, no HTTP, no writes. Pinned in Task 12 (`test_skip_during_compute_window`, `test_skip_when_orchestrator_running`).
4. **Attribution coverage near zero** (registry misses every address). Expected: whale score damped, direction `neutral`, `attribution_coverage` 0.0 shown on the board, no shock line fires. Pinned in Task 11 (`test_score_damped_when_coverage_low`).
5. **Dashboard with empty tables** (first boot before the first tick, or migration not applied). Expected: every `/api/crypto/*` endpoint returns `{available:false, reason}` with HTTP 200 and the page renders placeholders, never a blank page or a console error. Pinned in Task 14 (`test_snapshot_empty_table_available_false`) and Task 15 step 6.

---

### Task 1: Universe map and symbol normaliser

**Files:**
- Create: `src/ingestion/crypto_flow/__init__.py` (empty)
- Create: `src/ingestion/crypto_flow/universe.py`
- Create: `src/ingestion/crypto_flow/crypto_flow_universe.json`
- Create: `scripts/refresh_crypto_flow_universe.py`
- Test: `tests/ingestion/test_crypto_flow_universe.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Asset:
      ticker: str            # 'BTC-USD'
      alpaca: str            # 'BTC/USD'
      position: str          # 'BTCUSD'
      role: str              # 'benchmark' | 'alt' | 'stable' | 'commodity'
      active: bool
      chain: str | None      # 'bitcoin' | 'ethereum' | 'solana' | 'ripple' | 'dogecoin' | 'litecoin' | 'bitcoin_cash' | 'cardano' | 'polkadot' | 'tezos' | 'filecoin' | 'arbitrum' | 'polygon' | 'avalanche' | None
      evm_chainid: int | None     # Etherscan V2 chainid (1, 42161, 137, 43114, 8453, 56) or None
      token_contract: str | None  # ERC-20 contract on evm_chainid, None for native
      decimals: int | None
      binance: str | None    # 'BTCUSDT'
      hyperliquid: str | None  # 'BTC'
      bybit: str | None
      okx: str | None        # 'BTC-USDT-SWAP'
      coinbase: str | None   # 'BTC-USD'
      coingecko_id: str | None
  UNIVERSE_PATH: Path  # src/ingestion/crypto_flow/crypto_flow_universe.json
  def load_universe(path: Path = UNIVERSE_PATH, *, active_only: bool = True) -> list[Asset]
  def by_ticker(assets: list[Asset]) -> dict[str, Asset]
  def normalize_broker_symbol(sym: str, assets: list[Asset] | None = None) -> str | None
      # 'BTCUSD' | 'BTC/USD' | 'BTC-USD' -> 'BTC-USD'; 'SPY', 'AAPL260117C00150000', '' -> None
  def refresh_from_alpaca(existing: list[Asset], alpaca_symbols: list[str]) -> tuple[list[Asset], list[str], list[str]]
      # returns (merged, added_tickers, deactivated_tickers); never removes a row
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/ingestion/test_crypto_flow_universe.py
from src.ingestion.crypto_flow import universe as u

def test_universe_file_has_all_36_alpaca_pairs_and_roles():
    assets = u.load_universe(active_only=False)
    tickers = {a.ticker for a in assets}
    assert {'BTC-USD','ETH-USD','SOL-USD','XRP-USD','DOGE-USD','USDC-USD','USDT-USD','USDG-USD','PAXG-USD'} <= tickers
    assert len(tickers) == 36
    roles = {a.ticker: a.role for a in assets}
    assert roles['BTC-USD'] == 'benchmark' and roles['ETH-USD'] == 'alt'
    assert roles['USDC-USD'] == 'stable' and roles['PAXG-USD'] == 'commodity'

def test_evm_names_carry_chainid_and_contract():
    m = u.by_ticker(u.load_universe())
    assert m['ETH-USD'].evm_chainid == 1 and m['ETH-USD'].token_contract is None
    assert m['LINK-USD'].evm_chainid == 1 and m['LINK-USD'].token_contract.lower() == '0x514910771af9ca656af840dff83e8264ecf986ca'
    assert m['ARB-USD'].evm_chainid == 42161 and m['AVAX-USD'].evm_chainid == 43114 and m['POL-USD'].evm_chainid == 137

def test_normalize_broker_symbol_all_three_forms():
    for s in ('BTCUSD', 'BTC/USD', 'BTC-USD'):
        assert u.normalize_broker_symbol(s) == 'BTC-USD'
    assert u.normalize_broker_symbol('SPY') is None
    assert u.normalize_broker_symbol('AAPL260117C00150000') is None
    assert u.normalize_broker_symbol('') is None

def test_refresh_adds_and_deactivates_but_never_removes():
    existing = u.load_universe(active_only=False)
    merged, added, deact = u.refresh_from_alpaca(existing, ['BTC/USD', 'NEW/USD'])
    assert 'NEW-USD' in added and 'ETH-USD' in deact
    assert len(merged) == len(existing) + 1
    assert next(a for a in merged if a.ticker == 'ETH-USD').active is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/openclaw && pytest tests/ingestion/test_crypto_flow_universe.py -q`
Expected: FAIL with `ModuleNotFoundError: src.ingestion.crypto_flow`

- [ ] **Step 3: Write `crypto_flow_universe.json`**

One object per Alpaca active `*/USD` pair (list in spec §0): the 36 tickers. Fill venue symbols: `binance` = `<BASE>USDT` where Binance lists it (BTC ETH SOL XRP DOGE ADA AVAX LINK DOT LTC BCH UNI AAVE CRV SUSHI YFI BAT GRT FIL XTZ SHIB PEPE ARB LDO ONDO POL RENDER BONK WIF TRUMP; `null` for HYPE SKY USDG PAXG=`PAXGUSDT` yes); `hyperliquid` = base symbol where the 235-perp list has it (check with `curl -s https://api.hyperliquid.xyz/info -d '{"type":"meta"}'`); `okx` = `<BASE>-USDT-SWAP`; `coinbase` = `<BASE>-USD`; `coingecko_id` from CoinGecko's id list. Chains and EVM contracts (mainnet, chainid 1 unless noted): ETH native; LINK `0x514910771af9ca656af840dff83e8264ecf986ca`; UNI `0x1f9840a85d5af5bf1d1762f925bdaddc4201f984`; AAVE `0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9`; CRV `0xd533a949740bb3306d119cc777fa900ba034cd52`; LDO `0x5a98fcbea516cf06857215779fd812ca3bef1b32`; GRT `0xc944e90c64b2c07662a292be6244bdf05cda44a7`; SHIB `0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce`; PEPE `0x6982508145454ce325ddbe47a25d4ec3d2311933`; SUSHI `0x6b3595068778dd592e39a122f4f5a5cf09c90fe2`; YFI `0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e`; BAT `0x0d8775f648430679a709e98d2b0cb6250d2887ef`; ONDO `0xfaba6f8e4a5e8ab82f62fe7c39859fa577269be3`; SKY `0x56072c95faa701256059aa122697b133aded9279`; RENDER `0x6de037ef9ad2725eb40118bb1702ebb27e4aeb24`; USDC `0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48`; USDT `0xdac17f958d2ee523a2206206994597c13d831ec7`; USDG `0xe343167631d89b6ffc58b88d6b7fb0228795491d`; PAXG `0x45804880de22913dafe09f4980848ece6ecbaf78`; ARB on 42161 `0x912ce59144191c1204e64559fe8253a0e49e6548`; POL native on 137; AVAX native on 43114; HYPE chain `hyperliquid` (no EVM contract tracked); SOL BONK WIF TRUMP chain `solana` (SPL mints: BONK `DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263`, WIF `EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm`, TRUMP `6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN`); XRP `ripple`; DOGE `dogecoin`; LTC `litecoin`; BCH `bitcoin_cash`; ADA `cardano`; DOT `polkadot`; XTZ `tezos`; FIL `filecoin`. Verify each contract against Etherscan before committing (`curl "https://api.etherscan.io/v2/api?chainid=1&module=token&action=tokeninfo&contractaddress=…&apikey=$ETHERSCAN_API_KEY"` or the token page).

- [ ] **Step 4: Implement `universe.py` with the Interfaces signatures**

`normalize_broker_symbol` strips `/` and `-`, upper-cases, requires the result to end in `USD`, and looks the base up in the loaded universe (including inactive rows). `refresh_from_alpaca` converts `BASE/USD` → `BASE-USD`, appends unknown tickers as `Asset(role='alt', active=True, chain=None, …)` with every venue field `None`, and sets `active=False` on existing tickers absent from the Alpaca list.

- [ ] **Step 5: Implement `scripts/refresh_crypto_flow_universe.py`**

CLI: `--apply` (default dry-run). Runs `/root/go/bin/alpaca asset list --asset-class crypto --status active -q --jq '[.[]|select(.symbol|endswith("/USD")).symbol]'` via `subprocess.run(timeout=60)`, calls `refresh_from_alpaca`, prints `[crypto-universe] added=[…] deactivated=[…] total=N`, writes the JSON atomically (tmp + `os.replace`) only with `--apply`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_crypto_flow_universe.py -q`
Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
git add src/ingestion/crypto_flow/__init__.py src/ingestion/crypto_flow/universe.py src/ingestion/crypto_flow/crypto_flow_universe.json scripts/refresh_crypto_flow_universe.py tests/ingestion/test_crypto_flow_universe.py
git commit -m "crypto-flow: universe map for the 36 Alpaca pairs + broker symbol normaliser"
```

---

### Task 2: Storage — migration 168, parquet writers, DB writers, invariant

**Files:**
- Create: `src/database/migrations/168_crypto_flow_tables.sql`
- Create: `src/ingestion/crypto_flow/models.py`
- Create: `src/ingestion/crypto_flow/store.py`
- Modify: `CLAUDE.md:7-12` (invariant list)
- Test: `tests/ingestion/test_crypto_flow_store.py`

**Interfaces:**
- Produces:
  ```python
  # models.py
  @dataclass
  class WhaleEvent:
      ts_utc: datetime; source: str; chain: str; symbol: str; ticker: str | None
      amount: float; amount_usd: float | None
      from_addr: str | None; from_owner: str | None; from_owner_type: str | None
      to_addr: str | None; to_owner: str | None; to_owner_type: str | None
      tx_type: str           # 'transfer' | 'mint' | 'burn'
      tx_hash: str; out_index: int = 0
      direction: str = 'unknown'   # 'exchange_in' | 'exchange_out' | 'exchange_to_exchange' | 'unknown'
      def key(self) -> tuple[str, str, str, int]   # (source, chain, tx_hash, out_index)
  @dataclass
  class Observation:
      ts_utc: datetime; ticker: str; source: str; fields: dict[str, float | int | str | None]
  @dataclass
  class ProviderResult:
      provider: str; ok: bool; events: list[WhaleEvent]; observations: list[Observation]
      rows: int; latency_ms: int; err: str | None = None; skipped: str | None = None
  MARKET_TICKER = '__MARKET__'
  # store.py
  EVENTS_PATH = Path('data/master/crypto_whale_events.parquet'); FLOWS_PATH = Path('data/master/crypto_flows_1h.parquet'); ONCHAIN_DAILY_PATH = Path('data/master/crypto_onchain_daily.parquet')
  EVENT_KEY = ('source', 'chain', 'tx_hash', 'out_index'); FLOW_KEY = ('ticker', 'ts_utc')
  def events_to_frame(events: list[WhaleEvent]) -> pd.DataFrame
  def write_events(events: list[WhaleEvent], *, path: Path = EVENTS_PATH) -> int          # append_insert_only; returns new rows written
  def write_flows(rows: pd.DataFrame, *, path: Path = FLOWS_PATH) -> int                 # append_dedup mode='replace'; returns total rows
  def write_onchain_daily(rows: pd.DataFrame, *, path: Path = ONCHAIN_DAILY_PATH) -> int   # key (ticker, date, metric); append_dedup mode='replace'
  def read_events_window(since: datetime, *, path: Path = EVENTS_PATH, tickers: list[str] | None = None) -> pd.DataFrame
  def read_flows_window(since: datetime, *, path: Path = FLOWS_PATH, tickers: list[str] | None = None) -> pd.DataFrame
  def write_snapshot(conn, ts_utc: datetime, per_ticker: dict[str, dict]) -> int         # INSERT … ON CONFLICT DO NOTHING
  def write_provider_health(conn, ts_utc: datetime, results: list[ProviderResult]) -> int
  def write_events_live(conn, events: list[WhaleEvent]) -> int                            # upsert + DELETE older than 7 days
  def get_conn()  # psycopg2.connect(os.environ['POSTGRES_URI'])
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/ingestion/test_crypto_flow_store.py
from datetime import datetime, timezone, timedelta
import pandas as pd
from src.ingestion.crypto_flow import store, models

def _ev(h='aa', idx=0, usd=2e6):
    return models.WhaleEvent(ts_utc=datetime(2026,11,6,1,0,tzinfo=timezone.utc), source='btc_onchain', chain='bitcoin',
        symbol='BTC', ticker='BTC-USD', amount=25.0, amount_usd=usd, from_addr='bc1a', from_owner=None, from_owner_type=None,
        to_addr='bc1b', to_owner='binance', to_owner_type='exchange', tx_type='transfer', tx_hash=h, out_index=idx, direction='exchange_in')

def test_write_events_replay_is_idempotent(tmp_path):
    p = tmp_path / 'ev.parquet'
    assert store.write_events([_ev('aa'), _ev('bb')], path=p) == 2
    assert store.write_events([_ev('aa'), _ev('cc', usd=3e6)], path=p) == 1
    df = pd.read_parquet(p)
    assert len(df) == 3 and set(df.columns) >= set(models.WhaleEvent.__dataclass_fields__)

def test_write_flows_replace_same_hour(tmp_path):
    p = tmp_path / 'fl.parquet'
    ts = pd.Timestamp('2026-11-06 01:00', tz='UTC')
    a = pd.DataFrame([{'ticker':'BTC-USD','ts_utc':ts,'whale_usd_1h':1.0}])
    b = pd.DataFrame([{'ticker':'BTC-USD','ts_utc':ts,'whale_usd_1h':2.0}])
    store.write_flows(a, path=p); n = store.write_flows(b, path=p)
    df = pd.read_parquet(p)
    assert n == 1 and df.loc[0,'whale_usd_1h'] == 2.0

def test_read_events_window_filters_since_and_tickers(tmp_path):
    p = tmp_path / 'ev.parquet'
    old = _ev('old'); old.ts_utc = datetime(2026,11,1,tzinfo=timezone.utc)
    store.write_events([old, _ev('new')], path=p)
    df = store.read_events_window(datetime(2026,11,5,tzinfo=timezone.utc), path=p, tickers=['BTC-USD'])
    assert list(df['tx_hash']) == ['new']

class _Cur:
    def __init__(s): s.sql=[]; s.rowcount=1
    def execute(s, q, p=None): s.sql.append((q, p))
    def __enter__(s): return s
    def __exit__(s, *a): pass
class _Conn:
    def __init__(s): s.cur=_Cur(); s.committed=0
    def cursor(s): return s.cur
    def commit(s): s.committed += 1

def test_write_snapshot_one_insert_per_ticker_on_conflict_do_nothing():
    c = _Conn(); ts = datetime(2026,11,6,1,tzinfo=timezone.utc)
    n = store.write_snapshot(c, ts, {'BTC-USD': {'whale_score': 71}, models.MARKET_TICKER: {'fear_greed': 50}})
    assert n == 2 and c.committed == 1
    assert all('ON CONFLICT' in q for q, _ in c.cur.sql) and all('crypto_flow_snapshot' in q for q, _ in c.cur.sql)

def test_write_events_live_prunes_seven_days():
    c = _Conn(); store.write_events_live(c, [_ev('aa')])
    assert any('DELETE FROM crypto_whale_events_live' in q and "7 days" in q for q, _ in c.cur.sql)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ingestion/test_crypto_flow_store.py -q`
Expected: FAIL with `ImportError` on `store`/`models`

- [ ] **Step 3: Write migration `168_crypto_flow_tables.sql`**

Header comment in the style of 118 (purpose, NEVER-DELETE note, the cache exception). Tables:
```sql
CREATE TABLE IF NOT EXISTS crypto_flow_snapshot (
    ticker TEXT NOT NULL, ts_utc TIMESTAMPTZ NOT NULL, features JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (ticker, ts_utc));
CREATE INDEX IF NOT EXISTS idx_crypto_flow_snapshot_ts ON crypto_flow_snapshot (ts_utc DESC);
CREATE TABLE IF NOT EXISTS crypto_flow_provider_health (
    id BIGSERIAL PRIMARY KEY, provider TEXT NOT NULL, ts_utc TIMESTAMPTZ NOT NULL, ok BOOLEAN NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER, err TEXT, skipped TEXT);
CREATE INDEX IF NOT EXISTS idx_crypto_flow_provider_health_pt ON crypto_flow_provider_health (provider, ts_utc DESC);
CREATE TABLE IF NOT EXISTS crypto_whale_events_live (   -- 7-day CACHE of data/master/crypto_whale_events.parquet (the archive)
    event_key TEXT PRIMARY KEY, ts_utc TIMESTAMPTZ NOT NULL, source TEXT, chain TEXT, symbol TEXT, ticker TEXT,
    amount NUMERIC, amount_usd NUMERIC, from_owner TEXT, to_owner TEXT, direction TEXT, tx_hash TEXT);
CREATE INDEX IF NOT EXISTS idx_crypto_whale_events_live_ts ON crypto_whale_events_live (ts_utc DESC);
```

- [ ] **Step 4: Implement `models.py` and `store.py`**

`write_events`: build the frame (`ts_utc` tz-aware UTC), compute `before = parquet_store.row_count(path)` if exists, call `append_insert_only(path, df, EVENT_KEY)`, return `after - before`. `write_flows`: `append_dedup(path, rows, FLOW_KEY, mode='replace')`. Window readers: `parquet_store.read_filtered` if its filter API fits, else duckdb `SELECT * FROM read_parquet(?) WHERE ts_utc >= ? [AND ticker IN (...)]` — never a whole-file pandas read. `write_snapshot`: one `INSERT INTO crypto_flow_snapshot (ticker, ts_utc, features) VALUES (%s,%s,%s::jsonb) ON CONFLICT DO NOTHING` per ticker, `json.dumps` with `allow_nan=False` after replacing NaN/inf with `None` (lesson `1dcf3cb8`). `write_events_live`: upsert by `event_key = '|'.join(key)`, then `DELETE FROM crypto_whale_events_live WHERE ts_utc < NOW() - INTERVAL '7 days'`.

- [ ] **Step 5: Add the invariant line to CLAUDE.md**

In the "Core invariant" paragraph, after `crypto_bars_1h.parquet`, add `crypto_whale_events.parquet, crypto_flows_1h.parquet, or crypto_onchain_daily.parquet` so the sentence reads `…historical_regimes.parquet, crypto_bars_1h.parquet, crypto_whale_events.parquet, crypto_flows_1h.parquet, or crypto_onchain_daily.parquet.**`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_crypto_flow_store.py -q`
Expected: 5 passed

- [ ] **Step 7: Apply the migration on the box and verify**

Run: `cd /root/openclaw && npm run db:migrate 2>&1 | tail -3 && psql "$POSTGRES_URI" -Atc "\dt crypto_flow*" `
Expected: three tables listed (`crypto_flow_snapshot`, `crypto_flow_provider_health`, `crypto_whale_events_live`).

- [ ] **Step 8: Commit**

```bash
git add src/database/migrations/168_crypto_flow_tables.sql src/ingestion/crypto_flow/models.py src/ingestion/crypto_flow/store.py CLAUDE.md tests/ingestion/test_crypto_flow_store.py
git commit -m "crypto-flow: migration 168 (snapshot, provider health, 7-day live cache), parquet + DB writers, NEVER-DELETE invariant"
```

---

### Task 3: Provider contract and HTTP helper

**Files:**
- Create: `src/ingestion/crypto_flow/providers/__init__.py`
- Create: `src/ingestion/crypto_flow/providers/base.py`
- Create: `src/ingestion/crypto_flow/exchange_addresses.json`
- Create: `src/ingestion/crypto_flow/registry.py`
- Test: `tests/ingestion/test_crypto_flow_providers_base.py`

**Interfaces:**
- Consumes: `models.*`, `universe.Asset`, `_http_retry.fetch_with_retry`
- Produces:
  ```python
  # base.py
  def get_json(url: str, *, label: str, timeout: int = 20, headers: dict | None = None,
               method: str = 'GET', body: dict | None = None) -> dict | list | None
      # urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers={'User-Agent': 'openclaw-crypto-flow/1.0', ...}, method=method)
      # → fetch_with_retry(req, timeout=timeout, attempts=2, label=label); None on failure or non-JSON body
  class Provider:
      name: str; cadence: str  # 'tick' | 'hourly' | 'daily'
      def enabled(self) -> tuple[bool, str | None]     # (True, None) or (False, 'no_key:ETHERSCAN_API_KEY')
      def fetch(self, assets: list[Asset], since: datetime, now: datetime) -> ProviderResult   # subclasses implement _fetch; base wraps timing + exceptions
      def _fetch(self, assets, since, now) -> tuple[list[WhaleEvent], list[Observation]]
  def run_provider(p: Provider, assets, since, now) -> ProviderResult   # catches every Exception -> ok=False, err=repr
  def usd(amount: float, price: float | None) -> float | None
  # registry.py
  REGISTRY_PATH: Path
  def load_registry(path=REGISTRY_PATH) -> dict[str, dict[str, str]]     # {chain: {address_lower: owner}}
  def owner_of(chain: str, address: str | None, reg) -> tuple[str | None, str | None]  # (owner, 'exchange') or (None, None)
  def classify_direction(from_owner_type, to_owner_type) -> str   # exchange_in | exchange_out | exchange_to_exchange | unknown
  # providers/__init__.py
  def all_providers() -> list[Provider]      # filled in by Tasks 4–10; order = fallback preference
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/ingestion/test_crypto_flow_providers_base.py
from datetime import datetime, timezone
from src.ingestion.crypto_flow.providers import base
from src.ingestion.crypto_flow import registry, models

def test_get_json_non_json_body_returns_none(monkeypatch):
    monkeypatch.setattr(base, 'fetch_with_retry', lambda *a, **k: b'<!DOCTYPE html><html>error</html>')
    assert base.get_json('https://x/y', label='t') is None

def test_get_json_posts_body(monkeypatch):
    seen = {}
    def fake(req, **k): seen['data'] = req.data; seen['method'] = req.get_method(); return b'{"ok":1}'
    monkeypatch.setattr(base, 'fetch_with_retry', fake)
    assert base.get_json('https://x', label='t', method='POST', body={'type': 'meta'}) == {'ok': 1}
    assert seen['method'] == 'POST' and b'"type"' in seen['data']

class _Boom(base.Provider):
    name = 'boom'; cadence = 'tick'
    def _fetch(self, assets, since, now): raise RuntimeError('kaput')

def test_run_provider_turns_exception_into_failed_result():
    r = base.run_provider(_Boom(), [], datetime.now(timezone.utc), datetime.now(timezone.utc))
    assert r.ok is False and 'kaput' in r.err and r.events == [] and r.latency_ms >= 0

def test_registry_direction_classification():
    reg = registry.load_registry()
    assert reg['ETH'] and reg['BTC']
    owner, typ = registry.owner_of('ETH', '0x28C6c06298d514Db089934071355E5743bf21d60', reg)
    assert owner == 'Binance' and typ == 'exchange'
    assert registry.classify_direction(None, 'exchange') == 'exchange_in'
    assert registry.classify_direction('exchange', None) == 'exchange_out'
    assert registry.classify_direction('exchange', 'exchange') == 'exchange_to_exchange'
    assert registry.classify_direction(None, None) == 'unknown'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ingestion/test_crypto_flow_providers_base.py -q`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Seed `exchange_addresses.json`**

Fetch `https://raw.githubusercontent.com/clawinfra/whalecli/main/whalecli/data/exchange_addresses.json` into the scratchpad, read it, and write our file in the shape `{"ETH": {"Binance": [..], ...}, "BTC": {...}, "SOL": {}, "XRP": {}, "ARB": {}, "POL": {}, "AVAX": {}}` plus a top-level `"_meta": {"sources": ["whalecli@<commit>"], "updated": "2026-11-06"}`. Confirm `0x28c6c06298d514db089934071355e5743bf21d60` (Binance 14) is present under ETH/Binance; if whalecli lacks it, add it (Etherscan name tag "Binance 14"). Addresses are stored lower-case for EVM, as-is for BTC/SOL/XRP.

- [ ] **Step 4: Implement `base.py`, `registry.py`, `providers/__init__.py`**

`Provider.fetch` measures `time.monotonic()`, calls `enabled()` first (`skipped='no_key:…'` → `ok=True, rows=0`), then `_fetch`, and fills `rows=len(events)+len(observations)`. `all_providers()` returns `[]` for now; later tasks append their classes.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_crypto_flow_providers_base.py -q`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/crypto_flow/providers src/ingestion/crypto_flow/registry.py src/ingestion/crypto_flow/exchange_addresses.json tests/ingestion/test_crypto_flow_providers_base.py
git commit -m "crypto-flow: provider contract, JSON helper, exchange-address registry seeded from whalecli"
```

---

### Task 4: Provider `btc_onchain`

**Files:**
- Create: `src/ingestion/crypto_flow/providers/btc_onchain.py`
- Create: `tests/ingestion/fixtures/crypto_flow/blockchain_info_unconfirmed.json` (trimmed real sample, 5 txs incl. one ≥ $1 M)
- Test: `tests/ingestion/test_crypto_flow_btc_onchain.py`

**Interfaces:**
- Consumes: `base.get_json`, `registry.*`, `Asset`
- Produces: `class BtcOnchain(Provider)` — `name='btc_onchain'`, `cadence='tick'`. Endpoints: `https://blockchain.info/unconfirmed-transactions?format=json` (each tx: `hash`, `time`, `inputs[].prev_out.{addr,value}`, `out[].{addr,value}` in satoshi) and `https://mempool.space/api/blocks` → for the newest block not yet seen (`last_height` kept in `data/master/.crypto_flow_state.json`) `https://mempool.space/api/block/{id}/txs` (first 25 txs page; `vin[].prevout.{scriptpubkey_address,value}`, `vout[].{scriptpubkey_address,value}`). BTC price from the `derivs` snapshot if available else `https://api.coinbase.com/v2/prices/BTC-USD/spot`. Emits one `WhaleEvent` per **output** ≥ `MIN_USD` (default `1_000_000`, env `OPENCLAW_CRYPTO_WHALE_MIN_USD`), `out_index` = vout index, `from_addr` = first input address, owners via registry, direction via `classify_direction`. Skips change outputs back to an input address.

- [ ] **Step 1: Write the failing tests**

```python
def test_parses_unconfirmed_and_emits_only_large_outputs(fixture_json):
    p = btc_onchain.BtcOnchain(); evs = p.parse_unconfirmed(fixture_json, price=76000.0, reg=registry.load_registry(), min_usd=1_000_000)
    assert all(e.amount_usd >= 1_000_000 for e in evs) and len(evs) == 1
    assert evs[0].chain == 'bitcoin' and evs[0].ticker == 'BTC-USD' and evs[0].out_index == 0

def test_change_output_back_to_input_is_skipped(...)   # fixture tx with an output to its own input address and value ≥ threshold → not emitted

def test_fetch_survives_mempool_failure(monkeypatch):   # get_json returns None for mempool, fixture for blockchain.info → ok=True, events from one source only
```

- [ ] **Step 2: Run to verify fail** — `pytest tests/ingestion/test_crypto_flow_btc_onchain.py -q` → `ImportError`

- [ ] **Step 3: Implement `BtcOnchain` with `parse_unconfirmed(payload, *, price, reg, min_usd) -> list[WhaleEvent]` and `parse_block_txs(txs, height, *, price, reg, min_usd) -> list[WhaleEvent]`** as pure helpers; `_fetch` composes them. Append `BtcOnchain` to `all_providers()`.

- [ ] **Step 4: Run to verify pass** — 3 passed

- [ ] **Step 5: Commit** — `git commit -m "crypto-flow: btc_onchain provider (blockchain.info mempool + mempool.space blocks)"`

---

### Task 5: Provider `evm_onchain` (Etherscan V2)

**Files:**
- Create: `src/ingestion/crypto_flow/providers/evm_onchain.py`
- Create: `tests/ingestion/fixtures/crypto_flow/etherscan_tokentx.json`, `etherscan_txlist.json`, `etherscan_ratelimit.json` (`{"status":"0","message":"NOTOK","result":"Max rate limit reached"}`)
- Test: `tests/ingestion/test_crypto_flow_evm_onchain.py`

**Interfaces:**
- Consumes: `base`, `registry`, `Asset.evm_chainid/token_contract/decimals`, env `ETHERSCAN_API_KEY`
- Produces: `class EvmOnchain(Provider)` — `name='evm_onchain'`, `cadence='tick'`, `enabled()` false without the key. For each exchange address in the registry on each tracked chain (ETH 1, ARB 42161, POL 137, AVAX 43114, BASE 8453, BNB 56): `GET https://api.etherscan.io/v2/api?chainid={cid}&module=account&action=tokentx&address={addr}&startblock={last_block}&sort=desc&offset=100&apikey=…` (ERC-20) and `action=txlist` (native). Token-bucket **5 req/s** (`time.sleep` based, `_RATE = 5`). Maps `contractAddress` → ticker via the universe map; prices from the `derivs`/`macro_sent` snapshot (CoinGecko `coins/markets` fallback). Emits `WhaleEvent` for transfers ≥ `MIN_USD` where the exchange address is sender (`exchange_out`) or receiver (`exchange_in`). `last_block` per chain in `.crypto_flow_state.json`. **A `status:"0"` with a `result` string is an error** (`err='etherscan: Max rate limit reached'`), never zero rows.

- [ ] **Step 1: Write the failing tests**

```python
def test_disabled_without_key(monkeypatch): monkeypatch.delenv('ETHERSCAN_API_KEY', raising=False); assert EvmOnchain().enabled() == (False, 'no_key:ETHERSCAN_API_KEY')
def test_tokentx_maps_contract_to_ticker_and_direction(fixture): ...  # LINK transfer to Binance 14 → ticker 'LINK-USD', direction 'exchange_in', amount scaled by decimals 18
def test_etherscan_rate_limit_message_is_error_not_rows(fixture_ratelimit): ...  # parse returns ([], 'Max rate limit reached')
def test_rate_limiter_spaces_calls(monkeypatch): ...  # 6 calls → sleep total ≥ 0.2 s (patch time.sleep, assert sum)
```

- [ ] **Step 2–5:** fail → implement `parse_transfers(payload, *, chain_id, exchange_addr, owner, assets_by_contract, prices, min_usd) -> tuple[list[WhaleEvent], str | None]` + `_fetch` → pass → commit `"crypto-flow: evm_onchain provider (Etherscan V2, exchange-side transfers on 6 chains)"`.

---

### Task 6: Providers `derivs` and `liquidations`

**Files:**
- Create: `src/ingestion/crypto_flow/providers/derivs.py`, `providers/liquidations.py`
- Fixtures: `hl_meta_and_ctxs.json`, `binance_oi.json`, `binance_premium.json`, `binance_global_ls.json`, `binance_top_ls.json`, `binance_taker.json`, `bybit_oi.json`, `okx_oi.json`, `okx_liq.json`
- Test: `tests/ingestion/test_crypto_flow_derivs.py`

**Interfaces:**
- Produces: `class Derivs(Provider)` `name='derivs'`, one `Observation` per asset with `fields`: `oi_usd`, `funding_8h`, `premium`, `mark_px`, `vol_24h_usd`, `ls_ratio_global`, `ls_ratio_top`, `taker_buy_sell`, `oi_usd_total`, `venues` (comma list that answered). Hyperliquid `POST info {"type":"metaAndAssetCtxs"}` is primary (one call for all names: `openInterest × markPx`, `funding`, `premium`, `dayNtlVlm`); Binance `fapi/v1/openInterest`, `fapi/v1/premiumIndex`, `futures/data/globalLongShortAccountRatio?period=1h&limit=1`, `topLongShortPositionRatio`, `takerlongshortRatio` per asset with a Binance symbol (only ratios come solely from Binance → Bybit `v5/market/account-ratio` fallback for `ls_ratio_global`); Bybit/OKX `open-interest` as OI fallbacks; `oi_usd_total` = Σ venue OI when ≥ 2 answered else `None`. `class Liquidations(Provider)` `name='liquidations'`: OKX `GET /api/v5/public/liquidation-orders?instType=SWAP&uly={BASE}-USDT&state=filled&limit=100` → `liq_long_usd_1h`, `liq_short_usd_1h` (`posSide` long ⇒ long liquidation; `sz × bkPx × ctVal`; BTC-USDT-SWAP ctVal 0.01, ETH 0.1, else fetch `public/instruments` once per tick and cache `ctVal`).

- [ ] **Step 1: Failing tests** — `test_hyperliquid_primary_fills_oi_funding_for_all_mapped(fixture)`, `test_binance_fallback_only_for_ratios(monkeypatch)` (HL answers; Binance ratios merged; `venues == 'hyperliquid,binance'`), `test_oi_total_requires_two_venues`, `test_okx_liquidations_split_by_side(fixture)`, `test_binance_html_error_is_provider_degraded_not_crash` (get_json → None for every Binance URL → observations still present from HL, `err` None, field `ls_ratio_global` None).
- [ ] **Step 2–5:** fail → implement → pass → commit `"crypto-flow: derivs (Hyperliquid primary, Binance/Bybit/OKX fallbacks) + OKX liquidations providers"`.

---

### Task 7: Provider `spot_tape`

**Files:**
- Create: `src/ingestion/crypto_flow/providers/spot_tape.py`
- Fixtures: `binance_aggtrades.json`, `coinbase_trades.json`, `kraken_trades.json`
- Test: `tests/ingestion/test_crypto_flow_spot_tape.py`

**Interfaces:**
- Produces: `class SpotTape(Provider)` `name='spot_tape'`; per asset `Observation.fields`: `large_prints_n`, `large_prints_usd_buy`, `large_prints_usd_sell`, `tape_coverage_s` (seconds spanned by the 1 000 returned trades), `tape_venue`. Binance `GET /api/v3/aggTrades?symbol={}&limit=1000` (`m` = "was the buyer the maker": `m` true ⇒ taker **sold**, `m` false ⇒ taker **bought**); Coinbase `GET /products/{}/trades?limit=1000` (`side` is the **maker** side on Coinbase: `side:'sell'` ⇒ taker bought — document and test this inversion); Kraken `GET /0/public/Trades?pair={}` (`b`/`s` = taker side). Threshold `LARGE_PRINT_USD = 250_000` (env `OPENCLAW_CRYPTO_LARGE_PRINT_USD`).

- [ ] **Step 1: Failing tests** — `test_binance_maker_flag_inverts_to_taker_side`, `test_coinbase_side_is_maker_side_inverted`, `test_prints_below_threshold_not_counted`, `test_falls_back_to_coinbase_when_binance_none`.
- [ ] **Step 2–5:** fail → implement `classify_prints(trades: list[tuple[float, float, str]], min_usd) -> dict` (price, size, taker_side) → pass → commit `"crypto-flow: spot_tape provider (large prints, Binance→Coinbase→Kraken)"`.

---

### Task 8: Providers `deribit_vol` and `hl_whales`

**Files:**
- Create: `providers/deribit_vol.py`, `providers/hl_whales.py`
- Fixtures: `deribit_dvol.json`, `deribit_book_summary.json` (20 options across 2 expiries), `hl_leaderboard.json` (5 rows), `hl_clearinghouse.json`
- Test: `tests/ingestion/test_crypto_flow_vol_whales.py`

**Interfaces:**
- `class DeribitVol(Provider)` `name='deribit_vol'`, observations for `BTC-USD` and `ETH-USD`: `dvol` (last close from `public/get_volatility_index_data?currency=BTC&resolution=3600&start_timestamp=…&end_timestamp=…`), `atm_iv_30d` (from `public/get_book_summary_by_currency?currency=BTC&kind=option`: pick the expiry nearest 30 days, strike nearest `underlying_price`, average call/put `mark_iv`), `skew_25d` (put IV − call IV at the strikes whose |Δ| is nearest 0.25 using `public/get_book_summary` has no delta → approximate with strikes at ±10 % of spot on that expiry; document), `put_call_oi` (Σ put `open_interest` / Σ call). Instrument names parse as `BTC-26DEC26-80000-C`.
- `class HlWhales(Provider)` `name='hl_whales'`, `cadence='hourly'`: `GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` → top `N=200` by `accountValue` (cache the list for 24 h in `.crypto_flow_state.json`), then `POST info {"type":"clearinghouseState","user":addr}` for each (200 calls, 0.1 s spacing) → per coin Σ signed `positionValue` (`szi` sign) → observations `whale_perp_net_usd`, `whale_perp_n_long`, `whale_perp_n_short`, `whale_perp_accounts_seen`.

- [ ] **Step 1: Failing tests** — `test_instrument_name_parse`, `test_atm_iv_picks_30d_expiry_and_nearest_strike`, `test_put_call_oi_ratio`, `test_hl_whales_signed_sum_by_coin(fixture)`, `test_hl_whales_hourly_cadence_flag`.
- [ ] **Step 2–5:** fail → implement → pass → commit `"crypto-flow: deribit_vol (DVOL, ATM IV, skew, P/C OI) + hl_whales (top-200 perp positioning) providers"`.

---

### Task 9: Providers `xrp_onchain`, `sol_onchain`, `utxo_alts`

**Files:**
- Create: `providers/xrp_onchain.py`, `providers/sol_onchain.py`, `providers/utxo_alts.py`
- Fixtures: `xrpl_account_tx.json`, `solana_signatures.json`, `solana_tx.json`, `blockcypher_block.json`, `blockchair_txs.json`
- Test: `tests/ingestion/test_crypto_flow_alt_chains.py`

**Interfaces:**
- `XrpOnchain` (`cadence='tick'`): `POST https://xrplcluster.com/ {"method":"account_tx","params":[{"account":addr,"limit":50,"ledger_index_min":last}]}` for each registry XRP exchange address; `TransactionType=='Payment'` with `Amount` as drops string (÷1e6) ≥ `MIN_USD`/price.
- `SolOnchain` (`cadence='hourly'`, RPC base `HELIUS_API_KEY` ? `https://mainnet.helius-rpc.com/?api-key=…` : `https://api.mainnet-beta.solana.com`): `getSignaturesForAddress(addr, limit=50)` then `getTransaction(sig, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0})` for new signatures; parsed `transfer`/`transferChecked` instructions for SOL and the tracked SPL mints; **budget 60 RPC calls per run on the public endpoint**, 600 with Helius.
- `UtxoAlts` (`cadence='hourly'`): BlockCypher `GET /v1/{doge,ltc}/main` → latest `height`, `GET /v1/{coin}/main/blocks/{height}?txstart=0&limit=50` then `GET /v1/{coin}/main/txs/{hash}` for txs whose `total` ≥ threshold (token `BLOCKCYPHER_TOKEN` appended when set); Blockchair `GET /bitcoin-cash/transactions?q=output_total(1000000000..)&s=time(desc)&limit=50` (satoshi filter at ≥ 10 BCH, then USD filter).

- [ ] **Step 1: Failing tests** — one parse test per provider on its fixture (direction + amount), `test_sol_public_budget_stops_at_60_calls(monkeypatch)`, `test_utxo_alts_token_appended_when_env_set`.
- [ ] **Step 2–5:** fail → implement → pass → commit `"crypto-flow: xrp_onchain, sol_onchain (public RPC/Helius), utxo_alts (DOGE/LTC/BCH) providers"`.

---

### Task 10: Providers `cftc_cot`, `stables_defi`, `macro_sent`, `santiment`

**Files:**
- Create: `providers/cftc_cot.py`, `providers/stables_defi.py`, `providers/macro_sent.py`, `providers/santiment.py`
- Create: `tests/ingestion/fixtures/crypto_flow/santiment_exchange_balance.json`
- Fixtures: `cftc_tff.json`, `llama_stablecoins.json`, `llama_chains.json`, `llama_dexs.json`, `coingecko_global.json`, `coingecko_markets.json`, `coinpaprika_tickers.json`, `fng.json`
- Test: `tests/ingestion/test_crypto_flow_market_providers.py`

**Interfaces:** all emit `Observation(ticker=MARKET_TICKER, …)` except `macro_sent` which also emits per-asset `price_usd`, `mcap_usd`, `vol_24h_usd`, `ret_24h` (CoinGecko `coins/markets?vs_currency=usd&ids=<all coingecko_ids>&price_change_percentage=1h,24h`, header `x-cg-demo-api-key` when `COINGECKO_API_KEY` set; CoinPaprika `/v1/tickers` fallback).
- `CftcCot` (`cadence='daily'`): `GET https://publicreporting.cftc.gov/resource/gpe5-46if.json?$where=contract_market_name like '%BITCOIN%'&$order=report_date_as_yyyy_mm_dd DESC&$limit=12` → for `contract_market_name == 'BITCOIN'` (CME 5-BTC): `cot_lev_net = lev_money_positions_long − lev_money_positions_short`, `cot_am_net`, `cot_dealer_net`, `cot_report_date`, `cot_lev_net_chg_1w`.
- `StablesDefi` (`cadence='hourly'`): `stable_supply_usd` = Σ `circulating.peggedUSD` for USDT+USDC+USDG (match `symbol`), `tvl_usd` (Σ `api.llama.fi/v2/chains`), `dex_vol_24h_usd` (`overview/dexs?excludeTotalDataChart=true…` `total24h`); per-stable observations `circulating_usd`.
- `MacroSent` (`cadence='hourly'`): `btc_dominance`, `total_mcap_usd` (`/global`), `fear_greed` (`https://api.alternative.me/fng/?limit=1`).
- `Santiment` (`cadence='daily'`, `enabled()` false without `SANTIMENT_API_KEY`; header `Authorization: Apikey <key>`; `POST https://api.santiment.net/graphql`). The free tier (verified 2026-11-06, user `sidjain08`) serves `exchange_balance`, `exchange_inflow`, `exchange_outflow`, `whale_transaction_count_1m_usd_to_inf`, `daily_active_addresses` for slugs `bitcoin`, `ethereum` **with a 30-day lag** (`restrictedTo = now − 30 d`) over a 1-year window. Query `getMetric(metric){ timeseriesData(slug, from:"utc_now-45d", to:"utc_now-31d", interval:"1d"){ datetime value } }` per metric per slug (10 calls/run). These are **lagged daily history**, not live signals, so they do **not** go into `crypto_flows_1h`; they are written to a third append-only master `data/master/crypto_onchain_daily.parquet`, key `(ticker, date, metric)`, via `store.write_onchain_daily(df) -> int` (`append_dedup mode='replace'`, Santiment revises). Add that path to the NEVER-DELETE list (Task 2 invariant line) and to `master_freshness._CADENCES` as `('date', 40)` (lag-aware). The Crypto page shows the latest Santiment exchange-balance 7-day change for BTC/ETH on the market tiles labelled "(30 d lag)".

- [ ] **Step 1: Failing tests** — `test_cot_picks_5btc_contract_and_nets`, `test_stables_sum_three_symbols`, `test_macro_sent_header_when_key_set`, `test_macro_sent_paprika_fallback`, `test_santiment_disabled_without_key`, `test_santiment_rows_keyed_ticker_date_metric(fixture)` (BTC exchange_balance fixture → rows with `ticker='BTC-USD'`, `metric='exchange_balance'`, `date` from `datetime[:10]`), `test_santiment_restricted_error_is_error_not_rows` (GraphQL `errors[]` present → `err` set).
- [ ] **Step 2–5:** fail → implement → pass → commit `"crypto-flow: cftc_cot, stables_defi, macro_sent, santiment (lagged daily) providers"`.

---

### Task 11: Feature computation and whale-pressure score

**Files:**
- Create: `src/ingestion/crypto_flow/features.py`
- Test: `tests/ingestion/test_crypto_flow_features.py`

**Interfaces:**
- Consumes: event/observation frames from `store.read_*_window` and the current tick's `ProviderResult`s
- Produces:
  ```python
  DEFAULT_THRESHOLDS = {'delev_oi_chg_1h_pct': -5.0, 'delev_ret_1h_pct': -3.0, 'netflow_z_shock': 3.0,
                        'coverage_floor': 0.25, 'score_alert': 70}
  FEATURE_COLS: list[str]   # the per-ticker keys below, fixed order, used for the parquet columns
  def exchange_netflow(events: pd.DataFrame, ticker: str, now: datetime, hours: int) -> dict
      # whale_n, whale_usd, exch_in_usd, exch_out_usd, exch_netflow_usd (= in − out), attribution_coverage
  def netflow_zscore(hourly_netflow: pd.Series, current: float) -> float | None     # vs trailing 7×24 hourly values; None if < 48 points
  def deleveraging_flag(oi_chg_1h_pct: float | None, ret_1h_pct: float | None, thr=DEFAULT_THRESHOLDS) -> bool
  def oi_price_divergence(oi_chg_24h_pct, ret_24h_pct) -> str   # 'short_build' | 'long_build' | 'deleveraging' | 'covering' | 'none'
  def whale_score(*, exch_netflow_usd_24h: float, attribution_coverage: float, whale_usd_24h: float,
                  whale_usd_30d_mean: float | None, agreement_share: float | None, deriv_confirm: float | None,
                  tape_imbalance_1h: float | None, netflow_z: float | None) -> tuple[int, str]
      # components: netflow 0–40 = min(40, 3.5·log10(max(|netflow|,1))·cov_w) with cov_w = min(1, coverage/0.25);
      # velocity 0–25 = min(25, 8.3·log2(max(whale_usd_24h/whale_usd_30d_mean, 1))) (0 if mean None);
      # agreement 0–20 = round(20·agreement_share) (0 if None); derivatives 0–15 = round(15·deriv_confirm) (0 if None)
      # direction: 'distributing' if netflow_z ≥ 1 and (tape_imbalance_1h or 0) ≤ 0; 'accumulating' if netflow_z ≤ −1 and (tape_imbalance_1h or 0) ≥ 0; else 'neutral'
  def deriv_confirm(direction_sign: int, funding_8h, ls_ratio_global, taker_buy_sell) -> float | None   # share of the three that lean with direction_sign; None if all None
  def compute_ticker_features(ticker: str, now: datetime, events: pd.DataFrame, flows_hist: pd.DataFrame,
                              obs: dict[str, dict], agreement_share: float | None, thr=DEFAULT_THRESHOLDS) -> dict
  def compute_market_features(per_ticker: dict[str, dict], market_obs: dict, thr=DEFAULT_THRESHOLDS) -> dict
      # stable_supply_usd, stable_supply_chg_7d_pct (needs flows_hist of __MARKET__), btc_dominance, fear_greed, oi_usd_total,
      # delev_market (BTC or ETH delev_1h), whale_score_btc, whale_score_eth, cot_*, dvol_btc, shock_lines: list[str]
  def agreement_share(per_ticker_netflow_sign: dict[str, int], btc_sign: int) -> float | None   # share of alts matching BTC; None if < 3 alts
  ```

- [ ] **Step 1: Write the failing tests**

```python
def test_netflow_in_minus_out_and_coverage(): ...        # 3 events: in 2M (owner), out 1M (owner), 5M unknown → netflow 1e6, coverage 3/8
def test_score_bounds_and_components():
    s, d = features.whale_score(exch_netflow_usd_24h=1e7, attribution_coverage=1.0, whale_usd_24h=2e8, whale_usd_30d_mean=1e7,
                                agreement_share=1.0, deriv_confirm=1.0, tape_imbalance_1h=-0.2, netflow_z=2.0)
    assert s == 100 and d == 'distributing'
def test_score_damped_when_coverage_low():
    s, d = features.whale_score(exch_netflow_usd_24h=1e7, attribution_coverage=0.0, whale_usd_24h=0, whale_usd_30d_mean=None,
                                agreement_share=None, deriv_confirm=None, tape_imbalance_1h=None, netflow_z=None)
    assert s == 0 and d == 'neutral'
def test_direction_rules(): ...  # z=1.5 & imbalance +0.3 → neutral; z=-1.2 & imbalance 0 → accumulating
def test_deleveraging_flag_thresholds(): assert features.deleveraging_flag(-5.1, -3.0) and not features.deleveraging_flag(-4.9, -3.0) and not features.deleveraging_flag(None, -9)
def test_netflow_zscore_needs_48_points(): ...
def test_compute_ticker_features_has_fixed_columns(): assert set(features.FEATURE_COLS) <= set(result)
def test_market_features_shock_lines_only_above_thresholds(): ...  # BTC z=3.2 → one line; z=2.9 → none
```

- [ ] **Step 2: Run to verify fail** — `pytest tests/ingestion/test_crypto_flow_features.py -q` → `ImportError`
- [ ] **Step 3: Implement `features.py`** (pure pandas/math, no I/O; NaN → `None` on output).
- [ ] **Step 4: Run to verify pass** — 8 passed
- [ ] **Step 5: Commit** — `git commit -m "crypto-flow: per-ticker features, whale-pressure score, deleveraging flag, market features"`

---

### Task 12: Tick driver, scheduler registration, health registration

**Files:**
- Create: `scripts/run_crypto_flow_tick.py`
- Modify: `src/engine/cron-schedule.js` (after the `crypto-regime-tick` block, ~:815)
- Modify: `src/system_checks/checks/scheduled_jobs.py:38` (`CRON_POLICIES`)
- Modify: `src/system_checks/checks/master_freshness.py` (`_CADENCES`)
- Modify: `src/strategies/sync_data_ledger.py` (`PARQUET_MAP`, `PROVIDERS`)
- Modify: `.env.example` (flag + optional keys)
- Test: `tests/scripts/test_run_crypto_flow_tick.py`

**Interfaces:**
- Consumes: Tasks 1–11
- Produces:
  ```python
  # scripts/run_crypto_flow_tick.py
  def should_skip(now_utc: datetime, *, pgrep: Callable[[str], bool], force: bool) -> str | None
      # 'compute_window' when America/New_York time is Mon–Fri 15:00 ≤ t < 15:50; 'orchestrator_running' when pgrep('pipeline_orchestrator.py'); None otherwise or when force
  def due_providers(providers: list[Provider], now_utc: datetime, state: dict) -> list[Provider]
      # 'tick' always; 'hourly' when state['last_run'][name] is None or ≥ 55 min ago; 'daily' when ≥ 23 h ago
  def run_tick(*, now_utc: datetime, assets, providers, conn, state_path: Path, dry_run: bool) -> dict   # the counters
  def main(argv=None) -> int   # --dry-run (no writes, prints counters), --force (ignore skip), --providers a,b, --since-hours N (default 1)
  ```
  Summary line format (exact): `[crypto-flow] ts=2026-11-06T01:15Z providers_ok=11 providers_failed=1 providers_skipped=0 events_new=14 flows_rows=33 snapshot_rows=34 degraded=binance skipped=- dt_s=18.4`

- [ ] **Step 1: Write the failing tests**

```python
# tests/scripts/test_run_crypto_flow_tick.py
from datetime import datetime, timezone
from scripts import run_crypto_flow_tick as t   # add scripts/__init__.py if import fails, matching existing tests/scripts pattern

def test_skip_during_compute_window():
    # 2026-11-06 is a Friday; 20:10Z = 15:10 ET (EST)
    assert t.should_skip(datetime(2026,11,6,20,10,tzinfo=timezone.utc), pgrep=lambda p: False, force=False) == 'compute_window'
    assert t.should_skip(datetime(2026,11,6,20,55,tzinfo=timezone.utc), pgrep=lambda p: False, force=False) is None
    assert t.should_skip(datetime(2026,11,7,20,10,tzinfo=timezone.utc), pgrep=lambda p: False, force=False) is None  # Saturday
def test_skip_when_orchestrator_running():
    assert t.should_skip(datetime(2026,11,6,3,0,tzinfo=timezone.utc), pgrep=lambda p: 'orchestrator' in p, force=False) == 'orchestrator_running'
    assert t.should_skip(datetime(2026,11,6,3,0,tzinfo=timezone.utc), pgrep=lambda p: True, force=True) is None
def test_due_providers_by_cadence(): ...
def test_run_tick_writes_snapshot_even_when_a_provider_fails(tmp_path, monkeypatch): ...   # two fake providers (one raises) + fake conn → counters providers_failed==1, snapshot_rows>=1, degraded contains name
def test_dry_run_writes_nothing(tmp_path): ...
def test_gate_off_returns_zero(monkeypatch, capsys): monkeypatch.delenv('OPENCLAW_CRYPTO_FLOWS', raising=False); assert t.main([]) == 0; assert 'gate OFF' in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify fail** — `pytest tests/scripts/test_run_crypto_flow_tick.py -q` → `ImportError`

- [ ] **Step 3: Implement `scripts/run_crypto_flow_tick.py`**

Order inside `run_tick`: load state (`data/master/.crypto_flow_state.json`, `{last_run:{}, last_block:{}, last_height:…}`), run due providers sequentially (`run_provider`), `write_events` + `write_events_live`, build `obs` dict per ticker from this tick's observations merged over the last snapshot row per ticker (so hourly providers' fields persist between runs), read `events` window 30 d and `flows_hist` 30 d via `store.read_*_window`, compute per-ticker features for every active non-stable asset, `agreement_share`, market features, `write_flows` (one row per ticker + `__MARKET__`, `ts_utc` floored to the hour), `write_snapshot`, `write_provider_health`, print shock lines as `[crypto-flow] shock ticker=… kind=… value=…`, save state, print the summary line. `main`: gate check, `os.nice(19)`, `should_skip` (print `[crypto-flow] skipped=<reason>` rc 0), `get_conn()`.

- [ ] **Step 4: Register the scheduler job in `cron-schedule.js`**

After the `crypto-regime-tick` block add `_sched('crypto-flow-tick', '*/15 * * * *', () => {...}, { timezone: 'UTC' })`: return unless `process.env.OPENCLAW_CRYPTO_FLOWS === '1'`; spawn via `wrapCapped(PYTHON, ['scripts/run_crypto_flow_tick.py'], { memoryMax: process.env.OPENCLAW_CRYPTO_FLOW_MEMORY_MAX || '600M' })` from `src/lib/capped_spawn.js` (read its exported signature first; it returns `{cmd, args}` or similar — use it exactly as the backtest spawner does), detached, log `logs/crypto_flows_<date>.log`.

- [ ] **Step 5: Register health**

`scheduled_jobs.CRON_POLICIES['crypto-flow-tick'] = ('intraday_min', 15)` **only if** that policy kind is evaluated 24/7; otherwise add a new kind `('minutes', 20)` with the same semantics as `'hours'` (read the policy evaluator in the same file first). `master_freshness._CADENCES`: `'crypto_whale_events.parquet': ('ts_utc', 1)`, `'crypto_flows_1h.parquet': ('ts_utc', 1)`, `'crypto_onchain_daily.parquet': ('date', 40)`. `sync_data_ledger`: `PARQUET_MAP['crypto_flows_1h'] = 'crypto_flows_1h'`, `PARQUET_MAP['crypto_whale_events'] = 'crypto_whale_events'`, `PARQUET_MAP['crypto_onchain_daily'] = 'crypto_onchain_daily'`, `PROVIDERS[...] = 'keyless-multi+etherscan'`. `.env.example`: `OPENCLAW_CRYPTO_FLOWS=` (comment: `1` enables the 15-min flow tick), `OPENCLAW_CRYPTO_FLOW_MEMORY_MAX=600M`, `OPENCLAW_CRYPTO_WHALE_MIN_USD=1000000`, `OPENCLAW_CRYPTO_LARGE_PRINT_USD=250000`, `HELIUS_API_KEY=` and `SANTIMENT_API_KEY=` (provided; Solana RPC + lagged on-chain daily), and the optional `COINGECKO_API_KEY= BLOCKCYPHER_TOKEN= BLOCKCHAIR_API_KEY=` under a `# Crypto engine — free keys (spec §10.2)` comment.

- [ ] **Step 6: Run tests** — `pytest tests/scripts/test_run_crypto_flow_tick.py tests/ingestion/test_crypto_flow_*.py -q` → all passed; `node --check src/engine/cron-schedule.js` → no output.

- [ ] **Step 7: Live dry run on the box (no writes)**

Run: `cd /root/openclaw && set -a && . ./.env && set +a && nice -n 19 python3 scripts/run_crypto_flow_tick.py --dry-run --force 2>&1 | tail -20`
Expected: a `[crypto-flow] … providers_ok≥9 … dt_s<60` line; each failing provider named in `degraded=`; no parquet files created (`ls data/master/crypto_*` shows only `crypto_bars_1h.parquet`).

- [ ] **Step 8: Commit**

```bash
git add scripts/run_crypto_flow_tick.py src/engine/cron-schedule.js src/system_checks/checks/scheduled_jobs.py src/system_checks/checks/master_freshness.py src/strategies/sync_data_ledger.py .env.example tests/scripts/test_run_crypto_flow_tick.py
git commit -m "crypto-flow: 15-min tick driver (compute-window skip, counters), _sched registration under MemoryMax, health + ledger registration"
```

---

### Task 13: Regime monitor advisory columns

**Files:**
- Modify: `scripts/run_crypto_market_state.py:100-131`
- Test: `tests/regime/test_crypto_regime_advisory.py`

**Interfaces:**
- Produces: `def advisory_features(conn) -> dict` in `scripts/run_crypto_market_state.py` returning `{'adv_btc_netflow_z_7d', 'adv_btc_funding_8h', 'adv_delev_market', 'adv_dvol_btc', 'adv_whale_score_btc', 'adv_flow_age_min'}` from the latest `crypto_flow_snapshot` rows for `BTC-USD` and `__MARKET__`; `{}` when the table is empty or the query fails (fail-open, never changes the HMM path). Merged into `latest_features` before `_insert_state` and `build_latest_json` so they land in `features_json` and the JSON file.

- [ ] **Step 1: Failing test** — fake conn returning two rows → dict has the six keys; fake conn raising → `{}`; `cr.CRYPTO_FEATURE_COLS` unchanged (HMM inputs untouched).
- [ ] **Step 2–5:** fail → implement → pass → commit `"crypto-regime: advisory flow columns (netflow z, funding, deleveraging, DVOL) in features_json — not HMM inputs"`.

---

### Task 14: API router `routes_crypto.js`

**Files:**
- Create: `src/channels/api/routes_crypto.js`
- Modify: `src/channels/api/server.js:3379` (mount) and `:50-57` (export `broadcast` is already there; add the event watcher start)
- Test: `tests/test_routes_crypto.test.js`

**Interfaces:**
- Consumes: `require('../../database/postgres').query`, `broadcast` from `server.js`
- Produces (all respond HTTP 200; `{available:false, reason}` on empty/erroring tables):
  - `GET /api/crypto/snapshot` → `{available, ts_utc, market: {...features}, tickers: [{ticker, ...features}], provider_health: [{provider, ok, rows, latency_ms, err, ts_utc}] (latest per provider)}`
  - `GET /api/crypto/history/:ticker?days=7` → `{available, ticker, rows: [{ts_utc, exch_netflow_usd_24h, oi_usd, funding_8h, whale_score, price_usd}]}` from `crypto_flow_snapshot` (hourly rows; `days` clamped 1–30)
  - `GET /api/crypto/events?limit=50` → `{available, rows}` from `crypto_whale_events_live` (limit clamped 1–200)
  - `GET /api/crypto/providers` → latest health per provider + `degraded: [names with 3 consecutive ok=false]`
  - `GET /api/crypto/risk` → `{flags: {CRYPTO_FLOWS, CRYPTO_BREAKER, CRYPTO_POOLED_SIZING}, params: {whale_min_usd, large_print_usd, thresholds}, breaker: {status:'pending_phase_2'}, sizing: {status:'pending_phase_3', bench_cap_nav: 0.25, gross_cap_nav: 0.50}}` (flags from `process.env`; the pending blocks are literal so the page can render §6.6 now)
  - `GET /api/crypto/positions` → `{available, rows}` from `alpaca position list -q --jq '[.[]|select(.asset_class=="crypto")]'` via `child_process.execFile('/root/go/bin/alpaca', …, {timeout: 15000})`, symbols normalised with the same rule as `universe.normalize_broker_symbol` (reimplemented in 6 lines of JS: strip `/`,`-`, must end `USD`)
  - `startEventWatcher(broadcast, {intervalMs = 30000})` — polls `SELECT MAX(ts_utc) FROM crypto_whale_events_live`; when it advances, `broadcast({type:'crypto_event', rows:[new rows]})`.

- [ ] **Step 1: Write the failing tests** (`node --test`, inject a fake `query` via `router._setQueryForTests(fn)` as `routes_pipelines` does if it exposes one; otherwise use `require.cache` injection):

```js
test('snapshot empty table → available:false', …)   // fake query returns {rows:[]} → body.available === false, status 200
test('snapshot groups market row and tickers', …)   // rows for '__MARKET__' and 'BTC-USD' → body.market.fear_greed, body.tickers[0].ticker === 'BTC-USD'
test('history clamps days to 1..30', …)
test('events clamps limit and never throws on db error', …)  // query rejects → available:false, reason:'error'
test('positions normalises BTCUSD and BTC/USD to BTC-USD', …) // stub execFile
```

- [ ] **Step 2: Run to verify fail** — `node --test tests/test_routes_crypto.test.js` → cannot find module
- [ ] **Step 3: Implement the router; mount `app.use('/api/crypto', require('./routes_crypto'))` after the pipelines mount; call `require('./routes_crypto').startEventWatcher(broadcast)` right after `module.exports.broadcast = broadcast`.**
- [ ] **Step 4: Run to verify pass** — 5 passed; `node --check src/channels/api/server.js`
- [ ] **Step 5: Commit** — `git commit -m "dashboard: /api/crypto router (snapshot, history, events, providers, risk, positions) + SSE crypto_event watcher"`

---

### Task 15: Crypto page — scaffolding, nav, regime panel move, hero

**Files:**
- Modify: `src/channels/api/server.js` — nav `:4826`, page CSS near `:3576-3581`, page div before `<script>` at `:5333` (after `#pipeline-page` closes), `_setNavActive` `:6550`, `_hideAllPages` `:6556`, `loadMarket` `:5346-5355`, `showOverview` `:5867-5874`, `loadCryptoRegime` `:6231-6236`, the 60 s regime poll `:5381-5383`, SSE handler `:5437`
- Test: manual browser check (step 6) + `node --check`

**Interfaces:**
- Produces (client JS, inside the template literal): `showCrypto()`, `_cryptoState = {initialized:false, refreshTimer:null, selected:null, snapshot:null}`, `_initCrypto()`, `_refreshCrypto()` (fetches `/api/crypto/snapshot`, `/api/regime/crypto`, `/api/crypto/providers`, `/api/crypto/risk`, `/api/crypto/positions` via `_safeFetch`), `_renderCryptoHero(snap, regime, providers)`; DOM ids `crypto-page`, `crypto-inner`, `cr-hero`, `cr-regime` (holds the moved regime panel), `cr-board`, `cr-tiles`, `cr-feed`, `cr-risk`, `cr-detail`.

- [ ] **Step 1: Nav + routing**
  - Insert `<button class="nav-btn" id="nav-crypto" onclick="showCrypto()">Crypto</button>` immediately after the `nav-market` button (:4826).
  - `_setNavActive`: list becomes `['market','crypto','portfolio','strategies','research','pipelines']`.
  - `_hideAllPages`: add `const cp = document.getElementById('crypto-page'); if (cp) cp.style.display = 'none';`.
  - `loadMarket`: `const onCrypto = document.getElementById('crypto-page')?.style.display === 'block'; const onResearch = …; const onPipelines = …; const onMarket = !onPortfolio && !onStrategies && !onCrypto && !onResearch && !onPipelines;`.
  - CSS next to `#pipeline-page`: `#crypto-page{display:none;position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;background:var(--bg)}` and `#crypto-inner{max-width:1600px;margin:0 auto;padding:14px 16px 28px;display:flex;flex-direction:column;gap:14px}`.
- [ ] **Step 2: Page markup** after `</div>` closing `#pipeline-page`: `<div id="crypto-page"><div id="crypto-inner"><div id="cr-hero" class="regime-panel"></div><div id="cr-regime"></div><div id="cr-tiles" class="ov-group"></div><div id="cr-board-wrap" class="regime-panel"><div class="regime-panel-header">Whale Flow Board</div><div id="cr-board"></div><div id="cr-detail"></div></div><div id="cr-feed" class="regime-panel"></div><div id="cr-risk" class="regime-panel"></div></div></div>`.
- [ ] **Step 3: Move the regime panel.** In `showOverview` delete `cryptoRegimePlaceholder` and its concatenation and the `loadCryptoRegime();` call. Change `loadCryptoRegime()` to target `document.getElementById('cr-regime')` and to wrap the rendered HTML in `<div id="crypto-regime-panel" class="regime-panel">…</div>`; append the three advisory tiles (`adv_btc_netflow_z_7d` → "BTC netflow z", `adv_btc_funding_8h` → "Funding 8h", `adv_delev_market` → "Deleveraging") to `_renderCryptoRegimeStructure` by reading `data.latest.features_json.adv_*` when present (reuse the `regime-feat` tile markup; show `—` when absent). The 60 s poll at :5381 gains `if (document.getElementById('crypto-page')?.style.display === 'block') loadCryptoRegime();`.
- [ ] **Step 4: Controller.** `async function showCrypto(){ _hideAllPages(); document.getElementById('crypto-page').style.display='block'; _setNavActive('crypto'); if(!_cryptoState.initialized) _initCrypto(); await _refreshCrypto(); }`; `_initCrypto` sets `initialized`, starts `setInterval` 60 s that calls `_refreshCrypto()` only while the page is `display:block`, and registers the SSE `crypto_event` type in `_openSSE`'s message switch (:5437 area) to call `_appendCryptoEvents(rows)` (Task 16). `_refreshCrypto` fetches the five endpoints in `Promise.all`, calls `loadCryptoRegime()`, `_renderCryptoHero`, then the Task 16 renderers (stubbed as no-ops until Task 16).
- [ ] **Step 5: Hero.** `_renderCryptoHero(snap, regime, providers)`: BTC and ETH `price_usd` with `ret_24h` coloured green/red (from `snap.tickers`), regime badge reusing `_STATE_CLASS[regime.latest.state]` with `conf` and `streak` and `_ageStr`, breaker chip `shadow (phase 2)`, flows freshness chip (`_ageStr(snap.ts_utc)`, red when > 45 min), provider dots (one `<span class="regime-spark-tick">` per provider; green ok, red failed, grey degraded, title = `err`). When `snap.available === false` render `<div class="empty">No flow ticks yet — enable OPENCLAW_CRYPTO_FLOWS=1</div>`.
- [ ] **Step 6: Verify in the browser.** `node --check src/channels/api/server.js`; restart user-scope johnbot (`XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot`); open `http://127.0.0.1:3000/` (SSH tunnel), click Crypto: nav order Market · Crypto · Portfolio …; page shows hero + regime panel (CRISIS badge, five feature tiles, three advisory tiles with `—`), Market overview no longer shows the crypto regime panel; stay on Crypto for 5 min: no repaint by the Market poll; browser console has zero errors. Record the check in the commit message.
- [ ] **Step 7: Commit** — `git commit -m "dashboard: Crypto page scaffold (nav right of Market), crypto regime panel moved from Market, hero strip"`

---

### Task 16: Crypto page — flow board, market tiles, event feed, risk panel, mobile

**Files:**
- Modify: `src/channels/api/server.js` (client JS from Task 15 + CSS)
- Test: manual browser check + `node --check`

**Interfaces:**
- Produces: `_renderCryptoBoard(snap)`, `_renderCryptoTiles(snap)`, `_renderCryptoFeed(rows)`, `_appendCryptoEvents(rows)`, `_renderCryptoRisk(risk, positions)`, `_selectCryptoTicker(ticker)` (fetches `/api/crypto/history/:ticker?days=7`, draws four Chart.js line charts into `#cr-detail`: netflow, OI, funding, score; destroys previous `Chart` instances held in `_cryptoState.charts`).

- [ ] **Step 1: Board.** Table over `snap.tickers` where role ≠ stable, columns: Ticker · Price · 24h · Whale score (a 6-px bar coloured `--green` < 40, `--yellow` 40–69, `--red` ≥ 70, text score + direction) · Exch netflow 24h (signed USD, compact `$1.2M`, red positive/green negative — positive = onto exchanges) · z · OI (USD) · OI Δ24h % · Funding (ann. = `funding_8h × 3 × 365`, %) · L/S · Taker · Print imb 1h · Coverage %. Benchmark rows (role `benchmark`) pinned first; click any `th` to sort (`_cryptoState.sort = {key, dir}`); click a row → `_selectCryptoTicker`. Missing field → `—`. CSS: `.cr-table{width:100%;border-collapse:collapse;font-size:12px}.cr-table th{color:var(--muted);text-align:right;padding:6px 8px;cursor:pointer;border-bottom:1px solid var(--border)}.cr-table td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--border2)}.cr-table td:first-child,.cr-table th:first-child{text-align:left}.cr-bar{display:inline-block;height:6px;border-radius:3px;vertical-align:middle;margin-right:6px}`.
- [ ] **Step 2: Tiles.** `.ov-cards` grid with cards (reuse `.ov-card`): Stablecoin supply (+7d Δ), BTC dominance, Fear & Greed, Aggregate OI, Deleveraging (badge red when `delev_market`), COT leveraged-funds net (+1w Δ), DVOL BTC, `S_m crypto by regime` → four cells showing `pending phase 3` for now with the current regime highlighted using `regime.latest.state`.
- [ ] **Step 3: Feed.** Last 50 rows from `/api/crypto/events` newest first: time (`_ageStr`), chain, symbol, USD compact, `from_owner || 'unknown wallet'` → `to_owner || 'unknown wallet'`, direction tag coloured (`exchange_in` red, `exchange_out` green, else muted). `_appendCryptoEvents(rows)` prepends and trims to 50; the page title gets a `•` prefix for 10 s on a new event.
- [ ] **Step 4: Risk panel.** Two columns: left = flags (name + `on/off` chip), params (whale min USD, large-print USD, thresholds table), `breaker: pending phase 2`, `sizing: pending phase 3 — BTC cap 25 %, crypto gross cap 50 %`; right = current crypto positions table (`ticker qty market_value unrealized_plpc`) or `No crypto positions`.
- [ ] **Step 5: Mobile.** Inside the existing `@media (max-width:768px)` block: hide board columns 6–12 via `.cr-table .cr-opt{display:none}` (add class `cr-opt` to those `th`/`td`), stack `#cr-risk` columns.
- [ ] **Step 6: Verify in the browser** (after restart): board sorts, benchmark pinned, row click draws four charts and re-click another row replaces them without console errors; tiles show `—` where the snapshot lacks a field; feed renders `No events yet` when empty; risk panel shows flags from the environment; at 700 px width the optional columns disappear. Then enable the tick for real: `OPENCLAW_CRYPTO_FLOWS=1` in `.env`, restart johnbot, wait for the next `:00/:15/:30/:45`, `tail -3 logs/crypto_flows_$(date -u +%F).log` shows the summary line, reload the page: hero freshness green, board populated.
- [ ] **Step 7: Commit** — `git commit -m "dashboard: Crypto page — whale flow board, market tiles, live event feed, risk panel, mobile"`

---

### Task 17: Post-launch verification, docs, changelog

**Files:**
- Modify: `docs/archive/changelog.md` (new entry, newest first)
- Modify: `docs/systemd/README.md` (note the in-process `crypto-flow-tick`)
- Create: `docs/reference/crypto-flow-data.md` (one page: providers, fields, thresholds, env vars — lifted from spec §2–3, §10, kept in sync)
- Modify: `CLAUDE.md` Bot Registry / Current Projects (one line: Crypto engine P1 live, flag, page)

- [ ] **Step 1: 24-hour soak check** (next day): `python3 -m system_checks --check master_freshness --json | jq '.[] | select(.name|test("crypto_"))'` both green; `psql "$POSTGRES_URI" -Atc "select provider, count(*) filter (where ok), count(*) from crypto_flow_provider_health where ts_utc > now()-interval '24 hours' group by 1 order by 1"` → every provider ≥ 95 % ok except any named in the degraded list; `psql … -Atc "select count(distinct ts_utc) from crypto_flow_snapshot where ts_utc > now()-interval '24 hours'"` ≥ 90 (96 ticks minus the compute-window skips). Peak RSS of the tick from `journalctl --user -u johnbot --since -24h | grep -i 'crypto_flow' | grep -i memory` shows no OOM kills.
- [ ] **Step 2: Write the docs listed above.** Changelog entry: date, commits, flag, page, what is pending (P2 breaker, P3 sizing).
- [ ] **Step 3: Commit** — `git commit -m "docs: crypto engine Phase 1 live — flow tick, Crypto page, soak results"` and `git push origin main`.

---

## Self-review notes (done 2026-11-06)

- **Spec coverage**: §2.1 universe (T1), §2.2 providers (T4–T10: all 13 rows of the table have an owner), §2.3 registry (T3), §2.4 storage (T2), §2.5 scheduler + registrations (T12), §3 features/score/market row (T11), §3 regime advisory (T13), §3 strategy aux kind → **deferred to Phase 4 by design** (the P1 exit criterion requires 30 days of history first; `store.read_flows_window` is the point-in-time reader the aux loader will call), §6 page (T14–T16, all six sections; §6.6 sizing/breaker parameters render as `pending` placeholders fed by `/api/crypto/risk`), §7 P1 "done when" (T17).
- **Type consistency**: `Asset`, `WhaleEvent`, `Observation`, `ProviderResult`, `MARKET_TICKER`, `FEATURE_COLS`, `write_*`/`read_*` names are used identically across T2–T14. `normalize_broker_symbol` (Python) and the JS reimplementation in T14 share the rule.
- **Review Focus** items 1–5 each have a named test in T2, T3, T5, T11, T12, T14.
- **Proportion**: the plan carries signatures, field lists, endpoint URLs, thresholds and test assertions; no function bodies beyond the three formulas the tests do not pin (score components, direction rule, skip window).
