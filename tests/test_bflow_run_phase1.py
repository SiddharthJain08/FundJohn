"""Tests for src/research/bflow/run_phase1.py — driver + aggregation + report.

Pure aggregation on synthetic rows + a hermetic driver smoke (stub loader +
SQL-dispatching fake conn + injected sessions). NO network, NO DB, NO reading
the real prices.parquet.

Row contract (one per eval unit) = intent fields merged with oracle fields:
  intent: grain, worked_session, ticker, direction, regime, plus grain-specific
          weight fields (sum_size_pct for primary, qty/filled_avg_price broker).
  oracle: excess_net_bps, win_net_bps, win_start_minute, p_eod_dump,
          p_eod_close, exclude_reason, ... (see oracle.evaluate_intent).
"""
import os

import pytest

from src.research.bflow import run_phase1


# --------------------------------------------------------------------------
# row builder — a fully-shaped INCLUDED row (exclude_reason None)
# --------------------------------------------------------------------------
def row(session, ticker, direction, excess, *, grain="primary",
        win_net=None, win_start=120, sum_size_pct=1.0, qty=10.0,
        filled_avg_price=100.0, regime="LOW_VOL", p_eod_dump=100.0,
        p_eod_close=100.0, exclude_reason=None):
    """Build a row with the merged intent+oracle shape. win_net defaults to
    excess (the directed prize >= the null-relative excess in practice, but the
    tests only need them independently settable)."""
    if win_net is None:
        win_net = excess
    return {
        "grain": grain,
        "worked_session": session,
        "ticker": ticker,
        "direction": direction,
        "regime": regime,
        "sum_size_pct": sum_size_pct,
        "n_signals": 1,
        "qty": qty,
        "filled_avg_price": filled_avg_price,
        "excess_net_bps": excess,
        "win_net_bps": win_net,
        "win_start_minute": win_start,
        "win_gross_bps": win_net,
        "p_eod_dump": p_eod_dump,
        "p_eod_close": p_eod_close,
        "directed_in_last15": win_start >= 375,
        "flipped_in_last15": False,
        "exclude_reason": exclude_reason,
    }


def excl_row(session, ticker, direction, reason, *, grain="primary"):
    """An excluded row: oracle metric fields are None, exclude_reason set."""
    r = run_phase1.evaluate_all(
        [{"grain": grain, "worked_session": session, "ticker": ticker,
          "direction": direction, "regime": "LOW_VOL", "sum_size_pct": 1.0,
          "n_signals": 1, "qty": 1.0, "filled_avg_price": 1.0}],
        lambda s: {},   # loader returns no bars -> no_bars row
    )[0]
    assert r["exclude_reason"] == "no_bars"
    return r


# ==========================================================================
# 1. per_session_medians
# ==========================================================================
def test_per_session_medians_over_included_rows_only():
    rows = [
        row("2026-04-13", "AAA", "LONG", 4.0),
        row("2026-04-13", "BBB", "SHORT", 6.0),    # session median = 5.0
        row("2026-04-14", "CCC", "LONG", 10.0),    # session median = 10.0
        excl_row("2026-04-13", "DDD", "LONG", "no_bars"),  # MUST be ignored
    ]
    med = run_phase1.per_session_medians(rows, field="excess_net_bps")
    assert med == {"2026-04-13": 5.0, "2026-04-14": 10.0}


def test_per_session_medians_empty_when_all_excluded():
    rows = [excl_row("2026-04-13", "DDD", "LONG", "no_bars")]
    assert run_phase1.per_session_medians(rows) == {}


# ==========================================================================
# 2. aggregate_report — kill verdict GO / KILL + weights + exclusions
# ==========================================================================
def _go_rows():
    """5 sessions, every per-session median strongly > 2bps -> GO
    (median-of-medians well above 2, 100% positive sessions)."""
    rows = []
    for i, s in enumerate(["2026-04-13", "2026-04-14", "2026-04-15",
                           "2026-04-16", "2026-04-17"]):
        rows.append(row(s, f"T{i}A", "LONG", 8.0))
        rows.append(row(s, f"T{i}B", "SHORT", 12.0))   # per-session median 10
    return rows


def _kill_rows():
    """5 sessions, per-session medians at/around 0 -> KILL
    (median-of-medians ~0, far below 2bps)."""
    rows = []
    for i, s in enumerate(["2026-04-13", "2026-04-14", "2026-04-15",
                           "2026-04-16", "2026-04-17"]):
        rows.append(row(s, f"T{i}A", "LONG", -1.0))
        rows.append(row(s, f"T{i}B", "SHORT", 1.0))    # per-session median 0
    return rows


