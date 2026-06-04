import json
from pathlib import Path
import pandas as pd
import pytest

from src.pipeline.backfillers.edgar_shares import (
    parse_shares_series, merge_append_only,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "edgar_companyfacts_sample.json"


def _facts():
    return json.loads(FIXTURE.read_text())


def test_parse_extracts_dei_and_gaap_series():
    rows = parse_shares_series("NVDA", _facts())
    dates = {r["asof_date"] for r in rows}
    # 3 distinct end-dates: 2023-10-29 (gaap), 2024-01-26, 2024-04-26 (dei)
    assert dates == {"2023-10-29", "2024-01-26", "2024-04-26"}


def test_parse_dedupes_same_end_date_preferring_latest_filed():
    rows = parse_shares_series("NVDA", _facts())
    apr = [r for r in rows if r["asof_date"] == "2024-04-26"]
    assert len(apr) == 1
    assert apr[0]["shares"] == 2462500000  # the 10-Q/A filed later wins


def test_parse_rejects_implausible_units():
    facts = _facts()
    facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"].append(
        {"end": "2024-07-26", "val": 12, "form": "10-Q", "filed": "2024-08-20"}
    )  # 12 shares — implausible, must be dropped
    rows = parse_shares_series("NVDA", facts)
    assert "2024-07-26" not in {r["asof_date"] for r in rows}


def test_merge_append_only_never_drops_or_mutates(tmp_path):
    pq = tmp_path / "shares_outstanding.parquet"
    existing = pd.DataFrame([
        {"ticker": "NVDA", "asof_date": "2024-01-26", "shares": 2464000000,
         "form": "10-K", "filed": "2024-02-21"},
    ])
    existing.to_parquet(pq, index=False)
    new_rows = [
        # duplicate (ticker, asof_date) with DIFFERENT value — must NOT overwrite
        {"ticker": "NVDA", "asof_date": "2024-01-26", "shares": 1,
         "form": "10-K", "filed": "2024-02-21"},
        {"ticker": "NVDA", "asof_date": "2024-04-26", "shares": 2462500000,
         "form": "10-Q/A", "filed": "2024-06-15"},
    ]
    added = merge_append_only(pq, new_rows)
    out = pd.read_parquet(pq)
    assert added == 1
    assert len(out) == 2
    jan = out[out.asof_date == "2024-01-26"].iloc[0]
    assert jan.shares == 2464000000  # original row untouched
