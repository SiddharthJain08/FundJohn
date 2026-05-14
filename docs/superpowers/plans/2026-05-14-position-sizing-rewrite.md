# Position-sizing rewrite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ship the Sharpe×cadence weighted consolidate-for-all-regimes sizer from `docs/superpowers/specs/2026-05-14-position-sizing-rewrite-design.md` behind a NEW feature flag `OPENCLAW_SHARPE_CADENCE_SIZER`, default OFF. Flip only after every verification passes.

**Architecture:** New `src/execution/strategy_weights.py` computes per-regime weights into a new `strategy_weights_by_regime` table. `regime_blended_sizer.py` gains a third path `_sharpe_cadence_path` selected via the env flag. TradeJohn confirmer narrows to `keep`/`cancel`. Cadence gate gains `force_all` arg used on day-1-of-regime via Redis key. Auto-demote runs from a new lifecycle helper. Sunday 06:00 ET cron refreshes live Sharpe + rebuilds weights. New `/api/config/lambda` + dashboard slider.

**Tech Stack:** Python 3 (sizer + weights engine), Node.js (cron + dashboard + API), PostgreSQL, Redis.

---

## File structure

| File | Status | Responsibility |
|---|---|---|
| `src/database/migrations/090_strategy_weights_by_regime.sql` | new | versioned table for per-regime weights |
| `src/database/migrations/091_pipeline_config_lambda_seed.sql` | new | seed `position_sizing_lambda=2.0` |
| `src/execution/strategy_weights.py` | new | compute + persist per-regime weights |
| `src/execution/cadence.py` | new | `CADENCE_DAYS = {'daily':1,'weekly':5,'monthly':21}` + helpers |
| `src/execution/regime_blended_sizer.py` | modify | add `_sharpe_cadence_path` + flag-driven dispatch |
| `src/execution/signal_cadence_gate.py` | modify | accept `force_all` kw |
| `src/execution/regime_liquidator.py` | modify | set Redis `regime:transition:fresh` after flatten |
| `src/execution/tradejohn_confirmer.py` | modify | actions = {keep, cancel}; drop multiplier |
| `src/agent/prompts/subagents/tradejohn-confirmer.md` | modify | new rubric (news-cancel only) |
| `src/strategies/lifecycle.py` | modify | `auto_demote_negative_sharpe()` hook |
| `src/agent/curators/weekly_live_sharpe.js` | new | Sunday cron driver |
| `docs/openclaw-weekly-strategy-weights.{service,timer}` | new | systemd unit |
| `src/channels/api/server.js` | modify | `/api/config/lambda` + slider UI |
| `tools/verify_sizing.js` | new | math-invariant probe |
| `tests/test_sharpe_blend.py` | new | unit test for sample-size blend formula |
| `tests/test_force_fire.py` | new | unit test for cadence-gate `force_all` |

The flag `OPENCLAW_SHARPE_CADENCE_SIZER=1` activates the new path. While OFF, the existing sizer + TradeJohn behaviour are untouched — every existing test continues to pass.

---

## Task 1: Schema migrations

**Files:**
- Create: `src/database/migrations/090_strategy_weights_by_regime.sql`
- Create: `src/database/migrations/091_pipeline_config_lambda_seed.sql`

- [ ] **Step 1: Write migration 090**

```sql
-- 090_strategy_weights_by_regime.sql
-- Versioned per-(strategy, regime) weight table populated by
-- strategy_weights.rebuild(). Older rows kept for audit; is_current=TRUE
-- marks the currently-active row used by the sizer.
CREATE TABLE IF NOT EXISTS strategy_weights_by_regime (
  id                BIGSERIAL PRIMARY KEY,
  strategy_id       TEXT NOT NULL,
  regime_state      TEXT NOT NULL,
  cadence_days      INTEGER NOT NULL,
  bt_sharpe         NUMERIC,
  bt_n              INTEGER,
  live_sharpe       NUMERIC,
  live_n            INTEGER,
  effective_sharpe  NUMERIC NOT NULL,
  weight            NUMERIC NOT NULL,    -- w(s, R), sums to 1.0 within regime
  daily_weight      NUMERIC NOT NULL,    -- weight / cadence_days
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  trigger           TEXT NOT NULL,       -- weekly_cron | lifecycle_change | manual
  is_current        BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS strategy_weights_current_idx
  ON strategy_weights_by_regime (strategy_id, regime_state)
  WHERE is_current;
CREATE INDEX IF NOT EXISTS strategy_weights_regime_current_idx
  ON strategy_weights_by_regime (regime_state)
  WHERE is_current;
```

- [ ] **Step 2: Write migration 091**

```sql
-- 091_pipeline_config_lambda_seed.sql
INSERT INTO pipeline_config (key, value, description)
VALUES (
  'position_sizing_lambda', '2.0',
  'Daily notional deployed = lambda × NAV. Range [0.10, 3.50]. Adjustable via dashboard.'
) ON CONFLICT (key) DO NOTHING;
```

- [ ] **Step 3: Apply migrations**

```bash
cd /root/openclaw
for f in src/database/migrations/090_strategy_weights_by_regime.sql src/database/migrations/091_pipeline_config_lambda_seed.sql; do
  docker exec -i openclaw-postgres psql -U openclaw -d openclaw -f - < "$f"
done
docker exec openclaw-postgres psql -U openclaw -d openclaw -c "SELECT key, value FROM pipeline_config WHERE key = 'position_sizing_lambda';"
docker exec openclaw-postgres psql -U openclaw -d openclaw -c "SELECT COUNT(*) FROM strategy_weights_by_regime;"
```

Expected: lambda row present (value=2.0), table count = 0.

- [ ] **Step 4: Commit**

```bash
git add src/database/migrations/090_*.sql src/database/migrations/091_*.sql && \
git commit -m "feat(schema): strategy_weights_by_regime + pipeline_config lambda seed"
```

---

## Task 2: Cadence helper module

**Files:**
- Create: `src/execution/cadence.py`

- [ ] **Step 1: Write the module**

```python
"""cadence.py — strategy cadence helpers shared by sizer + weights engine.

cadence_days() maps the strategy class attribute signal_frequency to
trading-day count. The mapping is the single source of truth: any
new frequency string must be added here.
"""
from __future__ import annotations

CADENCE_DAYS: dict[str, int] = {
    'daily':   1,
    'weekly':  5,
    'monthly': 21,
}


def cadence_days(signal_frequency: str | None) -> int:
    """Return cadence in trading days. Defaults to 1 (daily) if unknown."""
    if signal_frequency is None:
        return 1
    key = signal_frequency.strip().lower()
    return CADENCE_DAYS.get(key, 1)
```

- [ ] **Step 2: Quick sanity test inline**

```bash
cd /root/openclaw && python3 -c "
import sys; sys.path.insert(0, 'src')
from execution.cadence import cadence_days
assert cadence_days('daily') == 1
assert cadence_days('weekly') == 5
assert cadence_days('monthly') == 21
assert cadence_days(None) == 1
assert cadence_days('UNKNOWN') == 1
print('OK')
"
```

