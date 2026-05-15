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


_spec = importlib.util.spec_from_file_location(
    "dbnomics_client",
    pathlib.Path(__file__).resolve().parents[1] / "src" / "ingestion" / "dbnomics_client.py",
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
    with patch("urllib.request.urlopen") as mock_open:
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
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        obs = DBnomicsClient().get_series("IMF/IFS/M.US.PCPI_PC_PP_PT")
    assert obs[1]["value"] is None
