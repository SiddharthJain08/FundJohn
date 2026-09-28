"""F4 (fix round 1, signals-memory task 3): fleet pin, static only.

For every manifest strategy whose `state` is live/candidate/paper, the
AST-collected aux-kind literals in its import closure must be a SUBSET of
`execution.engine._strategy_aux_needs(sid)` — literally, not a
reimplementation of it: for any sid WITH a requirements.json, this test
calls the REAL `_strategy_aux_needs` (via a bare `SimpleNamespace(id=sid)`
stand-in — that code path only ever reads `.id`, so no strategy class is
imported or instantiated) rather than re-parsing the JSON itself, so this
pins the actual production parser, including the F2 fix round 1
malformed-file fail-open, instead of a hand-rolled duplicate that could
silently drift from it. A sid with NO requirements.json under its exact id
is flagged directly (see below) rather than run through
`_strategy_aux_needs` — that function's MRO/alias source-scan fallback
would fail open to EVERY gated kind for a bare `SimpleNamespace` (it has no
resolvable `__mro__` source file), which would make a missing-requirements
sid pass this check vacuously.

This is the guard F5's two hygiene fixes exist to satisfy
(S_beta_spy.requirements.json; `financials` added to
S_sparse_basis_pursuit_sdf.requirements.json).

Ported from the reviewer's own static audit
(.superpowers/sdd/2026-09-28-signals-memory scratchpad `audit.py`,
referenced by name in task-3-fix1-brief.md F4) for the AST/import-closure
literal-collection half, with one narrowing: scoped to
`_AUX_LAZY_GATED_KINDS` only. The reviewer's raw audit vocabulary also
included realized_vol/vol_indices/iv_history/earnings — none of which
`_strategy_aux_needs` (or `load_aux_data`) can independently skip — and
running the unscoped vocabulary against the live/candidate/paper fleet
produces two literal false positives here
(`S_aligned_economic_index_regime_timing`,
`S_inverse_volatility_risk_parity`, both reading
`aux_data['realized_vol']` — a backtest-only, ungated category, out of
`_strategy_aux_needs`' scope entirely; verified 2026-09-28 by running the
unscoped audit directly against this fleet).

No strategy imports: only `execution.engine` (module-level constants +
`_strategy_aux_needs` itself — no different from any other engine unit
test importing engine.py) and `execution.acting_ingest_plan` (for its
`IMPL_DIR`, monkeypatched by the negative-control test the same way
test_engine_aux_lazy.py's tests do) are imported, plus stdlib
`ast`/`json`/`re`/`types`. No masters, no Postgres/HTTP, no subagents.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.engine import _AUX_LAZY_GATED_KINDS, _strategy_aux_needs  # noqa: E402
from execution import acting_ingest_plan  # noqa: E402

IMPL_DIR = ROOT / 'src' / 'strategies' / 'implementations'
MANIFEST_PATH = ROOT / 'src' / 'strategies' / 'manifest.json'
IN_SCOPE_STATES = {'live', 'candidate', 'paper'}
GATED_KINDS = set(_AUX_LAZY_GATED_KINDS)
NO_REQUIREMENTS_SENTINEL = '<no requirements.json under this id>'


def _local_imports(path: Path, seen: set) -> None:
    """Follow ImportFrom/Import statements to every LOCAL
    (src/strategies, src/lib) module reachable from `path`, recording each
    visited file's path in `seen`. AST-only (ast.parse + ast.walk) — never
    executes or imports the module itself. Module-resolution logic ported
    verbatim from the reviewer's scratchpad audit.py `local_imports()`."""
    if path in seen or not path.exists():
        return
    seen.add(path)
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return
    candidates: list[Path] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ''
            if mod.startswith('src.'):
                mod = mod[4:]
            if node.level:  # relative import: `from . import x` / `from .. import x`
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                cand = base.joinpath(*mod.split('.')) if mod else base
                candidates.append(cand)
                for alias in node.names:
                    candidates.append(cand / alias.name)
            else:
                cand = ROOT / 'src' / Path(*mod.split('.'))
                candidates.append(cand)
                for alias in node.names:
                    candidates.append(cand / alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name[4:] if alias.name.startswith('src.') else alias.name
                candidates.append(ROOT / 'src' / Path(*name.split('.')))
    for cand in candidates:
        for p in (cand.with_suffix('.py'), cand / '__init__.py'):
            if p.exists() and ('/src/strategies' in str(p) or '/src/lib' in str(p)):
                _local_imports(p, seen)


def _literal_kinds(path: Path) -> set:
    """Which GATED_KINDS names appear as a quoted string literal anywhere
    in this file's source TEXT. Regex-based (matches the reviewer's
    audit.py `strkeys()` exactly) rather than a strict
    aux_data[...]/.get(...) AST match — deliberately over-reports (any
    quoted occurrence of the kind name, not just a confirmed aux_data
    access) so the pin fails toward MORE scrutiny, never less."""
    text = path.read_text()
    return {k for k in GATED_KINDS if re.search(r"""['"]%s['"]""" % re.escape(k), text)}


def _declared_kinds(sid: str, impl_dir: Path) -> set | None:
    """None means no requirements.json under this exact id — a finding on
    its own for an in-scope strategy (see module docstring). When the file
    DOES exist, this calls the REAL `_strategy_aux_needs` with a bare
    `SimpleNamespace(id=sid)` — that function's id-based branch only ever
    reads `.id` before returning (it never inspects `type(strat)` unless
    the requirements.json is missing), so this pins the actual production
    parser rather than a second one."""
    req_path = impl_dir / f'{sid}.requirements.json'
    if not req_path.exists():
        return None
    return _strategy_aux_needs(SimpleNamespace(id=sid))


def _fleet_violations(manifest: dict, impl_dir: Path) -> list:
    violations = []
    for sid, entry in sorted(manifest.items()):
        if entry.get('state') not in IN_SCOPE_STATES:
            continue
        declared = _declared_kinds(sid, impl_dir)
        if declared is None:
            violations.append((sid, NO_REQUIREMENTS_SENTINEL))
            continue
        canonical = (entry.get('metadata') or {}).get('canonical_file')
        if not canonical:
            continue
        impl_path = impl_dir / canonical
        seen: set = set()
        _local_imports(impl_path, seen)
        found: set = set()
        for p in seen:
            found |= _literal_kinds(p)
        for kind in sorted(found - declared):
            violations.append((sid, kind))
    return violations


def test_fleet_aux_needs_pin():
    """The real fleet: production manifest.json + production
    IMPL_DIR/acting_ingest_plan.IMPL_DIR (NOT monkeypatched — this is the
    actual checked-in state after F5's two hygiene fixes)."""
    manifest = json.loads(MANIFEST_PATH.read_text())['strategies']
    violations = _fleet_violations(manifest, IMPL_DIR)
    assert not violations, (
        f'{len(violations)} fleet aux-needs pin violation(s) — a live/'
        f'candidate/paper strategy either has no requirements.json under '
        f'its exact id, or reads (in its import closure) an aux kind its '
        f'requirements.json does not declare: {violations}'
    )


def test_detector_catches_under_declaration_and_missing_reqs(tmp_path, monkeypatch):
    """Negative control: test_fleet_aux_needs_pin above only proves 'zero
    violations against TODAY's real fleet' — after F5's fixes, it can no
    longer demonstrate the detector actually catches anything. This builds
    a tiny synthetic manifest + impl dir with one under-declaring strategy
    (reads a literal 'financials' key; its requirements.json declares only
    prices — the exact shape F5 fixed for S_sparse_basis_pursuit_sdf) and
    one strategy with NO requirements.json at all (the exact shape F5
    fixed for S_beta_spy), and asserts both violations come back — proving
    the detector would have failed loudly before F5, not just that it's
    silent now.

    acting_ingest_plan.IMPL_DIR is monkeypatched to tmp_path because
    _strategy_aux_needs' requirements-file branch reads that module
    attribute directly (not a parameter) — same pattern
    test_engine_aux_lazy.py's own requirements-file tests use."""
    monkeypatch.setattr(acting_ingest_plan, 'IMPL_DIR', tmp_path)
    (tmp_path / 'S_under.py').write_text(
        "def f(aux_data):\n    return aux_data.get('financials')\n")
    (tmp_path / 'S_under.requirements.json').write_text(json.dumps(
        {'strategy_id': 'S_under', 'required': ['prices'], 'optional': []}))
    (tmp_path / 'S_noreqs.py').write_text('def f():\n    return 1\n')
    manifest = {
        'S_under':  {'state': 'live', 'metadata': {'canonical_file': 'S_under.py'}},
        'S_noreqs': {'state': 'live', 'metadata': {'canonical_file': 'S_noreqs.py'}},
        'S_skip':   {'state': 'deprecated', 'metadata': {'canonical_file': 'nope.py'}},
    }
    violations = _fleet_violations(manifest, tmp_path)
    assert ('S_under', 'financials') in violations
    assert ('S_noreqs', NO_REQUIREMENTS_SENTINEL) in violations
    assert len(violations) == 2   # deprecated-state sid excluded


def test_scope_excludes_realized_vol_and_similar_out_of_scope_kinds():
    """Documents WHY the two known false positives under the reviewer's
    unscoped vocabulary don't apply here: 'realized_vol' (read by
    S_aligned_economic_index_regime_timing and
    S_inverse_volatility_risk_parity) is not one of the kinds
    _strategy_aux_needs governs."""
    assert 'realized_vol' not in GATED_KINDS
    assert 'vol_indices' not in GATED_KINDS
    assert 'iv_history' not in GATED_KINDS
    assert 'earnings' not in GATED_KINDS


def test_in_scope_states_match_the_brief():
    assert IN_SCOPE_STATES == {'live', 'candidate', 'paper'}
