"""Check registry + @check decorator.

Each check is a zero-arg function returning (Status, detail_str) — the
runner wraps it with timing, exception handling, and dependency skip logic.

Dependencies are declared upfront so callers can filter ("skip broker
checks; ALPACA_API_KEY isn't set") instead of letting checks fail mid-run.
Recognized dep keys (see runner._dep_available):
    fs       — filesystem read (always available)
    db       — POSTGRES_URI set + container reachable
    broker   — ALPACA_API_KEY + ALPACA_SECRET_KEY in env
    llm      — /usr/local/bin/claude-bin exists + executable
    discord  — DISCORD_BOT_TOKEN in env
"""
from __future__ import annotations

from typing import Callable

# name → metadata dict
_REGISTRY: dict[str, dict] = {}


def check(name: str, *, tags: list[str], requires: list[str] | None = None) -> Callable:
    """Register a check. Decorated function must return (Status, str)."""
    def decorator(fn: Callable):
        if name in _REGISTRY:
            raise ValueError(f'system_checks: duplicate check name {name!r}')
        _REGISTRY[name] = {
            'fn': fn,
            'tags': list(tags),
            'requires': list(requires or []),
        }
        return fn
    return decorator


def all_checks() -> dict[str, dict]:
    """Return a shallow copy of the registry. For iteration only."""
    return dict(_REGISTRY)


def get(name: str) -> dict:
    if name not in _REGISTRY:
        raise KeyError(f'unknown check: {name!r}; known: {sorted(_REGISTRY)}')
    return _REGISTRY[name]


def _reset_for_tests() -> None:
    """Test-only: wipe the registry so duplicate-name tests can re-register."""
    _REGISTRY.clear()
