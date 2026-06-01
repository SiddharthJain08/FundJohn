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
            if r.status != 200:
                raise RuntimeError(f"FinBERT service status {r.status}")
            return json.loads(r.read())

    def score_batch(self, texts: list[str]) -> list[dict]:
        """Score many texts in ONE batched forward pass (server-side) — ~5x faster than N
        single /score calls, identical scores. Returns a list of {label, score} aligned to
        `texts`. Empty/blank texts get {"Neutral", 0.0}. Raises if /score_batch is unavailable
        (404 on a service that predates this endpoint) — callers should fall back to .score().
        """
        if not texts:
            return []
        body = json.dumps({"texts": list(texts)}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/score_batch",
            data=body,
            method="POST",
            headers={"content-type": "application/json"},
        )
        # Scale timeout with batch size (one forward pass, but larger batches take longer).
        timeout = max(self.timeout, 0.2 * len(texts))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                raise RuntimeError(f"FinBERT service status {r.status}")
            return json.loads(r.read())["results"]
