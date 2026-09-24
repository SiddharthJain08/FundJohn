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
# BBB legs belong to the benchmark sleeve (fix round 1, cost split).
BENCH_IDS = {'S_beta_spy'}

ENTRY = [
    {'ticker': 'AAA', 'bps': 4.0, 'notional_usd': 10_000.0, 'latency_s': 2.0, 'strategy_id': 'S_alpha1'},
    {'ticker': 'AAA', 'bps': 6.0, 'notional_usd': 20_000.0, 'latency_s': 4.0, 'strategy_id': 'S_alpha1'},
    {'ticker': 'BBB', 'bps': 20.0, 'notional_usd': 5_000.0, 'latency_s': 6.0, 'strategy_id': 'S_beta_spy'},
]
EXIT = [
    {'ticker': 'AAA', 'bps': 10.0, 'notional_usd': 30_000.0, 'strategy_id': 'S_alpha1'},
    {'ticker': 'BBB', 'bps': -2.0, 'notional_usd': 5_000.0, 'strategy_id': 'S_beta_spy'},
]


class _FakeCursor:
    """Stands in for a psycopg2 cursor: execute() ignores the SQL text
    (a fake can't exercise Postgres' own DISTINCT ON), fetchall() returns
    canned rows — so these tests exercise the Python-side dedupe in
    _dedupe_by_submission, which is the actual tested guarantee."""
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


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
    # No bench_ids injected -> everything lands in alpha (fail-open default).
    assert abs(st['cost_usd'] - 55.0) < 1e-6
    assert abs(st['cost_alpha_usd'] - 55.0) < 1e-6
    assert st['cost_bench_usd'] == 0.0


def test_summarize_splits_cost_alpha_vs_bench_by_strategy_id():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS, bench_ids=BENCH_IDS)
    # AAA legs (S_alpha1): entry 4+12=16, exit 30            -> alpha = 46
    # BBB legs (S_beta_spy): entry 10, exit -1                -> bench = 9
    assert abs(st['cost_alpha_usd'] - 46.0) < 1e-6
    assert abs(st['cost_bench_usd'] - 9.0) < 1e-6
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
    assert 'cost=$0 alpha / $0 bench' in line
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


def test_format_line_renders_the_alpha_bench_cost_split():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS, bench_ids=BENCH_IDS)
    line = fs.format_line(st, '2026-09-15')
    assert 'cost=$46 alpha / $9 bench' in line


# ── failure contract (fix round 1): ALWAYS a string, reason included ───────

def test_fill_slippage_line_renders_na_with_reason_on_any_failure(monkeypatch):
    monkeypatch.setattr(fs, 'load_entry_rows',
                        lambda conn, rd: (_ for _ in ()).throw(RuntimeError('db down')))
    line = fs.fill_slippage_line('2026-09-15', conn=object(), bench_ids=set())
    assert line == 'fill_slippage: n/a (RuntimeError: db down)'


def test_fill_slippage_line_scrubs_a_dsn_before_truncating(monkeypatch):
    """F3 / review I-4: a psycopg2-like error carrying the DSN (credentials
    and all) must never reach the Discord-posted line — the userinfo is
    scrubbed BEFORE the 60-char truncation, not after (truncation alone
    offers no guarantee the credentials land before the cut)."""
    dsn_err = 'conn failed: postgresql://tradejohn:s3cr3t@10.0.0.5/db boom'
    monkeypatch.setattr(fs, 'load_entry_rows',
                        lambda conn, rd: (_ for _ in ()).throw(RuntimeError(dsn_err)))
    line = fs.fill_slippage_line('2026-09-15', conn=object(), bench_ids=set())
    assert 's3cr3t' not in line
    assert 'tradejohn' not in line
    assert 'postgres://***@' in line

    # A DSN sitting past the 60-char cutoff must still never leak — the
    # scrub runs before truncation, so worst case the secret is simply gone
    # from the truncated tail, never partially exposed.
    long_prefix_err = ('could not connect to server: FATAL: password '
                       'authentication failed for user "x" at '
                       'postgresql://tradejohn:s3cr3t-pw@10.0.0.5:5432/fundjohn')
    monkeypatch.setattr(fs, 'load_entry_rows',
                        lambda conn, rd: (_ for _ in ()).throw(RuntimeError(long_prefix_err)))
    line2 = fs.fill_slippage_line('2026-09-15', conn=object(), bench_ids=set())
    assert 's3cr3t-pw' not in line2
    assert 'tradejohn' not in line2