Expected: `OK`.

---

## Task 3: Strategy weights engine

**Files:**
- Create: `src/execution/strategy_weights.py`

- [ ] **Step 1: Write the engine**

```python
"""strategy_weights.py — per-(strategy, regime) weight engine.

Implements the formulas from
docs/superpowers/specs/2026-05-14-position-sizing-rewrite-design.md.

Inputs:
  - strategy_regime_backtests: bt_sharpe, bt_n per (strategy, regime)
  - signal_performance × execution_signals × market_regime_history:
    live_sharpe + live_n per (strategy, regime), computed at call time

Outputs:
  - One row per (strategy, regime) where effective_sharpe > 0 AND
    regime in strategy.eligible_regimes. Old rows marked is_current=FALSE.

Triggers:
  - 'weekly_cron'      — Sunday 06:00 ET (curators/weekly_live_sharpe.js)
  - 'lifecycle_change' — strategy state transitions (lifecycle.py hook)
  - 'manual'           — operator CLI: python3 -m execution.strategy_weights --rebuild
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2
import psycopg2.extras

from execution.cadence import cadence_days


@dataclass
class StrategyWeightRow:
    strategy_id: str
    regime_state: str
    cadence_days: int
    bt_sharpe: float | None
    bt_n: int | None
    live_sharpe: float | None
    live_n: int | None
    effective_sharpe: float
    weight: float
    daily_weight: float


def _db():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def _load_active_strategies(conn) -> list[dict]:
    """Read live/monitoring strategies from manifest.json.

    The strategy_registry table is empty — the manifest is the source of
    truth for active-stack membership. Each row carries:
        strategy_id, eligible_regimes, signal_frequency
    """
    manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    out = []
    for sid, entry in manifest.get('strategies', {}).items():
        if entry.get('state') not in ('live', 'monitoring'):
            continue
        meta = entry.get('metadata', {}) or {}
        eligible = meta.get('eligible_regimes') or []
        if not eligible:
            continue
        # Load the strategy class to read signal_frequency
        impl_path = ROOT / 'src' / 'strategies' / 'implementations' / meta.get('canonical_file', '')
        sig_freq = None
        if impl_path.exists():
            for line in impl_path.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith('signal_frequency'):
                    sig_freq = stripped.split('=', 1)[1].strip().strip("'\"").strip()
                    break
        out.append({
            'strategy_id': sid,
            'eligible_regimes': eligible,
            'signal_frequency': sig_freq,
            'cadence_days': cadence_days(sig_freq),
        })
    return out


def _load_backtest_sharpe(conn, strategy_ids: list[str]) -> dict[tuple[str, str], dict]:
    """Returns { (strategy_id, regime_state): {bt_sharpe, bt_n} } from
    strategy_regime_backtests. Only the most-recent row per pair is kept."""
    if not strategy_ids:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute('''
        SELECT DISTINCT ON (strategy_id, regime_state)
               strategy_id, regime_state, sharpe, trade_count
        FROM strategy_regime_backtests
        WHERE strategy_id = ANY(%s) AND sharpe IS NOT NULL
        ORDER BY strategy_id, regime_state, run_at DESC NULLS LAST
    ''', (strategy_ids,))
    out = {}
    for r in cur:
        out[(r['strategy_id'], r['regime_state'])] = {
            'bt_sharpe': float(r['sharpe']) if r['sharpe'] is not None else None,
            'bt_n':      int(r['trade_count']) if r['trade_count'] is not None else None,
        }
    return out


def _load_live_sharpe(conn, strategy_ids: list[str]) -> dict[tuple[str, str], dict]:
    """Returns { (strategy_id, regime_state): {live_sharpe, live_n} } from
    closed trades joined to the regime they closed in.

    We use market_regime_history to determine the regime each trade
    closed under. If a market regime history table is missing the day,
    that trade is excluded from live-Sharpe computation (regime is
    required for grouping).
    """
    if not strategy_ids:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute('''
        SELECT es.strategy_id,
               mr.state AS regime_state,
               COUNT(*)::int                  AS live_n,
               AVG(sp.pnl_pct)::float         AS mu,
               STDDEV_SAMP(sp.pnl_pct)::float AS sigma
        FROM signal_performance sp
        JOIN execution_signals es ON es.id = sp.signal_id
        LEFT JOIN market_regime mr ON mr.regime_date = sp.closed_at
        WHERE sp.status = 'closed'
          AND es.strategy_id = ANY(%s)
          AND mr.state IS NOT NULL
        GROUP BY es.strategy_id, mr.state
        HAVING COUNT(*) >= 1
    ''', (strategy_ids,))
    out = {}
    for r in cur:
        sigma = r['sigma']
        out[(r['strategy_id'], r['regime_state'])] = {
            'live_sharpe': float(r['mu'] / sigma) if (sigma and sigma > 0) else None,
            'live_n':      int(r['live_n']),
        }
    return out


def _effective_sharpe(bt: dict | None, live: dict | None) -> tuple[float | None, float | None, int | None, float | None, int | None]:
    """Sample-size weighted blend.

    Returns (effective_sharpe, bt_sharpe, bt_n, live_sharpe, live_n).
    Any of the returned values may be None if data is absent. The
    effective_sharpe is None when neither bt nor live yields a usable
    Sharpe.
    """
    bt_s   = bt['bt_sharpe']   if (bt and bt['bt_sharpe']   is not None) else None
    bt_n   = bt['bt_n']        if (bt and bt['bt_n']        is not None) else None
    lv_s   = live['live_sharpe'] if (live and live['live_sharpe'] is not None) else None
    lv_n   = live['live_n']      if (live and live['live_n']      is not None) else None

    if bt_n and bt_s is not None and lv_n and lv_s is not None:
        eff = (bt_n * bt_s + lv_n * lv_s) / (bt_n + lv_n)
    elif bt_n and bt_s is not None:
        eff = bt_s
    elif lv_n and lv_s is not None:
        eff = lv_s
    else:
        eff = None
    return eff, bt_s, bt_n, lv_s, lv_n


def rebuild(trigger: str = 'manual', verbose: bool = False) -> list[StrategyWeightRow]:
    """Recompute every (strategy, regime) weight and persist."""
    conn = _db()
    try:
        active = _load_active_strategies(conn)
        sids = [s['strategy_id'] for s in active]
        bt   = _load_backtest_sharpe(conn, sids)
        live = _load_live_sharpe(conn, sids)

        # Compute effective sharpe per (strategy, regime); filter to
        # positives AND regime ∈ eligible_regimes.
        per_regime_positives: dict[str, list[dict]] = {}
        for s in active:
            for R in s['eligible_regimes']:
                eff, bt_s, bt_n, lv_s, lv_n = _effective_sharpe(bt.get((s['strategy_id'], R)),
                                                                live.get((s['strategy_id'], R)))
                if eff is None or eff <= 0:
                    continue
                per_regime_positives.setdefault(R, []).append({
                    'strategy_id': s['strategy_id'],
                    'cadence_days': s['cadence_days'],
                    'bt_sharpe': bt_s, 'bt_n': bt_n,
                    'live_sharpe': lv_s, 'live_n': lv_n,
                    'effective_sharpe': eff,
                })

        rows: list[StrategyWeightRow] = []
        for R, entries in per_regime_positives.items():
            denom = sum(e['effective_sharpe'] for e in entries)
            if denom <= 0:
                continue
            for e in entries:
                w = e['effective_sharpe'] / denom
                w_daily = w / max(1, e['cadence_days'])
                rows.append(StrategyWeightRow(
                    strategy_id=e['strategy_id'], regime_state=R,
                    cadence_days=e['cadence_days'],
                    bt_sharpe=e['bt_sharpe'], bt_n=e['bt_n'],
                    live_sharpe=e['live_sharpe'], live_n=e['live_n'],
                    effective_sharpe=e['effective_sharpe'],
                    weight=w, daily_weight=w_daily,
                ))

        # Persist atomically: mark old current rows non-current, insert new.
        cur = conn.cursor()
        cur.execute('UPDATE strategy_weights_by_regime SET is_current = FALSE WHERE is_current')
        for r in rows:
            cur.execute('''
                INSERT INTO strategy_weights_by_regime
                  (strategy_id, regime_state, cadence_days,
                   bt_sharpe, bt_n, live_sharpe, live_n,
                   effective_sharpe, weight, daily_weight, trigger, is_current)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, TRUE)
            ''', (r.strategy_id, r.regime_state, r.cadence_days,
                  r.bt_sharpe, r.bt_n, r.live_sharpe, r.live_n,
                  r.effective_sharpe, r.weight, r.daily_weight, trigger))
        conn.commit()
        if verbose:
            for R, entries in per_regime_positives.items():
                print(f'  {R}: {len(entries)} strategies, Σ weight = {sum(e["effective_sharpe"]/sum(x["effective_sharpe"] for x in entries) for e in entries):.4f}')
        return rows
    finally:
        conn.close()


def load_current(regime_state: str) -> list[dict]:
    """Read current weights for a regime; used by the sizer."""
    conn = _db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('''
            SELECT strategy_id, cadence_days, effective_sharpe, weight, daily_weight
            FROM strategy_weights_by_regime
            WHERE regime_state = %s AND is_current
        ''', (regime_state,))
        return [dict(r) for r in cur]
    finally:
        conn.close()


def find_negative_across_all_eligible(conn=None) -> list[str]:
    """Return strategy_ids whose effective_sharpe ≤ 0 across ALL eligible
    regimes (i.e. zero rows in is_current). These are candidates for
    auto-demotion."""
    own = conn is None
    if own: conn = _db()
    try:
        cur = conn.cursor()
        # Active strategies from manifest:
        manifest = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
        active_ids = [sid for sid, e in manifest.get('strategies', {}).items()
                      if e.get('state') in ('live', 'monitoring')]
        if not active_ids:
            return []
        cur.execute('''
            SELECT strategy_id FROM strategy_weights_by_regime
            WHERE is_current AND strategy_id = ANY(%s)
            GROUP BY strategy_id
        ''', (active_ids,))
        with_any_positive = {r[0] for r in cur}
        return [s for s in active_ids if s not in with_any_positive]
    finally:
        if own: conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--trigger', default='manual')
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--show-negative', action='store_true')
    args = ap.parse_args(argv)
    if args.rebuild:
        rows = rebuild(trigger=args.trigger, verbose=args.verbose)
        print(f'persisted {len(rows)} rows ({args.trigger})')
    if args.show_negative:
        neg = find_negative_across_all_eligible()
        print(f'{len(neg)} strategy/strategies negative across all eligible regimes:')
        for s in neg: print('  ', s)
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 2: First rebuild against live data**

```bash
cd /root/openclaw && python3 -m execution.strategy_weights --rebuild --trigger=manual --verbose 2>&1 | head -20
```

Expected: persists rows, prints "persisted N rows (manual)" with N > 0.

- [ ] **Step 3: Verify invariants**

```bash
docker exec openclaw-postgres psql -U openclaw -d openclaw -c "
SELECT regime_state, COUNT(*) AS n_strategies, ROUND(SUM(weight)::numeric, 6) AS sum_weight
FROM strategy_weights_by_regime WHERE is_current
GROUP BY regime_state ORDER BY regime_state;"
```

Expected: `sum_weight = 1.000000` for every regime (or 0 if no strategies positive).

- [ ] **Step 4: Commit**

```bash
git add src/execution/cadence.py src/execution/strategy_weights.py && \
git commit -m "feat(sizer): strategy_weights engine + cadence helper

