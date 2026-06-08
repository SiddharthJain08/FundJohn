"""SP-7 Phase C Task 6 — gate-OFF byte-identity + gate-ON per-strategy slicing."""
import pandas as pd


def _wide_prices():
    idx = pd.to_datetime(["2026-06-04", "2026-06-05"])
    return pd.DataFrame({"AAPL": [1.0, 2.0], "RDDT": [3.0, 4.0], "SPY": [5.0, 6.0]}, index=idx)


class _CaptureStrategy:
    def __init__(self, sid):
        self.id = sid
        self.calls = []
    def generate_signals(self, prices, regime, universe, aux):
        self.calls.append({"cols": list(prices.columns), "universe": list(universe),
                           "fin_keys": sorted(aux.get("financials", {}).keys())})
        return []


def _aux():
    return {"financials": {"AAPL": {}, "RDDT": {}, "SPY": {}},
            "insider_txns": {"AAPL": [], "RDDT": []},
            "options": {"AAPL": {}},
            "sentiment": {"RDDT": {}},
            "macro": {"vix": pd.Series([15.0])},
            "prices_30m": pd.DataFrame({"ticker": ["AAPL", "RDDT"], "close": [1, 2]})}


def _run(strategy_universes):
    from execution.engine import run_strategies
    strat = _CaptureStrategy("S_x")
    import execution.engine as eng
    # neutralize regime/instrument gates for the unit test
    orig_elig, orig_ic = eng.is_eligible, eng.instrument_class_for
    eng.is_eligible = lambda sid, r: True
    eng.instrument_class_for = lambda sid: "equity"
    try:
        run_strategies([strat], _wide_prices(), {"state": "LOW_VOL"},
                       ["AAPL", "RDDT", "SPY"], _aux(),
                       strategy_universes=strategy_universes)
    finally:
        eng.is_eligible, eng.instrument_class_for = orig_elig, orig_ic
    return strat.calls[0]


def test_gate_off_identical_inputs():
    call = _run(None)
    assert call["cols"] == ["AAPL", "RDDT", "SPY"]
    assert call["universe"] == ["AAPL", "RDDT", "SPY"]
    assert call["fin_keys"] == ["AAPL", "RDDT", "SPY"]


def test_gate_on_slices_prices_universe_and_aux():
    call = _run({"S_x": ["AAPL", "SPY"]})
    assert call["cols"] == ["AAPL", "SPY"]            # RDDT column gone
    assert call["universe"] == ["AAPL", "SPY"]
    assert call["fin_keys"] == ["AAPL", "SPY"]        # ticker-keyed aux sliced


def test_slice_aux_helper():
    from execution.engine import _slice_aux
    out = _slice_aux(_aux(), {"AAPL"})
    assert sorted(out["financials"]) == ["AAPL"]
    assert sorted(out["insider_txns"]) == ["AAPL"]
    assert "vix" in out["macro"]                       # macro passes whole
    assert list(out["prices_30m"]["ticker"]) == ["AAPL"]
