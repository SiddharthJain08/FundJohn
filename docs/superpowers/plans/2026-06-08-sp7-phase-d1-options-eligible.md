# SP-7 Phase D1 — options_eligible producer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a weekly producer that enumerates optionable underlyings from Alpaca and writes `data/.cache/options_eligibility.json` ({symbol: True}), which the daily metadata writer already reads — making `options_eligible` truthful and un-blocking the options-archive gate.

**Architecture:** A standalone module `src/pipeline/options_eligibility.py` pages the Alpaca `option contracts --status active` reference endpoint, collects the distinct set of `underlying_symbol`, and **full-replaces** the cache with the eligible subset of our active-equity universe — but only when the sweep ran to completion AND the result clears a sanity floor (else it keeps last week's cache). A weekly system systemd timer (shipped disabled) drives it. A freshness `system_check` and a best-effort Discord summary provide observability.

**Tech Stack:** Python 3, `subprocess` → Alpaca CLI (`/root/go/bin/alpaca`), `psycopg2`, pytest. Mirrors `src/pipeline/backfillers/alpaca_options.py`.

**Spec:** `docs/superpowers/specs/2026-06-08-sp7-phase-d1-options-eligible-producer-design.md`

**Branch/worktree:** `feat/sp7-phase-d1-options-eligible` (off live tip `eac2cf9`, Phase C merged, gates OFF).

---

## File Structure

- **Create** `src/pipeline/options_eligibility.py` — the producer (enumerate / build / gate / write / main). One responsibility: produce the eligibility cache.
- **Create** `tests/test_options_eligibility.py` — unit tests (mocked subprocess/DB; no live API).
- **Create** `src/system_checks/checks/options_eligibility_freshness.py` — freshness probe.
- **Modify** `src/system_checks/checks/__init__.py` — register the new check (one import line).
- **Create** `docs/openclaw-options-eligibility.service` + `docs/openclaw-options-eligibility.timer` — system systemd units (mirror `openclaw-options-archive.*`).

**Module-level API** (defined in Task 1–6, referenced consistently):
`_record_call(success, error=None)` · `_fetch_contracts_page(page_token=None, limit=PAGE_LIMIT) -> dict` · `_parse_underlyings(page) -> set[str]` · `enumerate_optionable_underlyings(fetch_page=_fetch_contracts_page, budget_s=SOFT_BUDGET_S, clock=time.time) -> (set[str], bool, int)` · `_load_universe() -> set[str]` · `_load_prior_cache(path=CACHE_PATH) -> dict` · `_atomic_write_cache(data, path=CACHE_PATH)` · `build_eligibility(optionable, universe) -> dict[str, bool]` · `decide_write(new, prior, completed, abs_floor=ABS_FLOOR) -> (bool, str)` · `_format_summary(stats) -> str` · `_post_summary(text, webhook_url=WEBHOOK_URL)` · `main(argv=None) -> int`

---

## Task 1: Module skeleton + page parse + sweep enumeration

**Files:**
- Create: `src/pipeline/options_eligibility.py`
- Test: `tests/test_options_eligibility.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_options_eligibility.py
from src.pipeline import options_eligibility as oe


def test_parse_underlyings_extracts_distinct():
    page = {'option_contracts': [
        {'underlying_symbol': 'AAPL', 'symbol': 'AAPL260101C1'},
        {'underlying_symbol': 'AAPL', 'symbol': 'AAPL260101P1'},
        {'underlying_symbol': 'MSFT', 'symbol': 'MSFT260101C1'},
        {'symbol': 'NOUNDERLYING'},          # missing field → skipped
    ]}
    assert oe._parse_underlyings(page) == {'AAPL', 'MSFT'}


def test_parse_underlyings_empty_page():
    assert oe._parse_underlyings({'option_contracts': []}) == set()
    assert oe._parse_underlyings({}) == set()


def _pager(pages):
    """Return a fetch_page(token) that walks a list of page dicts in order."""
    seq = iter(pages)
    def fetch(_token):
        return next(seq)
    return fetch


def test_enumerate_paginates_to_terminal():
    pages = [
        {'option_contracts': [{'underlying_symbol': 'AA'}], 'next_page_token': 't1'},
        {'option_contracts': [{'underlying_symbol': 'AAPL'}], 'next_page_token': 't2'},
        {'option_contracts': [{'underlying_symbol': 'MSFT'}], 'next_page_token': None},
    ]
    optionable, completed, n = oe.enumerate_optionable_underlyings(fetch_page=_pager(pages))
    assert optionable == {'AA', 'AAPL', 'MSFT'}
    assert completed is True
    assert n == 3


def test_enumerate_incomplete_on_page_error():
    def fetch(_token):
        raise RuntimeError('boom')
    optionable, completed, n = oe.enumerate_optionable_underlyings(fetch_page=fetch)
    assert completed is False
    assert optionable == set()


def test_enumerate_incomplete_on_budget():
    pages = [{'option_contracts': [{'underlying_symbol': 'AA'}], 'next_page_token': 't1'}] * 5
    # clock jumps past the deadline immediately on the first budget check
    ticks = iter([0, 1000, 1001, 1002, 1003, 1004])
    optionable, completed, n = oe.enumerate_optionable_underlyings(
        fetch_page=_pager(pages), budget_s=10, clock=lambda: next(ticks))
    assert completed is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/openclaw/.claude/worktrees/sp7-phase-d1-options-eligible && python3 -m pytest tests/test_options_eligibility.py -q`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError` (module + functions not defined).

- [ ] **Step 3: Write the module skeleton + the two functions**

```python
# src/pipeline/options_eligibility.py
"""SP-7 Phase D1: options_eligible producer.

Weekly bulk enumeration of optionable underlyings via the Alpaca options
*contracts* reference endpoint (`option contracts --status active`). Writes
data/.cache/options_eligibility.json ({symbol: True} for eligible names),
which src/pipeline/run_ticker_metadata_step.py reads to set the
ticker_metadata_snapshots.options_eligible column.

Safety: a COMPLETE + plausibly-sized sweep full-replaces the cache; any
failure (partial sweep, API outage, degenerate result) keeps the prior
cache, so eligibility is never silently wiped. Inert to land — no live
strategy reads an options predicate and the resolver/archive gates are OFF.

Spec: docs/superpowers/specs/2026-06-08-sp7-phase-d1-options-eligible-producer-design.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

ALPACA_BIN = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')
CACHE_PATH = Path(os.environ.get(
    'OPTIONS_ELIGIBILITY_CACHE',
    '/root/openclaw/data/.cache/options_eligibility.json'))
PAGE_LIMIT = int(os.environ.get('OPTIONS_ELIGIBILITY_PAGE_LIMIT', '10000'))
SOFT_BUDGET_S = int(os.environ.get('OPTIONS_ELIGIBILITY_BUDGET_S', '1800'))
ABS_FLOOR = int(os.environ.get('OPTIONS_ELIGIBILITY_MIN_FLOOR', '1000'))
WEBHOOK_URL = os.environ.get('OPENCLAW_OPTIONS_ELIGIBILITY_WEBHOOK', '')


def _record_call(success: bool, error: str | None = None) -> None:
    """Best-effort provider_health hook (mirrors backfillers/alpaca_options.py)."""
    try:
        from src.maintenance.provider_health import record
        record('alpaca', 'options_contracts', success=success, error=error)
    except Exception:
        pass


def _fetch_contracts_page(page_token: str | None = None, limit: int = PAGE_LIMIT) -> dict:
    """One page of `alpaca option contracts --status active`. Raises on failure."""
    args = [ALPACA_BIN, 'option', 'contracts', '--status', 'active',
            '--limit', str(limit)]
    if page_token:
        args.extend(['--page-token', page_token])
    res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        _record_call(False, f'rc={res.returncode}: {res.stderr.strip()[:160]}')
        raise RuntimeError(f'alpaca option contracts rc={res.returncode}: {res.stderr.strip()}')
    _record_call(True)
    return json.loads(res.stdout)


def _parse_underlyings(page: dict) -> set[str]:
    return {c.get('underlying_symbol')
            for c in (page.get('option_contracts') or [])
            if c.get('underlying_symbol')}


def enumerate_optionable_underlyings(fetch_page=_fetch_contracts_page,
                                     budget_s: int = SOFT_BUDGET_S,
                                     clock=time.time):
    """Page the active option-contracts list; collect distinct underlying_symbol.

    Returns (optionable: set[str], completed: bool, pages: int). completed is
    True ONLY when a page returns no next_page_token (a genuinely terminal
    sweep). Budget exceeded or any page error → completed=False (caller keeps
    the prior cache).
    """
    optionable: set[str] = set()
    token = None
    deadline = clock() + budget_s
    pages = 0
    while True:
        if clock() > deadline:
            log.warning('sweep budget exceeded after %d pages', pages)
            return optionable, False, pages
        try:
            page = fetch_page(token)
        except Exception as e:  # noqa: BLE001 — any error aborts the sweep safely
            log.warning('sweep page %d failed: %s', pages, e)
            return optionable, False, pages
        optionable |= _parse_underlyings(page)
        pages += 1
        token = page.get('next_page_token')
        if not token:
            return optionable, True, pages
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_options_eligibility.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/options_eligibility.py tests/test_options_eligibility.py
git commit -m "feat(sp7-d1): page-parse + sweep enumeration for options_eligible producer"
```

---

## Task 2: Real CLI page fetch (subprocess, mocked in test)

**Files:**
- Modify: `src/pipeline/options_eligibility.py` (already has `_fetch_contracts_page` from Task 1)
- Test: `tests/test_options_eligibility.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_fetch_contracts_page_parses_stdout(monkeypatch):
    class _R:
        returncode = 0
        stdout = '{"option_contracts": [{"underlying_symbol": "AAPL"}], "next_page_token": null}'
        stderr = ''
    monkeypatch.setattr(oe.subprocess, 'run', lambda *a, **k: _R())
    page = oe._fetch_contracts_page()
    assert oe._parse_underlyings(page) == {'AAPL'}


def test_fetch_contracts_page_raises_on_nonzero(monkeypatch):
    class _R:
        returncode = 1
        stdout = ''
        stderr = 'unauthorized'
    monkeypatch.setattr(oe.subprocess, 'run', lambda *a, **k: _R())
    import pytest
    with pytest.raises(RuntimeError):
        oe._fetch_contracts_page()


def test_fetch_contracts_page_builds_pagetoken_args(monkeypatch):
    seen = {}
    class _R:
        returncode = 0
        stdout = '{"option_contracts": []}'
        stderr = ''
    def fake_run(args, **k):
        seen['args'] = args
        return _R()
    monkeypatch.setattr(oe.subprocess, 'run', fake_run)
    oe._fetch_contracts_page(page_token='abc', limit=500)
    assert '--page-token' in seen['args'] and 'abc' in seen['args']
    assert '--status' in seen['args'] and 'active' in seen['args']
    assert '500' in seen['args']
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `python3 -m pytest tests/test_options_eligibility.py -q -k fetch_contracts`
Expected: PASS — `_fetch_contracts_page` already exists (Task 1). These tests **pin** its subprocess contract; if any fail, fix the function to match.

- [ ] **Step 3: (only if a test failed) align `_fetch_contracts_page`**

No code change expected — the Task-1 implementation already satisfies these. If `test_fetch_contracts_page_builds_pagetoken_args` fails, ensure the arg list is exactly `[ALPACA_BIN, 'option', 'contracts', '--status', 'active', '--limit', str(limit)] (+ ['--page-token', token])`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_options_eligibility.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_options_eligibility.py
git commit -m "test(sp7-d1): pin _fetch_contracts_page subprocess contract"
```

---

## Task 3: Cache I/O — load prior + atomic write

**Files:**
- Modify: `src/pipeline/options_eligibility.py`
- Test: `tests/test_options_eligibility.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_load_prior_cache_missing_returns_empty(tmp_path):
    assert oe._load_prior_cache(tmp_path / 'nope.json') == {}


def test_atomic_write_and_reload_roundtrip(tmp_path):
    p = tmp_path / 'sub' / 'cache.json'      # parent dir does not exist yet
    oe._atomic_write_cache({'AAPL': True, 'MSFT': True}, p)
    assert oe._load_prior_cache(p) == {'AAPL': True, 'MSFT': True}
    # no leftover temp file
    assert list(p.parent.glob('*.tmp')) == []


def test_atomic_write_replaces_existing(tmp_path):
    p = tmp_path / 'cache.json'
    oe._atomic_write_cache({'OLD': True}, p)
    oe._atomic_write_cache({'NEW': True}, p)
    assert oe._load_prior_cache(p) == {'NEW': True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_options_eligibility.py -q -k "prior_cache or atomic"`
Expected: FAIL — `_load_prior_cache` / `_atomic_write_cache` not defined.

- [ ] **Step 3: Add the functions**

```python
def _load_universe() -> set[str]:
    """Active US-equity symbols from alpaca_tradable_universe (the metadata
    universe; all 13.8k active rows are us_equity as of 2026-06-08)."""
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
        cur.execute("SELECT symbol FROM alpaca_tradable_universe "
                    "WHERE status='active' AND asset_class='us_equity'")
        return {r[0] for r in cur.fetchall()}


def _load_prior_cache(path=CACHE_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _atomic_write_cache(data: dict, path=CACHE_PATH) -> None:
    """Write via temp file + os.replace so a crash never leaves a partial cache."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f, sort_keys=True)
    os.replace(tmp, path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_options_eligibility.py -q`
Expected: PASS (11 tests). (`_load_universe` is exercised in Task 5/smoke, not unit-tested — it's a thin DB read.)

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/options_eligibility.py tests/test_options_eligibility.py
git commit -m "feat(sp7-d1): universe load + atomic cache I/O"
```

---

## Task 4: Build eligibility + the write-decision gate

**Files:**
- Modify: `src/pipeline/options_eligibility.py`
- Test: `tests/test_options_eligibility.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_build_eligibility_intersects_and_trues_only():
    optionable = {'AAPL', 'MSFT', 'SPX', 'TSLA'}     # SPX not in our universe
    universe = {'AAPL', 'MSFT', 'TSLA', 'KO'}        # KO not optionable
    out = oe.build_eligibility(optionable, universe)
    assert out == {'AAPL': True, 'MSFT': True, 'TSLA': True}   # KO absent, SPX dropped


def test_decide_write_incomplete_never_writes():
    ok, reason = oe.decide_write({'AAPL': True}, {}, completed=False)
    assert ok is False and 'incomplete' in reason


def test_decide_write_first_run_above_abs_floor():
    new = {f'S{i}': True for i in range(1500)}        # 1500 >= 1000
    ok, _ = oe.decide_write(new, {}, completed=True, abs_floor=1000)
    assert ok is True


def test_decide_write_below_abs_floor_keeps_prior():
    new = {f'S{i}': True for i in range(500)}         # 500 < 1000
    ok, reason = oe.decide_write(new, {}, completed=True, abs_floor=1000)
    assert ok is False and 'floor' in reason


def test_decide_write_relative_floor_blocks_implausible_shrink():
    prior = {f'S{i}': True for i in range(5000)}
    new = {f'S{i}': True for i in range(2000)}        # 2000 < 0.5*5000=2500
    ok, reason = oe.decide_write(new, prior, completed=True, abs_floor=1000)
    assert ok is False and 'floor' in reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_options_eligibility.py -q -k "build_eligibility or decide_write"`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Add the functions**

```python
def build_eligibility(optionable: set[str], universe: set[str]) -> dict[str, bool]:
    """Full-replace snapshot: {sym: True} for the optionable subset of our
    universe (absent ⇒ False at read time). A name that lost its listing simply
    drops out of the fresh snapshot."""
    return {sym: True for sym in sorted(optionable & universe)}


def decide_write(new: dict, prior: dict, completed: bool, abs_floor: int = ABS_FLOOR):
    """Return (should_write, reason). Write only on a complete sweep whose
    eligible count clears both the absolute floor and 50% of the prior count."""
    if not completed:
        return False, 'incomplete sweep — prior cache retained'
    n_new = sum(1 for v in new.values() if v)
    prior_n = sum(1 for v in prior.values() if v)
    floor = max(abs_floor, prior_n // 2)
    if n_new < floor:
        return False, f'sanity floor: {n_new} < {floor} (abs={abs_floor}, prior={prior_n})'
    return True, f'ok: {n_new} eligible'
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_options_eligibility.py -q`
Expected: PASS (16 tests).

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/options_eligibility.py tests/test_options_eligibility.py
git commit -m "feat(sp7-d1): eligibility build + completion/sanity-floor write gate"
```

---

## Task 5: Summary formatting + best-effort Discord post

**Files:**
- Modify: `src/pipeline/options_eligibility.py`
- Test: `tests/test_options_eligibility.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_format_summary_contains_counts():
    s = oe._format_summary({'eligible': 4200, 'universe': 13845, 'pages': 132,
                            'added': 10, 'removed': 3, 'secs': 640.0, 'action': 'WROTE'})
    assert '4200' in s and '13845' in s and '132' in s and 'WROTE' in s


def test_post_summary_noop_when_no_url(monkeypatch):
    called = {'n': 0}
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: called.__setitem__('n', called['n'] + 1))
    oe._post_summary('hi', webhook_url='')
    assert called['n'] == 0


def test_post_summary_failopen(monkeypatch):
    import urllib.request
    def boom(*a, **k):
        raise OSError('network down')
    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    oe._post_summary('hi', webhook_url='https://example/wh')   # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_options_eligibility.py -q -k "format_summary or post_summary"`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Add the functions**

```python
def _format_summary(stats: dict) -> str:
    return ('**options_eligible refresh** — '
            f"eligible={stats['eligible']} / universe={stats['universe']} · "
            f"pages={stats['pages']} · +{stats['added']}/-{stats['removed']} · "
            f"{stats['action']} · {stats['secs']:.0f}s")


def _post_summary(text: str, webhook_url: str = WEBHOOK_URL) -> None:
    """Best-effort Discord post. UA header is REQUIRED — Cloudflare 1010-bans the
    default python-urllib UA (reference_discord_urllib_cloudflare_ua)."""
    if not webhook_url or not text:
        return
    import urllib.request
    req = urllib.request.Request(
        webhook_url, data=json.dumps({'content': text}).encode(),
        headers={'Content-Type': 'application/json',
                 'User-Agent': 'OpenClaw-OptionsEligibility/1.0 (+botjohn)'})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001 — never fail the producer over Discord
        log.warning('discord post failed: %s', e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_options_eligibility.py -q`
Expected: PASS (19 tests).

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/options_eligibility.py tests/test_options_eligibility.py
git commit -m "feat(sp7-d1): summary formatting + UA-safe best-effort discord post"
```

---

## Task 6: `main()` orchestration + exit codes

**Files:**
- Modify: `src/pipeline/options_eligibility.py`
- Test: `tests/test_options_eligibility.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_main_writes_cache_on_complete_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(oe, 'CACHE_PATH', tmp_path / 'cache.json')
    monkeypatch.setattr(oe, 'enumerate_optionable_underlyings',
                        lambda **k: ({f'S{i}' for i in range(2000)} | {'AAPL'}, True, 5))
    monkeypatch.setattr(oe, '_load_universe', lambda: {f'S{i}' for i in range(2000)} | {'AAPL', 'KO'})
    rc = oe.main([])
    assert rc == 0
    data = oe._load_prior_cache(tmp_path / 'cache.json')
    assert data.get('AAPL') is True and 'KO' not in data
    assert sum(1 for v in data.values() if v) == 2001


def test_main_keeps_prior_on_incomplete_sweep(tmp_path, monkeypatch):
    p = tmp_path / 'cache.json'
    oe._atomic_write_cache({'PRIOR': True}, p)
    monkeypatch.setattr(oe, 'CACHE_PATH', p)
    monkeypatch.setattr(oe, 'enumerate_optionable_underlyings', lambda **k: (set(), False, 1))
    monkeypatch.setattr(oe, '_load_universe', lambda: {'PRIOR', 'X'})
    rc = oe.main([])
    assert rc == 1
    assert oe._load_prior_cache(p) == {'PRIOR': True}   # untouched


def test_main_keeps_prior_below_floor(tmp_path, monkeypatch):
    p = tmp_path / 'cache.json'
    oe._atomic_write_cache({'PRIOR': True}, p)
    monkeypatch.setattr(oe, 'CACHE_PATH', p)
    monkeypatch.setattr(oe, 'enumerate_optionable_underlyings', lambda **k: ({'AAPL'}, True, 3))
    monkeypatch.setattr(oe, '_load_universe', lambda: {'AAPL', 'PRIOR'})
    rc = oe.main([])
    assert rc == 1
    assert oe._load_prior_cache(p) == {'PRIOR': True}   # below abs_floor → kept
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_options_eligibility.py -q -k "main_"`
Expected: FAIL — `main` not defined.

- [ ] **Step 3: Add `main()` + the `__main__` guard**

```python
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='SP-7 D1 options_eligible producer')
    ap.add_argument('--dry-run', action='store_true',
                    help='write to /tmp and skip the Discord post')
    ap.add_argument('--limit', type=int, default=PAGE_LIMIT)
    ap.add_argument('--budget-s', type=int, default=SOFT_BUDGET_S)
    args = ap.parse_args(argv)

    cache_path = Path('/tmp/options_eligibility_dryrun.json') if args.dry_run else CACHE_PATH
    t0 = time.time()
    optionable, completed, pages = enumerate_optionable_underlyings(
        fetch_page=lambda tok: _fetch_contracts_page(tok, args.limit),
        budget_s=args.budget_s)
    universe = _load_universe()
    prior = _load_prior_cache(cache_path)
    new = build_eligibility(optionable, universe)
    should, reason = decide_write(new, prior, completed)

    prior_keys = {k for k, v in prior.items() if v}
    stats = {
        'eligible': len(new), 'universe': len(universe), 'pages': pages,
        'added': len(set(new) - prior_keys), 'removed': len(prior_keys - set(new)),
        'secs': time.time() - t0,
        'action': 'WROTE' if should else f'KEPT-PRIOR ({reason})',
    }
    if should:
        _atomic_write_cache(new, cache_path)
    summary = _format_summary(stats)
    log.info('options-eligibility %s', summary)
    if not args.dry_run:
        _post_summary(summary)
    return 0 if should else 1


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_options_eligibility.py -q`
Expected: PASS (22 tests).

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/options_eligibility.py tests/test_options_eligibility.py
git commit -m "feat(sp7-d1): main() orchestration with completion/floor exit codes"
```

