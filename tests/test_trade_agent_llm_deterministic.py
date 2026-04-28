"""tests/test_trade_agent_llm_deterministic.py

Integration tests for the deterministic-sizer path through
src/execution/trade_agent_llm.py. We monkeypatch the handoff read/write
+ subprocess.run + DB connection to keep the test hermetic.

Run:
    pytest tests/test_trade_agent_llm_deterministic.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))


def _structured_handoff(run_date='2026-04-27') -> dict:
    """Minimal structured handoff with one live signal + one prefiltered."""
    return {
        'cycle_date':   run_date,
        'generated_at': run_date + 'T18:02:15.960618+00:00',
        'regime':       {'state': 'TRANSITIONING', 'stress': 50.0, 'scale': 0.55},
        'portfolio':    {'portfolio_value': 1_000_000},
        'signals': [
            {
                'ticker':      'AAPL',
                'strategy_id': 'S_test',
                'direction':   'LONG',
                'entry':       100.0, 'stop': 95.0, 't1': 110.0,
                'p_t1':        0.7, 'ev_gbm': 0.04,
            },
        ],
        'prefiltered': [
            {'ticker': 'XOM', 'strategy_id': 'S_other',
             'reason': 'prefilter_negative_ev', 'ev': -0.012, 'p_t1': 0.3},
        ],
        'sigma_gate':        2.0,
        'd1_strategy_stats': {},
        'mastermind_rec':    None,
    }


def _write_handoff_file(workdir: Path, run_date: str, handoff: dict) -> Path:
    """The script reads handoffs from <ROOT>/output/handoffs/<date>_structured.json,
    via execution.handoff.read_handoff. We can't relocate ROOT cleanly, but we
    can monkeypatch read_handoff/write_handoff onto our tempdir.
    """
    out = workdir / 'output' / 'handoffs'
    out.mkdir(parents=True, exist_ok=True)
    p = out / f'{run_date}_structured.json'
    p.write_text(json.dumps(handoff))
    return p


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def fake_manifest(monkeypatch):
    """Patch trade_agent_llm.ROOT/.../manifest.json reads to return a
    tiny manifest with S_test='live'."""
    from execution import trade_agent_llm as tal

    fake = {'strategies': {'S_test': {'state': 'live'}}}
    fake_path = Path(tempfile.mkdtemp()) / 'manifest.json'
    fake_path.write_text(json.dumps(fake))

    # Patch the path resolution inside _run_deterministic_sizer.
    monkeypatch.setattr(tal, 'ROOT', fake_path.parent.parent)
    # Lay down the manifest where _run_deterministic_sizer expects it.
    target = tal.ROOT / 'src' / 'strategies' / 'manifest.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fake))
    return tal


@pytest.fixture
def hermetic_handoff_io(monkeypatch, tmp_path):
    """Patch execution.handoff to read/write under a tmpdir instead of
    real filesystem + Redis."""
    from execution import handoff as hf

    handoff_dir = tmp_path / 'handoffs'
    handoff_dir.mkdir()

    written: dict[str, dict] = {}
    # Seed it with the structured handoff.
    sh = _structured_handoff()
    (handoff_dir / f'{sh["cycle_date"]}_structured.json').write_text(json.dumps(sh))

    def _read(run_date: str, stage: str):
        if stage in written and written.get(f'{run_date}_{stage}'):
            return written[f'{run_date}_{stage}']
        p = handoff_dir / f'{run_date}_{stage}.json'
        if p.exists():
            return json.loads(p.read_text())
        return None

    def _write(run_date: str, stage: str, data):
        p = handoff_dir / f'{run_date}_{stage}.json'
        p.write_text(json.dumps(data, default=str))
        written[f'{run_date}_{stage}'] = data
        return True

    monkeypatch.setattr(hf, 'read_handoff', _read)
    monkeypatch.setattr(hf, 'write_handoff', _write)
    return {'dir': handoff_dir, 'written': written, 'read': _read}


@pytest.fixture
def no_postgres(monkeypatch):
    """Drop POSTGRES_URI so the idempotency check + veto_log writer skip
    cleanly without spinning up a real DB."""
    monkeypatch.delenv('POSTGRES_URI', raising=False)


@pytest.fixture
def no_subprocess(monkeypatch):
    """Replace subprocess.run with a sentinel that records every call.
    The deterministic path must NEVER reach subprocess.run."""
    calls: list = []
    real = subprocess.run

    def _spy(*args, **kwargs):
        calls.append({'args': args, 'kwargs': kwargs})
        # If something does invoke subprocess, fail loudly.
        raise AssertionError(
            f'subprocess.run invoked on deterministic path: args={args}')

    monkeypatch.setattr('subprocess.run', _spy)
    monkeypatch.setattr('execution.trade_agent_llm.subprocess.run', _spy)
    return calls


# ── Tests ────────────────────────────────────────────────────────────────

def test_deterministic_path_writes_sized_handoff(
    monkeypatch, hermetic_handoff_io, no_postgres, no_subprocess, fake_manifest,
):
    """OPENCLAW_DETERMINISTIC_TRADEJOHN=1 → sized handoff written without
    spawning node, payload.source='deterministic_sizer', orders[] non-empty."""
    monkeypatch.setenv('OPENCLAW_DETERMINISTIC_TRADEJOHN', '1')
    monkeypatch.setattr(sys, 'argv', ['trade_agent_llm.py', '--date', '2026-04-27'])

    # Re-execute the __main__ block as a script.
    code = (ROOT / 'src' / 'execution' / 'trade_agent_llm.py').read_text()
    with pytest.raises(SystemExit) as exc_info:
        exec(compile(code, 'trade_agent_llm.py', 'exec'),
             {'__name__': '__main__', '__file__': str(ROOT / 'src' / 'execution' / 'trade_agent_llm.py')})
    assert exc_info.value.code == 0

    # Sized handoff must be in the in-memory store now.
    sized = hermetic_handoff_io['written'].get('2026-04-27_sized')
    assert sized is not None
    assert sized.get('source') == 'deterministic_sizer'
    assert sized.get('cycle_date') == '2026-04-27'
    assert sized.get('total_green', 0) >= 1
    # No subprocess call recorded.
    assert no_subprocess == []


def test_prefilter_fold_into_vetoed(
    monkeypatch, hermetic_handoff_io, no_postgres, no_subprocess, fake_manifest,
):
    """prefiltered[] entries from the structured handoff get folded into
    the sized handoff's vetoed[] list."""
    monkeypatch.setenv('OPENCLAW_DETERMINISTIC_TRADEJOHN', '1')
    monkeypatch.setattr(sys, 'argv', ['trade_agent_llm.py', '--date', '2026-04-27'])

    code = (ROOT / 'src' / 'execution' / 'trade_agent_llm.py').read_text()
    with pytest.raises(SystemExit):
        exec(compile(code, 'trade_agent_llm.py', 'exec'),
             {'__name__': '__main__', '__file__': str(ROOT / 'src' / 'execution' / 'trade_agent_llm.py')})

    sized = hermetic_handoff_io['written'].get('2026-04-27_sized')
    assert sized is not None
    vetoed_tickers = {v.get('ticker') for v in sized.get('vetoed') or []}
    assert 'XOM' in vetoed_tickers, 'prefiltered XOM was not folded into vetoed[]'


def test_orders_have_alpaca_required_fields(
    monkeypatch, hermetic_handoff_io, no_postgres, no_subprocess, fake_manifest,
):
    """Every emitted order must carry the fields alpaca_executor reads:
    ticker, direction, entry, stop, t1, pct_nav."""
    monkeypatch.setenv('OPENCLAW_DETERMINISTIC_TRADEJOHN', '1')
    monkeypatch.setattr(sys, 'argv', ['trade_agent_llm.py', '--date', '2026-04-27'])

    code = (ROOT / 'src' / 'execution' / 'trade_agent_llm.py').read_text()
    with pytest.raises(SystemExit):
        exec(compile(code, 'trade_agent_llm.py', 'exec'),
             {'__name__': '__main__', '__file__': str(ROOT / 'src' / 'execution' / 'trade_agent_llm.py')})

    sized = hermetic_handoff_io['written'].get('2026-04-27_sized')
    for o in sized.get('orders', []):
        for required in ('ticker', 'direction', 'entry', 'stop', 't1', 'pct_nav'):
            assert required in o, f'order missing {required}: {o}'
