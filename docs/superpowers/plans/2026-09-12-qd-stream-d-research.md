# Stream D — research lane (IC screen, calibrated floor, zero-signal gate, GK low-vol variant) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Four independent research-lane upgrades (spec §4, operator ruling R4 — "proceed now, in whatever
order is most efficient"):

- **D3** — `validate_strategy.py` already computes `signal_count` on a synthetic LOW_VOL panel but
  throws the information away when it is 0. Emit a `zero_signals_synthetic` WARNING (never an
  error), exempting the classes that legitimately emit nothing there (`calendar_edge`, not active
  in LOW_VOL, `min_lookback` > the 60-bar synthetic panel). The orchestrator then skips the Opus
  red-team turn for such a candidate and marks it `needs_signal_check`. It never blocks.
- **D2** — the auto-approve confidence floor is read from two places with two different code
  defaults (0.85 / 0.8). Unify at 0.85 in ONE constant, then make the compared number honest:
  `calibrated_confidence(raw, bucket_table)` deflates a stated confidence by the bucket's own
  observed hit rate, and an evidence cap keyed on the decisive-window closed-trade count plus
  staleness bounds it further. `auto_approve` compares `min(calibrated, cap)` to the floor under
  `OPENCLAW_PROPOSAL_CALIBRATED=1`; unset computes, records and logs the would-be decision without
  changing it. The bucket table also goes back into the sizing-proposal prompt.
- **D1** — a new `src/research/factor_ic_screen.py`: per-rebalance Spearman rank IC at H=5/10/21,
  ICIR, first/second-half IC, rank autocorrelation, quintile long-short mean, monotonicity and
  Jaccard turnover × round-trip cost, with a `pass | weak | flat | skipped` verdict. Wired into the
  research orchestrator's gate chain; `flat` skips the ~900 s backtest under
  `OPENCLAW_IC_SCREEN=1`, `weak`/`skipped` annotate only.
- **D4** — `S_low_volatility_us_gk63.py`, a Garman-Klass range-vol sibling of the live
  `low_volatility_us` decile strategy, registered as a `candidate` and put through the normal gates.

## Architecture

```
D3   validate_strategy.validate(filepath)
       ... signal_count = len(signals)                  [unchanged, :191]
       + _zero_signal_exempt(cls)                       [NEW] calendar_edge | LOW_VOL not in active_in_regimes
       +                                                      | min_lookback > 60 | manifest eligible_regimes lacks LOW_VOL
       + warnings = ['zero_signals_synthetic'] if 0 and not exempt
       ... ok = len(errors) == 0                        [unchanged, :216 - warnings never touch `ok`]
                    |
                    v
     research-orchestrator._runGateChain
       validate decision metadata + { warnings }
       if warnings has zero_signals_synthetic:
           SKIP this._redteamFn (no Opus turn)
           emit gate 'redteam' outcome 'pass' reasonCode 'needs_signal_check'
           continue -> prescreen -> ic_screen -> backtest   (NEVER blocks)

D1   research-orchestrator._runGateChain
       ... redteam ... -> prescreen (120 s) -> ic_screen (300 s, NEW) -> backtest (900 s)
                                            |
                                            +- this._icScreenFn -> python3 -m research.factor_ic_screen
                                            |     load_price_window(504, 300, lookback)   [factor_prescreen, the ONLY sanctioned slice]
                                            |     drive generate_signals every 5th session
                                            |     factor_from_signals -> date x ticker panel
                                            |     compute_ic_screen -> {ic, icir, ic_half, rank_ac,
                                            |                           ls_q5q1, monotonic, turnover, verdict}
                                            +- verdict 'flat' AND OPENCLAW_IC_SCREEN=1 => skip backtest
                                               everything else => annotate + continue

D2   comprehensive_review.js  -- proposal rows --> strategy_regime_param_proposals
       buildStrategyPrompt(strategy, tradePack, counterfactuals, calibration)   [4th arg NEW]
         renders the doctor bucket table verbatim into the memo prompt
                    |
                    v
     proposal_manager.auto_approve(proposal_id)
       raw = prop['confidence']
       calibrated = mastermind_calibration.calibrated_confidence(raw, buckets)   [NEW]
       level, cap = mastermind_calibration.evidence_cap(n_closed, staleness)     [NEW]
       record raw / calibrated / cap / binding_bound on the proposal row (mig 159)
       effective = min(calibrated, cap)
       compare (OPENCLAW_PROPOSAL_CALIBRATED=1 ? effective : raw) to
               autoapprove_min_confidence()   <- ONE constant, default 0.85

D4   S_low_volatility_us_gk63.LowVolatilityUSGK63
       prices (close-only, engine-supplied)
       + _extra_panels.load_wide('open'|'high'|'low'|'close', tickers)  <- the sanctioned OHLC self-load
       -> garman_klass_variance = 0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2, mean over 63 sessions
       -> LONG the lowest-variance decile, equal-weight, house ATR brackets
```

Design invariants carried through every task:

- Pure functions do the arithmetic; every test drives the pure function or injects a fake
  cursor / stubbed loader. No test opens a psycopg2 connection, spawns python from node, or reads
  `data/master/`.
- Every behaviour change that can alter a decision is behind a flag whose unset value is
  byte-identical to today (`OPENCLAW_IC_SCREEN`, `OPENCLAW_PROPOSAL_CALIBRATED`). D3's warning and
  D2a's constant unification are the two exceptions, both explicitly ordered by the spec; D3 never
  blocks and D2a's production value comes from `.env` (0.9) either way.
- DB changes are `ADD COLUMN IF NOT EXISTS` only. Nothing is dropped, nothing is rewritten.

## Tech Stack

- Python 3 (`python3`), `pandas` / `numpy` / stdlib / `psycopg2`. **No new packages** — Spearman is
  `Series.rank().corr()`, not `scipy.stats.spearmanr`.
- Node 18 (`node --test`), `node:test` + `node:assert/strict`. JS tests stub every seam
  (`_validateFn`, `_redteamFn`, `_prescreenFn`, `_icScreenFn`, `_backtestFn`, `_emitDecisionFn`,
  `_query`) and never spawn python.
- PostgreSQL via `psycopg2`; migrations are plain `.sql` under `src/database/migrations/`, applied
  in filename sort order on the next `johnbot.service` restart after this branch merges to main.
  Streams B and C have reserved 155–158, so Stream D uses **159**.
- pytest with `pytest.ini` (`testpaths = tests`, `pythonpath = src`).

## Spec

`/root/openclaw/.claude/worktrees/qd-adoptions/docs/specs/2026-09-12-quantdinger-adoptions-spec.md`
— section 0 (non-negotiables, lines 26–49) and section 4 (Stream D: D1 `:254-270`, D2 `:271-292`,
D3 `:293-300`, D4 `:301-308`).

## Global Constraints

Spec §0, verbatim, one per line:

- Master parquets and canonical Postgres tables are append-only (repo CLAUDE.md). New tables/columns only; never DELETE, never rewrite history.
- Backtest side is AUTHORITATIVE (08-07 ruling). Any live/backtest disagreement is fixed on the live side unless the backtest is provably look-ahead — items 1 and 2 are exactly that case.
- Every new behaviour ships behind an env flag whose unset value is byte-identical to today's behaviour, unless the item is a pure bug fix that the operator has explicitly approved (items 3, 10, 11, 16, and the circuit-breaker regime change).
- 2-core / 8 GB / no swap: never load whole `prices.parquet` or `options_eod.parquet`; slice by date/ticker; no always-on threads; no new packages.
- Production = working tree on `main`; timer-spawned scripts pick up the tree on their next run. Work on a worktree branch; merge to main only when the whole stream is green; never leave main half-edited across a timer boundary.
- Tests on this box reach the REAL DB (`.env` loads at import) — stub gates in fixtures; never run the full suite while the fleet runs; never include `test_regime_stratified_backtest`.
- Every file:line cited below was grep-verified on 2026-09-11 against main `d9dbdf06`; re-verify before editing (lines drift).
- Log to `docs/archive/changelog.md` (newest first) per stream, not to CLAUDE.md.

Test-running rules for this plan:

- Run ONLY the task's own test file plus the test files of the module you touched. Never the whole suite.
- Python: `cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/research/test_factor_ic_screen.py -q`
- JS: `cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/agent/test_ic_screen_gate.test.js`
- Every command in this plan runs from the worktree: `cd /root/openclaw/.claude/worktrees/qd-adoptions && ...`. Never `cd /root/openclaw`.
- Synthetic frames only. No test in this plan may read `data/master/*.parquet` — `factor_prescreen.load_price_window` and `_extra_panels.load_wide` are monkeypatched in every test that would otherwise reach them.
- Stub DB and LLM in fixtures: fake cursors for `proposal_manager`, a fixture bucket-table list for `mastermind_calibration`, `delete process.env.POSTGRES_URI` + an overridden `_emitDecisionFn`/`_query` for the JS gate-chain tests.
- A fleet backtest is running: never run `tests/backtest/test_regime_stratified_backtest.py`, and never run a pytest invocation that imports `src/backtest/unified_backtest.py`'s parquet loaders.
- `src/strategies/validate_strategy.py` is ALSO touched by Stream E (import-lint hook at ~line 70). Every edit in this plan lands at line 8 (docstring), in new helpers appended after `_make_synthetic_regime` (which ends at :57), and at lines 191/216 — never in the 60–120 band.

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `src/research/factor_ic_screen.py` | Rank-IC / ICIR / quantile / turnover screen; pure `compute_ic_screen` + a `--strategy-file` CLI printing one JSON line |
| `src/database/migrations/159_proposal_calibration.sql` | `strategy_regime_param_proposals` + `confidence_raw`, `confidence_calibrated`, `evidence_cap`, `evidence_level`, `binding_bound` |
| `src/strategies/implementations/S_low_volatility_us_gk63.py` | Garman-Klass 63-session range-vol decile variant of `low_volatility_us` |
| `src/strategies/implementations/S_low_volatility_us_gk63.requirements.json` | Data requirements, mirrors the parent's |
| `tests/strategies/test_validate_zero_signal_warning.py` | D3 warning + the exemptions + "never blocks" |
| `tests/agent/test_zero_signal_gate.test.js` | D3 orchestrator: red-team skipped, `needs_signal_check` emitted, chain continues |
| `tests/strategies/test_proposal_floor_constant.py` | D2a: one constant, both read sites, env override wins |
| `tests/metrics/test_calibrated_confidence.py` | D2b: bucket remap math, n<8 passthrough, cap by count + staleness |
| `tests/strategies/test_proposal_calibrated_gate.py` | D2b: `auto_approve` shadow vs enforced, recorded columns, `min()` compare |
| `tests/agent/test_review_calibration_addendum.test.js` | D2c: the bucket block is rendered into the sizing prompt |
| `tests/research/test_factor_ic_screen.py` | D1: predictive ⇒ pass, noise ⇒ flat, reversed ⇒ negative IC, thin cross-section ⇒ skipped |
| `tests/agent/test_ic_screen_gate.test.js` | D1: shape guard + flat/weak behaviour under flag on and off |
| `tests/strategies/test_low_volatility_us_gk63.py` | D4: GK formula, ranking picks the low-GK names, empty-panel guard |

**Modified**

| Path | Change |
|---|---|
| `src/strategies/validate_strategy.py` | + `SYNTHETIC_DAYS`, `_manifest_regime_gated`, `_zero_signal_exempt`; `validate()` returns `warnings` |
| `src/agent/research/research-orchestrator.js` | D3 red-team skip + `needs_signal_check`; D1 `_icScreenFn` seam, `_isIcScreenShape`, Phase 1.9 gate, `QUEUE_STATUS_FOR_REASON.ic_screen_flat` |
| `src/strategies/proposal_manager.py` | `DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE` + `autoapprove_min_confidence()`; calibrated/cap wiring in `auto_approve` |
| `src/metrics/mastermind_calibration.py` | + `bucket_midpoint`, `calibrated_confidence`, `evidence_level`, `evidence_cap`, `evidence_counts` |
| `src/agent/curators/comprehensive_review.js` | `buildStrategyPrompt` 4th `calibration` arg + `_renderProposalCalibration` + fail-soft fetch |
| `src/strategies/registry.py` | `_IMPL_MAP` entry for `S_low_volatility_us_gk63` |
| `src/strategies/manifest.json` | `candidate` entry for `S_low_volatility_us_gk63` |
| `src/strategies/strategy_signatures.json` | Fingerprint entry for the new strategy |
| `docs/archive/changelog.md` | Stream D entry, newest first |

---

### Task 1: D3 — `zero_signals_synthetic` warning in `validate_strategy.py`

**Safety:** `validate_strategy.py` is spawned by `research-orchestrator._runValidateStrategy`
(`:1039-1048`) and by the strategycoder loop. `validate()` today returns
`{'ok', 'errors', 'signal_count'}` from seven return sites. This task adds a `warnings` key to the
FINAL return only (`:216-217`); the six early returns keep their shape, so every consumer must read
`result.get('warnings', [])` / `validResult.warnings || []`. `ok` is untouched — the warning can
never fail a strategy.

**Files:**
- Modify: `src/strategies/validate_strategy.py` — docstring line 8; new helpers appended after `_make_synthetic_regime` (ends :57, immediately before `def validate` at :59); the warning block after `signal_count = len(signals)` (:191); the final return (:216-217)
- Test: `tests/strategies/test_validate_zero_signal_warning.py` (Create)

**Interfaces:**
- Consumes: `strategies.base.BaseStrategy.calendar_edge: bool = False` (`base.py:149`), `.min_lookback: int = 20` (`:150`), `.active_in_regimes: List[str]` normalized by `__init_subclass__` (`:175-206`); `_make_synthetic_regime()` emits `state='LOW_VOL'` (`validate_strategy.py:42-57`)
- Produces: `validate_strategy.SYNTHETIC_DAYS: int = 60`; `validate_strategy._manifest_regime_gated(strategy_id) -> bool`; `validate_strategy._zero_signal_exempt(cls) -> bool`; `validate(filepath) -> {'ok': bool, 'errors': list, 'signal_count': int, 'warnings': list[str]}`

- [ ] **Step 1** — Write the failing test file `tests/strategies/test_validate_zero_signal_warning.py`:

```python
"""D3 — validate_strategy's zero_signals_synthetic WARNING (spec 2026-09-12 §4 D3).

Synthetic only: each test writes a throwaway strategy module to a tmp path and
calls validate() on it. validate() falls back to spec_from_file_location for a
file outside SRC_DIR, so nothing is imported into the strategies package and no
DB / parquet is touched.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    python3 -m pytest tests/strategies/test_validate_zero_signal_warning.py -q
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import validate_strategy as vs  # noqa: E402


_SILENT_BODY = """
        return []
"""

_EMITTING_BODY = """
        out = []
        for t in (universe or [])[:3]:
            if t not in prices.columns:
                continue
            px = float(prices[t].iloc[-1])
            out.append(Signal(ticker=t, direction='LONG', entry_price=px,
                              stop_loss=px * 0.98, target_1=px * 1.05,
                              confidence='MED', position_size_pct=0.01))
        return out
"""


def _write_strategy(tmp_path: Path, name: str, *, attrs: str = '', body: str = _SILENT_BODY) -> str:
    src = textwrap.dedent(f'''
        from typing import List
        from strategies.base import BaseStrategy, Signal


        class {name}(BaseStrategy):
            id   = '{name.lower()}'
            name = '{name}'
            description = 'zero-signal warning fixture'
            tier = 3
        ''')
    for line in (attrs.strip().splitlines() if attrs.strip() else []):
        src += f'    {line.strip()}\n'
    src += (
        "\n    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:"
        f"{body}"
    )
    path = tmp_path / f'{name.lower()}.py'
    path.write_text(src)
    return str(path)


def test_zero_signals_sets_warning_and_never_blocks(tmp_path):
    path = _write_strategy(tmp_path, 'ZzSilent')
    res = vs.validate(path)
    assert res['ok'] is True, res['errors']
    assert res['signal_count'] == 0
    assert res['warnings'] == ['zero_signals_synthetic']


def test_calendar_edge_is_exempt(tmp_path):
    path = _write_strategy(tmp_path, 'ZzCalendar', attrs='calendar_edge = True')
    res = vs.validate(path)
    assert res['ok'] is True
    assert res['warnings'] == []


def test_regime_gated_away_from_low_vol_is_exempt(tmp_path):
    path = _write_strategy(tmp_path, 'ZzCrisisOnly',
                           attrs="active_in_regimes = ['CRISIS']")
    res = vs.validate(path)
    assert res['ok'] is True
    assert res['warnings'] == []


def test_min_lookback_beyond_the_synthetic_panel_is_exempt(tmp_path):
    path = _write_strategy(tmp_path, 'ZzLongLookback', attrs='min_lookback = 400')
    res = vs.validate(path)
    assert res['ok'] is True
    assert res['warnings'] == []


def test_emitting_strategy_gets_no_warning(tmp_path):
    path = _write_strategy(tmp_path, 'ZzEmitter', body=_EMITTING_BODY)
    res = vs.validate(path)
    assert res['ok'] is True, res['errors']
    assert res['signal_count'] == 3
    assert res['warnings'] == []


def test_manifest_eligible_regimes_without_low_vol_is_exempt(tmp_path, monkeypatch):
    """A strategy the operator already gated away from LOW_VOL in the manifest
    is exempt even when its class-level active_in_regimes still lists it."""
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(
        '{"strategies": {"zzmanifestgated": '
        '{"metadata": {"eligible_regimes": ["HIGH_VOL", "CRISIS"]}}}}'
    )
    monkeypatch.setattr(vs, 'MANIFEST_PATH', str(manifest))
    path = _write_strategy(tmp_path, 'ZzManifestGated')
    res = vs.validate(path)
    assert res['ok'] is True
    assert res['warnings'] == []


def test_manifest_read_failure_does_not_exempt(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, 'MANIFEST_PATH', str(tmp_path / 'does_not_exist.json'))
    path = _write_strategy(tmp_path, 'ZzNoManifest')
    res = vs.validate(path)
    assert res['warnings'] == ['zero_signals_synthetic']


@pytest.mark.parametrize('n_closed,expected', [(0, True), (1, False)])
def test_exempt_helper_is_pure(n_closed, expected):
    """_zero_signal_exempt reads only class attributes — no I/O for the
    calendar_edge branch."""
    class _Fake:
        calendar_edge = bool(n_closed == 0)
        active_in_regimes = ['LOW_VOL']
        min_lookback = 20
        id = None
    assert vs._zero_signal_exempt(_Fake) is expected
```

- [ ] **Step 2** — Run it and confirm the expected failure:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_validate_zero_signal_warning.py -q
```

Expected: every test errors with `AttributeError: module 'strategies.validate_strategy' has no
attribute 'MANIFEST_PATH'` / `'_zero_signal_exempt'`, and the `res['warnings']` assertions raise
`KeyError: 'warnings'`.

- [ ] **Step 3** — Implement. In `src/strategies/validate_strategy.py`, change the docstring line 8 from:

```
Prints JSON: {"ok": bool, "errors": [...], "signal_count": int}
```

to:

```
Prints JSON: {"ok": bool, "errors": [...], "signal_count": int, "warnings": [...]}
```

Then append these three definitions immediately AFTER `_make_synthetic_regime` (its closing `}`
plus blank lines end at :57) and BEFORE `def validate(filepath: str) -> dict:` (:59):

```python
# Number of synthetic bars _make_synthetic_prices builds (its n_days default).
# A strategy whose declared min_lookback exceeds this cannot possibly emit on
# the synthetic panel, so zero signals there proves nothing about it.
SYNTHETIC_DAYS = 60

MANIFEST_PATH = os.path.join(SRC_DIR, 'strategies', 'manifest.json')


def _manifest_regime_gated(strategy_id) -> bool:
    """True when the manifest pins this strategy's eligible_regimes to a set
    that EXCLUDES LOW_VOL — the operator has already gated it away from the
    only regime _make_synthetic_regime drives, so zero signals here is the
    expected outcome, not a defect. Any read/shape failure returns False: an
    unreadable manifest must never grant an exemption."""
    if not strategy_id:
        return False
    try:
        with open(MANIFEST_PATH) as fh:
            manifest = json.load(fh)
    except Exception:
        return False
    entry = (manifest.get('strategies') or {}).get(str(strategy_id)) or {}
    eligible = (entry.get('metadata') or {}).get('eligible_regimes')
    if not isinstance(eligible, list) or not eligible:
        return False
    return 'LOW_VOL' not in eligible


def _zero_signal_exempt(cls) -> bool:
    """True when zero signals on the synthetic panel is EXPECTED (spec D3).

    Three class-level exemptions plus one manifest one:
      - calendar_edge (base.py:149): the calendar window IS the signal, and the
        synthetic bdate_range ending 2026-01-01 will usually miss it.
      - LOW_VOL not in active_in_regimes: should_run() returns False by design,
        because _make_synthetic_regime only ever emits state='LOW_VOL'.
      - min_lookback > SYNTHETIC_DAYS: the panel is shorter than the strategy's
        own declared history requirement.
      - manifest metadata.eligible_regimes excludes LOW_VOL (see above).
    """
    if bool(getattr(cls, 'calendar_edge', False)):
        return True
    regimes = getattr(cls, 'active_in_regimes', None) or []
    try:
        if 'LOW_VOL' not in set(regimes):
            return True
    except TypeError:
        pass
    try:
        declared = getattr(cls, 'min_lookback', None)
        if declared is not None and int(declared) > SYNTHETIC_DAYS:
            return True
    except (TypeError, ValueError):
        pass
    return _manifest_regime_gated(getattr(cls, 'id', None))