def test_aggregate_report_go_verdict():
    agg = run_phase1.aggregate_report(_go_rows())
    v = agg["kill_verdict"]
    assert v["go"] is True
    assert v["median_of_medians"] > 2.0
    assert v["frac_pos_sessions"] >= 0.60


def test_aggregate_report_kill_verdict():
    agg = run_phase1.aggregate_report(_kill_rows())
    v = agg["kill_verdict"]
    assert v["go"] is False
    # median-of-medians ~0 fails the >2bps bar
    assert not (v["median_of_medians"] > 2.0)


def test_aggregate_report_kill_verdict_primary_grain_only():
    # DISCRIMINATING: primary per-session medians all small-positive (0.5) ->
    # primary median-of-medians = 0.5 < 2 -> KILL. Dump a high-excess BROKER row
    # onto EVERY session: if broker leaked into the verdict, every session median
    # would jump >2 and frac_pos=1.0 -> GO. Correct behavior stays KILL.
    sessions = ["2026-04-13", "2026-04-14", "2026-04-15",
                "2026-04-16", "2026-04-17"]
    rows = []
    for i, s in enumerate(sessions):
        rows.append(row(s, f"P{i}", "LONG", 0.5))                 # primary median 0.5
        rows.append(row(s, f"B{i}", "LONG", 999.0, grain="broker"))  # would leak
    agg = run_phase1.aggregate_report(rows)
    assert agg["kill_verdict"]["go"] is False
    # the verdict's median-of-medians is the PRIMARY 0.5, not a broker-inflated value
    assert agg["kill_verdict"]["median_of_medians"] == 0.5
    # the per-session table is primary-only too
    assert set(agg["per_session_medians"].values()) == {0.5}


def test_aggregate_report_excluded_rows_counted_not_aggregated():
    rows = _go_rows()
    rows.append(excl_row("2026-04-13", "EXC1", "LONG", "no_bars"))
    rows.append(excl_row("2026-04-14", "EXC2", "SHORT", "no_bars"))
    agg = run_phase1.aggregate_report(rows)
    # exclusion counts present
    assert agg["exclusions"]["no_bars"] == 2
    # the excluded rows did not change the GO verdict (still all included rows)
    assert agg["kill_verdict"]["go"] is True


def test_aggregate_report_conviction_weight_shifts_median():
    # Two rows in one session: a small-excess heavy-conviction row and a
    # large-excess light-conviction row. Equal-weight median != conviction
    # median (the heavy row pulls the weighted statistic toward its value).
    rows = [
        row("2026-04-13", "HEAVY", "LONG", 1.0, sum_size_pct=100.0),
        row("2026-04-13", "LIGHT", "LONG", 50.0, sum_size_pct=1.0),
    ]
    agg = run_phase1.aggregate_report(rows)
    eq = agg["headline"]["primary"]["equal_weight"]["median"]
    cw = agg["headline"]["primary"]["conviction_weight"]["median"]
    # equal-weight median of {1,50} (our convention -> lower of the two = 1.0
    # at q=0.5 first-cum>=half), conviction-weight dominated by the heavy row.
    assert cw == 1.0          # heavy conviction sits on 1.0
    assert eq in (1.0, 50.0)  # equal-weight picks an actual value (no interp)
    # and the two weightings are reported as DISTINCT numbers when conviction
    # differs enough — flip the conviction to prove the weighting bites.
    rows2 = [
        row("2026-04-13", "HEAVY", "LONG", 50.0, sum_size_pct=100.0),
        row("2026-04-13", "LIGHT", "LONG", 1.0, sum_size_pct=1.0),
    ]
    cw2 = run_phase1.aggregate_report(rows2)["headline"]["primary"]["conviction_weight"]["median"]
    assert cw2 == 50.0
    assert cw2 != cw


def test_aggregate_report_range_adequacy_labeled_not_capturable():
    # The range-adequacy bound is the raw directed win_net_bps median.
    rows = [
        row("2026-04-13", "A", "LONG", 3.0, win_net=40.0),
        row("2026-04-13", "B", "LONG", 5.0, win_net=60.0),
    ]
    agg = run_phase1.aggregate_report(rows)
    ra = agg["range_adequacy_bound"]
    assert ra["median"] in (40.0, 60.0)
    assert ra["not_capturable"] is True