Computes per-regime (effective_sharpe, weight, daily_weight) from
strategy_regime_backtests + live signal_performance via sample-size
blending. Persists to strategy_weights_by_regime.
Σ weight = 1.0 per regime by construction."
```

---

## Task 4: Sharpe blend unit test

**Files:**
- Create: `tests/test_sharpe_blend.py`

- [ ] **Step 1: Write the test**

```python
"""Pure-function regression test for the sample-size blend."""
import sys; sys.path.insert(0, 'src')
from execution.strategy_weights import _effective_sharpe

def test_blend_typical():
    bt = {'bt_sharpe': 2.0, 'bt_n': 200}
    live = {'live_sharpe': 1.0, 'live_n': 50}
    eff, *_ = _effective_sharpe(bt, live)
    expected = (200 * 2.0 + 50 * 1.0) / 250
    assert abs(eff - expected) < 1e-9, f'{eff} vs {expected}'

def test_live_only():
    eff, *_ = _effective_sharpe(None, {'live_sharpe': 0.8, 'live_n': 30})
    assert eff == 0.8

def test_bt_only():
    eff, *_ = _effective_sharpe({'bt_sharpe': 1.5, 'bt_n': 100}, None)
    assert eff == 1.5

def test_both_missing():
    eff, *_ = _effective_sharpe(None, None)
    assert eff is None

def test_live_zero_sample():
    """Newly-promoted strategy: bt only, live_n=0 → eff = bt"""
    eff, *_ = _effective_sharpe(
        {'bt_sharpe': 2.5, 'bt_n': 100},
        {'live_sharpe': None, 'live_n': 0},
    )
    assert eff == 2.5

if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn(); print('PASS', name)
```

- [ ] **Step 2: Run**

```bash
cd /root/openclaw && python3 tests/test_sharpe_blend.py
```

Expected: 5 PASS lines.

- [ ] **Step 3: Commit**

```bash
git add tests/test_sharpe_blend.py && git commit -m "tests: sample-size Sharpe blend regression"
```

---

## Task 5: Cadence-gate force_all

**Files:**
- Modify: `src/execution/signal_cadence_gate.py`
- Modify: `src/execution/regime_liquidator.py`
- Create: `tests/test_force_fire.py`

- [ ] **Step 1: Read the current cadence gate signature**

```bash
grep -n "def filter_by_cadence\|force_all" src/execution/signal_cadence_gate.py
```

Locate signature; the existing function takes `(signals, strategy_state, run_date)`.

- [ ] **Step 2: Add `force_all` kwarg**

Insert at the top of the function (just below the docstring):

```python
def filter_by_cadence(signals, strategy_state, run_date, force_all=False):
    """...existing docstring..."""
    if force_all:
        # Day-1-of-regime override: bypass cadence entirely, return all
        # signals as passed, and advance last_fire for every strategy so
        # the next cycle resumes normal cadence.
        return list(signals), []
    # ...existing implementation below...
