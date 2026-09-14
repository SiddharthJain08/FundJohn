"""B2 digest: the fill_slippage: line, and its verdict against OUR OWN model.

The verdict compares realized median entry bp to the per-ticker one-way
half-spread in data/derived/ticker_cost_bps.json (unified_backtest
.load_ticker_cost_bps) — never a foreign asset class's bands. n=0 renders 'n/a'
rather than a zero that reads as "no slippage today".
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import fill_slippage as fs  # noqa: E402

COSTS = {'AAA': 4.0, 'BBB': 6.0, 'CCC': 100.0}

ENTRY = [
    {'ticker': 'AAA', 'bps': 4.0, 'notional_usd': 10_000.0, 'latency_s': 2.0},
    {'ticker': 'AAA', 'bps': 6.0, 'notional_usd': 20_000.0, 'latency_s': 4.0},
    {'ticker': 'BBB', 'bps': 20.0, 'notional_usd': 5_000.0, 'latency_s': 6.0},
]
EXIT = [
    {'ticker': 'AAA', 'bps': 10.0, 'notional_usd': 30_000.0},
    {'ticker': 'BBB', 'bps': -2.0, 'notional_usd': 5_000.0},
]


# ── verdict bands ──────────────────────────────────────────────────────────

def test_verdict_ok_at_or_below_1_5x_modelled():
    assert fs.verdict(7.5, 5.0) == 'OK'
    assert fs.verdict(5.0, 5.0) == 'OK'


def test_verdict_warn_between_1_5x_and_3x():
    assert fs.verdict(7.6, 5.0) == 'WARN'
    assert fs.verdict(15.0, 5.0) == 'WARN'


def test_verdict_fail_above_3x():
    assert fs.verdict(15.1, 5.0) == 'FAIL'


def test_verdict_na_without_a_model_or_a_median():
    assert fs.verdict(None, 5.0) == 'n/a'
    assert fs.verdict(7.5, None) == 'n/a'
    assert fs.verdict(7.5, 0.0) == 'n/a'


# ── modelled median over the tickers we actually traded ────────────────────

def test_modelled_median_uses_only_traded_tickers():
    assert fs.modelled_median_bps(['AAA', 'BBB'], cost_bps=COSTS) == 5.0


def test_modelled_median_none_when_the_artifact_covers_nothing():
    assert fs.modelled_median_bps(['ZZZ'], cost_bps=COSTS) is None
    assert fs.modelled_median_bps(['AAA'], cost_bps={}) is None


# ── summarize ──────────────────────────────────────────────────────────────

def test_summarize_computes_n_mean_median_p90():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS)
    assert st['entry_n'] == 3
    assert abs(st['entry_mean'] - 10.0) < 1e-9
    assert abs(st['entry_median'] - 6.0) < 1e-9
    assert abs(st['entry_p90'] - 20.0) < 1e-9
    assert st['exit_n'] == 2
    assert abs(st['exit_mean'] - 4.0) < 1e-9


def test_summarize_latency_median_is_over_entry_rows():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS)
    assert abs(st['latency_median_s'] - 4.0) < 1e-9


def test_summarize_cost_usd_is_the_signed_bp_weighted_sum():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS)
    # entry: 4bp*10k + 6bp*20k + 20bp*5k = 4 + 12 + 10 = 26
    # exit:  10bp*30k + (-2bp)*5k      = 30 - 1        = 29
    assert abs(st['cost_usd'] - 55.0) < 1e-6


def test_summarize_verdict_uses_the_modelled_median():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS)
    # One modelled value PER ENTRY ROW (AAA twice, BBB once), so the model is
    # weighted the same way the realized median is: median(4, 4, 6) = 4.0.
    assert abs(st['modelled_median'] - 4.0) < 1e-9
    assert st['verdict'] == 'OK'                 # 6.0 / 4.0 = 1.5 <= OK_MULT


def test_summarize_verdict_fails_when_realized_dwarfs_the_model():
    entry = [{'ticker': 'AAA', 'bps': 40.0, 'notional_usd': 1_000.0, 'latency_s': 1.0}]
    st = fs.summarize(entry, [], cost_bps=COSTS)
    assert st['verdict'] == 'FAIL'               # 40 / 4 = 10x


def test_zero_rows_render_na_everywhere():
    st = fs.summarize([], [], cost_bps=COSTS)
    line = fs.format_line(st, '2026-09-15')
    assert line.startswith('fill_slippage: ')
    assert 'entry n=0 mean=n/a median=n/a p90=n/a' in line
    assert 'exit n=0 mean=n/a' in line
    assert 'latency_med=n/a' in line
    assert 'cost=$0' in line
    assert 'verdict=n/a' in line
    assert 'asof=2026-09-15' in line


def test_format_line_renders_the_populated_case():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS)
    line = fs.format_line(st, '2026-09-15')
    assert line.startswith('fill_slippage: entry n=3 ')
    assert 'median=6.0bp' in line
    assert 'p90=20.0bp' in line
    assert 'exit n=2 mean=4.0bp' in line
    assert 'latency_med=4.0s' in line
    assert 'modelled_med=4.0bp' in line
    assert 'verdict=OK' in line


def test_fill_slippage_line_returns_none_on_any_failure(monkeypatch):
    monkeypatch.setattr(fs, 'load_entry_rows',
                        lambda conn, rd: (_ for _ in ()).throw(RuntimeError('db down')))
    assert fs.fill_slippage_line('2026-09-15', conn=object()) is None


def test_fill_slippage_line_uses_the_injected_connection(monkeypatch):
    monkeypatch.setattr(fs, 'load_entry_rows', lambda conn, rd: list(ENTRY))
    monkeypatch.setattr(fs, 'load_exit_rows', lambda conn, rd: list(EXIT))
    line = fs.fill_slippage_line('2026-09-15', conn=object(), cost_bps=COSTS)
    assert line.startswith('fill_slippage: entry n=3 ')
