#!/usr/bin/env python3
"""One-off: validate exchange_addresses.json on-chain; keep only active addresses, log the rest in _rejected."""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / 'src/ingestion/crypto_flow/exchange_addresses.json'
EVM = {'ETH', 'ARB', 'POL', 'AVAX', 'BASE', 'BNB'}
EVM_RE = re.compile(r'^0x[0-9a-f]{40}$')
B58_RE = re.compile(r'^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$')
SOL_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
XRP_RE = re.compile(r'^r[1-9A-HJ-NP-Za-km-z]{24,34}$')
XRP_MIN_DROPS = 1_000_000 * 1_000_000  # >= 1 M XRP
BECH_RE = re.compile(r'^bc1[ac-hj-np-z02-9]{11,71}$')
TODAY = '2026-11-06'
MIN_TX = 1000


def _key():
    for line in open('/root/openclaw/.env'):
        if line.startswith('ETHERSCAN_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"\'')
    sys.exit('no ETHERSCAN_API_KEY')


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'User-Agent': 'openclaw-crypto-flow/1.0', 'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _sol_rpc():
    key = os.environ.get('HELIUS_API_KEY')
    if not key:
        for line in open('/root/openclaw/.env'):
            if line.startswith('HELIUS_API_KEY='):
                key = line.split('=', 1)[1].strip().strip('"\'')
    return f'https://mainnet.helius-rpc.com/?api-key={key}' if key else 'https://api.mainnet-beta.solana.com'


def _get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'openclaw-crypto-flow/1.0'})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def fmt_ok(chain, a):
    if chain in EVM:
        return bool(EVM_RE.match(a))
    if chain == 'BTC':
        return bool(B58_RE.match(a) or BECH_RE.match(a))
    if chain == 'SOL':
        return bool(SOL_RE.match(a))
    if chain == 'XRP':
        return bool(XRP_RE.match(a))
    return True


def activity(chain, a, key):
    """Return (n_txs, err). err is None when the answer is trustworthy."""
    try:
        if chain == 'BTC':
            for attempt in (0, 1):
                time.sleep(1.0 if attempt == 0 else 5.0)
                try:
                    return _get(f'https://mempool.space/api/address/{a}')['chain_stats']['tx_count'], None
                except urllib.error.HTTPError as e:
                    if e.code != 429 or attempt:
                        return None, f'HTTP{e.code}'
        if chain == 'XRP':
            r = _post('https://xrplcluster.com/', {'method': 'account_info', 'params': [{'account': a}]})['result']
            if 'account_data' not in r:
                return (0, None) if r.get('error') == 'actNotFound' else (None, str(r.get('error'))[:40])
            # balance >= 1 M XRP counts as active; encode as MIN_TX so the shared threshold passes
            return (MIN_TX if int(r['account_data']['Balance']) >= XRP_MIN_DROPS else 0), None
        if chain == 'SOL':
            time.sleep(0.2)
            r = _post(_sol_rpc(), {'jsonrpc': '2.0', 'id': 1, 'method': 'getSignaturesForAddress',
                                   'params': [a, {'limit': 1000}]})
            return (len(r['result']), None) if isinstance(r.get('result'), list) else (None, 'rpc_error')
        if chain != 'ETH':
            return None, 'unsupported chain'
        for attempt in (0, 1):
            time.sleep(0.25 if attempt == 0 else 1.5)
            r = _get('https://api.etherscan.io/v2/api?chainid=1&module=account&action=txlist'
                     f'&address={a}&page=1&offset=1000&sort=desc&apikey={key}')
            if r.get('status') == '1':
                return len(r['result']), None
            err = str(r.get('result') or r.get('message'))
            if r.get('message') == 'No transactions found':
                return 0, None
        return None, err.replace(key, '***')[:80]
    except Exception as e:
        return None, f'{type(e).__name__}'


def main():
    d = json.loads(PATH.read_text())
    only = set(sys.argv[1:])  # optional: validate just these chains, keep the rest untouched
    key = _key() if not only or 'ETH' in only else ''
    out = {'_meta': dict(d['_meta'], validated=TODAY, validator='scripts/validate_exchange_addresses.py')}
    rejected = list(d.get('_rejected', []))
    before, after = {}, {}
    for chain, owners in d.items():
        if chain.startswith('_'):
            continue
        if only and chain not in only:
            out[chain] = owners
            continue
        out[chain] = {}
        for owner, addrs in owners.items():
            for a in addrs:
                before[chain] = before.get(chain, 0) + 1
                reason = None
                if not fmt_ok(chain, a):
                    reason = 'format'
                else:
                    n, err = activity(chain, a, key)
                    if err:
                        reason = f'unverified:{err}'
                    elif n < MIN_TX:
                        reason = f'inactive:{n}'
                if reason:
                    rejected.append({'chain': chain, 'owner': owner, 'address': a, 'reason': reason, 'checked': TODAY})
                else:
                    out[chain].setdefault(owner, []).append(a)
                    after[chain] = after.get(chain, 0) + 1
                print(chain, owner, a[:12], reason or 'ok', flush=True)
    out['_rejected'] = rejected
    PATH.write_text(json.dumps(out, indent=1))
    print('SUMMARY before', before, 'after', after, 'rejected', len(rejected))


if __name__ == '__main__':
    main()