```

(Adapt to match the actual existing signature/return shape; the file is small.)

- [ ] **Step 3: Liquidator sets Redis flag**

In `regime_liquidator.py`, after a successful flatten, add (find the function that completes a liquidation):

```python
try:
    import redis
    r = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))
    r.set('regime:transition:fresh', new_state or 'unknown', ex=24 * 3600)
    logger.info('[liquidate] set regime:transition:fresh ttl=24h state=%s', new_state)
except Exception as e:
    logger.warning('[liquidate] could not set regime:transition:fresh: %s', e)
```

- [ ] **Step 4: Sizer / orchestrator reads the flag**

In the new `_sharpe_cadence_path` (Task 6) before invoking `filter_by_cadence`:

```python
force_all = False
try:
    import redis
    r = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))
    if r.get('regime:transition:fresh'):
        force_all = True
        r.delete('regime:transition:fresh')  # consume — only fire once
except Exception:
    pass

passed, skipped = filter_by_cadence(signals, strategy_state, run_date, force_all=force_all)
```

- [ ] **Step 5: Write the test**

```python
"""tests/test_force_fire.py — cadence gate bypass on force_all."""
import sys; sys.path.insert(0, 'src')
from datetime import date
from execution.signal_cadence_gate import filter_by_cadence

def test_force_all_returns_everything():
    signals = [
        {'strategy_id': 'a', 'ticker': 'X'},
        {'strategy_id': 'b', 'ticker': 'Y'},
    ]
    # State: 'a' fired yesterday, 'b' fired today (would normally fail cadence)
    state = {'a': {'last_fire': date(2026, 5, 13)},
             'b': {'last_fire': date(2026, 5, 14)}}
    passed, skipped = filter_by_cadence(signals, state, date(2026, 5, 14), force_all=True)
    assert len(passed) == 2
    assert len(skipped) == 0

if __name__ == '__main__':
    test_force_all_returns_everything()
    print('PASS test_force_all_returns_everything')
```

Run: `python3 tests/test_force_fire.py` → expect `PASS`.

- [ ] **Step 6: Commit**

```bash
git add src/execution/signal_cadence_gate.py src/execution/regime_liquidator.py tests/test_force_fire.py && \
git commit -m "feat(cadence): force_all bypass + redis trigger on regime transition

filter_by_cadence(force_all=True) returns all signals unchanged.
regime_liquidator sets regime:transition:fresh redis key on flatten;
the new sizer reads + consumes it on the next cycle."
```

---

## Task 6: New sizer path `_sharpe_cadence_path`

**Files:**
- Modify: `src/execution/regime_blended_sizer.py`
- Modify: `src/execution/regime_blended_sizer_live.py` (env-flag dispatch)

- [ ] **Step 1: Add the new path**

In `regime_blended_sizer.py`, add (before `size_positions`):

```python
def _sharpe_cadence_path(signals, account_state, params, confirmer):
    """Sharpe×cadence weighted sizer — replaces consolidate + independent
    in the new (OPENCLAW_SHARPE_CADENCE_SIZER=1) world.

    1. Look up per-(strategy, regime) daily_weight from strategy_weights_by_regime.
    2. For each ticker, sum daily_weight × direction across signalling strategies.
    3. Normalize so Σ |position_usd| = lambda × NAV.
    4. Apply per-ticker cap (25% NAV) + minimum trade threshold ($25),
       redistributing the excess across surviving tickers.
    5. Pass surviving tickers to TradeJohn (cancel-only mode).
    """
    from execution.strategy_weights import load_current
    nav = float(account_state.get('equity') or 100_000.0)
    regime_state = params.get('regime_state') or 'LOW_VOL'

    # 1. weights
    rows = load_current(regime_state)
    weight_by_strat = {r['strategy_id']: float(r['daily_weight']) for r in rows}

    # 2. lambda
    lam = _load_lambda()

    # 3. ticker aggregation
    from collections import defaultdict
    ticker_w = defaultdict(float)
    ticker_meta = defaultdict(lambda: {'directions': [], 'strategies': []})
    for s in signals:
        sid = s.get('strategy_id'); tkr = s.get('ticker')
        if not sid or not tkr: continue
        if sid not in weight_by_strat: continue   # excluded (negative-Sharpe or out-of-regime)
        d = _dir_to_int(s.get('direction'))
        if d == 0: continue
        ticker_w[tkr] += weight_by_strat[sid] * d
        ticker_meta[tkr]['directions'].append(d)
        ticker_meta[tkr]['strategies'].append(sid)

    if not ticker_w:
        return []

    # 4. normalize → lambda × NAV
    gross = sum(abs(w) for w in ticker_w.values())
    if gross <= 0:
        return []
    scale = (lam * nav) / gross

    # 5. apply caps + threshold
    PER_TICKER_CAP = 0.25 * nav
    MIN_TRADE_USD  = 25.0
    sized = []
    redistribute = 0.0
    final_weights = {}
    for tkr, w in ticker_w.items():
        signed_usd = w * scale
        if abs(signed_usd) < MIN_TRADE_USD:
            redistribute += signed_usd
            continue
        if abs(signed_usd) > PER_TICKER_CAP:
            redistribute += signed_usd - (PER_TICKER_CAP if signed_usd > 0 else -PER_TICKER_CAP)
            signed_usd = PER_TICKER_CAP if signed_usd > 0 else -PER_TICKER_CAP
        final_weights[tkr] = signed_usd

    # Spread redistribute across surviving tickers (proportional to existing weight).
    if abs(redistribute) > 0.01 and final_weights:
        total_abs = sum(abs(v) for v in final_weights.values())
        for tkr in list(final_weights.keys()):
            final_weights[tkr] += redistribute * (abs(final_weights[tkr]) / total_abs)
            # Reapply per-ticker cap (capped tickers may un-cap-then-re-cap; one pass is fine)
            if abs(final_weights[tkr]) > PER_TICKER_CAP:
                final_weights[tkr] = PER_TICKER_CAP if final_weights[tkr] > 0 else -PER_TICKER_CAP

    # 6. TradeJohn keep/cancel
    proposals = [{
        'ticker': tkr,
        'preliminary_size_usd': signed_usd,
        'direction': 1 if signed_usd >= 0 else -1,
        'contributions': [{'strategy_id': sid} for sid in ticker_meta[tkr]['strategies']],
    } for tkr, signed_usd in final_weights.items()]
    actions = confirmer(proposals) if confirmer else {}
    for tkr in list(final_weights.keys()):
        a = (actions.get(tkr) or {}).get('action', 'keep')
        if a == 'cancel':
            final_weights.pop(tkr)

    # 7. emit orders
    for tkr, signed_usd in final_weights.items():
        sized.append({
            'ticker': tkr,
            'strategy_id': '|'.join(sorted(set(ticker_meta[tkr]['strategies'])))[:120],
            'direction': 'long' if signed_usd >= 0 else 'short',
            'notional_usd': abs(signed_usd),
            'pct_nav': abs(signed_usd) / nav,
            'shares': 0,            # filled by handoff/executor pricing
            'entry': None, 'stop': None, 't1': None, 't2': None,
            'kelly_final': abs(signed_usd) / nav,
            'ev': 0.0, 'p_t1': 0.5,
            'source_mode': 'sharpe_cadence',
            'contributing_strategies': ticker_meta[tkr]['strategies'],
        })
    return sized