---

## Task 7: Freshness system_check

**Files:**
- Create: `src/system_checks/checks/options_eligibility_freshness.py`
- Modify: `src/system_checks/checks/__init__.py`
- Test: `tests/test_options_eligibility_freshness_check.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_eligibility_freshness_check.py
import json
from src.system_checks.checks import options_eligibility_freshness as chk
from src.system_checks.types import Status


def test_warn_when_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(chk, '_CACHE', tmp_path / 'nope.json')
    status, _ = chk._options_eligibility_freshness()
    assert status is Status.WARN


def test_warn_when_below_floor(tmp_path, monkeypatch):
    p = tmp_path / 'cache.json'
    p.write_text(json.dumps({'AAPL': True}))
    monkeypatch.setattr(chk, '_CACHE', p)
    monkeypatch.setattr(chk, '_MIN_ELIGIBLE', 1000)
    status, detail = chk._options_eligibility_freshness()
    assert status is Status.WARN and 'eligible' in detail


def test_pass_when_fresh_and_populated(tmp_path, monkeypatch):
    p = tmp_path / 'cache.json'
    p.write_text(json.dumps({f'S{i}': True for i in range(1500)}))
    monkeypatch.setattr(chk, '_CACHE', p)
    monkeypatch.setattr(chk, '_MIN_ELIGIBLE', 1000)
    status, _ = chk._options_eligibility_freshness()
    assert status is Status.PASS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_eligibility_freshness_check.py -q`
