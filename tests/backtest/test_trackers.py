import pandas as pd
import pytest

from backtest.trackers import (
    BacktestContext, ConsistencyTracker, ExitCensusTracker, FinishedBacktest,
    HookTracker, InconsistentBacktest, IntradayBarTracker, MaxRuntimeTracker,
    TrackerCollection,
)


def _ctx(trades=None, counters=None, n=3):
    return BacktestContext(
        instance=None, close_wide=pd.DataFrame(), strategy_id='S',
        oos_dates=pd.date_range('2024-01-01', periods=n),
        trades=[] if trades is None else trades,
        counters={} if counters is None else counters)


class Rec:
    def __init__(self, name, log):
        self.name, self.log = name, log

    def initialize(self, ctx):
        self.log.append((self.name, 'init'))

    def handle(self, ctx, i):
        self.log.append((self.name, i))

    def finalize(self, info):
        info[self.name] = True


def test_collection_calls_in_order_and_finalize_merges():
    log = []
    c = TrackerCollection([Rec('a', log), Rec('b', log)])
    ctx = _ctx()
    c.initialize(ctx)
    c.handle(ctx, 0)
    assert log == [('a', 'init'), ('b', 'init'), ('a', 0), ('b', 0)]
    assert c.finalize() == {'a': True, 'b': True}
    assert c.names() == ['a', 'b']
    assert ctx.started > 0


def test_finished_backtest_propagates_from_handle():
    class Stop(Rec):
        def handle(self, ctx, i):
            raise FinishedBacktest('stop')
    c = TrackerCollection([Stop('x', [])])
    with pytest.raises(FinishedBacktest) as ei:
        c.handle(_ctx(), 0)
    assert ei.value.reason == 'stop' and str(ei.value) == 'stop'


def test_exit_census_uses_injected_fn_on_ctx_trades():
    ctx = _ctx(trades=[{'x': 1}])
    t = ExitCensusTracker(lambda tr: {'n': len(tr)})
    t.initialize(ctx)
    ctx.trades.append({'x': 2})
    info = {}
    t.finalize(info)
    assert info['exit_reasons'] == {'n': 2}


def test_hook_tracker_defaults_when_no_hook_counts():
    t = HookTracker()
    t.initialize(_ctx())
    info = {}
    t.finalize(info)
    assert (info['hook_exits'], info['hook_raised'], info['first_hook_raise']) == (0, 0, None)


def test_hook_tracker_reads_counters():
    ctx = _ctx(counters={'hook_exits': 3, 'hook_raised': 1, 'first_hook_raise': 'E: x'})
    t = HookTracker()
    t.initialize(ctx)
    info = {}
    t.finalize(info)
    assert (info['hook_exits'], info['hook_raised'], info['first_hook_raise']) == (3, 1, 'E: x')


def test_intraday_tracker_reads_shared_counters():
    ctx = _ctx()
    t = IntradayBarTracker()
    t.initialize(ctx)
    ctx.counters['intraday_no_bars'] = ctx.counters.get('intraday_no_bars', 0) + 1
    ctx.counters['intraday_no_bars'] += 1
    info = {}
    t.finalize(info)
    assert info == {'intraday_trades': 0, 'intraday_no_bars': 2,
                    'intraday_missing_entry_bar': 0}


def test_max_runtime_with_fake_clock():
    clock = iter([0, 5, 11]).__next__
    ctx = _ctx()
    ctx.started = 0
    t = MaxRuntimeTracker(10, clock=clock)
    t.initialize(ctx)
    t.handle(ctx, 0)
    ctx.current_date = pd.Timestamp('2024-01-02')
    t.handle(ctx, 1)
    with pytest.raises(FinishedBacktest) as ei:
        t.handle(ctx, 2)
    assert 'max_runtime' in str(ei.value) and 'budget 10s' in str(ei.value)
    assert 'at bar 2/3' in str(ei.value)
    info = {}
    t.finalize(info)
    assert info['elapsed_s'] == 11


def test_consistency_raises_on_nan_mark_and_on_nan_pnl():
    nan = float('nan')
    ctx = _ctx(trades=[{'ticker': 'A', 'entry_date': '2024-01-02', 'pnl_pct': 0.1,
                        'daily_marks': [('d', 0.0), ('d', nan)]}])
    t = ConsistencyTracker()
    t.initialize(ctx)
    with pytest.raises(InconsistentBacktest, match='nonfinite: A 2024-01-02'):
        t.handle(ctx, 0)
    ctx2 = _ctx(trades=[{'ticker': 'B', 'entry_date': 'e', 'pnl_pct': nan}])
    t2 = ConsistencyTracker()
    t2.initialize(ctx2)
    with pytest.raises(InconsistentBacktest, match='pnl_pct'):
        t2.handle(ctx2, 0)


def test_consistency_scans_each_trade_once():
    ctx = _ctx(trades=[{'ticker': 'A', 'entry_date': 'e', 'pnl_pct': 0.1, 'daily_marks': []}])
    t = ConsistencyTracker()
    t.initialize(ctx)
    t.handle(ctx, 0)
    assert t._seen == 1
    ctx.trades[0]['pnl_pct'] = float('nan')  # already scanned: not re-read
    t.handle(ctx, 1)
    ctx.trades.append({'ticker': 'B', 'entry_date': 'e', 'pnl_pct': 0.2})
    t.handle(ctx, 2)
    assert t._seen == 2
    info = {}
    t.finalize(info)
    assert info['consistency_checked'] == 2


def test_consistency_tolerates_missing_or_none_pnl():
    ctx = _ctx(trades=[{'ticker': 'A', 'entry_date': 'e'},
                       {'ticker': 'B', 'entry_date': 'e', 'pnl_pct': None}])
    t = ConsistencyTracker()
    t.initialize(ctx)
    t.handle(ctx, 0)
    assert t._seen == 2


def test_consistency_malformed_mark_is_inconsistent_not_typeerror():
    for bad in (5, (), ('d',), ('d', 'x')):
        ctx = _ctx(trades=[{'ticker': 'A', 'entry_date': 'e', 'pnl_pct': 0.1,
                            'daily_marks': [bad]}])
        t = ConsistencyTracker()
        t.initialize(ctx)
        with pytest.raises(InconsistentBacktest):
            t.handle(ctx, 0)
