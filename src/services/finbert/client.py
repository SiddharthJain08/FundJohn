"""HTTP client for the local FinBERT-Tone service.

Service runs at 127.0.0.1:7872 (finbert-sentiment.service).  Use this client
from MasterMind / dashboard / news-ingest paths instead of importing
transformers directly — keeps the model load cost off the caller process."""
from __future__ import annotations

import json
import urllib.request


class FinbertClient:
    def __init__(self, base_url: str = "http://127.0.0.1:7872", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def score(self, text: str) -> dict:
        if not text or not text.strip():
            raise ValueError("FinbertClient.score: empty text")
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/score",
            data=body,
            method="POST",
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            assert r.status == 200, f"FinBERT service status {r.status}"
            return json.loads(r.read())
