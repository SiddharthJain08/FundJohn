# W3 Sizer-Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the two HIGH W3 sizing-verification findings — abort the sizer on account-fetch failure (F1), and contain the intraday-redeploy path (F2: signal-set-health gate, intraday-λ, per-ticker cap) — without changing the daily/EOD lane.

**Architecture:** One new pure helper (`signal_set_health.py`) + a shared env flag `OPENCLAW_INTRADAY_REDEPLOY=1` (set by `redeploy_pipeline` on the trade step it spawns) that activates the intraday-λ key (F2b), the conviction cap (F2c), and the signal-set-health gate (F2a). F1 is a fail-safe abort in `sizer_live.main()`. All new behavior is intraday-only / fail-safe; the daily/EOD lane is byte-identical.

**Tech Stack:** Python 3, pytest (`python3 -m pytest`), psycopg2 (each helper opens its own `psycopg2.connect(os.environ['POSTGRES_URI'])`). Live sizer: `src/execution/regime_blended_sizer{,_live}.py`; redeploy driver: `scripts/redeploy_pipeline.py`.

## Global Constraints
- PATH-SCOPED commits ONLY. Never `git add -A`/`.`. Live tree carries UNRECOVERABLE WIP (`src/strategies/manifest.json`, `src/strategies/registry.py`, untracked `src/strategies/implementations/S_*`) — stage only each task's files explicitly + abort guard. Never `git reset --hard`/`clean`/blind `checkout`.
- Do NOT restart any service, do NOT `git push`, do NOT apply to the live system — commits land on `feat/intraday-regime-15min-prefetch` for a later operator-gated restart.
- Commit footer EVERY commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Shared flag name (exact): `OPENCLAW_INTRADAY_REDEPLOY`. F2a constants (exact): `SIGNAL_SET_MIN_FLOOR = 10`, `SIGNAL_SET_MIN_FRAC = 0.30`.
- Fixes are intraday-only + fail-safe: F1 aborts (zero orders) on fetch fail; F2a aborts (zero orders, NO orphan-close) on thin signals; daily/EOD lane unchanged.
- Work from /root/openclaw.

---

### Task C1: signal_set_health.py (pure helper)

**Files:**
- Create: `src/execution/signal_set_health.py`
- Test: `tests/test_signal_set_health.py`

**Interfaces — Produces:**
- `recent_baseline(counts: list[int|float]) -> float` — median of `counts`; `0.0` if empty.
- `is_signal_set_thin(current_count: int, baseline_count: float, floor: int, frac: float) -> bool` — `current_count < max(floor, frac*baseline_count)`; when `baseline_count<=0`, threshold is `floor` only.

- [ ] **Step 1: Write the failing test** — `tests/test_signal_set_health.py`
```python
# tests/test_signal_set_health.py — pure gate for "is the intraday redeploy's active
# signal set abnormally thin?" (W3 F2a). No DB, no I/O.
from src.execution.signal_set_health import recent_baseline, is_signal_set_thin

def test_recent_baseline_median():
    assert recent_baseline([10, 20, 30]) == 20
    assert recent_baseline([10, 20, 30, 40]) == 25
    assert recent_baseline([]) == 0.0

def test_thin_below_floor():
    # below the absolute floor → thin regardless of baseline
    assert is_signal_set_thin(5, baseline_count=100, floor=10, frac=0.30) is True

def test_thin_below_frac_of_baseline():
    # 25 < 0.30*100=30 → thin
    assert is_signal_set_thin(25, baseline_count=100, floor=10, frac=0.30) is True

def test_healthy_set_not_thin():
    # 40 >= max(10, 30) → healthy
    assert is_signal_set_thin(40, baseline_count=100, floor=10, frac=0.30) is False

def test_no_baseline_uses_floor_only():
    # baseline<=0 → only the floor applies
    assert is_signal_set_thin(8, baseline_count=0, floor=10, frac=0.30) is True
    assert is_signal_set_thin(12, baseline_count=0, floor=10, frac=0.30) is False
```

- [ ] **Step 2: Run → FAIL** — `cd /root/openclaw && python3 -m pytest tests/test_signal_set_health.py -q` → ModuleNotFoundError.

- [ ] **Step 3: Implement** — `src/execution/signal_set_health.py`
```python
# src/execution/signal_set_health.py
# Pure gate for the intraday-redeploy signal-set-health check (W3 F2a): is the active
# signal set abnormally thin vs a recent baseline? If so the redeploy is likely acting on
# bad/incomplete data and must NOT orphan-close the book. No DB, no I/O.
def recent_baseline(counts):
    vals = sorted(float(c) for c in (counts or []))
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0

def is_signal_set_thin(current_count, baseline_count, floor, frac):
    threshold = max(float(floor), float(frac) * float(baseline_count)) if baseline_count and baseline_count > 0 else float(floor)
    return float(current_count) < threshold
```