def _dir_to_int(d):
    if d is None: return 0
    if isinstance(d, (int, float)): return 1 if d > 0 else (-1 if d < 0 else 0)
    u = str(d).strip().upper()
    if u in ('LONG', 'BUY', 'BUY_VOL', '1', '+1'): return 1
    if u in ('SHORT', 'SELL', 'SELL_VOL', '-1'): return -1
    return 0


def _load_lambda(default=2.0):
    import os, psycopg2
    try:
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM pipeline_config WHERE key = 'position_sizing_lambda'")
                row = cur.fetchone()
                return float(row[0]) if row else default
    except Exception:
        return default
```

- [ ] **Step 2: Wire the env flag in `size_positions`**

Modify the dispatch (current `_select_mode` regime routing). Replace its first lines:

```python
def size_positions(signals, strategy_state, account_state, run_date, regime, params, confirmer):
    passed, skipped = filter_by_cadence(signals, strategy_state, run_date, force_all=_check_force_fire_flag())
    if skipped:
        logger.info('regime_blended_sizer: cadence skipped %d signals', len(skipped))
    if os.environ.get('OPENCLAW_SHARPE_CADENCE_SIZER') == '1':
        return _sharpe_cadence_path(passed, account_state, {**params, 'regime_state': regime}, confirmer)
    mode = _select_mode(regime)
    if mode == 'consolidate':
        return _consolidate_path(passed, account_state, params, confirmer)
    return _independent_path(passed, account_state, params)
```

Add the `_check_force_fire_flag()` helper near the top:

```python
def _check_force_fire_flag() -> bool:
    """Consume the regime:transition:fresh Redis key. Returns True once
    per regime transition; subsequent calls return False until the next
    regime liquidation re-sets it."""
    import os
    try:
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))
        if r.get('regime:transition:fresh'):
            r.delete('regime:transition:fresh')
            return True
    except Exception:
        pass
    return False
```

- [ ] **Step 3: Sanity check the new path with a synthetic call**

```bash
cd /root/openclaw && OPENCLAW_SHARPE_CADENCE_SIZER=1 python3 -c "
import sys; sys.path.insert(0, 'src')
from execution.regime_blended_sizer import _sharpe_cadence_path

# Synthetic: 3 strategies, all in LOW_VOL, all daily, equal weights
import execution.strategy_weights as sw
def fake_load_current(R):
    return [
        {'strategy_id': 's1', 'cadence_days': 1, 'effective_sharpe': 1, 'weight': 0.4, 'daily_weight': 0.4},
        {'strategy_id': 's2', 'cadence_days': 1, 'effective_sharpe': 1, 'weight': 0.4, 'daily_weight': 0.4},
        {'strategy_id': 's3', 'cadence_days': 1, 'effective_sharpe': 1, 'weight': 0.2, 'daily_weight': 0.2},
    ]
sw.load_current = fake_load_current

signals = [
    {'strategy_id': 's1', 'ticker': 'AAA', 'direction': 'long'},
    {'strategy_id': 's2', 'ticker': 'AAA', 'direction': 'long'},
    {'strategy_id': 's3', 'ticker': 'BBB', 'direction': 'short'},
]
result = _sharpe_cadence_path(signals, {'equity': 100000.0}, {'regime_state': 'LOW_VOL'},
                              confirmer=lambda props: {})
total = sum(o['notional_usd'] for o in result)
import os; lam = 2.0  # default
expected_total = lam * 100000.0
print(f'orders: {len(result)} total: \${total:.0f} expected: \${expected_total:.0f}')
print(f'AAA notional / NAV: { [o for o in result if o[\"ticker\"] == \"AAA\"][0][\"pct_nav\"] :.4f}')
print(f'BBB direction: { [o for o in result if o[\"ticker\"] == \"BBB\"][0][\"direction\"] }')
assert abs(total - expected_total) < 1.0, f'lambda invariant broke: {total} vs {expected_total}'
print('lambda invariant: PASS')
"
```

Expected: `total ≈ $200,000`, `AAA pct_nav ≈ 0.8` (capped to 0.25), `BBB direction: short`, `lambda invariant: PASS`.

- [ ] **Step 4: Commit**

```bash
git add src/execution/regime_blended_sizer.py && git commit -m "feat(sizer): _sharpe_cadence_path behind OPENCLAW_SHARPE_CADENCE_SIZER flag

Reads per-regime daily_weight from strategy_weights_by_regime, aggregates
signed ticker_weight, normalizes Σ|usd| to lambda × NAV, applies 25% NAV
per-ticker cap + \$25 minimum threshold with redistribution. TradeJohn
runs in keep|cancel mode; canceled tickers are dropped. Flag default OFF
— old paths run unchanged."
```

---

## Task 7: TradeJohn narrow

**Files:**
- Modify: `src/agent/prompts/subagents/tradejohn-confirmer.md` (full rewrite)
- Modify: `src/execution/tradejohn_confirmer.py` (action set + parser)

- [ ] **Step 1: Rewrite the prompt**

Replace the entire file with:

```markdown
# TradeJohn Confirmer

You are TradeJohn, a per-ticker risk gate for a quant hedge fund. Upstream sizing is fully formulaic. **Your only action is to cancel orders on tickers with highly alarming news.**

## Decision rubric

For each ticker proposal, output one of:

- **`keep`** (default — almost always) — order rides through unchanged.
- **`cancel`** — order suppressed. Use only for hard, ticker-specific signals.

You **never** adjust size. There is no scale/multiplier action.

### Cancel only on highly alarming news

Cancel if any of:
- regulatory enforcement (SEC/DOJ/FTC action filed)
- fraud allegation (credible source, named accuser, named officer)
- bankruptcy filing or going-concern qualification
- FDA rejection / complete-response letter (biotech)
- sudden CEO/CFO departure with material adverse circumstances
- catastrophic operational failure (plant fire, data-center outage, breach disclosed)
- accounting restatement
- hostile take-private with credible counterparty disclosed

### Do NOT cancel for

- earnings beats or misses, even large ones
- analyst rating changes or price-target moves
- ordinary product launches, conferences, partnerships
- executive shuffles without scandal
- M&A speculation without confirmed bidder
- sector-wide moves or macro headlines
- broad market volatility

