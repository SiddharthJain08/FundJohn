"""
Window Dressing by Pension Fund Managers — Lakonishok, Shleifer, Thaler &
Vishny (1991). Source: https://doi.org/10.3386/w3617

Pension funds accelerate selling of YTD losers heading into Q4 to window-
dress portfolios ahead of sponsor review, creating institutional-flow-
driven downward price pressure on losers unrelated to fundamentals, which
should mean-revert once the Q4 selling pressure lifts (turn-of-year
reversal). LONG only, per the paper's direction_vocab.

Variant 2/2 — the abstract pins the ranking mechanics exactly (rank at the
start of Q4 on trailing Jan1->Oct1 return, bottom decile = losers) but is
vague on the exact trade dates ("entering ~late Dec", "exit ... ~mid-to-
late Jan"). This variant deliberately reads those two windows differently
from a colleague's plausible reading:
  - entry = first trading day on/after Dec 20 (the LATE stage of the Q4
    selling acceleration the paper documents — not the very last trading
    day of the year, which risks missing most of the pre-reversal dip)
  - exit  = first trading day on/after Jan 15 (the EARLY half of "mid-to-
    late Jan" — a colleague could just as defensibly run the full month
    to Jan 31; this variant books the reversal before any January
    effect decay sets back in)
  - ATR stop multiplier tightened to 1.5x (vs. the 2.0x BaseStrategy
    default) to reflect the ~4-week expected hold here, rather than the
    multi-month hold other reversal strategies in this book use
The Jan1->Oct1 formation window, bottom-decile cut, and LONG-only
direction are unambiguous in the abstract and kept as-is.
"""
from __future__ import annotations
import sys
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal
from src.strategies.universe_default import sp500 as universe_filter

__all__ = ['WindowDressLoserReversal']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_window_dress_loser_reversal'

ENTRY_MONTH_DAY      = (12, 20)  # first trading day on/after Dec 20
EXIT_MONTH_DAY       = (1, 15)   # first trading day on/after Jan 15 (next year)
FORMATION_MONTH_DAY  = (10, 1)   # ranking snapshot: first trading day on/after Oct 1
YEAR_START_MONTH_DAY = (1, 1)    # trailing-9m base: first trading day on/after Jan 1
DECILE_FRAC    = 0.10
MIN_UNIVERSE   = 100              # paper's minimum_universe_size
BASE_SIZE      = 0.02
ATR_MULTIPLIER = 1.5              # variant 1: tightened for the ~4wk hold


def _equity_trading_days(prices: pd.DataFrame) -> pd.DatetimeIndex:
    """`prices.index` is a union across every instrument in the panel,
    including 24/7 instruments (crypto) that trade on calendar days the
    equity market is closed (e.g. Jan 1). Anchoring Jan1/Oct1 against the
    raw index can land on an equity-closed day where only a handful of
    non-equity tickers have data, collapsing the cross-section. Restrict
    to days where a meaningful fraction of the panel actually has equity
    prints."""
    counts = prices.notna().sum(axis=1)
    threshold = max(MIN_UNIVERSE, int(0.05 * prices.shape[1]))
    return prices.index[counts >= threshold]


def _first_td_on_or_after(idx: pd.DatetimeIndex, year: int, month: int, day: int):
    target = pd.Timestamp(year=year, month=month, day=day)
    candidates = idx[idx >= target]
    return candidates[0] if len(candidates) else None


def _is_first_td_on_or_after(idx: pd.DatetimeIndex, today: pd.Timestamp, target: pd.Timestamp) -> bool:
    """True iff `today` is the first element of `idx` that is >= `target`.
    Only meaningful when `idx` already includes `today` (live daily calls
    only ever see history up to and including the current bar — never the
    future — so this never looks past `today` to resolve a future calendar
    day the way a forward `_first_td_on_or_after` search over all of `idx`
    would need to)."""
    if today < target:
        return False
    prior = idx[idx < today]
    return len(prior) == 0 or prior[-1] < target