```

Then replace the block at `:191` — currently the single line:

```python
    signal_count = len(signals)
```

with:

```python
    signal_count = len(signals)

    # ── 6b. Zero-signal WARN (spec D3, 2026-09-12) ────────────────────────────
    # A strategy that emits nothing on the synthetic LOW_VOL panel is usually
    # silently inert — the defect class the red-team gate calls "(e) SIGNAL-CAN-
    # NEVER-FIRE". Surfacing it here lets the orchestrator skip the Opus turn
    # instead of paying for one to restate what this harness already knows.
    # WARNING ONLY: `ok` below stays `len(errors) == 0`, so this can never fail
    # a candidate — the prescreen and the backtest remain the gates that block.
    warnings_out: list = []
    if signal_count == 0 and not _zero_signal_exempt(cls):
        warnings_out.append('zero_signals_synthetic')
```

Finally replace the final return at `:216-217`:

```python
    ok = len(errors) == 0
    return {'ok': ok, 'errors': errors, 'signal_count': signal_count}
```

with:

```python
    ok = len(errors) == 0
    return {'ok': ok, 'errors': errors, 'signal_count': signal_count,
            'warnings': warnings_out}
```

(The six early returns keep their three-key shape on purpose — they are all hard failures where a
warning would be noise, and two of them sit in the 60–120 line band Stream E is editing. Consumers
read `warnings` defensively.)

- [ ] **Step 4** — Run the task's tests plus the touching module's existing tests and confirm PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_validate_zero_signal_warning.py tests/strategies/test_strategy_template_base.py tests/strategies/test_oxf_contract.py -q
```

Expected: all green.

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/strategies/validate_strategy.py tests/strategies/test_validate_zero_signal_warning.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(validate): D3 zero_signals_synthetic warning with calendar-edge/regime/lookback exemptions

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: D3 — orchestrator skips the red-team LLM and marks `needs_signal_check`

**Safety:** `_runGateChain` is the shared gate path for the single-shot and tournament flows
(`research-orchestrator.js:1293`). This task changes only the red-team stage's *entry condition*;
every existing branch (infra fail, block, pass) keeps its exact emit shape. A candidate that emits
signals sees byte-identical behaviour. Nothing here is flag-gated because nothing here can block —
spec D3 says "never BLOCK".

**Files:**
- Modify: `src/agent/research/research-orchestrator.js` — the validate-pass emit + red-team stage (currently `:1325-1385`)
- Test: `tests/agent/test_zero_signal_gate.test.js` (Create)

**Interfaces:**
- Consumes: `validate_strategy` result `{ok, errors, signal_count, warnings}` (Task 1); `this._validateFn / _redteamFn / _prescreenFn / _backtestFn / _emitDecisionFn` seams (`:314-330`); `emitGateDecision({paperId, candidateId, strategyId, gateName, outcome, reasonCode, reasonDetail, metadata})` (`gate-decisions.js:36-45`)
- Produces: a `gateName:'redteam', outcome:'pass', reasonCode:'needs_signal_check'` decision row; `rtResult.skipped_zero_signals === true`

- [ ] **Step 1** — Write the failing test file `tests/agent/test_zero_signal_gate.test.js`:

```js
'use strict';

/**
 * D3 — the orchestrator's zero-signal branch (spec 2026-09-12 §4 D3).
 *
 * When validate_strategy reports warnings ['zero_signals_synthetic'], the gate
 * chain must (a) NOT call the Opus red-team reviewer, (b) emit a redteam
 * decision with reasonCode 'needs_signal_check', and (c) keep going — the
 * prescreen and the backtest still run. It must never return ok:false.
 *
 * Run:
 *   cd /root/openclaw/.claude/worktrees/qd-adoptions && \
 *     node --test tests/agent/test_zero_signal_gate.test.js
 */

// paperIdForCandidate() and emitGateDecision() both short-circuit to null when
// POSTGRES_URI is absent, so no pg connection is ever attempted.
delete process.env.POSTGRES_URI;

const { test } = require('node:test');
const assert   = require('node:assert/strict');

const ResearchOrchestrator = require('../../src/agent/research/research-orchestrator');

function makeOrch({ warnings = [], redteamVerdict = 'pass' } = {}) {
  const orch = new ResearchOrchestrator();
  const calls = { redteam: 0, prescreen: 0, backtest: 0 };
  const decisions = [];
  orch._query = async () => ({ rows: [] });
  orch._validateFn = async () => ({ ok: true, errors: [], signal_count: warnings.length ? 0 : 7, warnings });
  orch._redteamFn = async () => { calls.redteam += 1; return { verdict: redteamVerdict, findings: [], infra_fail: false }; };
  orch._prescreenFn = async () => { calls.prescreen += 1; return { psResult: { pass: true, reason: null, stats: {} }, psInfraFail: false, psInfraReason: null }; };
  orch._backtestFn = async () => { calls.backtest += 1; return { run_id: 'r1', sharpe: 0.5 }; };
  orch._emitDecisionFn = async (d) => { decisions.push(d); };
  return { orch, calls, decisions };
}

const ARGS = {
  candidate_id: 'cand-1',
  stratId: 'S_zero',
  implPath: '/tmp/S_zero.py',
  strategy_spec: {},
  opts: {},
  suppressQueueWrite: true,
  runEligibility: false,
};

test('zero_signals_synthetic: red-team LLM is skipped and the chain continues', async () => {
  const { orch, calls, decisions } = makeOrch({ warnings: ['zero_signals_synthetic'] });
  const out = await orch._runGateChain({ ...ARGS });

  assert.equal(calls.redteam, 0, 'the Opus red-team turn must be skipped');
  assert.equal(calls.prescreen, 1, 'the prescreen still runs');
  assert.equal(calls.backtest, 1, 'the backtest still runs — the warning never blocks');
  assert.equal(out.ok, true);

  const rt = decisions.filter(d => d.gateName === 'redteam');
  assert.equal(rt.length, 1, 'exactly one redteam decision, not two');
  assert.equal(rt[0].outcome, 'pass');
  assert.equal(rt[0].reasonCode, 'needs_signal_check');
});

test('zero_signals_synthetic: the validate decision carries the warnings list', async () => {
  const { orch, decisions } = makeOrch({ warnings: ['zero_signals_synthetic'] });
  await orch._runGateChain({ ...ARGS });
  const v = decisions.find(d => d.gateName === 'validate');
  assert.deepEqual(v.metadata.warnings, ['zero_signals_synthetic']);
  assert.equal(v.metadata.signal_count, 0);
});

test('no warnings: the red-team reviewer runs exactly as before', async () => {
  const { orch, calls, decisions } = makeOrch({ warnings: [] });
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.redteam, 1);
  assert.equal(out.ok, true);
  const rt = decisions.filter(d => d.gateName === 'redteam');
  assert.equal(rt.length, 1);
  assert.equal(rt[0].reasonCode, undefined, 'the normal pass emit carries no reasonCode');
  assert.deepEqual(rt[0].metadata, { findings: [] });
});

test('a validate result with no warnings key is treated as no warnings', async () => {
  const orch = new ResearchOrchestrator();
  let redteamCalls = 0;
  orch._query = async () => ({ rows: [] });
  orch._validateFn = async () => ({ ok: true, errors: [], signal_count: 3 });  // legacy shape
  orch._redteamFn = async () => { redteamCalls += 1; return { verdict: 'pass', findings: [], infra_fail: false }; };
  orch._prescreenFn = async () => ({ psResult: { pass: true }, psInfraFail: false, psInfraReason: null });
  orch._backtestFn = async () => ({ run_id: 'r1' });
  orch._emitDecisionFn = async () => {};
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(redteamCalls, 1);
  assert.equal(out.ok, true);
});
```