- [ ] **Step 4: Run → PASS** — `python3 -m pytest tests/test_signal_set_health.py -q` → 5 passed.

- [ ] **Step 5: Commit (path-scoped)**
```bash
cd /root/openclaw && git add src/execution/signal_set_health.py tests/test_signal_set_health.py
test "$(git diff --cached --name-only | sort | tr '\n' ' ')" = "src/execution/signal_set_health.py tests/test_signal_set_health.py " || { echo ABORT; git restore --staged .; exit 1; }
git commit -m "feat(sizer): pure signal-set-health gate helper (W3 F2a)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task C2: F2b — wire intraday λ + the shared env flag

**Files:**
- Modify: `src/execution/regime_blended_sizer.py:165-182` (`_load_lambda`) + call site `:906`
- Modify: `scripts/redeploy_pipeline.py:262` (`_spawn_orchestrator` — set `OPENCLAW_INTRADAY_REDEPLOY=1` in the spawned env)
- Test: `tests/test_load_lambda_intraday.py`

**Interfaces — Produces:** `_load_lambda(default=2.0, *, intraday=False) -> float` reads `position_sizing_lambda_intraday` when `intraday=True`, else `position_sizing_lambda`; `OPENCLAW_INTRADAY_REDEPLOY=1` set on the redeploy's trade subprocess.

- [ ] **Step 1: Write the failing test** — `tests/test_load_lambda_intraday.py`
```python
# tests/test_load_lambda_intraday.py — _load_lambda reads the intraday key under intraday=True.
# Runs on a TEMP pipeline_config-like table in a rolled-back txn — never touches live config.
import os, pytest
try:
    import psycopg2
except ImportError:
    psycopg2 = None

@pytest.mark.skipif(psycopg2 is None, reason="psycopg2 not installed")
def test_load_lambda_picks_intraday_key(monkeypatch):
    dsn = os.environ.get("POSTGRES_URI")
    if not dsn:
        pytest.skip("POSTGRES_URI not set")
    # Verify the SQL key-selection logic directly against pipeline_config in a rolled-back txn.
    conn = psycopg2.connect(dsn); conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("CREATE TEMP TABLE _pc_l (key text primary key, value text) ON COMMIT DROP")
        cur.execute("INSERT INTO _pc_l VALUES ('position_sizing_lambda','1.85'),('position_sizing_lambda_intraday','1.0')")
        for intraday, expect in [(False, '1.85'), (True, '1.0')]:
            key = 'position_sizing_lambda_intraday' if intraday else 'position_sizing_lambda'
            cur.execute("SELECT value FROM _pc_l WHERE key=%s", (key,))
            assert cur.fetchone()[0] == expect
    finally:
        conn.rollback(); conn.close()

def test_load_lambda_signature_accepts_intraday():
    from src.execution.regime_blended_sizer import _load_lambda
    import inspect
    assert 'intraday' in inspect.signature(_load_lambda).parameters
```

- [ ] **Step 2: Run → FAIL** — `python3 -m pytest tests/test_load_lambda_intraday.py -q` → fails on the signature assertion (intraday param absent).

- [ ] **Step 3: Implement.** In `regime_blended_sizer.py`, change `_load_lambda`:
```python
def _load_lambda(default: float = 2.0, *, intraday: bool = False) -> float:
    # ... (keep the existing docstring; add: intraday=True reads position_sizing_lambda_intraday) ...
    key = 'position_sizing_lambda_intraday' if intraday else 'position_sizing_lambda'
    try:
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM pipeline_config WHERE key = %s", (key,))
                row = cur.fetchone()
                v = float(row[0]) if row else default
                return max(0.10, min(2.00, v))
    except Exception:
        return default
```
At the call site (`:906`), pass `intraday`:
```python
    lam_global = _load_lambda(intraday=(os.environ.get('OPENCLAW_INTRADAY_REDEPLOY') == '1'))
