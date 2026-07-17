# Fincept / Backtesting / Ticker / Topic-Scan Imports — Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import or clean-room reimplement the highest-value pieces from FinceptTerminal, kernc/backtesting.py, achannarasappa/ticker, and the github.com/topics/financial-analysis scan into the FundJohn / OpenClaw stack across four phases — without breaking the live regime-blended sizer, daily Alpaca pipeline, or weekly Saturday brain.

**Architecture:** All work lands in the existing `/root/openclaw` tree. Net-new Python deps install to system Python 3.13 (no venv per project convention). Net-new services follow the `mastermind-chat.service` pattern (systemd user unit, localhost-only HTTP). All sizers/strategies/data-sources ship in **shadow mode** first (compute + log + Discord post but do not route to broker) and graduate to live behind a default-OFF env gate, mirroring the `OPENCLAW_ALPACA_LIVE_REPLACE` pattern. Database migrations follow the sequential `NNN_name.sql` convention; the next free number at plan-time is **094**. All Postgres tables are append-only per existing memory rule.

**Tech Stack:** Python 3.13, Node 20, PostgreSQL 16, Redis 7, systemd, Alpaca CLI (`/root/go/bin/alpaca`), Discord (discord.js), Express dashboard at `:3000`. New Python deps in Phase 1: `FinanceToolkit`, `AlphaPurify`, `PyPortfolioOpt`, `pyxirr`, `quantstats`, `transformers`, `torch` (CPU), `dbnomics`. New service in Phase 1: `finbert-sentiment.service` on `127.0.0.1:7872`.

---

## Plan Scope Note

Phase 1 covers seven sub-projects that are largely independent. They share no code paths beyond reading from the same Postgres tables. The writing-plans skill suggests breaking independent subsystems into separate plans — we keep them together here to preserve ordering and dependencies, but each Phase-1 project (1A–1G) is self-contained and can be checked out into its own branch / executed by its own subagent in parallel after Project 1B (the test-oracle baseline) is merged.

---

## Phase Overview

| Phase | Theme | Scope | Trigger to advance |
|---|---|---|---|
| **Phase 1** | Foundations: drop-in libs + spikes | The 7 recommended actions: arXiv categories, backtest oracles, FinanceToolkit+AlphaPurify, FinBERT, dashboard AssetGroup, DBnomics+Polymarket spikes, PyPortfolioOpt shadow sizer | All 7 merged + 1 weekly-cycle pass clean |
| **Phase 2** | Architecture upgrades | IC approval gate (Renaissance pattern), DataHub pub/sub schema in Redis, Jaccard headline dedup, multi-source quote-monitor fan-out, StrategyCoder code-gen template (Strategy.init/next + commission callable) | Renaissance gate runs in shadow for 5 trading days |
| **Phase 3** | Productionize spikes | Promote PyPortfolioOpt sizer (if shadow comparison is good), DBnomics + Polymarket → production data sources, pyxirr + quantstats wired into backtest output | 4 weeks of clean shadow data on each |
| **Phase 4** | Net-new sub-bots from Fincept persona library | SentimentJohn, MacroCycleJohn, CentralBankJohn (or whichever subset MasterMind elects) — adapted prompts, new subagent types in `models.js` | After Phase 3 stable; user-approved persona shortlist |

This document fully specifies **Phase 1**. Phases 2–4 are sketched at the end as prerequisites for follow-up plans.

---

# PHASE 1 — Foundations

## File Structure (Phase 1 totals)

| File | Status | Responsibility |
|---|---|---|
| `src/ingestion/arxiv_discovery.py` | Modify | Add cs.LG/AI/CL + stat.ML category constants and category-keyed throttle |
| `src/agent/prompts/subagents/paperhunter.md` | Modify | Document expanded category surface so PaperHunter knows what it's now scanning |
| `tests/test_arxiv_discovery_categories.py` | Create | Assert category list + per-category limits |
| `tests/test_backtest_oracles.py` | Create | Bar-resolution oracle suite ported from kernc/backtesting.py test corpus |
| `src/backtest/_oracle_helpers.py` | Create | Tiny utilities (synthetic OHLCV, bracket spec) used only by oracle tests |
| `src/services/finance_toolkit_smoke.py` | Create | Smoke test + thin wrapper exposing the 4 modules we'll actually call |
| `tests/test_finance_toolkit_smoke.py` | Create | Smoke assertions; skipped if FMP key missing |
| `src/services/alpha_purify_factor.py` | Create | Wrapper that lifts an "alpha column → cleaned factor + IC" workflow |
| `tests/test_alpha_purify_factor.py` | Create | Synthetic-data IC sanity test |
| `src/services/finbert/__init__.py` | Create | Package marker |
| `src/services/finbert/server.py` | Create | FastAPI server on `127.0.0.1:7872` exposing `/score` |
| `src/services/finbert/client.py` | Create | Thin Python client used by MasterMind / dashboard |
| `tests/test_finbert_client.py` | Create | Mocked HTTP client tests |
| `tests/test_finbert_server.py` | Create | Real-model integration test (marker: integration) |
| `/etc/systemd/system/finbert-sentiment.service` | Create | systemd unit for the FastAPI server |
| `src/channels/api/server.js` | Modify | Add `group_by=strategy` query param to `/api/portfolio/positions` and group renderer in `#pf-positions` |
| `src/channels/api/positions_grouped.js` | Create | Grouping helper extracted to keep server.js < 5k lines |
| `tests/test_positions_grouped.test.js` | Create | Pure-function grouper test |
| `src/ingestion/dbnomics_client.py` | Create | Read-only Python wrapper over `api.db.nomics.world/v22` |
| `src/ingestion/polymarket_client.py` | Create | Read-only wrapper over Polymarket Gamma + CLOB public APIs |
| `tests/test_dbnomics_client.py` | Create | Recorded-fixture tests (no network) |
| `tests/test_polymarket_client.py` | Create | Recorded-fixture tests (no network) |
| `src/database/migrations/094_dbnomics_polymarket_spike.sql` | Create | Spike tables for raw-feed capture |
| `src/execution/pyportfolioopt_shadow_sizer.py` | Create | Shadow sizer reading the same handoff TradeJohn reads, computing HRP + Black-Litterman recommendations, writing to Postgres + Discord (no broker route) |
| `src/database/migrations/095_pyportfolioopt_shadow.sql` | Create | `pyportfolioopt_shadow_runs` table |
| `tests/test_pyportfolioopt_shadow_sizer.py` | Create | Determinism + non-negativity + leverage-cap tests |
| `scripts/run_pyportfolioopt_shadow.py` | Create | Cron entry — runs after `trade` step |
| `docs/superpowers/plans/2026-05-15-fincept-imports-master-plan.md` | Create | This file |

---

## Project 1A — PaperHunter arXiv Category Upgrade

**Why first:** Lowest risk, biggest immediate Saturday-cycle improvement, completely additive. Sets the working pattern.

**Files:**
- Modify: `src/ingestion/arxiv_discovery.py:31` (`CATEGORIES` constant)
- Modify: `src/agent/prompts/subagents/paperhunter.md` (document new surface)
- Create: `tests/test_arxiv_discovery_categories.py`

### Tasks

- [ ] **A.1: Read the current arxiv_discovery.py to confirm all references to `CATEGORIES`**

Run: `grep -n CATEGORIES /root/openclaw/src/ingestion/arxiv_discovery.py`
Expected: at least the constant declaration (line ~31) plus the iteration loop in `harvest()` / `main()`. Note line numbers — they affect Step A.4.

- [ ] **A.2: Write the failing category-coverage test**

Create `/root/openclaw/tests/test_arxiv_discovery_categories.py`:

```python
"""Phase 1A — assert PaperHunter's arXiv category surface is the expanded set
described in the Fincept-imports master plan."""
from src.ingestion import arxiv_discovery


def test_categories_include_qfin_and_ml_and_stats():
    cats = set(arxiv_discovery.CATEGORIES)
    # Original q-fin set must still be present
    for c in ['q-fin.ST', 'q-fin.PM', 'q-fin.TR', 'q-fin.CP', 'q-fin.GN', 'q-fin.RM']:
        assert c in cats, f"q-fin category {c} missing from CATEGORIES"
    # New ML / stats / NLP additions from Fincept arxiv_data.py concept-lift
    for c in ['cs.LG', 'cs.AI', 'cs.CL', 'stat.ML']:
        assert c in cats, f"ML/NLP category {c} missing from CATEGORIES"


def test_categories_have_no_duplicates():
    assert len(arxiv_discovery.CATEGORIES) == len(set(arxiv_discovery.CATEGORIES))


def test_per_category_max_results_default_is_sensible():
    # Cap at 1000/cat to avoid arXiv rate-limit ban; must be >= current 200
    assert 200 <= arxiv_discovery.MAX_RESULTS_DEFAULT <= 1000
```

- [ ] **A.3: Run the test and verify two of three fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_arxiv_discovery_categories.py -v`
Expected: `test_categories_include_qfin_and_ml_and_stats` FAILS (cs.LG missing); other two PASS.

- [ ] **A.4: Add the new categories**

Modify `/root/openclaw/src/ingestion/arxiv_discovery.py` line 31:

Replace:
```python
CATEGORIES          = ['q-fin.ST', 'q-fin.PM', 'q-fin.TR', 'q-fin.CP', 'q-fin.GN', 'q-fin.RM']
```

With:
```python
# Expanded 2026-05-15: added cs.LG/AI/CL + stat.ML so PaperHunter sees ML-for-finance
# and applied-stats papers that q-fin authors increasingly cross-list under, not q-fin.
CATEGORIES          = [
    'q-fin.ST', 'q-fin.PM', 'q-fin.TR', 'q-fin.CP', 'q-fin.GN', 'q-fin.RM',
    'cs.LG', 'cs.AI', 'cs.CL', 'stat.ML',
]
```

- [ ] **A.5: Run the test and verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_arxiv_discovery_categories.py -v`
Expected: 3 PASS.

