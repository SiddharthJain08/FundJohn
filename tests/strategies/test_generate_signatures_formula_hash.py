"""C7c: generate_signatures.signature_for_source prefers an exact formula_hash."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from strategies import generate_signatures as g
from strategies import signal_expr

ROOT = Path(__file__).resolve().parents[2]
DSL_SRC = 'class A:\n    SIGNAL = "sma(close, 20) - close"\n    regimes = "LOW_VOL"\n'


def test_signature_for_source_prefers_formula_hash():
    sig = g.signature_for_source(DSL_SRC)
    assert sig['formula_hash'] == signal_expr.formula_hash('sma(close, 20) - close')
    assert sig['formula_tokens'] == g.extract_formula_tokens(DSL_SRC)
    assert sig['formula_ops'] == ['close', 'sma']
    ann = g.signature_for_source('class A:\n    SIGNAL: str = "sma(close, 20) - close"\n')
    assert ann['formula_hash'] == sig['formula_hash']


def test_non_dsl_source_unchanged():
    src = "x = 'zscore'\ny = 'LONG'\nHIGH_VOL\n"
    sig = g.signature_for_source(src)
    assert 'formula_hash' not in sig and 'formula_ops' not in sig
    assert list(sig) == ['regime_set_hash', 'direction_hash', 'formula_tokens', 'regimes']
    assert sig == {
        'regime_set_hash': g.sha256('HIGH_VOL'),
        'direction_hash': g.sha256('LONG'),
        'formula_tokens': g.extract_formula_tokens(src),
        'regimes': ['HIGH_VOL'],
    }
    assert sig['formula_tokens'] == ['zscore']


def test_main_not_called(tmp_path, monkeypatch):
    real = g.SIG_OUT
    before = real.stat().st_mtime_ns if real.exists() else None
    monkeypatch.setattr(g, 'SIG_OUT', tmp_path / 'sigs.json')
    g.signature_for_source(DSL_SRC)
    assert not (tmp_path / 'sigs.json').exists()
    assert (real.stat().st_mtime_ns if real.exists() else None) == before


def test_bare_script_import_path():
    code = ("import sys; sys.path[:0]=['%s/src/strategies']; import generate_signatures as g; "
            "print(g.signature_for_source(%r)['formula_hash'])" % (ROOT, DSL_SRC))
    out = subprocess.run(['env', '-u', 'PYTHONPATH', 'python3', '-c', code],
                         cwd='/', capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert re.fullmatch(r'[0-9a-f]{16}', out.stdout.strip())