def test_fill_slippage_line_uses_the_injected_connection(monkeypatch):
    monkeypatch.setattr(fs, 'load_entry_rows', lambda conn, rd: list(ENTRY))
    monkeypatch.setattr(fs, 'load_exit_rows', lambda conn, rd: list(EXIT))
    line = fs.fill_slippage_line('2026-09-15', conn=object(), cost_bps=COSTS, bench_ids=set())
    assert line.startswith('fill_slippage: entry n=3 ')


# ── dedupe (fix round 1): one physical fill counts once ────────────────────

def test_load_entry_rows_dedupes_signals_sharing_one_submission():
    # overnight signal + same-day signal for the same (strategy, ticker) both
    # join the SAME alpaca_submissions row -- one physical fill, count once.
    rows = [
        ('AAA', 4.0, 40.0, 2.0, 'S1'),
        ('AAA', 4.0, 40.0, 2.0, 'S1'),
    ]
    out = fs.load_entry_rows(_FakeConn(rows), '2026-09-15')
    assert len(out) == 1
    assert out[0]['notional_usd'] == 40.0
    st = fs.summarize(out, [], cost_bps=COSTS)
    assert st['entry_n'] == 1
    assert abs(st['cost_usd'] - (4.0 / 10000.0 * 40.0)) < 1e-9


def test_load_entry_rows_keeps_distinct_strategy_or_ticker_rows():
    rows = [
        ('AAA', 4.0, 40.0, 2.0, 'S1'),
        ('BBB', 4.0, 40.0, 2.0, 'S1'),   # different ticker, same strategy
        ('AAA', 4.0, 40.0, 2.0, 'S2'),   # different strategy, same ticker
    ]
    out = fs.load_entry_rows(_FakeConn(rows), '2026-09-15')
    assert len(out) == 3


def test_load_exit_rows_dedupes_signals_sharing_one_submission():
    rows = [
        ('AAA', 10.0, 300.0, 'S1'),
        ('AAA', 10.0, 300.0, 'S1'),
    ]
    out = fs.load_exit_rows(_FakeConn(rows), '2026-09-15')
    assert len(out) == 1
    assert out[0]['notional_usd'] == 300.0


def test_load_exit_rows_never_dedupes_unmatched_left_join_legs():
    # strategy_id is None (no matching alpaca_submissions row) -- each row is
    # distinct data, never collapsed against another None-strategy_id row.
    rows = [
        ('AAA', 10.0, 0.0, None),
        ('BBB', -2.0, 0.0, None),
    ]
    out = fs.load_exit_rows(_FakeConn(rows), '2026-09-15')
    assert len(out) == 2


def test_fill_slippage_line_scrub_precedes_truncation_when_dsn_straddles_the_cut(monkeypatch):
    """Re-review finding 4: pin the ORDER (scrub, then truncate). Credentials
    sit inside the first 60 chars and the '@' lands just past the cut, so
    truncation alone would post 'tradejohn:' plus most of the password."""
    pw = 'p' * 45
    straddle_err = 'db: postgresql://tradejohn:' + pw + '@10.0.0.5/fundjohn'
    assert straddle_err.index('@') > 60 and straddle_err.index('tradejohn') < 60
    monkeypatch.setattr(fs, 'load_entry_rows',
                        lambda conn, rd: (_ for _ in ()).throw(RuntimeError(straddle_err)))
    line = fs.fill_slippage_line('2026-09-15', conn=object(), bench_ids=set())
    assert 'tradejohn' not in line
    assert 'ppppp' not in line
    assert 'postgres://***@' in line