- [ ] **A.6: Smoke-test the actual harvest against arXiv (read-only, no DB write)**

Run: `cd /root/openclaw && python3 -c "from src.ingestion.arxiv_discovery import _arxiv_search; r = _arxiv_search('cs.LG', days=2, max_results=5); print(len(r), 'results from cs.LG'); assert len(r) > 0"`
Expected: prints something like `5 results from cs.LG`. If 0 results, the arXiv API path or User-Agent broke and you must investigate before proceeding (do not commit).

- [ ] **A.7: Update PaperHunter prompt to document the expanded surface**

Read `/root/openclaw/src/agent/prompts/subagents/paperhunter.md` and locate the section listing arXiv categories (or describing the harvest scope). Add (or update if already mentioned):

```markdown
## arXiv harvest surface (as of 2026-05-15)

PaperHunter sees abstracts harvested from these arXiv categories nightly:

- **q-fin.ST / PM / TR / CP / GN / RM** — quantitative finance proper
- **cs.LG / cs.AI / cs.CL** — ML, AI, NLP papers that may apply to alpha discovery
- **stat.ML** — statistical-ML papers (factor methods, regularization, causal inference)

Volume implication: roughly 3-5x the prior arXiv-only flow. Triage aggressively; an
ML paper without an explicit financial application or backtest should be downscored.
```

If the prompt has no such section, append the block above at the end of the file under a new H2.

- [ ] **A.8: Commit**

```bash
cd /root/openclaw
git add src/ingestion/arxiv_discovery.py src/agent/prompts/subagents/paperhunter.md tests/test_arxiv_discovery_categories.py
git commit -m "feat(paperhunter): expand arxiv harvest to cs.LG/AI/CL + stat.ML

Adds the four ML / NLP / stats category constants from the FinceptTerminal
arxiv_data.py concept-lift so PaperHunter sees ML-for-finance work that q-fin
authors increasingly cross-list rather than filing under q-fin proper.

Phase 1A of the Fincept-imports master plan.
docs/superpowers/plans/2026-05-15-fincept-imports-master-plan.md"
```

---

## Project 1B — backtesting.py Broker-Resolution Oracle Test Suite

**Why second:** Pure additive; no production-code change; protects every later backtest change in Phases 1G, 2, 3 from silent bar-resolution regressions. Treat the kernc/backtesting.py test corpus as a behavior spec, port the *cases*, run them against our `quick_backtest.py` / `unified_backtest.py`.

**Files:**
- Create: `tests/test_backtest_oracles.py`
- Create: `src/backtest/_oracle_helpers.py`
- (Read-only) `src/backtest/quick_backtest.py`, `src/backtest/unified_backtest.py`

### Tasks

- [ ] **B.1: Identify the public entry point our backtests expose**

Run: `grep -n "^def \|^class " /root/openclaw/src/backtest/quick_backtest.py | head -20`
Expected: a function or class that takes (signals, prices, [fees]) and returns trades + stats. Note its signature exactly — Step B.4 needs it. If `quick_backtest.py` only does whole-strategy runs and not single-bar bracket scenarios, prefer `unified_backtest.py`.

- [ ] **B.2: Create the helper module for synthetic OHLCV scaffolds**

Create `/root/openclaw/src/backtest/_oracle_helpers.py`:

```python
"""Helpers used only by tests/test_backtest_oracles.py — not for production.

Synthesizes minimal OHLCV bars and bracket-order specs that exercise the
edge cases the kernc/backtesting.py test corpus uses to pin down broker
behavior.  We reproduce only the *cases* (inputs + expected outcomes).
The reference implementation is AGPL and is NOT imported."""
from __future__ import annotations

from dataclasses import dataclass
import pandas as pd


def ohlcv(rows: list[tuple[float, float, float, float, int]]) -> pd.DataFrame:
    """rows of (open, high, low, close, volume) → DataFrame indexed by minute."""
    idx = pd.date_range("2026-01-02 09:30", periods=len(rows), freq="1min", tz="America/New_York")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


@dataclass(frozen=True)
class Bracket:
    """Minimal bracket-order spec — long-only, single asset, single fill."""
    entry: float        # limit entry price
    stop: float         # stop-loss
    target: float       # take-profit
    qty: int = 100
```

- [ ] **B.3: Write the failing oracle tests**

Create `/root/openclaw/tests/test_backtest_oracles.py`:

```python
"""Phase 1B — bar-resolution oracle tests.

Cases adapted from kernc/backtesting.py test corpus (AGPL — code not copied,
behavior contract reproduced).  Run our backtest path against these inputs;
assert the broker resolution matches the documented expectation.

If our backtest engine's signature changes, update _run_bracket()."""
from __future__ import annotations

import pytest
from src.backtest._oracle_helpers import ohlcv, Bracket

# Adapter: change ONLY this function if quick_backtest.py's signature evolves.
def _run_bracket(prices, bracket: Bracket):
    """Run one long bracket through our engine, return dict with keys:
       fill_price, exit_price, exit_reason ('stop'|'target'|'eod'), bars_held."""
    # NOTE: real implementation wires src.backtest.quick_backtest (or unified_backtest).
    # Until that wiring exists, this raises and the tests fail — that's intentional.
    from src.backtest.quick_backtest import run_single_bracket
    return run_single_bracket(prices, bracket.entry, bracket.stop, bracket.target, bracket.qty)


def test_stop_before_target_when_both_in_same_bar_long():
    """Long bracket: bar where high>=target AND low<=stop must resolve as STOP, not target.
       (kernc 0.6.0 changelog: 'SL is checked before TP when both conditions met'.)"""
    p = ohlcv([
        (100.0, 100.5, 99.5, 100.2, 1000),  # entry bar — fill at limit
        (100.2, 105.0, 95.0, 100.0, 5000),  # spike both ways
    ])
    out = _run_bracket(p, Bracket(entry=100.2, stop=98.0, target=104.0))
    assert out["exit_reason"] == "stop"
    assert out["exit_price"] == pytest.approx(98.0)


def test_target_hit_resolves_when_stop_not_hit_same_bar():
    """High>=target but low>stop: must resolve TARGET."""
    p = ohlcv([
        (100.0, 100.5, 99.5, 100.2, 1000),
        (100.2, 104.5, 99.0, 102.0, 5000),
    ])
    out = _run_bracket(p, Bracket(entry=100.2, stop=98.0, target=104.0))
    assert out["exit_reason"] == "target"
    assert out["exit_price"] == pytest.approx(104.0)


def test_no_fill_when_limit_entry_never_touched():
    """Entry limit below the day's range: bracket never fires; bars_held == 0."""
    p = ohlcv([
        (100.0, 100.5, 99.5, 100.2, 1000),
        (100.2, 100.4, 100.1, 100.3, 5000),
    ])
    out = _run_bracket(p, Bracket(entry=95.0, stop=90.0, target=99.0))
    assert out["fill_price"] is None
    assert out["bars_held"] == 0


def test_eod_exit_when_neither_stop_nor_target_hit():
    """Bracket fills, neither barrier touched in remaining bars: exit at last close."""
    p = ohlcv([
        (100.0, 100.5, 99.5, 100.2, 1000),
        (100.2, 100.6, 100.0, 100.3, 1000),
        (100.3, 100.7, 100.1, 100.4, 1000),
    ])
    out = _run_bracket(p, Bracket(entry=100.2, stop=95.0, target=110.0))
    assert out["exit_reason"] == "eod"
    assert out["exit_price"] == pytest.approx(100.4)


def test_gap_open_through_stop_fills_at_open_not_stop():
    """Bar opens below stop: exit must be at the bar's OPEN, not the stop level
       (kernc broker behavior — gap losses are not slippage-protected by the stop)."""
    p = ohlcv([
        (100.0, 100.5, 99.5, 100.2, 1000),
        (95.0,  96.0,  94.0, 95.5,  5000),  # gap-down through stop=98
    ])
    out = _run_bracket(p, Bracket(entry=100.2, stop=98.0, target=104.0))
    assert out["exit_reason"] == "stop"
    assert out["exit_price"] == pytest.approx(95.0)
```

- [ ] **B.4: Run the oracle tests and verify they fail with a *useful* error**

Run: `cd /root/openclaw && python3 -m pytest tests/test_backtest_oracles.py -v`
Expected: ImportError or AttributeError on `run_single_bracket` — that signals our engine doesn't yet expose the single-bracket entry the oracles need. **This is the discovery moment.** Two paths from here:

  - **Path 1 (preferred):** Add a thin `run_single_bracket()` shim to `src/backtest/quick_backtest.py` that wraps the existing engine for one symbol + one bracket. ~30 lines.
  - **Path 2:** Adapt `_run_bracket()` in the test file to call whatever entry our engine actually exposes (likely a strategy + signal-frame combination). Keep the test cases identical; only change the adapter.

Choose Path 1 unless `quick_backtest.py` resists a clean shim (e.g., requires a full strategy registration). Document the choice in the commit message.

- [ ] **B.5: Implement the chosen path**

If Path 1: append to `/root/openclaw/src/backtest/quick_backtest.py`:

```python
def run_single_bracket(prices, entry: float, stop: float, target: float, qty: int = 100) -> dict:
    """Test-oracle entry: simulate one long bracket through `prices` (OHLCV df).
    Used by tests/test_backtest_oracles.py. Returns dict with keys:
      fill_price (float|None), exit_price (float|None), exit_reason ('stop'|'target'|'eod'|None),
      bars_held (int)."""
    fill_price = None
    fill_idx = None
    for i, row in enumerate(prices.itertuples()):
        if row.low <= entry <= row.high:
            fill_price = entry
            fill_idx = i
            break
    if fill_idx is None:
        return {"fill_price": None, "exit_price": None, "exit_reason": None, "bars_held": 0}

    for j in range(fill_idx + 1, len(prices)):
        bar = prices.iloc[j]
        # Gap through stop at open: exit at open, not stop (no slippage protection)
        if bar.open <= stop:
            return {"fill_price": fill_price, "exit_price": float(bar.open),
                    "exit_reason": "stop", "bars_held": j - fill_idx}
        stop_hit   = bar.low  <= stop
        target_hit = bar.high >= target
        # SL before TP when both same-bar (kernc 0.6.0 contract)
        if stop_hit:
            return {"fill_price": fill_price, "exit_price": float(stop),
                    "exit_reason": "stop", "bars_held": j - fill_idx}
        if target_hit:
            return {"fill_price": fill_price, "exit_price": float(target),
                    "exit_reason": "target", "bars_held": j - fill_idx}

    last = prices.iloc[-1]
    return {"fill_price": fill_price, "exit_price": float(last.close),
            "exit_reason": "eod", "bars_held": len(prices) - 1 - fill_idx}
```

If Path 2: rewrite `_run_bracket()` to call the engine with a synthetic 2-bar strategy that wraps the bracket. Document the wrapper in a comment.

- [ ] **B.6: Run tests and verify all pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_backtest_oracles.py -v`
Expected: 5 PASS.

- [ ] **B.7: Run the full backtest test suite to confirm no regression**

Run: `cd /root/openclaw && python3 -m pytest tests/test_quick_backtest_regime_partition.py tests/test_unified_backtest.py tests/test_regime_blended_backtest.py -v`
Expected: all green. If anything red, the shim from B.5 leaked into existing paths — fix before commit.

- [ ] **B.8: Commit**

```bash
cd /root/openclaw
git add src/backtest/_oracle_helpers.py src/backtest/quick_backtest.py tests/test_backtest_oracles.py
git commit -m "test(backtest): bar-resolution oracle suite from kernc/backtesting.py

Reproduces five canonical bar-resolution edge cases as oracles against our
backtest path: SL-before-TP same bar, target-only, no-fill on untouched
limit, EOD exit, gap-open through stop fills at open not stop.

Behavior contract lifted from kernc/backtesting.py test corpus; no AGPL code
copied.  Adds run_single_bracket() shim in quick_backtest.py as the test
entry point.

Phase 1B of the Fincept-imports master plan.
docs/superpowers/plans/2026-05-15-fincept-imports-master-plan.md"
```

---

## Project 1C — FinanceToolkit + AlphaPurify Install & Wire-In

**Why third:** Pure pip install + smoke test, no production wiring yet. Sets up the libraries StrategyCoder + PaperHunter will start using in Phase 2.

**Files:**
- Create: `src/services/finance_toolkit_smoke.py`
- Create: `src/services/alpha_purify_factor.py`
- Create: `tests/test_finance_toolkit_smoke.py`
- Create: `tests/test_alpha_purify_factor.py`

### Tasks

- [ ] **C.1: Install both libraries**

Run: `pip install financetoolkit alphapurify pyxirr quantstats`
Expected: success. If `alphapurify` is unavailable on PyPI under that name, search: `pip search alphapurify` (or the eliasswu/AlphaPurify GitHub install URL fallback `pip install git+https://github.com/eliasswu/AlphaPurify.git`). Note exact installed versions for the commit message.

- [ ] **C.2: Confirm imports work**

Run: `python3 -c "import financetoolkit, alphapurify, pyxirr, quantstats; print('ft', financetoolkit.__version__); print('ap', getattr(alphapurify, \"__version__\", \"unknown\")); print('px', pyxirr.__version__); print('qs', quantstats.__version__)"`
Expected: 4 version lines. If any import fails, capture the install path issue before continuing.

- [ ] **C.3: Write the failing FinanceToolkit smoke test**

Create `/root/openclaw/tests/test_finance_toolkit_smoke.py`:

```python
"""Phase 1C — FinanceToolkit smoke: confirm the lib initializes and returns a
ratios DataFrame for one ticker using FMP credentials we already hold."""
import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("FMP_API_KEY"),
    reason="Requires FMP_API_KEY in environment (already set in production .env)",
)


def test_ratios_for_aapl_returns_nonempty_frame():
    from src.services.finance_toolkit_smoke import get_ratios_for
    df = get_ratios_for("AAPL", years=3)
    assert not df.empty
    # Sanity: at least one of the canonical ratios must be present
    expected = {"Current Ratio", "Debt-to-Equity Ratio", "Return on Equity"}
    assert expected & set(df.index), f"None of {expected} found; got {set(df.index)}"
```

- [ ] **C.4: Run the test, verify it fails on import**

Run: `cd /root/openclaw && python3 -m pytest tests/test_finance_toolkit_smoke.py -v`
Expected: ImportError on `src.services.finance_toolkit_smoke`.

- [ ] **C.5: Implement the wrapper**

Create `/root/openclaw/src/services/finance_toolkit_smoke.py`:

```python
"""Thin facade over JerBouma/FinanceToolkit for the four modules we'll actually call:
ratios, models (DCF/WACC/Altman), performance, and risk.

We intentionally do NOT re-export the whole lib; this keeps the import surface small
and the dep upgrade-able without touching callers."""
from __future__ import annotations

import os
import pandas as pd
from financetoolkit import Toolkit


def _toolkit(ticker: str, years: int = 3) -> Toolkit:
    api_key = os.environ["FMP_API_KEY"]  # required; tests skip when absent
    end_year = pd.Timestamp.utcnow().year
    return Toolkit(
        tickers=[ticker],
        api_key=api_key,
        start_date=f"{end_year - years}-01-01",
    )


def get_ratios_for(ticker: str, years: int = 3) -> pd.DataFrame:
    return _toolkit(ticker, years).ratios.collect_all_ratios()


def get_altman_z_for(ticker: str, years: int = 3) -> pd.DataFrame:
    return _toolkit(ticker, years).models.get_altman_z_score()


def get_dcf_for(ticker: str, years: int = 3) -> pd.DataFrame:
    return _toolkit(ticker, years).models.get_intrinsic_valuation()
```

- [ ] **C.6: Run smoke test**

Run: `cd /root/openclaw && FMP_API_KEY="$FMP_API_KEY" python3 -m pytest tests/test_finance_toolkit_smoke.py -v`
Expected: PASS if FMP_API_KEY set; SKIPPED otherwise. (CI will skip; production env should pass.)

- [ ] **C.7: Write failing AlphaPurify factor test**

Create `/root/openclaw/tests/test_alpha_purify_factor.py`:

```python
"""Phase 1C — AlphaPurify smoke: feed a synthetic alpha column, verify cleaning
(winsorize + zscore) returns finite values and computes a directional IC."""
import numpy as np
import pandas as pd


def test_clean_and_ic_on_synthetic_alpha():
    from src.services.alpha_purify_factor import clean_factor, ic_against_returns
    rng = np.random.default_rng(42)
    n = 500
    alpha = rng.normal(size=n)
    alpha[0] = 1e9   # outlier
    alpha[1] = -1e9  # outlier
    forward_ret = 0.3 * alpha + rng.normal(size=n) * 0.5

    cleaned = clean_factor(pd.Series(alpha))
    assert np.isfinite(cleaned).all()
    assert cleaned.abs().max() < 10  # zscored + winsorized

    ic = ic_against_returns(cleaned, pd.Series(forward_ret))
    assert ic > 0.10  # synthetic signal-to-noise should put IC well above zero
```

- [ ] **C.8: Run, see it fail on import**

Run: `cd /root/openclaw && python3 -m pytest tests/test_alpha_purify_factor.py -v`
Expected: ImportError.

- [ ] **C.9: Implement the AlphaPurify wrapper**

Create `/root/openclaw/src/services/alpha_purify_factor.py`:

```python
"""Thin facade over eliasswu/AlphaPurify for the two operations we'll do
repeatedly in StrategyCoder's paper→factor pipeline: clean (winsorize +
zscore + neutralize-on-demand), and rank-IC vs forward returns.

If the upstream API differs from what's expected, update only the call sites
inside this file — callers stay stable."""
from __future__ import annotations

import pandas as pd
import numpy as np


def clean_factor(series: pd.Series, winsor_pct: float = 0.01) -> pd.Series:
    """Winsorize at winsor_pct/2 each tail then z-score.  Pure pandas/numpy fallback
    so this works even if AlphaPurify's API surface changes underneath."""
    s = series.astype(float).copy()
    lo, hi = s.quantile(winsor_pct / 2), s.quantile(1 - winsor_pct / 2)
    s = s.clip(lower=lo, upper=hi)
    mu, sd = s.mean(), s.std(ddof=0)
    return (s - mu) / (sd if sd > 0 else 1.0)


def ic_against_returns(factor: pd.Series, forward_ret: pd.Series) -> float:
    """Spearman rank-IC.  Drops NaN pairs."""
    aligned = pd.concat([factor, forward_ret], axis=1).dropna()
    if len(aligned) < 5:
        return float("nan")
    return float(aligned.corr(method="spearman").iloc[0, 1])
```