Expected: FAIL — check module does not exist.

- [ ] **Step 3: Create the check + register it**

```python
# src/system_checks/checks/options_eligibility_freshness.py
"""Strategies-tagged check: data/.cache/options_eligibility.json is fresh + populated."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..registry import check
from ..types import Status

_CACHE = Path(os.environ.get(
    'OPTIONS_ELIGIBILITY_CACHE',
    '/root/openclaw/data/.cache/options_eligibility.json'))
_MAX_AGE_DAYS = 10
_MIN_ELIGIBLE = int(os.environ.get('OPTIONS_ELIGIBILITY_MIN_FLOOR', '1000'))


@check(name='options_eligibility_freshness', tags=['strategies'], requires=[])
def _options_eligibility_freshness():
    """Advisory: WARN if the eligibility cache is missing / stale / too small.
    Never FAILs (the producer is weekly + gated)."""
    if not _CACHE.exists():
        return Status.WARN, f'cache missing: {_CACHE} (producer not yet run/enabled)'
    try:
        data = json.loads(_CACHE.read_text())
    except Exception as e:  # noqa: BLE001
        return Status.WARN, f'cache unreadable: {e}'
    n = sum(1 for v in data.values() if v)
    age_days = (time.time() - _CACHE.stat().st_mtime) / 86400
    if n < _MIN_ELIGIBLE:
        return Status.WARN, f'only {n} eligible (< {_MIN_ELIGIBLE})'
    if age_days > _MAX_AGE_DAYS:
        return Status.WARN, f'stale {age_days:.0f}d (>{_MAX_AGE_DAYS}d), {n} eligible'
    return Status.PASS, f'{n} eligible, {age_days:.0f}d old'
```

