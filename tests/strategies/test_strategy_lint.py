"""tests/strategies/test_strategy_lint.py — AST import allowlist (QD §5 E4).

Pure source-string linting: nothing is imported, nothing is executed, no DB.
The last test lints every live fleet file under
src/strategies/implementations/ (168 as of 2026-09-22, re-derived at test
time via glob — the count moves as strategies are added, so this file never
hardcodes it) and asserts zero violations.
"""
from __future__ import annotations

import contextlib
import glob
import io
import json
import sys
import tempfile
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

    def test_import_of_a_submodule_with_an_alias_stays_clean(self):
        # `import os.path as osp` binds osp to the os.path SUBMODULE, not to
        # `os` itself — the attribute policy must not treat it as os.
        self.assertEqual(
            sl.lint_source("import os.path as osp\nosp.join('a')\n"), [])


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

    def test_aliased_os_and_sys_imports_do_not_bypass_the_attribute_policy(self):
        vs = sl.lint_source('import os as _o\n_o.execv("/bin/sh", [])\n')
        self.assertIn('attribute', [v.kind for v in vs])
        self.assertTrue(any('_o.execv' in v.detail or 'os.execv' in v.detail for v in vs))

        vs = sl.lint_source('import sys as _s\n_s.modules\n')
        self.assertEqual([v.kind for v in vs], ['attribute'])

    def test_network_literal_passed_to_a_call_is_rejected(self):
        vs = sl.lint_source("pd.read_csv('https://example.com/data.csv')\n")
        self.assertTrue(any(v.kind == 'network' for v in vs))

        vs = sl.lint_source("requests_get(url='ftp://example.com/x')\n")
        self.assertTrue(any(v.kind == 'network' for v in vs))

    def test_a_url_in_a_docstring_is_not_a_violation(self):
        # A bare docstring alone would pass trivially even if the network
        # check scanned every ast.Constant in the tree — pair it with a real
        # Call so the test actually proves the URL isn't a Call argument.
        src = ('"""See https://example.com/paper.pdf for the reference."""\n'
               "df = df.rename(columns={'a': 'b'})\n")
        self.assertEqual(sl.lint_source(src), [])

    def test_a_url_in_a_comment_is_not_a_violation(self):
        src = ("x = 1  # see https://example.com/paper.pdf\n"
               "df = df.rename(columns={'a': 'b'})\n")
        self.assertEqual(sl.lint_source(src), [])

    def test_a_syntax_error_is_reported_as_one_violation(self):
        vs = sl.lint_source('def f(:\n')
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0].kind, 'syntax')

    def test_violations_format_with_line_numbers(self):
        lines = sl.format_violations(sl.lint_source('import socket\n'))
        self.assertEqual(len(lines), 1)
        self.assertIn('line 1', lines[0])
        self.assertIn('socket', lines[0])


class TestIntraday(unittest.TestCase):
    def kinds(self, body):
        return [v.kind for v in sl.lint_source(OK_HEAD + body)]

    def test_clean_opt_in_literal(self):
        self.assertEqual(sl.lint_source(
            OK_HEAD + "P = {'exit_on': 'session_close', 'entry_bar_et': '10:00', 'x': 1}\n"), [])

    def test_non_session_close_exit_on_is_ignored(self):
        self.assertEqual(sl.lint_source(OK_HEAD + "P = {'exit_on': 'target'}\n"), [])

    def test_missing_entry_bar(self):
        v = sl.lint_source(OK_HEAD + "P = {'exit_on': 'session_close'}\n")
        self.assertEqual([x.kind for x in v], ['intraday'])
        self.assertIn('requires entry_bar_et', v[0].detail)

    def test_bad_bar_literal_reported_once(self):
        v = sl.lint_source(OK_HEAD + "P = {'exit_on': 'session_close', 'entry_bar_et': '10:15'}\n")
        self.assertEqual([x.kind for x in v], ['intraday'])
        self.assertIn("'10:15'", v[0].detail)

    def test_bad_bar_without_exit_on_still_flagged(self):
        self.assertEqual(self.kinds("P = {'entry_bar_et': '16:00'}\n"), ['intraday'])

    def test_name_value_is_not_a_literal(self):
        v = sl.lint_source(OK_HEAD + "B = '10:00'\nP = {'exit_on': 'session_close', 'entry_bar_et': B}\n")
        self.assertEqual([x.kind for x in v], ['intraday'])
        self.assertIn('string literal', v[0].detail)

    def test_helper_call_good_and_bad(self):
        self.assertEqual(self.kinds("P = session_close_params('15:30', strategy_id='S')\n"), [])
        self.assertEqual(self.kinds("P = intraday.session_close_params('10:00')\n"), [])
        self.assertEqual(self.kinds("P = session_close_params('10:15')\n"), ['intraday'])
        self.assertEqual(self.kinds("B = '10:00'\nP = session_close_params(B)\n"), ['intraday'])
        self.assertEqual(self.kinds("P = session_close_params()\n"), ['intraday'])

    def test_dict_with_unpack_is_handled(self):
        self.assertEqual(self.kinds(
            "E = {}\nP = {**E, 'exit_on': 'session_close', 'entry_bar_et': '09:30'}\n"), [])