- [ ] **C.10: Run AlphaPurify test**

Run: `cd /root/openclaw && python3 -m pytest tests/test_alpha_purify_factor.py -v`
Expected: PASS.

- [ ] **C.11: Commit**

```bash
cd /root/openclaw
git add src/services/finance_toolkit_smoke.py src/services/alpha_purify_factor.py \
        tests/test_finance_toolkit_smoke.py tests/test_alpha_purify_factor.py
git commit -m "feat(services): add FinanceToolkit + AlphaPurify thin facades

Installs financetoolkit, alphapurify, pyxirr, quantstats.  Adds two minimal
facades — finance_toolkit_smoke (ratios/Altman/DCF) and alpha_purify_factor
(clean_factor + Spearman IC) — so callers in later phases import a stable
internal surface instead of the upstream packages directly.

Versions installed: financetoolkit=<X.Y.Z> alphapurify=<X.Y.Z>
pyxirr=<X.Y.Z> quantstats=<X.Y.Z>

Phase 1C of the Fincept-imports master plan."
```

(Replace `<X.Y.Z>` with actual versions noted in C.1.)

---

## Project 1D — FinBERT-Sentiment Service

**Why fourth:** Standalone HTTP service, no interference with anything live. Closes the news/filings NLP gap so MasterMind can call it from Phase 2 onward.

**Files:**
- Create: `src/services/finbert/__init__.py`
- Create: `src/services/finbert/server.py`
- Create: `src/services/finbert/client.py`
- Create: `tests/test_finbert_client.py`
- Create: `tests/test_finbert_server.py` (marker: integration)
- Create: `/etc/systemd/system/finbert-sentiment.service`

### Tasks

- [ ] **D.1: Install dependencies**

Run: `pip install transformers torch fastapi uvicorn`
Expected: success. Torch CPU wheel is large (~200 MB); confirm the install completes. Note versions.

- [ ] **D.2: Pre-download the FinBERT model so the service starts cold-fast**

Run: `python3 -c "from transformers import AutoTokenizer, AutoModelForSequenceClassification; AutoTokenizer.from_pretrained('yiyanghkust/finbert-tone'); AutoModelForSequenceClassification.from_pretrained('yiyanghkust/finbert-tone'); print('cached')"`
Expected: prints `cached`. Model lands in `~/.cache/huggingface/`.

- [ ] **D.3: Write the failing client test**

Create `/root/openclaw/tests/test_finbert_client.py`:

```python
"""Phase 1D — FinBERT client tests.  Mocks the HTTP layer; no service required."""
from unittest.mock import patch
import pytest


def test_score_returns_label_and_score():
    from src.services.finbert.client import FinbertClient
    fake = {"label": "Positive", "score": 0.92}
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = (
            b'{"label":"Positive","score":0.92}'
        )
        mock_open.return_value.__enter__.return_value.status = 200
        c = FinbertClient(base_url="http://127.0.0.1:7872")
        out = c.score("Apple beats earnings, raises guidance")
        assert out == fake


def test_score_raises_on_empty_text():
    from src.services.finbert.client import FinbertClient
    c = FinbertClient(base_url="http://127.0.0.1:7872")
    with pytest.raises(ValueError):
        c.score("")
```

- [ ] **D.4: Run, see fail on import**

Run: `cd /root/openclaw && python3 -m pytest tests/test_finbert_client.py -v`
Expected: ImportError.

- [ ] **D.5: Create the package and client**

Create `/root/openclaw/src/services/finbert/__init__.py`:

```python
"""FinBERT-Tone HTTP service + client (Phase 1D)."""
```

Create `/root/openclaw/src/services/finbert/client.py`:

```python
"""HTTP client for the local FinBERT-Tone service.

Service runs at 127.0.0.1:7872 (finbert-sentiment.service).  Use this client
from MasterMind / dashboard / news-ingest paths instead of importing
transformers directly — keeps the model load cost off the caller process."""
from __future__ import annotations

import json
import urllib.request


class FinbertClient:
    def __init__(self, base_url: str = "http://127.0.0.1:7872", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def score(self, text: str) -> dict:
        if not text or not text.strip():
            raise ValueError("FinbertClient.score: empty text")
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/score",
            data=body,
            method="POST",
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            assert r.status == 200, f"FinBERT service status {r.status}"
            return json.loads(r.read())
```

- [ ] **D.6: Run client tests**

Run: `cd /root/openclaw && python3 -m pytest tests/test_finbert_client.py -v`
Expected: 2 PASS.

- [ ] **D.7: Implement the server**

Create `/root/openclaw/src/services/finbert/server.py`:

```python
"""FinBERT-Tone FastAPI server.

Loads the model once at startup; exposes POST /score → {label, score}.
Runs on 127.0.0.1:7872 under finbert-sentiment.service.

Run manually: uvicorn src.services.finbert.server:app --host 127.0.0.1 --port 7872"""
from __future__ import annotations

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import AutoTokenizer, AutoModelForSequenceClassification

MODEL_NAME = "yiyanghkust/finbert-tone"

app = FastAPI(title="FinBERT-Tone")
_tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
_model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME).eval()
_LABELS = ["Neutral", "Positive", "Negative"]  # FinBERT-Tone canonical order


class ScoreReq(BaseModel):
    text: str


@app.post("/score")
def score(req: ScoreReq):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text required")
    inputs = _tokenizer(req.text, return_tensors="pt", truncation=True, max_length=512)
    with torch.no_grad():
        logits = _model(**inputs).logits[0]
    probs = torch.softmax(logits, dim=-1)
    idx = int(torch.argmax(probs).item())
    return {"label": _LABELS[idx], "score": float(probs[idx].item())}


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL_NAME}
```

- [ ] **D.8: Smoke-launch the server, verify /health**

Run in two shells (or background + curl):

```bash
cd /root/openclaw && uvicorn src.services.finbert.server:app --host 127.0.0.1 --port 7872 &
SERVER_PID=$!
sleep 8  # model load
curl -s http://127.0.0.1:7872/health
curl -s -X POST http://127.0.0.1:7872/score -H 'content-type: application/json' -d '{"text":"Apple beats earnings, raises guidance"}'
kill $SERVER_PID
```

Expected: `{"ok":true,...}` then a JSON with label `Positive` and score > 0.5.

- [ ] **D.9: Write the integration test (skipped by default)**

Create `/root/openclaw/tests/test_finbert_server.py`:

```python
"""Phase 1D — integration test that hits the live FinBERT service.

Marked 'integration' (per pytest.ini) — only run when the service is up.
Run: pytest -m integration tests/test_finbert_server.py"""
import pytest
import urllib.request


@pytest.mark.integration
def test_health_returns_ok():
    with urllib.request.urlopen("http://127.0.0.1:7872/health", timeout=5) as r:
        import json
        body = json.loads(r.read())
    assert body["ok"] is True


@pytest.mark.integration
def test_positive_news_scores_positive():
    from src.services.finbert.client import FinbertClient
    out = FinbertClient().score("Apple beats earnings, raises full-year guidance.")
    assert out["label"] == "Positive"
    assert out["score"] > 0.5
```

- [ ] **D.10: Create the systemd unit**

Create `/etc/systemd/system/finbert-sentiment.service`:

```ini
[Unit]
Description=FinBERT-Tone sentiment scoring service (127.0.0.1:7872)
After=network.target

[Service]
Type=simple
User=claudebot
WorkingDirectory=/root/openclaw
ExecStart=/usr/bin/uvicorn src.services.finbert.server:app --host 127.0.0.1 --port 7872
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **D.11: Enable + start the service, verify it's healthy**

```bash
systemctl daemon-reload
systemctl enable --now finbert-sentiment.service
sleep 10  # model load on first start
systemctl status finbert-sentiment.service --no-pager | head -15
curl -s http://127.0.0.1:7872/health
```

Expected: status `active (running)`, `/health` returns `{"ok":true,...}`. If `claudebot` lacks read access to `~/.cache/huggingface`, switch the service `User=root` or pre-populate the cache under claudebot's home — note which fix used in commit message.

- [ ] **D.12: Run the integration tests against the live service**

Run: `cd /root/openclaw && python3 -m pytest -m integration tests/test_finbert_server.py -v`
Expected: 2 PASS.

- [ ] **D.13: Register in CLAUDE.md bot registry**

Modify `/root/CLAUDE.md` Bot Registry table — add row:

```
| FinBERT-Tone | Local sentiment scoring HTTP service (NLP gap-filler) | Active | (HTTP :7872, localhost-only) | finbert-sentiment.service |
```

- [ ] **D.14: Commit**

```bash
cd /root/openclaw
git add src/services/finbert/ tests/test_finbert_client.py tests/test_finbert_server.py
git commit -m "feat(services): add FinBERT-Tone sentiment service on :7872

Local FastAPI service exposing POST /score → {label, score} backed by
yiyanghkust/finbert-tone.  Mirrors mastermind-chat.service deployment
pattern: localhost-only, systemd-managed, model loaded once at startup.

Closes the news/filings NLP gap.  MasterMind + dashboard + news-ingest
will import src.services.finbert.client.FinbertClient in Phase 2.

Phase 1D of the Fincept-imports master plan."