def test_aggregate_report_long_short_and_regime_split():
    rows = [
        row("2026-04-13", "A", "LONG", 4.0, regime="LOW_VOL"),
        row("2026-04-13", "B", "SHORT", 8.0, regime="HIGH_VOL"),
    ]
    agg = run_phase1.aggregate_report(rows)
    assert set(agg["long_short"].keys()) == {"LONG", "SHORT"}
    assert agg["long_short"]["LONG"]["n"] == 1
    assert agg["long_short"]["SHORT"]["n"] == 1
    assert set(agg["per_regime"].keys()) == {"LOW_VOL", "HIGH_VOL"}


def test_aggregate_report_oracle_time_histogram_buckets():
    # buckets [0-29, 30-89, 90-329, 330-374, 375-389]
    rows = [
        row("2026-04-13", "A", "LONG", 1.0, win_start=0),     # 0-29
        row("2026-04-13", "B", "LONG", 1.0, win_start=50),    # 30-89
        row("2026-04-13", "C", "LONG", 1.0, win_start=200),   # 90-329
        row("2026-04-13", "D", "LONG", 1.0, win_start=350),   # 330-374
        row("2026-04-13", "E", "LONG", 1.0, win_start=380),   # 375-389
    ]
    agg = run_phase1.aggregate_report(rows)
    hist = agg["oracle_time_histogram"]
    assert hist["0-29"] == 1
    assert hist["30-89"] == 1
    assert hist["90-329"] == 1
    assert hist["330-374"] == 1
    assert hist["375-389"] == 1


def test_aggregate_report_oracle_eod_directed_and_flipped():
    # DISCRIMINATING on value-based: directed value-based must read the DIRECTED
    # net (win_net_bps), NOT the null-relative excess. Row B has positive excess
    # (3.0) but negative directed net (win_net=-2.0) -> it IS oracle≈EOD by value
    # (directed net <= 0) even though its excess is positive.
    rows = [
        row("2026-04-13", "A", "LONG", 5.0, win_net=5.0, win_start=380),  # time-based
        row("2026-04-13", "B", "LONG", 3.0, win_net=-2.0, win_start=10),  # value-based (net<=0)
        row("2026-04-13", "C", "LONG", 3.0, win_net=4.0, win_start=10),   # neither
    ]
    agg = run_phase1.aggregate_report(rows)
    oe = agg["oracle_eq_eod"]
    # directed reported AND flipped reported, both time + value
    assert "directed" in oe and "flipped" in oe
    assert oe["directed"]["time_based"] == 1   # only A starts >=375
    # only B has DIRECTED net <= 0 (win_net_bps), proving we don't read excess
    assert oe["directed"]["value_based"] == 1


def test_aggregate_report_divergence_vs_parquet_close_flagged():
    # closes lookup keyed (session, ticker) -> parquet close. Divergence in bps
    # of oracle p_eod_close vs parquet close; >50bps flagged. Only over
    # exclude_reason-None rows.
    rows = [
        row("2026-04-13", "A", "LONG", 4.0, p_eod_close=100.0),  # match
        row("2026-04-13", "B", "LONG", 4.0, p_eod_close=100.0),  # big divergence
        excl_row("2026-04-13", "C", "LONG", "no_bars"),          # ignored
    ]
    closes = {("2026-04-13", "A"): 100.0,    # 0 bps
              ("2026-04-13", "B"): 102.0}    # ~200 bps -> flagged
    agg = run_phase1.aggregate_report(rows, closes=closes)
    dv = agg["divergence"]
    assert dv["n_compared"] == 2
    assert dv["n_flagged_gt50bps"] == 1


# ==========================================================================
# 3. format_report — load-bearing strings (spec §5/§6 wording)
# ==========================================================================
def test_format_report_contains_load_bearing_strings():
    agg = run_phase1.aggregate_report(_go_rows())
    md = run_phase1.format_report(agg)
    assert "not capturable" in md
    # dual benchmark naming (spec §5)
    assert "beats our current execution" in md
    assert "beats the validated backtest parity anchor" in md
    # directed vs flipped section
    assert "directed" in md.lower()
    assert "flipped" in md.lower() or "sign-flipped" in md.lower()
    # kill verdict line
    assert "GO" in md or "KILL" in md
    # DTBP/PDT context note
    assert "DTBP" in md or "PDT" in md


def test_format_report_shows_kill_for_kill_dataset():
    agg = run_phase1.aggregate_report(_kill_rows())
    md = run_phase1.format_report(agg)
    assert "KILL" in md


def test_format_report_documents_60_bar_minimum(monkeypatch):
    # minor finding 2: the oracle's 60-valid-bar minimum ('too_few_bars') is an
    # implementer-chosen included-set definition not named in the spec; the
    # report's data-quality section must document it so reviewers know the
    # included-set definition (exclusions are already counted, but the bar
    # threshold itself was undocumented).
    agg = run_phase1.aggregate_report(_go_rows())
    md = run_phase1.format_report(agg)
    assert "60" in md
    assert "too_few_bars" in md


