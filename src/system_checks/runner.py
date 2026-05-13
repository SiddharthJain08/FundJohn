"""Runner — execute one or many checks, with timing, dep-skip, error capture."""
from __future__ import annotations

import os
import time
import traceback
from pathlib import Path

from .registry import get, all_checks
from .types import CheckResult, Status


def _dep_available(dep: str) -> tuple[bool, str]:
    """Return (available, reason_if_not). 'reason_if_not' is empty when available."""
    if dep == 'fs':
        return True, ''
    if dep == 'db':
        if not os.environ.get('POSTGRES_URI'):
            return False, 'POSTGRES_URI not set'
        return True, ''
    if dep == 'broker':
        if not (os.environ.get('ALPACA_API_KEY') and os.environ.get('ALPACA_SECRET_KEY')):
            return False, 'ALPACA_API_KEY/SECRET not set'
        return True, ''
    if dep == 'llm':
        if not Path('/usr/local/bin/claude-bin').exists():
            return False, 'claude-bin not found'
        return True, ''
    if dep == 'discord':
        if not os.environ.get('DISCORD_BOT_TOKEN'):
            return False, 'DISCORD_BOT_TOKEN not set'
        return True, ''
    return False, f'unknown dep {dep!r}'


def run_one(name: str) -> CheckResult:
    meta = get(name)
    tags = meta['tags']
    requires = meta['requires']

    for dep in requires:
        ok, reason = _dep_available(dep)
        if not ok:
            return CheckResult(
                name=name, status=Status.SKIP, detail=reason,
                tags=tags, requires=requires, skipped_dep=dep,
            )

    t0 = time.monotonic()
    try:
        status, detail = meta['fn']()
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return CheckResult(
            name=name, status=Status.ERROR, detail=str(exc)[:200],
            elapsed_ms=elapsed_ms, tags=tags, requires=requires,
            error=traceback.format_exc()[-500:],
        )
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # Validate fn return shape — checks should return (Status, str).
    if not isinstance(status, Status):
        return CheckResult(
            name=name, status=Status.ERROR,
            detail=f'check returned non-Status {type(status).__name__}: {status!r}',
            elapsed_ms=elapsed_ms, tags=tags, requires=requires,
        )
    return CheckResult(
        name=name, status=status, detail=str(detail)[:500],
        elapsed_ms=elapsed_ms, tags=tags, requires=requires,
    )


def run_all(tags: list[str] | None = None, names: list[str] | None = None) -> list[CheckResult]:
    """Run every registered check, optionally filtered.

    tags  — only run checks carrying at least one of these tags
    names — only run checks with these exact names
    """
    selected = []
    for name, meta in all_checks().items():
        if names is not None and name not in names:
            continue
        if tags is not None and not (set(tags) & set(meta['tags'])):
            continue
        selected.append(name)
    return [run_one(n) for n in selected]


def summarize(results: list[CheckResult]) -> dict:
    """Aggregate counts for top-line reporting."""
    counts = {s.value: 0 for s in Status}
    for r in results:
        counts[r.status.value] += 1
    total = len(results)
    return {'total': total, **counts}
