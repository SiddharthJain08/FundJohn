# SP-4 Phases A–D — Non-Equity Research Origination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach the Saturday research origination pipeline to originate `option`/`etp`/`crypto` strategies end-to-end by threading a new `inferred_instrument_class` field from PaperHunter → orchestrator → StrategyCoder → lifecycle, gated default-OFF.

**Architecture:** A single new field, `inferred_instrument_class`, parallels SP-2 Phase D's `universe_filter_ref`. PaperHunter infers it (top-level in its JSON); the live Saturday path carries it automatically via the existing `{ ...hunterResult }` spread; `_codeStrategy` validates it (new exported `_validateInferredClass`, gated `OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT`) and enforces the option-underlying envelope; StrategyCoder writes it into the manifest; `lifecycle` already enforces per-class promotion thresholds. Corpus recognition (mastermind), ingestion breadth (arXiv/OpenAlex), a crypto-column advisory taxonomy (servers.json), and review-context (comprehensive_review/position_recommender) round out the "maximal" scope. **No schema migration. Equity origination byte-identical when the gate is OFF.**

**Tech Stack:** Python 3 (lifecycle, backtest, ingestion), Node.js (orchestrator, curators), Markdown prompts. Tests: pytest (`pytest tests/<f>.py -v`) + Node's built-in runner (`node --test`) + the established `node -e`-from-pytest validator pattern.

---

## Conventions (read once, applies to every task)

- **Worktree:** `/root/openclaw/.claude/worktrees/sp4-phases-a-d` (branch `worktree-sp4-phases-a-d`). `data/master` is symlinked to `/root/openclaw/data/master`. Run everything from the worktree root.
- **Python test header** (use verbatim at the top of every new `tests/*.py`):
  ```python
  from __future__ import annotations
  import sys
  from pathlib import Path
  ROOT = Path(__file__).resolve().parents[1]
  sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
  ```
- **Run a python test:** `pytest tests/<file>.py -v`
- **Run a JS validator test (node -e from pytest):** modeled on `tests/test_orchestrator_predicate_injection.py`.
- **The gate:** `OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1` enables instrument-class-at-mint. Absent/≠`1` ⇒ OFF ⇒ everything resolves to `equity` ⇒ byte-identical to today.
- **Commit after each task.** Stage only the files the task touched (never `git add -A`). End commit messages with the Co-Authored-By trailer.
- **Never edit** `src/strategies/base.py`.

---

## Task 0: Verify clean baseline

**Files:** none (verification only).

- [ ] **Step 1: Confirm worktree + symlink**

Run: `ls -l data/master/prices.parquet && git log --oneline -1`
Expected: the parquet resolves (symlink), HEAD is the SP-4 A–D design-spec commit on `worktree-sp4-phases-a-d`.

- [ ] **Step 2: Run the instrument_class + orchestrator regression suites**

Run: `pytest tests/test_lifecycle_instrument_class.py tests/test_instrument_class_sizer.py -v && node --test tests/test_research_parsejson.test.js`
Expected: all green (these are the suites this plan extends/relies on).

- [ ] **Step 3: Confirm the option-envelope helper exists**

Run: `python3 -c "import sys; sys.path.insert(0,'src'); from backtest.vol_index import is_supported_option_underlying, VALID_OPTION_UNDERLYINGS; print(sorted(VALID_OPTION_UNDERLYINGS)); print(is_supported_option_underlying('SPY'), is_supported_option_underlying('AAPL'))"`
Expected: prints `['QQQ', 'SPY', 'SPX', 'IWM', '^GSPC']` (order may vary) then `True False`. If `is_supported_option_underlying` is missing, STOP — the Phase 0 merge is incomplete.

---

# Group 1 — The spine (Phase B core)

## Task 1: `_validateInferredClass` in the orchestrator (gated, whitelist-checked)

**Files:**
- Modify: `src/agent/research/research-orchestrator.js` (add function after `_validateInferredFilter` at line 70; add export after line 1288)
- Test: `tests/test_orchestrator_instrument_class_injection.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_orchestrator_instrument_class_injection.py`:
```python
"""SP-4: _validateInferredClass — gate ON/OFF, whitelist, fallback.
Run: pytest tests/test_orchestrator_instrument_class_injection.py -v
"""
import json
import os
import subprocess
from pathlib import Path

WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NODE_SNIPPET = (
    'const Orch = require("./src/agent/research/research-orchestrator");'
    ' const fn = Orch._validateInferredClass;'
    ' const arg = process.argv[1];'
    ' const val = arg === "__NULL__" ? null : arg;'
    ' console.log(JSON.stringify(fn(val)));'
)


def _call(arg, *, gate_on):
    env = os.environ.copy()
    env['OPENCLAW_DIR'] = WORKTREE
    if gate_on:
        env['OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT'] = '1'
    else:
        env.pop('OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT', None)
    proc = subprocess.run(
        ['node', '-e', NODE_SNIPPET, arg],
        capture_output=True, text=True, cwd=WORKTREE, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_gate_off_always_equity():
    assert _call('option', gate_on=False) == 'equity'
    assert _call('crypto', gate_on=False) == 'equity'


def test_null_is_equity():
    assert _call('__NULL__', gate_on=True) == 'equity'


def test_valid_classes_pass_when_gate_on():
    for cls in ('equity', 'option', 'etp', 'crypto', 'futures'):
        assert _call(cls, gate_on=True) == cls


def test_unknown_falls_back_to_equity():
    assert _call('banana', gate_on=True) == 'equity'
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_orchestrator_instrument_class_injection.py -v`
Expected: FAIL — `Orch._validateInferredClass` is `undefined` (TypeError: fn is not a function).

- [ ] **Step 3: Implement `_validateInferredClass`**

In `src/agent/research/research-orchestrator.js`, immediately after the closing `}` of `_validateInferredFilter` (line 70), add:
```javascript

/**
 * SP-4: Validate an inferred_instrument_class against VALID_INSTRUMENT_CLASSES.
 * Returns the class if valid AND the gate is ON; otherwise 'equity' (the
 * byte-identical default — gate OFF, null, or unknown all resolve to equity).
 * Gate: OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1
 */
function _validateInferredClass(name) {
  if (name == null) return 'equity';
  if (process.env.OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT !== '1') return 'equity';  // gate
  const r = spawnSync(PYTHON, ['-c',
    'from src.strategies.lifecycle import VALID_INSTRUMENT_CLASSES; '
    + 'import sys; sys.exit(0 if sys.argv[1] in VALID_INSTRUMENT_CLASSES else 1)',
    name], { encoding: 'utf8', cwd: OPENCLAW_DIR });
  if (r.status !== 0) {
    console.warn(`[research-orch] PaperHunter emitted invalid instrument_class '${name}', falling back to equity`);
    return 'equity';
  }
  return name;
}
```

At the bottom of the file, after line 1288 (`module.exports._validateInferredFilter = _validateInferredFilter;`), add:
```javascript
module.exports._validateInferredClass = _validateInferredClass;
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_orchestrator_instrument_class_injection.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agent/research/research-orchestrator.js tests/test_orchestrator_instrument_class_injection.py
git commit -m "feat(sp4): _validateInferredClass — gated instrument_class validator

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Option-underlying envelope helper + orchestrator hard-gate

**Files:**
- Modify: `src/agent/research/research-orchestrator.js` (add `_optionUnderlyingSupported` after `_validateInferredClass`; export it; enforce in `_codeStrategy` at line 1046)
- Test: `tests/test_orchestrator_option_envelope.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_orchestrator_option_envelope.py`:
```python
"""SP-4: _optionUnderlyingSupported — only Phase-0 index/ETF underlyings pass.
Run: pytest tests/test_orchestrator_option_envelope.py -v
"""
import json
import os
import subprocess

WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NODE_SNIPPET = (
    'const Orch = require("./src/agent/research/research-orchestrator");'
    ' const fn = Orch._optionUnderlyingSupported;'
    ' const arg = process.argv[1];'
    ' const val = arg === "__NULL__" ? null : arg;'
    ' console.log(JSON.stringify(fn(val)));'
)


def _call(arg):
    env = os.environ.copy()
    env['OPENCLAW_DIR'] = WORKTREE
    proc = subprocess.run(
        ['node', '-e', NODE_SNIPPET, arg],
        capture_output=True, text=True, cwd=WORKTREE, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_supported_index_etf_pass():
    assert _call('SPY') is True
    assert _call('QQQ') is True
    assert _call('IWM') is True


def test_single_name_rejected():
    assert _call('AAPL') is False


def test_null_rejected():
    assert _call('__NULL__') is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_orchestrator_option_envelope.py -v`
Expected: FAIL — `_optionUnderlyingSupported` is undefined.

- [ ] **Step 3: Implement the helper + wire it into `_codeStrategy`**

In `src/agent/research/research-orchestrator.js`, after the `_validateInferredClass` function you added in Task 1, add:
```javascript

/**
 * SP-4: True iff `underlying` is in the Phase-0 synthetic-greeks envelope
 * (backtest.vol_index.VALID_OPTION_UNDERLYINGS — index/ETF ATM only). Used to
 * hard-reject out-of-envelope option strategies (single-name / OTM-wing) before
 * coding, since the synthetic engine can't price them with promotion-grade
 * fidelity. Not gated: the orchestrator only calls it when class=='option',
 * which itself requires the gate ON.
 */
function _optionUnderlyingSupported(underlying) {
  if (!underlying) return false;
  const r = spawnSync(PYTHON, ['-c',
    'from src.backtest.vol_index import is_supported_option_underlying; '
    + 'import sys; sys.exit(0 if is_supported_option_underlying(sys.argv[1]) else 1)',
    String(underlying)], { encoding: 'utf8', cwd: OPENCLAW_DIR });
  return r.status === 0;
}
```

Then export it (after the Task 1 export line at the bottom):
```javascript
module.exports._optionUnderlyingSupported = _optionUnderlyingSupported;
```

Now modify `_codeStrategy` (currently lines 1046-1059). Replace its opening so it validates the class and enforces the envelope. The current body starts:
```javascript
  async _codeStrategy(strategySpec) {
    const validInferred = _validateInferredFilter(strategySpec?.inferred_universe_filter ?? null);
    const ctx = {
      role:          'implement_strategy',
      STRATEGY_SPEC: JSON.stringify(strategySpec),
      instructions:  'Implement this strategy. Apply fundjohn:strategy-coder and fundjohn:backtest-plumb skills.',
      INFERRED_UNIVERSE_FILTER: validInferred,  // null or one of the 12 CANDIDATE_PREDICATES
    };
```
Replace it with:
```javascript
  async _codeStrategy(strategySpec) {
    const validInferred = _validateInferredFilter(strategySpec?.inferred_universe_filter ?? null);
    const validClass    = _validateInferredClass(strategySpec?.inferred_instrument_class ?? null);
    // SP-4 envelope guard: an option strategy must be on a Phase-0-supported
    // index/ETF underlying, else the synthetic greeks engine can't price it
    // with promotion-grade fidelity. Hard-reject here (propagates to
    // _codeFromQueue's catch → status 'coding'→'failed') rather than emit a
    // bogus backtest. Only fires when the gate is ON (validClass=='option').
    if (validClass === 'option'
        && !_optionUnderlyingSupported(strategySpec?.inferred_option_underlying ?? null)) {
      throw new Error(
        `option_envelope_unsupported: underlying `
        + `'${strategySpec?.inferred_option_underlying ?? null}' not in `
        + `VALID_OPTION_UNDERLYINGS (Phase-0 index/ETF-ATM envelope)`);
    }
    const ctx = {
      role:          'implement_strategy',
      STRATEGY_SPEC: JSON.stringify(strategySpec),
      instructions:  'Implement this strategy. Apply fundjohn:strategy-coder and fundjohn:backtest-plumb skills.',
      INFERRED_UNIVERSE_FILTER:  validInferred,  // null or one of the 12 CANDIDATE_PREDICATES
      INFERRED_INSTRUMENT_CLASS: validClass,     // equity (default/gate-off) | option | etp | crypto | futures
    };
```
(Leave the rest of `_codeStrategy` — the `_runSubagent` call and `_registerStrategy` — unchanged.)

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_orchestrator_option_envelope.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agent/research/research-orchestrator.js tests/test_orchestrator_option_envelope.py
git commit -m "feat(sp4): thread INFERRED_INSTRUMENT_CLASS into coder ctx + option-envelope hard-gate

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Mirror the (dead) READY-path spec merge for completeness

**Files:**
- Modify: `src/agent/research/research-orchestrator.js:433`

**Why:** the `classification.ready` path is dead in prod (researchjohn unmapped), but mirroring the universe-filter merge keeps the two fields symmetric so a future researchjohn revival doesn't silently drop the class. No test (dead path); covered by Task 15's live run via the saturday spread.

- [ ] **Step 1: Edit the merge**

At line 433, change:
```javascript
      const specWithPred = { ...item.strategy_spec, inferred_universe_filter: hr?.inferred_universe_filter ?? null };
```
to:
```javascript
      const specWithPred = {
        ...item.strategy_spec,
        inferred_universe_filter:  hr?.inferred_universe_filter ?? null,
        inferred_instrument_class: hr?.inferred_instrument_class ?? null,
      };
```

- [ ] **Step 2: Sanity-check syntax**

Run: `node -e "require('./src/agent/research/research-orchestrator')" && echo OK`
Expected: prints `OK` (module loads without syntax error).

- [ ] **Step 3: Commit**

```bash
git add src/agent/research/research-orchestrator.js
git commit -m "chore(sp4): mirror inferred_instrument_class in dead READY-path merge (symmetry)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: PaperHunter prompt — emit `inferred_instrument_class` + envelope + crypto Gate-2

**Files:**
- Modify: `src/agent/prompts/subagents/paperhunter.md`

No automated test (prompt file); validated by Task 15's real run. Each edit below is an exact insertion.

- [ ] **Step 1: Add a new Step 5b after the universe-inference rules (after line 167)**

After line 167 (`- Write your choice as \`inferred_universe_filter\` in the output JSON (see Step 7).`) insert:
```markdown

## Step 5b — Infer instrument class (SP-4)

Independently of the universe predicate, classify the strategy's instrument
class. Write it as `inferred_instrument_class` in the output JSON.

| Class | Choose when | Envelope (must hold or REJECT) |
|---|---|---|
| `option` | Strategy trades listed options / volatility (data_requirements include `options_eod`, or direction is `SELL_VOL`/`BUY_VOL`, e.g. straddles, strangles, variance/vol-premium, delta-hedged vol). | Underlying MUST be an index/ETF in our synthetic-greeks envelope: **SPY, SPX, ^GSPC, QQQ, IWM**, and ATM / near-term. Single-name options, OTM-wing/skew, or any other underlying → REJECT with `option_envelope_unsupported`. |
| `etp` | Strategy rotates/holds exchange-traded products / commodity ETPs (e.g. GLD, SLV, USO, sector ETFs) on price data. | No leverage-decay strategies (we don't model intraday decay). |
| `crypto` | Strategy trades crypto spot, **BTC-USD or ETH-USD only**, using price-derived signals (momentum, carry, trend). | Anything needing funding-rate, perpetual/OI, or order-book data → REJECT via Gate 2 (capability_gap). |
| `equity` (default) | Everything else (the existing equity-momentum/factor/mean-reversion world), OR when unsure. | — |

Also emit `inferred_option_underlying`: when `inferred_instrument_class` is
`option`, the single primary underlying ticker (e.g. `"SPY"`); otherwise `null`.

**Rules:**
- Emit exactly one of: `equity` | `option` | `etp` | `crypto`. Default to `equity` when unsure (the downstream gate treats null/unknown as equity).
- If the strategy is `option` but the underlying is NOT in {SPY, SPX, ^GSPC, QQQ, IWM} (or is OTM-wing/skew/single-name), emit the rejection stub with `rejection_reason_if_any: "option_envelope_unsupported"` and stop.
- `etp` and `crypto` strategies use `inferred_universe_filter: null` (the 12 predicates are equity universes — they do not apply).
```

- [ ] **Step 2: Extend Gate 2 (capability_gap) with the crypto-unavailable clause**

In Step 6, Gate 2 (lines 180-188), after the existing line 188 (`web_scrape`, `social_sentiment`, `alt_data` → always fire.`) insert:
```markdown

**Crypto data axis (SP-4):** BTC-USD / ETH-USD daily price bars ARE available
(via the canonical `prices` column — list `prices`, not a crypto-specific
column). But crypto microstructure columns — `funding_rate`, `perp_oi`,
`open_interest`, `order_book`, `spot_vol` — are NOT available; a crypto
strategy requiring any of them → always fire (`capability_gap`).
```

- [ ] **Step 3: Add the two new fields to the Step 7 output schema (after line 208)**

In the "all gates pass" JSON block, after line 208 (`"inferred_universe_filter": "<one of the 12 predicate names | null>",`) insert:
```markdown
  "inferred_instrument_class": "equity | option | etp | crypto",
  "inferred_option_underlying": "<index/ETF ticker when option, else null>",
```

- [ ] **Step 4: Refresh the arXiv harvest-surface note (lines 246-256)**

Append to the "arXiv harvest surface" section (after line 252's `stat.ML` bullet) a line documenting the SP-4 additions:
```markdown
- **q-fin.PR / q-fin.MF** — derivatives/securities pricing + mathematical finance (SP-4: feeds options & vol papers)

PaperHunter now classifies each paper's `inferred_instrument_class` — options
strategies are accepted only inside the index/ETF-ATM envelope (see Step 5b);
crypto only for BTC/ETH price-only signals.
```

- [ ] **Step 5: Commit**

```bash
git add src/agent/prompts/subagents/paperhunter.md
git commit -m "feat(sp4): PaperHunter infers instrument_class + option envelope + crypto Gate-2

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

# Group 2 — StrategyCoder + lifecycle (Phase C)

## Task 5: StrategyCoder prompt — emit `instrument_class` + per-class guidance

**Files:**
- Modify: `src/agent/prompts/subagents/strategycoder.md`

- [ ] **Step 1: Add an instrument-class section before the "Universe predicate" section (before line 50)**

Immediately before line 50 (`### Universe predicate`) insert:
```markdown
### Instrument class (SP-4)

The orchestrator injects the validated class into your context as:
- `INFERRED_INSTRUMENT_CLASS = "equity" | "option" | "etp" | "crypto"`

You MUST do BOTH of the following with that value:

1. **Manifest field** — set `"instrument_class": "<INFERRED_INSTRUMENT_CLASS>"`
   as a TOP-LEVEL key on the manifest entry (Artifact 3 below shows where).
2. **Module constant** — add `INSTRUMENT_CLASS = "<INFERRED_INSTRUMENT_CLASS>"`
   at module scope in the `.py` (right after imports), so lifecycle can
   auto-detect it on the register()-creates path.

Per-class implementation guidance:
- **`option`** — emit `Signal.direction` ∈ {`SELL_VOL`, `BUY_VOL`} (never LONG/SHORT for the option legs). Populate `Signal.option_spec` with an `OptionSpec` (imported from `strategies.base`): set `underlying`, `right`, `strike_rule`/`target_delta`, `dte_target`, `structure` (`single|straddle|strangle`), `hedge` (`none|delta`). The underlying MUST be one of SPY/SPX/^GSPC/QQQ/IWM. Universe filter is typically `options_eligible_only` or null.
- **`crypto`** — emit `Signal.direction` ∈ {`LONG`, `FLAT`} on `BTC-USD`/`ETH-USD`. Do NOT define a `universe_filter` (the 12 predicates are equity universes; leave it out → default applies but is irrelevant for a fixed crypto ticker set).
- **`etp`** — standard `LONG`/`FLAT` momentum/rotation on the ETP tickers (e.g. GLD/SLV/USO) using generic `prices`. No universe_filter unless an equity predicate genuinely applies.
- **`equity`** (default) — unchanged from today.
```

- [ ] **Step 2: Update the Artifact 3 manifest template (lines 121-132) to include `instrument_class`**

Replace the JSON block at lines 121-131 with:
```markdown
```json
"S_XX_your_strategy_id": {
  "state": "candidate",
  "state_since": "<ISO-8601 timestamp>",
  "metadata": {
    "canonical_file": "s_xx_your_strategy_id.py",
    "class": "YourStrategyClass",
    "description": "Brief description from strategy_spec"
  },
  "history": [],
  "instrument_class": "<INFERRED_INSTRUMENT_CLASS, default equity>"
}
```
```

- [ ] **Step 3: Commit**

```bash
git add src/agent/prompts/subagents/strategycoder.md
git commit -m "feat(sp4): StrategyCoder emits instrument_class (manifest + module const) + per-class templates

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: lifecycle — AST-detect `INSTRUMENT_CLASS` at register() (gated) + _stage manifest write

**Files:**
- Modify: `src/strategies/lifecycle.py` (add `_detect_module_instrument_class` after `_detect_module_predicate` at line 279; wire into `register()` at line 668)
- Modify: `src/agent/curators/saturday_brain.js:457-464` (thread `instrument_class` into the Tier-B STAGING manifest write)
- Test: `tests/test_lifecycle_instrument_class_detect.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_lifecycle_instrument_class_detect.py`:
```python
"""SP-4: lifecycle.register() AST-detects a module-level INSTRUMENT_CLASS
from the impl file, gated by OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT.
Run: pytest tests/test_lifecycle_instrument_class_detect.py -v
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import strategies.lifecycle as lc  # noqa: E402
from strategies.lifecycle import LifecycleStateMachine, StrategyState  # noqa: E402


def _write_impl(tmp_path, body):
    p = tmp_path / 'S_probe.py'
    p.write_text(body, encoding='utf-8')
    return p


def test_detect_reads_module_constant(tmp_path):
    p = _write_impl(tmp_path, 'INSTRUMENT_CLASS = "option"\n\nclass X: pass\n')
    assert lc._detect_module_instrument_class(p) == 'option'


def test_detect_none_when_absent(tmp_path):
    p = _write_impl(tmp_path, 'class X: pass\n')
    assert lc._detect_module_instrument_class(p) is None


def test_detect_ignores_unknown_value(tmp_path):
    p = _write_impl(tmp_path, 'INSTRUMENT_CLASS = "banana"\n')
    assert lc._detect_module_instrument_class(p) is None


def test_register_sets_instrument_class_when_gate_on(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT', '1')
    monkeypatch.setattr(lc, '_IMPLEMENTATIONS_DIR', tmp_path)
    _write_impl(tmp_path, 'INSTRUMENT_CLASS = "crypto"\nclass X: pass\n')
    sm = LifecycleStateMachine.new_empty()
    rec = sm.register('S_probe', initial_state=StrategyState.CANDIDATE,
                      metadata={'canonical_file': 'S_probe.py'})
    assert rec.instrument_class == 'crypto'


def test_register_defaults_equity_when_gate_off(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT', raising=False)
    monkeypatch.setattr(lc, '_IMPLEMENTATIONS_DIR', tmp_path)
    _write_impl(tmp_path, 'INSTRUMENT_CLASS = "crypto"\nclass X: pass\n')
    sm = LifecycleStateMachine.new_empty()
    rec = sm.register('S_probe', initial_state=StrategyState.CANDIDATE,
                      metadata={'canonical_file': 'S_probe.py'})
    assert rec.instrument_class == 'equity'
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_lifecycle_instrument_class_detect.py -v`
Expected: FAIL — `_detect_module_instrument_class` does not exist.

- [ ] **Step 3: Implement the detector + wire into register()**

In `src/strategies/lifecycle.py`, after `_detect_module_predicate` (ends line 279) add:
```python

def _detect_module_instrument_class(file_path) -> "str | None":
    """Return the instrument_class if the strategy file has a top-level
    ``INSTRUMENT_CLASS = "<class>"`` assignment whose value is in
    VALID_INSTRUMENT_CLASSES, else None. Mirrors _detect_module_predicate;
    returns None on FileNotFoundError / SyntaxError / OSError.
    """
    try:
        tree = ast.parse(Path(file_path).read_text())
    except (FileNotFoundError, SyntaxError, OSError):
        return None
    for node in tree.body:   # top-level only
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "INSTRUMENT_CLASS" in targets and isinstance(node.value, ast.Constant):
                val = node.value.value
                if isinstance(val, str) and val in VALID_INSTRUMENT_CLASSES:
                    return val
    return None
```

In `register()`, after the Phase-D predicate-detection block (ends line 667, `rec.universe_filter_ref = f"src.strategies.universe_default:{detected}"`) and BEFORE line 668 (`self._records[strategy_id] = rec`), add:
```python
        # SP-4: detect and persist instrument_class from the impl file, gated
        # by OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1. Covers the rare
        # register()-creates-the-record path (StrategyCoder normally writes the
        # manifest entry directly, which from_manifest already reads).
        if os.environ.get("OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT") == "1":
            canonical = (metadata or {}).get("canonical_file")
            if canonical:
                detected_ic = _detect_module_instrument_class(_IMPLEMENTATIONS_DIR / canonical)
                if detected_ic:
                    rec.instrument_class = detected_ic
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_lifecycle_instrument_class_detect.py -v && pytest tests/test_lifecycle_instrument_class.py -v`
Expected: both PASS (new 5 + existing 6).

- [ ] **Step 5: Thread instrument_class into the Tier-B STAGING manifest write**

In `src/agent/curators/saturday_brain.js`, the Phase-7 `_stage` manifest write (lines 457-464) currently omits `instrument_class`. Change the object so it carries the hunter's inferred class (default equity):
```javascript
        manifest.strategies[sid] = {
          state:       'staging',
          state_since: now,
          metadata: {
            canonical_file: `${sid.toLowerCase()}.py`,
            class:          sid,
            description:    (hunterResult.hypothesis_one_liner || sid).slice(0, 280),
          },
          history: [],
          instrument_class: hunterResult.inferred_instrument_class || 'equity',
```
(Keep the rest of the object — `eligible_regimes` etc. — unchanged; only add the `history`/`instrument_class` keys if not already present. If `history: []` is already in the object below line 464, do not duplicate it — add only `instrument_class`.)

- [ ] **Step 6: Sanity-check the JS + commit**

Run: `node -e "require('./src/agent/curators/saturday_brain')" && echo OK`
Expected: `OK`.
```bash
git add src/strategies/lifecycle.py tests/test_lifecycle_instrument_class_detect.py src/agent/curators/saturday_brain.js
git commit -m "feat(sp4): lifecycle AST-detects INSTRUMENT_CLASS at register() + Tier-B staging carries it

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

# Group 3 — Corpus recognition + ingestion breadth (Phase A)

## Task 7: Mastermind corpus prompt — un-gate crypto, options heuristics, class output + floors

**Files:**
- Modify: `src/agent/prompts/subagents/mastermind.md`

- [ ] **Step 1: Rewrite the "NEVER AVAILABLE" crypto line (line 61)**

Change line 61 from:
```markdown
- Futures/FX/crypto data
```
to:
```markdown
- Futures/FX data (unavailable)
- Crypto microstructure: funding-rate, perpetual/open-interest, order-book, spot-vol (unavailable). NOTE: BTC-USD and ETH-USD **daily price bars ARE available** — crypto price-only strategies (momentum/carry) are implementable.
```

- [ ] **Step 2: Add options + crypto heuristics to the "Strong positive" / "Strong negative" blocks**

In the "Strong positive signals" block (after line 85), add:
```markdown
- Index/ETF volatility-premium strategies (short straddle/strangle, variance, delta-hedged vol) on SPY/SPX/QQQ/IWM — implementable via our synthetic greeks engine (`option` class)
- BTC-USD / ETH-USD price-only momentum or carry — implementable (`crypto` class)
- Commodity/sector ETP rotation on price data (GLD/SLV/USO/sector ETFs) — implementable (`etp` class)
```
In the "Strong negative signals" block (after line 98), add:
```markdown
- Single-name or OTM-wing/skew options, exotic/structured/barrier options, forex options — out of our synthetic-engine envelope (reject)
- Crypto derivatives / perpetuals / funding-rate strategies — data unavailable (reject)
```

- [ ] **Step 3: Add `inferred_instrument_class` to the output schema (after line 157)**

In the JSON schema block, after line 157 (`"data_requirements_hint": {...},`) insert:
```markdown
    "inferred_instrument_class": "equity | option | etp | crypto",
```

- [ ] **Step 4: Add a per-class confidence-floor rule to the field docs (after line 187)**

After the "Saturday brain bucket override" paragraph (ends line 187) insert:
```markdown

**`inferred_instrument_class`** — classify each paper's instrument class
(`equity` default | `option` | `etp` | `crypto`). Apply a per-class confidence
floor when assigning the bucket, to avoid spending PaperHunter/backtest budget
on candidates that will fail the (higher) per-class promotion threshold:
- `option` papers: assign `high`/`implementable_candidate` only if `confidence ≥ 0.80`; below that, cap at `med`.
- `crypto` papers: only if `confidence ≥ 0.70`; below that, cap at `med`.
- `equity`/`etp`: unchanged (≥ 0.75 high).
These are heuristic pre-filters, not the authoritative gate (the lifecycle
promotion thresholds in `lifecycle.py` are authoritative).
```

- [ ] **Step 5: Commit**

```bash
git add src/agent/prompts/subagents/mastermind.md
git commit -m "feat(sp4): corpus rater recognizes option/etp/crypto + per-class confidence floors

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Mastermind.js — read `inferred_instrument_class` + apply class-aware bucket floor (no migration)

**Files:**
- Modify: `src/agent/curators/mastermind.js` (rating object at lines 883-942)
- Test: `tests/test_mastermind_class_floor.test.js`

**Why no migration:** the floor is applied in-memory at bucket-assignment time (before persist), so the persisted `predicted_bucket` already reflects it. The promotion SELECT reads `predicted_bucket` unchanged — no new column on `curated_candidates`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mastermind_class_floor.test.js`:
```javascript
/**
 * SP-4: per-class corpus bucket floor — option<0.80 and crypto<0.70 are
 * capped at 'med' even when implementability would otherwise promote them.
 * Run: node --test tests/test_mastermind_class_floor.test.js
 */
process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';
const { test } = require('node:test');
const assert   = require('node:assert/strict');
const { applyClassBucketFloor } = require('../src/agent/curators/mastermind');

test('option below 0.80 capped at med', () => {
  assert.equal(applyClassBucketFloor('implementable_candidate', 'option', 0.72), 'med');
});
test('option at/above 0.80 unchanged', () => {
  assert.equal(applyClassBucketFloor('implementable_candidate', 'option', 0.81), 'implementable_candidate');
});
test('crypto below 0.70 capped at med', () => {
  assert.equal(applyClassBucketFloor('high', 'crypto', 0.61), 'med');
});
test('crypto at/above 0.70 unchanged', () => {
  assert.equal(applyClassBucketFloor('high', 'crypto', 0.71), 'high');
});
test('equity unaffected', () => {
  assert.equal(applyClassBucketFloor('implementable_candidate', 'equity', 0.10), 'implementable_candidate');
});
test('non-high buckets pass through', () => {
  assert.equal(applyClassBucketFloor('low', 'option', 0.10), 'low');
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `node --test tests/test_mastermind_class_floor.test.js`
Expected: FAIL — `applyClassBucketFloor` is not exported / undefined.

- [ ] **Step 3: Implement the helper, apply it in the rating map, and export it**

In `src/agent/curators/mastermind.js`, near the other module constants (after line 100, `const ALL_BUCKETS = ...`), add:
```javascript
// SP-4: per-class confidence floor for high-tier corpus buckets. Caps
// option(<0.80)/crypto(<0.70) at 'med' so we don't spend PaperHunter/backtest
// budget on candidates that will fail the higher per-class promotion gate.
const CLASS_CONFIDENCE_FLOOR = { option: 0.80, crypto: 0.70 };
const HIGH_BUCKETS = new Set(['high', 'implementable_candidate']);

function applyClassBucketFloor(bucket, instrumentClass, confidence) {
  const floor = CLASS_CONFIDENCE_FLOOR[instrumentClass];
  if (floor != null && HIGH_BUCKETS.has(bucket) && (Number(confidence) || 0) < floor) {
    return 'med';
  }
  return bucket;
}
```

In the rating map (lines 902-926), after the bucket-override block (lines 912-915) and before the `return {` at line 916, add the class read + floor application. Replace lines 912-926's relevant portion so it reads:
```javascript
        let bucket = r.predicted_bucket || this._bucketFromConfidence(rawConf);
        if (rawImpl >= IMPL_BUCKET_THRESH) {
          bucket = 'implementable_candidate';
        }
        // SP-4: instrument class + per-class confidence floor.
        const instrumentClass = (typeof r.inferred_instrument_class === 'string'
          && ['equity', 'option', 'etp', 'crypto', 'futures'].includes(r.inferred_instrument_class))
          ? r.inferred_instrument_class : 'equity';
        bucket = applyClassBucketFloor(bucket, instrumentClass, rawConf);
        return {
          paper_id:                 p.paper_id,
          confidence:               rawConf,
          implementability_score:   rawImpl,
          inferred_instrument_class: instrumentClass,
          data_requirements_hint:   (r.data_requirements_hint && typeof r.data_requirements_hint === 'object')
                                        ? r.data_requirements_hint : null,
          predicted_bucket:         bucket,
          reasoning:                String(r.reasoning || '').slice(0, 2000),
          predicted_failure_modes:  Array.isArray(r.predicted_failure_modes) ? r.predicted_failure_modes : [],
          gate_predictions:         gatePredictions,
        };
```

At the bottom of the file (where other exports live — find `module.exports`), add `applyClassBucketFloor` to the exports. If the file does `module.exports = SomeClass;`, append:
```javascript
module.exports.applyClassBucketFloor = applyClassBucketFloor;
```

- [ ] **Step 4: Run to verify it passes**

Run: `node --test tests/test_mastermind_class_floor.test.js`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agent/curators/mastermind.js tests/test_mastermind_class_floor.test.js
git commit -m "feat(sp4): mastermind reads inferred_instrument_class + applies per-class bucket floor (no migration)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Ingestion breadth — arXiv categories + OpenAlex concepts/authors

**Files:**
- Modify: `src/ingestion/arxiv_discovery.py:44-47`
- Modify: `src/ingestion/openalex_discovery.py:69-74` and `:80-92`
- Test: `tests/test_ingestion_breadth.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingestion_breadth.py`:
```python
"""SP-4: ingestion harvest surface includes derivatives/vol categories + authors.
Run: pytest tests/test_ingestion_breadth.py -v
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))


def test_arxiv_has_derivatives_categories():
    from ingestion import arxiv_discovery as a
    assert 'q-fin.PR' in a.CATEGORIES
    assert 'q-fin.MF' in a.CATEGORIES
    # existing ones preserved
    assert 'q-fin.ST' in a.CATEGORIES and 'cs.LG' in a.CATEGORIES


def test_openalex_has_options_authors():
    from ingestion import openalex_discovery as o
    # crypto/options researchers added; existing equity authors preserved
    assert 'carr' in o.AUTHOR_WATCHLIST          # Peter Carr (options/vol)
    assert 'fama' in o.AUTHOR_WATCHLIST          # preserved
    # an options/vol concept added
    assert any(c not in ('C10138342', 'C64943373', 'C91602232', 'C93373587')
               for c in o.FINANCE_CONCEPTS)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_ingestion_breadth.py -v`
Expected: FAIL — `q-fin.PR` / `carr` not present.

- [ ] **Step 3: Edit arxiv_discovery.py CATEGORIES (lines 44-47)**

Replace lines 44-47 with:
```python
CATEGORIES          = [
    'q-fin.ST', 'q-fin.PM', 'q-fin.TR', 'q-fin.CP', 'q-fin.GN', 'q-fin.RM',
    'q-fin.PR', 'q-fin.MF',   # SP-4: derivatives/securities pricing + mathematical finance
    'cs.LG', 'cs.AI', 'cs.CL', 'stat.ML',
]
```

- [ ] **Step 4: Edit openalex_discovery.py concepts (lines 69-74) and authors (lines 80-92)**

Replace `FINANCE_CONCEPTS` (lines 69-74) with:
```python
FINANCE_CONCEPTS = [
    'C10138342',   # Finance (broad, level-1 parent)
    'C64943373',   # Alpha (finance)
    'C91602232',   # Volatility (finance)
    'C93373587',   # Mathematical finance
    'C2778572836', # Implied volatility (SP-4 — options/vol papers)
    'C2776164869', # Cryptocurrency (SP-4 — crypto papers)
]
```
Add to `AUTHOR_WATCHLIST` (inside the dict, before the closing `}` at line 92) — append these entries:
```python
    'carr':       'A5004055502',   # Peter Carr — options/volatility (NYU) [SP-4]
    'gatheral':   'A5046534564',   # Jim Gatheral — volatility surface (Baruch) [SP-4]
    'sinclair':   'A5005439539',   # Euan Sinclair — option vol-premium [SP-4]
```

> **NOTE for the implementer:** OpenAlex concept/author IDs above are best-effort. Before committing, verify each resolves at `https://api.openalex.org/concepts/<ID>` and `https://api.openalex.org/authors/<ID>`; if any returns 404, look up the correct ID by name via `https://api.openalex.org/authors?search=<name>` / `https://api.openalex.org/concepts?search=<term>` and substitute. The test only checks that *an* options author key (`carr`) and *a* new concept exist — it does not validate the ID strings — so a wrong ID would silently harvest nothing. Resolve them for real.

- [ ] **Step 5: Run to verify it passes + sanity-check imports**

Run: `pytest tests/test_ingestion_breadth.py -v && python3 -c "import sys; sys.path.insert(0,'.'); from src.ingestion import arxiv_discovery, openalex_discovery; print('import OK')"`
Expected: PASS + `import OK`.

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/arxiv_discovery.py src/ingestion/openalex_discovery.py tests/test_ingestion_breadth.py
git commit -m "feat(sp4): widen ingestion — q-fin.PR/MF arXiv cats + options/crypto OpenAlex authors/concepts

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: servers.json — crypto-column availability advisory taxonomy

**Files:**
- Modify: `src/agent/config/servers.json`
- Test: `tests/test_servers_crypto_taxonomy.py`

**Design:** add a top-level `crypto_data_taxonomy` advisory block (does NOT touch any `covered_columns`, so the capability gate's pass/fire logic is unchanged and still correctly rejects unavailable columns). It documents the axis for operators + mirrors the Gate-2 clause added in Task 4.

- [ ] **Step 1: Write the failing test**

Create `tests/test_servers_crypto_taxonomy.py`:
```python
"""SP-4: servers.json declares the crypto data availability taxonomy without
mutating any covered_columns (so the capability gate stays correct).
Run: pytest tests/test_servers_crypto_taxonomy.py -v
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVERS = ROOT / 'src' / 'agent' / 'config' / 'servers.json'


def test_taxonomy_present_and_shaped():
    data = json.loads(SERVERS.read_text())
    tax = data['crypto_data_taxonomy']
    assert 'prices' in ' '.join(tax['available']).lower()
    for col in ('funding_rate', 'perp_oi', 'order_book'):
        assert col in tax['unavailable']


def test_unavailable_cols_not_in_any_covered_columns():
    data = json.loads(SERVERS.read_text())
    covered = set()
    for s in data['servers']:
        covered.update(s.get('covered_columns', []))
    # The whole point: unavailable crypto cols must NOT be served, else the
    # capability gate would wrongly pass them.
    for col in ('funding_rate', 'perp_oi', 'order_book', 'spot_vol'):
        assert col not in covered


def test_servers_array_intact():
    data = json.loads(SERVERS.read_text())
    names = {s['name'] for s in data['servers']}
    assert {'fmp', 'alpaca', 'sec_edgar', 'tavily'} <= names
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_servers_crypto_taxonomy.py -v`
Expected: FAIL — `crypto_data_taxonomy` key missing.

- [ ] **Step 3: Add the advisory block**

In `src/agent/config/servers.json`, the top-level object currently has a single key `"servers"`. Add a sibling key. Change the closing of the file (lines 66-67) from:
```json
  ]
}
```
to:
```json
  ],
  "crypto_data_taxonomy": {
    "_comment": "SP-4 advisory — documents which crypto data is backtestable. The capability gate (paperhunter.md Gate 2) rejects any column NOT in a covered_columns list or the canonical set; these 'unavailable' names are intentionally absent from covered_columns so they keep failing.",
    "available": ["prices (BTC-USD / ETH-USD daily bars, via the canonical prices column)"],
    "unavailable": ["funding_rate", "perp_oi", "open_interest", "order_book", "spot_vol"]
  }
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_servers_crypto_taxonomy.py -v && python3 -c "import json; json.load(open('src/agent/config/servers.json')); print('valid JSON')"`
Expected: PASS (3 tests) + `valid JSON`.

- [ ] **Step 5: Commit**

```bash
git add src/agent/config/servers.json tests/test_servers_crypto_taxonomy.py
git commit -m "feat(sp4): declare crypto data availability taxonomy in servers.json (advisory, gate-safe)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

# Group 4 — Review-awareness (Phase D)

## Task 11: comprehensive_review — inject instrument_class + per-class thresholds into the Opus prompt

**Files:**
- Modify: `src/agent/curators/comprehensive_review.js` (`_reviewOne` lines 319-328 + `buildStrategyPrompt` lines 262-296)
- Test: `tests/test_comprehensive_review_class.test.js`

- [ ] **Step 1: Write the failing test**

Create `tests/test_comprehensive_review_class.test.js`:
```javascript
/**
 * SP-4: buildStrategyPrompt surfaces instrument_class + its promotion floor.
 * Run: node --test tests/test_comprehensive_review_class.test.js
 */
process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';
const { test } = require('node:test');
const assert   = require('node:assert/strict');
const { buildStrategyPrompt } = require('../src/agent/curators/comprehensive_review');

const emptyPack = { signals: [], pnl: [] };

test('option strategy prompt names the 0.80 floor', () => {
  const s = { id: 'S_x', name: 'X', status: 'live', tier: 2, backtest_sharpe: 0.6,
              backtest_return_pct: 5, backtest_max_dd_pct: 10, universe: [],
              signal_frequency: 'daily', parameters: {}, regime_conditions: {},
              instrument_class: 'option' };
  const p = buildStrategyPrompt(s, emptyPack, []);
  assert.match(p, /option/);
  assert.match(p, /0\.80/);
});

test('absent instrument_class defaults to equity in the prompt', () => {
  const s = { id: 'S_y', name: 'Y', status: 'live', tier: 2, backtest_sharpe: 0.6,
              backtest_return_pct: 5, backtest_max_dd_pct: 10, universe: [],
              signal_frequency: 'daily', parameters: {}, regime_conditions: {} };
  const p = buildStrategyPrompt(s, emptyPack, []);
  assert.match(p, /equity/);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `node --test tests/test_comprehensive_review_class.test.js`
Expected: FAIL — prompt has no instrument_class/threshold line (and `buildStrategyPrompt` may not be exported).

- [ ] **Step 3: Add a thresholds constant + an instrument-class line to the prompt**

In `src/agent/curators/comprehensive_review.js`, near the top (after the requires), add:
```javascript
// SP-4: mirror of lifecycle.py PROMOTION_THRESHOLDS (keep in sync). Used to
// tell the reviewer the correct per-class promotion floor for this strategy.
const PROMOTION_THRESHOLDS = {
  equity: { min_sharpe: 0.5,  max_drawdown: 0.20 },
  etp:    { min_sharpe: 0.5,  max_drawdown: 0.20 },
  option: { min_sharpe: 0.80, max_drawdown: 0.30 },
  crypto: { min_sharpe: 0.50, max_drawdown: 0.70 },
};
```

In `buildStrategyPrompt` (line 262), compute the class line and insert it into the template. Change the function so the body resolves the class first:
```javascript
function buildStrategyPrompt(strategy, tradePack, counterfactuals) {
  const ic = strategy.instrument_class || 'equity';
  const thr = PROMOTION_THRESHOLDS[ic] || PROMOTION_THRESHOLDS.equity;
  const classLine = `Instrument class: ${ic} (promotion floor: Sharpe ≥ ${thr.min_sharpe}, MaxDD ≤ ${(thr.max_drawdown * 100).toFixed(0)}%)`;
  return `${MEMO_SYSTEM_PREAMBLE}

Strategy: ${strategy.id} (${strategy.name})
Status: ${strategy.status}
Tier: ${strategy.tier}
${classLine}
Backtest: sharpe=${strategy.backtest_sharpe} ret=${strategy.backtest_return_pct}% dd=${strategy.backtest_max_dd_pct}%
Universe: ${(strategy.universe || []).join(', ')}
```
(Leave the remainder of the template string from `Signal frequency:` onward exactly as-is.)

If `buildStrategyPrompt` is not already exported, add at the file's `module.exports`:
```javascript
module.exports.buildStrategyPrompt = buildStrategyPrompt;
```

- [ ] **Step 4: Make `instrument_class` available on the strategy object**

In `_reviewOne` (line 319), the `strategy` comes from `_fetchStrategies` (DB, no instrument_class). Read it from the manifest before building the prompt. Immediately after line 321 (`const tradePack = await _buildTradePack(strategy.id);`) add:
```javascript
  // SP-4: enrich with instrument_class from the manifest (top-level field).
  try {
    const manifestPath = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');
    const mf = JSON.parse(require('fs').readFileSync(manifestPath, 'utf-8'));
    strategy.instrument_class = (mf.strategies || {})[strategy.id]?.instrument_class || 'equity';
  } catch (_) { strategy.instrument_class = strategy.instrument_class || 'equity'; }
```
(Confirm `path` and `OPENCLAW_DIR` are already required/defined at the top of the file — the agent confirmed a manifest read at lines 363-366 uses both, so they are in scope.)

- [ ] **Step 5: Run to verify it passes**

Run: `node --test tests/test_comprehensive_review_class.test.js`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add src/agent/curators/comprehensive_review.js tests/test_comprehensive_review_class.test.js
git commit -m "feat(sp4): comprehensive-review surfaces instrument_class + per-class promotion floor to Opus

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: position_recommender — carry instrument_class into the recommendation row

**Files:**
- Modify: `src/agent/curators/position_recommender.js` (loop at lines 230-282)
- Test: `tests/test_position_recommender_class.test.js`

- [ ] **Step 1: Write the failing test**

Create `tests/test_position_recommender_class.test.js`:
```javascript
/**
 * SP-4: _classForStrategy reads instrument_class from the manifest (default equity).
 * Run: node --test tests/test_position_recommender_class.test.js
 */
process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';
const { test } = require('node:test');
const assert   = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { _classForStrategyFromManifest } = require('../src/agent/curators/position_recommender');

test('reads declared instrument_class', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pr-'));
  const mf = path.join(dir, 'manifest.json');
  fs.writeFileSync(mf, JSON.stringify({ strategies: { S_a: { instrument_class: 'crypto' } } }));
  assert.equal(_classForStrategyFromManifest(mf, 'S_a'), 'crypto');
});

test('defaults equity when absent', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pr-'));
  const mf = path.join(dir, 'manifest.json');
  fs.writeFileSync(mf, JSON.stringify({ strategies: { S_a: {} } }));
  assert.equal(_classForStrategyFromManifest(mf, 'S_a'), 'equity');
  assert.equal(_classForStrategyFromManifest(mf, 'S_missing'), 'equity');
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `node --test tests/test_position_recommender_class.test.js`
Expected: FAIL — `_classForStrategyFromManifest` undefined.

- [ ] **Step 3: Implement the helper, use it in the loop, export it**

In `src/agent/curators/position_recommender.js`, add a helper near the other module functions:
```javascript
// SP-4: resolve a strategy's instrument_class from the manifest (top-level
// field, default equity). Pure (path injected) so it's unit-testable.
function _classForStrategyFromManifest(manifestPath, strategyId) {
  try {
    const mf = JSON.parse(require('fs').readFileSync(manifestPath, 'utf-8'));
    return (mf.strategies || {})[strategyId]?.instrument_class || 'equity';
  } catch (_) { return 'equity'; }
}
```

In the per-memo loop (lines 230-282), after line 232 (`const deltas = _deriveDeltas(memo.recommendations || {}, currentSize);`) add:
```javascript
    const instrumentClass = _classForStrategyFromManifest(
      require('path').join(OPENCLAW_DIR, 'src/strategies/manifest.json'), memo.strategy_id);
```
and include it on the pushed object (line 281), changing:
```javascript
    persisted.push({ strategy_id: memo.strategy_id, rec_id: rows[0].id, ...deltas });
```
to:
```javascript
    persisted.push({ strategy_id: memo.strategy_id, rec_id: rows[0].id, instrument_class: instrumentClass, ...deltas });
```
(Confirm `OPENCLAW_DIR` is defined at the top of the file; if not, define `const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');` near the requires. Do NOT change the `strategy_sizing_recommendations` INSERT — `instrument_class` rides only the in-memory digest object, so no DB column / migration is needed.)

At the file's `module.exports`, add:
```javascript
module.exports._classForStrategyFromManifest = _classForStrategyFromManifest;
```

- [ ] **Step 4: Run to verify it passes**

Run: `node --test tests/test_position_recommender_class.test.js`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agent/curators/position_recommender.js tests/test_position_recommender_class.test.js
git commit -m "feat(sp4): position-recs carries instrument_class in the recommendation digest

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: Per-class promotion-threshold regression test (confirm-only)

**Files:**
- Test: `tests/test_promotion_thresholds_per_class.py`

**Why:** Phase 0 already wired per-class thresholds; this locks the behavior so a future edit can't silently regress it (e.g., an option strategy passing at equity's 0.5).

- [ ] **Step 1: Write the test**

Create `tests/test_promotion_thresholds_per_class.py`:
```python
"""SP-4: candidate->live promotion applies the per-class threshold.
Run: pytest tests/test_promotion_thresholds_per_class.py -v
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from strategies.lifecycle import (  # noqa: E402
    LifecycleStateMachine, StrategyRecord, StrategyState, _promotion_threshold)


def _sm_with(instrument_class):
    rec = StrategyRecord(strategy_id='S_x', state=StrategyState.CANDIDATE,
                         state_since='2026-05-01T00:00:00Z',
                         instrument_class=instrument_class)
    return LifecycleStateMachine({'S_x': rec})


def test_thresholds_lookup():
    assert _promotion_threshold('option') == {'min_sharpe': 0.80, 'max_drawdown': 0.30}
    assert _promotion_threshold('crypto') == {'min_sharpe': 0.50, 'max_drawdown': 0.70}
    assert _promotion_threshold('equity') == {'min_sharpe': 0.5, 'max_drawdown': 0.20}


def test_option_blocked_at_equity_passing_sharpe():
    sm = _sm_with('option')
    ok, msg = sm.can_transition('S_x', StrategyState.LIVE,
                                {'sharpe': 0.6, 'max_drawdown': 0.10})
    assert not ok and '0.80' in msg or 'minimum 0.8' in msg


def test_option_passes_above_floor():
    sm = _sm_with('option')
    ok, _ = sm.can_transition('S_x', StrategyState.LIVE,
                              {'sharpe': 0.85, 'max_drawdown': 0.25})
    assert ok


def test_crypto_dd_tolerance():
    sm = _sm_with('crypto')
    ok, _ = sm.can_transition('S_x', StrategyState.LIVE,
                              {'sharpe': 0.6, 'max_drawdown': 0.65})
    assert ok  # 65% DD allowed for crypto, would fail equity's 20%
```

- [ ] **Step 2: Run to verify it passes (no impl change — confirm-only)**

Run: `pytest tests/test_promotion_thresholds_per_class.py -v`
Expected: PASS (4 tests). If `test_option_blocked_at_equity_passing_sharpe` fails on the assert wording, adjust the substring to match the actual `can_transition` message (`f"... < minimum {thr['min_sharpe']} ..."` ⇒ contains `0.8`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_promotion_thresholds_per_class.py
git commit -m "test(sp4): lock per-class candidate->live promotion thresholds

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

# Group 5 — Acceptance

## Task 14: Full regression + new-suite green

**Files:** none (verification).

- [ ] **Step 1: Run every new + adjacent test**

Run:
```bash
pytest tests/test_orchestrator_instrument_class_injection.py \
       tests/test_orchestrator_option_envelope.py \
       tests/test_lifecycle_instrument_class.py \
       tests/test_lifecycle_instrument_class_detect.py \
       tests/test_ingestion_breadth.py \
       tests/test_servers_crypto_taxonomy.py \
       tests/test_promotion_thresholds_per_class.py \
       tests/test_instrument_class_sizer.py -v
node --test tests/test_mastermind_class_floor.test.js \
            tests/test_comprehensive_review_class.test.js \
            tests/test_position_recommender_class.test.js \
            tests/test_research_parsejson.test.js
```
Expected: all green.

- [ ] **Step 2: Confirm gate-OFF byte-identical behavior**

Run: `pytest tests/test_orchestrator_predicate_injection.py -v` (the SP-2 Phase D suite — must still pass, proving we didn't disturb the universe_filter path).
Expected: PASS.

- [ ] **Step 3: No commit (verification task).** If anything fails, fix in the owning task and re-run.

---

## Task 15: Bounded real-run acceptance proof — option index-vol (OPERATOR GATE)

**Files:**
- Create: `scripts/sp4_origination_proof.py` (a one-shot harness)

**⚠️ OPERATOR GATE:** This task spends LLM budget (Sonnet paperhunter + strategycoder) and originates a strategy artifact. **Do NOT run Step 3 without explicit operator approval.** It is candidate-only (no live order, no promotion), surfaced for inspection. The controller MUST stop and request approval before Step 3.

- [ ] **Step 1: Write the proof harness**

Create `scripts/sp4_origination_proof.py`:
```python
"""SP-4 acceptance proof — originate ONE index-vol option strategy end-to-end,
candidate-only, with the gate ON. Surfaces the artifacts for operator review.
Does NOT promote, does NOT submit any order.

Usage (operator-approved only):
    OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1 python3 scripts/sp4_origination_proof.py --candidate-id <uuid>

Pick a curated index-vol option paper already in research_candidates (e.g. a
SPY/QQQ short-straddle or variance-premium paper), pass its candidate_id.
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--candidate-id', required=True)
    args = ap.parse_args()
    if os.environ.get('OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT') != '1':
        sys.exit('Refusing: set OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1 to run the proof.')
    # Drive paperhunter + strategycoder via the orchestrator's existing entry,
    # restricted to this one candidate, then print the resulting manifest entry.
    node = (
        'const O = require("./src/agent/research/research-orchestrator");'
        ' const o = new O();'
        ' (async () => {'
        '   const r = await o._runPaperHunter({ candidate_id: process.argv[1], source_url: "(proof)" });'
        '   console.log("HUNTER " + JSON.stringify({'
        '     instrument_class: r.inferred_instrument_class,'
        '     underlying: r.inferred_option_underlying,'
        '     universe: r.inferred_universe_filter,'
        '     strategy_id: r.strategy_id, rejected: r.rejection_reason_if_any }));'
        '   if (r.rejection_reason_if_any) process.exit(3);'
        '   await o._codeStrategy(r);'
        '   console.log("CODED " + (r.strategy_id||""));'
        ' })().catch(e => { console.error(e.message); process.exit(1); });'
    )
    subprocess.run(['node', '-e', node, args.candidate_id], cwd=ROOT, check=False)
    # Print the manifest entry for the coded strategy (operator inspection).
    print('--- manifest entries with non-equity instrument_class ---')
    mf = json.load(open(os.path.join(ROOT, 'src/strategies/manifest.json')))
    for sid, e in (mf.get('strategies') or {}).items():
        if e.get('instrument_class', 'equity') != 'equity':
            print(sid, e.get('instrument_class'), e.get('state'))


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: Syntax-check the harness (no LLM spend)**

Run: `python3 -c "import ast; ast.parse(open('scripts/sp4_origination_proof.py').read()); print('parse OK')"`
Expected: `parse OK`.

- [ ] **Step 3: 🛑 STOP — request operator approval, then run the proof**

The controller surfaces to the operator: *"Ready to run the bounded option-origination proof (Sonnet paperhunter+strategycoder, candidate-only, no order/promotion). Approve? Provide a curated index-vol option `candidate_id` (or ask me to discover one)."* On approval:
```bash
OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1 python3 scripts/sp4_origination_proof.py --candidate-id <approved-uuid>
```
Expected: `HUNTER {... instrument_class: "option", underlying: "SPY"/"QQQ" ...}` then `CODED S_...`, and the manifest listing shows the new strategy with `instrument_class: option`, `state: candidate`. If PaperHunter rejects with `option_envelope_unsupported`, that is also a valid demonstration of the guardrail (pick an in-envelope paper to show the success path).

- [ ] **Step 4: Commit the harness (regardless of run outcome)**

```bash
git add scripts/sp4_origination_proof.py
git commit -m "feat(sp4): bounded option-origination acceptance proof harness (operator-gated)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Final review (after all tasks)

Dispatch a final whole-diff code reviewer covering: (1) gate-OFF byte-identity (grep that every new branch keys on `OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT` or defaults to `equity`); (2) no schema migration introduced; (3) no master-data writes; (4) the `PROMOTION_THRESHOLDS` JS mirror in `comprehensive_review.js` matches `lifecycle.py`; (5) `_validateInferredClass`/`_optionUnderlyingSupported` exported. Then use **superpowers:finishing-a-development-branch** — surface to the operator for the merge decision (do NOT merge without approval; regen the integrity manifest on the VPS for the edited prompt files post-merge).

---

## Self-review (spec coverage)

- **Phase A** — ingestion breadth (Task 9), corpus recognition + class output + floors (Tasks 7, 8), crypto-column taxonomy (Task 10). ✓
- **Phase B** — PaperHunter inference + envelope + Gate-2 (Task 4), `_validateInferredClass` + gate (Task 1), option-underlying code enforcement (Task 2), dead-path symmetry (Task 3). ✓
- **Phase C** — StrategyCoder templates + manifest field (Task 5), lifecycle AST-detect + Tier-B staging carry (Task 6). ✓
- **Phase D** — review-context (Task 11), position-recs (Task 12), per-class threshold lock (Task 13). ✓
- **DoD** — deterministic tests (every task + Task 14) + bounded real run (Task 15, operator-gated). ✓
- **Constraints** — gate default-OFF byte-identical (Tasks 1, 6); no migration (Tasks 8, 12 explicitly avoid columns); option envelope (Tasks 2, 4); crypto taxonomy gate-safe (Task 10); worktree-isolated; merge surfaced (Final review). ✓
- **Type consistency** — `inferred_instrument_class` (top-level hunter field) → `INFERRED_INSTRUMENT_CLASS` (coder ctx key) → `instrument_class` (manifest field / `StrategyRecord`) used consistently across Tasks 1-6, 11-12. Gate var `OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT` consistent across Tasks 1, 6. `applyClassBucketFloor`, `_validateInferredClass`, `_optionUnderlyingSupported`, `_classForStrategyFromManifest`, `_detect_module_instrument_class`, `buildStrategyPrompt` — each defined once, referenced consistently.