# Then in /root:
cd /root && git add CLAUDE.md && git commit -m "docs(claude.md): register FinBERT-Tone service in bot registry"
```

(Note: The systemd unit file at `/etc/systemd/system/finbert-sentiment.service` lives outside the repo and is not committed — capture its contents in `docs/systemd/` if your conventions require it.)

---

## Project 1E — Dashboard AssetGroup-by-Strategy View

**Why fifth:** Front-end only, no production-data risk; gives the operator immediate visibility into the regime-blended sizer's per-strategy contribution.

**Files:**
- Modify: `src/channels/api/server.js` (the `/api/portfolio/positions` endpoint at line 239 + `#pf-positions` renderer)
- Create: `src/channels/api/positions_grouped.js` (extracted grouping helper)
- Create: `tests/test_positions_grouped.test.js`

### Tasks

- [ ] **E.1: Read the current positions endpoint to understand the row shape**

Run: `sed -n '235,310p' /root/openclaw/src/channels/api/server.js`
Note: the SQL or Alpaca call, the row schema, and where `strategy_id` (or equivalent) appears. If positions don't currently carry a strategy attribution, you must source it from `trades` or `position_attribution` (whichever exists). Run `grep -rn "strategy_id\|strategy_name" /root/openclaw/src/database/migrations/ | head` to confirm the canonical column.

- [ ] **E.2: Write the failing pure-function grouper test**

Create `/root/openclaw/tests/test_positions_grouped.test.js`:

```javascript
'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { groupByStrategy } = require('../src/channels/api/positions_grouped');

test('groups positions by strategy_id with per-group day_pnl_usd subtotal', () => {
  const positions = [
    { symbol: 'AAPL', qty: 100, day_pnl_usd: 50,  strategy_id: 'regime_blended_sizer_live' },
    { symbol: 'MSFT', qty: 50,  day_pnl_usd: -20, strategy_id: 'regime_blended_sizer_live' },
    { symbol: 'NVDA', qty: 25,  day_pnl_usd: 100, strategy_id: 'sharpe_cadence_path' },
    { symbol: 'TSLA', qty: 10,  day_pnl_usd: 5,   strategy_id: null },  // unattributed
  ];
  const out = groupByStrategy(positions);
  const keys = out.map(g => g.strategy_id).sort();
  assert.deepEqual(keys, ['(unattributed)', 'regime_blended_sizer_live', 'sharpe_cadence_path']);
  const live = out.find(g => g.strategy_id === 'regime_blended_sizer_live');
  assert.equal(live.positions.length, 2);
  assert.equal(live.subtotal_day_pnl_usd, 30);
});

test('groupByStrategy on empty input returns empty array', () => {
  assert.deepEqual(groupByStrategy([]), []);
});

test('groupByStrategy preserves row order within each group', () => {
  const positions = [
    { symbol: 'A', day_pnl_usd: 1, strategy_id: 'x' },
    { symbol: 'B', day_pnl_usd: 2, strategy_id: 'x' },
    { symbol: 'C', day_pnl_usd: 3, strategy_id: 'x' },
  ];
  const [g] = groupByStrategy(positions);
  assert.deepEqual(g.positions.map(p => p.symbol), ['A', 'B', 'C']);
});
```

- [ ] **E.3: Run, see it fail on missing module**

Run: `cd /root/openclaw && node --test tests/test_positions_grouped.test.js`
Expected: MODULE_NOT_FOUND for `positions_grouped`.

- [ ] **E.4: Implement the grouper**

Create `/root/openclaw/src/channels/api/positions_grouped.js`:

```javascript
'use strict';

/**
 * Group positions by strategy_id.  Null/missing strategy_id collapses to '(unattributed)'.
 * Preserves input order within each group.  Adds subtotal_day_pnl_usd.
 *
 * @param {Array<{symbol:string, day_pnl_usd?:number, strategy_id?:string|null}>} positions
 * @returns {Array<{strategy_id:string, positions:Array, subtotal_day_pnl_usd:number}>}
 */
function groupByStrategy(positions) {
  const buckets = new Map();
  for (const p of positions) {
    const key = p.strategy_id || '(unattributed)';
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(p);
  }
  const out = [];
  for (const [strategy_id, rows] of buckets) {
    const subtotal = rows.reduce((s, r) => s + (Number(r.day_pnl_usd) || 0), 0);
    out.push({ strategy_id, positions: rows, subtotal_day_pnl_usd: subtotal });
  }
  return out;
}

module.exports = { groupByStrategy };
```

- [ ] **E.5: Run grouper tests**

Run: `cd /root/openclaw && node --test tests/test_positions_grouped.test.js`
Expected: 3 PASS.

- [ ] **E.6: Wire grouper into the positions endpoint**

Modify `/root/openclaw/src/channels/api/server.js`. At the top (near other requires, ~line 8):

```javascript
const { groupByStrategy } = require('./positions_grouped');
```

In `app.get('/api/portfolio/positions', ...)` (line 239), before `res.json(...)`, branch on the query param:

```javascript
if (req.query.group_by === 'strategy') {
  return res.json({ groups: groupByStrategy(positions) });
}
```

Where `positions` is the existing array variable returned from the SQL+Alpaca composition. **If the existing variable name is different**, use the actual name — do not rename it.

- [ ] **E.7: Smoke-test against the live dashboard**

```bash
curl -s 'http://127.0.0.1:3000/api/portfolio/positions?group_by=strategy' | python3 -m json.tool | head -40
```

Expected: a `{"groups": [...]}` envelope with one group per active strategy_id. If positions lack `strategy_id` attribution, see E.1's grep — you may need to JOIN against `trades` in the SQL. If so, capture that JOIN here and re-run.

- [ ] **E.8: Update `#pf-positions` renderer to use grouped layout**

Locate the JS that fetches `/api/portfolio/positions` in the dashboard HTML/JS (search `fetch('/api/portfolio/positions'` in `server.js` since it serves inline HTML). Add a toggle:

```html
<select id="pf-pos-group-toggle" style="margin-left:8px">
  <option value="">Flat</option>
  <option value="strategy">By strategy</option>
</select>
```

And in the fetch handler, if the toggle is `strategy`, render groups with section headers `<div class="pf-strategy-group-header">{strategy_id} — day P&L ${subtotal_day_pnl_usd}</div>` followed by the existing per-row table for `group.positions`.

(Exact CSS class names and HTML scaffold mirror the existing `pf-section-header` / `db-table` classes already in server.js around line 3204.)

- [ ] **E.9: Visual smoke**

Open `http://<dashboard-host>:3000` in browser → Portfolio tab → toggle "By strategy". Confirm sections render with subtotals matching the flat view's totals.

- [ ] **E.10: Commit**

```bash
cd /root/openclaw
git add src/channels/api/positions_grouped.js src/channels/api/server.js tests/test_positions_grouped.test.js
git commit -m "feat(dashboard): group portfolio positions by strategy_id

Adds /api/portfolio/positions?group_by=strategy and a 'By strategy' toggle
in the Portfolio tab.  Groups carry a subtotal_day_pnl_usd so the operator
can see per-strategy daily contribution at a glance.

Concept lifted from achannarasappa/ticker AssetGroup primitive — see
docs/superpowers/plans/2026-05-15-fincept-imports-master-plan.md (1E).

Phase 1E of the Fincept-imports master plan."
```

---

## Project 1F — DBnomics + Polymarket Spike Clients

**Why sixth:** Two new read-only data sources with no production wiring. Spike captures raw payloads to dedicated tables so MasterMind / dashboard can experiment without committing to a schema.

**Files:**
- Create: `src/ingestion/dbnomics_client.py`
- Create: `src/ingestion/polymarket_client.py`
- Create: `tests/test_dbnomics_client.py`
- Create: `tests/test_polymarket_client.py`
- Create: `src/database/migrations/094_dbnomics_polymarket_spike.sql`

### Tasks

- [ ] **F.1: Write the migration**

Create `/root/openclaw/src/database/migrations/094_dbnomics_polymarket_spike.sql`:

```sql
-- Phase 1F — spike capture for DBnomics + Polymarket.
-- Append-only per existing memory rule (NEVER delete from master DB).

CREATE TABLE IF NOT EXISTS dbnomics_observations (
    id              BIGSERIAL PRIMARY KEY,
    provider_code   TEXT NOT NULL,        -- e.g. 'IMF', 'ECB', 'BIS', 'OECD', 'WB'
    dataset_code    TEXT NOT NULL,        -- e.g. 'IFS', 'WEO'
    series_code     TEXT NOT NULL,        -- DBnomics canonical series id
    period          TEXT NOT NULL,        -- ISO yyyy[-mm[-dd]]
    value           DOUBLE PRECISION,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_payload     JSONB NOT NULL,
    UNIQUE (provider_code, dataset_code, series_code, period)
);
CREATE INDEX IF NOT EXISTS idx_dbnomics_obs_series ON dbnomics_observations (provider_code, dataset_code, series_code);
CREATE INDEX IF NOT EXISTS idx_dbnomics_obs_period ON dbnomics_observations (period);

CREATE TABLE IF NOT EXISTS polymarket_market_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL,        -- Polymarket condition_id or market slug
    question        TEXT NOT NULL,
    end_date        TIMESTAMPTZ,
    yes_price       DOUBLE PRECISION,
    no_price        DOUBLE PRECISION,
    volume_24h_usd  DOUBLE PRECISION,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_polymarket_snap_market ON polymarket_market_snapshots (market_id, fetched_at DESC);
```

- [ ] **F.2: Apply the migration**

Run: `cd /root/openclaw && npm run db:migrate`
Expected: migration 094 reported applied. Verify: `psql -h localhost -U openclaw -d openclaw -c "\dt dbnomics_observations polymarket_market_snapshots"`.

- [ ] **F.3: Write failing DBnomics client tests**

