"""Pure per-ticker consolidation function for LOW_VOL/TRANSITIONING mode.

Formula (spec §"Locked design decisions"):
  ER_Weight           = strategy_memo_mult × signal_kelly_frac
  SignalPositionSize  = ER_Weight × regt_buying_power × λ(regime)
  TickerPositionSize  = Σ_signals (SignalPositionSize × SignalDirection)
  Direction           = sign(TickerPositionSize)

Bracket: from the largest-notional signal in the winning direction.
Direction-tie: emit no order (`direction_tie_net_zero` audit reason).
"""
from __future__ import annotations
from collections import defaultdict

def _direction_leader_bracket(signals: list[dict], winning_direction: int) -> dict:
    same_dir = [s for s in signals if s['direction'] == winning_direction]
    leader = max(same_dir, key=lambda s: s.get('strategy_memo_mult', 1.0) * s.get('kelly_p', 0))
    return {
        'entry_price': leader['entry_price'],
        'stop_loss': leader['stop_loss'],
        'take_profit_1': leader['take_profit_1'],
    }

def consolidate(signals: list[dict], regt_buying_power: float, params: dict) -> list[dict]:
    """Group signals by ticker, apply formula, emit one virtual order per ticker.

    Returns list of {ticker, direction, preliminary_size_usd, qty_signed_usd,
    contributions, bracket}. Vetoed tickers (direction-tie or below min-notional)
    are NOT in the return — caller writes audit rows separately if needed.
    """
    lambda_regime = params['liquidity_param']
    min_notional = params['min_signal_notional_usd']

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for sig in signals:
        kelly_p = float(sig.get('kelly_p', 0.0))
        memo_mult = float(sig.get('strategy_memo_mult', 1.0))
        er_weight = kelly_p * memo_mult
        signed_size = er_weight * regt_buying_power * lambda_regime * sig['direction']
        sig_with_size = {**sig, 'signed_signal_size_usd': signed_size,
                         'unsigned_signal_size_usd': abs(signed_size)}
        by_ticker[sig['ticker']].append(sig_with_size)

    out = []
    for ticker, ticker_sigs in by_ticker.items():
        net = sum(s['signed_signal_size_usd'] for s in ticker_sigs)
        gross = sum(s['unsigned_signal_size_usd'] for s in ticker_sigs)

        if abs(net) < 1e-6 or net == 0:
            # Direction tie → no trade.
            continue
        if abs(net) < min_notional:
            continue

        direction = 1 if net > 0 else -1
        bracket = _direction_leader_bracket(ticker_sigs, direction)

        contributions = []
        for s in ticker_sigs:
            weight = s['unsigned_signal_size_usd'] / gross if gross > 0 else 0.0
            contributions.append({
                'contributing_signal_id': s.get('signal_id'),
                'strategy_id': s['strategy_id'],
                'signal_position_size_usd': s['unsigned_signal_size_usd'],
                'attribution_weight': weight,
                'contributed_direction': s['direction'],
            })

        out.append({
            'ticker': ticker,
            'direction': direction,
            'preliminary_size_usd': abs(net),
            'qty_signed_usd': net,
            'bracket': bracket,
            'contributions': contributions,
        })

    return out
