"""tests/test_close_proxy_snapshot.py — close[t]-proxy snapshot util (mock-only)."""
from __future__ import annotations
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from ingestion import close_proxy_snapshot as cps  # noqa: E402
from ingestion.close_proxy_snapshot import fetch_close_proxy, CloseProxyError  # noqa: E402


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
