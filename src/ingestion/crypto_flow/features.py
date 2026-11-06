"""Pure feature computation + whale-pressure score for the crypto flow engine (no I/O)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd

from .models import MARKET_TICKER

DEFAULT_THRESHOLDS = {'delev_oi_chg_1h_pct': -5.0, 'delev_ret_1h_pct': -3.0, 'netflow_z_shock': 3.0,
                      'coverage_floor': 0.25, 'score_alert': 70}
FEATURE_COLS = ['whale_n_1h', 'whale_usd_1h', 'whale_n_24h', 'whale_usd_24h', 'exch_in_usd_24h', 'exch_out_usd_24h',
                'exch_netflow_usd_24h', 'exch_netflow_z_7d', 'attribution_coverage', 'oi_usd', 'oi_venues',
                'oi_chg_1h_pct', 'oi_chg_24h_pct', 'funding_8h', 'funding_ann', 'premium', 'ls_ratio_global',
                'ls_ratio_top', 'taker_buy_sell', 'oi_price_divergence', 'large_print_imbalance_1h',
                'large_print_usd_1h', 'liq_long_usd_1h', 'liq_short_usd_1h', 'whale_perp_net_usd', 'price_usd',
                'ret_1h', 'ret_24h', 'delev_1h', 'whale_score', 'whale_direction', 'whale_score_components']
# netflow component saturates (40 pts) at $10M of net exchange flow
_NETFLOW_PTS_PER_DECADE = 40.0 / 7.0


def _f(x):
    """Number -> python float; None/NaN/non-numeric -> None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize('UTC') if t.tzinfo is None else t.tz_convert('UTC')


def _rows(df, ticker) -> pd.DataFrame:
    """Rows of `df` for `ticker` with a normalised UTC ts_utc column; tolerates empty / column-less frames."""
    if df is None or len(df) == 0 or 'ticker' not in df.columns or 'ts_utc' not in df.columns:
        return pd.DataFrame(columns=['ticker', 'ts_utc'])
    out = df[df['ticker'] == ticker].copy()
    out['ts_utc'] = pd.to_datetime(out['ts_utc'], utc=True)
    return out


def exchange_netflow(events: pd.DataFrame, ticker: str, now: datetime, hours: int) -> dict:
    ev = _rows(events, ticker)
    t1 = _ts(now)
    ev = ev[(ev['ts_utc'] > t1 - timedelta(hours=hours)) & (ev['ts_utc'] <= t1)] if len(ev) else ev
    usd = pd.to_numeric(ev.get('amount_usd'), errors='coerce').fillna(0.0) if len(ev) else pd.Series(dtype=float)
    d = ev['direction'] if len(ev) else pd.Series(dtype=object)
    in_usd = float(usd[d.isin(['exchange_in', 'exchange_to_exchange'])].sum())
    out_usd = float(usd[d.isin(['exchange_out', 'exchange_to_exchange'])].sum())
    total = float(usd.sum())
    attributed = float(usd[d.isin(['exchange_in', 'exchange_out', 'exchange_to_exchange'])].sum())
    return {'whale_n': int(len(ev)), 'whale_usd': total, 'exch_in_usd': in_usd, 'exch_out_usd': out_usd,
            'exch_netflow_usd': in_usd - out_usd, 'attribution_coverage': attributed / total if total > 0 else 0.0}


def netflow_zscore(hourly_netflow: pd.Series, current: float) -> float | None:
    s = pd.to_numeric(hourly_netflow, errors='coerce').dropna()
    if len(s) < 48 or current is None:
        return None
    sd = float(s.std())
    if not sd > 0:
        return None
    return (float(current) - float(s.mean())) / sd


def deleveraging_flag(oi_chg_1h_pct, ret_1h_pct, thr=DEFAULT_THRESHOLDS) -> bool:
    if oi_chg_1h_pct is None or ret_1h_pct is None:
        return False
    return bool(oi_chg_1h_pct <= thr['delev_oi_chg_1h_pct'] and ret_1h_pct <= thr['delev_ret_1h_pct'])


def oi_price_divergence(oi_chg_24h_pct, ret_24h_pct) -> str:
    if not oi_chg_24h_pct or not ret_24h_pct:  # None or exactly zero
        return 'none'
    if oi_chg_24h_pct > 0:
        return 'long_build' if ret_24h_pct > 0 else 'short_build'
    return 'covering' if ret_24h_pct > 0 else 'deleveraging'


def _direction(netflow_z, tape_imbalance_1h) -> str:
    imb = tape_imbalance_1h or 0
    if netflow_z is None:
        return 'neutral'
    if netflow_z >= 1 and imb <= 0:
        return 'distributing'
    if netflow_z <= -1 and imb >= 0:
        return 'accumulating'
    return 'neutral'


