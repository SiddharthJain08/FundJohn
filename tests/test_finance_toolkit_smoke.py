"""Phase 1C — FinanceToolkit smoke: confirm the lib initializes and returns a
ratios DataFrame for one ticker using FMP credentials we already hold."""
import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("FMP_API_KEY"),
    reason="Requires FMP_API_KEY in environment (already set in production .env)",
)


def test_ratios_for_aapl_returns_nonempty_frame():
    from src.services.finance_toolkit_smoke import get_ratios_for
    df = get_ratios_for("AAPL", years=3)
    assert not df.empty
    expected = {"Current Ratio", "Debt-to-Equity Ratio", "Return on Equity"}
    assert expected & set(df.index), f"None of {expected} found; got {set(df.index)}"
