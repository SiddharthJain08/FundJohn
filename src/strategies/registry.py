from .implementations.shv13_call_put_iv_spread import CallPutIVSpread
from .implementations.shv14_otm_skew_factor import OTMSkewFactor
from .implementations.shv15_iv_term_structure import IVTermStructure
from .implementations.shv16_gex_regime import GEXRegime
from .implementations.shv17_earnings_straddle_fade import EarningsStraddleFade
from .implementations.shv19_iv_surface_tilt import IVSurfaceTilt
from .implementations.shv20_iv_dispersion_reversion import IVDispersionReversion
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
    'max_pain':          ('strategies.implementations.s5_max_pain',          'MaxPainGravity'),
    'dual_momentum':     ('strategies.implementations.s09_dual_momentum',     'DualMomentum'),
    'quality_value':     ('strategies.implementations.s10_quality_value',     'QualityValue'),
    'insider_cluster_buy': ('strategies.implementations.s12_insider', 'InsiderClusterBuy'),
    'iv_rv_arb':         ('strategies.implementations.s15_iv_rv_arb',         'IVRVArb'),
    'jt_momentum_12mo':  ('strategies.implementations.S_custom_jt_momentum_12mo', 'JTMomentum12Mo'),
    # S-HV7  S-HV12: hardcoded HV strategies (zero LLM tokens)
    'S_HV7_iv_crush_fade':           ('strategies.implementations.shv7_iv_crush_fade',            'IVCrushFade'),
    'S_HV8_gamma_theta_carry':       ('strategies.implementations.shv8_gamma_theta_carry',        'GammaThetaCarry'),
    'S_HV9_rv_momentum_div':         ('strategies.implementations.shv9_rv_momentum_div',          'RVMomentumDivergence'),
    'S_HV10_triple_gate_fear':       ('strategies.implementations.shv10_triple_gate_fear',        'TripleGateFear'),
    'S_HV11_cross_stock_dispersion': ('strategies.implementations.shv11_cross_stock_dispersion',  'CrossStockDispersion'),
    'S_HV12_vrp_normalization':      ('strategies.implementations.shv12_vrp_normalization',       'VRPNormalization'),
    # Legacy aliases — keeps existing DB signal records valid
    'S5_max_pain':       ('strategies.implementations.s5_max_pain',          'MaxPainGravity'),
    'S9_dual_momentum':  ('strategies.implementations.s09_dual_momentum',     'DualMomentum'),
    'S10_quality_value': ('strategies.implementations.s10_quality_value',     'QualityValue'),
    'S12_insider':       ('strategies.implementations.s12_insider', 'InsiderClusterBuy'),
    'S15_iv_rv_arb':     ('strategies.implementations.s15_iv_rv_arb',         'IVRVArb'),
    'S_custom_jt_momentum_12mo': ('strategies.implementations.S_custom_jt_momentum_12mo', 'JTMomentum12Mo'),
    'S23_regime_momentum':      ('strategies.implementations.S23_regime_momentum',      'RegimeMomentumStrategy'),
    'S24_52wk_high_proximity':  ('strategies.implementations.S24_52wk_high_proximity',  'FiftyTwoWeekHighProximityStrategy'),
    'S25_dual_momentum_v2':     ('strategies.implementations.S25_dual_momentum',         'DualMomentum'),
    'S_HV13_call_put_iv_spread': CallPutIVSpread,
    'S_HV14_otm_skew_factor': OTMSkewFactor,
    'S_HV15_iv_term_structure': IVTermStructure,
    'S_HV16_gex_regime': GEXRegime,
    'S_HV17_earnings_straddle_fade': EarningsStraddleFade,
    'S_HV19_iv_surface_tilt': IVSurfaceTilt,
    'S_HV20_iv_dispersion_reversion': IVDispersionReversion,

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