def whale_score(*, exch_netflow_usd_24h, attribution_coverage, whale_usd_24h, whale_usd_30d_mean, agreement_share,
                deriv_confirm, tape_imbalance_1h, netflow_z, return_components: bool = False):
    cov_w = min(1.0, attribution_coverage / DEFAULT_THRESHOLDS['coverage_floor'])
    netflow = round(min(40.0, _NETFLOW_PTS_PER_DECADE * math.log10(max(abs(exch_netflow_usd_24h), 1.0)) * cov_w))
    vel = 0
    if whale_usd_30d_mean:
        vel = round(min(25.0, 8.3 * math.log2(max(whale_usd_24h / whale_usd_30d_mean, 1.0))))
    comps = {'netflow': int(netflow), 'velocity': int(vel),
             'agreement': round(20 * agreement_share) if agreement_share is not None else 0,
             'derivatives': round(15 * deriv_confirm) if deriv_confirm is not None else 0}
    score, direction = int(sum(comps.values())), _direction(netflow_z, tape_imbalance_1h)
    return (score, direction, comps) if return_components else (score, direction)


def deriv_confirm(direction_sign: int, funding_8h, ls_ratio_global, taker_buy_sell):
    """Share of non-None derivatives factors leaning with the move (+1 distributing/bearish, -1 accumulating)."""
    leans = []
    for v, ref in ((funding_8h, 0.0), (ls_ratio_global, 1.0), (taker_buy_sell, 1.0)):
        if v is not None:
            leans.append((v < ref) if direction_sign > 0 else (v > ref))
    return sum(leans) / len(leans) if leans else None


def agreement_share(per_ticker_netflow_sign: dict, btc_sign: int):
    alts = [s for t, s in per_ticker_netflow_sign.items() if t != 'BTC-USD' and s]
    if len(alts) < 3 or not btc_sign:
        return None
    return sum(1 for s in alts if s == btc_sign) / len(alts)


def _oi_change(hist: pd.DataFrame, now: pd.Timestamp, back: timedelta, tol: timedelta, oi, venues):
    """% change of `oi` vs the nearest earlier hist row within `tol` of now-back with the same venue set."""
    if oi is None or not len(hist) or 'oi_usd' not in hist.columns or 'oi_venues' not in hist.columns:
        return None
    target = now - back
    h = hist[(hist['oi_venues'] == venues) & (hist['ts_utc'] <= target) & (hist['ts_utc'] >= target - tol)]
    h = h.dropna(subset=['oi_usd'])
    prev = _f(h.sort_values('ts_utc')['oi_usd'].iloc[-1]) if len(h) else None
    return (oi / prev - 1) * 100 if prev else None


def compute_ticker_features(ticker, now, events, flows_hist, obs, agreement_share, thr=DEFAULT_THRESHOLDS) -> dict:
    t = _ts(now)
    g = lambda k: _f(obs.get(k))  # noqa: E731
    h1, h24 = exchange_netflow(events, ticker, t, 1), exchange_netflow(events, ticker, t, 24)
    hist = _rows(flows_hist, ticker)
    hist = hist[hist['ts_utc'] < t]
    z = None
    if 'exch_netflow_usd_24h' in hist.columns:
        z = netflow_zscore(hist[hist['ts_utc'] >= t - timedelta(days=7)]['exch_netflow_usd_24h'], h24['exch_netflow_usd'])
    mean30 = None
    if 'whale_usd_24h' in hist.columns:
        w = pd.to_numeric(hist[hist['ts_utc'] >= t - timedelta(days=30)]['whale_usd_24h'], errors='coerce').dropna()
        mean30 = float(w.mean()) if len(w) >= 24 else None
    oi, venues = g('oi_usd'), obs.get('oi_venues')
    oi1 = _oi_change(hist, t, timedelta(hours=1), timedelta(minutes=30), oi, venues)
    oi24 = _oi_change(hist, t, timedelta(hours=24), timedelta(hours=2), oi, venues)
    ret1, ret24, imb = g('ret_1h'), g('ret_24h'), g('large_print_imbalance_1h')
    funding, ls, tk = g('funding_8h'), g('ls_ratio_global'), g('taker_buy_sell')
    sign = {'distributing': 1, 'accumulating': -1}.get(_direction(z, imb), 0)
    dc = deriv_confirm(sign, funding, ls, tk) if sign else None
    score, direction, comps = whale_score(
        exch_netflow_usd_24h=h24['exch_netflow_usd'], attribution_coverage=h24['attribution_coverage'],
        whale_usd_24h=h24['whale_usd'], whale_usd_30d_mean=mean30, agreement_share=agreement_share,
        deriv_confirm=dc, tape_imbalance_1h=imb, netflow_z=z, return_components=True)
    buy, sell = g('large_prints_usd_buy'), g('large_prints_usd_sell')
    return {
        'whale_n_1h': h1['whale_n'], 'whale_usd_1h': h1['whale_usd'], 'whale_n_24h': h24['whale_n'],
        'whale_usd_24h': h24['whale_usd'], 'exch_in_usd_24h': h24['exch_in_usd'], 'exch_out_usd_24h': h24['exch_out_usd'],
        'exch_netflow_usd_24h': h24['exch_netflow_usd'], 'exch_netflow_z_7d': z,
        'attribution_coverage': h24['attribution_coverage'], 'oi_usd': oi, 'oi_venues': venues,
        'oi_chg_1h_pct': oi1, 'oi_chg_24h_pct': oi24, 'funding_8h': funding,
        'funding_ann': funding * 3 * 365 if funding is not None else None, 'premium': g('premium'),
        'ls_ratio_global': ls, 'ls_ratio_top': g('ls_ratio_top'), 'taker_buy_sell': tk,
        'oi_price_divergence': oi_price_divergence(oi24, ret24), 'large_print_imbalance_1h': imb,
        'large_print_usd_1h': (buy or 0.0) + (sell or 0.0) if buy is not None or sell is not None else None,
        'liq_long_usd_1h': g('liq_long_usd_1h'), 'liq_short_usd_1h': g('liq_short_usd_1h'),
        'whale_perp_net_usd': g('whale_perp_net_usd'), 'price_usd': g('price_usd'), 'ret_1h': ret1, 'ret_24h': ret24,
        'delev_1h': deleveraging_flag(oi1, ret1, thr), 'whale_score': score, 'whale_direction': direction,
        'whale_score_components': comps}


