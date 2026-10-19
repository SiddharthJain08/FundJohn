import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import audit_strategy_lookbacks as a  # noqa: E402


def test_infer_literals():
    r = a.infer_lookback("x.rolling(252).mean(); y.shift(5); z.iloc[-300]")
    assert r["bars"] == 300


def test_infer_unbounded_and_non_literal():
    r = a.infer_lookback("s.ewm(span=20).mean(); s.rolling(self.n)")
    assert r["unbounded"] == ["ewm("]
    assert r["non_literal"] == 1


def test_declared_parsed():
    assert a.infer_lookback("max_lookback_days: int = 1500")["declared"] == 1500
    assert a.infer_lookback("x = 1")["declared"] is None


def test_audit_flags_exceeders(tmp_path):
    (tmp_path / "a.py").write_text("x.rolling(800).mean()")
    (tmp_path / "b.py").write_text("x.rolling(20).mean()")
    rows = a.audit(tmp_path)
    assert [r["exceeds"] for r in rows] == [True, False]
    assert rows[0]["inferred_days"] == 1166


def test_cli_exit_codes(tmp_path):
    (tmp_path / "a.py").write_text("x.rolling(800).mean()")
    assert a.main(["--impl-dir", str(tmp_path)]) == 0
    assert a.main(["--impl-dir", str(tmp_path), "--fail-on-exceed"]) == 1


def test_real_implementations_static_read():
    rows = a.audit(Path(__file__).resolve().parents[2] / "src/strategies/implementations")
    assert len(rows) > 100


def test_min_lookback_drives_engine_need(tmp_path):
    (tmp_path / "a.py").write_text("class S:\n    min_lookback = 1260\n")
    (tmp_path / "b.py").write_text("LB = 100\nclass S:\n    min_lookback = LB\n    max_lookback_days = 10\n    max_lookback_days = 20\n")
    (tmp_path / "c.py").write_text("class S:\n    min_lookback = self.x\n    y = z.rolling(int(self.n)).mean()\n")
    rows = {r["strategy_file"]: r for r in a.audit(tmp_path)}
    assert rows["a.py"]["exceeds"] and rows["a.py"]["engine_need_days"] == 2016
    assert rows["b.py"]["min_lookback"] == 100 and rows["b.py"]["declared"] == 20
    assert rows["c.py"]["non_literal"] == 2


def test_factor_matches_engine():
    import re as _re
    src = (Path(__file__).resolve().parents[2] / "src/execution/engine.py").read_text()
    m = _re.search(r"^LOOKBACK_CALENDAR_FACTOR\s*=\s*([\d.]+)", src, _re.M)
    assert float(m.group(1)) == a.LOOKBACK_CALENDAR_FACTOR


def test_fail_on_unverified(tmp_path):
    (tmp_path / "a.py").write_text("s.ewm(span=5)")
    assert a.main(["--impl-dir", str(tmp_path), "--fail-on-exceed"]) == 0
    assert a.main(["--impl-dir", str(tmp_path), "--fail-on-exceed", "--fail-on-unverified"]) == 1