def _empty(msg: str) -> List[Signal]:
    print(msg, file=sys.stderr)
    print('[debug] signals=0', file=sys.stderr)
    return []


def _losers_for_campaign(prices: pd.DataFrame, universe: List[str], year: int):
    """Bottom-decile YTD losers as of the Oct-1 formation date of `year`,
    ranked on trailing Jan1->Oct1 return. Returns (losers Series, formation_date) or None."""
    idx = _equity_trading_days(prices)
    jan1 = _first_td_on_or_after(idx, year, *YEAR_START_MONTH_DAY)
    oct1 = _first_td_on_or_after(idx, year, *FORMATION_MONTH_DAY)
    if jan1 is None or oct1 is None or jan1 >= oct1:
        return None
    tickers = [t for t in universe if t in prices.columns]
    if len(tickers) < MIN_UNIVERSE:
        return None
    start_px = prices.loc[jan1, tickers]
    end_px   = prices.loc[oct1, tickers]
    cum_ret  = (end_px / start_px) - 1.0
    cum_ret  = cum_ret.replace([float('inf'), float('-inf')], float('nan')).dropna()
    if len(cum_ret) < MIN_UNIVERSE:
        return None
    n = len(cum_ret)
    decile_n = max(1, int(n * DECILE_FRAC))
    losers = cum_ret.sort_values().iloc[:decile_n]
    return losers, oct1