Then append to `src/system_checks/checks/__init__.py` (after the last import line):

```python
from . import options_eligibility_freshness  # noqa: F401
```

- [ ] **Step 4: Run test + the registry to verify**

Run: `python3 -m pytest tests/test_options_eligibility_freshness_check.py -q`
Expected: PASS (3 tests).
Run: `python3 -m system_checks --list 2>/dev/null | grep options_eligibility_freshness`
Expected: the check appears in the registry listing.

- [ ] **Step 5: Commit**

```bash
git add src/system_checks/checks/options_eligibility_freshness.py \
        src/system_checks/checks/__init__.py \
        tests/test_options_eligibility_freshness_check.py
git commit -m "feat(sp7-d1): options_eligibility_freshness system_check"
```

---

## Task 8: Systemd units (shipped disabled)

**Files:**
- Create: `docs/openclaw-options-eligibility.service`
- Create: `docs/openclaw-options-eligibility.timer`

- [ ] **Step 1: Create the service unit** (mirrors `docs/openclaw-options-archive.service`)

```ini
# docs/openclaw-options-eligibility.service
[Unit]
Description=SP-7 D1 options_eligible producer (weekly bulk chain-probe)
After=network-online.target redis.service postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/python3 -m src.pipeline.options_eligibility
StandardOutput=append:/var/log/openclaw-options-eligibility.log
StandardError=append:/var/log/openclaw-options-eligibility.log
# rc=1 means "kept prior" (transient incomplete/floor) — not a hard failure
SuccessExitStatus=0 1
TimeoutStartSec=2400

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create the timer unit** (mirrors `docs/openclaw-options-archive.timer`)

```ini
# docs/openclaw-options-eligibility.timer
[Unit]
Description=Weekly options_eligible refresh — Sat 06:00 UTC

