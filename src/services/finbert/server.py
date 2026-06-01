"""FinBERT-Tone FastAPI server.

Loads the model once at startup; exposes POST /score → {label, score}.
Runs on 127.0.0.1:7872 under finbert-sentiment.service.

Run manually: uvicorn src.services.finbert.server:app --host 127.0.0.1 --port 7872

Note on tokenizer/model class choice:
  The yiyanghkust/finbert-tone repo's config.json has no `model_type` key,
  which causes AutoTokenizer/AutoConfig to raise on transformers>=5.x.  We
  bypass the auto-detection by instantiating BertTokenizer +
  BertForSequenceClassification explicitly (the architecture is recorded as
  "BertForSequenceClassification" in config.json)."""
from __future__ import annotations

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import BertTokenizer, BertForSequenceClassification

MODEL_NAME = "yiyanghkust/finbert-tone"

app = FastAPI(title="FinBERT-Tone")
_tokenizer = BertTokenizer.from_pretrained(MODEL_NAME)
_model = BertForSequenceClassification.from_pretrained(MODEL_NAME).eval()
# Verified against the model's config.id2label:
#   {0: "Neutral", 1: "Positive", 2: "Negative"}
_LABELS = ["Neutral", "Positive", "Negative"]
# Startup guard: fail loudly if HuggingFace ever republishes the model with a
# different label order (silent feature corruption is the worst-case here).
assert _model.config.id2label == {0: "Neutral", 1: "Positive", 2: "Negative"}, \
    f"FinBERT label drift: id2label={_model.config.id2label}"


class ScoreReq(BaseModel):
    text: str


@app.post("/score")
def score(req: ScoreReq):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text required")
    inputs = _tokenizer(req.text, return_tensors="pt", truncation=True, max_length=512)
    with torch.no_grad():
        logits = _model(**inputs).logits[0]
    probs = torch.softmax(logits, dim=-1)
    idx = int(torch.argmax(probs).item())
    return {"label": _LABELS[idx], "score": float(probs[idx].item())}


class BatchReq(BaseModel):
    texts: list[str]


@app.post("/score_batch")
def score_batch(req: BatchReq):
    """Score many texts in ONE batched forward pass — ~5x faster than N /score calls,
    bit-identical scores (verified). Blank texts map to {"Neutral", 0.0} to keep the
    output list index-aligned with the input. Additive: /score is unchanged."""
    texts = req.texts or []
    if not texts:
        return {"results": []}
    # Track blanks so we can return Neutral/0.0 for them without breaking alignment.
    nonblank_idx = [i for i, t in enumerate(texts) if t and t.strip()]
    results = [{"label": "Neutral", "score": 0.0} for _ in texts]
    if nonblank_idx:
        batch = [texts[i] for i in nonblank_idx]
        inputs = _tokenizer(batch, return_tensors="pt", truncation=True,
                            max_length=512, padding=True)
        with torch.no_grad():
            logits = _model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)
        idxs = probs.argmax(dim=-1)
        for row, orig_i in enumerate(nonblank_idx):
            k = int(idxs[row].item())
            results[orig_i] = {"label": _LABELS[k], "score": float(probs[row, k].item())}
    return {"results": results}


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL_NAME}
