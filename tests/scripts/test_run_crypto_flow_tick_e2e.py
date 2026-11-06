"""End-to-end dry-run tick: the REAL DeribitVol / StablesDefi / Derivs / MacroSent providers fed from the repo
fixtures (each provider module's get_json routed by URL substring) → every market key the dashboard reads and the
core BTC-USD per-ticker fields must come out non-None. Guards the cross-task field-name contract (I1)."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingestion.crypto_flow import universe
from src.ingestion.crypto_flow.providers import deribit_vol, derivs, macro_sent, stables_defi
from src.ingestion.crypto_flow.providers.deribit_vol import DeribitVol
from src.ingestion.crypto_flow.providers.derivs import Derivs
from src.ingestion.crypto_flow.providers.macro_sent import MacroSent
from src.ingestion.crypto_flow.providers.stables_defi import StablesDefi

ROOT = Path(__file__).resolve().parents[2]
FX = ROOT / 'tests' / 'ingestion' / 'fixtures' / 'crypto_flow'
_spec = importlib.util.spec_from_file_location('run_crypto_flow_tick_e2e', ROOT / 'scripts' / 'run_crypto_flow_tick.py')
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)

NOW = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)  # same as the fixture tests: Deribit expiries clear 7 DTE
DASHBOARD_TILE_KEYS = ('dvol_btc', 'atm_iv_30d_btc', 'skew_25d_btc', 'put_call_oi_btc', 'stable_supply_usd',
                       'btc_dominance', 'fear_greed', 'oi_usd_total')  # read by server.js _renderCryptoTiles
DASHBOARD_MARKET_KEYS = DASHBOARD_TILE_KEYS + ('whale_score_btc',)  # + the market row's BTC whale score


def fx(name):
    return json.loads((FX / name).read_text())['payload']


ROUTES = {  # distinctive substrings only ('global' alone would also hit Binance globalLongShortAccountRatio)
    'get_volatility_index_data': fx('deribit_dvol.json'),
    'get_book_summary_by_currency': fx('deribit_book_summary.json'),
    'stablecoins.llama.fi': fx('llama_stablecoins.json'),
    'api.llama.fi/v2/chains': fx('llama_chains.json'),
    'overview/dexs': fx('llama_dexs.json'),
    'api.hyperliquid.xyz': fx('hl_meta_and_ctxs.json'),
    'fapi/v1/openInterest': fx('binance_oi.json'),
    'premiumIndex': fx('binance_premium.json'),
    'globalLongShortAccountRatio': fx('binance_global_ls.json'),
    'topLongShortPositionRatio': fx('binance_top_ls.json'),
    'takerlongshortRatio': fx('binance_taker.json'),
    'okx.com/api/v5/public/open-interest': fx('okx_oi.json'),
    'api/v3/coins/markets': fx('coingecko_markets.json'),
    'api/v3/global': fx('coingecko_global.json'),
    'alternative.me/fng': fx('fng.json'),
}


def _router(url, **kw):
    for key, val in ROUTES.items():
        if key in url:
            return val
    return None


def test_dry_run_tick_fills_every_dashboard_market_key(monkeypatch, tmp_path):
    for mod in (deribit_vol, derivs, macro_sent, stables_defi):
        monkeypatch.setattr(mod, 'get_json', _router)
    monkeypatch.setattr(derivs.time, 'sleep', lambda s: None)
    seen = {}
    real_m, real_t = t.F.compute_market_features, t.F.compute_ticker_features

    def spy_market(*a, **k):
        seen['market'] = real_m(*a, **k)
        return seen['market']

    def spy_ticker(ticker, *a, **k):
        out = real_t(ticker, *a, **k)
        seen.setdefault('per', {})[ticker] = out
        return out
    monkeypatch.setattr(t.F, 'compute_market_features', spy_market)
    monkeypatch.setattr(t.F, 'compute_ticker_features', spy_ticker)
    sp = tmp_path / 'state.json'
    c = t.run_tick(now_utc=NOW, assets=universe.load_universe(),
                   providers=[Derivs(), DeribitVol(), StablesDefi(), MacroSent()], conn=None, state_path=sp,
                   dry_run=True, events_path=tmp_path / 'ev.parquet', flows_path=tmp_path / 'fl.parquet',
                   onchain_daily_path=tmp_path / 'od.parquet')
    assert c['providers_ok'] == 4 and c['providers_failed'] == 0, c
    m = seen['market']
    missing = [k for k in DASHBOARD_MARKET_KEYS if m.get(k) is None]
    assert not missing, missing
    assert m['stable_supply_missing'] is None or isinstance(m['stable_supply_missing'], str)
    assert m['dvol_eth'] is not None and m['options_degraded_btc'] is False
    btc = seen['per']['BTC-USD']
    assert all(btc.get(k) is not None for k in ('price_usd', 'funding_8h', 'oi_usd')), btc
    assert btc['price_spot_usd'] is not None  # macro_sent spot_px reaches price_spot_usd
    assert not sp.exists()  # dry-run: real state untouched


@pytest.mark.parametrize('key', DASHBOARD_TILE_KEYS)
def test_dashboard_reads_each_market_key(key):
    """Keeps DASHBOARD_TILE_KEYS honest: every key is actually read by the market tiles renderer (`m.<key>`)."""
    src = (ROOT / 'src' / 'channels' / 'api' / 'server.js').read_text()
    assert f'm.{key}' in src