[Timer]
OnCalendar=Sat *-*-* 06:00:00 UTC
Persistent=true
Unit=openclaw-options-eligibility.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Validate the unit syntax (no install)**

Run: `systemd-analyze verify docs/openclaw-options-eligibility.service docs/openclaw-options-eligibility.timer 2>&1 | grep -vi 'Unknown\|EnvironmentFile' || echo "syntax ok"`
Expected: no fatal syntax errors (warnings about the missing EnvironmentFile path in this worktree are fine — the real path exists on the live box).

- [ ] **Step 4: Confirm the OnCalendar expression is valid**

Run: `systemd-analyze calendar 'Sat *-*-* 06:00:00 UTC'`
Expected: prints the next Saturday 06:00 UTC elapse — confirms the schedule parses.

- [ ] **Step 5: Commit**

```bash
git add docs/openclaw-options-eligibility.service docs/openclaw-options-eligibility.timer
git commit -m "feat(sp7-d1): weekly systemd units (shipped disabled, Sat 06:00 UTC)"
```

---

## Task 9: Full-suite regression + manual live smoke

**Files:** none (verification only)

- [ ] **Step 1: Full new-test suite green**

Run: `python3 -m pytest tests/test_options_eligibility.py tests/test_options_eligibility_freshness_check.py -q`
Expected: PASS (25 tests).

