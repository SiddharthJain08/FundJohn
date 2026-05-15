"""Phase 1F — Polymarket client tests with recorded fixtures (no network)."""
import json
from unittest.mock import patch

from src.ingestion.polymarket_client import PolymarketClient


def _markets_fixture():
    return [{
        "id": "0xabc123",
        "question": "Will the Fed cut rates by July 2026?",
        "endDate": "2026-07-31T23:59:59Z",
        "outcomePrices": ["0.62", "0.38"],
        "volume24hr": 145200.50,
    }]


def test_list_active_markets_parses_outcomes():
    payload = json.dumps(_markets_fixture()).encode()
    # _http_retry.py does `from urllib.request import urlopen`, so the mock
    # has to target its local binding, not the original urllib.request.urlopen.
    with patch("src.ingestion._http_retry.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = payload
        mock_open.return_value.__enter__.return_value.status = 200
        markets = PolymarketClient().list_active_markets(limit=10)
    assert len(markets) == 1
    m = markets[0]
    assert m["market_id"] == "0xabc123"
    assert m["yes_price"] == 0.62
    assert m["no_price"] == 0.38
    assert m["volume_24h_usd"] == 145200.50
