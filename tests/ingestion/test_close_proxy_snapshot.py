"""tests/test_close_proxy_snapshot.py — close[t]-proxy snapshot util (mock-only)."""
from __future__ import annotations
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from ingestion import close_proxy_snapshot as cps  # noqa: E402
from ingestion.close_proxy_snapshot import fetch_close_proxy, CloseProxyError  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_subprocess_run():
    """Undo this file's `cps.subprocess.run = ...` stubs after every test.

    close_proxy_snapshot does a plain `import subprocess`, so `cps.subprocess`
    IS the global module object — `cps.subprocess.run = fake` rebinds
    subprocess.run for the ENTIRE pytest process, not just this module
    (verified: `cps.subprocess is subprocess` -> True). Nothing here restored
    it, so whichever stub ran last leaked into every later test that shells
    out.

    That was the whole cross-suite failure: running
    `tests/ingestion/ tests/execution/ tests/system_checks/` together left the
    line-184 stub ("warning: using cached creds\\n{SPY...}") installed, and 15
    downstream tests in test_option_hedge / test_orchestrator_* /
    test_papermint_coverage_check / test_universe_shadow_parity_check /
    test_dry_run_dataflow got that canned SPY snapshot back from their own
    subprocess.run calls instead of real output. All 39 passed in isolation,
    which is what made it look environmental for so long.

    Autouse so it also covers the unittest.TestCase classes below.
    """
    original = subprocess.run
    try:
        yield
    finally:
        subprocess.run = original
        cps.subprocess.run = original


def _fake_run(stdout: str = "", rc: int = 0, capture: dict | None = None):
    def run(args, **kw):
        if capture is not None:
            capture["args"] = args
        return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")
    return run


class TestFetchCloseProxy(unittest.TestCase):
    def test_basic_latest_trade_prices(self):
        snap = {"AAPL": {"latestTrade": {"p": 150.0}}, "MSFT": {"latestTrade": {"p": 300.0}}}
        cps.subprocess.run = _fake_run(json.dumps(snap))
        out = fetch_close_proxy(["AAPL", "MSFT"], None)
        self.assertEqual(out, {"AAPL": 150.0, "MSFT": 300.0})

    def test_missing_ticker_is_omitted_not_raised(self):
        snap = {"AAPL": {"latestTrade": {"p": 150.0}}}  # MSFT absent from response
        cps.subprocess.run = _fake_run(json.dumps(snap))
        out = fetch_close_proxy(["AAPL", "MSFT"], None)
        self.assertEqual(out, {"AAPL": 150.0})

    def test_total_failure_raises(self):
        cps.subprocess.run = _fake_run("", rc=1)  # every chunk fails
        with self.assertRaises(CloseProxyError):
            fetch_close_proxy(["AAPL", "MSFT"], None)

    def test_valid_response_no_prices_raises(self):
        cps.subprocess.run = _fake_run(json.dumps({}))  # rc=0 but no prices at all
        with self.assertRaises(CloseProxyError):
            fetch_close_proxy(["AAPL", "MSFT"], None)

    def test_brk_class_share_normalization_roundtrip(self):
        cap: dict = {}
        snap = {"BRK.B": {"latestTrade": {"p": 400.0}}}
        cps.subprocess.run = _fake_run(json.dumps(snap), capture=cap)
        out = fetch_close_proxy(["BRK-B"], None)
        # requested with a dot (Alpaca convention)
        self.assertIn("BRK.B", ",".join(cap["args"]))
        # returned keyed by the engine ticker (dash)
        self.assertEqual(out, {"BRK-B": 400.0})

    def test_indices_and_futures_skipped(self):
        snap = {"AAPL": {"latestTrade": {"p": 150.0}}}
        cps.subprocess.run = _fake_run(json.dumps(snap))
        out = fetch_close_proxy(["AAPL", "^GSPC", "NG=F"], None)
        self.assertEqual(out, {"AAPL": 150.0})

    def test_fallback_to_minute_then_daily_bar(self):
        snap = {"AAPL": {"minuteBar": {"c": 151.0}}, "MSFT": {"dailyBar": {"c": 299.0}}}
        cps.subprocess.run = _fake_run(json.dumps(snap))
        out = fetch_close_proxy(["AAPL", "MSFT"], None)
        self.assertEqual(out, {"AAPL": 151.0, "MSFT": 299.0})

    def test_empty_universe_returns_empty_no_raise(self):
        out = fetch_close_proxy([], None)
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()


class TestChunkPoisonHardening(unittest.TestCase):
    """2026-07-29: one invalid symbol 400'd its whole 50-symbol chunk —
    near-total coverage loss on the 12.5k universe. Offenders are now
    pre-filtered by class and residuals evicted-and-retried."""

    def test_prefilter_preferreds_rights_and_long_dot_suffixes(self):
        self.assertIsNone(cps._to_alpaca_equity("CMS-PRC"))
        self.assertIsNone(cps._to_alpaca_equity("ATH-PRD"))
        self.assertIsNone(cps._to_alpaca_equity("PLUN-RT"))
        self.assertIsNone(cps._to_alpaca_equity("FOO-WS"))
        self.assertIsNone(cps._to_alpaca_equity("BAR-UN"))
        self.assertIsNone(cps._to_alpaca_equity("DX-Y.NYB"))
        # Class shares still convert; plain symbols untouched.
        self.assertEqual(cps._to_alpaca_equity("BRK-B"), "BRK.B")
        self.assertEqual(cps._to_alpaca_equity("AAPL"), "AAPL")

    def test_invalid_symbol_evicted_and_chunk_retried(self):
        calls = []

        def run(args, **kw):
            calls.append(args)
            syms = args[args.index("--symbols") + 1]
            if "BADSYM" in syms:
                return types.SimpleNamespace(
                    returncode=1, stdout="",
                    stderr='{"error":"code=400, message=invalid symbol: BADSYM"}')
            snap = {"AAPL": {"latestTrade": {"p": 150.0}}}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(snap), stderr="")

        cps.subprocess.run = run
        out = fetch_close_proxy(["AAPL", "BADSYM"], None)
        self.assertEqual(out, {"AAPL": 150.0})
        self.assertEqual(len(calls), 2)  # poisoned attempt + retry without offender


