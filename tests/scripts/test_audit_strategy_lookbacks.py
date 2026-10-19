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