# ==========================================================================
# 4. evaluate_all — merges intent + oracle fields; missing bars -> no_bars
# ==========================================================================
def _bar(minute, vw, v=1000.0, h=None, l=None, c=None):
    h = vw if h is None else h
    l = vw if l is None else l
    c = vw if c is None else c
    return {"minute": minute, "o": vw, "h": h, "l": l, "c": c, "v": v, "vw": vw}


def _full_session_bars(low_at=120):
    """390 valid bars at vw=100 except a dip to 90 at minute low_at and the dump
    window flat at 100 — enough that oracle does NOT exclude (n_valid >= 60,
    dump window present)."""
    bars = []
    for m in range(390):
        vw = 90.0 if m == low_at else 100.0
        bars.append(_bar(m, vw))
    return bars


def test_evaluate_all_merges_intent_and_oracle_fields():
    intents = [{
        "grain": "primary", "worked_session": "2026-04-13", "ticker": "AAA",
        "direction": "LONG", "regime": "LOW_VOL", "sum_size_pct": 2.5,
        "n_signals": 3,
    }]
    loader = lambda s: {"AAA": _full_session_bars(low_at=120)}
    rows = run_phase1.evaluate_all(intents, loader)
    assert len(rows) == 1
    r = rows[0]
    # intent fields carried
    assert r["ticker"] == "AAA"
    assert r["regime"] == "LOW_VOL"
    assert r["sum_size_pct"] == 2.5
    # oracle fields merged
    assert r["exclude_reason"] is None
    assert r["win_net_bps"] is not None
    assert r["excess_net_bps"] is not None
    # LONG buying the dip at minute 120 -> oracle picks a window around it
    assert r["win_start_minute"] is not None


def test_evaluate_all_missing_ticker_bars_makes_no_bars_row():
    intents = [{
        "grain": "primary", "worked_session": "2026-04-13", "ticker": "GONE",
        "direction": "LONG", "regime": "LOW_VOL", "sum_size_pct": 1.0,
        "n_signals": 1,
    }]
    loader = lambda s: {}   # ticker absent
    rows = run_phase1.evaluate_all(intents, loader)
    assert len(rows) == 1
    r = rows[0]
    assert r["exclude_reason"] == "no_bars"
    # excluded rows still carry intent fields + a stable (all-None) metric schema
    assert r["ticker"] == "GONE"
    assert r["excess_net_bps"] is None
    assert r["win_net_bps"] is None
    assert "p_eod_dump" in r   # key present (None), stable parquet schema


def test_evaluate_all_calls_loader_once_per_session():
    calls = []

    def loader(s):
        calls.append(s)
        return {"A": _full_session_bars(), "B": _full_session_bars()}

    intents = [
        {"grain": "primary", "worked_session": "2026-04-13", "ticker": "A",
         "direction": "LONG", "regime": "LOW_VOL", "sum_size_pct": 1.0, "n_signals": 1},
        {"grain": "primary", "worked_session": "2026-04-13", "ticker": "B",
         "direction": "SHORT", "regime": "LOW_VOL", "sum_size_pct": 1.0, "n_signals": 1},
    ]
    run_phase1.evaluate_all(intents, loader)
    assert calls == ["2026-04-13"]   # one call, both tickers from one loader hit


# ==========================================================================
# 5. main — hermetic smoke with stub loader + SQL-dispatching fake conn
# ==========================================================================
class _FakeCursor:
    """Dispatches on SQL substring so one conn serves primary + broker +
    regime queries with the right canned rows."""

    def __init__(self, primary, broker, regime):
        self._primary = primary
        self._broker = broker
        self._regime = regime
        self._result = []

    def execute(self, sql, params=None):
        if "alpaca_submissions" in sql:
            self._result = self._broker
        elif "execution_runs" in sql:
            self._result = self._regime
        else:                       # signal_pnl / execution_signals
            self._result = self._primary

    def fetchall(self):
        return self._result


class _FakeConn:
    def __init__(self, primary, broker, regime):
        self._p, self._b, self._r = primary, broker, regime

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._p, self._b, self._r)