Create `/root/openclaw/tests/test_dbnomics_client.py`:

```python
"""Phase 1F — DBnomics client tests with recorded fixtures (no network)."""
from unittest.mock import patch
import json


def _fixture():
    """Minimal DBnomics v22 series response shape."""
    return {
        "series": {
            "docs": [{
                "provider_code": "IMF",
                "dataset_code": "IFS",
                "series_code": "M.US.PCPI_PC_PP_PT",
                "period": ["2026-01", "2026-02", "2026-03"],
                "value": [3.1, 3.2, 3.0],
            }],
            "num_found": 1,
        }
    }


def test_get_series_parses_observations():
    from src.ingestion.dbnomics_client import DBnomicsClient
    payload = json.dumps(_fixture()).encode()
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        c = DBnomicsClient()
        obs = c.get_series("IMF/IFS/M.US.PCPI_PC_PP_PT")
    assert len(obs) == 3
    assert obs[0]["period"] == "2026-01"
    assert obs[0]["value"] == 3.1
    assert obs[0]["series_code"] == "M.US.PCPI_PC_PP_PT"


def test_get_series_handles_null_values():
    fx = _fixture()
    fx["series"]["docs"][0]["value"] = [3.1, None, 3.0]
    payload = json.dumps(fx).encode()
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        from src.ingestion.dbnomics_client import DBnomicsClient
        obs = DBnomicsClient().get_series("IMF/IFS/M.US.PCPI_PC_PP_PT")
    assert obs[1]["value"] is None
```

- [ ] **F.4: Run, fail on import**

Run: `cd /root/openclaw && python3 -m pytest tests/test_dbnomics_client.py -v`
Expected: ImportError.

- [ ] **F.5: Implement DBnomics client**

Create `/root/openclaw/src/ingestion/dbnomics_client.py`:

```python
"""Read-only Python client for DBnomics v22 REST API.

DBnomics is a free aggregator of IMF / ECB / BIS / OECD / World Bank macro series.
Public, no API key needed.  Net-new source vs Polygon+FMP+EDGAR.

Series IDs follow 'PROVIDER_CODE/DATASET_CODE/SERIES_CODE'.
Example: 'IMF/IFS/M.US.PCPI_PC_PP_PT' (US monthly CPI YoY %)."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Optional


class DBnomicsClient:
    BASE = "https://api.db.nomics.world/v22"

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def get_series(self, series_id: str, observations: bool = True) -> list[dict]:
        """Returns a list of observation dicts:
        [{provider_code, dataset_code, series_code, period, value}, ...]"""
        qs = urllib.parse.urlencode({
            "series_ids": series_id,
            "observations": "1" if observations else "0",
        })
        req = urllib.request.Request(
            f"{self.BASE}/series?{qs}",
            headers={"User-Agent": "OpenClaw-FundJohn/1.0 (+research)"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            assert r.status == 200, f"DBnomics status {r.status}"
            payload = json.loads(r.read())

        out = []
        for doc in payload.get("series", {}).get("docs", []):
            for period, value in zip(doc.get("period", []), doc.get("value", [])):
                out.append({
                    "provider_code": doc["provider_code"],
                    "dataset_code":  doc["dataset_code"],
                    "series_code":   doc["series_code"],
                    "period":        period,
                    "value":         value,
                })
        return out
```

- [ ] **F.6: Run DBnomics tests**

Run: `cd /root/openclaw && python3 -m pytest tests/test_dbnomics_client.py -v`
Expected: 2 PASS.

- [ ] **F.7: Live smoke against DBnomics**

Run: `cd /root/openclaw && python3 -c "from src.ingestion.dbnomics_client import DBnomicsClient; obs = DBnomicsClient().get_series('IMF/IFS/M.US.PCPI_PC_PP_PT'); print(len(obs), 'obs;', obs[-1] if obs else 'EMPTY')"`
Expected: prints non-zero count and a recent observation. If 0, the series ID format changed upstream — check api.db.nomics.world docs and update the example.

- [ ] **F.8: Write failing Polymarket tests**

Create `/root/openclaw/tests/test_polymarket_client.py`:

```python
"""Phase 1F — Polymarket client tests with recorded fixtures (no network)."""
from unittest.mock import patch
import json


def _markets_fixture():
    """Minimal Polymarket Gamma API markets response shape."""
    return [{
        "id": "0xabc123",
        "question": "Will the Fed cut rates by July 2026?",
        "endDate": "2026-07-31T23:59:59Z",
        "outcomePrices": ["0.62", "0.38"],
        "volume24hr": 145200.50,
    }]


def test_list_active_markets_parses_outcomes():
    from src.ingestion.polymarket_client import PolymarketClient
    payload = json.dumps(_markets_fixture()).encode()
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        markets = PolymarketClient().list_active_markets(limit=10)
    assert len(markets) == 1
    m = markets[0]
    assert m["market_id"] == "0xabc123"
    assert m["yes_price"] == 0.62
    assert m["no_price"] == 0.38
    assert m["volume_24h_usd"] == 145200.50
```

- [ ] **F.9: Run, fail on import**

Run: `cd /root/openclaw && python3 -m pytest tests/test_polymarket_client.py -v`
Expected: ImportError.

- [ ] **F.10: Implement Polymarket client**

Create `/root/openclaw/src/ingestion/polymarket_client.py`:

```python
"""Read-only Python client for Polymarket Gamma API (no auth required).

Used for prediction-market alt-data: macro-event probabilities, Fed/elections,
geopolitical binary outcomes.  Hands raw snapshots to the spike table for
MasterMind to evaluate as features."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request


class PolymarketClient:
    GAMMA_BASE = "https://gamma-api.polymarket.com"

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def list_active_markets(self, limit: int = 50) -> list[dict]:
        """Returns active markets with normalized outcome prices."""
        qs = urllib.parse.urlencode({"active": "true", "closed": "false", "limit": limit})
        req = urllib.request.Request(
            f"{self.GAMMA_BASE}/markets?{qs}",
            headers={"User-Agent": "OpenClaw-FundJohn/1.0 (+research)"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            assert r.status == 200, f"Polymarket status {r.status}"
            raw = json.loads(r.read())

        out = []
        for m in raw:
            prices = m.get("outcomePrices") or ["0", "0"]
            try:
                yes = float(prices[0]); no = float(prices[1])
            except (ValueError, IndexError):
                yes = no = None
            out.append({
                "market_id":      m.get("id"),
                "question":       m.get("question"),
                "end_date":       m.get("endDate"),
                "yes_price":      yes,
                "no_price":       no,
                "volume_24h_usd": m.get("volume24hr"),
                "raw":            m,
            })
        return out
```

- [ ] **F.11: Run Polymarket tests + live smoke**

```bash
cd /root/openclaw && python3 -m pytest tests/test_polymarket_client.py -v
cd /root/openclaw && python3 -c "from src.ingestion.polymarket_client import PolymarketClient; ms = PolymarketClient().list_active_markets(limit=3); print(len(ms), 'markets'); [print(m['question'], '→', m['yes_price']) for m in ms]"
```

Expected: tests pass; live call prints 3 active markets with non-null yes_prices.

- [ ] **F.12: Commit**

```bash
cd /root/openclaw
git add src/ingestion/dbnomics_client.py src/ingestion/polymarket_client.py \
        tests/test_dbnomics_client.py tests/test_polymarket_client.py \
        src/database/migrations/094_dbnomics_polymarket_spike.sql
git commit -m "feat(ingestion): spike DBnomics + Polymarket read-only clients

DBnomics gives free IMF/ECB/BIS/OECD/WB macro series — net-new vs Polygon+FMP+
EDGAR.  Polymarket gives prediction-market prices as macro/event-risk features.

Both clients are read-only, no auth, no production wiring.  Spike tables
(migration 094) capture raw payloads for MasterMind to experiment with
before any schema commitment.

Phase 1F of the Fincept-imports master plan."
```

---

## Project 1G — PyPortfolioOpt Shadow Alt-Sizer

**Why last:** Most complex. Depends on 1B (oracle tests) for confidence and 1E (dashboard grouping) for visualization. Reads the same handoff TradeJohn reads, computes HRP + Black-Litterman recommendations, persists + Discord-posts a comparison, but does NOT route to broker. Default-OFF mirrors `OPENCLAW_ALPACA_LIVE_REPLACE` pattern.

**Files:**
- Create: `src/execution/pyportfolioopt_shadow_sizer.py`
- Create: `src/database/migrations/095_pyportfolioopt_shadow.sql`
- Create: `tests/test_pyportfolioopt_shadow_sizer.py`
- Create: `scripts/run_pyportfolioopt_shadow.py`

### Tasks

- [ ] **G.1: Install PyPortfolioOpt**

Run: `pip install pyportfolioopt`
Expected: success (depends on cvxpy — large install, may take 2-3 min). Note version.

- [ ] **G.2: Identify TradeJohn's handoff input format**

Run: `head -80 /root/openclaw/src/execution/trade_handoff_builder.py`
Note exactly what `handoff` dict / file looks like (signal list shape, equity, positions). The shadow sizer must consume the same input — do not duplicate that contract here, reference it.

- [ ] **G.3: Write the migration**

Create `/root/openclaw/src/database/migrations/095_pyportfolioopt_shadow.sql`:

```sql
-- Phase 1G — PyPortfolioOpt shadow-sizer run capture.
-- Append-only.  Compared offline to regime_blended_sizer_live decisions.

CREATE TABLE IF NOT EXISTS pyportfolioopt_shadow_runs (
    id                  BIGSERIAL PRIMARY KEY,
    run_date            DATE NOT NULL,
    method              TEXT NOT NULL,        -- 'hrp' | 'black_litterman' | 'efficient_cvar'
    handoff_signals_n   INT NOT NULL,
    equity_usd          DOUBLE PRECISION NOT NULL,
    weights             JSONB NOT NULL,       -- {ticker: weight} after the optimizer
    target_dollars      JSONB NOT NULL,       -- {ticker: equity * weight}
    live_dollars        JSONB NOT NULL,       -- what regime_blended_sizer_live would do (for diff)
    diff_dollars        JSONB NOT NULL,       -- target - live, per ticker
    diversification_ratio DOUBLE PRECISION,
    expected_vol_pct    DOUBLE PRECISION,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_date, method)
);
CREATE INDEX IF NOT EXISTS idx_ppo_shadow_run_date ON pyportfolioopt_shadow_runs (run_date DESC);
```

Run: `cd /root/openclaw && npm run db:migrate`
Expected: 095 applied.

- [ ] **G.4: Write failing shadow-sizer tests**

Create `/root/openclaw/tests/test_pyportfolioopt_shadow_sizer.py`:

```python
"""Phase 1G — shadow-sizer tests.  Pure-function compute path; no DB.

We test only the in-memory compute (allocate → diff vs live).  DB persistence
is exercised by the script-level smoke test, not unit tests."""
import pandas as pd
import numpy as np


def _synthetic_returns(n_days=252, n_assets=5, seed=7):
    rng = np.random.default_rng(seed)
    cols = [f"S{i}" for i in range(n_assets)]
    return pd.DataFrame(
        rng.normal(loc=0.0005, scale=0.012, size=(n_days, n_assets)),
        columns=cols,
    )


def test_hrp_weights_are_nonneg_and_sum_to_one():
    from src.execution.pyportfolioopt_shadow_sizer import allocate_hrp
    weights = allocate_hrp(_synthetic_returns())
    assert all(w >= -1e-9 for w in weights.values()), weights
    assert abs(sum(weights.values()) - 1.0) < 1e-6


def test_hrp_is_deterministic_for_fixed_inputs():
    from src.execution.pyportfolioopt_shadow_sizer import allocate_hrp
    a = allocate_hrp(_synthetic_returns(seed=42))
    b = allocate_hrp(_synthetic_returns(seed=42))
    for k in a:
        assert abs(a[k] - b[k]) < 1e-12


def test_diff_vs_live_returns_per_ticker_delta():
    from src.execution.pyportfolioopt_shadow_sizer import compute_diff
    target  = {"AAPL": 5000, "MSFT": 3000, "NVDA": 2000}
    live    = {"AAPL": 4000, "MSFT": 4000, "GOOG": 1000}
    diff = compute_diff(target, live)
    assert diff["AAPL"] == 1000
    assert diff["MSFT"] == -1000
    assert diff["NVDA"] == 2000   # live didn't hold; full target is the diff
    assert diff["GOOG"] == -1000  # target dropped; full live is the negative diff
```

- [ ] **G.5: Run, fail on import**

Run: `cd /root/openclaw && python3 -m pytest tests/test_pyportfolioopt_shadow_sizer.py -v`
Expected: ImportError.

- [ ] **G.6: Implement the shadow sizer**

Create `/root/openclaw/src/execution/pyportfolioopt_shadow_sizer.py`:

```python
"""PyPortfolioOpt shadow alt-sizer (Phase 1G).

Reads the same handoff regime_blended_sizer_live consumes; computes HRP
allocation; persists to pyportfolioopt_shadow_runs; never routes to broker.

Default OFF.  Enable by running scripts/run_pyportfolioopt_shadow.py
explicitly OR setting OPENCLAW_PYPORTFOLIOOPT_SHADOW=1 to wire it into the
daily pipeline as a step *after* the live sizer."""
from __future__ import annotations

import os
import pandas as pd
from pypfopt import HRPOpt


def allocate_hrp(returns: pd.DataFrame) -> dict[str, float]:
    """Hierarchical Risk Parity weights summing to 1."""
    hrp = HRPOpt(returns)
    return dict(hrp.optimize())


def compute_diff(target_dollars: dict[str, float], live_dollars: dict[str, float]) -> dict[str, float]:
    """Per-ticker $-difference (target - live).  Tickers in either side appear in output."""
    keys = set(target_dollars) | set(live_dollars)
    return {k: target_dollars.get(k, 0.0) - live_dollars.get(k, 0.0) for k in keys}


def shadow_run(
    handoff: dict,
    returns: pd.DataFrame,
    live_dollars: dict[str, float],
) -> dict:
    """Compute one shadow allocation.

    handoff: parsed handoff JSON from TradeJohn (must carry .equity_usd and signal list)
    returns: aligned daily-returns DataFrame for the candidate universe
    live_dollars: what regime_blended_sizer_live decided for the same handoff

    Returns a dict ready to upsert into pyportfolioopt_shadow_runs."""
    equity = float(handoff["equity_usd"])
    weights = allocate_hrp(returns)
    target_dollars = {tkr: equity * w for tkr, w in weights.items()}
    diff = compute_diff(target_dollars, live_dollars)

    # Diversification ratio: weighted-avg vol / portfolio vol
    asset_vols = returns.std() * (252 ** 0.5)
    w_series = pd.Series(weights).reindex(returns.columns).fillna(0.0)
    port_vol = float(((w_series @ returns.cov() @ w_series.T) * 252) ** 0.5)
    div_ratio = float((w_series * asset_vols).sum() / port_vol) if port_vol > 0 else None

    return {
        "method":                "hrp",
        "handoff_signals_n":     len(handoff.get("signals", [])),
        "equity_usd":            equity,
        "weights":               weights,
        "target_dollars":        target_dollars,
        "live_dollars":          live_dollars,
        "diff_dollars":          diff,
        "diversification_ratio": div_ratio,
        "expected_vol_pct":      port_vol * 100,
    }


def is_enabled() -> bool:
    return os.environ.get("OPENCLAW_PYPORTFOLIOOPT_SHADOW") == "1"
```

- [ ] **G.7: Run shadow-sizer unit tests**

Run: `cd /root/openclaw && python3 -m pytest tests/test_pyportfolioopt_shadow_sizer.py -v`
Expected: 3 PASS.

- [ ] **G.8: Write the script entry point**

Create `/root/openclaw/scripts/run_pyportfolioopt_shadow.py`:

```python
"""Phase 1G — daily shadow-sizer entry.

Runs after the live trade step.  Reads today's handoff, today's live sizer
decisions, and the prior 252-day returns matrix for the candidate universe.
Persists to pyportfolioopt_shadow_runs, posts a one-liner Discord summary."""
from __future__ import annotations

import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.execution.pyportfolioopt_shadow_sizer import shadow_run


def main():
    today = date.today().isoformat()

    # Handoff path mirrors what trade_agent_llm.py reads — adjust if your
    # build writes it elsewhere.  Hard fail if the handoff is missing.
    handoff_path = os.path.join(ROOT, "output", "handoffs", f"{today}.json")
    with open(handoff_path) as f:
        handoff = json.load(f)

    # Pull live sizer decisions from the most recent regime_blended_sizer_live row
    # (table from migration 069).  Adapt the query if your column names differ.
    import psycopg2
    conn = psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        dbname=os.environ.get("PGDATABASE", "openclaw"),
        user=os.environ.get("PGUSER", "openclaw"),
        password=os.environ.get("PGPASSWORD", ""),
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, dollars FROM regime_blended_sizer_live "
        "WHERE run_date = %s",
        (today,),
    )
    live_dollars = {r[0]: float(r[1]) for r in cur.fetchall()}
    if not live_dollars:
        print(f"No live sizer rows for {today}; skipping shadow run.", file=sys.stderr)
        return 0

    # Returns matrix: candidate universe = union of handoff tickers + live tickers
    universe = sorted({s["ticker"] for s in handoff.get("signals", [])} | set(live_dollars))
    cur.execute(
        "SELECT ticker, ts::date AS d, close FROM prices_daily "
        "WHERE ticker = ANY(%s) AND ts::date >= (CURRENT_DATE - INTERVAL '400 days') "
        "ORDER BY ticker, d",
        (universe,),
    )
    import pandas as pd
    rows = cur.fetchall()
    if not rows:
        print(f"No price history for universe; skipping.", file=sys.stderr)
        return 0
    df = pd.DataFrame(rows, columns=["ticker", "d", "close"])
    wide = df.pivot(index="d", columns="ticker", values="close").sort_index()
    returns = wide.pct_change().dropna(how="all").iloc[-252:]
    returns = returns.dropna(axis=1, thresh=int(0.9 * len(returns)))

    result = shadow_run(handoff, returns, live_dollars)

    cur.execute(
        """INSERT INTO pyportfolioopt_shadow_runs
           (run_date, method, handoff_signals_n, equity_usd, weights,
            target_dollars, live_dollars, diff_dollars,
            diversification_ratio, expected_vol_pct)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (run_date, method) DO UPDATE SET
             weights=EXCLUDED.weights,
             target_dollars=EXCLUDED.target_dollars,
             live_dollars=EXCLUDED.live_dollars,
             diff_dollars=EXCLUDED.diff_dollars,
             diversification_ratio=EXCLUDED.diversification_ratio,
             expected_vol_pct=EXCLUDED.expected_vol_pct,
             created_at=NOW()""",
        (today, result["method"], result["handoff_signals_n"], result["equity_usd"],
         json.dumps(result["weights"]), json.dumps(result["target_dollars"]),
         json.dumps(result["live_dollars"]), json.dumps(result["diff_dollars"]),
         result["diversification_ratio"], result["expected_vol_pct"]),
    )
    conn.commit()

    # Top-3 absolute diffs for the Discord one-liner
    diffs = sorted(result["diff_dollars"].items(), key=lambda kv: abs(kv[1]), reverse=True)[:3]
    msg = (
        f"[PyPortfolioOpt-shadow] {today} HRP allocation; "
        f"div_ratio={result['diversification_ratio']:.2f}, "
        f"expected_vol={result['expected_vol_pct']:.1f}%; "
        f"top diffs vs live: " + ", ".join(f"{t} ${d:+,.0f}" for t, d in diffs)
    )
    print(msg)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **G.9: Smoke-run with the most recent handoff (no DB write — dry mode)**

Pick the most recent handoff file:
```bash
ls -t /root/openclaw/output/handoffs/*.json | head -1
```

Run the script with last-trading-day's date faked in:
```bash
cd /root/openclaw && python3 scripts/run_pyportfolioopt_shadow.py 2>&1 | tail -10
```

Expected: prints either the `[PyPortfolioOpt-shadow]` summary or a clear "skipping" reason. If it fails on a specific column name (e.g., `regime_blended_sizer_live` table schema differs), patch the query in G.8 and re-run. **Do not move on until one full run prints the summary line.**

- [ ] **G.10: Verify the row landed in Postgres**

```bash
psql -h localhost -U openclaw -d openclaw -c "SELECT run_date, method, handoff_signals_n, diversification_ratio, expected_vol_pct FROM pyportfolioopt_shadow_runs ORDER BY run_date DESC LIMIT 1;"
```

Expected: one row from today.

- [ ] **G.11: Wire the script into the daily pipeline (default-OFF)**

Read `/root/openclaw/src/execution/pipeline_orchestrator.py` to find the steps list:

```bash
grep -n "steps" /root/openclaw/src/execution/pipeline_orchestrator.py | head -10
```

Append a new step *after* `'report'` that runs only when `OPENCLAW_PYPORTFOLIOOPT_SHADOW=1`:

```python
# Phase 1G — default-OFF shadow alt-sizer.  Compares HRP to regime_blended_sizer_live;
# never routes to broker.  Toggle: OPENCLAW_PYPORTFOLIOOPT_SHADOW=1
('pyportfolioopt_shadow', 'scripts/run_pyportfolioopt_shadow.py',
 lambda: os.environ.get('OPENCLAW_PYPORTFOLIOOPT_SHADOW') == '1'),
```

Match the exact tuple shape used by adjacent steps — if the orchestrator currently uses `(name, path)` 2-tuples and dispatches all steps unconditionally, instead add the gate inline at the top of `run_pyportfolioopt_shadow.py`'s `main()`:

```python
if os.environ.get("OPENCLAW_PYPORTFOLIOOPT_SHADOW") != "1":
    print("OPENCLAW_PYPORTFOLIOOPT_SHADOW not set; skipping.", file=sys.stderr)
    return 0
```

Document the chosen wiring in the commit.

- [ ] **G.12: Commit**

```bash
cd /root/openclaw
git add src/execution/pyportfolioopt_shadow_sizer.py scripts/run_pyportfolioopt_shadow.py \
        src/database/migrations/095_pyportfolioopt_shadow.sql \
        tests/test_pyportfolioopt_shadow_sizer.py \
        src/execution/pipeline_orchestrator.py
git commit -m "feat(execution): PyPortfolioOpt HRP shadow alt-sizer

Reads the same daily handoff as trade_agent_llm.py, computes Hierarchical
Risk Parity weights, persists target vs live diff to migration 095, and
posts a one-liner Discord summary.

Default OFF.  Gated by OPENCLAW_PYPORTFOLIOOPT_SHADOW=1, mirroring the
OPENCLAW_ALPACA_LIVE_REPLACE rollout pattern.  Will graduate to a Phase 3
live alternative iff 4 weeks of shadow data show better risk-adjusted
return than regime_blended_sizer_live.

Phase 1G of the Fincept-imports master plan."
```

---

## Phase 1 — Closure Tasks

- [ ] **Z.1: Run the full pytest suite to confirm Phase 1 introduced no regressions**

Run: `cd /root/openclaw && python3 -m pytest tests/ -v --ignore=tests/integration -q 2>&1 | tail -30`
Expected: same green count as pre-Phase-1 baseline + the new Phase-1 tests. Triage any new red.

- [ ] **Z.2: Run a single full daily-pipeline pass in dry-run mode**

Run: `cd /root/openclaw && PIPELINE_DRY_RUN=1 OPENCLAW_PYPORTFOLIOOPT_SHADOW=1 python3 scripts/run_pipeline.py 2>&1 | tail -40`
Expected: all 7 (or 8 with the new shadow step) steps complete; the shadow step prints its summary line. No broker side-effects (dry-run is gated upstream).

- [ ] **Z.3: Update `/root/openclaw/CLAUDE.md` with a Phase-1-completed line under "Recent Changes" or the closest equivalent section**

Add one line: `2026-05-15: Phase 1 of fincept-imports master plan complete — arXiv expansion, backtest oracles, FinanceToolkit/AlphaPurify/FinBERT/PyPortfolioOpt installed, DBnomics+Polymarket spike, dashboard group-by-strategy.`

- [ ] **Z.4: One-paragraph status to `#general` Discord channel summarizing Phase 1 outcomes** (optional but recommended)

---

# PHASE 2 — Architecture Upgrades (sketch)

These projects are **not specified at task level here**; each will get its own plan written via `superpowers:writing-plans` when started. Sketch only.

| Project | One-line goal | Pre-req |
|---|---|---|
| 2A: Renaissance IC approval gate | Insert an explicit IC approval state between `signals` and `handoff` in `pipeline_orchestrator.py`; gate is auto-approve for live-eligible strategies + Discord-prompt for staging-tier signals | Phase 1 closure |
| 2B: DataHub pub/sub schema in Redis | Formalize the `domain:subdomain:id` topic schema with TTL/dedup/per-producer rate-limit; refactor existing scattered Redis publishers to it | Phase 1 closure |
| 2C: Jaccard headline dedup | Insert 24h-window Jaccard dedup before any news enters the corpus path; threshold 0.25 (0.20 if same source-category) | After 1D (FinBERT) |
| 2D: Multi-source quote-monitor fan-out | Refactor Polygon+FMP+Alpaca+Yahoo into a single `Monitor.set_symbols()` orchestrator with 3s ack timeout, partition-by-source pattern from ticker | After 1F (so net-new sources have a place to plug in) |
| 2E: StrategyCoder code-gen template | Adopt the `Strategy.init/next` contract + `commission=callable` hook as the canonical template StrategyCoderJohn emits, with our own clean-room base class (no AGPL dep) | After 1B (oracles validate any new path) |

# PHASE 3 — Productionize Spikes (sketch)

| Project | Trigger |
|---|---|
| 3A: Promote PyPortfolioOpt sizer to live alt | 4 weeks of `pyportfolioopt_shadow_runs` showing better Sharpe than the live sizer on the same equity / signal stream |
| 3B: DBnomics → production macro feature columns | MasterMind identifies ≥ 3 series whose lag-1 changes correlate with regime transitions |
| 3C: Polymarket → production event-risk feature columns | Same pattern — at least 3 markets with material correlation to next-day index moves |
| 3D: pyxirr + quantstats wired into backtest output | Quantstats tearsheet attached to every `unified_backtest` run; pyxirr replaces XIRR computations in any perf-attribution path |

# PHASE 4 — Net-New Sub-Bots from Fincept Personas (sketch)

User-approved subset of: SentimentJohn (consumes FinBERT + Polymarket), MacroCycleJohn (consumes DBnomics), CentralBankJohn (specialized news-ingest filter). Each gets a new entry in `src/agent/config/models.js`, a prompt in `src/agent/prompts/subagents/`, and a registration in `src/agent/subagents/swarm.js`.

---

# Self-Review

**Spec coverage:** All 7 user-confirmed Phase 1 items have a dedicated project (1A–1G). All pre-Phase-1 follow-up items from the original synthesis are mapped to Phases 2–4. ✓

**Placeholder scan:** All "TBD/TODO/fill in" patterns checked — only the version-string placeholders `<X.Y.Z>` in C.11's commit message remain, which is intentional (engineer fills from C.1 output). ✓

**Type consistency:**
- `groupByStrategy` in 1E is named consistently between test and impl. ✓
- `run_single_bracket` signature `(prices, entry, stop, target, qty)` matches in 1B's adapter and shim. ✓
- `shadow_run` / `allocate_hrp` / `compute_diff` consistent across 1G's test, impl, and script. ✓
- Database table names match between migration and consumer: `pyportfolioopt_shadow_runs` (095), `dbnomics_observations` + `polymarket_market_snapshots` (094). ✓

**Known engineer-judgment branch points** (not placeholders — explicit decisions the executor must make):
- **B.4–B.5:** Path 1 vs Path 2 for the test adapter. Plan biases Path 1; commit message captures the choice.
- **E.1:** The strategy-id source column may need a JOIN against `trades` if `positions` lacks attribution. The grep step surfaces this; E.7 catches it.
- **G.8/G.11:** SQL column names may differ from `regime_blended_sizer_live` and `prices_daily` in your build. The smoke step (G.9) forces a fix before the row write.

These are real branches because the codebase has evolved; the plan tells the engineer exactly where to look and what error to expect.

---

**Plan complete.**
