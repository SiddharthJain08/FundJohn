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


def test_bucket_boundaries_0_6_and_0_8_agree_between_bucket_for_and_bucket_aggregates():
    """0.6 and 0.8 are each the (inclusive) LOWER bound of their own bucket
    under the half-open convention `lo <= c < hi`. Pins that bucket_for and
    _bucket_aggregates — two independent implementations of that convention —
    agree at both boundaries."""
    assert mc.bucket_for(0.6)[2] == '[0.6, 0.8]'
    assert mc.bucket_for(0.8)[2] == '[0.8, 1.0]'
    obs = [
        {'confidence': 0.6, 'direction_match': True},
        {'confidence': 0.8, 'direction_match': False},
    ]
    buckets = mc._bucket_aggregates(obs)
    row_68 = next(r for r in buckets if r['range'] == '[0.6, 0.8]')
    row_81 = next(r for r in buckets if r['range'] == '[0.8, 1.0]')
    assert row_68['count'] == 1 and row_68['matched'] == 1
    assert row_81['count'] == 1 and row_81['matched'] == 0


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


def test_raw_above_one_clamps_to_one_before_bucketing_and_scaling():
    """A stray raw > 1.0 clamps to 1.0 before both the bucket lookup and the
    ratio multiply — it lands in the top [0.8, 1.0] bucket (not out of
    BUCKETS' domain), and the returned value cannot exceed the clamped raw."""
    table = _table(**{'[0.8, 1.0]': (20, 0.72)})
    result = mc.calibrated_confidence(1.2, table)
    expected_ratio = max(0.5, min(1.0, 0.72 / 0.9))  # 0.8
    assert result == pytest.approx(1.0 * expected_ratio)


def test_raw_below_zero_clamps_to_zero():
    table = _table(**{'[0.0, 0.2]': (10, 0.1)})
    assert mc.calibrated_confidence(-0.3, table) == pytest.approx(0.0)


def test_raw_nan_returns_none():
    table = _table(**{'[0.8, 1.0]': (20, 0.5)})
    assert mc.calibrated_confidence(float('nan'), table) is None


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


def test_staleness_exactly_45_days_is_not_stale():
    """Strict '>' — exactly 45.0 days does NOT drop a level; a hair past it
    does."""
    assert mc.evidence_level(50, staleness_days=45.0) == 'medium'
    assert mc.evidence_level(50, staleness_days=45.0000001) == 'low'


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
    captured = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params=()):
            self.sql = sql
            captured['params'] = params
        def fetchone(self): return (42, last)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return _Cur()

    monkeypatch.setattr(mc, '_connect', lambda: _Conn())
    out = mc.evidence_counts('S_x', 'LOW_VOL', now=now)
    assert out['n_closed'] == 42
    assert out['staleness_days'] == pytest.approx(7.0, abs=1e-6)
    # Pins the bounded window (30-day COUNT filter, unbounded MAX) and the
    # placeholder order Task 5's fake cursor must also match.
    assert captured['params'] == (now - timedelta(days=30), 'S_x', 'LOW_VOL')


# ── evidence_counts: production DATE column + fail-closed DB contract ─────────
# signal_pnl.closed_at is a DATE column (migration 012_execution_engine.sql:62,
# never altered), so psycopg2 hands back a plain datetime.date, not a
# datetime.datetime. A bare `.replace(tzinfo=...)` on a date raises TypeError.

def _fake_conn(fetchone_return, *, raise_on_execute=None):
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params=()):
            if raise_on_execute is not None:
                raise raise_on_execute
        def fetchone(self): return fetchone_return

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return _Cur()

    return _Conn()


def test_evidence_counts_handles_a_date_closed_at(monkeypatch):
    """The real production shape: psycopg2 returns datetime.date for a DATE
    column. Must not raise, and must compute correct whole-day staleness."""
    from datetime import date, datetime, timezone
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    last = date(2026, 9, 5)  # midnight UTC on this date, per the normalisation rule

    monkeypatch.setattr(mc, '_connect', lambda: _fake_conn((5, last)))
    out = mc.evidence_counts('S_x', 'LOW_VOL', now=now)
    assert out['n_closed'] == 5
    assert out['staleness_days'] == pytest.approx(7.0, abs=1e-9)