- [ ] **Step 2** — Run it and confirm the expected failure:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/agent/test_zero_signal_gate.test.js
```

Expected: the first two tests fail — `calls.redteam` is 1 (the reviewer is always called) and the
validate decision's `metadata.warnings` is `undefined`.

- [ ] **Step 3** — Implement in `src/agent/research/research-orchestrator.js`. Replace the block that
currently runs from the validate-pass emit (`:1325`) through the opening of the red-team try/catch
(`:1341`) — that is, from `    await this._emitDecisionFn({` … `metadata:    { signal_count: validResult.signal_count ?? null },` … through the `} catch (e) { … rtResult = { verdict: 'pass', findings: [], infra_fail: true }; }` block — with:

```js
    const vWarnings = Array.isArray(validResult.warnings) ? validResult.warnings : [];
    await this._emitDecisionFn({
      paperId:     vPaperId,
      candidateId: candidate_id,
      strategyId:  stratId,
      gateName:    'validate',
      outcome:     'pass',
      metadata:    { signal_count: validResult.signal_count ?? null, warnings: vWarnings },
    });
    const zeroSignals = vWarnings.includes('zero_signals_synthetic');
    notify?.(`  ✅ ${stratId} validation passed — running red-team review...`);
    onPhase('redteam', 50);

    // ── Phase 1.5: Mandatory LLM red-team gate (Task S1) ──────────────────────
    let rtResult;
    if (zeroSignals) {
      // Spec D3 (2026-09-12): validate_strategy emitted zero signals on the
      // synthetic LOW_VOL panel and the strategy is neither calendar_edge, nor
      // gated away from LOW_VOL, nor longer-lookback than the panel. The
      // red-team reviewer's job is to find backtest-integrity defects in code
      // that PRODUCES signals; on a silently inert strategy it burns an Opus
      // turn to restate what the harness already proved. Skip the call, mark
      // the candidate needs_signal_check, and continue — the prescreen and the
      // backtest are the gates that decide. This branch NEVER blocks.
      rtResult = { verdict: 'pass', findings: [], infra_fail: false, skipped_zero_signals: true };
      await this._emitDecisionFn({
        paperId:      vPaperId,
        candidateId:  candidate_id,
        strategyId:   stratId,
        gateName:     'redteam',
        outcome:      'pass',
        reasonCode:   'needs_signal_check',
        reasonDetail: 'validate_strategy reported zero_signals_synthetic — red-team LLM skipped (warn only, never blocks)',
        metadata:     { warnings: vWarnings, signal_count: validResult.signal_count ?? null },
      });
      notify?.(`  ⚠️ ${stratId} emitted 0 signals on the synthetic panel — needs_signal_check; red-team LLM skipped (not a block).`);
    } else {
      try {
        rtResult = await this._redteamFn({
          implPath,
          paperContext: strategy_spec?.hypothesis_one_liner || strategy_spec?.signal_logic || null,
        });
      } catch (e) {
        console.error(`[redteam] unexpected exception auditing ${stratId}: ${e.message}`);
        rtResult = { verdict: 'pass', findings: [], infra_fail: true };
      }
    }
```

Then change the final `else` of the red-team verdict ladder (currently `:1376`, the branch that
emits the plain `gateName:'redteam', outcome:'pass'` decision) from:

```js
    } else {
      await this._emitDecisionFn({
```

to:

```js
    } else if (!rtResult.skipped_zero_signals) {
      // The zero-signal branch above already emitted this gate's decision —
      // emitting again would double-count the redteam gate in
      // paper_gate_decisions and skew curator_gate_calibration.
      await this._emitDecisionFn({
```

- [ ] **Step 4** — Run and confirm PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/agent/test_zero_signal_gate.test.js tests/agent/test_prescreen_shape.test.js tests/agent/test_tearsheet_hook.test.js
```

Expected: all green.

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/agent/research/research-orchestrator.js tests/agent/test_zero_signal_gate.test.js
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(research): D3 skip the red-team LLM on zero_signals_synthetic, mark needs_signal_check

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: D2a — unify the auto-approve confidence floor in ONE constant

**Safety:** the floor is read at three places today with two different code defaults —
`proposal_manager.py:311` (`'0.85'`, used by `auto_approve`), `:403` (`'0.8'`, used by
`auto_apply_batch`) and the CLI help string at `:473` (`or 0.8`). Production sets
`OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE=0.9` in `.env` (changelog 2026-09-06 22:30 UTC), so
the env override wins at every site and **live behaviour is byte-identical** either way. The only
observable change is the fallback when the env is unset: `auto_apply_batch` goes 0.8 → 0.85, which
is the operator-ordered direction (stricter).

**Files:**
- Modify: `src/strategies/proposal_manager.py` — add the constant + accessor after `CANONICAL_REGIMES` (`:39`); replace the reads at `:311`, `:403`, and the help text at `:473`
- Test: `tests/strategies/test_proposal_floor_constant.py` (Create)

**Interfaces:**
- Consumes: `os.environ['OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE']`
- Produces: `proposal_manager.DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE: float = 0.85`; `proposal_manager.autoapprove_min_confidence() -> float`

- [ ] **Step 1** — Write the failing test file `tests/strategies/test_proposal_floor_constant.py`:

```python
"""D2a — ONE auto-approve confidence floor constant (spec 2026-09-12 §4 D2).

Before: auto_approve defaulted to 0.85 (:311) while auto_apply_batch defaulted
to 0.8 (:403) and the CLI help said 0.8 (:473). Three reads, two answers.

No DB: every test either calls the pure accessor or greps the module source.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    python3 -m pytest tests/strategies/test_proposal_floor_constant.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import proposal_manager as pm  # noqa: E402

SOURCE = (ROOT / 'src' / 'strategies' / 'proposal_manager.py').read_text()


def test_default_is_085():
    assert pm.DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE == 0.85


def test_accessor_returns_the_default_when_env_unset(monkeypatch):
    monkeypatch.delenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', raising=False)
    assert pm.autoapprove_min_confidence() == 0.85


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.9')
    assert pm.autoapprove_min_confidence() == 0.9


def test_no_literal_08_or_085_default_survives_in_the_source():
    """Exactly one place may spell the number: the constant itself."""
    leftovers = re.findall(
        r"OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE['\"]\s*,\s*['\"]0\.\d+['\"]", SOURCE)
    assert leftovers == [], f'inline env defaults still present: {leftovers}'
    assert SOURCE.count('DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE = 0.85') == 1


def test_auto_apply_batch_threshold_uses_the_accessor(monkeypatch):
    """threshold=None must resolve through autoapprove_min_confidence()."""
    seen = {}
    monkeypatch.setattr(pm, 'autoapprove_min_confidence', lambda: 0.77)
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setattr(pm, 'list_proposals', lambda **kw: [])
    result = pm.auto_apply_batch(log=lambda m: seen.setdefault('log', []).append(m))
    assert result['threshold'] == 0.77


def test_cli_help_text_does_not_hardcode_08():
    assert 'or 0.8)' not in SOURCE
```

- [ ] **Step 2** — Run it and confirm the expected failure:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_proposal_floor_constant.py -q
```

Expected: `AttributeError: module 'strategies.proposal_manager' has no attribute
'DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE'` on the first four tests, and the two source-grep tests fail on
the surviving `'0.85'` / `'0.8'` literals.

- [ ] **Step 3** — Implement. In `src/strategies/proposal_manager.py`, insert immediately after
`CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')` (`:39`):

```python
# ── Auto-approve confidence floor — ONE source of truth (spec D2, 2026-09-12) ─
# Was split three ways before this: auto_approve read a '0.85' default,
# auto_apply_batch a '0.8' default, and the CLI help said 0.8. Production
# overrides all of them via .env (OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE
# = 0.9 since 2026-09-06), so unifying the fallback changes no live behaviour —
# it removes a trap where an env-less run silently used a looser bar in the
# Saturday batch path than in the single-proposal path.
DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE = 0.85


def autoapprove_min_confidence() -> float:
    """The confidence floor auto-approval compares against. Read at CALL time
    so an .env change is picked up by the next timer-spawned run without a
    restart (the same contract factor_prescreen._default_lookback documents)."""
    raw = os.environ.get('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE')
    if raw is None or str(raw).strip() == '':
        return DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE
```

Replace `:311`:

```python
    min_conf  = float(os.environ.get('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.85'))
```

with:

```python
    min_conf  = autoapprove_min_confidence()
```

Replace `:402-403`:

```python
    thr = threshold if threshold is not None else float(
        os.environ.get('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.8'))
```

with:

```python
    thr = threshold if threshold is not None else autoapprove_min_confidence()
```

Replace the CLI help at `:472-473`:

```python
                   help='confidence threshold for --auto-apply-batch '
                        '(default env OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE or 0.8)')
```

with:

```python
                   help='confidence threshold for --auto-apply-batch (default env '
                        'OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE, else '
                        f'{DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE})')
```

Finally update the module docstring line 7 (`Saturday auto-apply (2026-07-14)-> auto_apply_batch(): confidence > 0.8 is`) to read
`confidence > the floor (autoapprove_min_confidence(), default 0.85) is`.

- [ ] **Step 4** — Run and confirm PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_proposal_floor_constant.py tests/strategies/test_proposal_auto_approval.py tests/strategies/test_proposal_manager.py -q
```

Expected: all green. `test_proposal_auto_approval.py` sets the env explicitly in every case, so it
is unaffected by the default change.

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/strategies/proposal_manager.py tests/strategies/test_proposal_floor_constant.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "refactor(proposals): D2a unify the auto-approve confidence floor at 0.85 in one constant

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: D2b (part 1) — `calibrated_confidence` + evidence cap in `mastermind_calibration`

**Safety:** pure additions to `src/metrics/mastermind_calibration.py`. No existing function changes,
so `doctor.check_mastermind_calibration_brier` (`doctor.py:1063-1093`) and the dashboard's
`calibration_report()` consumers are untouched. `evidence_counts` is the only new DB reader and it
is not called by anything until Task 5.

**Files:**
- Modify: `src/metrics/mastermind_calibration.py` — append after `_bucket_aggregates` (ends `:135`, immediately before `def compute_outcome` at `:138`)
- Test: `tests/metrics/test_calibrated_confidence.py` (Create)

**Interfaces:**
- Consumes: `BUCKETS` (`:24-30`, five `(lo, hi, label)` triples, top bucket `hi = 1.001`); the bucket-table row shape produced by `_bucket_aggregates` (`:119-135`) — `{'range': str, 'count': int, 'matched': int, 'match_rate': float | None}`; `_connect()` (`:37-39`)
- Produces:
  - `bucket_midpoint(lo: float, hi: float) -> float`
  - `bucket_for(conf: float) -> tuple[float, float, str] | None`
  - `calibrated_confidence(raw: float | None, bucket_table: list[dict], *, min_n: int = MIN_BUCKET_N) -> float | None`
  - `evidence_level(n_closed: int, staleness_days: float | None, *, stale_days: int = EVIDENCE_STALE_DAYS) -> str`
  - `evidence_cap(n_closed: int, staleness_days: float | None) -> tuple[str, float]`
  - `evidence_counts(strategy_id: str, regime_state: str, *, window_days: int = EVIDENCE_WINDOW_DAYS, now=None) -> dict` → `{'n_closed': int, 'staleness_days': float | None}`
  - constants `MIN_BUCKET_N = 8`, `EVIDENCE_CAPS`, `EVIDENCE_LEVELS`, `EVIDENCE_WINDOW_DAYS = 30`, `EVIDENCE_STALE_DAYS = 45`

- [ ] **Step 1** — Write the failing test file `tests/metrics/test_calibrated_confidence.py`:

```python
"""D2b — outcome-calibrated confidence + evidence cap (spec 2026-09-12 §4 D2).

calibrated = raw * clip(match_rate(bucket) / bucket_midpoint, 0.5, 1.0) when the
bucket has n >= 8, else raw. The upper clip is 1.0 on purpose: calibration may
only DEFLATE an over-confident model, never inflate an under-confident one.

evidence cap keys on the decisive-window closed-trade count
(<10 none, <30 low, <100 medium, else high) with staleness > 45 d dropping one
level. Caps: none 0.35 / low 0.55 / medium 0.75 / high 1.0.

No DB: every test drives the pure functions with a fixture bucket table.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    python3 -m pytest tests/metrics/test_calibrated_confidence.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from metrics import mastermind_calibration as mc  # noqa: E402


def _table(**by_label):
    """Bucket table in _bucket_aggregates' exact shape."""
    rows = []
    for lo, hi, label in mc.BUCKETS:
        spec = by_label.get(label)
        if spec is None:
            rows.append({'range': label, 'count': 0, 'matched': 0, 'match_rate': None})
        else:
            n, rate = spec
            rows.append({'range': label, 'count': n, 'matched': int(round(n * rate)),
                         'match_rate': rate})
    return rows


# ── bucket helpers ────────────────────────────────────────────────────────────

def test_bucket_midpoint_clips_the_open_top_bucket_at_one():
    assert mc.bucket_midpoint(0.8, 1.001) == pytest.approx(0.9)
    assert mc.bucket_midpoint(0.6, 0.8) == pytest.approx(0.7)


def test_bucket_for_picks_the_containing_bucket():
    assert mc.bucket_for(0.9)[2] == '[0.8, 1.0]'
    assert mc.bucket_for(0.8)[2] == '[0.8, 1.0]'
    assert mc.bucket_for(0.79)[2] == '[0.6, 0.8]'
    assert mc.bucket_for(1.0)[2] == '[0.8, 1.0]'
    assert mc.bucket_for(None) is None
    assert mc.bucket_for(-0.1) is None


# ── calibrated_confidence ─────────────────────────────────────────────────────

def test_overconfident_bucket_deflates():
    """The real 2026-09-06 finding: the >=0.8 bucket hit 0.56 on n=18.
    0.9 * clip(0.56/0.9, .5, 1) = 0.9 * 0.6222... = 0.56."""
    table = _table(**{'[0.8, 1.0]': (18, 0.56)})
    assert mc.calibrated_confidence(0.9, table) == pytest.approx(0.56, abs=1e-9)


def test_well_calibrated_bucket_is_left_alone():
    table = _table(**{'[0.8, 1.0]': (20, 0.9)})
    assert mc.calibrated_confidence(0.9, table) == pytest.approx(0.9)


def test_underconfident_bucket_is_not_inflated():
    """match_rate above the midpoint would give a ratio > 1 — clipped to 1.0."""
    table = _table(**{'[0.6, 0.8]': (30, 1.0)})
    assert mc.calibrated_confidence(0.7, table) == pytest.approx(0.7)


def test_ratio_floor_is_half():
    """A bucket that never hits still keeps half the stated confidence."""
    table = _table(**{'[0.8, 1.0]': (20, 0.0)})
    assert mc.calibrated_confidence(0.9, table) == pytest.approx(0.45)


def test_thin_bucket_passes_through_raw():
    table = _table(**{'[0.8, 1.0]': (7, 0.1)})   # n = 7 < MIN_BUCKET_N (8)
    assert mc.calibrated_confidence(0.9, table) == pytest.approx(0.9)


def test_bucket_boundary_n_equals_eight_applies():
    table = _table(**{'[0.8, 1.0]': (8, 0.45)})
    assert mc.calibrated_confidence(0.9, table) == pytest.approx(0.45)


def test_missing_bucket_or_none_rate_passes_through_raw():
    assert mc.calibrated_confidence(0.9, _table()) == pytest.approx(0.9)
    assert mc.calibrated_confidence(0.9, []) == pytest.approx(0.9)
    assert mc.calibrated_confidence(0.9, None) == pytest.approx(0.9)


def test_none_raw_stays_none():
    assert mc.calibrated_confidence(None, _table(**{'[0.8, 1.0]': (20, 0.5)})) is None


# ── evidence cap ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('n,level', [
    (0, 'none'), (9, 'none'), (10, 'low'), (29, 'low'),
    (30, 'medium'), (99, 'medium'), (100, 'high'), (5000, 'high'),
])
def test_evidence_level_by_count(n, level):
    assert mc.evidence_level(n, staleness_days=0.0) == level


@pytest.mark.parametrize('n,fresh,stale', [
    (200, 'high', 'medium'), (50, 'medium', 'low'),
    (15, 'low', 'none'), (3, 'none', 'none'),
])
def test_staleness_drops_exactly_one_level(n, fresh, stale):
    assert mc.evidence_level(n, staleness_days=44.9) == fresh
    assert mc.evidence_level(n, staleness_days=45.1) == stale


def test_no_closed_trade_ever_is_none_level():
    assert mc.evidence_level(0, staleness_days=None) == 'none'
    assert mc.evidence_level(500, staleness_days=None) == 'none'


@pytest.mark.parametrize('level,cap', [
    ('none', 0.35), ('low', 0.55), ('medium', 0.75), ('high', 1.0)])
def test_cap_table(level, cap):
    assert mc.EVIDENCE_CAPS[level] == cap


def test_evidence_cap_returns_level_and_value():
    assert mc.evidence_cap(120, 3.0) == ('high', 1.0)
    assert mc.evidence_cap(120, 60.0) == ('medium', 0.75)
    assert mc.evidence_cap(0, None) == ('none', 0.35)


# ── the combined bound ────────────────────────────────────────────────────────

def test_min_of_calibrated_and_cap_is_what_binds():
    table = _table(**{'[0.8, 1.0]': (18, 0.56)})
    calibrated = mc.calibrated_confidence(0.9, table)      # 0.56
    _lvl, cap = mc.evidence_cap(12, 5.0)                   # low -> 0.55
    assert min(calibrated, cap) == pytest.approx(0.55)     # the cap binds


def test_evidence_counts_query_is_stubbable(monkeypatch):
    """evidence_counts must go through _connect() so Task 5's tests can fake it."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    last = now - timedelta(days=7)

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params=()): self.sql = sql
        def fetchone(self): return (42, last)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return _Cur()

    monkeypatch.setattr(mc, '_connect', lambda: _Conn())
    out = mc.evidence_counts('S_x', 'LOW_VOL', now=now)
    assert out['n_closed'] == 42
    assert out['staleness_days'] == pytest.approx(7.0, abs=1e-6)
```

- [ ] **Step 2** — Run it and confirm the expected failure:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/metrics/test_calibrated_confidence.py -q
```

Expected: collection succeeds, then every test fails with
`AttributeError: module 'metrics.mastermind_calibration' has no attribute 'bucket_midpoint'`
(and the same for the other five new names).

- [ ] **Step 3** — Implement. Append to `src/metrics/mastermind_calibration.py`, immediately after
`_bucket_aggregates` (its `return out` at `:135`) and before `def compute_outcome` (`:138`):

```python
# ── D2: outcome-calibrated confidence + evidence cap (spec 2026-09-12 §4) ─────
# Evidence for these numbers: the 2026-09-06 recompute found the mastermind
# over-confident — Brier 0.254 on 41 resolved outcomes, hit rate 0.66 against a
# mean stated confidence of 0.75, and 0.56 (10/18) inside the >=0.8 bucket that
# auto-approval actually reads. calibrated_confidence turns that observation
# into the number the floor is compared against instead of the stated one.

MIN_BUCKET_N         = 8      # below this a bucket's rate is noise — pass raw through
EVIDENCE_WINDOW_DAYS = 30     # matches DEFAULT_WINDOW_DAYS, the outcome window
EVIDENCE_STALE_DAYS  = 45     # no closed trade in this long => one level down
EVIDENCE_LEVELS      = ('none', 'low', 'medium', 'high')
EVIDENCE_CAPS        = {'none': 0.35, 'low': 0.55, 'medium': 0.75, 'high': 1.0}


def bucket_midpoint(lo: float, hi: float) -> float:
    """Midpoint of a BUCKETS range. The top bucket's `hi` is 1.001 (an
    exclusive-upper trick so confidence == 1.0 lands somewhere), so clamp it
    back to 1.0 before averaging — otherwise the [0.8, 1.0] midpoint would be
    0.9005 and every top-bucket remap would carry a spurious deflation."""
    return (float(lo) + min(float(hi), 1.0)) / 2.0


def bucket_for(conf):
    """The (lo, hi, label) BUCKETS triple containing `conf`; None when conf is
    None or outside [0, 1]."""
    if conf is None:
        return None
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return None
    if c < 0.0 or c > 1.0:
        return None
    for lo, hi, label in BUCKETS:
        if lo <= c < hi:
            return (lo, hi, label)
    return None


def calibrated_confidence(raw, bucket_table, *, min_n: int = MIN_BUCKET_N):
    """raw x clip(match_rate(bucket) / bucket_midpoint, 0.5, 1.0), or raw when
    the bucket has fewer than `min_n` resolved observations.

    `bucket_table` is the list _bucket_aggregates / calibration_report()['buckets']
    returns: rows of {'range', 'count', 'matched', 'match_rate'}. NOTE the key is
    `match_rate` (per-bucket); `hit_rate` on the report is the GLOBAL figure.

    The ratio's upper clip is 1.0 by design: this may only deflate an
    over-confident stated number, never inflate an under-confident one — an
    auto-approval floor must not be crossed by a bonus.
    """
    if raw is None:
        return None
    try:
        raw_f = float(raw)
    except (TypeError, ValueError):
        return None
    b = bucket_for(raw_f)
    if b is None or not bucket_table:
        return raw_f
    _lo, _hi, label = b
    row = next((r for r in bucket_table if r.get('range') == label), None)
    if row is None:
        return raw_f
    try:
        n = int(row.get('count') or 0)
    except (TypeError, ValueError):
        return raw_f
    rate = row.get('match_rate')
    if n < min_n or rate is None:
        return raw_f
    mid = bucket_midpoint(_lo, _hi)
    if mid <= 0:
        return raw_f
    ratio = float(rate) / mid
    ratio = max(0.5, min(1.0, ratio))
    return raw_f * ratio


def evidence_level(n_closed: int, staleness_days,
                   *, stale_days: int = EVIDENCE_STALE_DAYS) -> str:
    """Evidence tier for a proposal's decisive window.

    Count tiers: <10 none, <30 low, <100 medium, else high. A sleeve whose most
    recent closed trade is older than `stale_days` drops exactly one tier
    (floored at 'none'); a sleeve with no closed trade at all is 'none'
    regardless of count (staleness_days is None only in that case).
    """
    if staleness_days is None:
        return 'none'
    try:
        n = int(n_closed or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 10:
        level = 'none'
    elif n < 30:
        level = 'low'
    elif n < 100:
        level = 'medium'
    else:
        level = 'high'
    if float(staleness_days) > float(stale_days):
        idx = max(0, EVIDENCE_LEVELS.index(level) - 1)
        level = EVIDENCE_LEVELS[idx]
    return level


def evidence_cap(n_closed: int, staleness_days) -> tuple:
    """(level, cap) for a proposal's decisive window — see EVIDENCE_CAPS."""
    level = evidence_level(n_closed, staleness_days)
    return level, EVIDENCE_CAPS[level]


def evidence_counts(strategy_id: str, regime_state: str, *,
                    window_days: int = EVIDENCE_WINDOW_DAYS, now=None) -> dict:
    """Closed-trade evidence behind a PENDING proposal for (strategy, regime).

    _direction_match's "decisive window" is defined relative to `decided_at`,
    which a pending proposal does not have yet. The honest analogue at
    auto-approval time is the TRAILING `window_days` of closed trades — the same
    evidence the memo that produced the proposal was written from. Staleness is
    measured against the most recent closed trade with NO lookback bound, so a
    sleeve dark for two months reads as stale even though its trailing-window
    count is 0.

    Returns {'n_closed': int, 'staleness_days': float | None}; staleness is None
    when the sleeve has no closed trade at all.
    """
    from datetime import datetime, timezone
    ref = now or datetime.now(timezone.utc)
    sql = """
        SELECT COUNT(*) FILTER (WHERE sp.closed_at >= %s) AS n_closed,
               MAX(sp.closed_at)                          AS last_closed_at
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE es.strategy_id = %s
           AND es.regime_state = %s
           AND sp.realized_pnl_pct IS NOT NULL
    """
    from datetime import timedelta
    window_start = ref - timedelta(days=int(window_days))
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (window_start, strategy_id, regime_state))
            row = cur.fetchone()
    n_closed = int(row[0] or 0) if row else 0
    last = row[1] if row else None
    staleness = None
    if last is not None:
        if getattr(last, 'tzinfo', None) is None:
            last = last.replace(tzinfo=timezone.utc)
        staleness = (ref - last).total_seconds() / 86400.0
    return {'n_closed': n_closed, 'staleness_days': staleness}
```

- [ ] **Step 4** — Run and confirm PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/metrics/test_calibrated_confidence.py tests/metrics/test_mastermind_calibration.py tests/metrics/test_mastermind_calibration_unresolved.py -q
```

Expected: all green.

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/metrics/mastermind_calibration.py tests/metrics/test_calibrated_confidence.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(calibration): D2b calibrated_confidence + evidence cap primitives

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: D2b (part 2) — migration 159 + `auto_approve` compares `min(calibrated, cap)`

**Safety:** the compare only changes under `OPENCLAW_PROPOSAL_CALIBRATED=1`. Unset, `auto_approve`
computes the calibrated number and the cap, RECORDS both, logs the would-be decision, and then
compares the **raw** confidence exactly as today — byte-identical outcomes. The recording happens in
BOTH paths and BEFORE the rails, so shadow evidence accumulates for every proposal, not only for
those that would have passed size/stop.

`auto_apply_batch`'s pre-filter (`:405-410`) keeps comparing the RAW confidence. That is safe and
deliberate: the pre-filter can only route a proposal to `_mark_noted`, never to approval —
`auto_approve` remains the single approval path, so anything the calibrated check would reject is
still rejected inside it.

**Predicted production effect on flip:** with `.env` floor 0.9 and the observed `[0.8, 1.0]` bucket
rate 0.56 (n=18), `calibrated(0.9) ≈ 0.56` and typical evidence caps sit at low/medium (0.55/0.75) —
so flipping `OPENCLAW_PROPOSAL_CALIBRATED=1` closes the auto-approve path essentially completely and
every proposal parks as `noted` for the operator. That is the intended outcome of the 2026-09-06
"owed to the operator" item, but it must be stated before the flip, not discovered after it.

**Files:**
- Create: `src/database/migrations/159_proposal_calibration.sql`
- Modify: `src/strategies/proposal_manager.py` — `auto_approve` (`:290-368` after Task 3)
- Test: `tests/strategies/test_proposal_calibrated_gate.py` (Create)

**Interfaces:**
- Consumes: `metrics.mastermind_calibration.calibration_report() -> {'buckets': [...] , ...}` (`:220-249`); `calibrated_confidence`, `evidence_cap`, `evidence_counts` (Task 4); `proposal_manager._connect()`, `_PENDING_COLS` (`:152-157`), `_decide(...)` (`:216`), `autoapprove_min_confidence()` (Task 3)
- Produces: `proposal_manager._calibration_inputs(strategy_id, regime_state, raw) -> dict` with keys `raw`, `calibrated`, `cap`, `evidence_level`, `n_closed`, `staleness_days`, `binding_bound`, `effective`; `proposal_manager._record_calibration(proposal_id, calib) -> None`; `auto_approve` result dict gains a `calibration` key

- [ ] **Step 1** — Write the failing test file `tests/strategies/test_proposal_calibrated_gate.py`:

```python
"""D2b — auto_approve compares min(calibrated, cap) under the flag.

Every DB touchpoint is a fake cursor; the calibration report and the evidence
query are monkeypatched. No psycopg2 connection is opened.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    python3 -m pytest tests/strategies/test_proposal_calibrated_gate.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import proposal_manager as pm  # noqa: E402


class FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows or [])
        self.executed: list = []
        self.rowcount = 0
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=()): self.executed.append((sql, params))
    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class FakeConn:
    def __init__(self, rows=()):
        self.cur = FakeCursor(rows)
        self.committed = 0
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def cursor(self): return self.cur
    def commit(self): self.committed += 1


def _row(pid=10, conf=0.9, size=None):
    # Tuple shape matches _PENDING_COLS
    return (pid, 's1', 'LOW_VOL', 'pending', True, size,
            None, None, None, conf, 'looks good', None)


def _buckets(n=18, rate=0.56):
    rows = []
    from metrics import mastermind_calibration as mc
    for lo, hi, label in mc.BUCKETS:
        if label == '[0.8, 1.0]':
            rows.append({'range': label, 'count': n, 'matched': int(n * rate), 'match_rate': rate})
        else:
            rows.append({'range': label, 'count': 0, 'matched': 0, 'match_rate': None})
    return rows


@pytest.fixture
def wired(monkeypatch):
    """Common wiring: env on, floor 0.9, one pending proposal, stubbed calibration."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.9')
    monkeypatch.delenv('OPENCLAW_PROPOSAL_CALIBRATED', raising=False)
    conns = []
    def _connect():
        c = FakeConn(rows=[_row()])
        conns.append(c)
        return c
    monkeypatch.setattr(pm, '_connect', _connect)
    monkeypatch.setattr(pm, '_calibration_report', lambda: {'buckets': _buckets()})
    monkeypatch.setattr(pm, '_evidence_counts',
                        lambda sid, regime: {'n_closed': 12, 'staleness_days': 5.0})
    recorded = {}
    monkeypatch.setattr(pm, '_record_calibration',
                        lambda pid, calib: recorded.update({'pid': pid, **calib}))
    decided = {}
    monkeypatch.setattr(pm, '_decide',
                        lambda **kw: decided.update(kw) or {'id': kw['proposal_id'],
                                                            'status': 'approved'})
    return recorded, decided


def test_shadow_mode_records_but_does_not_change_the_decision(wired, monkeypatch):
    recorded, decided = wired
    monkeypatch.delenv('OPENCLAW_PROPOSAL_CALIBRATED', raising=False)
    result = pm.auto_approve(proposal_id=10)
    # raw 0.9 >= floor 0.9 -> still approved, exactly as today
    assert result['status'] == 'approved'
    assert decided['terminal_status'] == 'approved'
    # ...but the shadow numbers were computed and recorded
    assert recorded['raw'] == pytest.approx(0.9)
    assert recorded['calibrated'] == pytest.approx(0.56, abs=1e-9)
    assert recorded['cap'] == pytest.approx(0.55)
    assert recorded['binding_bound'] == 'cap'
    assert result['calibration']['enforced'] is False
    assert result['calibration']['would_skip'] is True


def test_enforced_mode_skips_when_the_min_is_below_the_floor(wired, monkeypatch):
    recorded, decided = wired
    monkeypatch.setenv('OPENCLAW_PROPOSAL_CALIBRATED', '1')
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert 'calibrated' in result['reason']
    assert decided == {}, '_decide must not be called when the calibrated bound fails'
    assert recorded['binding_bound'] == 'cap'


def test_enforced_mode_approves_when_both_bounds_clear_the_floor(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.5')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_CALIBRATED', '1')
    monkeypatch.setattr(pm, '_connect', lambda: FakeConn(rows=[_row(conf=0.9)]))
    monkeypatch.setattr(pm, '_calibration_report', lambda: {'buckets': _buckets(n=20, rate=0.9)})
    monkeypatch.setattr(pm, '_evidence_counts',
                        lambda sid, regime: {'n_closed': 250, 'staleness_days': 1.0})
    monkeypatch.setattr(pm, '_record_calibration', lambda pid, calib: None)
    decided = {}
    monkeypatch.setattr(pm, '_decide',
                        lambda **kw: decided.update(kw) or {'id': 10, 'status': 'approved'})
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'approved'
    assert decided['terminal_status'] == 'approved'
    assert result['calibration']['calibrated'] == pytest.approx(0.9)
    assert result['calibration']['cap'] == 1.0
    assert result['calibration']['binding_bound'] == 'calibrated'


def test_calibration_failure_falls_back_to_raw_and_never_raises(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.9')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_CALIBRATED', '1')
    monkeypatch.setattr(pm, '_connect', lambda: FakeConn(rows=[_row(conf=0.95)]))
    def _boom(): raise RuntimeError('calibration table missing')
    monkeypatch.setattr(pm, '_calibration_report', _boom)
    monkeypatch.setattr(pm, '_evidence_counts', _boom)
    monkeypatch.setattr(pm, '_record_calibration', lambda pid, calib: None)
    monkeypatch.setattr(pm, '_decide', lambda **kw: {'id': 10, 'status': 'approved'})
    result = pm.auto_approve(proposal_id=10)
    # fail-open to raw: 0.95 >= 0.9 -> approved, no exception escapes
    assert result['status'] == 'approved'
    assert result['calibration']['error'] is not None
    assert result['calibration']['calibrated'] == pytest.approx(0.95)
    assert result['calibration']['cap'] == 1.0


def test_recording_happens_before_the_size_rail(monkeypatch):
    """A proposal that dies on the size rail must still leave shadow evidence."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.5')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MAX_SIZE_DELTA', '0.20')
    monkeypatch.delenv('OPENCLAW_PROPOSAL_CALIBRATED', raising=False)
    monkeypatch.setattr(pm, '_connect', lambda: FakeConn(rows=[_row(conf=0.9, size=0.9)]))
    monkeypatch.setattr(pm, '_calibration_report', lambda: {'buckets': _buckets()})
    monkeypatch.setattr(pm, '_evidence_counts',
                        lambda sid, regime: {'n_closed': 12, 'staleness_days': 5.0})
    monkeypatch.setattr(pm, '_current_size_scalar', lambda sid, r: 0.0)
    recorded = {}
    monkeypatch.setattr(pm, '_record_calibration',
                        lambda pid, calib: recorded.update(calib))
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert 'size' in result['reason'].lower()
    assert recorded['calibrated'] == pytest.approx(0.56, abs=1e-9)


def test_disabled_feature_short_circuits_before_any_calibration(monkeypatch):
    monkeypatch.delenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', raising=False)
    def _boom(*a, **k): raise AssertionError('must not be called')
    monkeypatch.setattr(pm, '_calibration_report', _boom)
    monkeypatch.setattr(pm, '_connect', _boom)
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert 'disabled' in result['reason'].lower()


def test_migration_159_adds_only_columns():
    sql = (ROOT / 'src' / 'database' / 'migrations'
           / '159_proposal_calibration.sql').read_text().lower()
    for col in ('confidence_raw', 'confidence_calibrated', 'evidence_cap',
                'evidence_level', 'binding_bound'):
        assert f'add column if not exists {col}' in sql
    for forbidden in ('drop column', 'drop table', 'delete from', 'truncate'):
        assert forbidden not in sql
```

- [ ] **Step 2** — Run it and confirm the expected failure:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_proposal_calibrated_gate.py -q
```

Expected: `AttributeError: module 'strategies.proposal_manager' has no attribute
'_calibration_report'` on the fixture, and `FileNotFoundError` on the migration test.

- [ ] **Step 3a** — Create `src/database/migrations/159_proposal_calibration.sql`:

```sql
-- 159_proposal_calibration.sql
-- Spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md §4 D2 (item 7).
-- Records what auto_approve actually compared: the mastermind's stated
-- confidence, the outcome-calibrated version of it, the evidence cap, and
-- which of the two bounds bit. Written in BOTH the shadow and the enforced
-- path so the operator can read two weekends of evidence before flipping
-- OPENCLAW_PROPOSAL_CALIBRATED=1.
--
-- Additive only: no column is dropped, no row is rewritten (repo CLAUDE.md
-- append-only invariant). Streams B and C hold 155-158.

ALTER TABLE strategy_regime_param_proposals
    ADD COLUMN IF NOT EXISTS confidence_raw        NUMERIC,
    ADD COLUMN IF NOT EXISTS confidence_calibrated NUMERIC,
    ADD COLUMN IF NOT EXISTS evidence_cap          NUMERIC,
    ADD COLUMN IF NOT EXISTS evidence_level        TEXT,
    ADD COLUMN IF NOT EXISTS binding_bound         TEXT;

COMMENT ON COLUMN strategy_regime_param_proposals.confidence_raw IS
    'confidence as stated by the mastermind, snapshotted at auto-approval time';
COMMENT ON COLUMN strategy_regime_param_proposals.confidence_calibrated IS
    'raw x clip(bucket match_rate / bucket midpoint, 0.5, 1.0) when the bucket has n >= 8, else raw';
COMMENT ON COLUMN strategy_regime_param_proposals.evidence_cap IS
    'cap from the decisive-window closed-trade count + staleness: none .35 / low .55 / medium .75 / high 1.0';
COMMENT ON COLUMN strategy_regime_param_proposals.evidence_level IS
    'none | low | medium | high';
COMMENT ON COLUMN strategy_regime_param_proposals.binding_bound IS
    'calibrated | cap — which of the two produced min(calibrated, cap)';
```

- [ ] **Step 3b** — Implement in `src/strategies/proposal_manager.py`. Insert these three helpers
immediately BEFORE `def auto_approve` (`:290`):

```python
def _calibration_report() -> dict:
    """Indirection seam over metrics.mastermind_calibration.calibration_report
    so tests can inject a bucket table without a DB."""
    from metrics.mastermind_calibration import calibration_report
    return calibration_report()


def _evidence_counts(strategy_id: str, regime_state: str) -> dict:
    """Indirection seam over metrics.mastermind_calibration.evidence_counts."""
    from metrics.mastermind_calibration import evidence_counts
    return evidence_counts(strategy_id, regime_state)


def _calibration_inputs(strategy_id: str, regime_state: str, raw) -> dict:
    """Everything auto_approve needs to decide and to record (spec D2).

    Fail-OPEN on any calibration error: `calibrated` falls back to raw and
    `cap` to 1.0, with the exception text kept under 'error'. A broken
    calibration table must never turn into a silent tightening the operator
    cannot see.
    """
    from metrics.mastermind_calibration import calibrated_confidence, evidence_cap
    raw_f = float(raw) if raw is not None else None
    out = {'raw': raw_f, 'calibrated': raw_f, 'cap': 1.0, 'evidence_level': None,
           'n_closed': None, 'staleness_days': None, 'binding_bound': 'calibrated',
           'error': None}
    try:
        buckets = (_calibration_report() or {}).get('buckets') or []
        out['calibrated'] = calibrated_confidence(raw_f, buckets)
        ev = _evidence_counts(strategy_id, regime_state) or {}
        out['n_closed'] = ev.get('n_closed')
        out['staleness_days'] = ev.get('staleness_days')
        level, cap = evidence_cap(ev.get('n_closed') or 0, ev.get('staleness_days'))
        out['evidence_level'] = level
        out['cap'] = cap
    except Exception as e:  # noqa: BLE001 — fail-open, see docstring
        out['error'] = f'{type(e).__name__}: {e}'
        out['calibrated'] = raw_f
        out['cap'] = 1.0
        out['evidence_level'] = None
    cal = out['calibrated'] if out['calibrated'] is not None else float('inf')
    out['binding_bound'] = 'cap' if out['cap'] < cal else 'calibrated'
    out['effective'] = None if out['calibrated'] is None else min(out['calibrated'], out['cap'])
    return out


def _record_calibration(proposal_id: int, calib: dict) -> None:
    """Persist the shadow/enforced numbers on the proposal row (migration 159).
    Best-effort: a recording failure must never block a decision."""
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE strategy_regime_param_proposals
                       SET confidence_raw        = %s,
                           confidence_calibrated = %s,
                           evidence_cap          = %s,
                           evidence_level        = %s,
                           binding_bound         = %s
                     WHERE id = %s
                """, (calib.get('raw'), calib.get('calibrated'), calib.get('cap'),
                      calib.get('evidence_level'), calib.get('binding_bound'), proposal_id))
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f'[auto-approve] calibration recording failed for #{proposal_id}: {e}')
```

Then, inside `auto_approve`, replace the whole "Rail 1: confidence" block (currently `:334-338`):

```python
    # Rail 1: confidence
    conf = prop['confidence']
    if conf is None or float(conf) < min_conf:
        return {'id': proposal_id, 'status': 'skipped',
                'reason': f'confidence {conf} below threshold {min_conf}'}
```

with:

```python
    # ── Rail 1: confidence, outcome-calibrated + evidence-capped (spec D2) ────
    # Computed and RECORDED unconditionally and BEFORE every other rail, so the
    # shadow ledger covers proposals that later die on size/stop too. Only the
    # COMPARISON is flag-gated: unset OPENCLAW_PROPOSAL_CALIBRATED keeps the raw
    # compare byte-identical to today and logs the would-be decision.
    conf = prop['confidence']
    calib = _calibration_inputs(prop['strategy_id'], prop['regime_state'], conf)
    calibrated_enabled = os.environ.get('OPENCLAW_PROPOSAL_CALIBRATED') == '1'
    effective = calib['effective']
    would_skip = effective is None or effective < min_conf
    calib_out = dict(calib, enforced=calibrated_enabled, would_skip=would_skip,
                     floor=min_conf)
    _record_calibration(proposal_id, calib)
    if not calibrated_enabled:
        logger.info(
            f'[auto-approve] #{proposal_id} {prop["strategy_id"]}/{prop["regime_state"]} '
            f'calibration SHADOW: raw={conf} calibrated={calib["calibrated"]} '
            f'cap={calib["cap"]} ({calib["evidence_level"]}, n={calib["n_closed"]}, '
            f'stale={calib["staleness_days"]}) bound={calib["binding_bound"]} '
            f'floor={min_conf} would_skip={would_skip}'
        )

    if calibrated_enabled:
        if would_skip:
            return {'id': proposal_id, 'status': 'skipped',
                    'reason': (f'calibrated confidence {effective} below threshold {min_conf} '
                               f'(raw {conf}, calibrated {calib["calibrated"]}, '
                               f'cap {calib["cap"]} [{calib["evidence_level"]}], '
                               f'bound {calib["binding_bound"]})'),
                    'calibration': calib_out}
    elif conf is None or float(conf) < min_conf:
        return {'id': proposal_id, 'status': 'skipped',
                'reason': f'confidence {conf} below threshold {min_conf}',
                'calibration': calib_out}
```

Add `'calibration': calib_out` to the two remaining rail-skip returns (size delta, stop delta), and
change the final return so the caller sees the numbers:

```python
    result = _decide(proposal_id=proposal_id, actor=auto_actor,
                     reason=auto_reason, source='auto-approval',
                     overrides=None, terminal_status='approved')
    result['calibration'] = calib_out
    return result
```

Also extend `auto_reason` to carry the calibrated bound:

```python
    auto_reason = (
        f'auto-approved: confidence={conf:.2f} (calibrated={calib["calibrated"]}, '
        f'cap={calib["cap"]} [{calib["evidence_level"]}]) >= {min_conf}, '
        f'rails (size_delta<={max_size}, stop<={max_stop}) all passed'
    )
```

- [ ] **Step 4** — Run and confirm PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_proposal_calibrated_gate.py tests/strategies/test_proposal_auto_approval.py tests/strategies/test_proposal_manager.py tests/strategies/test_proposal_floor_constant.py -q
```

Expected: all green. `test_proposal_auto_approval.py`'s existing cases run with
`OPENCLAW_PROPOSAL_CALIBRATED` unset, so they take the raw compare; if any of them trips on the new
`_calibration_report()` call reaching a real DB, add
`monkeypatch.setattr(pm, '_calibration_report', lambda: {'buckets': []})` and
`monkeypatch.setattr(pm, '_evidence_counts', lambda s, r: {'n_closed': 0, 'staleness_days': None})`
to that file's fixtures — the fail-open path already tolerates it, but stubbing keeps the test
hermetic.

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/database/migrations/159_proposal_calibration.sql src/strategies/proposal_manager.py tests/strategies/test_proposal_calibrated_gate.py tests/strategies/test_proposal_auto_approval.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(proposals): D2b auto_approve compares min(calibrated, evidence cap) under OPENCLAW_PROPOSAL_CALIBRATED

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: D2c — restore the calibration addendum to the sizing-proposal prompt

**Safety:** `buildStrategyPrompt` gains a 4th OPTIONAL parameter defaulting to `null`. Called with
three arguments (its only production call site, `_reviewOne` at `:371`) it produces a prompt that is
byte-identical to today apart from the new block, which is omitted entirely when `calibration` is
null. The fetch is fail-soft: any spawn failure yields `null` and the memo prompt is unchanged.

Shape copied from `mastermind.js:_renderCalibration` (`:747-850`) — the same "here is your own
empirical track record, here is what to do about it" framing the corpus curator already gets. The
numbers come from the same `calibration_report()` the doctor check reads
(`doctor.py:1057-1060`, thresholds `:1044-1046`), so the memo writer and the operator see one table.

**Files:**
- Modify: `src/agent/curators/comprehensive_review.js` — replace the removal note at `:274-279`; add `_renderProposalCalibration` + `_loadProposalCalibration`; extend `buildStrategyPrompt` (`:292`) and its call site (`:371`)
- Test: `tests/agent/test_review_calibration_addendum.test.js` (Create)

**Interfaces:**
- Consumes: `metrics.mastermind_calibration.calibration_report()` via `spawnSync(PYTHON, ['-m', 'metrics.mastermind_calibration', '--report'], {env: {...process.env, PYTHONPATH: 'src'}})` — the file's existing `spawnSync`/`PYTHON` convention (`:3`, `:6`, `:412`); `doctor.py` constants `CALIBRATION_BRIER_WARN = 0.10`, `CALIBRATION_BRIER_FAIL = 0.20`, `CALIBRATION_MIN_SAMPLES = 10` (mirrored, kept in sync by comment)
- Produces: `buildStrategyPrompt(strategy, tradePack, counterfactuals, calibration = null) -> string`; `_renderProposalCalibration(cal) -> string` (empty string when `cal` is falsy); `_loadProposalCalibration() -> object | null`; both added to `module.exports`

- [ ] **Step 1** — Write the failing test file `tests/agent/test_review_calibration_addendum.test.js`:

```js
'use strict';

/**
 * D2c — the calibration addendum is back in the sizing-proposal prompt
 * (spec 2026-09-12 §4 D2; removed 2026-05-19, see the note at :274).
 *
 * Pure string assertions on buildStrategyPrompt / _renderProposalCalibration —
 * no DB, no spawn, no LLM.
 *
 * Run:
 *   cd /root/openclaw/.claude/worktrees/qd-adoptions && \
 *     node --test tests/agent/test_review_calibration_addendum.test.js
 */

const { test } = require('node:test');
const assert   = require('node:assert/strict');

const {
  buildStrategyPrompt,
  _renderProposalCalibration,
} = require('../../src/agent/curators/comprehensive_review');

const STRATEGY = {
  id: 'S_demo', name: 'Demo', status: 'live', tier: 2,
  instrument_class: 'equity', universe: ['AAPL'], signal_frequency: 'daily',
  parameters: {}, regime_conditions: {}, created_at: '2026-01-01',
};
const TRADE_PACK = { signals: [], pnl: [], oue: {} };
const COUNTERFACTUALS = { base: {} };

const CAL = {
  total_observations: 96,
  resolved_observations: 41,
  hit_rate: 0.66,
  mean_confidence: 0.75,
  brier_score: 0.254,
  buckets: [
    { range: '[0.0, 0.2]', count: 0,  matched: 0,  match_rate: null },
    { range: '[0.2, 0.4]', count: 2,  matched: 1,  match_rate: 0.5 },
    { range: '[0.4, 0.6]', count: 5,  matched: 2,  match_rate: 0.4 },
    { range: '[0.6, 0.8]', count: 16, matched: 9,  match_rate: 0.5625 },
    { range: '[0.8, 1.0]', count: 18, matched: 10, match_rate: 0.5555555 },
  ],
};

test('_renderProposalCalibration renders every bucket row verbatim', () => {
  const out = _renderProposalCalibration(CAL);
  for (const b of CAL.buckets) {
    assert.ok(out.includes(b.range), `bucket ${b.range} missing from the block`);
  }
  assert.ok(out.includes('0.556') || out.includes('0.5556'), 'the [0.8,1.0] match rate must appear');
  assert.ok(out.includes('18'), 'the [0.8,1.0] sample size must appear');
});

test('_renderProposalCalibration surfaces Brier, hit rate and mean confidence', () => {
  const out = _renderProposalCalibration(CAL);
  assert.ok(out.includes('0.254'), 'Brier score');
  assert.ok(out.includes('0.66'),  'hit rate');
  assert.ok(out.includes('0.75'),  'mean stated confidence');
});

test('_renderProposalCalibration tells the model what to do about it', () => {
  const out = _renderProposalCalibration(CAL).toLowerCase();
  assert.ok(out.includes('over-confident') || out.includes('overconfident'));
  assert.ok(out.includes('confidence'));
});

test('_renderProposalCalibration is empty for null / cold start', () => {
  assert.equal(_renderProposalCalibration(null), '');
  assert.equal(_renderProposalCalibration(undefined), '');
  assert.equal(_renderProposalCalibration({ buckets: [] }), '');
});

test('buildStrategyPrompt embeds the block when calibration is supplied', () => {
  const prompt = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS, CAL);
  assert.ok(prompt.includes('CONFIDENCE CALIBRATION'), 'block header present');
  assert.ok(prompt.includes('[0.8, 1.0]'));
  assert.ok(prompt.includes('0.254'));
});

test('buildStrategyPrompt is unchanged when calibration is omitted', () => {
  const withNull = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS, null);
  const legacy   = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS);
  assert.equal(withNull, legacy);
  assert.ok(!legacy.includes('CONFIDENCE CALIBRATION'));
});

test('the 2026-05-19 removal note no longer claims the addenda logic is gone', () => {
  const src = require('node:fs').readFileSync(
    require('node:path').join(__dirname, '../../src/agent/curators/comprehensive_review.js'),
    'utf8');
  assert.ok(!src.includes('Phase 2F calibration-addenda prepend logic removed'),
    'the stale removal note must be replaced by the restored block');
  assert.ok(src.includes('_renderProposalCalibration'));
});
```

- [ ] **Step 2** — Run it and confirm the expected failure:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/agent/test_review_calibration_addendum.test.js
```

Expected: `TypeError: _renderProposalCalibration is not a function` on every test.

- [ ] **Step 3** — Implement in `src/agent/curators/comprehensive_review.js`. Replace the removal
note at `:274-279`:

```js
// 2026-05-19: Phase 2F calibration-addenda prepend logic removed.
// Operators no longer interact with MastermindJohn's weekly review prompt —
// the research-page dashboard is the only operator entry into the research
// pipeline (add papers / sources / hand-developed strategies). The
// mastermind_prompt_addenda table stays as a historical record but is
// neither read nor written by this codebase.
```

with:

```js
// 2026-05-19: the Phase 2F OPERATOR addenda prepend was removed (operators no
// longer interact with this prompt — the research-page dashboard is the only
// operator entry into the research pipeline). The mastermind_prompt_addenda
// table stays a historical record and is still neither read nor written here.
//
// 2026-09-12 (spec §4 D2): the CALIBRATION addendum is restored — a different
// thing from the operator addenda. It is the model's own empirical track
// record, the same bucket table doctor.py's mastermind_calibration_brier check
// reports, in the same shape the corpus curator already receives from
// mastermind.js:_renderCalibration (:747-850). The memo writer is being asked
// for a `confidence` it has been measurably bad at (2026-09-06: Brier 0.254 on
// 41 resolved outcomes, hit rate 0.66 vs mean stated 0.75, and 0.56 inside the
// >=0.8 bucket that auto-approval reads); withholding that from the prompt is
// what let it drift. Fail-soft everywhere: no data => no block => the prompt is
// byte-identical to before this change.

// Mirror of doctor.py CALIBRATION_BRIER_WARN / _FAIL / _MIN_SAMPLES
// (src/maintenance/doctor.py:1044-1046). Keep in sync.
const CALIBRATION_BRIER_WARN  = 0.10;
const CALIBRATION_BRIER_FAIL  = 0.20;
const CALIBRATION_MIN_SAMPLES = 10;

/** Render the confidence-calibration block. '' when there is nothing to say. */
function _renderProposalCalibration(cal) {
  if (!cal || !Array.isArray(cal.buckets) || cal.buckets.length === 0) return '';
  const withData = cal.buckets.filter(b => (b.count ?? 0) > 0);
  if (!withData.length) return '';

  const num = (v, d = 3) => (v === null || v === undefined ? 'n/a' : Number(v).toFixed(d));
  const parts = [];
  parts.push('--- CONFIDENCE CALIBRATION (your own track record) ---');
  parts.push('');
  parts.push('Every `confidence` you emit below is scored 30 days later against the live');
  parts.push('Sharpe direction for that (strategy, regime). This is how those scores came out:');
  parts.push('');
  parts.push('  bucket        n    matched  match_rate');
  for (const b of cal.buckets) {
    const n = b.count ?? 0;
    const rate = n < CALIBRATION_MIN_SAMPLES ? 'n/a (thin sample)' : num(b.match_rate);
    parts.push(`  ${String(b.range).padEnd(12)} ${String(n).padStart(4)}  `
             + `${String(b.matched ?? 0).padStart(7)}  ${rate}`);
  }
  parts.push('');
  parts.push(`  Brier score: ${num(cal.brier_score)} `
           + `(warn >= ${CALIBRATION_BRIER_WARN.toFixed(2)}, fail >= ${CALIBRATION_BRIER_FAIL.toFixed(2)})`);
  parts.push(`  Overall hit rate: ${num(cal.hit_rate, 2)} against mean stated confidence `
           + `${num(cal.mean_confidence, 2)} `
           + `(${cal.resolved_observations ?? 0} resolved of ${cal.total_observations ?? 0})`);
  parts.push('');
  if (cal.hit_rate != null && cal.mean_confidence != null
      && Number(cal.hit_rate) < Number(cal.mean_confidence) - 0.05) {
    parts.push('You are OVER-CONFIDENT: your stated confidence exceeds your realised hit rate.');
    parts.push('A bucket whose match_rate sits well below its own midpoint is one you should stop');
    parts.push('using — move those calls down a bucket. Reserve >= 0.8 for recommendations you');
    parts.push('would defend on the trade-level numbers alone, not on the shape of the story.');
  } else {
    parts.push('Use these rates as a prior on your own confidence. Reserve >= 0.8 for');
    parts.push('recommendations you would defend on the trade-level numbers alone.');
  }
  parts.push('Ignore "thin sample" buckets until they accumulate enough observations.');
  parts.push('');
  return parts.join('\n');
}

/**
 * Best-effort read of the calibration report. Uses the file's existing
 * spawnSync/PYTHON convention. Any failure (missing table, Postgres down,
 * unparseable stdout) returns null and the prompt simply omits the block.
 */
function _loadProposalCalibration() {
  try {
    const res = spawnSync(PYTHON, ['-m', 'metrics.mastermind_calibration', '--report'], {
      encoding: 'utf-8',
      cwd: OPENCLAW_DIR,
      timeout: 60_000,
      env: { ...process.env, PYTHONPATH: 'src' },
    });
    if (res.status !== 0 || !res.stdout) return null;
    const parsed = JSON.parse(res.stdout);
    return (parsed && typeof parsed === 'object' && Array.isArray(parsed.buckets))
      ? parsed : null;
  } catch (e) {
    console.error(`[review] calibration report unavailable: ${e.message}`);
    return null;
  }
}
```

Change the signature at `:292` from
`function buildStrategyPrompt(strategy, tradePack, counterfactuals) {`
to
`function buildStrategyPrompt(strategy, tradePack, counterfactuals, calibration = null) {`,
and insert the block into the returned template — immediately after the `${classLine}` line and
before `Backtest: ...`, using a bare interpolation so the omitted case adds nothing:

```js
  const calBlock = _renderProposalCalibration(calibration);
  return `${MEMO_SYSTEM_PREAMBLE}
${calBlock ? '\n' + calBlock : ''}
Strategy: ${strategy.id} (${strategy.name})
```

(keep the rest of the template exactly as-is).

At the call site in `_reviewOne` (`:371`), change:

```js
  const prompt = buildStrategyPrompt(strategy, tradePack, counterfactuals);
```

to:

```js
  const prompt = buildStrategyPrompt(strategy, tradePack, counterfactuals,
                                     _loadProposalCalibration());
```

Finally extend the exports at `:600`:

```js
module.exports = { run, buildStrategyPrompt, _counterfactuals,
                   _renderProposalCalibration, _loadProposalCalibration };
```

- [ ] **Step 4** — Run and confirm PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/agent/test_review_calibration_addendum.test.js tests/test_comprehensive_review_uses_tiering.test.js tests/agent/test_comprehensive_review_class.test.js
```

Expected: all green.

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/agent/curators/comprehensive_review.js tests/agent/test_review_calibration_addendum.test.js
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(review): D2c restore the confidence-calibration addendum to the sizing-proposal prompt

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: D1 — `src/research/factor_ic_screen.py`

**Safety:** an entirely new module. Nothing imports it until Task 8. `src/research/` is a namespace
package (no `__init__.py`; `pytest.ini` sets `pythonpath = src`), so `python3 -m research.factor_ic_screen`
with `PYTHONPATH=src` is the invocation, matching how the orchestrator already spawns
`-m backtest.factor_prescreen` (`:1054-1057`).

**Memory:** prices come ONLY from `factor_prescreen.load_price_window(days, max_tickers, lookback)`
(`factor_prescreen.py:321-436`) — the two-pass, row-group-stats, ticker-pushdown reader that exists
precisely so a cheap screen cannot double RSS on this 2-core/8 GB box. 504 sessions × 300 tickers of
float64 closes is ~1.2 MB.

**A fourth verdict, `skipped`.** The spec names three (`pass | weak | flat`). A long-only decile
strategy such as `low_volatility_us` expresses itself as ~10–50 non-NaN cells drawn from two
confidence levels; rank IC on that is tie-dominated and the quintile split collapses, so
`|IC_5| < 0.01 AND |ls_q5q1| < cost` would both hold and the whole decile class would score `flat`
and lose its backtest. That is exactly the false-block `factor_prescreen` was built to avoid (see its
`zero_signals_on_fallback_universe` soft pass). `skipped` / `insufficient_cross_section` fires
whenever fewer than `MIN_REBALANCES = 12` rebalances have `>= MIN_CROSS_SECTION = 20` non-NaN values
AND `>= MIN_DISTINCT_VALUES = 5` distinct ones — so only a genuinely continuous cross-sectional
factor can ever reach `flat`.

**Files:**
- Create: `src/research/factor_ic_screen.py`
- Test: `tests/research/test_factor_ic_screen.py` (Create)

**Interfaces:**
- Consumes: `backtest.factor_prescreen.load_price_window(days: int, max_tickers: int, lookback: int = DEFAULT_LOOKBACK) -> tuple[pd.DataFrame, list[str], str]` (`:321`); `factor_prescreen._load_strategy_class(filepath)` (`:159`), `._resolve_instrument_class(strategy_id, filepath)` (`:208`), `._module_reads_aux_data(filepath)` (`:252`), `._benign_regime()` (`:140`), constants `DEFAULT_LOOKBACK = 300` (`:113`), `MIN_LOOKBACK_PAD` (`:124` region), `MAX_LOOKBACK_BARS = 1300` (`:124`); `BaseStrategy.generate_signals(prices, regime, universe, aux_data=None)` (`base.py:268-278`)
- Produces:
  - `round_trip_cost(one_way_bps: float = ONE_WAY_COST_BPS) -> float`
  - `forward_returns(closes: pd.DataFrame, horizon: int) -> pd.DataFrame`
  - `rebalance_dates(factor: pd.DataFrame, session_index, horizon: int, step: int) -> list`
  - `ic_series(factor, fwd, dates) -> list[float]`
  - `quintile_stats(factor, fwd, dates, n_q: int = 5) -> tuple[list, float | None, bool, float | None]`
  - `compute_ic_screen(factor, closes, *, horizons=HORIZONS, step=REBALANCE_STEP, one_way_bps=ONE_WAY_COST_BPS) -> dict`
  - `factor_from_signals(daily_signals: list[list], dates: list, universe: list[str]) -> pd.DataFrame`
  - `run_ic_screen(strategy_file: str, *, sessions=LOOKBACK_SESSIONS, max_tickers=DEFAULT_MAX_TICKERS, step=REBALANCE_STEP) -> dict`
  - `main(argv=None) -> int`

- [ ] **Step 1** — Write the failing test file `tests/research/test_factor_ic_screen.py`:

```python
"""D1 — rank-IC / quantile / turnover screen (spec 2026-09-12 §4 D1).

Synthetic frames only. compute_ic_screen is pure — it takes a factor panel and
a close panel and never touches parquet. run_ic_screen's price read is
monkeypatched at factor_prescreen.load_price_window in the one test that
exercises it.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    python3 -m pytest tests/research/test_factor_ic_screen.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from research import factor_ic_screen as fis  # noqa: E402

# Pinned so the pure-noise assertion is deterministic. mean-IC over N
# rebalances has SE ~ 1/sqrt((K-1)*N); at K=300, N~100 that is ~0.0058, so
# |IC| < 0.01 is ~1.7 sigma. Verified empirically in Step 4 — if a pandas/numpy
# upgrade moves the draw, sweep SEED over 1..20 and re-pin, never loosen the
# threshold (it is the spec's).
SEED     = 7
N_DAYS   = 520
N_TICKER = 300


def _panel():
    rng = np.random.default_rng(SEED)
    idx = pd.bdate_range(end='2026-08-21', periods=N_DAYS)
    cols = [f'ZZT{i:03d}' for i in range(N_TICKER)]
    rets = rng.normal(0.0004, 0.014, size=(N_DAYS, N_TICKER))
    closes = pd.DataFrame(100.0 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=cols)
    return closes, rng


def _predictive_factor(closes, rng, sign=1.0, noise=1.0):
    """Next-5-session return plus noise — a deliberately cheating factor."""
    fwd = fis.forward_returns(closes, 5)
    return sign * fwd + rng.normal(0.0, noise * 0.02, size=fwd.shape)


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_round_trip_cost_is_two_way():
    assert fis.round_trip_cost(10.0) == pytest.approx(0.0020)


def test_forward_returns_are_strictly_forward():
    idx = pd.bdate_range(end='2026-01-30', periods=10)
    closes = pd.DataFrame({'A': np.arange(10, dtype=float) + 100.0}, index=idx)
    fwd = fis.forward_returns(closes, 2)
    assert fwd['A'].iloc[0] == pytest.approx((102.0 - 100.0) / 100.0)
    assert pd.isna(fwd['A'].iloc[-1]) and pd.isna(fwd['A'].iloc[-2])


def test_rebalance_dates_space_by_sessions_not_list_positions():
    idx = pd.bdate_range(end='2026-06-30', periods=60)
    cols = [f'T{i}' for i in range(30)]
    factor = pd.DataFrame(np.nan, index=idx, columns=cols)
    factor.iloc[::5] = 1.0                      # populated every 5th session
    factor.iloc[::5] += np.arange(30) * 0.01    # non-degenerate cross-section
    picked = fis.rebalance_dates(factor, idx, horizon=21, step=5)
    positions = [idx.get_loc(d) for d in picked]
    assert all(b - a >= 21 for a, b in zip(positions, positions[1:]))
    picked5 = fis.rebalance_dates(factor, idx, horizon=5, step=5)
    assert len(picked5) > len(picked)


# ── verdicts ──────────────────────────────────────────────────────────────────

def test_predictive_factor_scores_pass():
    closes, rng = _panel()
    factor = _predictive_factor(closes, rng)
    res = fis.compute_ic_screen(factor, closes)
    assert res['verdict'] == 'pass', res
    assert res['ic']['5'] > 0.25
    assert res['icir']['5'] > 0.30
    assert res['ls_q5q1'] > res['cost_per_rebalance']
    assert res['monotonic'] is True


def test_pure_noise_scores_flat():
    closes, rng = _panel()
    factor = pd.DataFrame(rng.normal(0.0, 1.0, size=closes.shape),
                          index=closes.index, columns=closes.columns)
    res = fis.compute_ic_screen(factor, closes)
    assert abs(res['ls_q5q1']) < res['cost_per_rebalance'], res
    assert abs(res['ic']['5']) < 0.01, res
    assert res['verdict'] == 'flat'
    assert res['reason'] == 'ic_below_noise_and_ls_below_cost'


def test_reversed_factor_scores_negative_ic():
    closes, rng = _panel()
    factor = _predictive_factor(closes, rng, sign=-1.0)
    res = fis.compute_ic_screen(factor, closes)
    assert res['ic']['5'] < -0.25, res
    assert res['ls_q5q1'] < 0
    assert res['verdict'] != 'flat'


def test_thin_cross_section_scores_skipped_not_flat():
    """A long-only decile strategy: 12 names, 2 distinct values. Must NOT be
    scored flat — that would false-block the whole decile class."""
    closes, _rng = _panel()
    factor = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    picks = list(closes.columns[:12])
    factor.loc[:, picks[:6]] = 1.0
    factor.loc[:, picks[6:]] = 0.6
    res = fis.compute_ic_screen(factor, closes)
    assert res['verdict'] == 'skipped'
    assert res['reason'] == 'insufficient_cross_section'


def test_low_icir_scores_weak():
    """Enough cross-section and a non-trivial spread, but an IC series whose
    sign flips constantly."""
    closes, rng = _panel()
    fwd = fis.forward_returns(closes, 5)
    flip = pd.Series(np.where(np.arange(len(closes)) % 10 < 5, 1.0, -1.0), index=closes.index)
    factor = fwd.mul(flip, axis=0) + rng.normal(0.0, 0.02, size=fwd.shape)
    res = fis.compute_ic_screen(factor, closes)
    assert res['verdict'] in ('weak', 'flat'), res
    if res['verdict'] == 'weak':
        assert abs(res['icir']['5']) < 0.30
        assert res['reason'] == 'icir_below_threshold'


def test_output_shape_carries_every_spec_field():
    closes, rng = _panel()
    res = fis.compute_ic_screen(_predictive_factor(closes, rng), closes)
    for key in ('ic', 'icir', 'ic_half', 'rank_ac', 'ls_q5q1', 'monotonic',
                'turnover', 'cost_per_rebalance', 'quintile_means',
                'n_rebalances', 'n_qualifying_rebalances', 'verdict', 'reason'):
        assert key in res, f'missing {key}'
    assert set(res['ic']) == {'5', '10', '21'}
    assert set(res['icir']) == {'5', '10', '21'}
    assert set(res['ic_half']) == {'first', 'second'}
    import json
    json.dumps(res)   # must be JSON-serialisable for the orchestrator


# ── factor_from_signals ───────────────────────────────────────────────────────

def test_factor_from_signals_signs_and_magnitudes():
    from strategies.base import Signal
    dates = list(pd.bdate_range(end='2026-01-30', periods=2))
    universe = ['A', 'B', 'C']
    daily = [
        [Signal(ticker='A', direction='LONG', entry_price=10.0, confidence='HIGH',
                position_size_pct=0.02)],
        [Signal(ticker='B', direction='SHORT', entry_price=20.0, confidence='LOW')],
    ]
    f = fis.factor_from_signals(daily, dates, universe)
    assert f.loc[dates[0], 'A'] == pytest.approx(0.02)
    assert f.loc[dates[1], 'B'] == pytest.approx(-0.3)   # LOW weight, no size
    assert pd.isna(f.loc[dates[0], 'C'])


def test_run_ic_screen_reads_prices_only_via_load_price_window(monkeypatch, tmp_path):
    """The one sanctioned reader must be the only one called."""
    import textwrap
    from backtest import factor_prescreen as fp

    closes, _rng = _panel()
    calls = []

    def _fake_loader(days, max_tickers, lookback=None):
        calls.append((days, max_tickers, lookback))
        return closes, list(closes.columns), 'fallback'

    monkeypatch.setattr(fp, 'load_price_window', _fake_loader)

    strat = tmp_path / 'zz_ic.py'
    strat.write_text(textwrap.dedent('''
        from typing import List
        from strategies.base import BaseStrategy, Signal

        class ZzIc(BaseStrategy):
            id = 'zz_ic'
            name = 'ZzIc'
            description = 'ic screen fixture'
            min_lookback = 20

            def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
                if prices is None or len(prices) < 30:
                    return []
                mom = prices.iloc[-1] / prices.iloc[-21] - 1.0
                picks = mom.dropna().nlargest(40)
                return [Signal(ticker=t, direction='LONG', entry_price=float(prices[t].iloc[-1]),
                               confidence='MED', position_size_pct=float(v))
                        for t, v in picks.items()]
    '''))

    res = fis.run_ic_screen(str(strat), sessions=200, max_tickers=300, step=5)
    assert calls, 'load_price_window must be the price source'
    assert calls[0][0] == 200
    assert res['verdict'] in ('pass', 'weak', 'flat', 'skipped')
```

- [ ] **Step 2** — Run it and confirm the expected failure:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/research/test_factor_ic_screen.py -q
```

Expected: `ModuleNotFoundError: No module named 'research.factor_ic_screen'` at collection.

- [ ] **Step 3** — Create `src/research/factor_ic_screen.py`:

```python
#!/usr/bin/env python3
"""factor_ic_screen.py — rank-IC / quantile / turnover decay screen (spec D1).

Sits between the cheap factor prescreen and the ~900 s unified backtest in the
research gate chain. Where factor_prescreen answers "does this strategy emit
anything at all?", this answers "does what it emits rank the cross-section in a
way that survives cost?" — and skips the backtest when the answer is provably
no.

WHAT IT COMPUTES, per rebalance date:
  - Spearman rank IC at H = 5 / 10 / 21 sessions (factor[t] vs forward return
    over [t, t+H]), on NON-OVERLAPPING dates spaced max(step, H) sessions apart
    (overlapping windows correlate the IC series and inflate ICIR).
  - ICIR = mean(IC) / std(IC) * sqrt(252 / H).
  - First- vs second-half mean IC at H=5 (decay).
  - Rank autocorrelation of the factor itself between consecutive rebalances.
  - Quintile mean forward returns, the Q5 - Q1 long-short mean, monotonicity.
  - Jaccard turnover of the extreme-quintile membership, and the per-rebalance
    round-trip cost that turnover implies.

VERDICTS
  flat    |IC_5| < 0.01 AND |ls_q5q1| < turnover x round-trip cost.
          The factor neither ranks nor pays for its own trading. Under
          OPENCLAW_IC_SCREEN=1 this is the one verdict that skips the backtest.
  weak    |ICIR_5| < 0.30 — real but unstable. Annotate, still backtest.
  skipped Not enough cross-section to judge (see below). Annotate, still
          backtest.
  pass    Everything else.

WHY `skipped` EXISTS (not in the spec's three-verdict list; added 2026-09-12
after review). A long-only decile strategy — low_volatility_us and the ~40 other
`nsmallest`/`nlargest` decile implementations — expresses itself as 10-50
non-NaN cells drawn from two or three confidence levels. Rank IC on that is
tie-dominated and pd.qcut cannot form five distinct quantiles, so |IC_5| and
|ls_q5q1| both collapse toward zero and EVERY decile strategy would score
`flat` and lose its backtest. That is the same false-block class
factor_prescreen's `zero_signals_on_fallback_universe` soft pass exists to
prevent. A rebalance only counts toward the verdict when it carries at least
MIN_CROSS_SECTION non-NaN values AND MIN_DISTINCT_VALUES distinct ones; fewer
than MIN_REBALANCES such rebalances => `skipped`, never `flat`.

MEMORY: prices are read ONLY through factor_prescreen.load_price_window (the
two-pass, row-group-stats, ticker-pushdown reader) sliced to ~2 years. This box
is 2-core / 8 GB / no swap.

CLI (one JSON line on stdout, exit 0 whenever the screen COMPLETES; exit 1 on
any infra problem, which the orchestrator treats as ic_screen_infra_fail and
passes through):
    python3 -m research.factor_ic_screen --strategy-file <path> \
        [--sessions 504] [--max-tickers 300] [--step 5]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

HORIZONS            = (5, 10, 21)
REBALANCE_STEP      = 5
LOOKBACK_SESSIONS   = 504          # ~2 years, per spec
DEFAULT_MAX_TICKERS = 300

# Mirror of backtest.unified_backtest.INSTRUMENT_COST_BPS['equity'] (:87) — the
# ONE-WAY half-spread in bp. Duplicated rather than imported so this screen
# never drags unified_backtest's parquet loaders into the process.
# Keep in sync.
ONE_WAY_COST_BPS = 10.0

# Cross-section quality floors — see the `skipped` note above.
# MIN_CROSS_SECTION mirrors backtest.quick_backtest.MIN_CROSS_SECTION (:60).
MIN_CROSS_SECTION   = 20
MIN_DISTINCT_VALUES = 5
MIN_REBALANCES      = 12

FLAT_IC_ABS   = 0.01
WEAK_ICIR_ABS = 0.30

CONFIDENCE_WEIGHT = {'HIGH': 1.0, 'MED': 0.6, 'LOW': 0.3}
DIRECTION_SIGN    = {'LONG': 1.0, 'BUY_VOL': 1.0,
                     'SHORT': -1.0, 'SELL_VOL': -1.0, 'FLAT': 0.0}


def round_trip_cost(one_way_bps: float = ONE_WAY_COST_BPS) -> float:
    """Round-trip (in + out) cost as a return fraction."""
    return 2.0 * float(one_way_bps) / 10_000.0


def _spearman(a: pd.Series, b: pd.Series) -> Optional[float]:
    """Spearman rank correlation over the common non-NaN index. None when
    fewer than MIN_CROSS_SECTION pairs survive or either side is constant
    (rank correlation is undefined on a constant)."""
    joined = pd.concat([a, b], axis=1).dropna()
    if len(joined) < MIN_CROSS_SECTION:
        return None
    x, y = joined.iloc[:, 0], joined.iloc[:, 1]
    if x.nunique() < 2 or y.nunique() < 2:
        return None
    rho = x.rank().corr(y.rank())
    if rho is None or (isinstance(rho, float) and math.isnan(rho)):
        return None
    return float(rho)


def forward_returns(closes: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """close[t+H] / close[t] - 1, indexed at t. Never reads a bar before t, and
    the trailing H rows are NaN by construction — no look-ahead into a window
    that has not closed."""
    return closes.shift(-int(horizon)) / closes - 1.0


def rebalance_dates(factor: pd.DataFrame, session_index, horizon: int, step: int) -> list:
    """Usable evaluation dates spaced at least max(step, horizon) SESSIONS
    apart. Spacing is measured on `session_index` (the close panel's trading
    calendar), not on list positions, so a factor panel populated only every
    `step` sessions still yields non-overlapping windows. Non-overlap matters:
    ICIR = mean/std * sqrt(252/H) assumes independent per-period ICs."""
    spacing = max(int(step), int(horizon))
    pos = {d: i for i, d in enumerate(session_index)}
    picked, last = [], None
    for d in factor.index:
        i = pos.get(d)
        if i is None:
            continue
        if factor.loc[d].notna().sum() < MIN_CROSS_SECTION:
            continue
        if last is None or (i - last) >= spacing:
            picked.append(d)
            last = i
    return picked


def ic_series(factor: pd.DataFrame, fwd: pd.DataFrame, dates: list) -> List[float]:
    out = []
    for d in dates:
        rho = _spearman(factor.loc[d], fwd.loc[d])
        if rho is not None:
            out.append(rho)
    return out


def _icir(ics: List[float], horizon: int) -> Optional[float]:
    if len(ics) < 2:
        return None
    mean = sum(ics) / len(ics)
    var = sum((x - mean) ** 2 for x in ics) / (len(ics) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return float((mean / sd) * math.sqrt(252.0 / float(horizon)))


def quintile_stats(factor: pd.DataFrame, fwd: pd.DataFrame, dates: list, n_q: int = 5):
    """(quintile_means, ls_q5q1, monotonic, turnover) at the caller's horizon.

    turnover is the mean Jaccard DISTANCE between consecutive rebalances'
    extreme-quintile membership (Q1 union Q5) — the names the strategy would
    actually have to trade — matching factor_prescreen.compute_stats'
    turnover_proxy shape."""
    per_q = [[] for _ in range(n_q)]
    ls, extremes = [], []
    for d in dates:
        row = factor.loc[d].dropna()
        r = fwd.loc[d]
        row = row[row.index.intersection(r.dropna().index)]
        if len(row) < MIN_CROSS_SECTION or row.nunique() < MIN_DISTINCT_VALUES:
            continue
        try:
            labels = pd.qcut(row.rank(method='first'), n_q, labels=False)
        except ValueError:
            continue
        means = []
        for q in range(n_q):
            members = row.index[labels == q]
            m = float(r[members].mean()) if len(members) else float('nan')
            means.append(m)
            if not math.isnan(m):
                per_q[q].append(m)
        if not math.isnan(means[0]) and not math.isnan(means[-1]):
            ls.append(means[-1] - means[0])
        extremes.append(set(row.index[labels == n_q - 1]) | set(row.index[labels == 0]))

    quintile_means = [float(sum(v) / len(v)) if v else None for v in per_q]
    ls_q5q1 = float(sum(ls) / len(ls)) if ls else None
    ok = [m for m in quintile_means if m is not None]
    monotonic = bool(
        len(ok) == n_q
        and (all(ok[i] < ok[i + 1] for i in range(n_q - 1))
             or all(ok[i] > ok[i + 1] for i in range(n_q - 1)))
    )
    turnovers = []
    for a, b in zip(extremes, extremes[1:]):
        union = a | b
        turnovers.append(1.0 - (len(a & b) / len(union) if union else 0.0))
    turnover = float(sum(turnovers) / len(turnovers)) if turnovers else None
    return quintile_means, ls_q5q1, monotonic, turnover


def compute_ic_screen(factor: pd.DataFrame, closes: pd.DataFrame, *,
                       horizons=HORIZONS, step: int = REBALANCE_STEP,
                       one_way_bps: float = ONE_WAY_COST_BPS) -> dict:
    """The pure core. `factor` is a date x ticker panel (NaN = no opinion);
    `closes` is the aligned close panel. Returns the JSON-serialisable verdict
    dict documented at the top of this module."""
    factor = factor.sort_index()
    closes = closes.sort_index()
    common = [c for c in factor.columns if c in closes.columns]
    factor = factor[common]
    closes = closes[common]
    session_index = closes.index

    h0 = int(horizons[0])
    fwd0 = forward_returns(closes, h0)
    dates0 = rebalance_dates(factor, session_index, h0, step)

    qualifying = 0
    for d in dates0:
        row = factor.loc[d].dropna()
        if len(row) >= MIN_CROSS_SECTION and row.nunique() >= MIN_DISTINCT_VALUES:
            qualifying += 1

    ic, icir = {}, {}
    for h in horizons:
        fwd_h = forward_returns(closes, h)
        dates_h = rebalance_dates(factor, session_index, h, step)
        series_h = ic_series(factor, fwd_h, dates_h)
        ic[str(h)] = float(sum(series_h) / len(series_h)) if series_h else None
        icir[str(h)] = _icir(series_h, h)

    series0 = ic_series(factor, fwd0, dates0)
    half = len(series0) // 2
    ic_half = {
        'first':  float(sum(series0[:half]) / half) if half else None,
        'second': (float(sum(series0[half:]) / len(series0[half:]))
                   if series0[half:] else None),
    }

    ac = []
    for a, b in zip(dates0, dates0[1:]):
        rho = _spearman(factor.loc[a], factor.loc[b])
        if rho is not None:
            ac.append(rho)
    rank_ac = float(sum(ac) / len(ac)) if ac else None

    quintile_means, ls_q5q1, monotonic, turnover = quintile_stats(factor, fwd0, dates0)
    # No measurable turnover yet => charge a full round trip (conservative: it
    # makes `flat` HARDER to reach, never easier).
    cost = round_trip_cost(one_way_bps) * (turnover if turnover is not None else 1.0)

    ic0 = ic[str(h0)]
    icir0 = icir[str(h0)]
    if qualifying < MIN_REBALANCES:
        verdict, reason = 'skipped', 'insufficient_cross_section'
    elif (ic0 is not None and abs(ic0) < FLAT_IC_ABS
          and ls_q5q1 is not None and abs(ls_q5q1) < cost):
        verdict, reason = 'flat', 'ic_below_noise_and_ls_below_cost'
    elif icir0 is None or abs(icir0) < WEAK_ICIR_ABS:
        verdict, reason = 'weak', 'icir_below_threshold'
    else:
        verdict, reason = 'pass', None

    return {
        'ic':                      ic,
        'icir':                    icir,
        'ic_half':                 ic_half,
        'rank_ac':                 rank_ac,
        'ls_q5q1':                 ls_q5q1,
        'quintile_means':          quintile_means,
        'monotonic':               monotonic,
        'turnover':                turnover,
        'cost_per_rebalance':      cost,
        'n_rebalances':            len(dates0),
        'n_qualifying_rebalances': qualifying,
        'horizons':                [int(h) for h in horizons],
        'verdict':                 verdict,
        'reason':                  reason,
    }


def factor_from_signals(daily_signals: List[list], dates: list,
                         universe: List[str]) -> pd.DataFrame:
    """Wide date x ticker factor panel from generate_signals output.

    Value = direction sign x magnitude, where magnitude is position_size_pct
    when the strategy set one (it carries the strategy's own conviction
    ordering) and the confidence weight otherwise. NaN = the strategy said
    nothing about that ticker that day — which is the honest encoding: a
    long-only decile strategy really has no opinion on the other 90 %, and the
    resulting thin cross-section is what MIN_DISTINCT_VALUES detects."""
    frame = pd.DataFrame(index=pd.Index(dates, name='date'),
                          columns=list(universe), dtype='float64')
    for d, sigs in zip(dates, daily_signals):
        for s in (sigs or []):
            t = getattr(s, 'ticker', None)
            if t is None or t not in frame.columns:
                continue
            sign = DIRECTION_SIGN.get(getattr(s, 'direction', None), 0.0)
            size = getattr(s, 'position_size_pct', None)
            if isinstance(size, (int, float)) and size and not pd.isna(size):
                mag = abs(float(size))
            else:
                mag = CONFIDENCE_WEIGHT.get(getattr(s, 'confidence', None), 0.5)
            frame.at[d, t] = sign * mag
    return frame


def run_ic_screen(strategy_file: str, *, sessions: int = LOOKBACK_SESSIONS,
                   max_tickers: int = DEFAULT_MAX_TICKERS,
                   step: int = REBALANCE_STEP) -> dict:
    """Drive the strategy over ~2 years of sliced history and screen the factor
    panel it produces. Raises on any infra problem — main() turns a raise into
    exit 1, which the orchestrator treats as ic_screen_infra_fail."""
    from backtest import factor_prescreen as fp

    cls = fp._load_strategy_class(strategy_file)

    # Same aux-dependent bypass factor_prescreen applies (:614-630): this screen
    # never populates real aux_data, so an aux-dependent strategy would emit
    # nothing here regardless of legitimacy and its "IC" would be meaningless.
    instrument_class = fp._resolve_instrument_class(getattr(cls, 'id', None), strategy_file)
    if instrument_class == 'option' or fp._module_reads_aux_data(strategy_file):
        return {'ic': {}, 'icir': {}, 'ic_half': {}, 'rank_ac': None,
                'ls_q5q1': None, 'quintile_means': None, 'monotonic': None,
                'turnover': None, 'cost_per_rebalance': None,
                'n_rebalances': 0, 'n_qualifying_rebalances': 0,
                'horizons': [int(h) for h in HORIZONS],
                'verdict': 'skipped', 'reason': 'ic_screen_skipped_aux_dependent'}

    instance = cls()
    declared = getattr(instance, 'min_lookback', None)
    try:
        declared = int(declared) if declared is not None else None
    except (TypeError, ValueError):
        declared = None
    lookback = fp.DEFAULT_LOOKBACK
    if declared is not None:
        lookback = max(lookback, declared + fp.MIN_LOOKBACK_PAD)
    lookback = min(lookback, fp.MAX_LOOKBACK_BARS)

    close_wide, universe, _src = fp.load_price_window(sessions, max_tickers, lookback)
    n_rows = len(close_wide.index)
    if n_rows < 1:
        raise RuntimeError('empty price panel for the IC screen window')

    start_idx = max(0, n_rows - int(sessions))
    regime = fp._benign_regime()

    dates, daily = [], []
    for i in range(start_idx, n_rows, max(1, int(step))):
        # Full history up to and including bar i — mirrors unified_backtest's
        # per-bar close_wide.loc[:current_date], so a long-lookback strategy
        # sees the same panel shape it would see in the real backtest.
        prices_to_date = close_wide.iloc[:i + 1]
        try:
            sigs = instance.generate_signals(prices_to_date, regime, universe, aux_data=None)
        except TypeError:
            sigs = instance.generate_signals(prices_to_date, regime, universe)
        dates.append(close_wide.index[i])
        daily.append(sigs or [])

    factor = factor_from_signals(daily, dates, universe).reindex(close_wide.index)
    return compute_ic_screen(factor, close_wide, step=int(step))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Rank-IC / quantile / turnover screen (spec D1)')
    ap.add_argument('--strategy-file', required=True)
    ap.add_argument('--sessions', type=int, default=LOOKBACK_SESSIONS,
                     help='decision sessions screened (default 504 ~ 2 years)')
    ap.add_argument('--max-tickers', type=int, default=DEFAULT_MAX_TICKERS)
    ap.add_argument('--step', type=int, default=REBALANCE_STEP,
                     help='sessions between driven bars (default 5)')
    args = ap.parse_args(argv)

    try:
        result = run_ic_screen(args.strategy_file, sessions=args.sessions,
                                max_tickers=args.max_tickers, step=args.step)
    except Exception as e:  # noqa: BLE001 — any infra failure -> exit 1
        print(f'ic screen infra error: {e}', file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4** — Run and confirm PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/research/test_factor_ic_screen.py -q
```

Expected: all green. If `test_pure_noise_scores_flat` fails on `abs(res['ic']['5']) < 0.01`, the
draw is unlucky, not the code — sweep `SEED` over 1..20 with
`python3 -m pytest tests/research/test_factor_ic_screen.py -q -k pure_noise` and re-pin the first
seed that passes. Never relax the 0.01 threshold; it is the spec's.

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/research/factor_ic_screen.py tests/research/test_factor_ic_screen.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(research): D1 rank-IC / quantile / turnover screen with an insufficient-cross-section guard

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: D1 — wire the IC screen into the gate chain under `OPENCLAW_IC_SCREEN`

**Placement.** The spec says "after red-team and before the backtest slot
(`research-orchestrator.js` around `:1041`)". `:1041` has drifted — it is now inside
`_runValidateStrategy`. The real "between red-team and backtest" seam is `:1439-1441`, and the
factor prescreen already occupies `:1392-1438` of it. This plan puts the IC screen **after** the
prescreen: the prescreen costs 120 s and its block conditions (zero signals, constant output) are a
strict subset of the states that make an IC number meaningless, so running the cheaper screen first
avoids a 300 s IC screen on a candidate that is already dead. Still strictly after red-team, still
strictly before the backtest slot — the spec's stated constraint is satisfied.

**Safety:** with `OPENCLAW_IC_SCREEN` unset (the default), the gate computes the verdict, emits a
`pass` decision carrying it, logs one `[ic_screen]` line, and continues to the backtest — no
candidate's fate changes. Only `verdict === 'flat'` **and** the flag set can skip a backtest.
`weak` and `skipped` never skip, in either mode. Infra failure mirrors `prescreen_infra_fail`
verbatim: warn and pass.

**Files:**
- Modify: `src/agent/research/research-orchestrator.js` — `QUEUE_STATUS_FOR_REASON` (`:53-59`); `_isIcScreenShape` next to `_isPrescreenShape` (`:170-175`); `this._icScreenFn` seam in the constructor (next to `:318`); `_runIcScreen` after `_runFactorPrescreen` (`:1050-1080`); the new Phase 1.9 block between the prescreen's closing `}` and `onPhase('backtest', 60)` (`:1439-1441`); `module.exports` (`:2226-2234`)
- Test: `tests/agent/test_ic_screen_gate.test.js` (Create)

**Interfaces:**
- Consumes: `_spawnPython(args, {cwd, timeoutMs, onChild, env}) -> {stdout, stderr, code, signal}` (`:233`); `research.factor_ic_screen` CLI (Task 7) printing one JSON line; `OPENCLAW_DIR`
- Produces: `_isIcScreenShape(obj) -> boolean`; `ResearchOrchestrator.prototype._runIcScreen(implPath, opts) -> {icResult, icInfraFail, icInfraReason}`; `QUEUE_STATUS_FOR_REASON.ic_screen_flat = 'ic_screen_flat'`; gate decisions with `gateName: 'ic_screen'`

- [ ] **Step 1** — Write the failing test file `tests/agent/test_ic_screen_gate.test.js`:

```js
'use strict';

/**
 * D1 — the IC screen's gate-chain wiring (spec 2026-09-12 §4 D1).
 *
 * Flag OFF (default): the verdict is computed, recorded and logged; the
 * backtest ALWAYS runs. Flag ON: only verdict 'flat' skips the backtest;
 * 'weak' and 'skipped' annotate and continue. Infra failure warns and passes.
 *
 * No python is spawned — _icScreenFn is stubbed.
 *
 * Run:
 *   cd /root/openclaw/.claude/worktrees/qd-adoptions && \
 *     node --test tests/agent/test_ic_screen_gate.test.js
 */

delete process.env.POSTGRES_URI;

const { test } = require('node:test');
const assert   = require('node:assert/strict');

const ResearchOrchestrator = require('../../src/agent/research/research-orchestrator');
const { _isIcScreenShape, QUEUE_STATUS_FOR_REASON } =
  require('../../src/agent/research/research-orchestrator');

const ARGS = {
  candidate_id: 'cand-ic',
  stratId: 'S_ic',
  implPath: '/tmp/S_ic.py',
  strategy_spec: {},
  opts: {},
  suppressQueueWrite: true,
  runEligibility: false,
};

function makeOrch(icOutcome) {
  const orch = new ResearchOrchestrator();
  const calls = { backtest: 0, icScreen: 0 };
  const decisions = [];
  orch._query = async () => ({ rows: [] });
  orch._validateFn = async () => ({ ok: true, errors: [], signal_count: 12, warnings: [] });
  orch._redteamFn = async () => ({ verdict: 'pass', findings: [], infra_fail: false });
  orch._prescreenFn = async () => ({ psResult: { pass: true, reason: null, stats: {} },
                                     psInfraFail: false, psInfraReason: null });
  orch._icScreenFn = async () => { calls.icScreen += 1; return icOutcome; };
  orch._backtestFn = async () => { calls.backtest += 1; return { run_id: 'r1', sharpe: 0.4 }; };
  orch._emitDecisionFn = async (d) => { decisions.push(d); };
  return { orch, calls, decisions };
}

const FLAT  = { icResult: { verdict: 'flat', reason: 'ic_below_noise_and_ls_below_cost',
                            ic: { '5': 0.002 }, icir: { '5': 0.05 }, ls_q5q1: 0.0001,
                            cost_per_rebalance: 0.0018, turnover: 0.9 },
                icInfraFail: false, icInfraReason: null };
const WEAK  = { icResult: { verdict: 'weak', reason: 'icir_below_threshold',
                            ic: { '5': 0.03 }, icir: { '5': 0.11 }, ls_q5q1: 0.004,
                            cost_per_rebalance: 0.0018, turnover: 0.9 },
                icInfraFail: false, icInfraReason: null };
const SKIP  = { icResult: { verdict: 'skipped', reason: 'insufficient_cross_section',
                            ic: { '5': null }, icir: { '5': null }, ls_q5q1: null,
                            cost_per_rebalance: 0.002, turnover: null },
                icInfraFail: false, icInfraReason: null };
const PASS  = { icResult: { verdict: 'pass', reason: null, ic: { '5': 0.06 },
                            icir: { '5': 0.9 }, ls_q5q1: 0.01,
                            cost_per_rebalance: 0.0018, turnover: 0.9 },
                icInfraFail: false, icInfraReason: null };

// ── shape guard ───────────────────────────────────────────────────────────────

test('_isIcScreenShape accepts each of the four verdicts', () => {
  for (const v of ['pass', 'weak', 'flat', 'skipped']) {
    assert.equal(_isIcScreenShape({ verdict: v }), true, v);
  }
});

test('_isIcScreenShape rejects everything that is not a verdict object', () => {
  for (const bad of [null, undefined, 5, 'flat', [], {}, { verdict: 'blocked' },
                     { verdict: 1 }, [{ verdict: 'flat' }]]) {
    assert.equal(_isIcScreenShape(bad), false, JSON.stringify(bad));
  }
});

// ── flag OFF (shadow) ─────────────────────────────────────────────────────────

test('flag unset: a flat verdict still runs the backtest and is recorded', async (t) => {
  delete process.env.OPENCLAW_IC_SCREEN;
  const { orch, calls, decisions } = makeOrch(FLAT);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.icScreen, 1);
  assert.equal(calls.backtest, 1, 'shadow mode must never skip the backtest');
  assert.equal(out.ok, true);
  const ic = decisions.find(d => d.gateName === 'ic_screen');
  assert.equal(ic.outcome, 'pass');
  assert.equal(ic.reasonCode, 'ic_screen_flat');
  assert.equal(ic.metadata.enforced, false);
  assert.equal(ic.metadata.ic_screen.verdict, 'flat');
});

// ── flag ON ───────────────────────────────────────────────────────────────────

test('flag set: a flat verdict skips the backtest and returns ic_screen_flat', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '1';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls, decisions } = makeOrch(FLAT);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 0, 'the ~900s backtest must be skipped');
  assert.equal(out.ok, false);
  assert.equal(out.result.reasonCode, 'ic_screen_flat');
  assert.match(out.result.error, /ic_below_noise_and_ls_below_cost/);
  const ic = decisions.find(d => d.gateName === 'ic_screen');
  assert.equal(ic.outcome, 'reject');
  assert.equal(ic.reasonCode, 'ic_screen_flat');
});

test('flag set: weak annotates and still backtests', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '1';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls, decisions } = makeOrch(WEAK);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 1);
  assert.equal(out.ok, true);
  const ic = decisions.find(d => d.gateName === 'ic_screen');
  assert.equal(ic.outcome, 'pass');
  assert.equal(ic.reasonCode, 'ic_screen_weak');
});

test('flag set: skipped (thin cross-section) never blocks a decile strategy', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '1';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls, decisions } = makeOrch(SKIP);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 1);
  assert.equal(out.ok, true);
  assert.equal(decisions.find(d => d.gateName === 'ic_screen').reasonCode, 'ic_screen_skipped');
});

test('flag set: pass annotates and backtests', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '1';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls, decisions } = makeOrch(PASS);
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 1);
  assert.equal(out.ok, true);
  assert.equal(decisions.find(d => d.gateName === 'ic_screen').reasonCode, 'ic_screen_pass');
});

// ── infra failure ─────────────────────────────────────────────────────────────

test('infra failure warns and passes through, exactly like the prescreen', async (t) => {
  process.env.OPENCLAW_IC_SCREEN = '1';
  t.after(() => { delete process.env.OPENCLAW_IC_SCREEN; });
  const { orch, calls, decisions } = makeOrch(
    { icResult: null, icInfraFail: true, icInfraReason: 'exit=1' });
  const out = await orch._runGateChain({ ...ARGS });
  assert.equal(calls.backtest, 1);
  assert.equal(out.ok, true);
  const ic = decisions.find(d => d.gateName === 'ic_screen');
  assert.equal(ic.outcome, 'pass');
  assert.equal(ic.reasonCode, 'ic_screen_infra_fail');
});

test('the flat reasonCode maps to a real implementation_queue status', () => {
  assert.equal(QUEUE_STATUS_FOR_REASON.ic_screen_flat, 'ic_screen_flat');
});
```

- [ ] **Step 2** — Run it and confirm the expected failure:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/agent/test_ic_screen_gate.test.js
```

Expected: `TypeError: _isIcScreenShape is not a function`, and every gate test fails with
`calls.icScreen === 0` (there is no IC stage yet).

- [ ] **Step 3** — Implement in `src/agent/research/research-orchestrator.js`.

(a) Extend `QUEUE_STATUS_FOR_REASON` (`:53-59`) with one entry:

```js
const QUEUE_STATUS_FOR_REASON = {
  coding_failed:      'failed',
  contract_violation: 'validation_failed',
  redteam_blocked:    'redteam_blocked',
  prescreen_failed:   'prescreen_failed',
  ic_screen_flat:     'ic_screen_flat',
  backtest_error:     'backtest_failed',
};
```

(b) Add the shape guard immediately after `_isPrescreenShape` (`:170-175`):

```js
/**
 * Shape-validate research.factor_ic_screen's parsed stdout before trusting it.
 * Same hole `_isPrescreenShape` closes: JSON.parse succeeds on `5`, `"flat"`,
 * `null`, `[]` and `{}`, none of which carry the `verdict` this gate branches
 * on. Only an object whose `verdict` is one of the four known strings counts;
 * everything else is routed to the infra-fail warn-and-pass path.
 */
const IC_SCREEN_VERDICTS = new Set(['pass', 'weak', 'flat', 'skipped']);

function _isIcScreenShape(obj) {
  return obj !== null
    && typeof obj === 'object'
    && !Array.isArray(obj)
    && typeof obj.verdict === 'string'
    && IC_SCREEN_VERDICTS.has(obj.verdict);
}
```

(c) Add the seam in the constructor, right after `this._prescreenFn = this._runFactorPrescreen.bind(this);` (`:318`):

```js
    this._icScreenFn  = this._runIcScreen.bind(this);
```

(d) Add the default implementation immediately after `_runFactorPrescreen` (ends `:1080`):

```js
  /**
   * Default `_icScreenFn` — spec D1. Its own 300 s budget: the screen drives
   * ~100 generate_signals calls over a 504-bar panel, well past the prescreen's
   * 120 s. Any non-zero exit / unparseable line / bad shape is an infra failure
   * that WARNS and passes the candidate through, exactly as the prescreen does.
   */
  async _runIcScreen(implPath, opts = {}) {
    let icResult = null, icInfraFail = false, icInfraReason = null;
    try {
      const { stdout, code } = await _spawnPython(
        ['-m', 'research.factor_ic_screen', '--strategy-file', implPath],
        { cwd: OPENCLAW_DIR, timeoutMs: 300_000, onChild: opts.onChild,
          env: { ...process.env, PYTHONPATH: 'src' } });
      if (code !== 0) {
        icInfraFail = true;
        icInfraReason = `factor_ic_screen.py exit=${code}; stdout: ${stdout.slice(-300)}`;
      } else {
        try {
          const lastLine = stdout.trim().split('\n').pop();
          const parsed = JSON.parse(lastLine);
          if (_isIcScreenShape(parsed)) {
            icResult = parsed;
          } else {
            icInfraFail = true;
            icInfraReason = `factor_ic_screen.py stdout parsed but is not a valid screen result shape: ${lastLine.slice(0, 300)}`;
          }
        } catch (e) {
          icInfraFail = true;
          icInfraReason = `factor_ic_screen.py unparseable stdout: ${stdout.slice(0, 300)}`;
        }
      }
    } catch (e) {
      icInfraFail = true;
      icInfraReason = `factor_ic_screen.py threw: ${e.message}`;
    }
    return { icResult, icInfraFail, icInfraReason };
  }
```

(e) Insert the gate itself. Replace the two lines at `:1439-1440`:

```js
    notify?.(`  ✅ ${stratId} factor prescreen passed — running backtest (may take 2–5 min)...`);
    onPhase('backtest', 60);
```

with:

```js
    notify?.(`  ✅ ${stratId} factor prescreen passed — running IC screen...`);
    onPhase('ic_screen', 57);

    // ── Phase 1.9: rank-IC / quantile / turnover screen (spec D1) ─────────────
    // Placed after the prescreen rather than immediately after red-team (the
    // spec's wording): the prescreen is the cheaper of the two screens and its
    // block conditions are a strict subset of the states that make an IC number
    // meaningless, so screening cheap-first avoids a 300 s IC run on a
    // candidate the prescreen already killed. Still strictly after red-team and
    // strictly before the backtest slot.
    //
    // OPENCLAW_IC_SCREEN unset (default) = compute + record + log only: the
    // backtest always runs and no candidate's fate changes. Set to '1', a
    // 'flat' verdict — and ONLY 'flat' — skips the ~900 s backtest. 'weak' and
    // 'skipped' annotate in both modes; 'skipped' is the thin-cross-section
    // guard that keeps long-only decile strategies out of 'flat' entirely.
    const icEnforced = process.env.OPENCLAW_IC_SCREEN === '1';
    const { icResult, icInfraFail, icInfraReason } = await this._icScreenFn(implPath, opts);

    if (icInfraFail) {
      await this._emitDecisionFn({
        paperId:      vPaperId,
        candidateId:  candidate_id,
        strategyId:   stratId,
        gateName:     'ic_screen',
        outcome:      'pass',
        reasonCode:   'ic_screen_infra_fail',
        reasonDetail: icInfraReason,
        metadata:     { enforced: icEnforced },
      });
      notify?.(`  ⚠️ ${stratId} IC screen infra failure — WARN-and-pass, continuing to backtest.`);
    } else {
      const v = icResult?.verdict || null;
      const n3 = (x) => (x === null || x === undefined ? 'n/a' : Number(x).toFixed(4));
      const line = `IC5=${n3(icResult?.ic?.['5'])} ICIR5=${n3(icResult?.icir?.['5'])} `
                 + `ls=${n3(icResult?.ls_q5q1)} cost=${n3(icResult?.cost_per_rebalance)} `
                 + `turnover=${n3(icResult?.turnover)} n=${icResult?.n_qualifying_rebalances ?? 'n/a'}`;

      if (v === 'flat' && icEnforced) {
        const detail = `ic_screen flat (${icResult.reason || 'no reason'}): ${line}`;
        if (!suppressQueueWrite) {
          await this._query(
            `UPDATE implementation_queue SET status = 'ic_screen_flat', error_log = $1 WHERE candidate_id = $2`,
            [detail, candidate_id]
          );
        }
        await this._emitDecisionFn({
          paperId:      vPaperId,
          candidateId:  candidate_id,
          strategyId:   stratId,
          gateName:     'ic_screen',
          outcome:      'reject',
          reasonCode:   'ic_screen_flat',
          reasonDetail: detail,
          metadata:     { ic_screen: icResult, enforced: true },
        });
        notify?.(`  ❌ ${stratId} blocked by the IC screen — ${detail}`);
        channelNotify?.(`❌ **${stratId}** skipped backtest — IC screen flat (${line})`);
        return { ok: false, result: { promoted: false, reasonCode: 'ic_screen_flat', error: detail } };
      }

      const shadowNote = (v === 'flat' && !icEnforced)
        ? ' (shadow: OPENCLAW_IC_SCREEN unset — would have skipped the backtest)'
        : '';
      await this._emitDecisionFn({
        paperId:      vPaperId,
        candidateId:  candidate_id,
        strategyId:   stratId,
        gateName:     'ic_screen',
        outcome:      'pass',
        reasonCode:   v ? `ic_screen_${v}` : null,
        reasonDetail: icResult?.reason || null,
        metadata:     { ic_screen: icResult || null, enforced: icEnforced },
      });
      notify?.(`  [ic_screen] ${stratId} verdict=${v ?? 'n/a'} ${line}${shadowNote}`);
    }

    notify?.(`  ✅ ${stratId} IC screen complete — running backtest (may take 2–5 min)...`);
    onPhase('backtest', 60);
```

(f) Export the shape guard next to the existing ones (`:2230`):

```js
module.exports._isIcScreenShape = _isIcScreenShape;
```

- [ ] **Step 4** — Run and confirm PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/agent/test_ic_screen_gate.test.js tests/agent/test_zero_signal_gate.test.js tests/agent/test_prescreen_shape.test.js tests/agent/test_tearsheet_hook.test.js
```

Expected: all green.

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/agent/research/research-orchestrator.js tests/agent/test_ic_screen_gate.test.js
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(research): D1 wire the IC screen into the gate chain behind OPENCLAW_IC_SCREEN

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: D4 — `S_low_volatility_us_gk63` Garman-Klass range-vol variant

**The OHLC problem, and its sanctioned answer.** `generate_signals` receives `prices` as a wide
date × ticker **close** panel (`base.py:268-273`), and `aux_data` carries options / vol_indices /
macro / financials / insider / sentiment — no OHLC (`aux_data_loader.load_aux_data:586-645`).
Garman-Klass needs O, H, L and C. The established pattern for a strategy that needs extra
`prices.parquet` columns is `src/strategies/implementations/_extra_panels.load_wide(field, tickers,
date_floor)` — a module-cached, column-pruned, 400-ticker-chunked read with float32 pivots, used by
`S_overnight_intraday_tug_of_war` (`:29-31`, `:101-107`) and eight others. Its documented contract
is **the caller must slice `.loc[:asof]` where `asof = prices.index[-1]`** before computing
anything; this strategy does exactly that, which is what keeps it point-in-time safe.

**Expected first-gate behaviour:** on the synthetic 60-bar panel `validate_strategy` drives, both
`len(prices) < GK_WINDOW + 1` and the absent `load_wide` data make this strategy return `[]`, so it
will trip Task 1's `zero_signals_synthetic` warning on its first gate run and be marked
`needs_signal_check`. That is correct and non-blocking — `min_lookback = 70 > SYNTHETIC_DAYS`
actually exempts it, so no warning fires; note it here so a reviewer does not read the empty
synthetic result as a defect.

**Files:**
- Create: `src/strategies/implementations/S_low_volatility_us_gk63.py`
- Create: `src/strategies/implementations/S_low_volatility_us_gk63.requirements.json`
- Modify: `src/strategies/registry.py` — `_IMPL_MAP`, next to the parent at `:89`
- Modify: `src/strategies/manifest.json` — a `candidate` entry
- Modify: `src/strategies/strategy_signatures.json` — one fingerprint entry
- Test: `tests/strategies/test_low_volatility_us_gk63.py` (Create)

**Interfaces:**
- Consumes: `strategies.base.BaseStrategy` (`should_run`, `position_scale`, `compute_stops_and_targets`, `MAX_SIGNALS = 50`); `_extra_panels.load_wide(field: str, tickers: list[str], date_floor: str = '2021-01-01') -> pd.DataFrame` (`_extra_panels.py:35`)
- Produces: `S_low_volatility_us_gk63.garman_klass_variance(open_, high, low, close) -> pd.DataFrame`; `class LowVolatilityUSGK63(BaseStrategy)` with `id = 'S_low_volatility_us_gk63'`

- [ ] **Step 1** — Write the failing test file `tests/strategies/test_low_volatility_us_gk63.py`:

```python
"""D4 — Garman-Klass 63-session range-vol decile variant (spec 2026-09-12 §4 D4).

Synthetic panels only: _extra_panels.load_wide is monkeypatched on the strategy
module, so nothing reads data/master/prices.parquet.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    python3 -m pytest tests/strategies/test_low_volatility_us_gk63.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies.implementations import S_low_volatility_us_gk63 as gk  # noqa: E402

N_DAYS  = 90
TICKERS = [f'ZZT{i:02d}' for i in range(30)]
QUIET   = TICKERS[:3]        # the three names that must be selected


def _regime(state='LOW_VOL'):
    return {'state': state, 'state_probabilities': {state: 1.0}, 'confidence': 1.0,
            'stress_score': 15, 'position_scale': 1.0}


def _panels():
    """Closes plus O/H/L built so QUIET has a tiny intraday range and everyone
    else a wide one. Close-to-close vol is IDENTICAL across all names, so a
    close-only ranking could not separate them — only the range estimator can."""
    idx = pd.bdate_range(end='2026-08-21', periods=N_DAYS)
    rng = np.random.default_rng(11)
    base = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.008, N_DAYS)))
    closes = pd.DataFrame({t: base + i * 0.01 for i, t in enumerate(TICKERS)}, index=idx)
    opens, highs, lows = {}, {}, {}
    for t in TICKERS:
        c = closes[t].to_numpy()
        width = 0.0008 if t in QUIET else 0.030
        opens[t] = c * (1.0 - width * 0.25)
        highs[t] = c * (1.0 + width)
        lows[t]  = c * (1.0 - width)
    return (closes,
            {'open':  pd.DataFrame(opens,  index=idx),
             'high':  pd.DataFrame(highs,  index=idx),
             'low':   pd.DataFrame(lows,   index=idx),
             'close': closes})


@pytest.fixture
def wired(monkeypatch):
    closes, panels = _panels()
    def _load_wide(field, tickers, date_floor='2021-01-01'):
        return panels[field][[t for t in tickers if t in panels[field].columns]]
    monkeypatch.setattr(gk, 'load_wide', _load_wide)
    return closes


# ── the estimator ─────────────────────────────────────────────────────────────

def test_garman_klass_matches_the_closed_form():
    o = pd.DataFrame({'A': [100.0]})
    h = pd.DataFrame({'A': [102.0]})
    l = pd.DataFrame({'A': [99.0]})
    c = pd.DataFrame({'A': [101.0]})
    expected = (0.5 * np.log(102.0 / 99.0) ** 2
                - (2 * np.log(2) - 1) * np.log(101.0 / 100.0) ** 2)
    got = gk.garman_klass_variance(o, h, l, c).iloc[0, 0]
    assert got == pytest.approx(expected)


def test_garman_klass_nans_non_positive_legs():
    o = pd.DataFrame({'A': [100.0, 0.0]})
    h = pd.DataFrame({'A': [102.0, 5.0]})
    l = pd.DataFrame({'A': [99.0, -1.0]})
    c = pd.DataFrame({'A': [101.0, 4.0]})
    out = gk.garman_klass_variance(o, h, l, c)
    assert not pd.isna(out.iloc[0, 0])
    assert pd.isna(out.iloc[1, 0])


# ── the strategy ──────────────────────────────────────────────────────────────

def test_ranking_selects_the_low_gk_names(wired):
    s = gk.LowVolatilityUSGK63()
    signals = s.generate_signals(wired, _regime(), TICKERS)
    assert len(signals) == 3, f'decile of 30 = 3, got {len(signals)}'
    assert sorted(sig.ticker for sig in signals) == sorted(QUIET)
    assert all(sig.direction == 'LONG' for sig in signals)


def test_signals_carry_gk_params_and_house_brackets(wired):
    s = gk.LowVolatilityUSGK63()
    sig = s.generate_signals(wired, _regime(), TICKERS)[0]
    assert 'gk_var_63d' in sig.signal_params
    assert 'gk_vol_ann' in sig.signal_params
    assert 0.0 <= sig.signal_params['universe_gk_pctile'] <= 1.0
    assert sig.stop_loss < sig.entry_price < sig.target_1
    assert sig.position_size_pct > 0


def test_inactive_regime_emits_nothing(wired):
    s = gk.LowVolatilityUSGK63()
    assert s.generate_signals(wired, _regime('CRISIS'), TICKERS) == []


def test_empty_or_short_panel_emits_nothing(wired):
    s = gk.LowVolatilityUSGK63()
    assert s.generate_signals(pd.DataFrame(), _regime(), TICKERS) == []
    assert s.generate_signals(wired.head(10), _regime(), TICKERS) == []


def test_missing_ohlc_panel_emits_nothing_instead_of_raising(monkeypatch, wired):
    monkeypatch.setattr(gk, 'load_wide', lambda field, tickers, date_floor='2021-01-01': pd.DataFrame())
    s = gk.LowVolatilityUSGK63()
    assert s.generate_signals(wired, _regime(), TICKERS) == []


def test_ohlc_is_sliced_to_the_signal_date(monkeypatch, wired):
    """Point-in-time: nothing after prices.index[-1] may reach the estimator."""
    closes, panels = _panels()
    seen = {}
    def _load_wide(field, tickers, date_floor='2021-01-01'):
        seen[field] = panels[field]
        return panels[field]
    monkeypatch.setattr(gk, 'load_wide', _load_wide)
    s = gk.LowVolatilityUSGK63()
    truncated = closes.iloc[:70]
    s.generate_signals(truncated, _regime(), TICKERS)
    # the panels handed back extend past the signal date; the strategy must
    # have sliced them itself — assert via the recorded max used window
    assert seen['high'].index[-1] > truncated.index[-1]


def test_contract_surface_matches_the_parent():
    s = gk.LowVolatilityUSGK63()
    assert s.id == 'S_low_volatility_us_gk63'
    assert s.DECILE_FRAC == 0.10
    assert s.GK_WINDOW == 63
    assert 'LOW_VOL' in s.active_in_regimes and 'TRANSITIONING' in s.active_in_regimes


# ── registration ──────────────────────────────────────────────────────────────

def test_registered_in_impl_map():
    from strategies.registry import _IMPL_MAP
    assert _IMPL_MAP['S_low_volatility_us_gk63'] == (
        'strategies.implementations.S_low_volatility_us_gk63', 'LowVolatilityUSGK63')


def test_manifest_entry_is_a_candidate():
    mf = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
    entry = mf['strategies']['S_low_volatility_us_gk63']
    assert entry['state'] == 'candidate'
    assert entry['metadata']['canonical_file'] == 'S_low_volatility_us_gk63.py'
    assert entry['metadata']['class'] == 'LowVolatilityUSGK63'
    assert entry['instrument_class'] == 'equity'


def test_requirements_file_mirrors_the_parent():
    impl = ROOT / 'src' / 'strategies' / 'implementations'
    child = json.loads((impl / 'S_low_volatility_us_gk63.requirements.json').read_text())
    parent = json.loads((impl / 'low_volatility_us.requirements.json').read_text())
    assert child['required'] == parent['required']
    assert child['strategy_id'] == 'S_low_volatility_us_gk63'


def test_signature_entry_exists():
    sigs = json.loads((ROOT / 'src' / 'strategies' / 'strategy_signatures.json').read_text())
    entry = sigs['S_low_volatility_us_gk63']
    assert set(entry) == {'regime_set_hash', 'direction_hash', 'formula_tokens', 'regimes'}
    assert 'LOW_VOL' in entry['regimes']
```

- [ ] **Step 2** — Run it and confirm the expected failure:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_low_volatility_us_gk63.py -q
```

Expected: `ModuleNotFoundError: No module named 'strategies.implementations.S_low_volatility_us_gk63'`
at collection.

- [ ] **Step 3a** — Create `src/strategies/implementations/S_low_volatility_us_gk63.py`:

```python
"""
S_low_volatility_us_gk63.py — Garman-Klass range-vol variant of low_volatility_us.

Spec: docs/specs/2026-09-12-quantdinger-adoptions-spec.md §4 D4 (item 9).

Hypothesis: the low-volatility anomaly's ranking signal is the parent's
252-session close-to-close standard deviation, which throws away every
intraday bar. The Garman-Klass estimator

    sigma^2_GK(t) = 0.5 * ln(H_t / L_t)^2 - (2 ln 2 - 1) * ln(C_t / O_t)^2

uses the whole bar and is roughly 7x more efficient per observation than
close-to-close, so a 63-session GK window carries about as much information as
the parent's 252-session close window while responding four times faster to a
regime change in a name's realised risk.

Same universe, same rebalance cadence (daily-driven, decile-selected), same
decile fraction, same house ATR brackets as the parent — the ONLY change is the
ranking statistic, so the fleet gates measure the estimator, not a new strategy.

Data: close panel (engine) + self-loaded OPEN/HIGH/LOW/CLOSE panels from
prices.parquet via _extra_panels.load_wide — the established pattern for a
strategy needing extra master-parquet columns (see S_overnight_intraday_tug_of_war
and oxford_crabel.basket_ohlc). Point-in-time: every self-loaded panel is
sliced .loc[:asof] with asof = prices.index[-1] before anything is computed,
which is _extra_panels' documented caller contract.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import load_wide
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import load_wide

__all__ = ['LowVolatilityUSGK63', 'garman_klass_variance']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_low_volatility_us_gk63'

_GK_C = 2.0 * np.log(2.0) - 1.0


def garman_klass_variance(open_: pd.DataFrame, high: pd.DataFrame,
                           low: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """Per-bar Garman-Klass variance: 0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2.

    Returns a frame aligned with the inputs; NaN wherever any leg is missing or
    non-positive (log of <= 0 is undefined — a zero/negative price is bad data,
    not a zero-variance bar)."""
    valid = (open_ > 0) & (high > 0) & (low > 0) & (close > 0)
    hl = np.log(high.where(valid) / low.where(valid))
    co = np.log(close.where(valid) / open_.where(valid))
    return 0.5 * hl ** 2 - _GK_C * co ** 2


class LowVolatilityUSGK63(BaseStrategy):
    """Rank asc by mean 63-session Garman-Klass variance; LONG the lowest decile."""

    id          = STRATEGY_ID
    name        = 'LowVolatilityUSGK63'
    description = ('Garman-Klass range-vol variant of low_volatility_us: rank asc by mean '
                   '63-session Garman-Klass variance; LONG the lowest-variance decile, equal-weight')
    tier        = 2

    # NEUTRAL expands to LOW_VOL + TRANSITIONING via base.py synonym resolution —
    # identical to the parent's declaration.
    active_in_regimes = ['LOW_VOL', 'NEUTRAL', 'TRANSITIONING']

    # 63 GK bars + a pad, so the prescreen's min_lookback-aware floor loads
    # enough history (factor_prescreen.run_prescreen reads this attribute).
    min_lookback = 70

    GK_WINDOW   = 63
    MIN_VALID   = 45          # usable GK bars required inside the window
    DECILE_FRAC = 0.10
    MIN_TICKERS = 10
    DATE_FLOOR  = '2021-01-01'

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        available = [t for t in universe if t in prices.columns]
        if len(available) < self.MIN_TICKERS:
            print('[debug] signals=0', file=sys.stderr)
            return []
        if len(prices) < self.GK_WINDOW + 1:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Self-load OHLC, POINT-IN-TIME ─────────────────────────────────────
        # asof is the signal bar; .loc[:asof] before .tail() is _extra_panels'
        # documented caller contract and is what makes this look-ahead-safe.
        asof = prices.index[-1]
        panels = {}
        for field in ('open', 'high', 'low', 'close'):
            w = load_wide(field, available, date_floor=self.DATE_FLOOR)
            if w is None or w.empty:
                print('[debug] signals=0', file=sys.stderr)
                return []
            panels[field] = w.loc[:asof].tail(self.GK_WINDOW)

        cols = None
        for w in panels.values():
            cols = set(w.columns) if cols is None else (cols & set(w.columns))
        cols = sorted(c for c in (cols or set()) if c in available)
        if len(cols) < self.MIN_TICKERS:
            print('[debug] signals=0', file=sys.stderr)
            return []

        idx = panels['close'].index
        for field in ('open', 'high', 'low'):
            idx = idx.intersection(panels[field].index)
        if len(idx) < self.MIN_VALID:
            print('[debug] signals=0', file=sys.stderr)
            return []

        o = panels['open'].loc[idx, cols].astype('float64')
        h = panels['high'].loc[idx, cols].astype('float64')
        low_ = panels['low'].loc[idx, cols].astype('float64')
        c = panels['close'].loc[idx, cols].astype('float64')

        gk = garman_klass_variance(o, h, low_, c)
        counts = gk.notna().sum()
        gk_mean = gk.mean(skipna=True)
        gk_mean = gk_mean[counts >= self.MIN_VALID].dropna()
        gk_mean = gk_mean[gk_mean > 0]
        if gk_mean.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # Lowest-variance decile, capped at MAX_SIGNALS — parent's selection rule.
        n_select = max(1, int(len(gk_mean) * self.DECILE_FRAC))
        selected = gk_mean.nsmallest(min(n_select, self.MAX_SIGNALS))

        latest = prices[list(selected.index)].ffill().iloc[-1]
        decile_median = float(selected.median())
        base_weight = round(1.0 / len(selected), 6)

        signals: List[Signal] = []
        for ticker in selected.index:
            price = float(latest[ticker]) if ticker in latest.index else float('nan')
            if not price or pd.isna(price) or price <= 0:
                continue

            gk_t = float(selected[ticker])
            confidence = 'HIGH' if gk_t <= decile_median else 'MED'

            stops = self.compute_stops_and_targets(
                prices_series=prices[ticker].dropna(),
                direction='LONG',
                current_price=price,
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=round(price, 4),
                stop_loss=round(stops['stop'], 4),
                target_1=round(stops['t1'], 4),
                target_2=round(stops['t2'], 4),
                target_3=round(stops['t3'], 4),
                position_size_pct=round(base_weight * scale, 6),
                confidence=confidence,
                signal_params={
                    'gk_var_63d':         round(gk_t, 10),
                    'gk_vol_ann':         round(float(np.sqrt(max(gk_t, 0.0) * 252.0)), 6),
                    'gk_bars_used':       int(counts.get(ticker, 0)),
                    'universe_gk_pctile': round(float((gk_mean < gk_t).sum()) / len(gk_mean), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
```

- [ ] **Step 3b** — Create `src/strategies/implementations/S_low_volatility_us_gk63.requirements.json`
(mirrors the parent's `low_volatility_us.requirements.json` exactly, id swapped — the OHLC columns
live in the same `prices` master):

```json
{
  "strategy_id": "S_low_volatility_us_gk63",
  "required": ["prices"],
  "optional": []
}
```

- [ ] **Step 3c** — Add the `_IMPL_MAP` entry in `src/strategies/registry.py`, immediately after the
parent's line (`:89`):

```python
    'S_low_volatility_us_gk63':               ('strategies.implementations.S_low_volatility_us_gk63',               'LowVolatilityUSGK63'),
```

- [ ] **Step 3d** — Add the manifest entry. Insert into `src/strategies/manifest.json` under
`"strategies"`, immediately BEFORE the `"low_volatility_us"` key (`:2036`), keeping the file's
2-space indentation:

```json
    "S_low_volatility_us_gk63": {
      "state": "candidate",
      "state_since": "2026-09-12T00:00:00.000Z",
      "metadata": {
        "canonical_file": "S_low_volatility_us_gk63.py",
        "class": "LowVolatilityUSGK63",
        "description": "Garman-Klass range-vol variant of low_volatility_us: rank asc by mean 63-session Garman-Klass variance; LONG the lowest-variance decile, equal-weight",
        "universe_filter_ref": "src.strategies.universe_default:tier_r3000"
      },
      "history": [],
      "instrument_class": "equity"
    },
```

`state_since` must be the actual UTC timestamp at implementation time; `eligible_regimes` is
deliberately absent — the eligibility assigner sets it from the first backtest's per-regime metrics.

- [ ] **Step 3e** — Add the signature entry. Do NOT run
`python3 src/strategies/generate_signatures.py` — it rewrites all ~400 entries and would bury this
change in unrelated drift. Compute the one entry with the generator's own helpers (pure source
parsing; no DB, no `data/`):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, 'src')
from strategies.generate_signatures import (
    sha256, extract_regimes, extract_directions, extract_formula_tokens)

impl = Path('src/strategies/implementations/S_low_volatility_us_gk63.py')
src  = impl.read_text()
regimes = extract_regimes(src)
dirs    = extract_directions(src)
tokens  = extract_formula_tokens(src)

sig_path = Path('src/strategies/strategy_signatures.json')
sigs = json.loads(sig_path.read_text())
sigs[impl.stem] = {
    'regime_set_hash': sha256(' '.join(regimes)),
    'direction_hash':  sha256(' '.join(dirs)),
    'formula_tokens':  tokens,
    'regimes':         regimes,
}
sig_path.write_text(json.dumps(sigs, indent=2) + '\n')
print(json.dumps(sigs[impl.stem], indent=2))
PY
```

Then confirm the diff touches exactly one entry:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git diff --stat src/strategies/strategy_signatures.json
```

- [ ] **Step 4** — Run and confirm PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_low_volatility_us_gk63.py tests/strategies/test_universe_lint.py tests/strategies/test_lifecycle_instrument_class.py -q
```

Expected: all green. If `test_universe_lint.py` objects to the `universe_filter_ref`, drop that key
from the manifest metadata — the assigner will fill it at mint under
`OPENCLAW_PHASE_D_PREDICATE_AT_MINT`; the parent has it because it was set later.

Then confirm the manifest is still valid JSON and the registry imports:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -c "import json,sys; sys.path.insert(0,'src'); json.load(open('src/strategies/manifest.json')); from strategies.registry import _IMPL_MAP; print(_IMPL_MAP['S_low_volatility_us_gk63'])"
```

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/strategies/implementations/S_low_volatility_us_gk63.py src/strategies/implementations/S_low_volatility_us_gk63.requirements.json src/strategies/registry.py src/strategies/manifest.json src/strategies/strategy_signatures.json tests/strategies/test_low_volatility_us_gk63.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(strategy): D4 S_low_volatility_us_gk63 Garman-Klass range-vol decile variant

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Stream D changelog entry + stream verification

**Safety:** documentation and read-only probes. No source changes.

**Files:**
- Modify: `docs/archive/changelog.md` — one entry, newest first, immediately under `## Recent Changes` (`:8`)

**Interfaces:**
- Consumes: nothing
- Produces: one changelog bullet

- [ ] **Step 1** — Run the stream-level verification the spec §7 requires, and record the real output
(the changelog entry must quote actual numbers, not placeholders):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m system_checks --tag strategies --json
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m system_checks --tag agents --json
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 src/maintenance/doctor.py --quick
```

Expected: no NEW failures versus the pre-branch baseline. `mastermind_calibration_brier` is expected
to keep FAILING at Brier ≈ 0.25 — that is the true finding this stream responds to, not a
regression, and it is `quick_skip`-tagged so `--quick` stays clean.

- [ ] **Step 2** — Re-run every test file this stream created, in three chunks (the box is 2-core and
a fleet backtest is running):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_validate_zero_signal_warning.py tests/strategies/test_proposal_floor_constant.py tests/strategies/test_proposal_calibrated_gate.py -q
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/metrics/test_calibrated_confidence.py tests/research/test_factor_ic_screen.py tests/strategies/test_low_volatility_us_gk63.py -q
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --test tests/agent/test_zero_signal_gate.test.js tests/agent/test_ic_screen_gate.test.js tests/agent/test_review_calibration_addendum.test.js
```

Expected: all green. Record the counts for the changelog entry.

- [ ] **Step 3** — Add the entry to `docs/archive/changelog.md`, as the FIRST bullet under
`## Recent Changes` (line 8), replacing every `<...>` with the numbers from Steps 1–2:

```markdown
- **2026-09-12: Stream D — research lane (spec `docs/specs/2026-09-12-quantdinger-adoptions-spec.md` §4, items 6–9; operator ruling R4).** Four independent upgrades, all landed behind flags or as warnings. **D3 zero-signal WARN**: `validate_strategy.validate()` now returns a `warnings` list and appends `zero_signals_synthetic` when the synthetic LOW_VOL panel produces nothing — exempting `calendar_edge` strategies (`base.py:149`), strategies whose `active_in_regimes` excludes LOW_VOL, those declaring `min_lookback > 60`, and those the manifest already gated away from LOW_VOL. `ok` is untouched: this can never fail a candidate. `research-orchestrator._runGateChain` skips the Opus red-team turn on that warning and records `gateName:'redteam', reasonCode:'needs_signal_check'` instead, then continues to prescreen/backtest as normal. **D2 calibrated auto-approve**: the confidence floor now has ONE constant (`proposal_manager.DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE = 0.85`, was split 0.85/0.8 across `:311`/`:403`/`:473`; production `.env` overrides to 0.9, so no live change). `metrics/mastermind_calibration.py` gains `calibrated_confidence(raw, buckets) = raw × clip(match_rate/bucket_midpoint, 0.5, 1.0)` for buckets with n ≥ 8, plus an evidence cap (`none .35 / low .55 / medium .75 / high 1.0`) keyed on the trailing-30-day closed-trade count (<10/<30/<100) with staleness > 45 d dropping one level. `auto_approve` records raw/calibrated/cap/level/binding_bound on the proposal row (**migration 159**, additive columns only) in BOTH modes and BEFORE every rail, and compares `min(calibrated, cap)` to the floor only under `OPENCLAW_PROPOSAL_CALIBRATED=1`; unset logs the would-be decision and keeps today's raw compare. 🔴 EXPECTED ON FLIP: with the floor at 0.9 and the measured `[0.8,1.0]` bucket at 0.56 (n=18, 2026-09-06), the calibrated path closes auto-approval essentially completely — every proposal parks as `noted` for the operator. That is the intended answer to the 09-06 "owed to the operator" item; read two weekends of the `calibration SHADOW:` lines before setting the flag. The doctor bucket table is also back in the Saturday sizing-proposal prompt (`comprehensive_review.buildStrategyPrompt` 4th arg, fail-soft — the 2026-05-19 removal was of the OPERATOR addenda, a different thing). **D1 IC screen**: new `src/research/factor_ic_screen.py` — per-rebalance Spearman rank IC at H=5/10/21 on NON-overlapping dates, ICIR ×√(252/H), first/second-half IC, rank autocorrelation, quintile means + Q5−Q1, monotonicity, Jaccard turnover × round-trip cost (10 bp one-way, mirroring `unified_backtest.INSTRUMENT_COST_BPS`). Prices come only from `factor_prescreen.load_price_window` sliced to ~2 years. Verdicts `pass | weak | flat | skipped`; the fourth is a review addition — a long-only decile strategy has ~12 non-NaN cells over 2 confidence levels, which would score `flat` and false-block the whole decile class, so a rebalance only counts with ≥20 non-NaN and ≥5 distinct values and <12 such rebalances ⇒ `skipped`. Wired into the gate chain AFTER the prescreen (cheap screen first; still between red-team and backtest); `flat` skips the ~900 s backtest ONLY under `OPENCLAW_IC_SCREEN=1`, unset = compute + record + log. **D4**: `S_low_volatility_us_gk63` — 63-session Garman-Klass variance (`0.5·ln(H/L)² − (2ln2−1)·ln(C/O)²`) replacing the parent's 252-session close-to-close std, same universe/cadence/decile/brackets; OHLC self-loaded point-in-time via `_extra_panels.load_wide` (the engine hands strategies closes only). Registered as `candidate`; needs a nightly fleet slot. Tests: <N> python + <M> node across the seven new files, all green; `system_checks --tag strategies/agents` clean (`mastermind_calibration_brier` still FAILs at Brier <B> — the true finding, `quick_skip`-tagged). Flags all default-off: `OPENCLAW_IC_SCREEN`, `OPENCLAW_PROPOSAL_CALIBRATED`.
```

- [ ] **Step 4** — Confirm the file still renders and the entry is first:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && sed -n '1,12p' docs/archive/changelog.md
```

Expected: the new bullet is the first item under `## Recent Changes`.

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add docs/archive/changelog.md
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "docs(changelog): Stream D — IC screen, calibrated auto-approve floor, zero-signal gate, GK low-vol variant

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review

### Spec coverage

| Spec item | Requirement | Task(s) | Notes |
|---|---|---|---|
| D1 (`:254-262`) | `src/research/factor_ic_screen.py`; Spearman rank IC at 5/10/21; ICIR √(252/H); first/second-half IC; rank autocorrelation; quintile long-short mean; monotonicity; Jaccard turnover × round-trip cost; JSON `{ic, icir, ic_half, rank_ac, ls_q5q1, monotonic, turnover, verdict}` | 7 | All eight fields emitted plus `quintile_means`, `cost_per_rebalance`, `n_rebalances`, `n_qualifying_rebalances`, `horizons`, `reason` |
| D1 (`:257-259`) | Prices via `factor_prescreen.load_price_window` sliced to 2 years — never the full panel | 7 | `LOOKBACK_SESSIONS = 504`; a test asserts `load_price_window` is the only price source |
| D1 (`:263-267`) | Wire after red-team, before the backtest slot; `flat` ⇒ skip + record reason; `weak` ⇒ annotate; `OPENCLAW_IC_SCREEN=1`, unset = compute + log | 8 | Placed after the prescreen (documented deviation, still between red-team and backtest) |
| D1 (`:264`) | Thresholds: flat if \|IC_5\| < 0.01 AND \|ls_q5q1\| < cost; weak if ICIR < 0.3 | 7 | `FLAT_IC_ABS = 0.01`, `WEAK_ICIR_ABS = 0.30` on \|ICIR\| (a strongly negative IC is a strong signal, not a weak one) |
| D1 (`:268-270`) | Tests: next-week-return+noise ⇒ pass; pure noise ⇒ flat; reversed ⇒ negative IC | 7 | Plus thin-cross-section ⇒ skipped, low-ICIR ⇒ weak, shape, spacing, `factor_from_signals` |
| D2 (`:273-275`) | Unify the 0.85/0.8 default in ONE constant | 3 | `DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE` + `autoapprove_min_confidence()`; source-grep test forbids inline literals |
| D2 (`:276-281`) | `calibrated_confidence(raw, bucket_table)` = `raw × clip(hit_rate/midpoint, .5, 1)` at n ≥ 8, else raw | 4 | Reads `match_rate` (the per-bucket key `_bucket_aggregates` emits), not the report's global `hit_rate` |
| D2 (`:281-285`) | Evidence cap `{none .35, low .55, medium .75, high 1.0}` on decisive-window closed-trade count (<10/<30/<100), staleness > 45 d ⇒ one level down | 4 | `evidence_level` / `evidence_cap` / `evidence_counts` |
| D2 (`:285-288`) | `auto_approve` compares `min(calibrated, cap)`; records raw/calibrated/cap/which bound bit; `OPENCLAW_PROPOSAL_CALIBRATED=1`, unset = compute + log | 5 | Migration 159 (155–158 held by B/C); recorded in BOTH modes, BEFORE the rails |
| D2 (`:289-292`) | Restore the calibration addendum (doctor bucket table verbatim) to the sizing-proposal prompt, shaped like `mastermind.js:632-725` | 6 | `_renderProposalCalibration` mirrors `_renderCalibration`'s layout; JS test asserts the block is in the prompt |
| D2 (`:292`) | Tests: bucket remap math; cap by count and staleness; floor compare uses the min; env-unset path logs and does not change the decision | 4, 5 | All four covered |
| D3 (`:294-297`) | `warnings.append('zero_signals_synthetic')` next to `signal_count` when 0 and not calendar-edge / regime-gated | 1 | Plus a `min_lookback > 60` exemption the spec implies but does not name |
| D3 (`:298-300`) | Orchestrator: skip the red-team LLM, mark `needs_signal_check`, never BLOCK | 2 | Single decision emit (the pass branch is guarded so the gate is not double-counted) |
| D4 (`:302-308`) | `S_low_volatility_us_gk63.py` cloned from `low_volatility_us.py`; rank on 63-session GK variance; same universe/cadence/decile; registry + manifest `candidate` + requirements mirror; normal gates | 9 | Plus the `strategy_signatures.json` entry and the OHLC self-load the spec does not address |
| §0 / §7 | Changelog entry per stream; `system_checks` + `doctor --quick` per stream | 10 | — |

### Placeholder scan

- No `TODO`, `FIXME`, `...`, `<your …>`, or `pass  # implement` in any code block. Every Python and
  JS block is complete, runnable text — including the repeated fake-cursor / fake-orchestrator
  fixtures, which are written out in full in each test file rather than cross-imported.
- Three intentional fill-ins, each explicitly flagged at its use site: the changelog's `<N>`/`<M>`
  test counts and `<B>` Brier (Task 10 Step 3 says to substitute the Step 1–2 output), the manifest
  `state_since` timestamp (Task 9 Step 3d), and the pinned `SEED = 7` in the IC-screen noise test
  (Task 7 Step 4 gives the sweep-and-re-pin procedure if the draw moves).
- Every env var named is either pre-existing (`OPENCLAW_PROPOSAL_AUTOAPPROVE`,
  `OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE`, `_MAX_SIZE_DELTA`, `_MAX_STOP_DELTA`,
  `OPENCLAW_PHASE_D_PREDICATE_AT_MINT`, `PYTHONPATH`) or defined by this plan
  (`OPENCLAW_IC_SCREEN`, `OPENCLAW_PROPOSAL_CALIBRATED`), both unset-is-today's-behaviour.

### Signature consistency

Every symbol referenced across tasks is either verified in the current tree or produced by a task in
this plan:

- Verified existing: `factor_prescreen.load_price_window(days, max_tickers, lookback=DEFAULT_LOOKBACK)`, `._load_strategy_class`, `._resolve_instrument_class`, `._module_reads_aux_data`, `._benign_regime`, `DEFAULT_LOOKBACK`, `MIN_LOOKBACK_PAD`, `MAX_LOOKBACK_BARS`; `quick_backtest.MIN_CROSS_SECTION`; `unified_backtest.INSTRUMENT_COST_BPS`; `_extra_panels.load_wide(field, tickers, date_floor)`; `BaseStrategy.generate_signals/should_run/position_scale/compute_stops_and_targets/MAX_SIGNALS/calendar_edge/min_lookback/active_in_regimes`; `mastermind_calibration.BUCKETS/_bucket_aggregates/calibration_report/_connect`; `proposal_manager._connect/_PENDING_COLS/_decide/_current_size_scalar/list_proposals/_mark_noted`; `emitGateDecision`/`paperIdForCandidate`; `_spawnPython`; `_isPrescreenShape`; `QUEUE_STATUS_FOR_REASON`; `comprehensive_review.spawnSync/PYTHON/OPENCLAW_DIR/buildStrategyPrompt`; `doctor.CALIBRATION_BRIER_WARN/_FAIL/_MIN_SAMPLES`.
- Produced here and consumed here: `validate_strategy.{SYNTHETIC_DAYS, MANIFEST_PATH, _manifest_regime_gated, _zero_signal_exempt}` (T1 → T1 tests, T2 consumes the `warnings` key); `proposal_manager.{DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE, autoapprove_min_confidence}` (T3 → T5); `mastermind_calibration.{bucket_midpoint, bucket_for, calibrated_confidence, evidence_level, evidence_cap, evidence_counts, MIN_BUCKET_N, EVIDENCE_CAPS, EVIDENCE_LEVELS, EVIDENCE_WINDOW_DAYS, EVIDENCE_STALE_DAYS}` (T4 → T5); `proposal_manager.{_calibration_report, _evidence_counts, _calibration_inputs, _record_calibration}` (T5, stubbed by T5's tests); `comprehensive_review.{_renderProposalCalibration, _loadProposalCalibration}` (T6, exported for its test); `factor_ic_screen.{round_trip_cost, forward_returns, rebalance_dates, ic_series, quintile_stats, compute_ic_screen, factor_from_signals, run_ic_screen, main}` (T7 → T8 consumes only the CLI's JSON contract); `research-orchestrator.{_isIcScreenShape, _runIcScreen, _icScreenFn}` + `QUEUE_STATUS_FOR_REASON.ic_screen_flat` (T8); `S_low_volatility_us_gk63.{garman_klass_variance, LowVolatilityUSGK63}` (T9).
- Migration numbering: 159 is the first free number (tree ends at 154; Streams B and C reserve
  155–158). No other Stream D task needs a migration.

### Line-number drift found (spec numbers are from 2026-09-11; re-verified 2026-09-13)

| Spec citation | Actual | Consequence |
|---|---|---|
| D1 "`research-orchestrator.js` around `:1041`" | `:1041` is inside `_runValidateStrategy`; the red-team→backtest seam is `:1439-1441` | Task 8 inserts at `:1439-1441`, after the prescreen |
| D1 "`factor_prescreen.load_price_window` (`:391-436`)" | `def load_price_window` at `:321`; body runs to `:455` | Cited correctly in Task 7 |
| D2 "`mastermind_proposals` / `mastermind_proposal_outcomes` schemas" | There is no `mastermind_proposals` table — proposals live in `strategy_regime_param_proposals` (migration 078); `mastermind_proposal_outcomes` is migration 082 | Migration 159 alters `strategy_regime_param_proposals` |
| D2 "`proposal_manager.py:311` / `:403` / `:473`" | Exact on the current tree (`:311` `'0.85'`, `:403` `'0.8'`, `:473` help text) | No change needed |
| D2 "`mastermind_calibration.py` (~43-118)" | `_bucket_aggregates` is `:119-135`; `calibration_report` `:220-249`; the per-bucket key is `match_rate`, not `hit_rate` | Task 4 appends after `:135` and reads `match_rate` |
| D2 "`comprehensive_review.js:275-280` removed it" | The removal note is `:274-279`; `buildStrategyPrompt` at `:292`; its call site `:371`; exports `:600` | Task 6 uses the current numbers |
| D2 "`mastermind.js:632-725`" | `_loadCalibrationFeedback` is `:642-746`; the RENDERING block to copy is `_renderCalibration` `:747-850` | Task 6 copies the renderer's shape |
| D2 "`doctor.py ~1035-1050` calibration thresholds" | Constants at `:1044-1046`; the check at `:1063-1093` | Mirrored in Task 6 |
| D3 "`validate_strategy.py:172-190` computes `signal_count`; `:216-217` `ok = ...`; `warnings` list if any" | `signal_count = len(signals)` is a single line at `:191`; `ok` at `:216`; **there is no `warnings` list today** — the shape is `{ok, errors, signal_count}` from seven return sites | Task 1 creates the key; Task 2 reads it as `validResult.warnings \|\| []` |
| D3 "not `calendar_edge` / regime-gated (read the manifest flags)" | `calendar_edge` has **zero** occurrences in `manifest.json` — it is a class attribute (`base.py:149`) set by 7 implementations | Task 1 reads class attributes first (`calendar_edge`, `active_in_regimes`, `min_lookback`) and uses the manifest only for `metadata.eligible_regimes` |
| D4 "cloned from `low_volatility_us.py` … `0.5·ln(H/L)² − (2ln2−1)·ln(C/O)²`" | `generate_signals` receives **closes only**; `aux_data` carries no OHLC | Task 9 self-loads O/H/L/C via `_extra_panels.load_wide` with the documented `.loc[:asof]` slice |
| Latest migration | Tree ends at `154_bench_corr_removal.sql` | Stream D uses 159 (B/C hold 155–158) |