class TestSameDayGuardAndCoverage(unittest.TestCase):
    def test_stale_trade_rejected_in_asof_mode(self):
        snap = {"AAPL": {"latestTrade": {"p": 150.0, "t": "2026-07-29T19:00:01Z"}},
                "OLDY": {"latestTrade": {"p": 9.0, "t": "2026-07-21T15:00:00Z"}}}
        cps.subprocess.run = _fake_run(json.dumps(snap))
        out = fetch_close_proxy(["AAPL", "OLDY"], "2026-07-29", min_coverage=0.0)
        self.assertEqual(out, {"AAPL": 150.0})

    def test_untimestamped_node_rejected_in_asof_mode(self):
        snap = {"AAPL": {"latestTrade": {"p": 150.0}}}
        cps.subprocess.run = _fake_run(json.dumps(snap))
        with self.assertRaises(CloseProxyError):
            fetch_close_proxy(["AAPL"], "2026-07-29", min_coverage=0.9)

    def test_legacy_none_asof_accepts_untimestamped(self):
        snap = {"AAPL": {"latestTrade": {"p": 150.0}}}
        cps.subprocess.run = _fake_run(json.dumps(snap))
        out = fetch_close_proxy(["AAPL"], None)
        self.assertEqual(out, {"AAPL": 150.0})

    def test_coverage_floor_raises(self):
        # Denominator is provider-RETURNED symbols (2026-07-29): MSFT/GOOG are
        # returned but unpriced today, so coverage = 1/3 < the 0.5 floor.
        snap = {"AAPL": {"latestTrade": {"p": 150.0, "t": "2026-07-29T19:00:01Z"}},
                "MSFT": {"latestTrade": {"p": 300.0, "t": "2026-07-20T19:00:01Z"}},
                "GOOG": {"latestTrade": {"p": 200.0, "t": "2026-07-20T19:00:01Z"}}}
        cps.subprocess.run = _fake_run(json.dumps(snap))
        with self.assertRaises(CloseProxyError):
            fetch_close_proxy(["AAPL", "MSFT", "GOOG"], "2026-07-29",
                              min_coverage=0.5)

    def test_delisted_symbols_absent_from_response_do_not_count(self):
        # A 10-year panel carries delisted tickers; when the provider omits
        # them entirely they must not drag coverage down (they are not a
        # signal that the snapshot service is degraded).
        snap = {"AAPL": {"latestTrade": {"p": 150.0, "t": "2026-07-29T19:00:01Z"}}}
        cps.subprocess.run = _fake_run(json.dumps(snap))
        out = fetch_close_proxy(["AAPL", "DEADCO", "GONEINC"], "2026-07-29",
                                min_coverage=0.9)
        self.assertEqual(out, {"AAPL": 150.0})

    def test_coverage_floor_passes_and_daily_bar_counts(self):
        snap = {"AAPL": {"latestTrade": {"p": 150.0, "t": "2026-07-29T19:00:01Z"}},
                "MSFT": {"dailyBar": {"c": 300.0, "t": "2026-07-29T04:00:00Z"}}}
        cps.subprocess.run = _fake_run(json.dumps(snap))
        out = fetch_close_proxy(["AAPL", "MSFT"], "2026-07-29", min_coverage=0.9)
        self.assertEqual(out, {"AAPL": 150.0, "MSFT": 300.0})


class TestRthConditionArrayParse(unittest.TestCase):
    """2026-07-29: during RTH a quote carries a condition LIST ("c": ["R"]).
    The old parser scanned for the first '[' ANYWHERE and sliced from it,
    landing mid-object → parse failure → every chunk empty → CloseProxyError
    → the 15:00 same-day chain would abort at signals. Measured live."""

    def test_object_payload_with_inner_condition_array(self):
        snap = {"SPY": {"latestQuote": {"ap": 739.64, "c": ["R"],
                                        "t": "2026-07-29T13:42:00Z"},
                        "latestTrade": {"p": 739.47,
                                        "t": "2026-07-29T13:42:01Z"}}}
        cps.subprocess.run = _fake_run(json.dumps(snap))
        self.assertEqual(fetch_close_proxy(["SPY"], None), {"SPY": 739.47})

    def test_list_payload_still_parses(self):
        cps.subprocess.run = _fake_run(json.dumps([]))
        # A bare list is not a snapshot dict — no prices, and the universe was
        # non-empty, so the loud error (not a silent empty) is correct.
        with self.assertRaises(CloseProxyError):
            fetch_close_proxy(["SPY"], None)

    def test_preamble_before_json_body_tolerated(self):
        snap = {"SPY": {"latestTrade": {"p": 739.47}}}
        cps.subprocess.run = _fake_run("warning: using cached creds\n" + json.dumps(snap))
        self.assertEqual(fetch_close_proxy(["SPY"], None), {"SPY": 739.47})
