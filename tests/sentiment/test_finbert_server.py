"""Phase 1D — integration test that hits the live FinBERT service.

Marked 'integration' (per pytest.ini) — only run when the service is up.
Run: pytest -m integration tests/test_finbert_server.py"""
import pytest
import urllib.request


@pytest.mark.integration
def test_health_returns_ok():
    with urllib.request.urlopen("http://127.0.0.1:7872/health", timeout=5) as r:
        import json
        body = json.loads(r.read())
    assert body["ok"] is True


@pytest.mark.integration
def test_positive_news_scores_positive():
    from src.services.finbert.client import FinbertClient
    out = FinbertClient().score("Apple beats earnings, raises full-year guidance.")
    assert out["label"] == "Positive"
    assert out["score"] > 0.5