- [ ] **Step 2: Targeted regression — nothing else broke**

Run: `python3 -m pytest tests/ -q -k "metadata or ticker_metadata or system_checks or alpaca_options" 2>&1 | tail -20`
Expected: no NEW failures vs the branch baseline (record any pre-existing reds; do not "fix" unrelated pre-existing failures).

- [ ] **Step 3: Live dry-run smoke (manual, needs Alpaca auth in env)**

Run (with `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` exported from `.env`):
`python3 -m src.pipeline.options_eligibility --dry-run 2>&1 | tail -5`
Expected: a summary line with `eligible=<thousands> / universe=~13845 · pages=<~130> · WROTE` and `/tmp/options_eligibility_dryrun.json` populated. Verify: `python3 -c "import json;d=json.load(open('/tmp/options_eligibility_dryrun.json'));print('AAPL',d.get('AAPL'),'count',sum(d.values()))"` → `AAPL True count <thousands>`.

- [ ] **Step 4: End-to-end — metadata writer consumes it (manual)**

Point the real cache at the dry-run output and confirm the writer flips the column:
`OPTIONS_ELIGIBILITY_CACHE=/tmp/options_eligibility_dryrun.json python3 -c "from src.pipeline.run_ticker_metadata_step import load_json, OPTIONS_ELIGIBILITY_CACHE; import os; os.environ.setdefault('OPTIONS_ELIGIBILITY_CACHE','/tmp/options_eligibility_dryrun.json'); c=load_json('/tmp/options_eligibility_dryrun.json'); print('AAPL eligible in cache ->', c.get('AAPL'))"`
Expected: `AAPL eligible in cache -> True`. (Full snapshot write is exercised by the live daily metadata step once the producer runs for real; this confirms the read path.)

