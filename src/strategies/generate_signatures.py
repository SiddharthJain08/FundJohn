#!/usr/bin/env python3
"""
Generates strategy_signatures.json — fingerprint hashes used by PaperHunter
for duplicate detection (Jaccard on formula_tokens + regime_set match).

Usage:
    cd /root/openclaw && python3 src/strategies/generate_signatures.py
"""
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

# cron-schedule.js runs this file as a bare script (sys.path[0] = src/strategies):
# make the `strategies` package importable (same shim as strategy_lint.py).
_SRC_DIR = str(Path(__file__).resolve().parents[1])
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

IMPL_DIR   = Path(__file__).parent / 'implementations'
MANIFEST   = Path(__file__).parent / 'manifest.json'
SIG_OUT    = Path(__file__).parent / 'strategy_signatures.json'

REGIME_TOKENS   = {'HIGH_VOL', 'LOW_VOL', 'NEUTRAL', 'TRANSITIONING', 'RISK_OFF', 'TREND'}
DIRECTION_TOKENS = {'LONG', 'SHORT', 'BUY_VOL', 'SELL_VOL', 'FLAT', 'HOLD'}

# Signal-relevant formula tokens to extract (lowercased words)
FORMULA_VOCAB = [
    'iv_spread', 'iv_rank', 'iv_crush', 'iv_term_structure', 'iv_surface',
    'vrp', 'gamma', 'theta', 'gex', 'vega', 'delta', 'skew', 'dispersion',
    'momentum', 'dual_momentum', 'regime', 'max_pain', 'insider', 'cluster_buy',
    'quality', 'value', 'zscore', 'reversion', 'mean_reversion', 'breakout',
    'rv', 'realized_vol', 'hv', 'atr', 'beta', 'correlation', 'earnings',
    'straddle', 'strangle', 'term_structure', 'backwardation', 'contango',
    'tilt', 'centroid', 'weighted', 'put_call', 'call_put', 'otm', 'atm',
    'proximity', '52wk', 'high_52', 'short_interest', 'unusual_flow',
]


def sha256(val: str) -> str:
    return hashlib.sha256(val.encode()).hexdigest()[:16]


def extract_regimes(source: str) -> list[str]:
    found = sorted(t for t in REGIME_TOKENS if t in source)
    return found or ['ANY']


def extract_directions(source: str) -> list[str]:
    found = sorted(t for t in DIRECTION_TOKENS if f"'{t}'" in source or f'"{t}"' in source)
    return found or ['LONG', 'SHORT']


def extract_formula_tokens(source: str) -> list[str]:
    src_lower = source.lower()
    return sorted(t for t in FORMULA_VOCAB if t in src_lower)


def _dsl_signal_literal(source: str):
    """The class-body ``SIGNAL = "<literal>"`` / ``SIGNAL: str = "<literal>"``
    string, found via ast (the source is never imported); None if absent."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                targets, value = stmt.targets, stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets, value = [stmt.target], stmt.value
            else:
                continue
            if (any(isinstance(t, ast.Name) and t.id == 'SIGNAL' for t in targets)
                    and isinstance(value, ast.Constant) and isinstance(value.value, str)
                    and value.value.strip()):
                return value.value
    return None


def signature_for_source(source: str) -> dict:
    """Per-file signature dict. A DSL source (class-body SIGNAL literal) also
    gets an exact ``formula_hash`` and op/column ``formula_tokens``."""
    regimes = extract_regimes(source)
    dirs    = extract_directions(source)
    sig = {
        'regime_set_hash':  sha256(' '.join(regimes)),
        'direction_hash':   sha256(' '.join(dirs)),
        'formula_tokens':   extract_formula_tokens(source),
        'regimes':          regimes,
    }
    expr = _dsl_signal_literal(source)
    if expr is not None:
        try:
            from strategies import signal_expr   # lazy: only DSL sources need it
            h = signal_expr.formula_hash(expr)
            used = {n.id for n in ast.walk(ast.parse(expr.strip(), mode='eval'))
                    if isinstance(n, ast.Name)}
            sig['formula_hash'] = h
            sig['formula_tokens'] = sorted(n for n in used if n != 'abs')
        except Exception as e:  # malformed SIGNAL: keep the substring signature
            print(f'[generate_signatures] SIGNAL not hashable ({e}); substring signature kept',
                  file=sys.stderr)
    return sig


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    strategies = manifest.get('strategies', {})

    sigs: dict = {}

    py_files = sorted(IMPL_DIR.glob('*.py'))
    # Skip __init__.py and underscore-prefixed files (templates / reference examples;
    # see src/strategies/implementations/_template_example.py — Phase 2E).
    py_files = [f for f in py_files if f.name != '__init__.py' and not f.name.startswith('_')]

    for path in py_files:
        source = path.read_text(errors='replace')
        strategy_id = path.stem

        sigs[strategy_id] = signature_for_source(source)

    # Merge the EJECTED-strategy signature archive (candidate reaper,
    # 2026-07-14): ejected strategies have their implementation files
    # DELETED, so a pure on-disk regen would erase their fingerprints and
    # re-enable the research pipeline to mint near-duplicates. The archive
    # preserves them forever; on-disk entries win on key collision (a live
    # re-implementation supersedes its ejected ancestor's snapshot).
    ejected_path = Path(__file__).parent / 'strategy_signatures_ejected.json'
    n_ejected = 0
    try:
        ejected = json.loads(ejected_path.read_text())
        for sid, entry in ejected.items():
            if sid not in sigs:
                sigs[sid] = entry
                n_ejected += 1
    except FileNotFoundError:
        pass

    SIG_OUT.write_text(json.dumps(sigs, indent=2) + '\n')
    print(f'Wrote {len(sigs)} entries to {SIG_OUT} ({n_ejected} from the ejected archive)')


if __name__ == '__main__':
    main()
