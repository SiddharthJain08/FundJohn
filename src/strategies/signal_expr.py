"""signal_expr.py — safe expression compiler for declarative strategy signals (C7a).

A strategy may declare ``SIGNAL = "sma(close, 20) / sma(close, 60) - 1"``.  The
string is untrusted (LLM-written), so it goes through two passes:

1. ``validate_expr_syntax`` — a Python ``ast`` whitelist pass.  It runs BEFORE
   sympy's ``parse_expr`` (which uses ``eval``) is ever reached, in the lint
   process and at class definition.  Attribute / Subscript / Lambda / str /
   Compare / BoolOp / keyword and star arguments are all rejected.  This module
   imports only the stdlib at module level: the lint process never loads
   sympy or pandas (they are imported lazily inside ``compile_signal``,
   ``formula_hash`` and ``CompiledSignal.__call__``).
2. ``compile_signal`` — sympy ``parse_expr`` with a curated ``local_dict``
   (declared columns as Symbols, whitelisted ops as Functions) and a minimal
   ``global_dict``; the resulting tree is compiled to a vectorised callable.

Conventions (plan "Decisions beyond the spec" 1-6):
* panel in = two-level ``(field, ticker)`` columns (see ``make_panel``); out =
  wide ``date x ticker`` frame.
* NaN cells mean "no bar": every windowed op runs per column on ``s.dropna()``
  and is reindexed back.  No tail slicing (full-history recursion for ema/rsi).
* ``std``/``zscore`` use ddof=1; ``max``/``min`` are rolling only; ``abs`` is
  always allowed arithmetic; division is ``safe_div(a, b, fill=nan)``.
* Boolean/comparison syntax is rejected — a SIGNAL is a numeric score.
* Powers: exponent literals only up to |64| when the exponent has no column
  (write ``x ** 0.5``, not ``x ** (1/2)``), and no ``**`` inside an exponent;
  this keeps the canonical-hash evaluation (evaluate=True) from being asked to
  compute a huge integer power.
"""
from __future__ import annotations

import ast
import hashlib
import keyword
from dataclasses import dataclass
from typing import Mapping

__all__ = ['SignalExprError', 'OpSpec', 'DEFAULT_OPS', 'CompiledSignal',
           'validate_expr_syntax', 'compile_signal', 'formula_hash', 'make_panel']

MAX_EXPR_CHARS = 2000
MAX_NODES = 500
MAX_CONST = 1e6
MAX_CONST_EXPONENT = 64


class SignalExprError(ValueError):
    """The SIGNAL expression is malformed, unsafe, or uses unknown vocabulary."""


@dataclass(frozen=True)
class OpSpec:
    name: str
    shape: str          # 'xn' = (x, n), 'n' = (n), 'x' = (x)


DEFAULT_OPS: Mapping[str, OpSpec] = {
    **{n: OpSpec(n, 'xn') for n in ('sma', 'ema', 'std', 'zscore', 'rsi', 'pct_change',
                                    'max', 'min', 'lag')},
    'atr': OpSpec('atr', 'n'),
    'rank_pct': OpSpec('rank_pct', 'x'),
}

_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_UNOPS = (ast.USub, ast.UAdd)


def _is_num(node) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) in (int, float)


def _has(node, kind) -> bool:
    return any(isinstance(n, kind) for n in ast.walk(node))


def _check_columns(columns, ops) -> None:
    for c in columns:
        if not isinstance(c, str) or not c.isidentifier() or keyword.iskeyword(c):
            raise SignalExprError(f'invalid column name {c!r}')
        if c in ops or c == 'abs':
            raise SignalExprError(f'column name {c!r} collides with an operator')


