from src.ingestion.crypto_flow import universe as u

def test_universe_file_has_all_36_alpaca_pairs_and_roles():
    assets = u.load_universe(active_only=False)
    tickers = {a.ticker for a in assets}
    assert {'BTC-USD','ETH-USD','SOL-USD','XRP-USD','DOGE-USD','USDC-USD','USDT-USD','USDG-USD','PAXG-USD'} <= tickers
    assert len(tickers) == 36
    roles = {a.ticker: a.role for a in assets}
    assert roles['BTC-USD'] == 'benchmark' and roles['ETH-USD'] == 'alt'
    assert roles['USDC-USD'] == 'stable' and roles['PAXG-USD'] == 'commodity'

def test_evm_names_carry_chainid_and_contract():
    m = u.by_ticker(u.load_universe())
    assert m['ETH-USD'].evm_chainid == 1 and m['ETH-USD'].token_contract is None
    assert m['LINK-USD'].evm_chainid == 1 and m['LINK-USD'].token_contract.lower() == '0x514910771af9ca656af840dff83e8264ecf986ca'
    assert m['ARB-USD'].evm_chainid == 42161 and m['AVAX-USD'].evm_chainid == 43114 and m['POL-USD'].evm_chainid == 137

def test_normalize_broker_symbol_all_three_forms():
    for s in ('BTCUSD', 'BTC/USD', 'BTC-USD'):
        assert u.normalize_broker_symbol(s) == 'BTC-USD'
    assert u.normalize_broker_symbol('SPY') is None
    assert u.normalize_broker_symbol('AAPL260117C00150000') is None
    assert u.normalize_broker_symbol('') is None

def test_refresh_adds_and_deactivates_but_never_removes():
    existing = u.load_universe(active_only=False)
    merged, added, deact = u.refresh_from_alpaca(existing, ['BTC/USD', 'NEW/USD'])
    assert 'NEW-USD' in added and 'ETH-USD' in deact
    assert len(merged) == len(existing) + 1
    assert next(a for a in merged if a.ticker == 'ETH-USD').active is False

def test_load_universe_active_only_drops_inactive_rows(tmp_path):
    p = tmp_path / 'u.json'
    p.write_text('{"assets": [{"ticker":"A-USD","alpaca":"A/USD","position":"AUSD","role":"alt","active":false},'
                 '{"ticker":"B-USD","alpaca":"B/USD","position":"BUSD","role":"alt","active":true}]}')
    assert [a.ticker for a in u.load_universe(p)] == ['B-USD']
    assert [a.ticker for a in u.load_universe(p, active_only=False)] == ['A-USD', 'B-USD']

def test_normalize_matches_inactive_rows_and_explicit_assets():
    only = [a for a in u.load_universe(active_only=False) if a.ticker == 'ETH-USD']
    assert u.normalize_broker_symbol('ethusd', only) == 'ETH-USD'
    assert u.normalize_broker_symbol('BTCUSD', only) is None

def test_refresh_reactivates_returning_pair_and_is_idempotent():
    existing = u.load_universe(active_only=False)
    all_syms = [a.alpaca for a in existing]
    merged, added, deact = u.refresh_from_alpaca(existing, all_syms)
    assert added == [] and deact == [] and merged == existing
