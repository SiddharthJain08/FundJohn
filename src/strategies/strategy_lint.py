"""strategy_lint.py — AST import allowlist for LLM-written strategies (QD §5 E4).

NOT A SANDBOX. This is a guardrail against a generated strategy reaching the
network or the filesystem by accident or prompt drift. It is trivially
defeatable by anyone trying: `backtest` is allowlisted (30 fleet files import
`backtest.quick_backtest` or a sibling of it, as of the 2026-09-22 census
below) and transitively reaches the whole repo. Treat a clean lint as "no
obvious I/O", never as "safe to run untrusted code".

ALLOWED_ROOTS is the MEASURED import census of src/strategies/implementations/
(AST walk over the live fleet — 168 files as of 2026-09-22; the count moves
as strategies are added, this docstring won't be updated every time) — not a
wish list. The test `TestFleetIsClean` re-derives it every run: widening the
allowlist is a deliberate, reviewed act, and narrowing it fails loudly instead
of silently rejecting the next candidate the strategycoder prompt produces.

`os` and `sys` are allowed as MODULES but restricted by ATTRIBUTE. 147 fleet
files import sys and 30 import os (2026-09-22 census), and the strategycoder
prompt itself mandates `print(..., file=sys.stderr)` on every strategy.
Rejecting the module would reject the fleet; rejecting `os.system` /
`os.remove` / `sys.modules` is the part that actually matters.
"""
from __future__ import annotations

import ast
import json
import sys
from collections import namedtuple
from pathlib import Path

Violation = namedtuple('Violation', 'line kind detail')

# ── The measured census (see the module docstring) ───────────────────────────
# Re-run 2026-09-22 against the live fleet (168 files, up from the 156-file
# 2026-09-13 census this list was originally drawn from): 27 absolute roots
# found, all already present below — zero additions required. `collections`
# and `re` remain in the allowlist even though neither census shows them at
# module level in src/strategies/implementations/: both are pure stdlib with
# no I/O, both appear in the spec's own list, and `re` is used inside
# function bodies across the fleet. Widening for a root that ISN'T in the
# measured census (as opposed to these two, deliberately over-allowed
# stdlib modules) must be recorded as a reviewed decision, not silently.
ALLOWED_ROOTS = frozenset({
    # repo packages
    'strategies', 'src', 'backtest', 'lib', 'base', '_extra_panels',
    # scientific stack
    'pandas', 'numpy', 'scipy', 'sklearn', 'statsmodels', 'pyarrow',
    # stdlib the fleet actually uses
    '__future__', 'typing', 'sys', 'os', 'json', 'pathlib', 'traceback',
    'math', 'datetime', 'itertools', 'functools', 'logging', 'dataclasses',
    'enum', 'statistics', 'collections', 're',
})

# Attribute-level policy for the two modules whose ROOT is allowed only
# because the fleet needs a narrow slice of them. The 2026-09-22 census only
# attests os.{path, environ} and sys.{stderr, path, exit} in live use; the
# wider set below (getenv/sep/pathsep/linesep/name, stdout/argv/
# version_info/maxsize/platform) is inherited from the original QD spec §5 E4
# resolution rather than freshly measured — all are read-only / no-I/O
# attributes, so keeping them cannot make TestFleetIsClean fail.
ALLOWED_ATTRS = {
    'os':  frozenset({'environ', 'getenv', 'path', 'sep', 'pathsep', 'linesep', 'name'}),
    'sys': frozenset({'stderr', 'stdout', 'path', 'exit', 'argv',
                      'version_info', 'maxsize', 'platform'}),
}

BANNED_CALLS = frozenset({'open', 'eval', 'exec', 'compile', '__import__'})

# Method NAMES that write or reach out. Every one verified to have zero hits
# across the live fleet. `rename` is deliberately absent: the fleet's few
# uses are all Series.rename / DataFrame.rename, and a name-based ban there
# would reject legitimate code.
BANNED_METHODS = frozenset({
    'write_text', 'write_bytes', 'unlink', 'rmdir', 'mkdir', 'chmod',
    'symlink_to', 'hardlink_to', 'touch',
    'to_csv', 'to_parquet', 'to_pickle', 'to_hdf', 'to_sql',
    'system', 'popen', 'remove', 'makedirs',
    'urlopen', 'check_output', 'Popen',
})


def _root(dotted: str) -> str:
    return (dotted or '').split('.')[0]


def lint_source(source: str, filename: str = '<candidate>') -> list:
    """Return the list of Violations in `source`. Never imports, never execs."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return [Violation(getattr(e, 'lineno', 0) or 0, 'syntax', f'syntax error: {e.msg}')]

    out = []
    for node in ast.walk(tree):
        # import x, import x.y
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root(alias.name)
                if root not in ALLOWED_ROOTS:
                    out.append(Violation(node.lineno, 'import',
                                         f'import {alias.name!r} — root {root!r} is not on the allowlist'))
        # from x import y  /  from . import y
        elif isinstance(node, ast.ImportFrom):
            if node.level:                       # relative — inside the package, fine
                continue
            root = _root(node.module or '')
            if root not in ALLOWED_ROOTS:
                out.append(Violation(node.lineno, 'import',
                                     f'from {node.module!r} import … — root {root!r} is not on the allowlist'))
            elif root in ALLOWED_ATTRS and (node.module or '') == root:
                # `from os import system` would otherwise walk straight past the
                # attribute policy: the name is bound locally and never appears
                # as an ast.Attribute. Apply the same allowlist to the names.
                for alias in node.names:
                    if alias.name not in ALLOWED_ATTRS[root]:
                        out.append(Violation(node.lineno, 'import',
                                             f'from {root} import {alias.name} — only '
                                             f'{sorted(ALLOWED_ATTRS[root])} are permitted'))
        # os.<attr> / sys.<attr>
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            mod = node.value.id
            if mod in ALLOWED_ATTRS and node.attr not in ALLOWED_ATTRS[mod]:
                out.append(Violation(node.lineno, 'attribute',
                                     f'{mod}.{node.attr} — only '
                                     f'{sorted(ALLOWED_ATTRS[mod])} are permitted'))
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in BANNED_CALLS:
                out.append(Violation(node.lineno, 'call', f'{fn.id}(…) is not permitted'))
            elif isinstance(fn, ast.Attribute):
                if fn.attr in BANNED_CALLS:
                    out.append(Violation(node.lineno, 'call', f'.{fn.attr}(…) is not permitted'))
                elif fn.attr in BANNED_METHODS:
                    out.append(Violation(node.lineno, 'method',
                                         f'.{fn.attr}(…) writes or reaches out — not permitted '
                                         f'in a strategy file'))

    out.sort(key=lambda v: (v.line, v.kind, v.detail))
    return out


def lint_file(path) -> list:
    p = Path(path)
    try:
        source = p.read_text(encoding='utf-8')
    except Exception as e:
        return [Violation(0, 'io', f'cannot read {p}: {e}')]
    return lint_source(source, filename=str(p))


def format_violations(violations) -> list:
    return [f'[import-lint] line {v.line}: {v.kind}: {v.detail}' for v in violations]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print('Usage: strategy_lint.py <file> [<file>...]', file=sys.stderr)
        return 2
    rows = []
    for path in argv:
        for v in lint_file(path):
            rows.append({'file': path, 'line': v.line, 'kind': v.kind, 'detail': v.detail})
    print(json.dumps({'ok': not rows, 'violations': rows}, indent=2))
    return 1 if rows else 0


if __name__ == '__main__':
    sys.exit(main())