def test_main_smoke_writes_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("BFLOW_ANALYSIS_DIR", str(tmp_path))

    # injected SPY session index — signal 04-10 -> worked 04-13 (< today)
    sessions = ["2026-04-13", "2026-04-14", "2026-04-15"]
    today = "2026-06-05"

    primary = [
        {"signal_date": "2026-04-10", "ticker": "AAA", "direction": "LONG",
         "n_signals": 3, "sum_size_pct": 2.5, "regime_state": "LOW_VOL"},
        {"signal_date": "2026-04-10", "ticker": "BBB", "direction": "SHORT",
         "n_signals": 2, "sum_size_pct": 1.5, "regime_state": "HIGH_VOL"},
    ]
    broker = [
        {"run_date": "2026-04-13", "ticker": "AAA", "direction": "LONG",
         "qty": 10.0, "filled_avg_price": 100.0, "strategy_id": "S1"},
    ]
    regime = [{"run_date": "2026-04-13", "regime_state": "LOW_VOL"}]
    conn = _FakeConn(primary, broker, regime)

    def loader(session):
        return {"AAA": _full_session_bars(low_at=120),
                "BBB": _full_session_bars(low_at=300)}

    rc = run_phase1.main(
        ["--grain", "both", "--limit", "2", "--no-fetch"],
        conn=conn, loader=loader, sessions=sessions, today=today,
        closes_loader=lambda pairs: {},  # hermetic: never read the real parquet
    )
    assert rc == 0

    parquet = tmp_path / "bflow_phase1_oracle_orders.parquet"
    report = tmp_path / "bflow_phase1_report.md"
    assert parquet.exists()
    assert report.exists()
    text = report.read_text()
    assert "not capturable" in text
    assert ("GO" in text) or ("KILL" in text)


def test_main_no_post_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("BFLOW_ANALYSIS_DIR", str(tmp_path))
    posted = []
    monkeypatch.setattr(run_phase1, "post_discord",
                        lambda *a, **k: posted.append(a))

    sessions = ["2026-04-13", "2026-04-14"]
    primary = [{"signal_date": "2026-04-10", "ticker": "AAA",
                "direction": "LONG", "n_signals": 1, "sum_size_pct": 1.0,
                "regime_state": "LOW_VOL"}]
    conn = _FakeConn(primary, [], [{"run_date": "2026-04-13",
                                    "regime_state": "LOW_VOL"}])
    loader = lambda s: {"AAA": _full_session_bars()}

    rc = run_phase1.main(
        ["--grain", "primary", "--no-fetch"],
        conn=conn, loader=loader, sessions=sessions, today="2026-06-05",
        closes_loader=lambda pairs: {},  # hermetic: never read the real parquet
    )
    assert rc == 0
    assert posted == []   # --post not given -> never posts


def test_main_builds_parquet_closes_from_loader(tmp_path, monkeypatch):
    # REGRESSION (important finding): the real driver MUST build a
    # (worked_session,ticker)->parquet-close map and feed it into
    # aggregate_report so the §5/§6 parquet cross-check actually runs. Before the
    # fix `main()` left closes=None, so the report always rendered
    # "compared 0, median n/a". Here we inject a closes_loader (so the real
    # prices.parquet is never read) and assert (a) it was called with the
    # evaluated (session,ticker) pairs and (b) the report shows compared > 0.
    monkeypatch.setenv("BFLOW_ANALYSIS_DIR", str(tmp_path))

    sessions = ["2026-04-13", "2026-04-14", "2026-04-15"]
    today = "2026-06-05"
    primary = [
        {"signal_date": "2026-04-10", "ticker": "AAA", "direction": "LONG",
         "n_signals": 1, "sum_size_pct": 1.0, "regime_state": "LOW_VOL"},
    ]
    conn = _FakeConn(primary, [], [{"run_date": "2026-04-13",
                                    "regime_state": "LOW_VOL"}])
    # The included row's p_eod_close (last RTH minute close) is 100.0 from these
    # flat-100 bars; supply a parquet close that diverges >50bps so it is also
    # FLAGGED, proving the comparison both ran and applied the threshold.
    loader = lambda s: {"AAA": _full_session_bars(low_at=120)}

    seen = {}

    def closes_loader(pairs):
        seen["pairs"] = list(pairs)
        return {("2026-04-13", "AAA"): 102.0}   # ~200bps vs the 100.0 close

    rc = run_phase1.main(
        ["--grain", "primary", "--no-fetch"],
        conn=conn, loader=loader, sessions=sessions, today=today,
        closes_loader=closes_loader,
    )
    assert rc == 0
    # the loader was called with the evaluated (session, ticker) pairs
    assert ("2026-04-13", "AAA") in seen["pairs"]
    report = (tmp_path / "bflow_phase1_report.md").read_text()
    # the parquet cross-check actually ran (was "compared 0" before the fix)
    assert "compared 1" in report
    assert ">50bps flagged 1" in report
