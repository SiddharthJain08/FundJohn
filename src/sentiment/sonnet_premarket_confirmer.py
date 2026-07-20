"""Sonnet 4.6 pre-market panic confirmer.

Pattern: subprocess to /usr/local/bin/claude-bin, --output-format json,
--max-budget-usd, JSON-with-fenced-fallback parse, fail-open on any error
(verdict='llm_error'). This is now the sole LLM news-veto path — the inline
sizer confirmer (formerly execution/tradejohn_confirmer.py) was retired 2026-07-20.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass


PANIC_VERDICTS = (
    'bullish', 'neutral', 'bearish_news_driven', 'bearish_idiosyncratic',
)

DEFAULT_MAX_BUDGET_USD = 0.50
DEFAULT_MODEL = 'sonnet'

_JSON_OBJ_RE = re.compile(r'\{[\s\S]*\}')


@dataclass(frozen=True)
class PremarketConfirmerInput:
    ticker: str
    held_qty: float
    panic_score: float
    news_count: int
    finbert_neg_ratio: float
    social_bear_ratio: float
    top_headlines: list[tuple[str, float, str]]


@dataclass(frozen=True)
class PremarketConfirmerResult:
    verdict: str
    severity: int | None
    rationale: str
    evidence_uuids: list[str]
    cost_usd: float | None


_PROMPT_TEMPLATE = """You are a pre-market risk reviewer for a trading desk.

A rule-based scanner flagged the following ticker for potential panic-selling
risk. Inspect the headlines and decide whether the news is a genuine,
idiosyncratic, bearish catalyst that warrants flattening the position before
the open, or whether it is routine noise that does NOT justify action.

Ticker: {ticker}
Held qty (signed): {held_qty}
Composite panic_score (0-100): {panic_score:.1f}
News count in pre-market window: {news_count}
FinBERT negative ratio: {finbert_neg_ratio:.2f}
Social bear ratio: {social_bear_ratio:.2f}

Top headlines (with FinBERT score; negative = bearish):
{headline_block}

Reply with ONLY a single JSON object, no prose, no markdown fences:
{{
  "panic_verdict": "bullish" | "neutral" | "bearish_news_driven" | "bearish_idiosyncratic",
  "severity": 1..5,
  "rationale": "<2-3 sentences citing the specific headlines>",
  "evidence_uuids": ["<uuid>", ...]
}}

Definitions:
- bearish_news_driven: hard catalyst (guidance cut, CFO/CEO departure, fraud,
  major customer loss, regulatory action, going-concern doubt, M&A break).
- bearish_idiosyncratic: company-specific bearish pressure that isn't a hard
  named catalyst but is real and unlikely to mean-revert by close (e.g.,
  multi-source downgrades, sector-relative weakness with a named driver).
- neutral: routine, mixed, or noise (price-target tweak with no thesis change,
  brokerage upgrade-then-downgrade noise, generic sector comment).
- bullish: the news is on balance positive even if FinBERT scored it negative.

Severity scale (apply only to bearish_* verdicts; use 1 otherwise):
1 = mild, fade-by-noon likely
3 = real but limited downside
5 = hard catalyst, flatten-by-open is defensible
"""


def _build_prompt(inp: PremarketConfirmerInput) -> str:
    if inp.top_headlines:
        headline_block = '\n'.join(
            f'  - [{score:+.2f}] {headline}  (uuid={uuid})'
            for headline, score, uuid in inp.top_headlines
        )
    else:
        headline_block = '  (no headlines available)'
    return _PROMPT_TEMPLATE.format(
        ticker=inp.ticker,
        held_qty=inp.held_qty,
        panic_score=inp.panic_score,
        news_count=inp.news_count,
        finbert_neg_ratio=inp.finbert_neg_ratio,
        social_bear_ratio=inp.social_bear_ratio,
        headline_block=headline_block,
    )


def _extract_inner_json(body: str) -> dict | None:
    body = body.strip()
    fenced = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', body)
    if fenced:
        body = fenced.group(1)
    else:
        match = _JSON_OBJ_RE.search(body)
        if match:
            body = match.group(0)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def confirm_panic(inp: PremarketConfirmerInput,
                  max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
                  model: str = DEFAULT_MODEL) -> PremarketConfirmerResult:
    """Invoke claude-bin Sonnet, parse, return a typed result. Never raises."""
    prompt = _build_prompt(inp)

    proc = subprocess.run(
        [
            '/usr/local/bin/claude-bin',
            '--print',
            '--output-format', 'json',
            '--model', model,
            '--max-budget-usd', f'{max_budget_usd:.2f}',
        ],
        input=prompt.encode(),
        capture_output=True,
        timeout=300,
    )

    if proc.returncode != 0:
        return PremarketConfirmerResult(
            verdict='llm_error',
            severity=None,
            rationale=f'claude-bin exit {proc.returncode}: {proc.stderr[:200].decode(errors="replace")}',
            evidence_uuids=[],
            cost_usd=None,
        )

    try:
        outer = json.loads(proc.stdout.decode())
        cost = float(outer.get('total_cost_usd') or 0.0)
        inner = _extract_inner_json(outer.get('result', ''))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return PremarketConfirmerResult(
            verdict='llm_error', severity=None,
            rationale=f'outer parse: {e}', evidence_uuids=[], cost_usd=None,
        )

    if inner is None:
        return PremarketConfirmerResult(
            verdict='llm_error', severity=None,
            rationale='inner JSON not found in Sonnet output',
            evidence_uuids=[], cost_usd=cost,
        )

    verdict = str(inner.get('panic_verdict', '')).strip()
    if verdict not in PANIC_VERDICTS:
        return PremarketConfirmerResult(
            verdict='llm_error', severity=None,
            rationale=f'unknown verdict {verdict!r}', evidence_uuids=[],
            cost_usd=cost,
        )

    try:
        severity = int(inner.get('severity'))
    except (TypeError, ValueError):
        severity = None

    rationale = str(inner.get('rationale', '')).strip()
    evidence = [str(u) for u in (inner.get('evidence_uuids') or [])]

    return PremarketConfirmerResult(
        verdict=verdict,
        severity=severity,
        rationale=rationale,
        evidence_uuids=evidence,
        cost_usd=cost,
    )
