#!/usr/bin/env python3
"""Register EVERY oxf_* strategy as a research candidate (manifest state=candidate
+ strategy_registry pending_approval row). Idempotent — safe to re-run. Discovers
classes the same way the contract test does. Reuses register_one() from
register_oxford_strategy.py (the verified lifecycle API)."""
import os, sys, importlib, pkgutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import psycopg2
from strategies.oxford_crabel import OxfordBaseStrategy
import strategies.implementations as impl
sys.path.insert(0, os.path.dirname(__file__))
from register_oxford_strategy import register_one


def discover():
    out = []
    for m in pkgutil.iter_modules(impl.__path__):
        if not m.name.startswith('oxf_'):
            continue
        mod = importlib.import_module(f'strategies.implementations.{m.name}')
        for obj in vars(mod).values():
            if (isinstance(obj, type) and issubclass(obj, OxfordBaseStrategy)
                    and obj is not OxfordBaseStrategy):
                out.append(obj)
    # stable order by id
    return sorted(set(out), key=lambda c: c.id)


def main():
    root = os.path.join(os.path.dirname(__file__), '..')
    manifest_path = os.path.join(root, 'src', 'strategies', 'manifest.json')
    classes = discover()
    print(f'discovered {len(classes)} oxf strategies')
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        for cls in classes:
            register_one(cls.id, cls.__name__, cls.name, cls.description,
                         manifest_path, conn)
    finally:
        conn.close()
    print(f'done: {len(classes)} registered (idempotent)')


if __name__ == '__main__':
    main()
