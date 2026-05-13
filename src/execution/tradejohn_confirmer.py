"""TradeJohn confirmer — per-ticker approve/veto/scale LLM call.

Invoked ONLY in LOW_VOL/TRANSITIONING regimes after ticker_consolidator
has produced per-ticker preliminary sizes. Outputs per-ticker
{action: approve|veto|scale, multiplier, rationale}.

Fail-OPEN behavior: any LLM failure (timeout, parse error, missing
ticker in response) defaults the affected ticker to approve@multiplier=1.0
so the formula-result rides through. The cycle continues; a :warning:
is posted to #botjohn-log for operator awareness.

Spec: docs/superpowers/specs/2026-05-11-regime-blended-position-sizing-design.md §11
"""
from __future__ import annotations
import json
import logging
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
FAIL_OPEN_DEFAULT = {'action': 'approve', 'multiplier': 1.0, 'rationale': 'fail_open_default'}

def _build_prompt(proposals: list[dict]) -> str:
    """Compose the per-cycle prompt from the static template + per-ticker proposals."""
    template = PROMPT_PATH.read_text() if PROMPT_PATH.exists() else _FALLBACK_TEMPLATE
    payload = {'proposals': proposals}
    return template + '\n\n## INPUT\n```json\n' + json.dumps(payload, indent=2, default=str) + '\n```'

_FALLBACK_TEMPLATE = """You are TradeJohn, a per-ticker position-sizing confirmer.
For each proposal in INPUT, output an action (approve | veto | scale)
and a multiplier (0 to 2). Respond with strict JSON: a top-level object
keyed by ticker, each value {"action", "multiplier", "rationale"}."""

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
        action = entry.get('action', 'approve')
        if action not in ('approve', 'veto', 'scale'):
            logger.warning('tradejohn_confirmer: ticker %s invalid action %r; fail-open', ticker, action)
            out[ticker] = dict(FAIL_OPEN_DEFAULT)
            continue
        try:
            multiplier = float(entry.get('multiplier', 1.0))
            multiplier = max(0.0, min(2.0, multiplier))  # clamp to [0, 2]
        except (TypeError, ValueError):
            multiplier = 1.0
        out[ticker] = {'action': action, 'multiplier': multiplier,
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
