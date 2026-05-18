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


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL_NAME}