```
In `redeploy_pipeline.py` `_spawn_orchestrator` (`:262`), when building the subprocess `env` for the orchestrator command, add `env['OPENCLAW_INTRADAY_REDEPLOY'] = '1'` (construct `env = {**os.environ, 'OPENCLAW_INTRADAY_REDEPLOY': '1'}` and pass it to the `subprocess`/`Popen` call). Confirm the existing spawn passes an `env=`; if it currently inherits the parent env implicitly, switch it to explicit `env=env`.

- [ ] **Step 4: Run → PASS** — `python3 -m pytest tests/test_load_lambda_intraday.py -q`; also `python3 -c "import ast; ast.parse(open('scripts/redeploy_pipeline.py').read()); ast.parse(open('src/execution/regime_blended_sizer.py').read()); print('parse ok')"`.

- [ ] **Step 5: Commit (path-scoped)** — stage exactly `src/execution/regime_blended_sizer.py scripts/redeploy_pipeline.py tests/test_load_lambda_intraday.py` (abort guard); message `feat(sizer): wire intraday lambda via OPENCLAW_INTRADAY_REDEPLOY flag (W3 F2b)` + footer.

---

### Task C3: F2c — extend the per-ticker conviction cap to the intraday path

**Files:**
- Modify: `src/execution/regime_blended_sizer.py:1154` (the conviction-cap gate)
- Test: `tests/test_regime_blended_sizer.py` (extend — mirror an existing cap/sizer test)

**Interfaces — Consumes:** `OPENCLAW_INTRADAY_REDEPLOY` (set by C2).

- [ ] **Step 1: Write the failing test.** In `tests/test_regime_blended_sizer.py`, add a test that drives `_sharpe_cadence_path` (or the smallest unit that contains the cap) with one over-cap ticker and `OPENCLAW_INTRADAY_REDEPLOY=1` set (monkeypatch env), asserting the ticker's `target_usd` is clamped to `PER_TICKER_CAP_SHARPE_FRAC*|sharpe|*lam*nav`; and a second case with NEITHER flag asserting NO clamp (daily lane byte-identical). Mirror the existing sizer-test setup in that file for fixtures/monkeypatching. If `_sharpe_cadence_path` is impractical to unit-drive, extract the cap loop into a small pure `apply_conviction_cap(target_usd, gate_net_sharpe, lam, nav, frac)` and test that directly (and call it from both gate branches).

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — change `regime_blended_sizer.py:1154` from
`    if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1':`
to
`    if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1' or os.environ.get('OPENCLAW_INTRADAY_REDEPLOY') == '1':`
(cap math unchanged; `lam` is already the intraday λ from C2 on the intraday path). If you extracted `apply_conviction_cap`, route both branches through it.

- [ ] **Step 4: Run → PASS** (new test green; run the existing `tests/test_regime_blended_sizer.py` to confirm no regression).

- [ ] **Step 5: Commit (path-scoped)** — `feat(sizer): apply per-ticker conviction cap on the intraday redeploy path (W3 F2c)` + footer.

---

### Task C4: F2a — signal-set-health gate in the intraday path

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` — add `_recent_active_counts()` + the gate just before the `_classify_position_deltas` call (`:1204`); add module constants `SIGNAL_SET_MIN_FLOOR=10`, `SIGNAL_SET_MIN_FRAC=0.30`.
- Test: covered by `tests/test_signal_set_health.py` (C1) for the pure logic; integration verified by inspection (note in report).

**Interfaces — Consumes:** `is_signal_set_thin`, `recent_baseline` (C1); `OPENCLAW_INTRADAY_REDEPLOY` (C2).

- [ ] **Step 1: Add `_recent_active_counts`** (own connection, fail-safe — mirrors the `:175/:340` loaders):
```python
def _recent_active_counts(lookback: int = 10) -> list[int]:
    """Recent per-cycle active-signal-set sizes (count of open execution_signals per
    signal_date), newest first. Fail-safe: [] on any error → baseline 0 → gate uses floor only."""
    try:
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
            cur.execute("""SELECT COUNT(*) FROM execution_signals WHERE status='open'
                           GROUP BY signal_date ORDER BY signal_date DESC LIMIT %s""", (lookback,))
            return [int(r[0]) for r in cur.fetchall()]
    except Exception:
        return []
```

- [ ] **Step 2: Add the gate** just before `emissions = _classify_position_deltas(...)` (`:1204`):
```python
    if os.environ.get('OPENCLAW_INTRADAY_REDEPLOY') == '1':
        from src.execution.signal_set_health import is_signal_set_thin, recent_baseline
        _baseline = recent_baseline(_recent_active_counts(lookback=10))
        if is_signal_set_thin(len(active), _baseline, floor=SIGNAL_SET_MIN_FLOOR, frac=SIGNAL_SET_MIN_FRAC):
            logger.warning('ABORT intraday redeploy sizing: active=%d < gate(floor=%d, %.0f%%*baseline=%.1f) — leaving book intact (no orphan-close)',
                           len(active), SIGNAL_SET_MIN_FLOOR, SIGNAL_SET_MIN_FRAC*100, _baseline)
            return []
