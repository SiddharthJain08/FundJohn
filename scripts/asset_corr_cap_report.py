#!/usr/bin/env python3
"""Measure-first calibration for the asset-correlation cluster cap.

For the CURRENT live book (Alpaca positions): build price-return correlation,
then sweep (corr_thr x cap_pct) and report, per cell: clusters found, gross
before->after, released $ (== DTBP freed at the position level), and the
sell-down (which currently-held names get trimmed). Read-only. Serial/nice.

Usage (with Alpaca + parquet env):
  nice -n 19 python3 scripts/asset_corr_cap_report.py
"""
import os, sys, json, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from execution import asset_correlation as ac
from execution import asset_correlation_filter as acf

ALPACA = os.environ.get('ALPACA_BIN', '/root/go/bin/alpaca')


def _positions():
    r = subprocess.run([ALPACA, 'position', 'list'], capture_output=True, text=True)
    return json.loads(r.stdout)


def main():
    pos = _positions()
    # signed target == current market value (the book we'd be capping)
    target = {p['symbol']: float(p['market_value']) for p in pos}
    conv = {p['symbol']: abs(float(p['market_value'])) for p in pos}  # proxy until run live
    # NAV from account equity
    acct = json.loads(subprocess.run([ALPACA, 'account', 'get'],
                                     capture_output=True, text=True).stdout)
    nav = float(acct['equity'])
    window = int(os.environ.get('OPENCLAW_ASSET_CORR_WINDOW', '63'))
    corr = ac.price_return_corr(list(target), window=window)
    if not corr:
        print('no correlation (parquet unavailable)'); return 1
    print(f'book: {len(target)} names, gross=${sum(abs(v) for v in target.values()):,.0f}, '
          f'NAV=${nav:,.0f}, window={window}d')
    for corr_thr in (0.6, 0.7, 0.8):
        for cap_pct in (0.15, 0.20, 0.25):
            out, audit = acf.cap_correlated_clusters(target, conv, corr, nav,
                                                     cap_pct=cap_pct, corr_thr=corr_thr)
            sells = [(t, round(target[t]), round(out[t]))
                     for t in target if abs(out[t]) < abs(target[t]) - 1.0]
            mb = [c['members'] for c in audit['clusters'] if len(c['members']) >= 2]
            print(f'thr={corr_thr} cap={cap_pct:.0%}: clusters>=2={len(mb)} '
                  f'gross ${audit["total_gross_before"]:,.0f}->${audit["total_gross_after"]:,.0f} '
                  f'released(DTBP freed)=${audit["released_usd"]:,.0f} sells={len(sells)}')
            if corr_thr == 0.7 and cap_pct == 0.20:
                print('   clusters:', mb)
                print('   sell-downs:', sells)
    return 0


if __name__ == '__main__':
    sys.exit(main())
