"""Contract test for the btree_index_integrity system_check (LRN-20260604-003).

Shape-only on purpose: the live verdict flips as corruption appears/repairs,
so asserting PASS/FAIL here would rot. The live validation is operational
(the check named the 4 corrupt indexes on 2026-06-04 before repair).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / 'src')):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

pytestmark = pytest.mark.integration


def test_check_registered_and_returns_contract_shape():
    from system_checks.registry import all_checks
    from system_checks.types import Status
    import system_checks.checks  # noqa: F401  (side-effect registration)

    registry = all_checks()
    assert 'btree_index_integrity' in registry, 'check must be registered'
    entry = registry['btree_index_integrity']
    assert 'storage' in entry['tags']

    status, detail = entry['fn']()
    assert isinstance(status, Status)
    assert status in (Status.PASS, Status.WARN, Status.FAIL, Status.SKIP, Status.ERROR)
    assert isinstance(detail, str) and len(detail) > 0
