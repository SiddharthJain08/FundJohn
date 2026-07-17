"""Phase 1D — FinBERT client tests.  Mocks the HTTP layer; no service required."""
from unittest.mock import patch
import pytest


def test_score_returns_label_and_score():
    from src.services.finbert.client import FinbertClient
    fake = {"label": "Positive", "score": 0.92}
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = (
            b'{"label":"Positive","score":0.92}'
        )
        mock_open.return_value.__enter__.return_value.status = 200
        c = FinbertClient(base_url="http://127.0.0.1:7872")
        out = c.score("Apple beats earnings, raises guidance")
        assert out == fake


def test_score_raises_on_empty_text():
    from src.services.finbert.client import FinbertClient
    c = FinbertClient(base_url="http://127.0.0.1:7872")
    with pytest.raises(ValueError):
        c.score("")