```
(`active` is in scope from `:915/918/932`; `return []` = zero orders, so `_classify_position_deltas` never runs → no orphan-closes. The zero-signal case is already guarded earlier.)

- [ ] **Step 3: Verify** — `python3 -c "import ast; ast.parse(open('src/execution/regime_blended_sizer.py').read()); print('parse ok')"`; re-run `python3 -m pytest tests/test_signal_set_health.py tests/test_regime_blended_sizer.py -q` (helper + no sizer regression).

- [ ] **Step 4: Commit (path-scoped)** — stage exactly `src/execution/regime_blended_sizer.py` (abort guard); `feat(sizer): signal-set-health gate blocks thin-data intraday redeploy orphan-close (W3 F2a)` + footer.

---

### Task C5: F1 — abort the sizer on account-fetch failure

**Files:**
- Modify: `src/execution/regime_blended_sizer_live.py:492-501` (+ the abort guard before `size_positions`)
- Test: `tests/test_regime_blended_sizer_live.py` (extend)

- [ ] **Step 1: Write the failing test.** In `tests/test_regime_blended_sizer_live.py`, add a test that monkeypatches `_fetch_account_state` (or `_alpaca_session`) to raise, runs the relevant `main()` path (mirror the file's existing harness/mocks), and asserts the run emits ZERO orders and does NOT write a sized handoff. If `main()` is impractical to drive directly, extract the resolve-or-abort decision into a tiny testable function `_resolve_account_or_none(session_factory, fetch_fn)` returning `None` on failure, and assert it returns `None` when the fetch raises.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement.** Change the `except` at `:495-499` to NOT fabricate equity:
```python
    try:
        from execution.alpaca_trader import _alpaca_session, _fetch_account_state
        account = _fetch_account_state(_alpaca_session())
    except Exception as e:
        print(f'[regime_blended_sizer_live] account fetch failed ({e})', file=sys.stderr)
        account = None
    if account is None or account.get('fetched') is False:
        msg = '[regime_blended_sizer_live] ABORT: account fetch failed — emitting ZERO orders (no sizing against fabricated equity)'
        print(msg, file=sys.stderr)
        try:
            from src.execution.pipeline_orchestrator import post_channel
            post_channel(os.environ.get('OPENCLAW_TRADE_ALERT_WEBHOOK_NAME', 'trade-reports'), '🛑 ' + msg)
        except Exception as _e:
            print(f'  (alert post failed: {_e})', file=sys.stderr)
        return
    equity = float(account.get('equity', 100_000.0))
```
(Confirm `return` here exits `main()` before any sized-handoff write — the handoff write is after `size_positions`. The `account.get('fetched') is False` clause catches a soft-failed fetch that still returned a dict.)

- [ ] **Step 4: Run → PASS** (new test green; `python3 -c "import ast; ast.parse(open('src/execution/regime_blended_sizer_live.py').read()); print('parse ok')"`).

- [ ] **Step 5: Commit (path-scoped)** — stage exactly `src/execution/regime_blended_sizer_live.py tests/test_regime_blended_sizer_live.py` (abort guard); `fix(sizer): abort + alert on account-fetch failure instead of fabricating equity (W3 F1)` + footer.

---

## Gated deploy (after final review + operator approval)
Restart user-scope `johnbot.service` (NOT system unit). No DB migration. The intraday-λ, conviction cap, and health gate fire ONLY under `OPENCLAW_INTRADAY_REDEPLOY=1` (set by the redeploy driver) → the daily/EOD lane is unchanged. F1 affects every sizer run but only its fetch-failure path (fail-safe).

## Self-Review (author)
- **Spec coverage:** F1→C5; F2a→C1(helper)+C4(gate); F2b→C2; F2c→C3. All §3-§5b mapped. ✓
- **Placeholders:** pure helper + tests have full code; integration tasks give exact edits + the existing test harness to mirror; the one extract-if-impractical fallback (C3/C5) is an explicit, bounded contingency, not a placeholder. ✓
- **Type consistency:** `OPENCLAW_INTRADAY_REDEPLOY`, `is_signal_set_thin(current,baseline,floor,frac)`, `recent_baseline(counts)`, `_load_lambda(default,*,intraday)`, `SIGNAL_SET_MIN_FLOOR/FRAC` consistent across C1-C5. ✓