def test_evidence_counts_handles_a_naive_datetime_closed_at(monkeypatch):
    """A naive datetime.datetime (no tzinfo) is assumed UTC."""
    from datetime import datetime, timezone
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    last_naive = datetime(2026, 9, 10)  # no tzinfo

    monkeypatch.setattr(mc, '_connect', lambda: _fake_conn((3, last_naive)))
    out = mc.evidence_counts('S_x', 'LOW_VOL', now=now)
    assert out['staleness_days'] == pytest.approx(2.0, abs=1e-9)


def test_evidence_counts_keeps_an_aware_datetime_closed_at_as_is(monkeypatch):
    """An aware datetime.datetime (non-UTC tz) is NOT re-interpreted — it is
    converted correctly by ordinary aware-datetime subtraction."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    tz_minus5 = timezone(timedelta(hours=-5))
    last_aware = datetime(2026, 9, 9, 20, 0, tzinfo=tz_minus5)  # == 2026-09-10T01:00:00Z

    monkeypatch.setattr(mc, '_connect', lambda: _fake_conn((1, last_aware)))
    out = mc.evidence_counts('S_x', 'LOW_VOL', now=now)
    expected = (now - datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)).total_seconds() / 86400.0
    assert out['staleness_days'] == pytest.approx(expected, abs=1e-9)


def test_evidence_counts_handles_a_naive_now(monkeypatch):
    """`now` itself may be a naive datetime (e.g. a caller that forgot
    tzinfo) — it is assumed UTC, same as `closed_at`."""
    from datetime import datetime, timezone
    now_naive = datetime(2026, 9, 12)  # no tzinfo
    last = datetime(2026, 9, 5, tzinfo=timezone.utc)

    monkeypatch.setattr(mc, '_connect', lambda: _fake_conn((2, last)))
    out = mc.evidence_counts('S_x', 'LOW_VOL', now=now_naive)
    assert out['staleness_days'] == pytest.approx(7.0, abs=1e-9)


def test_evidence_counts_fails_closed_on_db_error(monkeypatch, caplog):
    """Any exception while talking to the DB -> logged at WARNING, returns
    the 'none'-level default rather than raising or fabricating a count."""
    import logging
    monkeypatch.setattr(
        mc, '_connect',
        lambda: _fake_conn(None, raise_on_execute=RuntimeError('connection refused')))
    with caplog.at_level(logging.WARNING):
        out = mc.evidence_counts('S_x', 'LOW_VOL')
    assert out == {'n_closed': 0, 'staleness_days': None}
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_evidence_counts_fails_closed_when_connect_itself_raises(monkeypatch):
    def _raise():
        raise RuntimeError('no db available')
    monkeypatch.setattr(mc, '_connect', _raise)
    out = mc.evidence_counts('S_x', 'LOW_VOL')
    assert out == {'n_closed': 0, 'staleness_days': None}


# ── report-shape safety net (pure-additions guarantee) ─────────────────────────
# D2b is a pure addition to mastermind_calibration.py: no existing function's
# signature or output may change. This pins calibration_report()'s exact shape
# on a fixture so doctor.check_mastermind_calibration_brier and the dashboard's
# calibration_report() consumers are provably byte-identical, not just
# "nothing in the diff touched them."

def test_calibration_report_shape_is_pinned(monkeypatch):
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params=None): self.sql = sql
        def fetchall(self):
            return [(0.9, True, 'approved'),
                    (0.85, False, 'approved'),
                    (0.3, None, 'rejected')]

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return _Cur()

    monkeypatch.setattr(mc, '_connect', lambda: _Conn())
    report = mc.calibration_report()
    assert report == {
        'total_observations': 3,
        'resolved_observations': 2,
        'hit_rate': pytest.approx(0.5),
        'mean_confidence': pytest.approx(0.875),
        'brier_score': pytest.approx(0.36625),
        'buckets': [
            {'range': '[0.0, 0.2]', 'count': 0, 'matched': 0, 'match_rate': None},
            {'range': '[0.2, 0.4]', 'count': 0, 'matched': 0, 'match_rate': None},
            {'range': '[0.4, 0.6]', 'count': 0, 'matched': 0, 'match_rate': None},
            {'range': '[0.6, 0.8]', 'count': 0, 'matched': 0, 'match_rate': None},
            {'range': '[0.8, 1.0]', 'count': 2, 'matched': 1, 'match_rate': pytest.approx(0.5)},
        ],
    }