def _walk(tree, columns, ops):
    """Whitelist walk. `columns` may be None (collect mode) -> returns names used."""
    names, count = set(), [0]

    def bad(node, why=''):
        raise SignalExprError(f'{type(node).__name__} is not allowed in a SIGNAL expression'
                              + (f' ({why})' if why else ''))

    def window(node):
        if not (isinstance(node, ast.Constant) and type(node.value) is int and node.value >= 1):
            raise SignalExprError('window must be a positive integer literal')
        if node.value > 100_000:
            raise SignalExprError('window too large')

    def visit(node):
        count[0] += 1
        if count[0] > MAX_NODES:
            raise SignalExprError('expression too large')
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _BINOPS):
                bad(node.op)
            if isinstance(node.op, ast.Pow):
                r = node.right
                if _has(r, ast.Pow):
                    raise SignalExprError("'**' is not allowed inside an exponent")
                if not _has(r, ast.Name):
                    lit = r.operand if isinstance(r, ast.UnaryOp) else r
                    if not (_is_num(lit) and abs(lit.value) <= MAX_CONST_EXPONENT):
                        raise SignalExprError('constant exponent must be a numeric literal '
                                              f'with |value| <= {MAX_CONST_EXPONENT}')
            visit(node.left)
            visit(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _UNOPS):
                bad(node.op)
            visit(node.operand)
        elif isinstance(node, ast.Constant):
            if not _is_num(node):
                bad(node)
            if abs(node.value) > MAX_CONST or node.value != node.value:
                raise SignalExprError('numeric literal out of range')
        elif isinstance(node, ast.Name):
            if columns is None:
                if node.id not in ops and node.id != 'abs':
                    names.add(node.id)
            elif node.id not in columns:
                raise SignalExprError(f"unknown symbol '{node.id}'")
            else:
                names.add(node.id)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                bad(node, 'only plain function names may be called')
            if node.keywords:
                bad(node.keywords[0], 'keyword arguments')
            if any(isinstance(a, ast.Starred) for a in node.args):
                bad(node, 'star arguments')
            f = node.func.id
            if f == 'abs':
                if len(node.args) != 1:
                    raise SignalExprError('abs() takes exactly 1 argument')
                visit(node.args[0])
            elif f in ops:
                shape, a = ops[f].shape, node.args
                want = {'xn': 2, 'n': 1, 'x': 1}[shape]
                if len(a) != want:
                    raise SignalExprError(f'{f}() takes {want} argument(s), got {len(a)}')
                if shape == 'xn':
                    visit(a[0])
                    window(a[1])
                elif shape == 'n':
                    window(a[0])
                    if columns is not None:
                        missing = {'high', 'low', 'close'} - set(columns)
                        if missing:
                            raise SignalExprError(f'{f}() needs columns high/low/close '
                                                  f'(missing {sorted(missing)})')
                    names.update({'high', 'low', 'close'})
                else:
                    visit(a[0])
            else:
                raise SignalExprError(f"unknown function '{f}'")
        else:
            bad(node)

    visit(tree)
    return names


def _parse(expr):
    if not isinstance(expr, str):
        raise SignalExprError('SIGNAL must be a string')
    if len(expr) > MAX_EXPR_CHARS:
        raise SignalExprError('expression too long')
    try:
        return ast.parse(expr.strip(), mode='eval').body
    except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
        raise SignalExprError(f'cannot parse expression: {e}') from None


def validate_expr_syntax(expr: str, *, columns: set, ops=DEFAULT_OPS) -> None:
    """Python-ast whitelist pass only. Never imports sympy."""
    _check_columns(columns, ops)
    try:
        _walk(_parse(expr), set(columns), ops)
    except RecursionError:
        raise SignalExprError('expression too deeply nested') from None


# ── sympy pass ────────────────────────────────────────────────────────────────
def _sympy_parse(expr, names, ops, *, evaluate):
    import sympy
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations
    local = {c: sympy.Symbol(c) for c in names}
    local.update({o: sympy.Function(o) for o in ops})
    local['abs'] = sympy.Abs
    glob = {'__builtins__': {}, 'Symbol': sympy.Symbol, 'Integer': sympy.Integer,
            'Float': sympy.Float, 'Rational': sympy.Rational,
            # evaluate=False rewrites BinOps into these constructor calls
            'Add': sympy.Add, 'Mul': sympy.Mul, 'Pow': sympy.Pow}
    try:
        return parse_expr(expr.strip(), local_dict=local, global_dict=glob,
                          transformations=standard_transformations, evaluate=evaluate)
    except SignalExprError:
        raise
    except Exception as e:
        raise SignalExprError(f'cannot parse expression: {e}') from None


def formula_hash(expr: str, *, columns=None, ops=DEFAULT_OPS) -> str:
    """16-hex canonical hash (commutative-order invariant).  columns=None treats
    every non-op Name as a column (generate_signatures uses this form)."""
    import sympy
    tree = _parse(expr)
    if columns is None:
        names = _walk(tree, None, ops)
        _check_columns(names, ops)
    else:
        _check_columns(columns, ops)
        names = _walk(tree, set(columns), ops)
    canon = _sympy_parse(expr, names, ops, evaluate=True)
    return hashlib.sha256(sympy.srepr(canon).encode()).hexdigest()[:16]


def make_panel(fields: Mapping):
    """Two-level (field, ticker) panel from {field: date x ticker frame}."""
    import pandas as pd
    return pd.concat(dict(fields), axis=1, names=['field', 'ticker'])