def compute_market_features(per_ticker: dict, market_obs: dict, thr=DEFAULT_THRESHOLDS, *, flows_hist=None, now=None) -> dict:
    mo = market_obs or {}
    btc, eth = per_ticker.get('BTC-USD', {}), per_ticker.get('ETH-USD', {})
    supply, supply_chg = _f(mo.get('stable_supply_usd')), None
    if supply is not None and flows_hist is not None and now is not None:
        h = _rows(flows_hist, MARKET_TICKER)
        t = _ts(now) - timedelta(days=7)
        if len(h) and 'stable_supply_usd' in h.columns:
            h = h[(h['ts_utc'] <= t) & (h['ts_utc'] >= t - timedelta(hours=12))].dropna(subset=['stable_supply_usd'])
            prev = _f(h.sort_values('ts_utc')['stable_supply_usd'].iloc[-1]) if len(h) else None
            supply_chg = (supply / prev - 1) * 100 if prev else None
    ois = [_f(p.get('oi_usd')) for p in per_ticker.values()]
    ois = [o for o in ois if o is not None]
    delev = bool(btc.get('delev_1h') or eth.get('delev_1h'))
    shocks = []
    for tk, p in per_ticker.items():
        z = p.get('exch_netflow_z_7d')
        if z is not None and z >= thr['netflow_z_shock'] and (p.get('attribution_coverage') or 0) >= thr['coverage_floor']:
            shocks.append(f'shock ticker={tk} kind=netflow_z value={z}')
        if (p.get('whale_score') or 0) >= thr['score_alert'] and p.get('whale_direction') not in (None, 'neutral'):
            shocks.append(f"shock ticker={tk} kind=whale_score value={p['whale_score']}")
    if delev:
        shocks.append('shock ticker=__MARKET__ kind=delev value=1')
    out = {'stable_supply_usd': supply, 'stable_supply_chg_7d_pct': supply_chg}
    for k in ('btc_dominance', 'total_mcap_usd', 'fear_greed', 'tvl_usd', 'dex_vol_24h_usd'):
        out[k] = _f(mo.get(k))
    out.update({'oi_usd_total': float(sum(ois)) if ois else None, 'delev_market': delev,
                'whale_score_btc': btc.get('whale_score'), 'whale_score_eth': eth.get('whale_score'),
                'whale_direction_btc': btc.get('whale_direction'), 'agreement_share': _f(mo.get('agreement_share')),
                'cot_report_date': mo.get('cot_report_date')})
    for k in ('cot_lev_net', 'cot_lev_net_chg_1w', 'dvol_btc', 'atm_iv_30d_btc', 'skew_25d_btc', 'put_call_oi_btc',
              'san_btc_exchange_balance_30d_lag', 'san_eth_exchange_balance_30d_lag'):
        out[k] = _f(mo.get(k))
    out['providers_degraded'] = list(mo.get('providers_degraded') or [])
    out['shock_lines'] = shocks
    return out
