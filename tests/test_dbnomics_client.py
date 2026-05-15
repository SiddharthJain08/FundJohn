"""Phase 1F — DBnomics client tests with recorded fixtures (no network)."""
# NOTE: src/ingestion/__init__.py has a long-standing import error
# (`fetch_polygon_universe` not exported by pipeline.py — pre-dates this branch,
# see beea4cd on main). Mirror the importlib workaround used in
# tests/test_arxiv_discovery_categories.py to load the client module directly
# without tripping the unrelated package-init bug.
import importlib.util
import pathlib
import sys
from unittest.mock import patch
import json


# Pre-load _http_retry the same way arxiv_discovery.py does, so dbnomics_client's
# `from _http_retry import fetch_with_retry` resolves without tripping
# src.ingestion.__init__.
_INGESTION_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "ingestion"
sys.path.insert(0, str(_INGESTION_DIR))
# Only load _http_retry once across the test session — the sibling polymarket
# test file may have loaded it already, and a second module instance would
# break `patch("_http_retry.urlopen")` for whichever file ran first.
if "_http_retry" not in sys.modules:
    _retry_spec = importlib.util.spec_from_file_location(
        "_http_retry", _INGESTION_DIR / "_http_retry.py",
    )
    _http_retry = importlib.util.module_from_spec(_retry_spec)
    sys.modules["_http_retry"] = _http_retry
    _retry_spec.loader.exec_module(_http_retry)

_spec = importlib.util.spec_from_file_location(
    "dbnomics_client",
    _INGESTION_DIR / "dbnomics_client.py",
)
dbnomics_client = importlib.util.module_from_spec(_spec)
sys.modules["dbnomics_client"] = dbnomics_client
_spec.loader.exec_module(dbnomics_client)
DBnomicsClient = dbnomics_client.DBnomicsClient


def _fixture():
    """Minimal DBnomics v22 series response shape."""
    return {
        "series": {
            "docs": [{
                "provider_code": "IMF",
                "dataset_code": "IFS",
                "series_code": "M.US.PCPI_PC_PP_PT",
                "period": ["2026-01", "2026-02", "2026-03"],
                "value": [3.1, 3.2, 3.0],
            }],
            "num_found": 1,
        }
    }


def test_get_series_parses_observations():
    payload = json.dumps(_fixture()).encode()
    # _http_retry.py does `from urllib.request import urlopen`, so the mock
    # has to target its local binding, not the original urllib.request.urlopen.
    with patch("_http_retry.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        c = DBnomicsClient()
        obs = c.get_series("IMF/IFS/M.US.PCPI_PC_PP_PT")
    assert len(obs) == 3
    assert obs[0]["period"] == "2026-01"
    assert obs[0]["value"] == 3.1
    assert obs[0]["series_code"] == "M.US.PCPI_PC_PP_PT"


def test_get_series_handles_null_values():
    fx = _fixture()
    fx["series"]["docs"][0]["value"] = [3.1, None, 3.0]
    payload = json.dumps(fx).encode()
    # _http_retry.py does `from urllib.request import urlopen`, so the mock
    # has to target its local binding, not the original urllib.request.urlopen.
    with patch("_http_retry.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        obs = DBnomicsClient().get_series("IMF/IFS/M.US.PCPI_PC_PP_PT")
    assert obs[1]["value"] is None