class CompiledSignal:
    def __init__(self, expr, columns, required_lookback, formula_hash_, tree):
        self.expr = expr
        self.columns = frozenset(columns)
        self.required_lookback = int(required_lookback)
        self.formula_hash = formula_hash_
        self._tree = tree

    def __repr__(self):
        return f'CompiledSignal({self.expr!r}, lookback={self.required_lookback}, hash={self.formula_hash})'

    def __call__(self, panel):
        import numpy as np
        import pandas as pd
        from strategies import indicators as ind
        from strategies.safe_ops import safe_div
        import sympy
        from sympy.core.function import AppliedUndef

        cache = {}

        def field(name):
            if name not in cache:
                try:
                    cache[name] = panel[name].astype(float)
                except KeyError:
                    raise SignalExprError(f"panel has no field '{name}'") from None
            return cache[name]

        def percol(frame, fn):
            if not isinstance(frame, pd.DataFrame):
                raise SignalExprError('windowed operator applied to a constant')
            out = pd.DataFrame({i: fn(frame.iloc[:, i].dropna()).reindex(frame.index)
                                for i in range(frame.shape[1])}, index=frame.index)
            out.columns = frame.columns
            return out.astype(float)

        def atr(n):
            h, lo, c = field('high'), field('low'), field('close')
            cols = {}
            for t in c.columns:
                hlc = pd.DataFrame({'high': h[t], 'low': lo[t], 'close': c[t]}).dropna()
                cols[t] = ind.atr(hlc, n, method='true_range',
                                  fallback_pct=None).reindex(c.index)
            out = pd.DataFrame(cols, index=c.index)
            out.columns = c.columns
            return out.astype(float)

        def ev(node):
            if isinstance(node, sympy.Symbol):
                return field(node.name)
            if isinstance(node, sympy.Number):
                return np.float64(float(node))
            if isinstance(node, sympy.Add):
                acc = ev(node.args[0])
                for a in node.args[1:]:
                    acc = acc + ev(a)
                return acc
            if isinstance(node, sympy.Mul):
                acc = ev(node.args[0])
                for a in node.args[1:]:
                    acc = acc * ev(a)
                return acc
            if isinstance(node, sympy.Pow):
                base, exp = node.args
                if isinstance(exp, sympy.Number) and float(exp) < 0:
                    d = ev(base) ** abs(float(exp)) if float(exp) != -1 else ev(base)
                    return safe_div(1.0, d, fill=np.nan)
                with np.errstate(invalid='ignore', divide='ignore', over='ignore'):
                    return ev(base) ** ev(exp)
            if isinstance(node, sympy.Abs):
                return abs(ev(node.args[0]))
            if isinstance(node, AppliedUndef):
                f = node.func.__name__
                if f == 'atr':
                    return atr(int(node.args[0]))
                x = ev(node.args[0])
                if f == 'rank_pct':
                    if not isinstance(x, pd.DataFrame):
                        raise SignalExprError('rank_pct applied to a constant')
                    return x.rank(axis=1, pct=True)
                n = int(node.args[1])
                fn = {
                    'sma': lambda s: ind.sma(s, n),
                    'ema': lambda s: ind.ema(s, n),
                    'std': lambda s: s.rolling(n).std(),
                    'zscore': lambda s: ind.zscore(s, n),
                    'rsi': lambda s: ind.rsi(s, n),
                    'max': lambda s: s.rolling(n).max(),
                    'min': lambda s: s.rolling(n).min(),
                    'lag': lambda s: s.shift(n),
                    'pct_change': lambda s: safe_div(s, s.shift(n), fill=np.nan) - 1,
                }[f]
                return percol(x, fn)
            raise SignalExprError(f'unsupported node {type(node).__name__}')

        out = ev(self._tree)
        if not isinstance(out, pd.DataFrame):
            raise SignalExprError('expression did not produce a frame')
        return out.replace([np.inf, -np.inf], np.nan)


def _lookback(node) -> int:
    import sympy
    from sympy.core.function import AppliedUndef
    if isinstance(node, sympy.Symbol):
        return 1
    if isinstance(node, sympy.Number):
        return 0
    if isinstance(node, (sympy.Add, sympy.Mul, sympy.Pow, sympy.Abs)):
        return max(_lookback(a) for a in node.args)
    if isinstance(node, AppliedUndef):
        f = node.func.__name__
        if f == 'atr':
            return int(node.args[0]) + 1
        lb = _lookback(node.args[0])
        if f == 'rank_pct':
            return lb
        n = int(node.args[1])
        return lb + n - 1 if f in ('sma', 'std', 'zscore', 'max', 'min', 'ema') else lb + n
    raise SignalExprError(f'unsupported node {type(node).__name__}')


def compile_signal(expr: str, *, columns: set, ops=DEFAULT_OPS) -> CompiledSignal:
    _check_columns(columns, ops)
    tree = _parse(expr)
    try:
        used = _walk(tree, set(columns), ops)
    except RecursionError:
        raise SignalExprError('expression too deeply nested') from None
    if not used:
        raise SignalExprError('expression references no column')
    st = _sympy_parse(expr, used, ops, evaluate=False)
    return CompiledSignal(expr, used, _lookback(st),
                          formula_hash(expr, columns=columns, ops=ops), st)
