from strategies.option_direction import normalize_option_direction as nd


def test_long_aliases():
    for d in ('long', 'LONG', 'buy', 'BUY', 'buy_vol', 'BUY_VOL'):
        assert nd(d) == 'long'


def test_short_aliases():
    for d in ('short', 'SHORT', 'sell', 'SELL', 'sell_vol', 'SELL_VOL'):
        assert nd(d) == 'short'


def test_unknown_failsclosed():
    assert nd('sideways') is None
    assert nd(None) is None
    assert nd('') is None