- [ ] **Step 5: Commit a short verification note**

```bash
git commit --allow-empty -m "chore(sp7-d1): live dry-run smoke verified (eligible≈N, AAPL True, metadata read path ok)"
```

---

## Self-Review (completed by plan author)

- **Spec coverage:** D1.a existence (Task 1/4 `_parse_underlyings`+`build_eligibility`); D1.b weekly timer (Task 8); D1.c bulk enumeration (Task 1); completion-gate+sanity-floor+atomic write (Tasks 3–4,6); `{sym: bool}` contract (Task 3/4); freshness check (Task 7); Discord summary (Task 5); inert-to-land (no consumer touched — verified in spec §8); testing matrix (Tasks 1–9). All spec sections map to a task.
- **Placeholder scan:** none — every step ships complete code/commands.
- **Type/name consistency:** `enumerate_optionable_underlyings` returns a **3-tuple** `(set, bool, int)` consistently in Tasks 1, 6, and all tests; `decide_write`/`build_eligibility`/`_atomic_write_cache`/`_format_summary` signatures match across tasks; `CACHE_PATH`/`ABS_FLOOR`/`WEBHOOK_URL` module globals referenced consistently; monkeypatch targets (`oe.CACHE_PATH`, `oe.enumerate_optionable_underlyings`, `oe._load_universe`) exist.
- **Out of scope (unchanged):** consumers (predicates/archive/metadata writer), the `OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE` flip, D2–D5.
