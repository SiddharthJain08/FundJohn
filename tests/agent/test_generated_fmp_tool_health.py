"""The generated FMP tool module (src/agent/tools/mcp/fmp.js → workspaces/
default/tools/fmp.py) is the shared `_get` behind backfillers/fmp.py,
intraday_financials.py and every sub-agent FMP call. It must (1) record each
call into data_provider_health and (2) tell a tier-gated SYMBOL 402 apart
from a quota 402 instead of raising 'free tier limit hit' for both."""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _generate_module(monkeypatch, tmp_path):
    src = subprocess.check_output(
        ['node', '-e', "const {generatePython}=require('./src/agent/tools/mcp/fmp.js');"
                       "process.stdout.write(generatePython({name:'fmp',description:'t'}))"],
        cwd=ROOT, text=True)
    # stub the sibling rate limiter the generated module imports
    rl = types.ModuleType('_rate_limiter')
    rl._acquire_token = lambda p: None
    rl._cycle_cache_get = lambda *a, **k: None
    rl._cycle_cache_set = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, '_rate_limiter', rl)
    mod = types.ModuleType('fmp_generated')
    mod.__file__ = str(tmp_path / 'fmp.py')
    exec(compile(src, mod.__file__, 'exec'), mod.__dict__)
    return mod


class _Resp:
    def __init__(self, status, body='', json=None):
        self.status_code = status; self.text = body; self._json = json if json is not None else []
    def json(self): return self._json
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'{self.status_code} error')


def test_generated_get_records_and_classifies(monkeypatch, tmp_path):
    mod = _generate_module(monkeypatch, tmp_path)
    calls = []
    from src.maintenance import provider_health as ph
    monkeypatch.setattr(ph, 'record', lambda p, e, *, success, error=None: calls.append((p, e, success, error)))

    responses = {
        'income-statement': _Resp(200, '[]', [{'date': '2026-06-30'}]),
        'ratios': _Resp(402, "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available under your current subscription"),
        'quote': _Resp(402, 'Limit Reach . Please upgrade your plan'),
        'profile': _Resp(429, 'Too Many Requests'),
    }
    monkeypatch.setattr(mod.requests, 'get', lambda url, params=None, timeout=None: responses[url.rsplit('/', 1)[1]])

    assert mod._get('income-statement', {'symbol': 'AAPL'}) == [{'date': '2026-06-30'}]
    with pytest.raises(mod.FmpSymbolGated):
        mod._get('ratios', {'symbol': 'ABR.PRD'})
    with pytest.raises(RuntimeError, match='quota'):
        mod._get('quote', {'symbol': 'AAPL'})
    with pytest.raises(RuntimeError):
        mod._get('profile', {'symbol': 'AAPL'})

    assert calls == [
        ('fmp', 'income_statement', True, None),
        ('fmp', 'ratios', True, None),                       # gated symbol: provider healthy
        ('fmp', 'quote', False, 'HTTP 402: Limit Reach . Please upgrade your plan'),
        ('fmp', 'profile', False, 'HTTP 429: Too Many Requests'),
    ]


def test_symbol_gated_is_still_a_runtime_error_for_legacy_handlers(monkeypatch, tmp_path):
    mod = _generate_module(monkeypatch, tmp_path)
    assert issubclass(mod.FmpSymbolGated, RuntimeError)