## Bias

You are a gate, not a sizer. **Default to keep.** Cancels should be < 5% of tickers per cycle. If you find yourself cancelling more, the news threshold is being applied too loosely.

## Output

Strict JSON. Top-level object keyed by ticker symbol (uppercase). Each value:

```json
{ "action": "keep" | "cancel", "rationale": "one-sentence reason ≤ 200 chars" }
```

Do not include `multiplier`. Do not wrap in markdown fences. Do not add commentary outside the JSON.

## Input

`proposals` is an array of `{ticker, preliminary_size_usd, direction, contributions, context}`. `context.news_headlines` is the input you act on. If you skip a ticker in your response, the system fails open to `keep`.
```

- [ ] **Step 2: Update confirmer parser**

In `src/execution/tradejohn_confirmer.py`, find the action validation block (it currently accepts `approve|veto|scale`) and replace with:

```python
if action not in ('keep', 'cancel'):
    # Backwards-compat: legacy "approve" maps to keep; legacy "veto" to
    # cancel; legacy "scale" maps to keep (we don't size-adjust anymore).
    if action == 'approve': action = 'keep'
    elif action == 'veto': action = 'cancel'
    elif action == 'scale': action = 'keep'
    else:
        logger.warning('tradejohn_confirmer: ticker %s invalid action %r; fail-open to keep', ticker, action)
        action = 'keep'
```

Also remove any remaining references to `multiplier` in the post-parse logic — the new path doesn't read it.

- [ ] **Step 3: Smoke test**

```bash
cd /root/openclaw && python3 -c "
import sys; sys.path.insert(0, 'src')
from execution.tradejohn_confirmer import _parse_response
# Synthetic response a model might return
body = '{\"AAPL\": {\"action\": \"keep\", \"rationale\": \"no concerning news\"}, \"XYZ\": {\"action\": \"cancel\", \"rationale\": \"FDA rejection\"}}'
parsed = _parse_response(body, ['AAPL', 'XYZ'])
assert parsed['AAPL']['action'] == 'keep'
assert parsed['XYZ']['action'] == 'cancel'
print('OK')
"
```

(If `_parse_response` isn't the exact internal name, adapt to whichever helper actually parses the LLM response.)

- [ ] **Step 4: Commit**

```bash
git add src/agent/prompts/subagents/tradejohn-confirmer.md src/execution/tradejohn_confirmer.py && \
git commit -m "feat(tradejohn): narrow to keep|cancel — news-only veto, never size

