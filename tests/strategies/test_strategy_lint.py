"""tests/strategies/test_strategy_lint.py — AST import allowlist (QD §5 E4).

Pure source-string linting: nothing is imported, nothing is executed, no DB.
The last test lints every live fleet file under
src/strategies/implementations/ (168 as of 2026-09-22, re-derived at test
time via glob — the count moves as strategies are added, so this file never
hardcodes it) and asserts zero violations.
"""
from __future__ import annotations

import glob
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import strategy_lint as sl  # noqa: E402

OK_HEAD = (
    'from __future__ import annotations\n'
    'import sys\n'
    'import pandas as pd\n'
    'import numpy as np\n'
    'from typing import List\n'
    'from strategies.base import BaseStrategy, Signal\n'
)


class TestAllowed(unittest.TestCase):
    def test_a_typical_strategy_header_is_clean(self):
        self.assertEqual(sl.lint_source(OK_HEAD), [])

    def test_every_census_root_is_allowed(self):
        # `os` and `sys` are attribute-restricted even on a `from ... import`
        # (see test_from_os_import_system_does_not_bypass_the_attribute_policy
        # below) — an arbitrary imported name like `x` would legitimately
        # violate that policy, so those two roots use a permitted name here
        # instead. Every other root has no attribute restriction.
        permitted_from_name = {'os': 'environ', 'sys': 'stderr'}
        for root in ['strategies', 'typing', '__future__', 'sys', 'pandas', 'numpy',
                     'os', 'src', 'backtest', 'json', 'sklearn', '_extra_panels',
                     'pathlib', 'scipy', 'traceback', 'math', 'datetime', 'itertools',
                     'pyarrow', 'base', 'lib', 'statsmodels', 'functools', 'logging',
                     'dataclasses', 'enum', 'statistics']:
            self.assertEqual(sl.lint_source(f'import {root}\n'), [], root)
            name = permitted_from_name.get(root, 'x')
            self.assertEqual(sl.lint_source(f'from {root} import {name}\n'), [], root)

    def test_relative_imports_are_allowed(self):
        self.assertEqual(sl.lint_source('from ..base import BaseStrategy\n'), [])
        self.assertEqual(sl.lint_source('from ._greeks_filter import f\n'), [])

    def test_the_mandated_stderr_debug_line_is_allowed(self):
        src = OK_HEAD + "print('[debug] signals=0', file=sys.stderr)\n"
        self.assertEqual(sl.lint_source(src), [])

    def test_the_sys_path_and_os_path_idioms_are_allowed(self):
        src = ("import os, sys\n"
               "sys.path.insert(0, 'src/strategies')\n"
               "p = os.path.join(os.path.dirname(__file__), 'x')\n"
               "v = os.environ.get('OPENCLAW_X', '0')\n")
        self.assertEqual(sl.lint_source(src), [])

    def test_dotted_allowed_roots_are_allowed(self):
        self.assertEqual(sl.lint_source('from statsmodels.tsa.stattools import coint\n'), [])
        self.assertEqual(sl.lint_source('from src.strategies.universe_default import sp500\n'), [])
        self.assertEqual(sl.lint_source('from backtest.quick_backtest import run\n'), [])


class TestRejected(unittest.TestCase):
    def _kinds(self, src):
        return sorted({v.kind for v in sl.lint_source(src)})

    def test_network_and_process_roots_are_rejected(self):
        for root in ['subprocess', 'socket', 'requests', 'urllib', 'http',
                     'shutil', 'importlib', 'ctypes', 'pickle', 'multiprocessing']:
            vs = sl.lint_source(f'import {root}\n')
            self.assertEqual(len(vs), 1, root)
            self.assertEqual(vs[0].kind, 'import')
            self.assertIn(root, vs[0].detail)

    def test_from_import_of_a_rejected_root_is_caught(self):
        vs = sl.lint_source('from urllib.request import urlopen\n')
        self.assertEqual([v.kind for v in vs], ['import'])
        self.assertEqual(vs[0].line, 1)

    def test_from_os_import_system_does_not_bypass_the_attribute_policy(self):
        # The name is bound locally, so it never appears as an ast.Attribute —
        # the ImportFrom branch has to apply the same allowlist.
        for stmt, bad in [('from os import system', 'system'),
                          ('from os import remove, path', 'remove'),
                          ('from sys import modules', 'modules')]:
            vs = sl.lint_source(stmt + '\n')
            self.assertTrue(any(v.kind == 'import' and bad in v.detail for v in vs), stmt)

    def test_from_os_import_of_a_permitted_name_is_clean(self):
        self.assertEqual(sl.lint_source('from os import environ, path\n'), [])
        self.assertEqual(sl.lint_source('from sys import stderr\n'), [])
        # A dotted submodule of an allowed root keeps root-level semantics.
        self.assertEqual(sl.lint_source('from os.path import join\n'), [])

    def test_dangerous_os_and_sys_attributes_are_rejected(self):
        self.assertEqual(self._kinds('import os\nos.system("rm -rf /")\n'),
                         ['attribute', 'method'])
        self.assertEqual(self._kinds('import os\nos.remove("/tmp/x")\n'),
                         ['attribute', 'method'])
        self.assertEqual(self._kinds('import sys\nsys.modules.clear()\n'), ['attribute'])

    def test_banned_builtin_calls_are_rejected(self):
        for expr, kind in [("open('/etc/passwd')", 'call'),
                           ("eval('1+1')", 'call'),
                           ("exec('x=1')", 'call'),
                           ("compile('x', 'f', 'exec')", 'call'),
                           ("__import__('os')", 'call')]:
            vs = sl.lint_source(expr + '\n')
            self.assertTrue(any(v.kind == kind for v in vs), expr)

    def test_banned_write_methods_are_rejected(self):
        for expr in ['p.write_text("x")', 'p.unlink()', 'df.to_csv("/tmp/x.csv")',
                     'df.to_parquet("/tmp/x.pq")', 'p.mkdir()']:
            vs = sl.lint_source(expr + '\n')
            self.assertTrue(any(v.kind == 'method' for v in vs), expr)

    def test_dataframe_rename_is_NOT_a_violation(self):
        self.assertEqual(sl.lint_source('df = df.rename(columns={"a": "b"})\n'), [])

    def test_a_syntax_error_is_reported_as_one_violation(self):
        vs = sl.lint_source('def f(:\n')
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0].kind, 'syntax')

    def test_violations_format_with_line_numbers(self):
        lines = sl.format_violations(sl.lint_source('import socket\n'))
        self.assertEqual(len(lines), 1)
        self.assertIn('line 1', lines[0])
        self.assertIn('socket', lines[0])


class TestFleetIsClean(unittest.TestCase):
    def test_every_live_strategy_file_lints_clean(self):
        impl = ROOT / 'src' / 'strategies' / 'implementations'
        files = sorted(glob.glob(str(impl / '*.py')))
        self.assertGreater(len(files), 100, 'fleet not found — wrong ROOT?')
        offenders = {}
        for f in files:
            vs = sl.lint_file(f)
            if vs:
                offenders[Path(f).name] = sl.format_violations(vs)
        self.assertEqual(offenders, {},
                         'the allowlist must cover every legitimate fleet import; '
                         'fix the strategy or widen ALLOWED_ROOTS deliberately')


if __name__ == '__main__':
    unittest.main()
