"""
Strategy Registry — loads and validates all approved strategy implementations.
The execution engine calls get_approved_strategies() to get the active set.
"""

import importlib
import os
import sys
import logging
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# Map strategy DB id → Python class
# Canonical IDs (no numeric prefix)
_IMPL_MAP = {
    'max_pain':          ('src.strategies.implementations.max_pain',          'MaxPainGravity'),
    'dual_momentum':     ('src.strategies.implementations.dual_momentum',     'DualMomentum'),
    'quality_value':     ('src.strategies.implementations.quality_value',     'QualityValue'),
    'insider_cluster_buy': ('src.strategies.implementations.insider_cluster_buy', 'InsiderClusterBuy'),
    'iv_rv_arb':         ('src.strategies.implementations.iv_rv_arb',         'IVRVArb'),
    'jt_momentum_12mo':  ('src.strategies.implementations.S_custom_jt_momentum_12mo', 'JTMomentum12Mo'),
    # Legacy aliases — keeps existing DB signal records valid
    'S5_max_pain':       ('src.strategies.implementations.max_pain',          'MaxPainGravity'),
    'S9_dual_momentum':  ('src.strategies.implementations.dual_momentum',     'DualMomentum'),
    'S10_quality_value': ('src.strategies.implementations.quality_value',     'QualityValue'),
    'S12_insider':       ('src.strategies.implementations.insider_cluster_buy', 'InsiderClusterBuy'),
    'S15_iv_rv_arb':     ('src.strategies.implementations.iv_rv_arb',         'IVRVArb'),
    'S_custom_jt_momentum_12mo': ('src.strategies.implementations.S_custom_jt_momentum_12mo', 'JTMomentum12Mo'),
}


def load_strategy_class(strategy_id: str):
    """Import and return the class for a given strategy_id. Returns None on failure."""
    if strategy_id not in _IMPL_MAP:
        logger.warning(f"No implementation registered for strategy_id={strategy_id}")
        return None

    module_path, class_name = _IMPL_MAP[strategy_id]
    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        return cls
    except Exception as e:
        logger.error(f"Failed to load {strategy_id}: {e}")
        return None


def get_approved_strategies(db_rows: List[dict]) -> List:
    """
    Given a list of strategy_registry rows from Postgres (dicts), return
    instantiated strategy objects for those that are approved and have an
    implementation registered.

    db_rows expected fields: id, parameters (dict), status
    """
    instances = []
    for row in db_rows:
        sid    = row.get('id')
        status = row.get('status', '')
        if status != 'approved':
            continue

        cls = load_strategy_class(sid)
        if cls is None:
            continue

        params = row.get('parameters') or {}
        try:
            instance = cls(parameters=params)
            # Override id with the DB row id so signals are written with the correct FK key
            instance.id = sid
            instances.append(instance)
            logger.info(f"Loaded strategy: {sid}")
        except Exception as e:
            logger.error(f"Failed to instantiate {sid}: {e}")

    return instances


def list_registered_ids() -> List[str]:
    """Return all strategy IDs with registered implementations."""
    return list(_IMPL_MAP.keys())


def validate_all() -> Dict[str, bool]:
    """Try importing all registered implementations. Returns {id: ok}."""
    results = {}
    for sid in _IMPL_MAP:
        cls = load_strategy_class(sid)
        results[sid] = cls is not None
    return results