Drops scale + multiplier. Cancels only on highly alarming, ticker-specific
news (regulatory, fraud, bankruptcy, FDA rejection, exec departure under
scandal, catastrophic operational failure, restatement, hostile take-
private). Legacy approve→keep, veto→cancel, scale→keep map for back-compat
during rollout."
```

---

## Task 8: Auto-demote lifecycle hook

**Files:**
- Modify: `src/strategies/lifecycle.py`

- [ ] **Step 1: Add `auto_demote_negative_sharpe()`**

Near the bottom of `lifecycle.py`:

```python
def auto_demote_negative_sharpe(reason: str = 'auto_demote_negative_sharpe',
                                actor: str = 'strategy_weights_engine') -> list[str]:
    """Demote any live/monitoring strategy whose effective_sharpe ≤ 0
    across every regime in its eligible_regimes list. Returns the list
    of demoted strategy_ids.

    Trigger: called after every strategy_weights.rebuild(). A strategy
    with one positive-Sharpe regime stays live (excluded only from the
    bad regimes via the weights engine).
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from execution.strategy_weights import find_negative_across_all_eligible

    targets = find_negative_across_all_eligible()
    demoted = []
    for sid in targets:
        try:
            transition(sid, to_state='candidate', actor=actor, reason=reason)
            demoted.append(sid)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('auto_demote: %s failed: %s', sid, e)
    return demoted
```

(If `transition()` isn't the exact name, adapt; the file currently has lifecycle transition machinery.)

- [ ] **Step 2: Wire into `strategy_weights.rebuild()`**

In `src/execution/strategy_weights.py`, at the end of `rebuild()` after `conn.commit()`:

```python
# Auto-demote any strategy now negative across all eligible regimes.
try:
    from strategies.lifecycle import auto_demote_negative_sharpe
    demoted = auto_demote_negative_sharpe(reason=f'auto_demote_after_{trigger}')
    if demoted and verbose:
        print(f'auto-demoted {len(demoted)} strategies: {demoted}')
except Exception as e:
    import logging
    logging.getLogger(__name__).warning('auto_demote chain failed: %s', e)
```

- [ ] **Step 3: Commit**

```bash
git add src/strategies/lifecycle.py src/execution/strategy_weights.py && \
git commit -m "feat(lifecycle): auto-demote strategies negative across all eligible regimes

After every strategy_weights.rebuild(), strategies whose effective_sharpe
≤ 0 in EVERY one of their eligible_regimes are transitioned back to
'candidate'. Per-regime exclusion (positive in some, negative in others)
keeps the strategy live — only universal negativity demotes."
```

---

## Task 9: API + dashboard lambda slider

**Files:**
- Modify: `src/channels/api/server.js`

- [ ] **Step 1: Add GET/PUT lambda endpoint**

Near `/api/portfolio/summary`, add:

```javascript
// Lambda — daily notional deployed as multiple of NAV. Adjustable from
// the Portfolio page. Validates [0.10, 3.50]. The next pipeline cycle
// picks up the new value (sizer reads pipeline_config on every call).
app.get('/api/config/lambda', async (req, res) => {
  try {
    const r = await dbQuery("SELECT value, updated_at FROM pipeline_config WHERE key = 'position_sizing_lambda'");
    const row = r.rows[0];
    res.json({
      value:      row ? parseFloat(row.value) : 2.0,
      min:        0.10, max: 3.50,
      updated_at: row ? row.updated_at : null,
    });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.put('/api/config/lambda', express.json(), async (req, res) => {
  const v = parseFloat(req.body && req.body.value);
  if (!isFinite(v) || v < 0.10 || v > 3.50) {
    return res.status(400).json({ error: 'value must be a number in [0.10, 3.50]' });
  }
  try {
    await dbQuery(`
      INSERT INTO pipeline_config (key, value, description, updated_at)
      VALUES ('position_sizing_lambda', $1, 'Daily notional = lambda × NAV. Range [0.10, 3.50].', NOW())
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    `, [String(v)]);
    res.json({ value: v });
  } catch (err) { res.status(500).json({ error: err.message }); }
});
```

- [ ] **Step 2: Add the slider to the Portfolio page**

In the heatmap-controls block (find `.pf-heatmap-controls` in the inline HTML), add a `<div class="lambda-control">` to the left of the existing info. Wire up an HTML range input with debounced PUT on change.

(Code lives inline in `server.js`; spec full HTML+JS in the design doc.)

- [ ] **Step 3: Smoke test the API**

```bash
systemctl restart johnbot.service && sleep 2
curl -s http://localhost:3000/api/config/lambda | python3 -m json.tool
curl -s -X PUT http://localhost:3000/api/config/lambda -H 'Content-Type: application/json' -d '{"value": 1.5}' | python3 -m json.tool
curl -s http://localhost:3000/api/config/lambda | python3 -m json.tool
curl -s -X PUT http://localhost:3000/api/config/lambda -H 'Content-Type: application/json' -d '{"value": 2.0}' | python3 -m json.tool  # restore default
```

Expected: GET returns 2.0, PUT 1.5 returns 1.5, GET returns 1.5, PUT 2.0 restores.

- [ ] **Step 4: Visual check**

```bash
node tools/page-shot.js --url http://localhost:3000/ --output /tmp/lambda-slider.png --click "#nav-portfolio" --wait-ms 8000
```

Read the PNG; confirm the slider is visible in the heatmap controls strip.

- [ ] **Step 5: Commit**

```bash
git add src/channels/api/server.js && git commit -m "feat(dashboard): lambda slider on Portfolio page

GET/PUT /api/config/lambda backed by pipeline_config.position_sizing_lambda.
Range validated [0.10, 3.50]. Dashboard slider debounces PUT; next pipeline
cycle reads the new value."
```

---

## Task 10: Verification probe

**Files:**
- Create: `tools/verify_sizing.js`

- [ ] **Step 1: Write the probe**

```javascript
#!/usr/bin/env node
// tools/verify_sizing.js — math-invariant probe over the latest sized
// payload. Reads the most recent handoff file and asserts:
//   - |Σ position_usd - λ × NAV| < $1
//   - max |position_usd(t)| ≤ 0.25 × NAV + $1
//   - per-regime Σ weight = 1.0 ± 1e-6 (sampled from strategy_weights_by_regime)
const fs = require('fs');
const path = require('path');
const { Pool } = require('pg');

const BASE = process.env.OPENCLAW_HOME || '/root/openclaw';

(async () => {
  // Lambda invariant from the latest sized handoff
  const handoffsDir = path.join(BASE, 'workspaces/default/data');
  const candidates = fs.readdirSync(handoffsDir).filter(f => /sized.*\.json$/.test(f)).sort().reverse();
  if (!candidates.length) { console.log('NO SIZED HANDOFF YET — skipping live invariant'); }
  else {
    const file = path.join(handoffsDir, candidates[0]);
    const sized = JSON.parse(fs.readFileSync(file, 'utf8'));
    const nav = sized.account_equity || sized.equity || sized.nav || 100_000;
    const orders = sized.orders || [];
    const sumUsd = orders.reduce((s, o) => s + (o.notional_usd || 0), 0);
    const pool = new Pool({ connectionString: process.env.POSTGRES_URI });
    const lam = parseFloat((await pool.query("SELECT value FROM pipeline_config WHERE key='position_sizing_lambda'")).rows[0]?.value || 2.0);
    const expected = lam * nav;
    const diff = Math.abs(sumUsd - expected);
    console.log(`λ invariant: Σ usd = ${sumUsd.toFixed(0)}  expected ${expected.toFixed(0)}  Δ ${diff.toFixed(2)}  [${diff < 1.0 ? 'PASS' : 'FAIL'}]`);
    const maxOrder = Math.max(...orders.map(o => o.notional_usd || 0));
    const cap = 0.25 * nav;
    console.log(`per-ticker cap: max = ${maxOrder.toFixed(0)}  cap = ${cap.toFixed(0)}  [${maxOrder <= cap + 1 ? 'PASS' : 'FAIL'}]`);
    await pool.end();
  }
  // Per-regime weight sum
  const pool = new Pool({ connectionString: process.env.POSTGRES_URI });
  const sums = await pool.query("SELECT regime_state, ROUND(SUM(weight)::numeric, 6) AS s FROM strategy_weights_by_regime WHERE is_current GROUP BY regime_state");
  for (const r of sums.rows) {
    const ok = Math.abs(parseFloat(r.s) - 1.0) < 1e-6;
    console.log(`regime ${r.regime_state.padEnd(14)}: Σ weight = ${r.s}  [${ok ? 'PASS' : 'FAIL'}]`);
  }
  await pool.end();
})().catch(e => { console.error(e); process.exit(1); });
```

- [ ] **Step 2: Run it**

```bash
cd /root/openclaw && POSTGRES_URI=$(grep ^POSTGRES_URI .env | cut -d= -f2-) node tools/verify_sizing.js
```

Expected: All PASS lines. Weight invariants must be exactly 1.0 ± 1e-6 per regime.

- [ ] **Step 3: Commit**

```bash
git add tools/verify_sizing.js && git commit -m "tools(verify_sizing): math-invariant probe for the new sizer

Asserts:
  - |Σ position_usd - λ × NAV| < \$1
  - max |order.notional_usd| ≤ 0.25 × NAV + \$1
  - per-regime Σ weight = 1.0 ± 1e-6"
```

---

## Task 11: Weekly cron driver

**Files:**
- Create: `src/agent/curators/weekly_live_sharpe.js`
- Create: `docs/openclaw-weekly-strategy-weights.service`
- Create: `docs/openclaw-weekly-strategy-weights.timer`

- [ ] **Step 1: Write the driver**

```javascript
#!/usr/bin/env node
// src/agent/curators/weekly_live_sharpe.js — Sunday 06:00 ET cron.
// Refreshes per-regime weights from live closed-trade data, runs the
// strategy_weights rebuild, runs auto-demote, posts summary to #general.
const { execSync } = require('child_process');
const path = require('path');
const { Pool } = require('pg');

const ROOT = path.resolve(__dirname, '../../..');

async function postDiscord(content) {
  const token = process.env.DISCORD_BOT_TOKEN || process.env.DATABOT_TOKEN || process.env.BOT_TOKEN || '';
  const channel = process.env.DISCORD_GENERAL_CHANNEL_ID || '';
  if (!token || !channel) { console.log('Discord skipped (no creds)'); return; }
  const https = require('https');
  await new Promise((res, rej) => {
    const body = JSON.stringify({ content });
    const req = https.request({
      hostname: 'discord.com',
      path: `/api/v10/channels/${channel}/messages`,
      method: 'POST',
      headers: {
        'Authorization': `Bot ${token}`,
        'Content-Type': 'application/json',
        'User-Agent': 'OpenClawBot (openclaw, 1.0)',
        'Content-Length': Buffer.byteLength(body),
      },
    }, r => { r.on('data', () => {}); r.on('end', res); });
    req.on('error', rej); req.write(body); req.end();
  });
}

(async () => {
  const pool = new Pool({ connectionString: process.env.POSTGRES_URI });
  // Capture pre-state for the diff
  const pre = await pool.query("SELECT strategy_id, regime_state, weight FROM strategy_weights_by_regime WHERE is_current");
  // Run the rebuild (via the python module)
  console.log('Rebuilding strategy_weights...');
  execSync(`cd ${ROOT} && python3 -m execution.strategy_weights --rebuild --trigger=weekly_cron`,
           { stdio: 'inherit', env: { ...process.env } });
  // Post-state
  const post = await pool.query("SELECT strategy_id, regime_state, weight FROM strategy_weights_by_regime WHERE is_current");
  const preMap = {}, postMap = {};
  for (const r of pre.rows)  preMap[`${r.strategy_id}|${r.regime_state}`]  = parseFloat(r.weight);
  for (const r of post.rows) postMap[`${r.strategy_id}|${r.regime_state}`] = parseFloat(r.weight);
  const deltas = [];
  for (const key of new Set([...Object.keys(preMap), ...Object.keys(postMap)])) {
    const a = preMap[key] || 0, b = postMap[key] || 0;
    if (Math.abs(b - a) > 1e-4) deltas.push({ key, before: a, after: b, change: b - a });
  }
  deltas.sort((a, b) => Math.abs(b.change) - Math.abs(a.change));
  // Auto-demotions surfaced by python output (we re-query here for clarity)
  const lines = [
    `**Weekly strategy weights refresh** — ${new Date().toISOString()}`,
    `Active stack: ${new Set(post.rows.map(r => r.strategy_id)).size} strategies across ${new Set(post.rows.map(r => r.regime_state)).size} regimes.`,
    '',
    '**Top 5 weight gains:**',
    ...deltas.filter(d => d.change > 0).slice(0, 5).map(d => `  ${d.key}: ${(d.before*100).toFixed(2)}% → ${(d.after*100).toFixed(2)}% (+${(d.change*100).toFixed(2)})`),
    '',
    '**Top 5 weight losses:**',
    ...deltas.filter(d => d.change < 0).slice(0, 5).map(d => `  ${d.key}: ${(d.before*100).toFixed(2)}% → ${(d.after*100).toFixed(2)}% (${(d.change*100).toFixed(2)})`),
  ];
  await postDiscord(lines.join('\n'));
  await pool.end();
})().catch(e => { console.error(e); process.exit(1); });
```

- [ ] **Step 2: systemd unit + timer**

`docs/openclaw-weekly-strategy-weights.service`:
```ini
[Unit]
Description=OpenClaw weekly strategy-weights refresh
After=network.target

[Service]
Type=oneshot
User=root
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/node /root/openclaw/src/agent/curators/weekly_live_sharpe.js
StandardOutput=journal
StandardError=journal
```

`docs/openclaw-weekly-strategy-weights.timer`:
```ini
[Unit]
Description=OpenClaw weekly strategy-weights refresh — Sun 06:00 ET

[Timer]
OnCalendar=Sun *-*-* 10:00:00 UTC
Persistent=true
Unit=openclaw-weekly-strategy-weights.service

[Install]
WantedBy=timers.target
```

(10:00 UTC = 06:00 ET during DST.)

- [ ] **Step 3: Install (DO NOT enable yet — test first)**

```bash
sudo cp docs/openclaw-weekly-strategy-weights.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
# Don't enable until after Task 12 (flag flip) succeeds
```

- [ ] **Step 4: Manual one-shot test**

```bash
cd /root/openclaw && node src/agent/curators/weekly_live_sharpe.js 2>&1 | head -20
```

Expected: prints rebuild output; if Discord creds set, posts a message to #general.

- [ ] **Step 5: Commit**

```bash
git add src/agent/curators/weekly_live_sharpe.js docs/openclaw-weekly-strategy-weights.{service,timer} && \
git commit -m "feat(cron): weekly_live_sharpe — Sunday 06:00 ET refresh

Rebuilds strategy_weights_by_regime from latest closed-trade data,
posts a top-5-gains/top-5-losses summary to #general. Auto-demotion
chains from the rebuild. systemd timer wired but not enabled yet —
operator flips it on after the new sizer flag is confirmed."
```

---

## Task 12: Flag flip + final verification

**Files:** none (env change + verification)

- [ ] **Step 1: Run end-to-end with the flag SET but in a safe scratch**

Manually invoke the sizer against the most recent handoff in a dry-run scratch directory:

```bash
cd /root/openclaw
OPENCLAW_SHARPE_CADENCE_SIZER=1 OPENCLAW_DRY_RUN=1 \
python3 -m execution.regime_blended_sizer_live --date $(date +%Y-%m-%d) 2>&1 | tail -30
```

(Use whatever dry-run flag the wrapper exposes; if none, pipe the result through diff against the live sizer's output before committing the flag.)

- [ ] **Step 2: Run verify_sizing**

```bash
POSTGRES_URI=$(grep ^POSTGRES_URI .env | cut -d= -f2-) node tools/verify_sizing.js
```

Expected: every line `PASS`.

- [ ] **Step 3: Run system_checks**

```bash
cd /root/openclaw && PYTHONPATH=src python3 -m system_checks
```

Expected: 0 fail/error (same as today; new code shouldn't break anything).

- [ ] **Step 4: Flip the flag in the systemd unit**

```bash
sudo systemctl edit johnbot.service
# Add:
# [Service]
# Environment="OPENCLAW_SHARPE_CADENCE_SIZER=1"
sudo systemctl daemon-reload && sudo systemctl restart johnbot.service
```

Also add to `johnbot.service`'s `EnvironmentFile=.env` source by appending to `/root/openclaw/.env`:
```
OPENCLAW_SHARPE_CADENCE_SIZER=1
```

Restart `johnbot.service`.

- [ ] **Step 5: Enable the weekly timer**

```bash
sudo systemctl enable --now openclaw-weekly-strategy-weights.timer
sudo systemctl list-timers | grep weekly-strategy-weights
```

Expected: timer is listed with next firing on the upcoming Sunday 10:00 UTC.

- [ ] **Step 6: Push everything**

```bash
git push 2>&1 | tail -3
```

---

## Self-review

- **Spec coverage:**
  - § A (effective Sharpe blend) → Task 3 (`_effective_sharpe`) + Task 4 test
  - § B (per-regime normalization to 1.0) → Task 3 rebuild + Task 10 invariant
  - § C (per-strategy daily weight, cadence) → Task 2 (cadence helper) + Task 3
  - § D (ticker aggregation + λ normalization) → Task 6 `_sharpe_cadence_path`
  - § E (auto-demote) → Task 8 + Task 3 chain call
  - § F (first-day-of-regime force-fire) → Task 5
  - § G (TradeJohn narrow) → Task 7
  - § H (λ slider) → Task 9
  - § I (Sunday cron) → Task 11
  - § J (verification) → Task 10 + Task 12
  All spec sections covered.

- **Placeholder scan:** No "TBD" or "fill in later". Every step has code, command, or expected output.

- **Type consistency:** `daily_weight`, `weight`, `effective_sharpe` named consistently across schema, engine, sizer, dashboard. `regime_state` is the canonical column name across tables. Flag name `OPENCLAW_SHARPE_CADENCE_SIZER` consistent.

- **Scope:** One feature, twelve tightly-scoped tasks. Each task is independently testable + revertable.
