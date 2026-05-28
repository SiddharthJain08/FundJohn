"""GLW replay: verify S15 fires SHORT before the 2026-05-28 crash."""
import os
import pandas as pd
import pytest
from src.strategies.implementations.s15_insider_opportunistic_short import (
    OpportunisticInsiderShort,
)
from src.strategies import aux_data_loader as adl


INSIDER_PATH = adl.INSIDER_PATH
PRICES_PATH = adl.ROOT / 'data' / 'master' / 'prices.parquet'

pytestmark = pytest.mark.skipif(
    not INSIDER_PATH.exists() or not PRICES_PATH.exists(),
    reason='master parquets not available in this env',
)


def test_s15_fires_glw_short_before_2026_05_28_crash(monkeypatch):
    monkeypatch.setenv('OPENCLAW_S15_INSIDER_OPPORTUNISTIC', '1')

    as_of = '2026-05-22'

    prices_long = pd.read_parquet(PRICES_PATH)
    glw = prices_long[prices_long['ticker'] == 'GLW'].copy()
    if glw.empty:
        pytest.skip('GLW not in prices parquet')
    glw['date'] = pd.to_datetime(glw['date'])
    glw = glw.sort_values('date')
    cutoff = pd.Timestamp(as_of)
    glw = glw[glw['date'] <= cutoff]
    prices = pd.DataFrame({'GLW': glw.set_index('date')['close']})

    aux = adl.load_aux_data(as_of, strategy_id='S15_insider_opportunistic_short')
    glw_short = aux['insider_txns'].get('GLW', [])
    glw_long = aux['insider_history_long'].get('GLW', [])
    if not glw_short:
        pytest.skip('No GLW insider txns in 45d slice — fixture stale')

    s = OpportunisticInsiderShort()
    regime = {'state': 'LOW_VOL'}
    signals = s.generate_signals(prices, regime, ['GLW'], aux_data=aux)

    assert len(signals) == 1, (
        f"expected SHORT signal on GLW; got {len(signals)} signals. "
        f"glw_short_count={len(glw_short)}, glw_long_count={len(glw_long)}"
    )
    sig = signals[0]
    assert sig.direction == 'SHORT'
    assert sig.signal_params['opportunistic_count'] >= 2, (
        f"expected >=2 opportunistic sellers; got opp={sig.signal_params.get('opportunistic_count')}, "
        f"routine={sig.signal_params.get('routine_count')}"
    )
    assert sig.signal_params['c_suite_present'] is True, (
        f"expected C-suite present in GLW cluster"
    )
