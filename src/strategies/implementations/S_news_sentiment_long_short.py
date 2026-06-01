"""S_news_sentiment_long_short — sentiment-primary cross-sectional long/short.

Primary signal = FinBERT news sentiment (news_flow.score). LONG strongly-positive names,
SHORT strongly-negative names, requiring a minimum article count to suppress single-headline
noise. Reads aux_data['sentiment'] (historical backfill, point-in-time).

CAVEAT (documented in spec): backtest signal is NEWS-ONLY; the live signal additionally
includes Reddit/StockTwits social sentiment, which has no historical record to backtest.
Zero LLM tokens.
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal
from strategies.confirmation import news_flow as nf

INSTRUMENT_CLASS = 'equity'


class NewsSentimentLongShort(BaseStrategy):
    id = 'S_news_sentiment_long_short'
    name = 'News-Sentiment Long/Short'
    description = 'Sentiment-primary cross-sectional long/short from FinBERT news (news-only in backtest)'
    tier = 3
    signal_frequency = 'daily'
    min_lookback = 20
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    data_requirements = ['prices', 'sentiment']

    def default_parameters(self) -> dict:
        return {'min_articles': 2, 'long_thresh': 0.3, 'short_thresh': -0.3, 'max_each': 20}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        sent = (aux_data or {}).get('sentiment', {})
        if not sent:
            return []
        p = self.parameters
        scale = self.position_scale(regime_state)

        scored = []
        for t in universe:
            if t not in prices.columns:
                continue
            sc = nf.score(sent.get(t), p)
            if sc >= p['long_thresh']:
                scored.append((t, 'LONG', sc))
            elif sc <= p['short_thresh']:
                scored.append((t, 'SHORT', sc))

        longs = sorted([x for x in scored if x[1] == 'LONG'], key=lambda x: x[2], reverse=True)[:p['max_each']]
        shorts = sorted([x for x in scored if x[1] == 'SHORT'], key=lambda x: x[2])[:p['max_each']]

        signals: List[Signal] = []
        for t, direction, sc in longs + shorts:
            ts = prices[t].dropna()
            if len(ts) < 2:
                continue
            cur = float(ts.iloc[-1])
            if cur <= 0:
                continue
            stops = self.compute_stops_and_targets(ts, direction, cur, regime_state=regime_state)
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=cur,
                stop_loss=stops['stop'], target_1=stops['t1'],
                target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=round((1.0 / max(p['max_each'], 1)) * scale, 4),
                confidence='MED',
                signal_params={'sentiment': round(float(sc), 4),
                               'news_count': (sent.get(t) or {}).get('news_count_24h')},
            ))
        return signals[:self.MAX_SIGNALS]
