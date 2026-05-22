"""AST-based linter for universe_filter predicates.

Enforces:
  1. Signature: def universe_filter(meta, as_of) -> bool
  2. No imports of datetime, time, os, calendar in the predicate module
  3. No first-order callees that themselves import the forbidden modules

Transitive scan allowlist: src.strategies.universe_meta is the canonical
input-type module and is permitted to import datetime (for date-typed fields).
"""
from __future__ import annotations
import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_IMPORTS = {"datetime", "time", "os", "calendar"}
EXPECTED_PARAMS = ("meta", "as_of")

# Modules whose own imports are definitionally allowed (input-type definitions,
# not predicate logic).
TRANSITIVE_ALLOWLIST = {
    "src.strategies.universe_meta",
}


@dataclass
class LintError:
    path: Path
    line: int
    message: str


def _find_universe_filter(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "universe_filter":
            return node
    return None


def _signature_ok(fn: ast.FunctionDef) -> bool:
    args = fn.args.args
    if len(args) != len(EXPECTED_PARAMS):
        return False
    return [a.arg for a in args] == list(EXPECTED_PARAMS)


def _forbidden_imports(tree: ast.AST) -> list[str]:
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in FORBIDDEN_IMPORTS:
                bad.append(node.module)
    return bad


def _local_imports(tree: ast.AST) -> list[str]:
    """Return import paths that look local (start with src. or tests.)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith(("src.", "tests.")):
                out.append(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(("src.", "tests.")):
                    out.append(alias.name)
    return out


def _module_file(import_path: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(import_path)
        if spec and spec.origin:
            return Path(spec.origin)
    except (ImportError, ValueError, ModuleNotFoundError):
        pass
    return None


def scan_module(path: Path) -> list[LintError]:
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    errors: list[LintError] = []

    fn = _find_universe_filter(tree)
    if fn is None:
        return errors  # no predicate in this module; nothing to lint

    if not _signature_ok(fn):
        errors.append(LintError(path, fn.lineno,
            "universe_filter signature must be (meta, as_of)"))

    for bad in _forbidden_imports(tree):
        errors.append(LintError(path, 1,
            f"forbidden import in predicate module: {bad}"))

    for local in _local_imports(tree):
        if local in TRANSITIVE_ALLOWLIST:
            continue
        f = _module_file(local)
        if f is None or not f.exists():
            continue
        callee_tree = ast.parse(f.read_text(), filename=str(f))
        bad_transitive = _forbidden_imports(callee_tree)
        if bad_transitive:
            errors.append(LintError(path, 1,
                f"transitive forbidden import via {local}: {bad_transitive}"))

    return errors
