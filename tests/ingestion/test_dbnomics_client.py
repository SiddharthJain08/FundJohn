"""Phase 1F — DBnomics client tests with recorded fixtures (no network)."""
import json
from datetime import date
from unittest.mock import patch

from src.ingestion.dbnomics_client import DBnomicsClient


def _fixture():
    """Minimal DBnomics v22 series response shape."""
    return {
        "series": {
            "docs": [{
                "provider_code": "IMF",
                "dataset_code":  "IFS",
                "series_code":   "M.US.PCPI_PC_PP_PT",
                "@frequency":    "monthly",
                "period":            ["2026-01", "2026-02", "2026-03"],
                "period_start_day":  ["2026-01-01", "2026-02-01", "2026-03-01"],
                "value":             [3.1, 3.2, 3.0],
            }],
            "num_found": 1,
        }
    }


def test_get_series_parses_observations():
    payload = json.dumps(_fixture()).encode()
    # _http_retry.py does `from urllib.request import urlopen`, so the mock
    # has to target its local binding, not the original urllib.request.urlopen.
    with patch("src.ingestion._http_retry.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        c = DBnomicsClient()
        obs = c.get_series("IMF/IFS/M.US.PCPI_PC_PP_PT")
    assert len(obs) == 3
    assert obs[0]["period"] == "2026-01"
    assert obs[0]["value"] == 3.1
    assert obs[0]["series_code"] == "M.US.PCPI_PC_PP_PT"
    # Phase 1F#13 — new keys populated.
    assert obs[0]["period_start"] == date(2026, 1, 1)
    assert obs[0]["frequency"] == "monthly"
    assert obs[2]["period_start"] == date(2026, 3, 1)


def test_get_series_handles_null_values():
    fx = _fixture()
    fx["series"]["docs"][0]["value"] = [3.1, None, 3.0]
    payload = json.dumps(fx).encode()
    # _http_retry.py does `from urllib.request import urlopen`, so the mock
    # has to target its local binding, not the original urllib.request.urlopen.
    with patch("src.ingestion._http_retry.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        obs = DBnomicsClient().get_series("IMF/IFS/M.US.PCPI_PC_PP_PT")
    assert obs[1]["value"] is None
    # Even with a NULL value, period_start + frequency should still be set.
    assert obs[1]["period_start"] == date(2026, 2, 1)
    assert obs[1]["frequency"] == "monthly"


def test_get_series_falls_back_when_period_start_day_absent():
    """Older DBnomics responses (or unusual datasets) may omit period_start_day.
    The client should fall back to parsing the `period` string directly."""
    fx = _fixture()
    del fx["series"]["docs"][0]["period_start_day"]
    payload = json.dumps(fx).encode()
    with patch("src.ingestion._http_retry.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        obs = DBnomicsClient().get_series("IMF/IFS/M.US.PCPI_PC_PP_PT")
    # Fallback parser still resolves '2026-01' -> 2026-01-01.
    assert obs[0]["period_start"] == date(2026, 1, 1)
    assert obs[0]["frequency"] == "monthly"
