"""TradeJohn confirmer — per-ticker keep/cancel LLM call.

Invoked by regime_blended_sizer's sharpe-cadence path on the
new-exposure delta emissions (orphan closes and flip closes bypass
the confirmer entirely — closes always execute). Outputs per-ticker
{action: keep|cancel, rationale}.

The legacy `scale` action was removed 2026-05-14 when the sizer
became authoritative for size — the confirmer is news-veto-only.
Legacy invocation from the LOW_VOL/TRANSITIONING consolidate path was
deleted with the ticker_consolidator module 2026-05-21.

Fail-OPEN behavior: any LLM failure (timeout, parse error, missing
ticker in response) defaults the affected ticker to keep so the
formula-result rides through. The cycle continues; a :warning: is
posted to #botjohn-log for operator awareness.

Spec: docs/archive/superpowers/specs/2026-05-11-regime-blended-position-sizing-design.md §11
"""
from __future__ import annotations
import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / 'agent' / 'prompts' / 'subagents' / 'tradejohn-confirmer.md'
CLAUDE_BIN = '/usr/local/bin/claude-bin'
# Per-call dollar cap. Replaces the legacy --max-tokens flag (removed from
# claude-bin). Sonnet output is ~$15/M, input ~$3/M; CLAUDE.md/auto-memory
# cache overhead ~$0.05; up to ~50 tickers/call with rationales fits well
# under $0.50.
DEFAULT_MAX_BUDGET_USD = 0.50
DEFAULT_MODEL = 'sonnet'
FAIL_OPEN_DEFAULT = {'action': 'keep', 'rationale': 'fail_open_default'}

def _strip_sentiment_section(template: str) -> str:
    """Remove the `## Sentiment & News Inputs` block from the template.

    Walks lines, skipping from the `## Sentiment & News Inputs` header
    until (but not including) the next `## ` header — or EOF if none.
    Inclusive of the stripped header; exclusive of the next section.
    """
    out = []
    skipping = False
    for line in template.splitlines(keepends=True):
        stripped = line.lstrip()
        if not skipping and stripped.startswith('## Sentiment & News Inputs'):
            skipping = True
            continue
        if skipping and stripped.startswith('## ') and not stripped.startswith('## Sentiment & News Inputs'):
            skipping = False
            out.append(line)
            continue
        if skipping:
            continue
        out.append(line)
    return ''.join(out)


def _build_prompt(proposals: list[dict]) -> str:
    """Compose the per-cycle prompt from the static template + per-ticker proposals.

    Gated on OPENCLAW_CONFIRMER_SENTIMENT=1. When the gate is OFF (default),
    the `## Sentiment & News Inputs` section is stripped from the template AND
    any `sentiment` key on each proposal is dropped — so the prompt + payload
    are byte-equivalent to pre-Task-11 behavior.
    """
    gate_on = os.environ.get('OPENCLAW_CONFIRMER_SENTIMENT') == '1'
    template = PROMPT_PATH.read_text() if PROMPT_PATH.exists() else _FALLBACK_TEMPLATE
    if not gate_on:
        template = _strip_sentiment_section(template)
        proposals = [
            {k: v for k, v in p.items() if k != 'sentiment'}
            for p in proposals
        ]
    payload = {'proposals': proposals}
    return template + '\n\n## INPUT\n```json\n' + json.dumps(payload, indent=2, default=str) + '\n```'

_FALLBACK_TEMPLATE = """You are TradeJohn, a per-ticker risk gate. Upstream sizing is
fully formulaic. Your only action is to cancel orders on tickers with highly
alarming, ticker-specific news. Default to keep; cancel < 5% of tickers per cycle.
Respond with strict JSON: a top-level object keyed by uppercase ticker,
each value {"action": "keep" | "cancel", "rationale": "≤ 200 chars"}.
Do not include a multiplier field."""

def _default_runner(prompt: str, max_budget_usd: float = DEFAULT_MAX_BUDGET_USD) -> str:
    """Spawn claude-bin and return stdout. Raises on non-zero exit."""
    proc = subprocess.run(
        [
            CLAUDE_BIN,
            '--print',
            '--output-format', 'json',
            '--model', DEFAULT_MODEL,
            '--max-budget-usd', f'{max_budget_usd:.2f}',
        ],
        input=prompt.encode(), capture_output=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'claude-bin exit {proc.returncode}: {proc.stderr[:200].decode()}')
    return proc.stdout.decode()

_JSON_OBJ_RE = re.compile(r'\{[\s\S]*\}')

def _extract_inner_json(body: str) -> dict | None:
    """Pull the per-ticker JSON object out of a string body.

    The confirmer prompt asks for strict JSON, but models sometimes wrap it
    in ```json fences or prefix it with reasoning. Strip fences, then fall
    back to the largest {...} match.
    """
    body = body.strip()
    # ```json ... ``` or ``` ... ```
    fenced = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', body)
    if fenced:
        body = fenced.group(1)
    else:
        m = _JSON_OBJ_RE.search(body)
        if m:
            body = m.group(0)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None

def _parse_response(raw: str, expected_tickers: list[str]) -> dict[str, dict]:
    """Parse LLM JSON. Per-ticker malformed → fail-open. Total garbage → all fail-open."""
    try:
        # claude-bin --output-format=json wraps in {result: ..., ...}; handle both.
        outer = json.loads(raw)
        body = outer.get('result', outer) if isinstance(outer, dict) else outer
        if isinstance(body, str):
            inner = _extract_inner_json(body)
            if inner is None:
                raise json.JSONDecodeError('no JSON object found in result body', body, 0)
            body = inner
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('tradejohn_confirmer: failed to parse LLM response (%s); fail-open all', e)
        return {t: dict(FAIL_OPEN_DEFAULT) for t in expected_tickers}

    out = {}
    for ticker in expected_tickers:
        entry = body.get(ticker) if isinstance(body, dict) else None
        if not isinstance(entry, dict):
            logger.warning('tradejohn_confirmer: ticker %s missing or malformed; fail-open', ticker)
            out[ticker] = dict(FAIL_OPEN_DEFAULT)
            continue
        action = entry.get('action', 'keep')
        # Legacy compat: prior schema used approve|veto|scale. Map to the
        # new keep|cancel set so an older confirmer call site keeps working.
        if action == 'approve': action = 'keep'
        elif action == 'veto':  action = 'cancel'
        elif action == 'scale': action = 'keep'  # we no longer adjust size
        if action not in ('keep', 'cancel'):
            logger.warning('tradejohn_confirmer: ticker %s invalid action %r; fail-open to keep', ticker, action)
            out[ticker] = dict(FAIL_OPEN_DEFAULT)
            continue
        out[ticker] = {'action': action,
                       'rationale': str(entry.get('rationale', ''))[:500]}
    return out

def confirm(proposals: list[dict], runner=None) -> dict[str, dict]:
    """Invoke LLM, return per-ticker {action, multiplier, rationale}."""
    if not proposals:
        return {}
    if runner is None:
        runner = _default_runner
    tickers = [p['ticker'] for p in proposals]
    prompt = _build_prompt(proposals)
    try:
        raw = runner(prompt, DEFAULT_MAX_BUDGET_USD)
    except Exception as e:
        logger.warning('tradejohn_confirmer: runner failed (%s); fail-open all tickers', e)
        return {t: dict(FAIL_OPEN_DEFAULT) for t in tickers}
    return _parse_response(raw, tickers)