class WindowDressLoserReversal(BaseStrategy):
    """LONG the bottom-decile YTD losers (ranked Jan1->Oct1 trailing return)
    from first trading day on/after Dec 20 through first trading day
    on/after Jan 15, betting on the turn-of-year reversal once pension-fund
    Q4 window-dressing selling pressure lifts (Lakonishok, Shleifer, Thaler
    & Vishny 1991)."""

    id                = STRATEGY_ID
    name              = 'WindowDressLoserReversal'
    description       = (
        'LONG bottom-decile YTD losers (ranked Jan1->Oct1) from late Dec '
        'through mid-Jan, betting on the turn-of-year reversal once '
        'pension-fund window-dressing selling pressure lifts.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    calendar_edge     = True   # window IS the signal; ports across regime flips
    min_lookback      = 210    # ~Jan1->Oct1 trading days
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {'base_size': BASE_SIZE, 'atr_multiplier': ATR_MULTIPLIER}

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        today = prices.index[-1]
        if today.month not in (12, 1):
            return []  # outside the Dec20->Jan15 campaign window entirely

        campaign_year = today.year if today.month == 12 else today.year - 1
        idx          = _equity_trading_days(prices)
        target_entry = pd.Timestamp(year=campaign_year, month=ENTRY_MONTH_DAY[0], day=ENTRY_MONTH_DAY[1])
        target_exit  = pd.Timestamp(year=campaign_year + 1, month=EXIT_MONTH_DAY[0], day=EXIT_MONTH_DAY[1])

        if today < target_entry:
            return []  # window hasn't opened yet (Dec, before Dec 20)
        # entry_date is always resolvable — Dec 20 of campaign_year is already
        # in the past relative to `today`, whether today is in Dec or Jan, so
        # this forward search never has to look past data we already have.
        entry_date = _first_td_on_or_after(idx, campaign_year, *ENTRY_MONTH_DAY)
        is_entry   = _is_first_td_on_or_after(idx, today, target_entry)

        if today.month == 12:
            # target_exit is next calendar year's data, which `today` (still
            # in December) can't see yet — exit is evaluated only in January.
            is_exit = False
        elif today < target_exit:
            is_exit = False  # January, still inside the hold window
        else:
            is_exit = _is_first_td_on_or_after(idx, today, target_exit)
            if not is_exit:
                return []  # already exited on a prior trading day this January

        picked = _losers_for_campaign(prices, universe, campaign_year)
        if picked is None:
            return _empty(f'[{STRATEGY_ID}] campaign {campaign_year} formation failed (universe/history)')
        losers, formation_date = picked

        scale     = self.position_scale(regime_state)
        base_size = float(self.parameters.get('base_size', BASE_SIZE))
        atr_mult  = float(self.parameters.get('atr_multiplier', ATR_MULTIPLIER))

        signals: List[Signal] = []
        for ticker, ret in losers.items():
            px_val = prices[ticker].loc[today]
            if pd.isna(px_val) or float(px_val) <= 0:
                continue
            px = float(px_val)
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue

            if is_exit:
                signals.append(Signal(
                    ticker=ticker, direction='FLAT', entry_price=px,
                    stop_loss=round(px * 0.95, 4),
                    target_1=px, target_2=px, target_3=px,
                    position_size_pct=0.0, confidence='HIGH',
                    signal_params={
                        'trigger': 'window_dress_exit', 'campaign_year': campaign_year,
                        'formation_date': str(formation_date.date()), 'regime': regime_state,
                    },
                ))
            else:
                st = self.compute_stops_and_targets(
                    series, 'LONG', px, atr_multiplier=atr_mult, regime_state=regime_state)
                signals.append(Signal(
                    ticker=ticker, direction='LONG', entry_price=px,
                    stop_loss=float(st['stop']), target_1=float(st['t1']),
                    target_2=float(st['t2']), target_3=float(st['t3']),
                    position_size_pct=round(base_size * scale, 4),
                    confidence='HIGH' if is_entry else 'MED',
                    signal_params={
                        'trigger': 'window_dress_entry' if is_entry else 'window_dress_hold',
                        'campaign_year': campaign_year,
                        'formation_date': str(formation_date.date()),
                        'cum_return_jan_oct': round(float(ret), 4),
                        'regime': regime_state,
                    },
                ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        print(
            f'[{STRATEGY_ID}] today={today.date()} campaign={campaign_year} '
            f'entry={entry_date.date()} is_entry={is_entry} '
            f'is_exit={is_exit} n_losers={len(losers)} signals={len(signals)} regime={regime_state}',
            file=sys.stderr,
        )
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import os, json as _json
    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    long_df = pd.read_parquet(f'{PARQUET_ROOT}/prices.parquet', columns=['ticker', 'date', 'close'])
    long_df['date'] = pd.to_datetime(long_df['date'])
    prices_df = long_df.pivot(index='date', columns='ticker', values='close').sort_index()

    reg_df = pd.read_parquet(f'{PARQUET_ROOT}/historical_regimes.parquet')
    reg_df['date'] = pd.to_datetime(reg_df['date'])
    reg_df = reg_df.sort_values('date')

    def _regime_on(dt):
        mask = reg_df['date'] <= dt
        return str(reg_df.loc[mask, 'regime'].iloc[-1]) if mask.any() else 'LOW_VOL'

    trading_idx = _equity_trading_days(prices_df)
    years = sorted(set(prices_df.index.year))
    rows = []
    for y in years:
        picked = _losers_for_campaign(prices_df, list(prices_df.columns), y)
        if picked is None:
            continue
        losers, formation_date = picked
        entry_date = _first_td_on_or_after(trading_idx, y, *ENTRY_MONTH_DAY)
        exit_date  = _first_td_on_or_after(trading_idx, y + 1, *EXIT_MONTH_DAY)
        if entry_date is None or exit_date is None:
            continue
        reg = _regime_on(entry_date)
        for ticker in losers.index:
            entry_px = prices_df[ticker].get(entry_date)
            exit_px  = prices_df[ticker].get(exit_date)
            if entry_px is None or exit_px is None or pd.isna(entry_px) or pd.isna(exit_px) or float(entry_px) <= 0:
                continue
            pnl = (float(exit_px) - float(entry_px)) / float(entry_px)
            rows.append({
                'strategy_id': STRATEGY_ID, 'signal_date': str(entry_date.date()),
                'regime_state': reg, 'pnl': pnl, 'r_multiple': round(pnl / 0.02, 4),
            })

    trades_df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    print(f'[backtest] completed_trades={len(trades_df)}', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 3, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
