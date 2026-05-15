"""Thin facade over JerBouma/FinanceToolkit for the four modules we'll actually call:
ratios, models (DCF/WACC/Altman), performance, and risk.

We intentionally do NOT re-export the whole lib; this keeps the import surface small
and the dep upgrade-able without touching callers."""
from __future__ import annotations

import os
import pandas as pd
from financetoolkit import Toolkit


def _toolkit(ticker: str, years: int = 3) -> Toolkit:
    api_key = os.environ["FMP_API_KEY"]
    end_year = pd.Timestamp.utcnow().year
    return Toolkit(
        tickers=[ticker],
        api_key=api_key,
        start_date=f"{end_year - years}-01-01",
    )


def get_ratios_for(ticker: str, years: int = 3) -> pd.DataFrame:
    return _toolkit(ticker, years).ratios.collect_all_ratios()


def get_altman_z_for(ticker: str, years: int = 3) -> pd.DataFrame:
    return _toolkit(ticker, years).models.get_altman_z_score()


def get_dcf_for(ticker: str, years: int = 3) -> pd.DataFrame:
    return _toolkit(ticker, years).models.get_intrinsic_valuation()