class TestCLIContract(unittest.TestCase):
    """Hermetic: `main()` called in-process (no subprocess) against a real
    temp file, with stdout captured. Exercises exactly what
    research-orchestrator.js's pre-flight parses: `JSON.parse(stdout)` then
    reads `.ok` and `.violations[].{file,line,kind,detail}`.
    """

    def _run_main(self, *paths):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sl.main([str(p) for p in paths])
        return rc, json.loads(buf.getvalue())

    def test_cli_json_contract_on_a_clean_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'clean.py'
            f.write_text(OK_HEAD)
            rc, payload = self._run_main(f)
        self.assertEqual(rc, 0)
        self.assertEqual(payload, {'ok': True, 'violations': [], 'warnings': []})

    def test_cli_json_contract_on_a_violating_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'bad.py'
            f.write_text('import socket\n')
            rc, payload = self._run_main(f)
        self.assertEqual(rc, 1)
        self.assertIs(payload['ok'], False)
        self.assertEqual(len(payload['violations']), 1)
        for v in payload['violations']:
            self.assertEqual(set(v.keys()), {'file', 'line', 'kind', 'detail'})
        self.assertEqual(payload['violations'][0]['kind'], 'import')

    def test_lint_file_on_an_unreadable_path_reports_io(self):
        vs = sl.lint_file('/nonexistent/path/does-not-exist-strategy_lint.py')
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0].kind, 'io')

    def test_a_syntax_broken_file_reports_syntax_and_the_json_still_parses(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'broken.py'
            f.write_text('def f(:\n')
            rc, payload = self._run_main(f)
        self.assertEqual(rc, 1)
        self.assertIs(payload['ok'], False)
        self.assertEqual(len(payload['violations']), 1)
        self.assertEqual(payload['violations'][0]['kind'], 'syntax')
        self.assertEqual(set(payload['violations'][0].keys()), {'file', 'line', 'kind', 'detail'})


class TestWarnRules(unittest.TestCase):
    """C1: mechanical rules on a WARN channel; never change ok/violations/rc without --strict."""

    def _run_main(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sl.main([str(a) for a in argv])
        return rc, json.loads(buf.getvalue())

    def _warn_file(self, d, name='w.py'):
        f = Path(d) / name
        f.write_text(OK_HEAD + 's = pd.Series([1.0])\nr = s.rolling(5).mean()\nx = s.shift(-1)\n')
        return f

    def test_each_rule_fires(self):
        cases = [
            ('x = s.shift(-1)\n', 'negative_shift'),
            ('x = s.shift(periods=-2)\n', 'negative_shift'),
            ('x = lag(s, -3)\n', 'negative_shift'),
            ('import pandas as pd\nd = pd.read_parquet("p")\n', 'unguarded_parquet_read'),
            ('r = s.rolling(20).mean()\n', 'rolling_without_min_periods'),
            ('e = 1e-9\n', 'raw_epsilon_literal'),
        ]
        for snippet, kind in cases:
            with self.subTest(snippet=snippet):
                self.assertEqual([v.kind for v in sl.lint_warnings(OK_HEAD + snippet)], [kind])

    def test_rules_do_not_fire_on_clean_forms(self):
        for snippet in ('s.shift(1)\n', 's.rolling(20, min_periods=20).mean()\n',
                        'e = 1e-3\n', 'e = 0.0\n',
                        'from strategies.asof import asof_align\nimport pandas as pd\npd.read_parquet("p")\n',
                        'from strategies import asof\nimport pandas as pd\npd.read_csv("p")\n',
                        'import strategies.asof\nimport pandas as pd\npd.read_csv("p")\n'):
            with self.subTest(snippet=snippet):
                self.assertEqual(sl.lint_warnings(OK_HEAD + snippet), [])

    def test_main_block_findings_are_tagged(self):
        vs = sl.lint_warnings(OK_HEAD + "if __name__ == '__main__':\n    x = s.shift(-1)\n")
        self.assertEqual(len(vs), 1)
        self.assertTrue(vs[0].detail.endswith('[__main__]'))

    def test_format_warnings_line(self):
        vs = sl.lint_warnings(OK_HEAD + 'e = 1e-9\n')
        self.assertTrue(sl.format_warnings(vs)[0].startswith('[strategy-lint-warn] line 7: raw_epsilon_literal: '))

    def test_lint_source_never_returns_warn_kinds(self):
        self.assertEqual(sl.lint_source(OK_HEAD + 'x = s.shift(-1)\ne = 1e-9\n'), [])

    def test_warn_rules_never_change_ok_or_rc(self):
        with tempfile.TemporaryDirectory() as d:
            f = self._warn_file(d)
            rc, payload = self._run_main(f)
            self.assertEqual(sl.lint_file(f), [])
        self.assertEqual(rc, 0)
        self.assertIs(payload['ok'], True)
        self.assertEqual(payload['violations'], [])
        self.assertEqual(len(payload['warnings']), 2)
        for w in payload['warnings']:
            self.assertEqual(set(w.keys()), {'file', 'line', 'kind', 'detail'})

    def test_strict_promotes_warnings(self):
        with tempfile.TemporaryDirectory() as d:
            rc, payload = self._run_main('--strict', self._warn_file(d))
        self.assertEqual(rc, 1)
        self.assertIs(payload['ok'], False)
        self.assertEqual(len(payload['violations']), 2)
        self.assertEqual(payload['warnings'], [])

    def test_census_shape(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._warn_file(d, 'a.py')
            b = Path(d) / 'b.py'
            b.write_text(OK_HEAD)
            c = sl.census([str(a), str(b)])
        self.assertEqual(set(c.keys()), {'files', 'rules'})
        self.assertEqual(c['files'], 2)
        self.assertEqual(set(c['rules']), set(sl.WARN_KINDS))
        for kind in sl.WARN_KINDS:
            self.assertEqual(set(c['rules'][kind]), {'files', 'hits', 'hits_in_main'})
        self.assertEqual(c['rules']['negative_shift'], {'files': 1, 'hits': 1, 'hits_in_main': 0})

    def test_census_cli_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            rc, payload = self._run_main('--census', self._warn_file(d))
        self.assertEqual(rc, 0)
        self.assertIn('rules', payload)

    def test_validate_prints_warnings_to_stderr(self):
        from unittest import mock
        from strategies import validate_strategy
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'cand.py'
            f.write_text('x = 1\n')
            warn = sl.Violation(3, 'negative_shift', 'shift(-1)')
            err = io.StringIO()
            with mock.patch.object(sl, 'lint_file_warnings', return_value=[warn]), \
                 mock.patch.object(sl, 'lint_file', return_value=[sl.Violation(1, 'import', 'bad')]), \
                 contextlib.redirect_stderr(err):
                res = validate_strategy.validate(str(f))
        self.assertIs(res['ok'], False)
        self.assertIn('[strategy-lint-warn]', err.getvalue())


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
